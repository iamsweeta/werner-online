"""Durable asynchronous previews for One route. HTTP never waits for OCR.

SQLite or PostgreSQL persists job status and idempotency across restarts.
Only explicit /api/import/commit applies a preview to the price library.
"""
from __future__ import annotations
import hashlib,json,re,sqlite3,threading,time,uuid
from contextlib import contextmanager
from pathlib import Path
from . import cloud_db,data_store
from . import document_imports as imports,tariff_documents as documents

STALE_SECONDS=90
MAX_ACTIVE=2


def root():return imports.root()/'route_jobs'


def ident(value):
    if not re.fullmatch('[a-f0-9]{32}',str(value)):raise ValueError('Некорректный номер задания распознавания')
    return str(value)


@contextmanager
def database(folder=None):
    folder=folder or root();folder.mkdir(parents=True,exist_ok=True)
    from .cloud_db import connect
    with connect(folder/'jobs.sqlite3','importjobs') as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, fingerprint TEXT, company TEXT, origin TEXT, destination TEXT,
          filename TEXT, created REAL, updated REAL, status TEXT, message TEXT, payload TEXT)''')
        yield conn


def _update(folder,key,status=None,message=None,payload=None):
    with database(folder) as conn:
        row=conn.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
        if not row or row['status'] not in ('queued','parsing'):return
        old=json.loads(row['payload']);old.update(payload or {})
        conn.execute('UPDATE jobs SET status=?,message=?,payload=?,updated=? WHERE id=?',
                     (status or row['status'],message or row['message'],json.dumps(old,ensure_ascii=False),time.time(),key))


def _stale(conn):
    conn.execute("UPDATE jobs SET status='interrupted',message=? WHERE status IN ('queued','parsing') AND updated<?",
                 ('Распознавание прервано: сервер перестал отвечать или перезапустился. Повторите распознавание. Сохранённые прайсы не изменены.',time.time()-STALE_SECONDS))


def cleanup():
    folder=root()
    if not cloud_db.enabled() and not (folder/'jobs.sqlite3').exists():return
    with imports.e.STATE_LOCK, database(folder) as conn:
        _stale(conn)
        rows=conn.execute("SELECT id FROM jobs WHERE status NOT IN ('queued','parsing') AND updated<?",(time.time()-imports.PREVIEW_TTL,)).fetchall()
        for row in rows:
            data_store.delete(folder/(row['id']+'.upload'))
            conn.execute('DELETE FROM jobs WHERE id=?',(row['id'],))


def status(key,folder=None):
    key=ident(key);folder=folder or root()
    with database(folder) as conn:
        _stale(conn)
        row=conn.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
    if not row:raise FileNotFoundError('Задание не найдено или предпросмотр истёк. Загрузите файл заново.')
    data=json.loads(row['payload']);state=row['status'];message=row['message']
    if state=='ready':
        preview=data['preview'];meta=preview['meta'];token=preview['token']
        if data_store.exists(folder.parent/'applied'/(token+'.json')) or data_store.exists(folder.parent/'files'/(token+meta['extension'])):state='committed';message='Цены этого маршрута уже применены. Оригинал доступен в библиотеке.'
        elif time.time()-row['updated']>imports.PREVIEW_TTL or not data_store.exists(folder.parent/'pending'/(token+'.json')):
            state='expired';message='Предпросмотр истёк. Загрузите файл заново и проверьте цены.'
    return {'job_id':key,'status':state,'company':row['company'],'origin':row['origin'],'destination':row['destination'],
            'filename':row['filename'],'message':message,**{k:v for k,v in data.items() if k!='preview'},
            'elapsed_seconds':max(0,int(time.time()-row['created'])),
            'stage_elapsed_seconds':max(0,int(time.time()-data.get('ocr_stage_started',row['created']))),
            **({'preview':data['preview']} if state=='ready' else {}),'can_retry':state in {'error','interrupted'} and (bool(data.get('document_id')) or data_store.exists(folder/(key+'.upload')))}


def start(raw,filename,company,origin,destination,job_id=None,document_id=None):
    origin,destination=imports._validate(company,origin,destination)
    if not raw or len(raw)>documents.MAX_BYTES:raise ValueError('Выберите непустой документ размером до 20 МБ')
    name=documents.normalize_filename(filename)
    if Path(name).suffix.lower() not in {'.pdf','.xls','.xlsx','.csv','.zip'}:raise ValueError('Поддерживаются PDF, XLS, XLSX, CSV и ZIP')
    key=ident(job_id) if job_id else uuid.uuid4().hex;folder=root()
    identity=[company,origin,destination,name]
    if document_id:identity.append(document_id)
    fingerprint=hashlib.sha256(raw+json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
    payload=json.dumps({'document_id':document_id} if document_id else {})
    cleanup()
    launch=False
    # Store the input reference and job in the same PostgreSQL transaction.
    # StateLock also lets data_store reuse this connection instead of opening a
    # nested connection while the job transaction holds an advisory lock.
    with imports.e.STATE_LOCK, database(folder) as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing=conn.execute('SELECT fingerprint,status FROM jobs WHERE id=?',(key,)).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint:raise ValueError('Этот номер задания относится к другому файлу или маршруту. Начните новую загрузку.')
            if existing['status'] in {'error','interrupted'}:
                if conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','parsing')").fetchone()[0]>=MAX_ACTIVE:raise ValueError('Дождитесь завершения других документов.')
                conn.execute("UPDATE jobs SET status='queued',message='Повторяю распознавание',payload=?,updated=? WHERE id=?",(payload,time.time(),key))
                launch=True
        else:
            _stale(conn)
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','parsing')").fetchone()[0]>=MAX_ACTIVE:
                raise ValueError('Уже распознаются два документа. Дождитесь завершения и повторите загрузку.')
            if not document_id:data_store.write_bytes(folder/(key+'.upload'),raw)
            now=time.time();conn.execute('INSERT INTO jobs(id,fingerprint,company,origin,destination,filename,created,updated,status,message,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (key,fingerprint,company,origin,destination,name,now,now,'queued','Готовлю распознавание сохранённого файла…' if document_id else 'Файл принят. Готовлю распознавание…',payload))
    if not existing or launch:
        threading.Thread(target=_run,args=(folder,key,str(filename),company,origin,destination,document_id),daemon=True).start()
    return status(key,folder)


def start_saved(document_id,origin,destination,job_id=None):
    from .price_library import original
    meta,path=original(document_id)
    imports._validate(meta['company'],origin,destination)
    # Object storage reads are outside the global price transaction/lock.
    try:raw=data_store.read_bytes(path)
    except FileNotFoundError as exc:
        raise ValueError('Оригинал прайса отсутствует в хранилище. Проверьте подключение или загрузите файл в библиотеку заново.') from exc
    if meta.get('sha256') and hashlib.sha256(raw).hexdigest()!=meta['sha256']:
        raise ValueError('Оригинал не прошёл проверку целостности. Цены не изменены.')
    return start(raw,meta['original_filename'],meta['company'],origin,destination,job_id,document_id)


def _run(folder,key,filename,company,origin,destination,document_id=None):
    from . import scan_ocr
    stop=threading.Event()
    def heartbeat():
        while not stop.wait(10):
            try:_update(folder,key)
            except (OSError,sqlite3.Error,cloud_db.StorageUnavailable):pass
    monitor=threading.Thread(target=heartbeat,daemon=True);monitor.start()
    def progress(info):
        _update(folder,key,message=info['message'],payload={'ocr_page':info['done'],'ocr_total':info['total'],'ocr_stage_started':time.time()})
    context=scan_ocr._PROGRESS.set(progress)
    try:
        _update(folder,key,status='parsing',message='Читаю документ и проверяю выбранное направление…')
        if document_id:
            from .price_library import original
            _,path=original(document_id)
        else:path=folder/(key+'.upload')
        raw=data_store.read_bytes(path)
        result=imports.preview(raw,filename,company,origin,destination,document_id=document_id) if document_id else imports.preview(raw,filename,company,origin,destination)
        _update(folder,key,status='ready',message='Распознавание завершено. Проверьте цены перед сохранением.',payload={'preview':result})
    except Exception as exc:
        _update(folder,key,status='error',message=str(exc)[:1500])
    finally:
        stop.set();monitor.join(timeout=1);scan_ocr._PROGRESS.reset(context)
        try:
            with database(folder) as conn:finished=conn.execute('SELECT status FROM jobs WHERE id=?',(key,)).fetchone()
            if finished and finished['status']=='ready':data_store.delete(folder/(key+'.upload'))
        except Exception:pass


def retry(key):
    key=ident(key)
    with database() as conn:job=conn.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
    if not job:raise ValueError('Задание не найдено')
    if job['status'] not in {'error','interrupted'}:return status(key)
    document_id=json.loads(job['payload']).get('document_id')
    if document_id:return start_saved(document_id,job['origin'],job['destination'],job_id=key)
    return start(data_store.read_bytes(root()/(key+'.upload')),job['filename'],job['company'],job['origin'],job['destination'],job_id=key)


def recover():
    if not cloud_db.enabled():return
    with database() as conn:
        conn.execute("UPDATE jobs SET status='interrupted',message=? WHERE status IN ('queued','parsing')",
                     ('Сервер перезапущен. Нажмите «Повторить распознавание»: файл уже сохранён.',))
