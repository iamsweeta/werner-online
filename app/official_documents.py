"""Public document endpoints used by Dellin / Vozovoz's own tariff pages."""
from __future__ import annotations
from .business_time import tariff_today

import json
import re
from urllib.parse import urlencode, urlsplit, parse_qs, quote, urljoin

from bs4 import BeautifulSoup

from . import carrier_catalogs
from .network import Download, validate_reply
from .online_tariffs import fetch
from .tariff_documents import parse_document
from .cities import normalize_city


def dellin_city(origin):
    # IDs of the two explicitly identified document sources, never a default.
    known={'Москва':'3','Санкт-Петербург':'1'}
    if origin in known:return known[origin]
    raw=carrier_catalogs.catalog_bytes('https://www.dellin.ru/documents/')
    soup=BeautifulSoup(raw,'lxml');ids=set()
    for a in soup.select('a[href*="pricelist_pdf"]'):
        if normalize_city(a.get_text(' ',strip=True))!=origin:continue
        values=parse_qs(urlsplit(a['href']).query).get('city',[])
        ids.update(v for v in values if v.isdigit())
    for select in soup.select('select[name="city"]'):
        for option in select.select('option[value]'):
            if normalize_city(option.get_text(' ',strip=True))==origin and option['value'].isdigit():ids.add(option['value'])
    if len(ids)!=1:raise ValueError(f'ДЛ: город {origin} не подтверждён в каталоге PDF. Скачайте прайс на сайте и загрузите файл вручную.')
    return ids.pop()


def vozovoz_city_ids(origin,destination):
    raw=carrier_catalogs.catalog_bytes('https://vozovoz.ru/tariffs/')
    node=BeautifulSoup(raw,'lxml').select_one('script#__NUXT_DATA__')
    if node is None:raise ValueError('Возовоз: изменился справочник городов страницы тарифов')
    data=json.loads(node.string or node.get_text())
    if not isinstance(data,list):raise ValueError('Возовоз: неизвестный формат справочника')
    pairs=[]
    for item in data:
        if not isinstance(item,dict):continue
        indexes=[item.get(k) for k in ('name','guid')]
        if not all(isinstance(i,int) and not isinstance(i,bool) and 0<=i<len(data) for i in indexes):continue
        name,guid=(data[i] for i in indexes)
        if isinstance(name,str) and isinstance(guid,str) and re.fullmatch(r'[a-f0-9-]{36}',guid):pairs.append((guid,name))
    return tuple(carrier_catalogs.exact_id(pairs,city,'Возовоз') for city in (origin,destination))


def _vozovoz_generate_reply(payload, transport):
    """Two verified TLS clients for the same public, read-only generator."""
    url='https://api.vozovoz.ru/v1/tariff/get'
    headers={'Origin':'https://vozovoz.ru','Referer':'https://vozovoz.ru/tariffs/'}
    if transport=='requests':
        import requests
        with requests.Session() as session:
            with session.post(url,json=payload,headers=headers,timeout=(10,20),
                              verify=True,allow_redirects=False,stream=True) as response:
                body=bytearray()
                for chunk in response.iter_content(65536):
                    if len(body)+len(chunk)>512*1024:raise ValueError('Возовоз: слишком большой ответ генератора')
                    body.extend(chunk)
                return Download(bytes(body),url,transport,response.headers.get('content-type',''),response.status_code)
    from curl_cffi import requests, CurlOpt, CurlSslVersion, CurlHttpVersion
    body=bytearray()
    def receive(chunk):
        if len(body)+len(chunk)>512*1024:raise ValueError('Возовоз: слишком большой ответ генератора')
        body.extend(chunk)
    with requests.Session(curl_options={CurlOpt.SSLVERSION:CurlSslVersion.TLSv1_2|(int(CurlSslVersion.TLSv1_2)<<16),
                                      CurlOpt.HTTP_VERSION:CurlHttpVersion.V1_1,CurlOpt.IPRESOLVE:1}) as session:
        response=session.post(url,json=payload,headers=headers,
                              timeout=30,verify=True,allow_redirects=False,content_callback=receive)
    return Download(bytes(body),url,transport,response.headers.get('content-type',''),response.status_code)


