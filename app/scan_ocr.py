"""Bounded local OCR; original uploads remain untouched and are never sent out."""
from __future__ import annotations
import json,os,subprocess,sys,tempfile,threading,time
from contextvars import ContextVar
from pathlib import Path

_PROGRESS=ContextVar('scan_ocr_progress',default=None)
_SLOTS=threading.BoundedSemaphore(2)
TIMEOUT=600
STAGE_TIMEOUT=120
WARNING='Цены прочитаны со скана (OCR). Числа проверены повторным OCR или сравнением контуров с проверенными образцами этого документа. Ошибки распознавания всё равно возможны. Проверьте маршрут, дату и цены по страницам оригинала перед подтверждением.'


def needed(raw):
    from .tariff_documents import pdf_reader
    # Text PDFs retain their existing parser and do not pay the OCR cost.
    return not any((p.extract_text() or '').strip() for p in pdf_reader(raw).pages[:2])


def prepare(raw,company,origin=None,destination=None):
    from .tariff_documents import _SESSION
    cache=_SESSION.get();key=('ocr',raw,company)
    if cache is not None and key in cache:return cache[key]
    # A full-library result can serve every route in this parsing session.
    # A route-only result must never be used as the document's complete index.
    if destination:key=(*key,origin,destination)
    if cache is not None and key in cache:return cache[key]
    if company!='ДЛ':raise ValueError('В PDF нет текстового слоя. OCR поддерживает скан первой межтерминальной таблицы ДЛ. Для другого макета используйте текстовый PDF или XLSX-шаблон.')
    from . import ocr_cache
    saved=ocr_cache.get(raw,company,origin,destination)
    if saved is not None:
        if _PROGRESS.get():_PROGRESS.get()({'done':0,'total':0,'message':'Использую проверенное распознавание этого же файла'})
        if cache is not None:cache[key]=saved
        return saved
    base=Path(__file__).resolve().parent.parent
    if not all((base/'data/ocr'/f'{lang}.traineddata').is_file() for lang in ('rus','eng')):raise ValueError('Не найдены модели OCR. Распакуйте весь архив приложения, включая data/ocr.')
    try:import pymupdf;from PIL import Image
    except ImportError as exc:raise ValueError('Не установлены компоненты OCR. Перезапустите START_WINDOWS.cmd или установите requirements.txt.') from exc
    if not _SLOTS.acquire(blocking=False):raise ValueError('Уже распознаются два скана. Дождитесь завершения и повторите загрузку.')
    try:
        with tempfile.TemporaryDirectory(prefix='tariff-ocr-') as directory:
            root=Path(directory);source=root/'source.pdf';target=root/'result.json';progress=root/'progress.json';source.write_bytes(raw)
            # Process isolation, hard timeout, no shell, no network or shared PDF state.
            env={**os.environ,'OMP_THREAD_LIMIT':'1','PYTHONUTF8':'1'}
            selection={'origin_filter':origin,'destination':destination} if destination else {}
            process=subprocess.Popen([sys.executable,'-m','app.scan_ocr_worker',str(source),str(target),str(progress),json.dumps(selection,ensure_ascii=False)],cwd=base,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            deadline=time.monotonic()+TIMEOUT;last=None;changed=time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic()>deadline:raise ValueError('OCR занял больше 10 минут. Разделите PDF на части и загрузите их вместе.')
                    if progress.exists():
                        try:
                            status=json.loads(progress.read_text(encoding='utf-8'))
                            if status!=last:
                                changed=time.monotonic();last=status
                                if _PROGRESS.get():_PROGRESS.get()(status)
                        except (OSError,ValueError):pass
                    if time.monotonic()-changed>STAGE_TIMEOUT:
                        stage=(last or {}).get('message','Запуск OCR')
                        raise ValueError(f'OCR остановлен: этап «{stage}» не завершился за {STAGE_TIMEOUT} секунд. Цены не применены. Попробуйте более чёткий PDF или текстовый PDF/XLSX; если ошибка повторяется, проверьте нагрузку и память сервера.')
                    time.sleep(.15)
                if process.returncode or not target.exists():raise ValueError('OCR не завершился. Проверьте установку зависимостей и свободную память; попробуйте PDF меньшего размера.')
                result=json.loads(target.read_text(encoding='utf-8'))
                if result.get('error'):raise ValueError(result['error'])
            finally:
                if process.poll() is None:
                    process.kill();process.wait(timeout=5)
        ocr_cache.put(raw,company,result,origin,destination)
        if cache is not None:cache[key]=result
        return result
    finally:_SLOTS.release()


def pairs(result,origin=None):
    from .cities import normalize_city
    if origin and normalize_city(origin)!=result['origin']:
        raise ValueError(f"В заголовке скана ДЛ указано отправление из {result['origin']}, а выбран город {origin}. Выберите {result['origin']} или автоматическое определение города.")
    return [(result['origin'],r['destination']) for r in result['rows'] if r['destination']!=result['origin']]


def parse(result,origin,destination):
    from .online_tariffs import values_from_tiers
    from .scan_ocr_worker import KG_BOUNDS,FIXED_BOUNDS
    pairs(result,origin)
    row=next((r for r in result['rows'] if r['destination']==destination),None)
    if not row:
        error=next((e['message'] for e in result['errors'] if e.get('destination')==destination),None)
        raise ValueError(error or f'В скане ДЛ не найдена однозначная строка {origin} → {destination}')
    vector=any(c.get('method')=='vector_shape' for c in row.get('ocr_cells',[]))
    nums=row['numbers']
    values=values_from_tiers([(*b,v) for b,v in zip(KG_BOUNDS,nums[6:])],fixed=[(*b,v) for b,v in zip(FIXED_BOUNDS,nums[:5])],minimum=nums[5])
    return values,{'parser':'ДЛ · скан межтерминальной таблицы · OCR','source_page':row['source_page'],
                  'document_date':result.get('document_date'),'tax_basis':result.get('tax_basis'),
                  'ocr_cache_hit':bool(result.get('ocr_cache_hit')),'ocr_vector_verified':vector,
                  'route_verified':True,'ocr':True,'ocr_pages':result['ocr_pages'],'ocr_cells':row['ocr_cells'],
                  'calculation_basis':('Контуры цифр сопоставлены с образцами, дважды прочитанными OCR в этом документе; на каждой странице проверена контрольная строка. ' if vector else 'OCR: два чтения чисел совпали; требуется проверка по оригиналу. ')+ 'ДЛ: фиксированные суммы до 35 кг (объём до 0,1 м³), далее max(минимум, вес × руб/кг). До 10 000 кг, без адресной доставки и допуслуг.'}
