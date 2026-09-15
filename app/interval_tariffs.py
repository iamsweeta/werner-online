"""Explicit weight intervals, with independent shipment and published-rate fields."""
import math
import re

from .cities import normalize_city, city_pattern
from .tariff_model import price_signature

INTERVAL_HEADER = ['Компания', 'Откуда', 'Куда', 'Вес от, кг', 'Вес до, кг', 'Тариф', 'Единица', 'Минимум, руб']


def is_interval_header(row):
    from .online_tariffs import norm
    return [norm(x) for x in row[:8]] == [norm(x) for x in INTERVAL_HEADER]


def project_intervals(intervals):
    """Lower bound exclusive, upper inclusive; equal bounds mean a control point.

    No gap is interpolated. Every item retains its source interval and unit.
    A rate at a control weight does not claim the whole common bin is uniform.
    """
    from .v42_engine import COMMON_PROFILES
    ordered=sorted(intervals,key=lambda r:(r['lo'],r['hi']))
    for i,row in enumerate(ordered):
        if i and (ordered[i-1]['hi']>row['lo'] or ordered[i-1]['hi']==row['lo']==row['hi']):
            raise ValueError('Весовые интервалы перекрываются. Укажите один тариф для каждого веса.')
    values={}
    for p in COMMON_PROFILES:
        if p.get('is_minimum_profile'):continue
        w=p['weight_kg']
        matches=[r for r in ordered if (r['lo']<w<=r['hi']) or r['lo']==w==r['hi']]
        if not matches:continue
        r=matches[0];rate=r['value'] if r['unit']=='rate' else None
        price=max(r.get('minimum') or 0,w*rate) if rate is not None else r['value']
        values[p['id']]={'kind':'exact','price':round(price,4),'tariff_kind':r['unit'],
            'weight_from':r['lo'],'weight_to':None if math.isinf(r['hi']) else r['hi'],
            'source_row':r.get('source_row'),'source_page':r.get('source_page'),
            **({'rate_per_kg':rate,'minimum':r.get('minimum')} if rate is not None else {})}
    if not values:raise ValueError('В интервалах нет контрольных весов приложения')
    minima=[r for r in ordered if r.get('minimum') is not None]
    if minima:
        r=min(minima,key=lambda r:r['minimum'])
        values['min']={'kind':'exact','price':r['minimum'],'tariff_kind':'minimum',
            'source_row':r.get('source_row'),'source_page':r.get('source_page')}
    return values


def interval_row(cells, row_no=None, page_no=None):
    from .online_tariffs import norm, number
    lo=0 if cells[0] in ('',None,0,'0','0.0','0,0') else number(cells[0])
    hi=math.inf if norm(cells[1]) in {'∞','без ограничения','без ограничений'} else number(cells[1])
    if lo<0 or hi<lo or not math.isfinite(lo):raise ValueError('Неверные границы веса')
    unit=re.sub(r'\s+','',norm(cells[3])).replace('руб.','₽').replace('руб','₽')
    if unit in {'₽','₽/отправка','₽/отправление','₽/отправку'}:unit='fixed'
    elif unit=='₽/кг':unit='rate'
    else:raise ValueError('Единица должна быть «руб» или «руб/кг»')
    val=number(cells[2]);minimum=number(cells[4]) if len(cells)>4 and cells[4] not in ('',None) else None
    if val<=0 or (minimum is not None and minimum<=0):raise ValueError('Тариф и минимум должны быть положительными')
    if unit=='fixed' and minimum is not None:raise ValueError('Минимум применяется к ставке руб/кг; фиксированная сумма уже является ценой отправки')
    return dict(lo=lo,hi=hi,value=val,unit=unit,minimum=minimum,source_row=row_no,source_page=page_no)


