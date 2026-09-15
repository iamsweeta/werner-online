"""The published Proline route calculator, using its own form and CSRF token.

Only separately published regional direction pages are accepted. A generic
calculator can otherwise return its default minimum for an unsupported pair.
"""
from __future__ import annotations
import hashlib,json,math,re,time
from datetime import datetime
from bs4 import BeautifulSoup
from .cities import normalize_city,UnpublishedTariff
from .carrier_catalogs import exact_id
from .network import Download,validate_reply,USER_AGENT,MAX_BYTES
from .v42_engine import PROFILE_BY_ID

ROUTE_PAGES={
    ('Москва','Санкт-Петербург'):'https://proline.su/dostavka/sbp-msk/',
    ('Санкт-Петербург','Москва'):'https://proline.su/dostavka/sbp-msk/',
    ('Санкт-Петербург','Краснодар'):'https://proline.su/dostavka/sankt-peterburg-krasnodar/',
    ('Санкт-Петербург','Ростов-на-Дону'):'https://proline.su/dostavka/spb-rostov-na-donu/',
}

def form_config(raw):
    soup=BeautifulSoup(raw,'lxml')
    for node in soup.select('script:not([src])'):
        match=re.search(r'var calcConf\s*=\s*("(?:[^"\\]|\\.)*")\s*;',node.get_text())
        if match:
            config=json.loads(json.loads(match.group(1)))
            if isinstance(config.get('cities'),dict):return soup,config['cities']
    raise ValueError('Пролайн: справочник формы калькулятора изменился')

def response_price(data):
    from .online_tariffs import number
    if not isinstance(data,dict) or data.get('error'):raise ValueError('Пролайн: калькулятор не подтвердил стоимость')
    price=number(data.get('price_real'));visible=number(data.get('price'))
    work=data.get('worklist')
    if not isinstance(work,list) or not work:raise ValueError('Пролайн: нет детализации расчёта')
    total=sum(number(x.get('price')) for x in work)
    if not math.isclose(price,visible,abs_tol=.01) or not math.isclose(price,total,abs_tol=.01):
        raise ValueError('Пролайн: сумма расчёта не совпадает с детализацией')
    return price

def collect(origin,destination,profile_id='w100', *, profile_ids=None):
    from curl_cffi import requests as cr,CurlOpt,CurlSslVersion,CurlHttpVersion
    from .v42_collectors import _route_soup
    from .legacy import DOWNLOAD_DIR
    origin=normalize_city(origin);destination=normalize_city(destination)
    url=ROUTE_PAGES.get((origin,destination))
    if not url:raise UnpublishedTariff('Пролайн: нет подключённой опубликованной страницы для этой пары городов')
    ids=list(dict.fromkeys(profile_ids or [profile_id]))
    ids=['w001' if pid=='min' else pid for pid in ids]
    corridor={origin,destination}=={'Москва','Санкт-Петербург'}
    deadline=time.monotonic()+max(50, 12*len(ids))
    headers={'User-Agent':USER_AGENT,'Cache-Control':'no-cache'}
    with cr.Session(curl_options={CurlOpt.SSLVERSION:CurlSslVersion.TLSv1_2|(int(CurlSslVersion.TLSv1_2)<<16),
                                  CurlOpt.IPRESOLVE:1,CurlOpt.HTTP_VERSION:CurlHttpVersion.V1_1}) as session:
        def request(method,target,**kwargs):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Пролайн: истекло время расчёта')
            body=bytearray()
            def receive(chunk):
                if len(body)+len(chunk)>MAX_BYTES:raise ValueError('Пролайн: ответ слишком большой')
                body.extend(chunk)
            response=session.request(method,target,timeout=min(25,remaining),verify=True,allow_redirects=False,
                                     content_callback=receive,**kwargs)
            reply=Download(bytes(body),response.url,'curl-cffi-tls12',response.headers.get('Content-Type',''),response.status_code)
            if 300<=reply.status_code<400:raise ValueError('Пролайн: перенаправление формы; маршрут не подтверждён')
            return validate_reply(reply,'JSON' if method=='POST' else 'HTML')
        page=request('GET',url,headers=headers)
        if corridor:
            from .online_tariffs import parse_proline
            parse_proline(page.content,origin,destination)
        else:
            _route_soup(page.content,page.url,url,origin,destination)
        soup,cities=form_config(page.content)
        entries=[(key,value.get('title','')) for key,value in cities.items()]
        dep=exact_id(entries,origin,'Пролайн');arr=exact_id(entries,destination,'Пролайн')
        csrf=soup.select_one('meta[name="csrf-token"]');param=soup.select_one('meta[name="csrf-param"]')
        if not csrf or not param or not csrf.get('content') or not param.get('content'):
            raise ValueError('Пролайн: параметры защиты формы не найдены')
        values={}; responses=[];errors={}
        for pid in ids:
            weight=PROFILE_BY_ID[pid]['weight_kg']
            volume=0.0 if corridor else weight/200
            cargo={'v':f'{volume:g}','w':f'{weight:g}','from':dep,'to':arr,'from_type':'1','to_type':'1',
                   'cargo_places':'1','cargo_get_time':cities[arr].get('cargo_get_time',''),
                   'from_out_kad_km':'0','to_out_kad_km':'0','from_in_ttk':'0','from_in_gr':'0',
                   'to_in_ttk':'0','to_in_gr':'0','address_city_from':'','address_city_to':''}
            fields={'data['+key+']':value for key,value in cargo.items()};fields[param['content']]=csrf['content']
            try:
                result=request('POST','https://proline.su/',data=fields,headers={**headers,'Referer':url,'X-Requested-With':'XMLHttpRequest'})
                data=json.loads(result.content);price=response_price(data)
            except Exception as exc:
                if not values:raise
                errors[pid]=str(exc);continue
            basis=f'Калькулятор: {weight:g} кг, {volume:g} м³, 1 место, терминал → терминал; без дополнительных услуг'
            values[pid]={'kind':'exact','price':price,'volume_m3':volume,'calculation_basis':basis,
                         'captured_at':datetime.now().astimezone().isoformat(timespec='seconds')}
            responses.append({'profile_id':pid,'request':cargo,'response':data})
    stamp=datetime.now().astimezone().isoformat(timespec='seconds')
    evidence=json.dumps({'origin':origin,'destination':destination,'captured_at':stamp,'source_url':url,
                         'calculations':responses,'errors':errors},ensure_ascii=False,indent=2).encode()
    digest=hashlib.sha256(evidence).hexdigest();filename='LIVE-proline-'+digest[:20]+'.json'
    DOWNLOAD_DIR.mkdir(parents=True,exist_ok=True);(DOWNLOAD_DIR/filename).write_bytes(evidence)
    meta={'origin':origin,'destination':destination,'captured_at':stamp,'source_url':url,'source_file':filename,
          'sha256':digest,'transport':result.transport,'source_type':'Официальный калькулятор Пролайн',
          'volume_m3':volume,'calculation_basis':f'Калькулятор: {weight:g} кг, {volume:g} м³, 1 место, терминал → терминал; без дополнительных услуг',
          'partial_errors':[f'{pid}: {error}' for pid,error in errors.items()][:5],'missing_profile_errors':errors}
    return values,meta
