"""Portable backups and atomic import of v53–63 data into an empty cloud DB."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from . import cloud_db as db,data_store as store,v42_engine as e

DATABASE_FILES={'documents':'imports/documents.sqlite3','bulk':'bulk/jobs.sqlite3'}


def _allowed_data(name):
    from restore_data import allowed
    return allowed(name) and not name.endswith('.sqlite3')


def assert_empty():
    with db.transaction() as conn:
        for kind,tables in db.TABLES.items():
            for table in tables:
                if conn.execute(f'SELECT 1 FROM tariff_{kind}.{table} LIMIT 1').fetchone():
                    raise ValueError('В облачной базе уже есть данные. Перенос разрешён только в пустую базу; существующие цены не изменены.')
        for name in ('json_data','objects'):
            if conn.execute(f"SELECT 1 FROM tariff_state.{name} WHERE key NOT LIKE 'system/%%' LIMIT 1").fetchone():
                raise ValueError('Облачное хранилище уже используется. Создайте отдельную пустую базу для переноса.')


def migrate(archive_path,on_progress=None):
    from restore_data import restore
    if not db.enabled():raise ValueError('Перенос предназначен для TARIFF_STORAGE=cloud.')
    notify=on_progress or (lambda message:None)
    notify('Проверяю резервную копию…')
    with tempfile.TemporaryDirectory(dir=e.RUNTIME_DIR) as temp:
        source=Path(temp)/'verified';restored=restore(archive_path,source)
        originals=[p for p in source.rglob('*') if p.is_file() and _allowed_data(p.relative_to(source).as_posix())]
        count=0
        with e.STATE_LOCK:
            assert_empty()
            for kind,relative in DATABASE_FILES.items():
                path=source/relative
                if not path.exists():continue
                local=sqlite3.connect(path);local.row_factory=sqlite3.Row
                try:
                    names={r[0] for r in local.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    with db.transaction() as remote:
                        for table,(_,columns) in db.TABLES[kind].items():
                            if table not in names:continue
                            cols=','.join(columns)
                            cursor=local.execute(f'SELECT rowid,{cols} FROM {table} ORDER BY rowid')
                            while rows:=cursor.fetchmany(200):
                                with remote.cursor() as out:
                                    out.executemany(f'INSERT INTO tariff_{kind}.{table}(rowid,{cols}) VALUES ('+','.join(['%s']*(len(columns)+1))+')',[tuple(r) for r in rows])
                                count+=len(rows)
                            remote.execute(f"SELECT setval(pg_get_serial_sequence('tariff_{kind}.{table}','rowid'),COALESCE(MAX(rowid),1),MAX(rowid) IS NOT NULL) FROM tariff_{kind}.{table}")
                finally:local.close()
            for index,path in enumerate(originals,1):
                name=path.relative_to(source).as_posix()
                notify(f'Переношу файлы: {index} из {len(originals)}')
                if name.endswith('.json') and not name.startswith('downloads/'):
                    store.write_json(e.RUNTIME_DIR/name,json.loads(path.read_text(encoding='utf-8')))
                else:store.store_file(path,logical_path=e.RUNTIME_DIR/name)
            with db.transaction() as conn:
                conn.execute("UPDATE tariff_bulk.jobs SET status='paused',message='Данные перенесены. Можно продолжить сбор.' WHERE status IN ('queued','running','pausing')")
                conn.execute("UPDATE tariff_bulk.routes SET state='pending' WHERE state='running'")
                conn.execute("UPDATE tariff_bulk.jobs SET export_status='idle',export_file=NULL")
            result={'ok':True,'files':restored['files'],'rows':count,'message':'Перенос завершён. Прайсы и ручные цены доступны в обоих разделах.'}
            store.write_json(e.RUNTIME_DIR/'system/migration_completed.json',result)
        from .price_library import _MIGRATED
        _MIGRATED.clear()
    return result


def backup():
    """Same schema-2 backup as local mode, restorable without cloud services."""
    folder=e.RUNTIME_DIR/'exports';folder.mkdir(parents=True,exist_ok=True)
    target=folder/('tariff_cloud_backup_'+uuid.uuid4().hex+'.zip')
    try:
        with e.STATE_LOCK,tempfile.TemporaryDirectory(dir=folder) as temp:
            temp=Path(temp)
            with db.transaction() as remote:
                for kind,relative in DATABASE_FILES.items():
                    path=temp/relative;path.parent.mkdir(parents=True,exist_ok=True)
                    with sqlite3.connect(path) as local:
                        for table,(ddl,columns) in db.TABLES[kind].items():
                            local.execute(f'CREATE TABLE {table} ({ddl})')
                            cols=','.join(columns)
                            # Server-side cursor avoids buffering every bulk result.
                            with remote.cursor(name='backup_'+uuid.uuid4().hex) as cursor:
                                cursor.execute(f'SELECT rowid,{cols} FROM tariff_{kind}.{table} ORDER BY rowid')
                                while rows:=cursor.fetchmany(100):
                                    local.executemany(f'INSERT INTO {table}(rowid,{cols}) VALUES ('+','.join(['?']*(len(columns)+1))+')',rows)
                for name,payload in remote.execute('SELECT key,payload FROM tariff_state.json_data'):
                    if not _allowed_data(name):continue
                    path=temp/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(payload,encoding='utf-8')
                originals=[r[0] for r in remote.execute('SELECT key FROM tariff_state.objects') if _allowed_data(r[0])]
                for name in originals:
                    source=store.materialize(e.RUNTIME_DIR/name)
                    path=temp/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,path)
            hashes={}
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
                for path in temp.rglob('*'):
                    if not path.is_file():continue
                    name=path.relative_to(temp).as_posix();hashes[name]=store._hash_file(path);archive.write(path,name)
                archive.writestr('manifest.json',json.dumps({'created_at':e._now(),'type':'tariff_data_backup','schema':2,'sha256':hashes},ensure_ascii=False))
                archive.writestr('RESTORE.txt','В облако: раздел Хранилище → Перенести резервную копию, только в пустую базу.\n'
                    'Локально: python restore_data.py backup.zip --target NEW_FOLDER\n'
                    'Затем TARIFF_STORAGE=local, TARIFF_DATA_DIR=NEW_FOLDER.\n'
                    'Пароли, API-ключи и временные предпросмотры не включены.\n')
        return target
    except BaseException:target.unlink(missing_ok=True);raise
