"""Restore a verified backup into a NEW data directory; never overwrite data."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile

MAX_SIZE=2*1024**3


def allowed(name):
    p=PurePosixPath(name)
    if str(p)!=name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:return False
    if name in {'imports/revision.json','imports/documents.sqlite3','bulk/jobs.sqlite3',
                'prices_revision.json','v42_moscow_spb_live.json','v42_spb_moscow_live.json'}:return True
    return ((len(p.parts)==3 and p.parts[:2] in {('imports','files'),('imports','routes')})
            or (len(p.parts)==2 and p.parts[0] in {'manual','routes','downloads','collect_jobs'}))


def restore(archive_path, target):
    target=Path(target).expanduser().absolute()
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise ValueError('Папка назначения должна отсутствовать или быть пустой. Существующие данные не изменены.')
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        entries=archive.infolist()
        if len(entries)>100000 or sum(i.file_size for i in entries)>MAX_SIZE:raise ValueError('Архив превышает лимит 2 ГБ / 100 000 файлов')
        names=[i.filename for i in entries]
        if len(set(names))!=len(names):raise ValueError('В архиве повторяются имена файлов')
        if 'manifest.json' not in names:raise ValueError('Это не резервная копия приложения')
        manifest=json.loads(archive.read('manifest.json'))
        if not isinstance(manifest,dict):raise ValueError('Повреждён манифест резервной копии')
        if (manifest.get('schema'),manifest.get('type')) not in {(1,'user_documents_backup'),(2,'tariff_data_backup')}:
            raise ValueError('Неизвестный формат резервной копии')
        payload=[i for i in entries if i.filename not in {'manifest.json','RESTORE.txt'}]
        if any(not allowed(i.filename) or i.is_dir() or stat.S_ISLNK(i.external_attr>>16) for i in payload):
            raise ValueError('Архив содержит недопустимые пути')
        hashes=manifest.get('sha256',{})
        if not isinstance(hashes,dict):raise ValueError('Повреждён список контрольных сумм')
        if manifest['schema']==2 and set(hashes)!={i.filename for i in payload}:raise ValueError('Неполный список контрольных сумм')
        with tempfile.TemporaryDirectory(dir=target.parent,prefix='.tariff-restore-') as staging:
            data=Path(staging)/'data';data.mkdir()
            total=0
            for info in payload:
                destination=data/info.filename;destination.parent.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256()
                with archive.open(info) as source,destination.open('wb') as out:
                    for chunk in iter(lambda:source.read(1024*1024),b''):
                        total+=len(chunk)
                        if total>MAX_SIZE:raise ValueError('Распакованные данные превышают 2 ГБ')
                        out.write(chunk);digest.update(chunk)
                if manifest['schema']==2 and digest.hexdigest()!=hashes[info.filename]:raise ValueError('Повреждён файл: '+info.filename)
                if info.filename.endswith('.sqlite3'):
                    with sqlite3.connect(destination) as db:
                        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Повреждена база данных')
            # An empty target can appear during validation; never merge/replace it.
            if target.exists():target.rmdir()
            os.rename(data,target)
    return {'files':len(payload),'bytes':total,'target':str(target)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Восстановить цены и файлы в новую папку данных')
    parser.add_argument('archive');parser.add_argument('--target',required=True)
    args=parser.parse_args()
    try:
        result=restore(args.archive,args.target)
        print(f"Восстановлено файлов: {result['files']}. Укажите TARIFF_DATA_DIR={result['target']} и перезапустите приложение.")
    except (ValueError,OSError,zipfile.BadZipFile,sqlite3.Error) as error:
        parser.exit(1,'Восстановление не выполнено: '+str(error)+'\n')
