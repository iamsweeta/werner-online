"""Browser-driven migration: upload once, poll status, retry after restart."""
import json
import threading
import uuid
from pathlib import Path
from . import cloud_db,data_store,v42_engine as e

LOCK=threading.Lock()
WORKER=None
INSTANCE=uuid.uuid4().hex
CURRENT={}


def status():
    if not cloud_db.enabled():return {'status':'unavailable','message':'Включите облачное хранение.'}
    if WORKER and WORKER.is_alive():return CURRENT.copy()
    job=data_store.read_json(e.RUNTIME_DIR/'system/migration.json',{'status':'idle'})
    completed=data_store.read_json(e.RUNTIME_DIR/'system/migration_completed.json',{})
    if completed:return {**completed,'status':'done'}
    if job.get('status')=='running' and job.get('worker')!=INSTANCE:
        job.update(status='interrupted',message='Сервер перезапущен. Повторите перенос: загруженный архив сохранён.')
    return job


def _save(job):
    # Called before/after the atomic import, never from its progress callback.
    data_store.write_json(e.RUNTIME_DIR/'system/migration.json',job)


def start(path=None):
    global WORKER,CURRENT
    if not cloud_db.enabled():raise ValueError('Сначала подключите PostgreSQL и файловое хранилище.')
    cloud_db.initialize()
    with LOCK:
        if WORKER and WORKER.is_alive():raise ValueError('Перенос уже выполняется.')
        # Do not hold a transaction open while receiving a browser upload.
        with e.STATE_LOCK:
            from .cloud_transfer import assert_empty
            assert_empty()
            if path:
                data_store.store_file(path,logical_path=e.RUNTIME_DIR/'system/migration.zip')
            else:
                if not data_store.exists(e.RUNTIME_DIR/'system/migration.zip'):raise ValueError('Выберите резервную копию.')
        job={'status':'running','worker':INSTANCE,'created_at':e._now(),'message':'Архив принят. Проверяю данные…'}
        CURRENT=job
        _save(job)
        def run():
            from .cloud_transfer import migrate
            try:
                source=data_store.materialize(e.RUNTIME_DIR/'system/migration.zip')
                # Initial/final status is durable. Intermediate counters are
                # in memory, avoiding a second DB connection during the import.
                def progress(message):job['message']=message
                result=migrate(source,progress)
                job.update(result,status='done',finished_at=e._now());_save(job)
                data_store.delete(e.RUNTIME_DIR/'system/migration.zip')
            except Exception as exc:
                job.update(status='error',message=str(exc)[:700])
                try:_save(job)
                except Exception:pass
        WORKER=threading.Thread(target=run,daemon=True,name='cloud-migration');WORKER.start()
        return job.copy()
