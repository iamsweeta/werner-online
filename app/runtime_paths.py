"""One data location for prices, document originals, SQLite and collector files."""
import os
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent/'.env',override=False)
except ImportError:pass


def on_render():
    return os.environ.get('RENDER','').lower() in {'true','1'} or bool(os.environ.get('RENDER_SERVICE_ID'))


def disk_mount(data_directory):
    """A configured directory alone is not evidence of a mounted disk."""
    directory=Path(data_directory).resolve()
    for candidate in (directory,*directory.parents):
        if candidate==candidate.parent:break
        if os.path.ismount(candidate):return str(candidate)
    return None


def runtime_directory(base_directory):
    configured=os.environ.get('TARIFF_DATA_DIR','').strip()
    if configured:return Path(configured).expanduser().resolve()
    # Do not create /var/data and then mistake an ephemeral folder for a disk.
    if on_render() and os.path.ismount('/var/data'):
        return Path('/var/data/tariff-app')
    return (Path(base_directory)/'runtime').resolve()


def persistence_info(directory):
    from .cloud_db import enabled
    if enabled():
        return {'platform':'render' if on_render() else 'local_or_other','mount_path':None,
                'data_directory':str(Path(directory).resolve()),'persistence_status':'cloud',
                'needs_attention':False,'configured':True,
                'message':'Облачное хранение: цены — в PostgreSQL, документы — в закрытом файловом хранилище. Перезапуск Render не удаляет сохранённые данные.'}
    directory=Path(directory).resolve()
    render=on_render()
    mount=disk_mount(directory) if render else None
    unsafe=render and mount is None
    return {'platform':'render' if render else 'local_or_other',
            'mount_path':mount,'data_directory':str(directory),
            'persistence_status':'disk_detected' if mount else 'unverified' if render else 'local',
            'needs_attention':unsafe,
            'message':('На Render не обнаружен отдельный диск для данных. Прайсы и файлы могут пропасть после перезапуска. Скачайте резервную копию и подключите облачное хранение по инструкции /storage-guide или Persistent Disk с TARIFF_DATA_DIR внутри него.' if unsafe else
                       'Данные записываются на отдельный диск. Убедитесь в Render, что это Persistent Disk; резервную копию можно скачать ниже.' if mount else
                       'Данные хранятся в папке приложения без срока удаления. При обновлении сохраняйте папку данных.'),
            'configured':bool(os.environ.get('TARIFF_DATA_DIR','').strip())}
