"""Durable asynchronous previews for One route. HTTP never waits for OCR.

SQLite makes status/idempotency visible to all web workers sharing TARIFF_DATA_DIR.
Only explicit /api/import/commit applies a preview to the price library.
"""
from __future__ import annotations
import hashlib,json,re,sqlite3,threading,time,uuid
from contextlib import contextmanager
from pathlib import Path
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
    conn=sqlite3.connect(folder/'jobs.sqlite3',timeout=5);conn.row_factory=sqlite3.Row
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, fingerprint TEXT, company TEXT, origin TEXT, destination TEXT,
          filename TEXT, created REAL, updated REAL, status TEXT, message TEXT, payload TEXT)''')
        with conn:yield conn
    finally:conn.close()


def _update(folder,key,status=None,message=None,payload=None):
    with database(folder) as conn:
        row=conn.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
        if not row or row['status'] not in ('queued','parsing'):return
        old=json.loads(row['payload']);old.update(payload or {})
        conn.execute('UPDATE jobs SET status=?,message=?,payload=?,updated=? WHERE id=?',
                     (status or row['status'],message or row['message'],json.dumps(old,ensure_ascii=False),time.time(),key))


def _stale(conn):
    conn.execute("UPDATE jobs SET status='interrupted',message=? WHERE status IN ('queued','parsing') AND updated<?",
                 ('Распознавание прервано: сервер перестал отвечать или перезапустился. Загрузите файл заново. Сохранённые прайсы не изменены.',time.time()-STALE_SECONDS))


def cleanup():
    folder=root()
    if not (folder/'jobs.sqlite3').exists():return
    with database(folder) as conn:
        _stale(conn)
        rows=conn.execute("SELECT id FROM jobs WHERE status NOT IN ('queued','parsing') AND updated<?",(time.time()-imports.PREVIEW_TTL,)).fetchall()
        for row in rows:
            (folder/(row['id']+'.upload')).unlink(missing_ok=True)
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
        if (folder.parent/'files'/(token+meta['extension'])).is_file():state='committed';message='Этот прайс уже сохранён в библиотеке.'
        elif time.time()-row['updated']>imports.PREVIEW_TTL or not (folder.parent/'pending'/(token+'.json')).is_file():
            state='expired';message='Предпросмотр истёк. Загрузите файл заново и проверьте цены.'
    return {'job_id':key,'status':state,'company':row['company'],'origin':row['origin'],'destination':row['destination'],
            'filename':row['filename'],'message':message,**{k:v for k,v in data.items() if k!='preview'},
            **({'preview':data['preview']} if state=='ready' else {})}


def start(raw,filename,company,origin,destination,job_id=None):
    origin,destination=imports._validate(company,origin,destination)
    if not raw or len(raw)>documents.MAX_BYTES:raise ValueError('Выберите непустой документ размером до 20 МБ')
    name=documents.normalize_filename(filename)
    if Path(name).suffix.lower() not in {'.pdf','.xls','.xlsx','.csv','.zip'}:raise ValueError('Поддерживаются PDF, XLS, XLSX, CSV и ZIP')
    key=ident(job_id) if job_id else uuid.uuid4().hex;folder=root()
    fingerprint=hashlib.sha256(raw+json.dumps([company,origin,destination,name],ensure_ascii=False).encode()).hexdigest()
    cleanup()
    with database(folder) as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing=conn.execute('SELECT fingerprint FROM jobs WHERE id=?',(key,)).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint:raise ValueError('Этот номер задания относится к другому файлу или маршруту. Начните новую загрузку.')
        else:
            _stale(conn)
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','parsing')").fetchone()[0]>=MAX_ACTIVE:
                raise ValueError('Уже распознаются два документа. Дождитесь завершения и повторите загрузку.')
            (folder/(key+'.upload')).write_bytes(raw)
            now=time.time();conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (key,fingerprint,company,origin,destination,name,now,now,'queued','Файл принят. Готовлю распознавание…','{}'))
    if not existing:
        threading.Thread(target=_run,args=(folder,key,str(filename),company,origin,destination),daemon=True).start()
    return status(key,folder)


def _run(folder,key,filename,company,origin,destination):
    from . import scan_ocr
    stop=threading.Event()
    def heartbeat():
        while not stop.wait(10):
            try:_update(folder,key)
            except (OSError,sqlite3.Error):pass
    monitor=threading.Thread(target=heartbeat,daemon=True);monitor.start()
    def progress(info):
        _update(folder,key,message=info['message'],payload={'ocr_page':info['done'],'ocr_total':info['total']})
    context=scan_ocr._PROGRESS.set(progress)
    try:
        _update(folder,key,status='parsing',message='Читаю документ и проверяю выбранное направление…')
        raw=(folder/(key+'.upload')).read_bytes()
        result=imports.preview(raw,filename,company,origin,destination)
        _update(folder,key,status='ready',message='Распознавание завершено. Проверьте цены перед сохранением.',payload={'preview':result})
    except Exception as exc:
        _update(folder,key,status='error',message=str(exc)[:1500])
    finally:
        stop.set();monitor.join(timeout=1);scan_ocr._PROGRESS.reset(context)
        (folder/(key+'.upload')).unlink(missing_ok=True)
