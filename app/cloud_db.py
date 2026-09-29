"""PostgreSQL persistence. Local installations retain SQLite.

SQL namespaces are fixed, credentials never reach API errors, and cloud errors
never fall back to a disposable local database. No background keep-alive queries.
"""
from contextlib import contextmanager
import os
import re
import sqlite3
import threading


class StorageUnavailable(RuntimeError):
    pass


def enabled():
    mode=os.environ.get('TARIFF_STORAGE','local').strip().lower()
    if mode not in {'local','cloud'}:
        raise StorageUnavailable('TARIFF_STORAGE должен быть local или cloud.')
    return mode=='cloud'


# Explicit schemas also describe portable SQLite backups and one-time migration.
TABLES={
 'documents':{
  'files':('id TEXT PRIMARY KEY,company TEXT,meta TEXT,active INTEGER',('id','company','meta','active')),
  'prices':('file TEXT,origin TEXT,destination TEXT,profile TEXT,payload TEXT,PRIMARY KEY(file,origin,destination,profile)',('file','origin','destination','profile','payload')),
  'excluded':('company TEXT,origin TEXT,destination TEXT,PRIMARY KEY(company,origin,destination)',('company','origin','destination')),
  'route_documents':('company TEXT,origin TEXT,destination TEXT,file TEXT,PRIMARY KEY(company,origin,destination)',('company','origin','destination','file'))},
 'bulk':{
  'jobs':('id TEXT PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT,config TEXT,message TEXT,export_status TEXT,export_file TEXT',('id','status','created_at','updated_at','config','message','export_status','export_file')),
  'routes':("job TEXT,idx INTEGER,origin TEXT,destination TEXT,state TEXT DEFAULT 'pending',PRIMARY KEY(job,idx)",('job','idx','origin','destination','state')),
  'results':('job TEXT,idx INTEGER,company TEXT,status TEXT,payload BLOB,PRIMARY KEY(job,idx,company)',('job','idx','company','status','payload'))},
 'importjobs':{
  'jobs':('id TEXT PRIMARY KEY,fingerprint TEXT,company TEXT,origin TEXT,destination TEXT,filename TEXT,created REAL,updated REAL,status TEXT,message TEXT,payload TEXT',('id','fingerprint','company','origin','destination','filename','created','updated','status','message','payload'))}}

_pool=None
_pool_url=None
_init_lock=threading.RLock()
_ready=False
_local=threading.local()
LOCK_ID=63008423


def pool():
    global _pool,_pool_url,_ready
    url=os.environ.get('DATABASE_URL','').strip()
    if not url.startswith(('postgresql://','postgres://')):
        raise StorageUnavailable('Добавьте DATABASE_URL из Neon в Environment сервиса Render.')
    # Allow plain connections only for explicit local integration testing.
    from urllib.parse import urlsplit,parse_qs
    parsed=urlsplit(url)
    if parsed.hostname not in {'localhost','127.0.0.1','::1',None} and parse_qs(parsed.query).get('sslmode',[''])[0] not in {'require','verify-ca','verify-full'}:
        raise StorageUnavailable('Для внешней базы DATABASE_URL должен содержать sslmode=require или verify-full.')
    with _init_lock:
        if _pool is None or _pool_url!=url:
            if _pool is not None:_pool.close()
            from psycopg_pool import ConnectionPool
            _pool=ConnectionPool(url,min_size=0,max_size=4,max_idle=60,timeout=20,
                reconnect_timeout=20,open=True,check=ConnectionPool.check_connection,
                kwargs={'connect_timeout':12,'prepare_threshold':None},name='tariff-db')
            _pool_url=url;_ready=False
    return _pool