def parse_interval_workbook(raw,company,origin,destination):
    from .tariff_documents import workbook_rows
    intervals=[]
    for title,rows in workbook_rows(raw):
        header=next((i for i,r in enumerate(rows[:20]) if is_interval_header(r)),None)
        if header is None:continue
        for i,r in enumerate(rows[header+1:],header+2):
            if len(r)<7 or str(r[0]).strip()!=company or normalize_city(r[1])!=origin or normalize_city(r[2])!=destination:continue
            if r[5] in ('',None):continue
            try:intervals.append(interval_row(list(r[3:8]),row_no=f'{title}!{i}'))
            except ValueError as exc:raise ValueError(f'{title}, строка {i}: {exc}') from exc
    return project_intervals(intervals),{'parser':'Интервальные тарифы · явные единицы','route_verified':True,
        'calculation_basis':'Фиксированные суммы повторяются внутри исходного интервала. Ставка руб/кг сохраняется отдельно; сумма = max(вес × ставка, указанный минимум). Контрольный вес — верхняя граница строки сравнения. Без объёмных тарифов и допуслуг.'}


def parse_werner(raw,origin,destination):
    """Verified native 3 fixed + 7 weight + 6 volume columns; fail on redesign."""
    from .tariff_documents import workbook_rows, dated
    from .online_tariffs import norm, number, values_from_tiers
    matches=[]
    for title,rows in workbook_rows(raw):
        header=' '.join(str(v or '') for r in rows[:25] for v in r)
        if not re.search(r'перевозка сборных грузов из\s+(?:города\s+)?'+city_pattern(origin)+r'(?=\s|$)',header,re.I):continue
        if not re.search(r'wernerus\.ru|вернер',header,re.I):continue
        fixed_group=next((i for i,r in enumerate(rows[:25]) if 'фиксированные тарифы' in norm(' '.join(str(x or '') for x in r))),None)
        if fixed_group is None:continue
        # Merged cells may add blank columns; locate actual weight labels.
        for hidx in range(fixed_group,min(fixed_group+5,len(rows))):
            h=rows[hidx];labels=[re.sub(r'\s+','',norm(x)).replace('–','-').replace('—','-') for x in h]
            fixed_cols=[]
            for weight in (5,20,40):
                cols=[j for j,x in enumerate(labels) if x.startswith(f'до{weight}кг')]
                if len(cols)!=1:break
                fixed_cols.append(cols[0])
            expected=['от3000','2000-2999','1000-1999','500-999','200-499','100-199','41-99']
            rate_cols=[]
            for label in expected:
                cols=[j for j,x in enumerate(labels) if x in {label,label+'кг'}]
                if len(cols)!=1:break
                rate_cols.append(cols[0])
            compact=re.sub(r'\s+','',norm(header))
            if len(fixed_cols)!=3 or len(rate_cols)!=7 or 'руб' not in compact or not re.search(r'руб\.?/кг',compact):continue
            if fixed_cols!=sorted(fixed_cols) or rate_cols!=sorted(rate_cols) or max(fixed_cols)>=min(rate_cols):continue
            for rno,r in enumerate(rows[hidx+1:],hidx+2):
                section=norm(' '.join(str(x or '') for x in r))
                if re.match(r'(дополнительные условия|забор и доставка|погрузо.?разгрузочные|дополнительные услуги)',section):break
                dest_cells=[j for j,x in enumerate(r[:min(fixed_cols)]) if normalize_city(x)==destination]
                if len(dest_cells)!=1:continue
                if len(r)<=max(rate_cols):raise ValueError('Werner: неполная строка тарифов')
                fixed=[number(r[j]) for j in fixed_cols];rates=[number(r[j]) for j in rate_cols]
                kg=[(3000,math.inf),(2000,2999),(1000,1999),(500,999),(200,499),(100,199),(41,99)]
                vals=values_from_tiers([(lo,hi,v) for (lo,hi),v in zip(kg,rates)],fixed=[(0,5,fixed[0]),(6,20,fixed[1]),(21,40,fixed[2])])
                for value in vals.values():value['source_row']=f'{title}!{rno}'
                matches.append((vals,{'parser':'Werner · исходный XLS/XLSX','source_row':f'{title}!{rno}',
                    'document_date':dated(header),'route_verified':True,
                    'calculation_basis':'Werner: фиксированные тарифы до 5/20/40 кг при ограничениях объёма из прайса; далее опубликованные руб/кг. Цены без НДС, если так указано в оригинале. Допуслуги не включены.'}))
            break
    if len(matches)!=1:raise ValueError('Werner: не найдена единственная строка маршрута в исходном макете 3 фиксированных + 7 весовых колонок. Используйте шаблон диапазонов при другом формате.')
    return matches[0]
