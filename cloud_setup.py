"""Administration from a computer, without a paid Render Shell."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='Проверка облака и перенос резервной копии')
    parser.add_argument('action',choices=['check','migrate','backup'])
    parser.add_argument('file',nargs='?')
    parser.add_argument('--env',default='render.env')
    args=parser.parse_args()
    from dotenv import load_dotenv
    if not Path(args.env).is_file():raise ValueError('Не найден файл настроек '+args.env)
    load_dotenv(args.env,override=False)
    from app import cloud_db,data_store,cloud_transfer
    if not cloud_db.enabled():raise ValueError('В настройках требуется TARIFF_STORAGE=cloud.')
    data_store.root().mkdir(parents=True,exist_ok=True)
    try:
        if args.action=='check':result=data_store.check()
        elif args.action=='migrate':
            if not args.file:raise ValueError('Укажите путь к ZIP резервной копии.')
            result=cloud_transfer.migrate(Path(args.file),print)
        else:
            if not args.file:raise ValueError('Укажите имя нового ZIP для резервной копии.')
            destination=Path(args.file)
            if destination.exists():raise ValueError('Файл назначения уже существует.')
            import shutil
            source=cloud_transfer.backup();shutil.move(source,destination)
            result={'ok':True,'file':str(destination)}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:cloud_db.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        from app.cloud_db import StorageUnavailable
        print('Операция не выполнена: '+(str(exc) if isinstance(exc,(ValueError,StorageUnavailable)) else 'Проверьте архив, подключение и настройки.'))
        raise SystemExit(1)