def initialize():
    global _ready
    p=pool()
    with _init_lock:
        if _ready:return
        try:
            with p.connection() as conn:
                conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
                for kind,tables in TABLES.items():
                    schema='tariff_'+kind
                    conn.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
                    for name,(ddl,_) in tables.items():
                        # SQLite rowid order is used to choose the latest document.
                        ddl=ddl.replace(' BLOB',' BYTEA').replace(' REAL',' DOUBLE PRECISION')
                        conn.execute(f'CREATE TABLE IF NOT EXISTS {schema}.{name} (rowid BIGSERIAL UNIQUE,{ddl})')
                conn.execute('CREATE INDEX IF NOT EXISTS document_route ON tariff_documents.prices(origin,destination)')
                conn.execute('CREATE INDEX IF NOT EXISTS route_state ON tariff_bulk.routes(job,state,idx)')
                conn.execute('CREATE INDEX IF NOT EXISTS result_status ON tariff_bulk.results(job,status)')
                conn.execute('CREATE SCHEMA IF NOT EXISTS tariff_state')
                conn.execute('CREATE TABLE IF NOT EXISTS tariff_state.json_data (key TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now())')
                conn.execute('CREATE TABLE IF NOT EXISTS tariff_state.objects (key TEXT PRIMARY KEY,object_key TEXT NOT NULL,sha256 TEXT NOT NULL,size BIGINT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now())')
            _ready=True
        except StorageUnavailable:raise
        except Exception as exc:
            raise StorageUnavailable('Не удалось подключить базу данных. Проверьте DATABASE_URL, доступность и лимиты Neon. Локальная замена базы не создавалась.') from exc


@contextmanager
def transaction():
    """Reuse the outer price transaction across JSON, library and revisions."""
    initialize()
    current=getattr(_local,'connection',None)
    if current is not None:
        yield current
        return
    try:
        with pool().connection() as conn:
            yield conn
    except StorageUnavailable:raise
    except Exception as exc:
        # Programming errors must remain visible in tests/server logs; connection
        # details from a driver exception must never be shown in a response.
        import psycopg
        from psycopg_pool import PoolTimeout
        if isinstance(exc,(psycopg.Error,PoolTimeout)):
            raise StorageUnavailable('База данных временно недоступна. Изменения не подтверждены; повторите действие после восстановления подключения.') from exc
        raise


class StateLock:
    def __init__(self):self.lock=threading.RLock();self.state=threading.local()
    def __enter__(self):
        self.lock.acquire()
        try:
            depth=getattr(self.state,'depth',0)
            if depth==0 and enabled():
                cm=transaction();conn=cm.__enter__()
                try:conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
                except BaseException:
                    import sys
                    cm.__exit__(*sys.exc_info());raise
                self.state.context=cm;_local.connection=conn
            self.state.depth=depth+1
            return self
        except BaseException:self.lock.release();raise
    def __exit__(self,*args):
        try:
            self.state.depth-=1
            if self.state.depth==0 and getattr(self.state,'context',None):
                cm=self.state.context;self.state.context=None;_local.connection=None
                return cm.__exit__(*args)
        finally:self.lock.release()


class Row(dict):
    def __getitem__(self,key):
        return tuple(self.values())[key] if isinstance(key,(int,slice)) else super().__getitem__(key)
    def __iter__(self):return iter(self.values())


def row_factory(cursor):
    names=[c.name for c in cursor.description] if cursor.description else []
    return lambda values:Row(zip(names,values))


class PG:
    def __init__(self,conn,kind):self.conn=conn;self.kind=kind
    def sql(self,query):
        # Queries belong to this application, not user input. Translate only
        # placeholders outside string literals and known table identifiers.
        parts=re.split("('(?:''|[^'])*')",query)
        tables='|'.join(TABLES[self.kind])
        for i in range(0,len(parts),2):
            parts[i]=parts[i].replace('%','%%').replace('?','%s')
            parts[i]=re.sub(r'\b(FROM|JOIN|UPDATE|INTO)\s+('+tables+r')\b',
                lambda m:m[1]+' tariff_'+self.kind+'.'+m[2],parts[i],flags=re.I)
        return ''.join(parts)
    def execute(self,query,params=()):
        if query.strip().upper().startswith(('CREATE ','PRAGMA ')):return self.conn.cursor(row_factory=row_factory)
        if query.strip().upper()=='BEGIN IMMEDIATE':
            return self.conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
        cur=self.conn.cursor(row_factory=row_factory)
        cur.execute(self.sql(query),params)
        return cur
    def executemany(self,query,params):
        cur=self.conn.cursor(row_factory=row_factory);cur.executemany(self.sql(query),params);return cur
    def executescript(self,script):
        for query in script.split(';'):
            if query.strip():self.execute(query)


@contextmanager
def connect(path,kind):
    if enabled():
        with transaction() as conn:yield PG(conn,kind)
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(path,timeout=30);conn.row_factory=sqlite3.Row
        try:
            with conn:yield conn
        finally:conn.close()


def close():
    global _pool,_ready
    if _pool is not None:_pool.close();_pool=None
    _ready=False
