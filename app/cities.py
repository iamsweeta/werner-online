"""City names and route keys. This catalog contains names, never tariff values."""
from __future__ import annotations
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR=Path(__file__).resolve().parent.parent/'data'
ALIASES={'мск':'Москва','питер':'Санкт-Петербург','moscow':'Москва','москва':'Москва','спб':'Санкт-Петербург','с-петербург':'Санкт-Петербург',
         'санкт петербург':'Санкт-Петербург','saint petersburg':'Санкт-Петербург',
         'ростов на дону':'Ростов-на-Дону','н. новгород':'Нижний Новгород','н.новгород':'Нижний Новгород',
         'пет.камчатск':'Петропавловск-Камчатский','пет. камчатск':'Петропавловск-Камчатский'}

def _clean(value):
    return re.sub(r'^г\.\s*','',re.sub(r'\s+',' ',str(value or '').replace('ё','е').replace('Ё','Е').replace('–','-').replace('—','-')).strip(),flags=re.I)

@lru_cache(maxsize=1)
def city_names():
    values=json.loads((DATA_DIR/'customer_destinations.json').read_text(encoding='utf-8'))
    extra=DATA_DIR/'additional_cities.json'
    if extra.exists():values+=json.loads(extra.read_text(encoding='utf-8'))
    names={_clean(x) for x in values if isinstance(x,str) and _clean(x)}
    return ['Москва','Санкт-Петербург']+sorted(names-{'Москва','Санкт-Петербург'},key=str.casefold)

@lru_cache(maxsize=1)
def _canonical():return {x.casefold():x for x in city_names()}

@lru_cache(maxsize=1)
def _compact_canonical():
    groups={}
    for name in city_names():groups.setdefault(re.sub(r'\s+','',name.casefold()),[]).append(name)
    return {key:values[0] for key,values in groups.items() if len(values)==1}

def normalize_city(value):
    cleaned=_clean(value)
    canonical=ALIASES.get(cleaned.casefold(),_canonical().get(cleaned.casefold()))
    if canonical:return canonical
    # The customer's workbook joins words in several city names. Resolve only
    # an unambiguous exact spelling after removing spaces, never fuzzy matches.
    compact=re.sub(r'\s+','',cleaned.casefold())
    return _compact_canonical().get(compact,cleaned)

MAIN_ORIGINS = ['Москва', 'Санкт-Петербург']

@lru_cache(maxsize=1)
def main_cities():
    rows=json.loads((DATA_DIR/'customer_routes.json').read_text(encoding='utf-8'))['routes']
    names={normalize_city(c) for route in rows if normalize_city(route[0]) in MAIN_ORIGINS for c in route}
    return MAIN_ORIGINS + sorted(names-set(MAIN_ORIGINS),key=str.casefold)

def route_supported(origin,destination):
    o,d=normalize_city(origin),normalize_city(destination)
    return o!=d and o in city_names() and d in city_names()

def route_slug(origin,destination):
    raw=json.dumps([normalize_city(origin),normalize_city(destination)],ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def city_slug(value):
    letters='абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
    latin=['a','b','v','g','d','e','e','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','h','ts','ch','sh','sch','','y','','e','yu','ya']
    table=dict(zip(letters,latin))
    value=normalize_city(value).lower()
    return re.sub('-+','-',''.join(table.get(c,c if c.isalnum() else '-') for c in value)).strip('-')

def city_pattern(value):
    city=normalize_city(value).lower()
    if city=='москва':return r'москв(?:а|ы|у|е|ой)'
    if city=='санкт-петербург':return r'(?:санкт[\s–—-]*петербург(?:а|у|е|ом)?|с[.\s–—-]*петербург(?:а|у|е|ом)?|спб|питер(?:а|у|е)?)'
    def word(token):
        if token in {'на','дону'}:return re.escape(token)
        if token.endswith('ний'):return re.escape(token[:-3])+r'н(?:ий|его|ему|ем|им)'
        if token.endswith('ый'):return re.escape(token[:-2])+r'(?:ый|ого|ому|ом|ым)'
        if token.endswith('ий'):return re.escape(token[:-2])+r'(?:ий|ого|ому|ом|им)'
        if token.endswith('а'):return re.escape(token[:-1])+r'(?:а|ы|у|е|ой)'
        if token.endswith('я'):return re.escape(token[:-1])+r'(?:я|и|ю|е|ей)'
        if token.endswith('ь'):return re.escape(token[:-1])+r'(?:ь|и|ью)'
        if re.search('[бвгджзклмнпрстфхцчшщ]$',token):return re.escape(token)+r'(?:а|у|е|ом)?'
        return re.escape(token)
    return r'[\s–—-]+'.join(word(t) for t in re.split(r'[\s-]+',city))

class UnpublishedTariff(ValueError):
    def __init__(self,detail):super().__init__('Нет опубликованного тарифа для выбранного маршрута: '+detail)
