"""Current official tariffs. Each adapter validates its own route and layout.

No bundled values or calculator guesses are used here. The legacy collectors
remain available for the carriers with PDF/API-specific integrations.
"""
from __future__ import annotations
from .cities import city_pattern, city_slug, UnpublishedTariff
from . import carrier_catalogs

import hashlib
import io
import json
import math
import re
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from openpyxl import load_workbook

from .v42_engine import COMMON_PROFILES, normalize_city

ADAPTERS = {'Мейджик', 'Пролайн', 'ФастТранс', 'АТЭК', 'БСК', 'Фортуна', 'ЭкспедицияПлюс'}


def number(value):
    if isinstance(value, bool):
        raise ValueError('Вместо цены получено логическое значение')
    text = re.sub(r'[\s\u00a0\u202f]', '', str(value)).replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        raise ValueError(f'Числовая колонка не распознана: {str(value)[:60]}')
    result = float(text)
    if not math.isfinite(result) or result <= 0:
        raise ValueError('Тариф должен быть положительным числом')
    return result


def norm(value):
    return ' '.join(str(value or '').lower().replace('ё', 'е').split())


def text(node):
    return node.get_text(' ', strip=True) if node else ''


def bounds(label):
    label = re.sub(r'(?<=\d)[ \u00a0](?=\d{3}\b)', '', norm(label))
    nums = [float(x.replace(',', '.')) for x in re.findall(r'\d+(?:[.,]\d+)?', label)]
    if not nums:
        raise ValueError(f'Не распознан весовой диапазон: {label}')
    if len(nums) >= 2:
        return nums[0], nums[1]
    return (nums[0], math.inf) if any(x in label for x in ('от', 'свыше')) else (0, nums[0])


def values_from_tiers(tiers, *, fixed=(), minimum=None):
    """Tiers are inclusive printed bounds. Never extend a finite final tier."""
    vals = {}
    for p in COMMON_PROFILES:
        if p.get('is_minimum_profile'):
            if minimum is not None:
                vals[p['id']] = {'kind': 'exact', 'price': number(minimum)}
            continue
        weight = p['weight_kg']
        fixed_hit = next((v for lo, hi, v in fixed if lo <= weight <= hi), None)
        if fixed_hit is not None:
            vals[p['id']] = {'kind': 'exact', 'price': round(number(fixed_hit), 2)}
            continue
        rate = next((v for lo, hi, v in tiers if lo <= weight <= hi), None)
        if rate is not None:
            rate = number(rate)
            vals[p['id']] = {'kind': 'exact', 'price': round(max(minimum or 0, weight * rate), 2),
                             'rate_per_kg': rate, 'minimum': minimum}
    if not vals:
        raise ValueError('Для выбранного маршрута не распознан ни один диапазон')
    return vals


def fetch(company, origin, destination, url, fmt='HTML', referer=None, *, prefer_ranges=False):
    from pathlib import Path
    from .v42_collectors import _bounded_source_download
    from . import document_cache
    key=(company,origin,url,fmt,referer)
    cached=document_cache.get(key)
    if cached:
        raw,meta=cached
        return raw,{**meta,'origin':origin,'destination':destination}
    ident = hashlib.sha256(f'{company}|{origin}|{destination}|{url}'.encode()).hexdigest()[:16]
    source = {'id': 'LIVE-' + ident, 'company': company, 'block_title': 'official',
              'url': url, 'reference_url': referer or url, 'document_format': fmt,'prefer_ranges':prefer_ranges,
              'range_chunk_bytes':14000 if company=='Фортуна' else 8000}
    result = _bounded_source_download(source)
    if result.get('status') != 'downloaded':
        raise RuntimeError(result.get('error') or 'Официальный источник не ответил')
    raw = Path(result['path']).read_bytes()
    final = result.get('final_url') or url
    # A redirect to another carrier or an authentication host is not tariff evidence.
    host = urlsplit(url).hostname.removeprefix('www.')
    final_host = (urlsplit(final).hostname or '').removeprefix('www.')
    if final_host != host and not final_host.endswith('.' + host):
        raise ValueError('Источник перенаправил запрос на посторонний сайт')
    meta = {'origin': origin, 'destination': destination, 'source_url': final,
            'source_file': result['file'], 'sha256': hashlib.sha256(raw).hexdigest(),
            'captured_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'source_type': f'Официальный прайс {company}', 'transport': result.get('connector'),
            'calculation_basis': 'Опубликованный тариф по весу; объём и дополнительные услуги не включены'}
    from .source_conditions import document_conditions
    meta.update(document_conditions(raw,fmt))
    document_cache.put(key,raw,meta)
    return raw, meta


def discover_price_link(raw, page_url, company):
    soup = BeautifulSoup(raw, 'lxml')
    candidates = []
    for a in soup.select('a[href]'):
        url = urljoin(page_url, a['href'])
        if (urlsplit(url).hostname or '').removeprefix('www.') != (urlsplit(page_url).hostname or '').removeprefix('www.'):
            continue
        if not re.search(r'\.(xlsx|xls)(?:\?|$)', url, re.I):
            continue
        label = norm(text(a))
        if company == 'Фортуна' and 'общий прайс' in label:
            candidates.append(url)
        elif company == 'ЭкспедицияПлюс' and re.search(r'прайс[ -]*лист', label):
            candidates.append(url)
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        raise ValueError('На странице не найдена единственная ссылка на основной междугородний прайс')
    return candidates[0]


def parse_workbook(company, raw, origin, destination):
    from .tariff_documents import workbook_view
    wb = workbook_view(raw)
    try:
        sheet=origin if origin in wb.sheetnames else None
        if company=='Фортуна':
            confirmed=[s.title for s in wb if any(norm(r[0])=='из/в г. '+norm(origin) for r in list(s.values)[:12] if r)]
            if len(confirmed)==1:sheet=confirmed[0]
        if sheet is None:
            raise UnpublishedTariff(f'В прайсе нет листа города отправления {origin}')
        rows = list(wb[sheet].values)
        if company == 'ЭкспедицияПлюс':
            if not any(('из г.' + origin).lower() in norm(' '.join(str(v or '') for v in r)) for r in rows[:8]):
                raise ValueError('Заголовок прайса не подтверждает город отправления')
            headers = next(r for r in rows[:12] if sum('кг' in norm(v) for v in r) >= 5)
            matches = [r for r in rows if normalize_city(str(r[0] or '')) == destination]
            if len(matches) != 1:
                raise ValueError('Не найдена однозначная строка назначения в прайсе')
            row = matches[0]
            tiers = [(*bounds(h), number(row[i])) for i, h in enumerate(headers) if 'кг' in norm(h)]
            return values_from_tiers(tiers, minimum=number(row[1]))
        # Fortuna publishes TWO sections on each city sheet: inbound then outbound.
        # Reading the first destination row silently reverses the route.
        headers = next(r for r in rows[:12] if any('до 0,99 кг' in str(v) for v in r))
        active = False
        matches = []
        for row_no,row in enumerate(rows,1):
            first = norm(row[0])
            if first.startswith('из г.'):
                active = norm(origin) == first.removeprefix('из г.').strip()
                continue
            if first.startswith('в г.'):
                active = False
            if active and normalize_city(str(row[0] or '')) == destination:
                matches.append((row_no,row))
        # The same destination may occur for road and rail. The comparison
        # uses the road tariff, never a cheaper railway row below it.
        road=[m for m in matches if 'авто' in norm(m[1][2])]
        if road:matches=road
        elif matches and all('жд' in norm(m[1][2]).replace('/','').replace(' ','') for m in matches):
            raise UnpublishedTariff('Фортуна: в документе есть только железнодорожная перевозка; автотариф не опубликован')
        if len(matches) != 1:
            raise ValueError('Не найдена единственная исходящая строка Фортуны')
        row_no,row = matches[0]
        # Blank published cells affect only their own bands, not the entire
        # route. Malformed nonblank values still fail visibly.
        available=lambda v:v is not None and norm(v).rstrip('.') not in {'','-','—','–','дог','договорная','по запросу'}
        fixed = [(*bounds(headers[i]), number(row[i])) for i in range(3, 7) if available(row[i])]
        tiers = [(*bounds(headers[i]), number(row[i])) for i in range(8, 15) if available(row[i])]
        minimum=number(row[7]) if available(row[7]) else None
        if not fixed and not tiers:
            raise UnpublishedTariff('Фортуна: стоимость договорная («дог» в официальном прайсе), числовые ставки не опубликованы')
        values=values_from_tiers(tiers, fixed=fixed, minimum=minimum)
        # Include the <1 kg fixed band in MIN even though the first control
        # point in the comparison is exactly 1 kg.
        candidates=[v for _,_,v in fixed]+([minimum] if minimum else [])
        if candidates:values['min']={'kind':'exact','price':min(candidates)}
        terms=[str(v).strip() for r in rows for v in r if isinstance(v,str) and 'ндс' in v.lower()]
        basis='Фортуна: исходящий автотариф по весу; отдельные железнодорожные ставки исключены. '+(' '.join(dict.fromkeys(terms))[:1200] if terms else 'Налоговые условия — по оригиналу прайса.')
        for value in values.values():
            value['source_row']=f'{sheet}!{row_no}'
            value['calculation_basis']=basis
        return values
    finally:
        wb.close()


def parse_bsk_route(raw, origin, destination):
    """Read the official route form, never infer a missing terminal-page row."""
    soup=BeautifulSoup(raw,'lxml');origin=normalize_city(origin);destination=normalize_city(destination)
    for ident,city in [('ship_city',origin),('dest_city',destination)]:
        selected=soup.select('#'+ident+' option[selected]')
        if len(selected)!=1 or normalize_city(text(selected[0]))!=city:
            raise ValueError('БСК: ответ не подтвердил выбранные города')
    matches=[]
    for heading in soup.select('h4'):
        if not text(heading).startswith('Тарифы на грузоперевозки маршрута'):continue
        direction=[normalize_city(x.strip()) for x in re.split(r'→+',text(heading.find('strong'))) if x.strip()]
        if direction==[origin,destination]:matches.append(heading)
    if not matches:raise UnpublishedTariff('БСК: раздел тарифов не опубликовал выбранный маршрут')
    if len(matches)!=1:raise ValueError('БСК: несколько тарифов для выбранного маршрута; требуется проверка')
    heading=matches[0];table=heading.parent.find('table')
    if table is None:raise ValueError('БСК: нет таблицы выбранного маршрута')
    groups=table.select('thead tr');body=table.select('tbody tr')
    if len(groups)!=2 or len(body)!=1:raise ValueError('БСК: изменилась структура тарифной таблицы')
    top=groups[0].find_all(['th','td'],recursive=False)
    headers=groups[1].find_all(['th','td'],recursive=False)
    cells=body[0].find_all(['th','td'],recursive=False)
    if len(top)!=5 or 'мин. стоимость' not in text(top[1]) or re.sub(r'\s+','',text(top[2]))!='стоимостьза1кг₽':
        raise ValueError('БСК: весовые колонки и валюта не подтверждены')
    count=int(top[2].get('colspan',0));volume_count=int(top[3].get('colspan',0))
    if count<1 or len(headers)!=count+volume_count or len(cells)!=3+count+volume_count:
        raise ValueError('БСК: число цен не совпадает с заголовками')
    def price(cell):
        value=text(cell).replace('\xa0','').replace(' ','')
        # The route page uses English thousands groups, e.g. 1,234.50.
        if re.fullmatch(r'\d{1,3}(?:,\d{3})+\.\d{2}',value):value=value.replace(',','')
        return number(value)
    tiers=[];previous=0
    for h,c in zip(headers[:count],cells[2:2+count]):
        label=text(h)
        if not re.fullmatch(r'До\s+[\d\s]+\s*кг',label,re.I):raise ValueError('БСК: весовой заголовок не распознан')
        high=bounds(label)[1]
        if high<=previous:raise ValueError('БСК: неверный порядок весовых диапазонов')
        value=None if text(c) in {'','-','—','–'} else price(c)
        tiers.append((previous,high,value));previous=high
    # Parcel is a separate product with its own volume restriction; use the
    # published cargo minimum, as in the other carrier comparisons.
    values=values_from_tiers(tiers,minimum=price(cells[1]))
    jumps=[(lo,hi,rate) for (prev_lo,prev_hi,prev_rate),(lo,hi,rate) in zip(tiers,tiers[1:]) if prev_rate and rate and rate>prev_rate*10]
    if jumps:
        for profile in COMMON_PROFILES:
            weight=profile['weight_kg']
            for lo,hi,rate in jumps:
                if not profile.get('is_minimum_profile') and lo<weight<=hi and profile['id'] in values:
                    values[profile['id']]['calculation_basis']=f'Проверьте у БСК: источник публикует резкий рост ставки до {rate:g} ₽/кг для {lo:g}–{hi:g} кг. Значение прочитано буквально, без исправления или подстановки.'
    note='Опубликованный весовой тариф БСК; max(минимальная плата, вес × ставка). Бандероль, объём и допуслуги не включены.'
    if '→→' in text(heading):
        via=text(heading) if not heading.get('title') else heading['title'].strip()
        note+=' Источник указал составной маршрут; пометка: '+via+'.'
    return values,{'calculation_basis':note,'route_verified':True}


def parse_bsk(raw, origin, destination):
    soup = BeautifulSoup(raw, 'lxml')
    # The terminal page carries outbound tariffs in its single tariff table.
    if origin not in text(soup.find('h1')):
        raise ValueError('БСК: открыта страница другого терминала')
    tables = [t for t in soup.select('table') if 'Бандероль' in text(t) and 'мин. стоимость' in text(t)]
    if len(tables) != 1:
        raise ValueError('БСК: не найдена таблица тарифов')
    table = tables[0]
    headers = table.select('thead tr')[-1].find_all(['th', 'td'], recursive=False)
    matches = [r for r in table.select('tr') if r.find(['td','th']) and normalize_city(text(r.find(['td','th']))) == destination]
    if len(matches) != 1:
        raise ValueError('БСК: не найдена строка назначения')
    cells = matches[0].find_all(['td','th'], recursive=False)
    if len(cells) != 4 + len(headers):
        raise ValueError('БСК: изменилась структура весовых колонок')
    tiers=[]; previous=0
    for h, c in zip(headers, cells[4:]):
        high = bounds(text(h).split('кг')[0])[1]
        # Each cell contains weight and volume prices on separate lines.
        first = c.get_text('|', strip=True).split('|')[0]
        tiers.append((previous, high, None if first.strip() in {"", "-", "—", "–"} else number(first)))
        previous=high
    return values_from_tiers(tiers, minimum=number(text(cells[2])))


def parse_fastrans(raw, origin, destination):
    soup = BeautifulSoup(raw, 'lxml')
    heading = text(soup.find('h1'))
    if origin not in heading or destination not in heading or heading.index(origin) > heading.index(destination):
        raise ValueError('ФастТранс: страница относится к другому направлению')
    marker = soup.select_one('.price-table__kg')
    if marker is None:
        raise ValueError('ФастТранс: таблица весовых тарифов не найдена')
    table = marker.find_parent('table')
    rows = table.select('tr')
    headers = rows[0].find_all(['th','td'], recursive=False)
    rates = rows[1].find_all('td', recursive=False)
    if len(headers) != len(rates) or not all(c.select_one('.price-table__r-kg') for c in rates):
        raise ValueError('ФастТранс: изменилась структура тарифов')
    tiers=[]; previous=0
    for h,c in zip(headers,rates):
        boundary=number(text(h))
        high=math.inf if h.select_one('.price-table__from') else boundary
        tiers.append((previous,high,number(text(c))));previous=boundary
    zone=text(soup)
    start=zone.find('Фиксированные тарифы'); end=zone.find('Цены указаны',start)
    if start<0 or end<0:raise ValueError('ФастТранс: фиксированные тарифы не распознаны')
    fixed=[];previous=0
    for kg, money in re.findall(r'До\s*([\d,]+)\s*кг/.*?:\s*([\d\s]+)₽',zone[start:end],re.I):
        high=number(kg);fixed.append((previous,high,number(money)));previous=high
    if not fixed:raise ValueError('ФастТранс: пустой блок фиксированных тарифов')
    return values_from_tiers(tiers,fixed=fixed)


def parse_atec(raw, origin, destination):
    require_corridor('АТЭК',origin,destination)
    soup=BeautifulSoup(raw,'lxml')
    content=text(soup)
    match=re.search(r'СТОИМОСТЬ МЕЖТЕРМИНАЛЬНОЙ ПЕРЕВОЗКИ (.+?)СТОИМОСТЬ ЭКСПЕДИРОВАНИЯ',content,re.I)
    if not match:raise ValueError('АТЭК: блок межтерминальных тарифов не найден')
    zone=match.group(1)
    for route in ('САНКТ-ПЕТЕРБУРГ - МОСКВА','МОСКВА - САНКТ-ПЕТЕРБУРГ'):
        if route not in zone:raise ValueError('АТЭК: не подтверждено действие тарифа в обе стороны')
    headers=re.findall(r'(?:до|от)\s+[\d\s]+\s*кг',zone,re.I)
    values=re.findall(r'([\d\s]+(?:[,.]\d+)?)\s*₽\s*(\(фикс\.\)|/кг)',zone)
    if len(headers)!=len(values) or not headers:raise ValueError('АТЭК: число колонок изменилось')
    tiers=[];fixed=[];previous=0
    for h,(v,unit) in zip(headers,values):
        lo,hi=bounds(h);lo=max(lo,previous)
        (tiers if unit=='/кг' else fixed).append((lo,hi,number(v)));previous=hi
    return values_from_tiers(tiers,fixed=fixed)


def parse_proline(raw, origin, destination):
    require_corridor('Пролайн',origin,destination)
    soup=BeautifulSoup(raw,'lxml')
    # Site form maps origin id 2 to form-city-1 (SPB) and id 1 to form-city-2 (Moscow).
    form=soup.select_one('select[name="from-t"]')
    form=form.find_parent('form') if form else None
    if form is None:raise ValueError('Пролайн: форма направления не найдена')
    options={o['value']:norm(text(o)) for o in form.select('select[name="from-t"] option[value]')}
    if 'петербург' not in options.get('2','') or 'моск' not in options.get('1',''):
        raise ValueError('Пролайн: изменились идентификаторы городов')
    block=soup.select_one('.form-city-1' if origin=='Санкт-Петербург' else '.form-city-2')
    if block is None:raise ValueError('Пролайн: нет таблицы выбранного направления')
    table=next((t for t in block.select('table') if 'Цена за кг' in text(t)),None)
    if table is None:raise ValueError('Пролайн: весовая таблица отсутствует')
    rows=table.select('tr');headers=rows[0].find_all('td')[1:];rates=rows[1].find_all('td')[1:]
    if len(headers)!=len(rates):raise ValueError('Пролайн: изменилась структура таблицы')
    # Only the printed kg rates are known here; the shipment minimum is separate.
    tiers=[];previous=0
    for h,c in zip(headers,rates):
        hi=bounds(text(h))[1];tiers.append((previous,hi,number(text(c))));previous=hi
    vals=values_from_tiers(tiers)
    # Small shipments can have a minimum order charge, absent from this table.
    return {pid:v for pid,v in vals.items() if next(p['weight_kg'] for p in COMMON_PROFILES if p['id']==pid)>=100}


def parse_magic(raw, destination):
    rows=json.loads(raw.decode('utf-8-sig'))
    if not isinstance(rows,list):raise ValueError('Мейджик: вместо списка тарифов получен другой ответ')
    matches=[r for r in rows if normalize_city(text(BeautifulSoup(str(r.get('NAME','')),'lxml'))) == destination]
    if len(matches)!=1:raise ValueError('Мейджик: строка назначения не подтверждена')
    r=matches[0]
    fixed=[(0,1,number(r['1']['M'])),(1,5,number(r['2']['M']))]
    tiers=[];previous=0
    for i,high in enumerate((250,750,1250,2500,5000,10000,20000),3):
        tiers.append((previous,high,number(r[str(i)]['M'])));previous=high
    return values_from_tiers(tiers,fixed=fixed,minimum=number(r['MIN_PRICE']))


def parse_railcontinent(raw, origin, destination):
    soup=BeautifulSoup(raw,'lxml')
    aliases={'Москва':r'москва','Санкт-Петербург':r'(?:с[.\s–—-]*петербург|санкт[\s–—-]*петербург|спб)'}
    def city_matches(value,city):return normalize_city(value)==normalize_city(city) or bool(re.fullmatch(aliases.get(city, re.escape(norm(city))),norm(value)))
    section=re.compile(r'^тарифы\s+(из города|в город)\s+(.+?)\s+по\s+(весу|объ[её]му)',re.I)
    current=None;matches=[]
    for node in soup.find_all(True):
        # Only a short leaf heading identifies a section. A containing DIV that
        # includes multiple tables must never lend its label to the first table.
        if node.name!='table' and not node.find(['table','h1','h2','h3','h4','h5','p','div']):
            candidate=section.search(norm(text(node)))
            if candidate:current=candidate.groups()
        if node.name!='table' or not current:continue
        direction,city,unit=current
        if direction!='из города' or unit!='весу' or not city_matches(city,origin):continue
        rows=node.find_all('tr')
        header=next((r.find_all(['td','th'],recursive=False) for r in rows if 'мин.' in norm(text(r)) and 'до 99' in norm(text(r))),None)
        if not header or len(header)<18:continue
        for row in rows:
            cells=row.find_all(['td','th'],recursive=False)
            if len(cells)!=len(header) or not city_matches(text(cells[0]),destination) or norm(text(cells[1]))!='авто':continue
            tiers=[]
            for h,c in zip(header[5:],cells[5:]):
                label=norm(text(h))
                lo,hi=bounds(label)
                if 'и более' in label:hi=math.inf
                tiers.append((lo,hi,number(text(c))))
            matches.append(values_from_tiers(tiers,minimum=number(text(cells[2]))))
    if len(matches)!=1:raise ValueError('Рейл Континент: исходящая весовая таблица и единственная строка назначения не подтверждены')
    return matches[0]


def parse_railcontinent_workbook(raw, origin, destination):
    aliases={'Москва':r'москва','Санкт-Петербург':r'(?:с[.\s–—-]*петербург|санкт[\s–—-]*петербург|спб)'}
    def city_matches(value,city):return normalize_city(value)==normalize_city(city) or bool(re.fullmatch(aliases.get(city, re.escape(norm(city))),norm(value)))
    section=re.compile(r'^тарифы\s+(из города|в город)\s+(.+?)\s+руб/(кг|м3)$')
    matches=[]
    from .tariff_documents import workbook_view
    wb=workbook_view(raw)
    try:
        for ws in wb:
            current=None;header=None
            for row in ws.values:
                row=list(row)
                while row and row[-1] in (None,''):row.pop()
                if not row:continue
                title=section.fullmatch(norm(row[0]))
                if title:current=title.groups();header=None;continue
                if not current:continue
                direction,city,unit=current
                if direction!='из города' or unit!='кг' or not city_matches(city,origin):continue
                if norm(row[0])=='в город' and len(row)>=18 and norm(row[2])=='мин. цена':header=row;continue
                if not header or len(row)!=len(header) or not city_matches(row[0],destination) or norm(row[1])!='авто' or norm(row[4])!='нет':continue
                tiers=[]
                for label,rate in zip(header[5:],row[5:]):
                    lo,hi=bounds(label)
                    if 'и более' in norm(label):hi=math.inf
                    tiers.append((lo,hi,number(rate)))
                matches.append(values_from_tiers(tiers,minimum=number(row[2])))
    finally:wb.close()
    if len(matches)!=1:raise ValueError('Рейл Континент: XLSX не подтвердил единственную исходящую весовую строку для выбранного маршрута')
    return matches[0]


def collect(company, origin, destination, profile_id="w100"):
    origin=normalize_city(origin);destination=normalize_city(destination)
    if company=='Пролайн' and {origin,destination}!={'Москва','Санкт-Петербург'}:
        from .proline import collect as calculator
        return calculator(origin,destination,profile_id)
    if company=='АТЭК':require_corridor(company,origin,destination)
    if company in {'Фортуна','ЭкспедицияПлюс'}:
        page='https://fte.ru/price/' if company=='Фортуна' else 'https://nevatk.ru/tarif/'
        raw,_=fetch(company,origin,destination,page)
        url=discover_price_link(raw,page,company)
        raw,meta=fetch(company,origin,destination,url,'XLSX',page,prefer_ranges=(company=='Фортуна'))
        vals=parse_workbook(company,raw,origin,destination)
    elif company=='БСК':
        url=carrier_catalogs.bsk_route_url(origin,destination)
        raw,meta=fetch(company,origin,destination,url,'HTML','https://123789.ru/prices')
        vals,details=parse_bsk_route(raw,origin,destination);meta.update(details)
    elif company=='Мейджик':
        if {origin,destination}!={'Москва','Санкт-Петербург'}:carrier_catalogs.magic_cities(origin,destination)
        url='https://magic-trans.ru/include/mt-cost-traffic.php?'+urlencode({'cityFrom':origin,'cityTo':destination})
        raw,meta=fetch(company,origin,destination,url,'JSON','https://magic-trans.ru/tarify/')
        vals=parse_magic(raw,destination)
    else:
        urls={
            'Пролайн':'https://proline.su/dostavka/sbp-msk/',
            'ФастТранс':None,
            'АТЭК':'https://old.atec-logistic.ru/service-new/gruzoperevozki-iz-spb-v-msk/',
            'БСК':'https://123789.ru/terminals-addresses/'+city_slug(origin),
        }
        if company=='ФастТранс':
            urls[company]=('https://fastrans.ru/city/'+('moscow_saint-petersburg/' if origin=='Москва' else 'saint-petersburg_moscow/')) if {origin,destination}=={'Москва','Санкт-Петербург'} else carrier_catalogs.fastrans_route_url(origin,destination)
        raw,meta=fetch(company,origin,destination,urls[company])
        parser={'Пролайн':parse_proline,'ФастТранс':parse_fastrans,'АТЭК':parse_atec,'БСК':parse_bsk}[company]
        vals=parser(raw,origin,destination)
        if company == 'Пролайн':
            from .proline import collect as calculator
            missing=[p['id'] for p in COMMON_PROFILES
                     if not p.get('is_minimum_profile') and p['id'] not in vals]
            if missing:
                try:
                    extra, detail=calculator(origin,destination,profile_ids=missing)
                    # Each row keeps its own original file and calculation.
                    vals.update({pid:{**detail,**item} for pid,item in extra.items()})
                except Exception as exc:
                    meta['partial_errors']=[f'Пролайн, малые веса: {exc}']
    return vals,meta


def require_corridor(company,origin,destination):
    if {normalize_city(origin),normalize_city(destination)}!={'Москва','Санкт-Петербург'}:
        raise UnpublishedTariff(f'{company}: подключённая таблица публикует тарифы только Москва ↔ Санкт-Петербург')
