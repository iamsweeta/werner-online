"""Read carrier-owned city identifiers; refuse ambiguous or missing names."""
from __future__ import annotations
import json
import re
import threading
import time
from urllib.parse import urlsplit,urljoin
from bs4 import BeautifulSoup
from .network import download
from .cities import normalize_city,UnpublishedTariff

_CACHE={}
_LOCKS={}
_LOCK=threading.Lock()

def catalog_bytes(url, *, json_response=False):
    with _LOCK:lock=_LOCKS.setdefault(url,threading.Lock())
    with lock:
        item=_CACHE.get(url)
        if item and time.monotonic()-item[0]<3600:return item[1]
        result=download(url,expected='JSON' if json_response else 'HTML',timeout=50,
                        preferred_transport='curl-cffi-tls12' if json_response else '')
        _CACHE[url]=(time.monotonic(),result.content)
        return result.content

def exact_id(entries,city,company):
    target=normalize_city(city)
    found={str(ident) for ident,name in entries if normalize_city(name)==target and str(ident)}
    if len(found)!=1:
        why='не найден' if not found else 'неоднозначен; требуется уточнение региона'
        raise UnpublishedTariff(f'{company}: город «{city}» {why} в справочнике источника')
    return found.pop()

def keyless_city_ids(company,origin,destination):
    host='wernerus.ru' if company=='Werner' else 'glavtrassa.ru'
    url=f'https://{host}/api/calc/?method=api_city&responseFormat=json'
    data=json.loads(catalog_bytes(url,json_response=True))
    if not isinstance(data,list) or not all(isinstance(x,dict) and 'id' in x and 'name' in x for x in data):
        raise ValueError(f'{company}: формат справочника городов изменился')
    entries=[(x['id'],x['name']) for x in data]
    return exact_id(entries,origin,company),exact_id(entries,destination,company)

def select_city_id(url,selector,city,company):
    soup=BeautifulSoup(catalog_bytes(url),'lxml')
    select=soup.select_one(selector)
    if select is None:raise ValueError(f'{company}: не найден справочник городов в форме')
    return exact_id([(x.get('value',''),x.get_text(' ',strip=True)) for x in select.select('option[value]')],city,company)

def newline_origin(origin):
    # Preserve the explicitly named Moscow terminal; never hide the terminal choice.
    selected='Москва-Север' if normalize_city(origin)=='Москва' else origin
    ident=select_city_id('https://tknl.ru/price/','select[name="FROM"]',selected,'Новая Линия')
    return ident,selected

def kit_origin(origin):
    return select_city_id('https://tk-kit.ru/rates-new','#select_city_from',origin,'КИТ')

def fastrans_route_url(origin,destination):
    page='https://fastrans.ru/city/moscow_saint-petersburg/'
    soup=BeautifulSoup(catalog_bytes(page),'lxml');entries=[]
    for a in soup.select('a[href]'):
        url=urljoin(page,a['href']);path=urlsplit(url).path
        if urlsplit(url).hostname!='fastrans.ru':continue
        match=re.fullmatch(r'/city/([a-z0-9_-]+)/?',path)
        if match:entries.append((match.group(1),a.get_text(' ',strip=True)))
    # The current city's own link is omitted by the navigation. Its slug is
    # also explicitly published in the route URL used to read this catalog.
    entries.extend([('moscow','Москва'),('saint-petersburg','Санкт-Петербург')])
    o=exact_id(entries,origin,'ФастТранс');d=exact_id(entries,destination,'ФастТранс')
    return f'https://fastrans.ru/city/{o}_{d}/'

def magic_cities(origin,destination):
    soup=BeautifulSoup(catalog_bytes('https://magic-trans.ru/tarify/'),'lxml')
    entries=[(x.get('data-code',''),x.get('value','')) for x in soup.select('input[data-code][value]')]
    exact_id(entries,origin,'Мейджик');exact_id(entries,destination,'Мейджик')

def baikal_route_url(origin,destination):
    page='https://www.baikalsr.ru/city/'
    soup=BeautifulSoup(catalog_bytes(page),'lxml');entries=[]
    for a in soup.select('a[href]'):
        url=urljoin(page,a['href']);match=re.fullmatch(r'/city/([a-z0-9-]+)/?',urlsplit(url).path)
        if match and (urlsplit(url).hostname or '').removeprefix('www.')=='baikalsr.ru':
            entries.append((match.group(1),a.get_text(' ',strip=True)))
    o=exact_id(entries,origin,'Байкал Сервис');d=exact_id(entries,destination,'Байкал Сервис')
    return f'https://www.baikalsr.ru/city/{o}__{d}/'


def kit_pdf_origin(origin,pdf):
    """Resolve the one documented off-city terminal without hiding its name."""
    from pypdf import PdfReader
    from io import BytesIO
    text=' '.join((PdfReader(BytesIO(pdf)).pages[0].extract_text() or '').split())
    match=re.search(r'Тарифы на перевозку груза из г\. (.+?) в другие представительства',text)
    if not match:raise ValueError('КИТ: заголовок исходящего PDF не найден')
    terminal=normalize_city(match.group(1))
    if terminal==normalize_city(origin):return terminal
    # The current Krasnodar city selector exports a PDF headed "Новая Адыгея".
    # Accept this only if BOTH the PDF and the current official Krasnodar
    # branch page confirm the same primary terminal street and building.
    if normalize_city(origin)=='Краснодар' and terminal=='Новая Адыгея':
        url='https://tk-kit.ru/contacts/branch?kladr_city_code=230000100000'
        soup=BeautifulSoup(catalog_bytes(url),'lxml')
        heading=soup.find('h1')
        address=re.compile(r'песочная,\s*(?:д\.\s*)?3/5\s*а',re.I)
        primary=[p.get_text(' ',strip=True) for p in soup.find_all('p') if 'Доставка грузов по городу Краснодару' in p.get_text()]
        if heading and heading.get_text(' ',strip=True)=='Контакты в г. Краснодар' and address.search(text) and any(address.search(p) for p in primary):
            return terminal
    raise ValueError(f'КИТ: город в PDF ({terminal}) не подтверждён как терминал отправления из {origin}')


def bsk_route_url(origin,destination):
    page='https://123789.ru/prices'
    soup=BeautifulSoup(catalog_bytes(page),'lxml');params=[]
    from urllib.parse import urlencode
    for ident,city in [('ship_city',origin),('dest_city',destination)]:
        select=soup.select_one('#'+ident)
        if select is None or select.get('name')!=ident+'[]':
            raise ValueError('БСК: изменился выбор городов в разделе тарифов')
        value=exact_id([(x.get('value',''),x.get_text(' ',strip=True)) for x in select.select('option[value]')],city,'БСК')
        params.append((select['name'],value))
    return page+'?'+urlencode(params)
