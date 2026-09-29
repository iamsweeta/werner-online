"""Retention information and consistent, portable backups in local/cloud mode."""
import json
import hashlib
import sqlite3
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from . import v42_engine as e


def cleanup_previews():
    from .route_import_jobs import cleanup
    cleanup()
    from .ocr_cache import cleanup as cleanup_ocr
    cleanup_ocr()
    from .price_library import JOBS, LOCK
    with e.STATE_LOCK, LOCK:
        active={key for key,row in JOBS.items() if row.get('status')=='parsing'}
        for name in ('pending','multi_pending'):
            for path in (e.RUNTIME_DIR/'imports'/name).glob('*'):
                if path.is_file() and not path.is_symlink() and path.stem not in active:
                    if time.time()-path.stat().st_mtime>1800:path.unlink(missing_ok=True)


def summary():
    from . import cloud_db,data_store
    # Remote status must not queue behind uploads or run cleanup transactions.
    # Local cleanup is fast and preserves the existing retention behaviour.
    if not cloud_db.enabled():cleanup_previews()
    root=e.RUNTIME_DIR/'imports'
    files=[p for p in (root/'files').glob('*') if p.is_file() and not p.is_symlink()]
    from .runtime_paths import persistence_info
    storage=persistence_info(e.RUNTIME_DIR)
    remote_stats=None
    if cloud_db.enabled():
        with cloud_db.transaction() as conn:
            remote_stats=conn.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM tariff_state.objects WHERE starts_with(key,'imports/files/')").fetchone()
    return {'location':str(root.resolve()),'files':remote_stats[0] if remote_stats else len(files),'bytes':remote_stats[1] if remote_stats else sum(p.stat().st_size for p in files),
            'persistence':storage,
            'confirmed_retention':'Подтверждённые прайсы не удаляются приложением. Сохранность после перезапуска зависит от постоянного хранилища сервера.',
            'disabled_retention':'Отключённый прайс не участвует в расчётах, но оригинал остаётся в хранилище.',
            'ocr_cache_retention_days':7,'ocr_cache_max_bytes':32*1024*1024,
            'manual_retention':'Ручные цены хранятся без срока удаления.',
            'preview_retention_minutes':30,'online_freshness_minutes':30,
            'online_retention':'Последние успешные онлайн-цены хранятся без срока удаления; после 30 минут теряют только отметку LIVE.',
            'data_directory':str(e.RUNTIME_DIR.resolve()),
            'backup_note':'Архив содержит оригиналы, распознанные, ручные и онлайн-цены, историю загрузок и выбора файлов. API-ключи, готовые Excel и временные предпросмотры не включаются.'}


def backup():
    from .cloud_db import enabled
    if enabled():
        from .cloud_transfer import backup as cloud_backup
        return cloud_backup()
    folder=e.RUNTIME_DIR/'exports';folder.mkdir(parents=True,exist_ok=True)
    target=folder/('documents_backup_'+uuid.uuid4().hex+'.zip')
    root=e.RUNTIME_DIR/'imports'
    try:
        with e.STATE_LOCK, tempfile.TemporaryDirectory(dir=folder) as temporary:
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
                hashes={}
                def add(path,name):
                    digest=hashlib.sha256()
                    with path.open('rb') as source,archive.open(name,'w',force_zip64=True) as destination:
                        for block in iter(lambda:source.read(1024*1024),b''):
                            destination.write(block);digest.update(block)
                    hashes[name]=digest.hexdigest()
                for name in ('files','routes'):
                    for path in sorted((root/name).glob('*')):
                        if path.is_file() and not path.is_symlink():add(path,'imports/'+name+'/'+path.name)
                if (root/'revision.json').is_file():add(root/'revision.json','imports/revision.json')
                for relative in ('imports/documents.sqlite3','bulk/jobs.sqlite3'):
                    path=e.RUNTIME_DIR/relative
                    if not path.is_file() or path.is_symlink():continue
                    snapshot=Path(temporary)/path.name
                    source=sqlite3.connect(path)
                    destination=sqlite3.connect(snapshot)
                    try:source.backup(destination)
                    finally:destination.close();source.close()
                    add(snapshot,relative)
                for folder_name,pattern in [('manual','*.json'),('routes','*.json'),('collect_jobs','*.json'),('downloads','*')]:
                    for path in sorted((e.RUNTIME_DIR/folder_name).glob(pattern)):
                        if path.is_file() and not path.is_symlink():add(path,folder_name+'/'+path.name)
                for name in ('prices_revision.json','v42_spb_moscow_live.json','v42_moscow_spb_live.json'):
                    path=e.RUNTIME_DIR/name
                    if path.is_file() and not path.is_symlink():add(path,name)
                archive.writestr('RESTORE.txt','В новой версии приложения выполните: python restore_data.py backup.zip --target ПУТЬ_К_НОВОЙ_ПАПКЕ\n'
                                 'Папка должна отсутствовать или быть пустой. После успешной проверки укажите её как TARIFF_DATA_DIR и перезапустите приложение.\n'
                                 'На Render эта папка должна быть внутри Persistent Disk. Готовые Excel пересоздаются из сохранённых цен.\n'
                                 'API-ключи настройте отдельно. Не объединяйте две базы SQLite.\n')
                archive.writestr('manifest.json',json.dumps({'created_at':e._now(),'type':'tariff_data_backup','schema':2,'sha256':hashes},ensure_ascii=False))
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise
