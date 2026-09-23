"""User-entered cells, independent of online evidence and uploaded originals."""
from decimal import Decimal,InvalidOperation
from copy import deepcopy
from . import v42_engine as e


def path(origin,destination):return e.RUNTIME_DIR/'manual'/('route_'+e._route_cfg(origin,destination)['slug']+'.json')


def pack(origin,destination):return e._read_json(path(origin,destination),{'profiles':{}})


def validate(company,origin,destination,profile):
    o,d=e.route_pair(origin,destination)
    if company not in e.COMPANIES or not e.is_supported_route(o,d) or profile not in e.PROFILE_BY_ID:
        raise ValueError('Выберите компанию, направление и вес из списка')
    return o,d,e.PROFILE_BY_ID[profile]


def put(company,origin,destination,profile,value,unit):
    o,d,p=validate(company,origin,destination,profile)
    rate=p['weight_kg']>=100 and not p.get('is_minimum_profile')
    if unit not in ({'rub','rub_per_kg'} if rate else {'rub'}):raise ValueError('Для этого диапазона цена указывается в рублях за отправку')
    if isinstance(value,bool) or not isinstance(value,(str,int,float)):raise ValueError('Введите положительную цену числом')
    try:
        number=Decimal(str(value).strip().replace('\u00a0','').replace(' ','').replace(',','.'))
        if not number.is_finite() or number<=0 or number>Decimal('1000000000'):raise InvalidOperation
        number=number.quantize(Decimal('.0001') if unit=='rub_per_kg' else Decimal('.01'))
        if number<=0:raise InvalidOperation
    except (InvalidOperation,ValueError):raise ValueError('Введите положительную цену без формул, от 0,01 ₽ или 0,0001 ₽/кг')
    price=number*Decimal(str(p['weight_kg'])) if unit=='rub_per_kg' else number
    row={'kind':'exact','price':float(price),'rate_per_kg':float(number) if unit=='rub_per_kg' else None,
         'manual_value':float(number),'manual_unit':unit,'captured_at':e._now(),'manual':True,
         'source_type':'Введено вручную','data_origin':'manual','source_url':None,
         'calculation_basis':'Цена введена пользователем для конкретной компании, маршрута и диапазона; с сайтом перевозчика не сверялась.'}
    with e.STATE_LOCK:
        current=pack(o,d);current['route']={'origin':o,'destination':d}
        current.setdefault('profiles',{}).setdefault(profile,{})[company]=row
        e._robust_json_write(path(o,d),current)
        from .document_imports import bump_revision
        bump_revision()
    return {'ok':True,'company':company,'origin':o,'destination':d,'profile':profile,'value':float(number),'unit':unit,'saved_at':row['captured_at']}


def remove(company,origin,destination,profile):
    o,d,_=validate(company,origin,destination,profile)
    count=clear_covered(company,o,d,[profile])
    return {'ok':True,'removed':count}


def clear_covered(company,origin,destination,profiles):
    with e.STATE_LOCK:
        current=pack(origin,destination);count=0
        for pid in profiles:
            if current.get('profiles',{}).get(pid,{}).pop(company,None) is not None:count+=1
        if count:
            e._robust_json_write(path(origin,destination),current)
            from .document_imports import bump_revision
            bump_revision()
        return count


def quote(row,company,profile):
    p=e.PROFILE_BY_ID[profile];price=row['price'];rate=row.get('rate_per_kg')
    return {**deepcopy(row),'company':company,'company_label':e.COMPANY_LABELS[company],'profile_id':profile,
            'status':'ok','online':False,'uploaded':False,'manual':True,'document_selected':False,
            'comparison_value':price,'comparison_unit':'₽','price_is_minimum':False,
            'tariff_unit':'₽/кг' if p['weight_kg']>=100 and not p.get('is_minimum_profile') else '₽',
            'tariff_value':rate if p['weight_kg']>=100 and not p.get('is_minimum_profile') else price,
            'published_rate_per_kg':rate,'effective_rate_per_kg':price/p['weight_kg'] if not p.get('is_minimum_profile') else None,
            'freshness':'manual','display_text':f'{price:g} ₽','message':'Введено вручную пользователем. Хранится до изменения, удаления или применения нового прайса для этой ячейки.'}
