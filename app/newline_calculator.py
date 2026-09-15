"""Ask New Line's public calculator only for weights missing from its PDF."""
from __future__ import annotations
import hashlib
import json
import math
from datetime import datetime
from bs4 import BeautifulSoup
from .network import Download, validate_reply, USER_AGENT, MAX_BYTES, download
from .carrier_catalogs import exact_id
from .v42_engine import PROFILE_BY_ID

PAGE='https://tknl.ru/calculator/'
ENDPOINT='https://tknl.ru/bitrix/components/colorit/calculator4/ajax/calc.php'


def parse_reply(data, weight):
    if not isinstance(data,dict) or data.get('status') is not True:
        raise ValueError('Новая Линия: '+str((data or {}).get('message','ошибка калькулятора'))[:300])
    row=data.get('result_nocourier') or data.get('result')
    if not isinstance(row,dict):raise ValueError('Новая Линия: отсутствует детализация расчёта')
    if row.get('individual') is True or row.get('no_calc') is True:
        return None
    if not math.isclose(float(row.get('fullWeight_normal',-1)),weight,abs_tol=.001):
        raise ValueError('Новая Линия: ответ не подтвердил запрошенный вес')
    shipping=row.get('shipping') or []
    candidates=[x for x in shipping if x.get('code')=='delivery' and x.get('type')==2
                and 'межтерминальная' in str(x.get('name','')).lower()]
    if len(candidates)!=1:raise ValueError('Новая Линия: межтерминальная стоимость не выделена')
    value=candidates[0].get('value')
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
        raise ValueError('Новая Линия: некорректная сумма перевозки')
    return round(value,2)


def collect_missing(origin, destination, origin_terminal, destination_terminal, profile_ids):
    from curl_cffi import requests as cr, CurlOpt, CurlSslVersion, CurlHttpVersion
    from .legacy import DOWNLOAD_DIR
    page=download(PAGE,expected='HTML',timeout=35)
    soup=BeautifulSoup(page.content,'lxml')
    ids=[]
    for selector,city in [('#changeFrom',origin_terminal),('#changeTo',destination_terminal)]:
        select=soup.select_one(selector)
        if select is None:raise ValueError('Новая Линия: справочник калькулятора изменился')
        ids.append(exact_id([(o.get('value',''),o.get_text(' ',strip=True)) for o in select.select('option[value]')],city,'Новая Линия'))
    values={};unavailable={};responses=[]
    opts={CurlOpt.SSLVERSION:CurlSslVersion.TLSv1_2|(int(CurlSslVersion.TLSv1_2)<<16),
          CurlOpt.IPRESOLVE:1,CurlOpt.HTTP_VERSION:CurlHttpVersion.V1_1}
    with cr.Session(curl_options=opts) as session:
        for pid in profile_ids:
            weight=PROFILE_BY_ID[pid]['weight_kg'];volume=weight/250
            # The form requires an insured value to run. Only the separately
            # named interterminal shipping line is used, never the insured total.
            fields={'FORM_TYPE':'calc','AJAX':'Y','FROM[CITY_ID]':ids[0],'TO[CITY_ID]':ids[1],
                    'FROM[IS_DELIVERY]':'N','TO[IS_DELIVERY]':'N','PARAMS':'all','PARAMS[1][TYPE]':'all',
                    'PARAMS[1][WEIGHT]':str(weight),'PARAMS[1][VOLUME]':str(volume),
                    'PARAMS[1][COUNT_PLACE]':str(max(1,math.ceil(weight/500))),
                    'GRUZ':'Груз','INSURANCE[IS_ACTIVE]':'Y','INSURANCE[PRICE]':'100'}
            body=bytearray()
            def receive(chunk):
                if len(body)+len(chunk)>MAX_BYTES:raise ValueError('Новая Линия: ответ слишком большой')
                body.extend(chunk)
            result=session.post(ENDPOINT,data=fields,headers={'User-Agent':USER_AGENT,'Referer':PAGE,'X-Requested-With':'XMLHttpRequest'},
                                timeout=25,verify=True,allow_redirects=False,content_callback=receive)
            reply=validate_reply(Download(bytes(body),result.url,'curl-cffi-tls12',result.headers.get('Content-Type',''),result.status_code),'JSON')
            data=json.loads(reply.content);price=parse_reply(data,weight)
            responses.append({'profile_id':pid,'request':fields,'response':data})
            if price is None:
                unavailable[pid]='Официальный калькулятор: индивидуальный расчёт для этого веса; цена предоставляется по запросу.'
            else:values[pid]={'kind':'exact','price':price,'volume_m3':volume,
                              'calculation_basis':f'Калькулятор: {weight:g} кг / {volume:g} м³; отдельная строка межтерминальной доставки, без страхования и допуслуг.'}
    stamp=datetime.now().astimezone().isoformat(timespec='seconds')
    evidence=json.dumps({'origin':origin,'destination':destination,'origin_terminal':origin_terminal,
                         'destination_terminal':destination_terminal,'captured_at':stamp,'calculations':responses},ensure_ascii=False,indent=2).encode()
    digest=hashlib.sha256(evidence).hexdigest();filename='LIVE-newline-'+digest[:20]+'.json'
    DOWNLOAD_DIR.mkdir(parents=True,exist_ok=True);(DOWNLOAD_DIR/filename).write_bytes(evidence)
    meta={'source_url':PAGE,'source_file':filename,'sha256':digest,'captured_at':stamp,
          'source_type':'Официальный калькулятор Новая Линия','transport':'curl-cffi-tls12'}
    return {pid:{**meta,**row} for pid,row in values.items()},unavailable,meta
