"""Durable JSON in PostgreSQL and private originals in S3-compatible storage.

Local files are only a cache in cloud mode. An upload is acknowledged only
after its bytes are stored remotely and its database reference is committed.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from . import cloud_db as db


def root():
    from .v42_engine import RUNTIME_DIR
    return RUNTIME_DIR.resolve()


def key(path):
    try:return Path(path).resolve().relative_to(root()).as_posix()
    except ValueError:raise ValueError('Путь находится вне папки данных') from None


def managed(path):
    if path is None or not db.enabled():return False
    try:key(path);return True
    except ValueError:return False


def configuration():
    values={name:os.environ.get(name,'').strip() for name in
        ('S3_ENDPOINT_URL','S3_BUCKET','S3_ACCESS_KEY_ID','S3_SECRET_ACCESS_KEY')}
    missing=[name for name,value in values.items() if not value]
    if missing:raise db.StorageUnavailable('Не заполнены настройки хранилища: '+', '.join(missing))
    from urllib.parse import urlsplit
    endpoint=urlsplit(values['S3_ENDPOINT_URL'])
    if endpoint.scheme!='https' and not (endpoint.scheme=='http' and endpoint.hostname in {'127.0.0.1','localhost','::1'}):
        raise db.StorageUnavailable('S3_ENDPOINT_URL должен быть адресом HTTPS из настроек хранилища.')
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise db.StorageUnavailable('S3_ENDPOINT_URL должен содержать только адрес S3, без ключей и параметров.')
    return values


@lru_cache(maxsize=2)
def _client(endpoint,access,secret,region):
    import boto3
    from botocore.config import Config
    return boto3.client('s3',endpoint_url=endpoint,aws_access_key_id=access,aws_secret_access_key=secret,
        region_name=region,config=Config(signature_version='s3v4',s3={'addressing_style':'path'},
        connect_timeout=10,read_timeout=45,retries={'mode':'standard','total_max_attempts':3},
        request_checksum_calculation='when_required',response_checksum_validation='when_required'))


def client():
    cfg=configuration()
    return _client(cfg['S3_ENDPOINT_URL'],cfg['S3_ACCESS_KEY_ID'],cfg['S3_SECRET_ACCESS_KEY'],
                   os.environ.get('S3_REGION','us-east-1')),cfg['S3_BUCKET']


def prefix():
    value=os.environ.get('S3_PREFIX','tariff-app').strip('/')
    if not value or '..' in value.split('/') or '\\' in value:raise db.StorageUnavailable('Некорректный S3_PREFIX.')
    return value


def read_json(path,default):
    with db.transaction() as conn:
        row=conn.execute('SELECT payload FROM tariff_state.json_data WHERE key=%s',(key(path),)).fetchone()
    # A corrupt/unavailable database must not look like an empty price table.
    return json.loads(row[0]) if row else deepcopy(default)


def write_json(path,payload):
    with db.transaction() as conn:
        conn.execute('INSERT INTO tariff_state.json_data(key,payload) VALUES (%s,%s) '
                     'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,updated_at=now()',
                     (key(path),json.dumps(payload,ensure_ascii=False,separators=(',',':'))))


def _atomic_bytes(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:temp.write_bytes(raw);temp.replace(path)
    finally:temp.unlink(missing_ok=True)


def _hash_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def store_file(path,logical_path=None):
    """Persist a collector/export file before publishing its reference."""
    logical_path=logical_path or path
    if not managed(logical_path):return Path(path)
    path=Path(path);sha=_hash_file(path);size=path.stat().st_size
    object_key=f'{prefix()}/objects/{sha}'
    with db.transaction() as conn:
        existing=conn.execute('SELECT sha256 FROM tariff_state.objects WHERE key=%s',(key(logical_path),)).fetchone()
        if existing and existing[0]==sha:return path
        s3,bucket=client()
        try:
            with path.open('rb') as body:
                s3.put_object(Bucket=bucket,Key=object_key,Body=body,ContentLength=size,ContentType='application/octet-stream')
        except Exception as exc:
            raise db.StorageUnavailable('Не удалось сохранить файл в облаке. Проверьте S3-настройки, доступ и свободный объём. Цены из файла не применены.') from exc
        conn.execute('INSERT INTO tariff_state.objects(key,object_key,sha256,size) VALUES (%s,%s,%s,%s) '
            'ON CONFLICT(key) DO UPDATE SET object_key=excluded.object_key,sha256=excluded.sha256,size=excluded.size,updated_at=now()',
            (key(logical_path),object_key,sha,size))
    return path


def write_bytes(path,raw):
    # Local staging is not success: the caller only returns after remote commit.
    _atomic_bytes(path,raw)
    return store_file(path)


def metadata(path):
    with db.transaction() as conn:
        return conn.execute('SELECT object_key,sha256,size FROM tariff_state.objects WHERE key=%s',(key(path),)).fetchone()


def materialize(path):
    path=Path(path)
    if not managed(path):return path
    row=metadata(path)
    if not row:raise FileNotFoundError('Файл не найден в облачном хранилище')
    object_key,sha,size=row
    if path.is_file() and path.stat().st_size==size and _hash_file(path)==sha:return path
    s3,bucket=client();path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        body=s3.get_object(Bucket=bucket,Key=object_key)['Body']
        try:
            with temp.open('wb') as out:
                for chunk in iter(lambda:body.read(1024*1024),b''):out.write(chunk)
        finally:body.close()
        if temp.stat().st_size!=size or _hash_file(temp)!=sha:
            raise db.StorageUnavailable('Контрольная сумма файла не совпала. Повторите скачивание; данные не изменены.')
        temp.replace(path)
    except db.StorageUnavailable:raise
    except Exception as exc:raise db.StorageUnavailable('Оригинал временно недоступен в хранилище. Проверьте подключение и S3-настройки.') from exc
    finally:temp.unlink(missing_ok=True)
    return path


def read_bytes(path):return materialize(path).read_bytes()


def exists(path):
    if not managed(path):return Path(path).is_file()
    with db.transaction() as conn:
        k=key(path)
        return bool(conn.execute('SELECT 1 FROM tariff_state.objects WHERE key=%s UNION ALL SELECT 1 FROM tariff_state.json_data WHERE key=%s LIMIT 1',(k,k)).fetchone())


def paths(directory,pattern='*'):
    if not managed(directory):return list(Path(directory).glob(pattern))
    from fnmatch import fnmatch
    k=key(directory).rstrip('/')+'/'
    with db.transaction() as conn:
        rows=conn.execute('SELECT key FROM tariff_state.objects WHERE starts_with(key,%s) UNION SELECT key FROM tariff_state.json_data WHERE starts_with(key,%s)',(k,k)).fetchall()
    return [root()/r[0] for r in rows if '/' not in r[0][len(k):] and fnmatch(r[0][len(k):],pattern)]


def delete(path):
    if managed(path):
        # Drop the logical temporary reference only. Content-addressed objects
        # may be shared by confirmed files and must never be deleted here.
        with db.transaction() as conn:
            conn.execute('DELETE FROM tariff_state.json_data WHERE key=%s',(key(path),))
            conn.execute('DELETE FROM tariff_state.objects WHERE key=%s',(key(path),))
    Path(path).unlink(missing_ok=True)


def check():
    """Explicit read/write/read-back check, not a keep-alive health probe."""
    db.initialize();s3,bucket=client()
    name=f'{prefix()}/checks/{uuid.uuid4().hex}';raw=os.urandom(32)
    try:
        s3.put_object(Bucket=bucket,Key=name,Body=raw)
        response=s3.get_object(Bucket=bucket,Key=name)['Body']
        try:actual=response.read()
        finally:response.close()
        if actual!=raw:raise ValueError('readback')
        with db.transaction() as conn:conn.execute('SELECT 1')
    except Exception as exc:raise db.StorageUnavailable('Проверка облака не пройдена. Проверьте S3_ENDPOINT_URL, бакет и ключ с правами чтения/записи/удаления.') from exc
    finally:
        try:s3.delete_object(Bucket=bucket,Key=name)
        except Exception:pass
    return {'ok':True,'database':'PostgreSQL','files':'S3','message':'База доступна. Пробный файл сохранён и прочитан из облака.'}
