"""Local retention information and consistent, portable user-document backups."""
import json
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
    cleanup_previews()
    root=e.RUNTIME_DIR/'imports'
    files=[p for p in (root/'files').glob('*') if p.is_file() and not p.is_symlink()]
    return {'location':str(root.resolve()),'files':len(files),'bytes':sum(p.stat().st_size for p in files),
            'confirmed_retention':'Бессрочно на сервере приложения (при локальном запуске — на вашем компьютере). Автоматического удаления подтверждённых прайсов нет.',
            'disabled_retention':'Отключённый прайс не участвует в расчётах, но оригинал остаётся на диске.',
            'ocr_cache_retention_days':7,'ocr_cache_max_bytes':32*1024*1024,
            'manual_retention':'Ручные цены хранятся без срока удаления.',
            'preview_retention_minutes':30,'online_freshness_minutes':30,
            'online_retention':'Последние успешные онлайн-цены хранятся без срока удаления; после 30 минут теряют только отметку LIVE.',
            'data_directory':str(e.RUNTIME_DIR.resolve()),
            'backup_note':'Архив содержит оригиналы, распознанные и ручные цены, историю отключений. API-ключи и временные предпросмотры не включаются.'}


def backup():
    folder=e.RUNTIME_DIR/'exports';folder.mkdir(parents=True,exist_ok=True)
    target=folder/('documents_backup_'+uuid.uuid4().hex+'.zip')
    root=e.RUNTIME_DIR/'imports'
    try:
        with e.STATE_LOCK, tempfile.TemporaryDirectory(dir=folder) as temporary:
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
                for name in ('files','routes'):
                    for path in sorted((root/name).glob('*')):
                        if path.is_file() and not path.is_symlink():archive.write(path,'imports/'+name+'/'+path.name)
                if (root/'revision.json').is_file():archive.write(root/'revision.json','imports/revision.json')
                if (root/'documents.sqlite3').is_file():
                    snapshot=Path(temporary)/'documents.sqlite3'
                    source=sqlite3.connect(root/'documents.sqlite3')
                    destination=sqlite3.connect(snapshot)
                    try:source.backup(destination)
                    finally:destination.close();source.close()
                    archive.write(snapshot,'imports/documents.sqlite3')
                for path in sorted((e.RUNTIME_DIR/'manual').glob('*.json')):
                    if path.is_file() and not path.is_symlink():archive.write(path,'manual/'+path.name)
                archive.writestr('RESTORE.txt','Закройте приложение. Сохраните копию текущей папки runtime/imports. '
                                 'Замените её целиком папкой imports из этого архива. Также замените runtime/manual папкой manual из архива (если папки нет, удалите старую runtime/manual после сохранения её копии). Запустите приложение и пересоберите Excel.\n'
                                 'Подтверждённые документы и история отключений восстанавливаются вместе. Не объединяйте две базы SQLite.\n')
                archive.writestr('manifest.json',json.dumps({'created_at':e._now(),'type':'user_documents_backup','schema':1},ensure_ascii=False))
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise
