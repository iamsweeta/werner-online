"""Interactive, local-only Neon setup. Never upload the generated render.env.

The Neon management API key is read without echo and never saved. This helper
creates one private bucket and a storage credential in the selected branch.
"""
import getpass
import json
import os
import re
from pathlib import Path
import secrets
from urllib.parse import urlsplit,parse_qs

API='https://console.neon.tech/api/v2'


class NeonAPIError(ValueError):
    def __init__(self,status,method,path):
        self.status=status
        self.method=method
        self.path=path
        if status==401:
            hint='Нужен действующий Neon API key с доступом к проекту, а не пароль PostgreSQL или S3-ключ.'
        elif status==403:
            hint='У этого ключа нет доступа к операции или проекту. Проверьте права ключа.'
        elif status==404:
            hint='Проверьте Project ID, ветку и доступность Object Storage для этой ветки.'
        elif status==429:
            hint='Достигнут лимит запросов. Повторите позже.'
        else:
            hint='Сохраните этот код и название операции. Секретные значения не нужны для диагностики.'
        super().__init__(f'Neon: HTTP {status} при {method} {path}. {hint}')


def select_project(api):
    """Personal keys need org_id for listing; exact project lookup does not.

    Do not require a broader organization key just to configure one project.
    A project ID is not a secret and every exact GET still checks permissions.
    """
    try:
        projects=api('GET','/projects').get('projects',[])
    except NeonAPIError as exc:
        if exc.status not in {400,403}:raise
        projects=[]
    if projects:return choose(projects,'проект')
    print('\nСписок проектов недоступен для этого ключа. Откроем ваш проект по Project ID.')
    print('Neon → Overview → справа блок Project → ID. Нужен ID, а не имя проекта.')
    project_id=input('Вставьте Project ID: ').strip()
    if not re.fullmatch(r'[a-z0-9-]{1,60}',project_id):
        raise ValueError('Некорректный Project ID. Скопируйте только значение ID из блока Project в Neon Overview.')
    project=api('GET','/projects/'+project_id).get('project')
    if not isinstance(project,dict) or project.get('id')!=project_id:
        raise ValueError('Neon не подтвердил выбранный Project ID. Проверьте значение в Overview.')
    print('Проект: '+str(project.get('name',project_id)))
    return project


def choose(items,label):
    if not items:raise ValueError('Нет доступных '+label+'. Создайте проект Neon в AWS Frankfurt и повторите.')
    print('\nВыберите '+label+':')
    for i,item in enumerate(items,1):print(f"{i}. {item.get('name',item['id'])} ({item['id']})")
    while True:
        answer=input('Номер: ').strip()
        if answer.isdigit() and 1<=int(answer)<=len(items):return items[int(answer)-1]
        print('Введите номер из списка.')


def env_text(values):
    return '\n'.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in values.items())+'\n'


def main():
    import requests
    output=Path(__file__).resolve().parent/'render.env'
    if output.exists():raise ValueError('render.env уже существует. Используйте его или сохраните отдельно перед повторной настройкой.')
    print('Настройка отдельного хранилища для тарифов. Создаётся только закрытый бакет; существующие данные не изменяются.')
    print('Сначала создайте бесплатный проект Neon в регионе AWS Frankfurt (eu-central-1).')
    database=getpass.getpass('Вставьте PostgreSQL connection string из Neon → Connect (ввод скрыт): ').strip()
    parsed=urlsplit(database)
    if parsed.scheme not in {'postgres','postgresql'} or not parsed.hostname or not parsed.password or parse_qs(parsed.query).get('sslmode',[''])[0] not in {'require','verify-ca','verify-full'}:
        raise ValueError('Нужна полная строка postgresql://… со значением sslmode=require. Не вставляйте команду psql или внешние кавычки.')
    token=getpass.getpass('Вставьте Neon API key для настройки Storage (ввод скрыт): ').strip()
    if not token:raise ValueError('API key не введён.')
    session=requests.Session();session.headers.update({'Authorization':'Bearer '+token})
    def api(method,path,payload=None):
        response=session.request(method,API+path,json=payload,timeout=30)
        if not response.ok:
            raise NeonAPIError(response.status_code,method,path)
        return response.json()
    try:
        project=select_project(api)
        base='/projects/'+project['id']
        branch=choose(api('GET',base+'/branches').get('branches',[]),'рабочую ветку (например production или main)')
        base+='/branches/'+branch['id']
        state=api('GET',base+'/storage')
        if not state.get('s3_endpoint') or not state.get('region'):raise ValueError('Object Storage недоступен. Выберите поддерживаемый регион, например AWS Frankfurt.')
        bucket='tariff-prices-'+secrets.token_hex(3)
        api('POST',base+'/buckets',{'name':bucket,'access_level':'private'})
        credential=api('POST',base+'/credentials',{'scopes':['storage:read','storage:write'],'principal_type':'user'})
        if not credential.get('token_id') or not credential.get('s3_secret_access_key'):
            raise ValueError('Neon не вернул S3-ключ. Проверьте раздел Credentials выбранной ветки.')
        values={'TARIFF_STORAGE':'cloud','DATABASE_URL':database,
            'S3_ENDPOINT_URL':state['s3_endpoint'],'S3_REGION':state['region'],'S3_BUCKET':bucket,
            'S3_ACCESS_KEY_ID':credential['token_id'],'S3_SECRET_ACCESS_KEY':credential['s3_secret_access_key'],
            'S3_PREFIX':'tariff-app','APP_USERNAME':'manager','APP_AUTH_MODE':'public','APP_PASSWORD':secrets.token_urlsafe(24),
            'TARIFF_DATA_DIR':'./runtime','PYTHON_VERSION':'3.12.11'}
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as out:out.write(env_text(values))
        print('\nГотово: render.env рядом с этой программой.')
        print('Render → ваш сервис → Environment → Add from .env: вставьте содержимое render.env.')
        print('Вход свободный: APP_AUTH_MODE=public. Посетителям доступны общие цены, файлы и изменения.')
        print('Чтобы включить пароль, задайте APP_AUTH_MODE=password. Логин manager, пароль — APP_PASSWORD в render.env.')
        print('render.env содержит секреты. Не загружайте его в GitHub и не отправляйте в чат.')
        print('Служебный Neon API key нигде не записан; после проверки его можно отозвать.')
    finally:session.close()


if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):print('\nНастройка отменена.')
    except Exception as exc:
        # Only our own validation messages may be printed, never raw HTTP/SDK data.
        print('Не завершено: '+(str(exc) if isinstance(exc,ValueError) else 'Проверьте интернет и настройки. Попробуйте ещё раз.'))
        raise SystemExit(1)