def vozovoz_generate(dep,arr):
    """Same public request as the site's 'Сформировать' button, no demo key."""
    errors=[]
    for transport in ('curl-cffi-tls12','requests'):
        try:
            reply=_vozovoz_generate_reply({'from':[dep],'to':[arr],'orgId':''},transport)
        except ValueError:
            raise
        except Exception as exc:
            message=str(exc)
            # Do not retry access denials through another client.
            if re.search(r'\b(?:401|403|407)\b',message):
                raise ValueError('Возовоз: доступ к генератору прайсов отклонён сетью или сервером. '
                                 'Скачайте прайс на vozovoz.ru/tariffs/ и загрузите его в «Прайс-листы».') from exc
            errors.append(message[:200])
            continue
        result=validate_reply(reply,'JSON')
        break
    else:
        raise ValueError('Возовоз: генератор прайсов недоступен. '+'; '.join(errors))
    paths=json.loads(result.content)
    if not isinstance(paths,list) or not 1<=len(paths)<=5:raise ValueError('Возовоз: генератор не вернул тарифные файлы')
    urls=[]
    for path in paths:
        if not isinstance(path,str) or not re.fullmatch(r'/tariff/[^/\\?#]+\.(?:xls|xlsx|zip)',path,re.I):
            raise ValueError('Возовоз: генератор вернул неподдерживаемую ссылку')
        urls.append('https://files.vozovoz.ru'+quote(path,safe='/'))
    return list(dict.fromkeys(urls))


def collect(company,origin,destination):
    if company=='Werner':
        page='https://wernerus.ru/clients/prices/'
        dep={'Москва':'35','Санкт-Петербург':'36'}.get(origin)
        # This is a public, undated document endpoint (service 46, origin ID),
        # not a price snapshot. Fetch it even if the HTML catalogue is down.
        direct_error=None
        if dep:
            try:
                url=f'https://wernerus.ru/prices/46_from_{dep}_RUB.xlsx'
                raw,meta=fetch(company,origin,destination,url,'XLSX',referer=page,prefer_ranges=True)
                values,parsed=parse_document(raw,'price.xlsx',company,origin,destination)
            except (ValueError,RuntimeError,OSError) as exc:direct_error=str(exc)
        if not dep or direct_error:
            raw,_=fetch(company,origin,destination,page,'HTML')
            links=set()
            for a in BeautifulSoup(raw,'lxml').select('a[href]'):
                url=urljoin(page,a['href']);parts=urlsplit(url)
                if parts.hostname not in {'wernerus.ru','www.wernerus.ru'} or parts.scheme!='https':continue
                if not re.search(r'\.xlsx?(?:$)',parts.path,re.I):continue
                if (dep and re.search(r'_from_'+dep+r'_RUB\.xlsx?$',parts.path,re.I)) or normalize_city(a.get_text(' ',strip=True))==origin:links.add(url)
            if len(links)!=1:raise ValueError('Werner: не найдена единственная текущая ссылка XLS/XLSX для города отправления. Загрузите скачанный прайс вручную.')
            url=links.pop();ext=urlsplit(url).path.rsplit('.',1)[-1].lower()
            raw,meta=fetch(company,origin,destination,url,ext.upper(),referer=page,prefer_ranges=True)
            values,parsed=parse_document(raw,'price.'+ext,company,origin,destination)
    elif company=='ДЛ':
        url='https://www.dellin.ru/pricelist_pdf/?'+urlencode({'city':dellin_city(origin),'is_region':0,'is_future':0})
        raw,meta=fetch(company,origin,destination,url,'PDF',referer='https://www.dellin.ru/documents/')
        values,parsed=parse_document(raw,'price.pdf',company,origin,destination)
    elif company=='Возовоз':
        urls=vozovoz_generate(*vozovoz_city_ids(origin,destination));matches=[];errors=[]
        for url in urls:
            ext=urlsplit(url).path.rsplit('.',1)[-1].lower()
            try:
                raw,meta=fetch(company,origin,destination,url,ext.upper(),referer='https://vozovoz.ru/tariffs/')
                values,parsed=parse_document(raw,'price.'+ext,company,origin,destination)
                _check_current_document(parsed)
                matches.append((values,meta,parsed))
            except Exception as exc:errors.append(str(exc))
        if not matches:raise ValueError('Возовоз: не получен прайс выбранного маршрута. '+'; '.join(errors[:2]))
        # Identical documents can be returned twice. Different prices must never
        # be silently chosen by file order.
        if any(v!=matches[0][0] for v,_,_ in matches[1:]):
            raise ValueError('Возовоз: документы содержат разные тарифы одного маршрута. Загрузите нужный прайс вручную.')
        values,meta,parsed=matches[0]
        if errors:meta['additional_document_errors']=errors
    else:raise ValueError('Нет такого адаптера документов')
    _check_current_document(parsed)
    return values,{**meta,**parsed,'source_type':'Официальный '+('PDF ДЛ' if company=='ДЛ' else 'Excel Werner' if company=='Werner' else 'Excel Возовоза · обычная перевозка')}


def _check_current_document(parsed):
    if parsed.get('document_date'):
        from datetime import date
        if date.fromisoformat(parsed['document_date'])>tariff_today():raise ValueError('Документ содержит будущие тарифы вместо текущих')
