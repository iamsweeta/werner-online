from __future__ import annotations

import json
import os
import time
import math
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from .tariff_model import tariff_value, tariff_unit
from .network import explain_error
from .cities import normalize_city, route_supported, route_slug

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = BASE_DIR / "runtime"
ROUTE_CONFIG = {
    ("Санкт-Петербург", "Москва"): {
        "slug": "spb_moscow",
        "pack": DATA_DIR / "v42" / "spb_moscow.json",
        "live": RUNTIME_DIR / "v42_spb_moscow_live.json",
    },
    ("Москва", "Санкт-Петербург"): {
        "slug": "moscow_spb",
        "pack": DATA_DIR / "v42" / "moscow_spb.json",
        "live": RUNTIME_DIR / "v42_moscow_spb_live.json",
    },
}
STATE_LOCK = threading.RLock()
LIVE_TTL_SECONDS = 1800

def age_seconds(stamp: str | None) -> float:
    try:
        dt = datetime.fromisoformat(str(stamp))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return max(0.0, (datetime.now().astimezone() - dt).total_seconds())
    except (TypeError, ValueError):
        return float('inf')

@lru_cache(maxsize=16)
def _cached_json(path: str, modified: int, size: int) -> Any:
    return json.loads(Path(path).read_text(encoding='utf-8'))

COMPANIES = [
    "Werner", "ДЛ", "ПЭК", "КИТ", "Возовоз", "Мейджик", "Байкал Сервис",
    "Главтрасса", "Пролайн", "Фортуна", "ФастТранс", "Рейл Континент", "АТЭК",
    "Новая Линия", "БСК", "ЭкспедицияПлюс", "CTSgroup",
]
COMPANY_LABELS = {
    "Werner": "Werner", "ДЛ": "ДЛ", "ПЭК": "ПЭК", "КИТ": "КИТ", "Возовоз": "Возовоз",
    "Мейджик": "Мейджик", "Байкал Сервис": "Байкал Сервис", "Главтрасса": "Главтрасса",
    "Пролайн": "Пролайн", "Фортуна": "Фортуна", "ФастТранс": "ФастТранс",
    "Рейл Континент": "РейлКонтинент", "АТЭК": "АТЭК", "Новая Линия": "НоваяЛиния",
    "БСК": "БСК", "ЭкспедицияПлюс": "ЭкспедицияПлюс", "CTSgroup": "CTSGroup",
}

COMMON_PROFILES = [
    {"id":"w001","label":"0–1 кг","weight_kg":1.0,"volume_m3":0.0,"description":"0–1 кг","range_weight":"0–1 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w003","label":"1–3 кг","weight_kg":3.0,"volume_m3":0.0,"description":"1–3 кг","range_weight":"1–3 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w005","label":"3–5 кг","weight_kg":5.0,"volume_m3":0.0,"description":"3–5 кг","range_weight":"3–5 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w010","label":"5–10 кг","weight_kg":10.0,"volume_m3":0.0,"description":"5–10 кг","range_weight":"5–10 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w015","label":"10–15 кг","weight_kg":15.0,"volume_m3":0.0,"description":"10–15 кг","range_weight":"10–15 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w020","label":"15–20 кг","weight_kg":20.0,"volume_m3":0.0,"description":"15–20 кг","range_weight":"15–20 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w035","label":"20–35 кг","weight_kg":35.0,"volume_m3":0.0,"description":"20–35 кг","range_weight":"20–35 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w040","label":"35–40 кг","weight_kg":40.0,"volume_m3":0.0,"description":"35–40 кг","range_weight":"35–40 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"w050","label":"40–50 кг","weight_kg":50.0,"volume_m3":0.0,"description":"40–50 кг","range_weight":"40–50 кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"},
    {"id":"min","label":"МИН","weight_kg":1.0,"volume_m3":0.0,"description":"МИН — минимальная стоимость отправки","range_weight":"МИН","range_volume":"","unit":"₽","is_minimum_profile":True,"comparison_mode":"minimum","tariff_type":"Минимальная стоимость"},
]
for w in (100,200,250,300,400,500,600,700,750,800,1000,1200,1500,2000,2500,3000,5000,10000,20000):
    COMMON_PROFILES.append({"id":f"w{w}","label":f"до {w} кг","weight_kg":float(w),"volume_m3":0.0,"description":f"до {w} кг","range_weight":f"до {w} кг","range_volume":"","unit":"₽/кг","comparison_mode":"published_rate","tariff_type":"Ставка из прайса"})
PROFILE_BY_ID = {p["id"]: p for p in COMMON_PROFILES}

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def route_pair(origin: str, destination: str) -> tuple[str, str]:
    return normalize_city(origin), normalize_city(destination)

def is_supported_route(origin: str, destination: str) -> bool:
    return route_supported(origin, destination)

def paired_destination(origin: str) -> str | None:
    origin = normalize_city(origin)
    if origin == "Санкт-Петербург": return "Москва"
    if origin == "Москва": return "Санкт-Петербург"
    return "Москва" if origin != "Москва" else "Санкт-Петербург"

def _route_cfg(origin: str, destination: str) -> dict[str, Any]:
    key=route_pair(origin,destination)
    if not route_supported(*key):
        raise ValueError("Выберите два разных города из списка")
    return ROUTE_CONFIG.get(key) or {"slug":route_slug(*key), "pack":None,
                                   "live":RUNTIME_DIR/"routes"/(route_slug(*key)+".json")}

def _read_json(path: Path | None, default: Any) -> Any:
    # A fresh install has no LIVE file yet. Missing files are a normal state and
    # must return immediately; retrying them makes /api/options appear frozen.
    if path is None or not path.exists():
        return deepcopy(default)
    for delay in (0.0, 0.02, 0.05, 0.10):
        if delay:
            time.sleep(delay)
        try:
            st = path.stat()
            return deepcopy(_cached_json(str(path), st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            return deepcopy(default)
        except (PermissionError, OSError, json.JSONDecodeError):
            continue
        except Exception:
            break
    return deepcopy(default)

def _robust_json_write(path: Path, payload: Any) -> None:
    """Atomic-ish JSON persistence resilient to Windows AV/indexer locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text=json.dumps(payload,ensure_ascii=False,indent=2)
    tmp=path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text,encoding='utf-8')
    last=None
    try:
        for delay in (0.0,0.03,0.06,0.12,0.22,0.35,0.50):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp,path)
                _cached_json.cache_clear()
                return
            except (PermissionError,OSError) as exc:
                last=exc
        raise last or OSError(f'Не удалось записать {path}')
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass

def _base_pack(origin: str, destination: str) -> dict[str, Any]:
    # Installation files are structure/catalogues, never a source of prices.
    # Keep this boundary even when upgrading an installation with old data/v42.
    return {"profiles":{},"companies":{},"route":{"origin":origin,"destination":destination}}

def _live_pack(origin: str, destination: str) -> dict[str, Any]:
    o,d=route_pair(origin,destination)
    return _read_json(_route_cfg(o,d)["live"], {"schema":3,"route":{"origin":o,"destination":d},"companies":{},"profiles":{}})

def _write_live(origin: str, destination: str, live: dict[str, Any]) -> None:
    _robust_json_write(_route_cfg(origin,destination)['live'], live)

def live_path_for(origin: str, destination: str) -> Path:
    return _route_cfg(origin,destination)["live"]

def begin_live_attempt(company: str, origin: str, destination: str, profile_id: str | None=None) -> str:
    if company not in COMPANIES: raise KeyError(company)
    attempt_id=f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    with STATE_LOCK:
        live=_live_pack(origin,destination)
        meta=live.setdefault("companies",{}).setdefault(company,{})
        meta.update({
            "current_attempt_id":attempt_id,
            "last_attempt_at":_now(),
            "attempt_status":"running",
            "last_error":None,
            "rows_current":0,
            "requested_profile":profile_id,
            "unavailable_profiles":{}, "unavailable_evidence":{},
            "missing_profile_errors":{}, "partial_errors":[],
        })
        _write_live(origin,destination,live)
    return attempt_id

def save_live_update(company: str, origin: str, destination: str, profile_values: dict[str, dict[str, Any]], meta: dict[str, Any], attempt_id: str | None=None) -> None:
    if company not in COMPANIES:
        raise KeyError(company)
    if meta.get('origin') and normalize_city(meta['origin']) != normalize_city(origin):
        raise ValueError('Прайс относится к другому городу отправления')
    if meta.get('destination') and normalize_city(meta['destination']) != normalize_city(destination):
        raise ValueError('Прайс относится к другому городу назначения')
    with STATE_LOCK:
        if not attempt_id and not live_company_state(origin,destination,company).get('current_attempt_id'):
            attempt_id=begin_live_attempt(company,origin,destination)
        live=_live_pack(origin,destination)
        company_meta=live.setdefault("companies",{}).setdefault(company,{})
        aid=attempt_id or company_meta.get("current_attempt_id")
        if company_meta.get('current_attempt_id') != aid:
            raise ValueError('Устаревшая попытка обновления: её результат отклонён')
        captured_at=str(meta.get("captured_at") or _now())
        for pid in meta.get('unavailable_profiles', {}):
            live.setdefault('profiles', {}).setdefault(pid, {}).pop(company, None)
        for pid,item in profile_values.items():
            if pid not in PROFILE_BY_ID or not isinstance(item,dict):
                continue
            kind=item.get("kind") or "missing"
            if kind not in {"exact","lower_bound"} or item.get("price") is None:
                continue
            price=item['price']
            if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0:
                raise ValueError(f'Некорректная цена {company}, {pid}')
            document_date=item.get('document_date') or meta.get('document_date')
            if document_date:
                from datetime import date
                if date.fromisoformat(document_date)>date.today():
                    raise ValueError('Источник содержит будущие тарифы; текущая цена не заменена')
            row={**item,
                 "attempt_id":aid,"captured_at":item.get("captured_at") or captured_at,"data_origin":"online",
                 "source_type":item.get("source_type") or meta.get("source_type"),"source_url":item.get("source_url") or meta.get("source_url"),
                 "destination_variant":item.get("destination_variant") or meta.get("destination_variant"),"transport":item.get("transport") or meta.get("transport") or meta.get("browser")}
            row.update({k: item.get(k,meta.get(k)) for k in ('source_file','sha256','calculation_basis','volume_m3','document_date','source_page','source_row','tax_basis')})
            live.setdefault("profiles",{}).setdefault(pid,{})[company]=row
        company_meta.update({k:v for k,v in meta.items() if v is not None})
        company_meta.update({"current_attempt_id":aid,"last_success_at":captured_at,"data_origin":"online"})
        _write_live(origin,destination,live)

def finish_live_attempt(company: str, origin: str, destination: str, attempt_id: str, *, rows: int, error: str | None=None) -> None:
    with STATE_LOCK:
        live=_live_pack(origin,destination)
        meta=live.setdefault("companies",{}).setdefault(company,{})
        # Ignore an obsolete worker finishing after a newer manual refresh started.
        if meta.get("current_attempt_id") != attempt_id:
            return
        status="success" if rows>0 and not error else ("partial" if rows>0 else "failed")
        if not rows and error and explain_error(error)["code"]=="route_unpublished":status="unavailable"
        meta.update({
            "attempt_status":status,
            "last_error":error,
            "rows_current":int(rows),
            "last_finished_at":_now(),
        })
        _write_live(origin,destination,live)

def live_company_state(origin: str, destination: str, company: str) -> dict[str, Any]:
    return dict((_live_pack(origin,destination).get("companies") or {}).get(company,{}) or {})

def profile_for_weight(weight: float, is_minimum: bool=False) -> dict[str, Any] | None:
    if is_minimum: return PROFILE_BY_ID["min"]
    w=float(weight)
    return min((p for p in COMMON_PROFILES if not p.get("is_minimum_profile")), key=lambda p: abs(float(p["weight_kg"])-w), default=None)

def _route_quote(company: str, origin: str, destination: str, profile_id: str, packs=None, *, derive_minimum=True) -> dict[str, Any]:
    o,d=route_pair(origin,destination); p=PROFILE_BY_ID[profile_id]
    from .document_imports import pack as imported_pack
    base_pack,live,imports=packs if packs is not None else (_base_pack(o,d),_live_pack(o,d),imported_pack(o,d))
    if profile_id == 'min' and derive_minimum:
        packs = (base_pack, live, imports)
        candidates = [_route_quote(company, o, d, p['id'], packs, derive_minimum=False)
                      for p in COMMON_PROFILES]
        candidates = [x for x in candidates if x.get('comparison_value') is not None
                      and not x.get('price_is_minimum') and isinstance(x.get('price'), (int, float))
                      and math.isfinite(x['price']) and x['price'] > 0]
        # A 100 kg quote alone cannot prove a carrier's smallest shipment price.
        # Require its explicit minimum or the first comparison weight (0–1 kg).
        for source in ('online', 'uploaded', 'last_good'):
            pool = [x for x in candidates if x.get('uploaded')] if source == 'uploaded' else (
                [x for x in candidates if x.get('online')] if source == 'online' else
                [x for x in candidates if not x.get('online') and not x.get('uploaded')])
            if not any(x['profile_id'] in {'min','w001'} for x in pool):
                continue
            chosen = min(pool, key=lambda x: x['price'])
            value = float(chosen['price'])
            return {**chosen, 'profile_id': 'min', 'price': value, 'comparison_value': value,
                    'price_is_minimum': False, 'effective_rate_per_kg': None,
                    'published_rate_per_kg': None, 'minimum_charge': value,
                    'tariff_value':value,'tariff_unit':'₽',
                    'minimum_source_profile': chosen['profile_id'],
                    'minimum_basis': 'published_or_first_weight',
                    'display_text': f'{value:g} ₽',
                    'message': 'Минимальная стоимость среди загруженных тарифов компании '
                               f'для {o} → {d}. ' + chosen.get('message', '')}
    base_meta=(base_pack.get("companies") or {}).get(company,{}) or {}
    company_live=(live.get("companies") or {}).get(company,{}) or {}
    base_item=((base_pack.get("profiles") or {}).get(profile_id) or {}).get(company,{"kind":"missing","price":None}) or {}
    live_item=((live.get("profiles") or {}).get(profile_id) or {}).get(company)
    uploaded_item=((imports.get('profiles') or {}).get(profile_id) or {}).get(company)
    live_current=bool(isinstance(live_item,dict) and live_item.get('kind')=='exact'
                      and live_item.get('price') is not None and live_item.get('attempt_id')
                      and live_item.get('attempt_id')==company_live.get('current_attempt_id')
                      and company_live.get('attempt_status') in {'success','partial'}
                      and age_seconds(live_item.get('captured_at'))<LIVE_TTL_SECONDS
                      and profile_id not in (company_live.get('unavailable_profiles') or {}))
    # Same policy in the route view and bulk export: current exact online
    # evidence first, confirmed documents for the remaining weights.
    uploaded=bool(uploaded_item) and not live_current
    unavailable = (company_live.get('unavailable_profiles') or {}).get(profile_id) if not uploaded else None
    use_live=isinstance(live_item,dict) and live_item.get("kind") in {"exact","lower_bound"} and live_item.get("price") is not None
    item=uploaded_item if uploaded else live_item if use_live else base_item
    if unavailable:
        item={'kind':'missing','price':None};use_live=False
    kind=item.get("kind") or "missing"; price=item.get("price")

    row_attempt=item.get("attempt_id") if use_live and not uploaded else None
    current_attempt=company_live.get("current_attempt_id")
    current_status=company_live.get("attempt_status")
    online=bool(not uploaded and use_live and row_attempt and row_attempt==current_attempt and current_status in {"success","partial"}
                and age_seconds(item.get('captured_at')) < LIVE_TTL_SECONDS)
    if uploaded:
        freshness='user_document'
        data_origin='uploaded'
    elif online:
        freshness="online_live"
        data_origin="online"
    elif use_live:
        freshness="last_good_live"
        data_origin="last_good"
    else:
        freshness="last_good_snapshot"
        data_origin="last_good"

    source_type=item.get("source_type") if use_live or uploaded else base_meta.get("source_type")
    source_url=item.get("source_url") if use_live or uploaded else base_meta.get("source_url")
    captured_at=item.get("captured_at") if use_live or uploaded else base_meta.get("captured_at")
    destination_variant=(item.get("destination_variant") if use_live or uploaded else base_meta.get("destination_variant"))
    base={
        "company":company,"company_label":COMPANY_LABELS.get(company,company),"comparison_unit":"₽",
        "source_type":source_type or "Официальный источник","source_url":source_url,"captured_at":captured_at,
        "pricing_engine":"v51_verified_sources","profile_id":profile_id,"destination_variant":destination_variant,"origin_terminal":item.get("origin_terminal") if uploaded else company_live.get("origin_terminal") if use_live else None,
        "data_origin":data_origin,"online":online,"freshness":freshness,"uploaded":uploaded,
        "live_attempt_id":row_attempt if use_live else None,"transport":item.get("transport") if use_live else None,
        "refresh_status":current_status or "not_run","refresh_attempted_at":company_live.get("last_attempt_at"),
        "refresh_error":company_live.get("last_error"),
        "error_info":explain_error(company_live["last_error"]) if company_live.get("last_error") else None,
        "source_file":item.get('source_file') if use_live or uploaded else None,
        "sha256":item.get('sha256') if use_live or uploaded else None,
        **{k:item.get(k) for k in ('original_filename','uploaded_at','document_date','source_page','source_pages','source_row','archive_member','tariff_kind','weight_from','weight_to','tax_basis')},
        "calculation_basis":item.get('calculation_basis') or 'Опубликованный тариф по весу; объём и дополнительные услуги не включены',
        "volume_m3":item.get('volume_m3'),
    }
    if unavailable:
        proof=company_live.get('unavailable_evidence') or {}
        return {**base,**proof,'status':'on_request','availability':'on_request','online':False,
                'freshness':'missing','price':None,'comparison_value':None,'price_is_minimum':False,
                'display_text':'по запросу','message':unavailable}
    if kind=="exact" and price is not None:
        weight=float(p.get("weight_kg") or 0)
        effective_rate=(float(price)/weight) if weight>0 and not p.get("is_minimum_profile") else None
        where=(f" ({destination_variant})" if destination_variant else "")
        if uploaded:
            msg=f"Файл пользователя для {o} → {d}. Импорт подтверждён пользователем; актуальность у перевозчика не проверена."
        elif online:
            msg=f"LIVE: получено при последней онлайн-загрузке и проверено для {o} → {d}{where}."
        elif use_live:
            msg=f"LAST GOOD: последнее успешно полученное онлайн-значение для {o} → {d}{where}; текущая попытка его не подтверждала."
        else:
            msg=f"LAST GOOD: архивная запись исходного проекта для {o} → {d}{where}; актуальность не проверена."
        if not uploaded and current_status=="failed" and company_live.get("last_error"):
            msg += " Последнее обновление завершилось ошибкой; сохранённое значение не выдано за LIVE."
        return {**base,"status":"ok","price":float(price),"comparison_value":float(price),"price_is_minimum":False,
                "tariff_value":item.get('rate_per_kg') if p['weight_kg']>=100 and not p.get('is_minimum_profile') else float(price),
                "tariff_unit":tariff_unit(p),
                "display_text":f"{float(price):.0f} ₽","effective_rate_per_kg":round(effective_rate,4) if effective_rate is not None else None,
                "published_rate_per_kg":item.get("rate_per_kg"),"minimum_charge":item.get("minimum") or item.get("minimum_charge"),"message":msg}
    if kind=="lower_bound" and price is not None:
        label="LIVE" if online else "LAST GOOD"
        return {**base,"status":"ok","price":float(price),"comparison_value":None,"price_is_minimum":True,
                "display_text":"нет точной строки","message":f"{label}: официальный источник публикует для {o} → {d} цену от {float(price):.0f} ₽. Это не точный тариф выбранного веса."}
    detail=(company_live.get('missing_profile_errors') or {}).get(profile_id) or (company_live.get("last_error") if current_status in {"failed","unavailable"} else None) or base_meta.get("note") or f"Для {o} → {d} пока нет подтверждённой строки этого перевозчика."
    return {**base,"status":"document_unavailable","price":None,"comparison_value":None,"price_is_minimum":False,
            "freshness":"missing","display_text":"прайс не загружен","message":detail}

def quote(company: str, origin: str, destination: str, profile_id: str) -> dict[str, Any]:
    if company not in COMPANIES: raise KeyError(company)
    if profile_id not in PROFILE_BY_ID: raise KeyError(profile_id)
    if not is_supported_route(origin,destination):
        raise ValueError("Выберите два разных города из списка")
    return _route_quote(company,origin,destination,profile_id)

def matrix(origin: str, destination: str, companies: list[str]) -> list[dict[str, Any]]:
    from .document_imports import pack
    packs=(_base_pack(origin,destination),_live_pack(origin,destination),pack(origin,destination))
    return [{"profile":deepcopy(p),"items":[_route_quote(c,origin,destination,p["id"],packs) for c in companies]} for p in COMMON_PROFILES]

def coverage(origin: str, destination: str, company: str) -> dict[str, Any]:
    from .document_imports import pack
    packs=(_base_pack(origin,destination),_live_pack(origin,destination),pack(origin,destination))
    items=[_route_quote(company,origin,destination,p["id"],packs) for p in COMMON_PROFILES if not p.get("is_minimum_profile")]
    exact=sum(1 for x in items if tariff_value(x, PROFILE_BY_ID[x["profile_id"]]) is not None)
    live_now=sum(1 for x in items if x.get("online") and (x.get("comparison_value") is not None or (x.get("price_is_minimum") and x.get("price") is not None)))
    live_exact=sum(1 for x in items if x.get("online") and tariff_value(x, PROFILE_BY_ID[x["profile_id"]]) is not None)
    live_lower=sum(1 for x in items if x.get("online") and x.get("price_is_minimum") and x.get("price") is not None)
    lower=sum(1 for x in items if x.get("price_is_minimum") and x.get("price") is not None)
    first=items[0] if items else {}; state=live_company_state(origin,destination,company)
    label=f"точных {exact}/{len(items)} · LIVE {live_now} (точных {live_exact}, «от» {live_lower})" if exact else (f"есть цена «от» · LIVE {live_lower}" if lower else "нет валидированной строки")
    imported=sum(1 for x in items if x.get('uploaded'))
    if imported:label+=f' · из файла {imported}'
    errs=[state.get("last_error")] if state.get("last_error") else []
    return {"id":company,"label":COMPANY_LABELS.get(company,company),"coverage_label":label,
            "method":"Свежесть проверяется отдельно для каждого диапазона и направления.","imported_rows":imported,
            "source_url":state.get("source_url") or first.get("source_url"),"evidence_rows":exact,"embedded_rows":exact,"collected_rows":live_now,
            "last_collected_at":state.get("last_success_at"),"last_attempt_at":state.get("last_attempt_at"),
            "refresh_status":state.get("attempt_status") or "not_run","partial_errors":state.get("partial_errors",[]),"collection_errors":errs,"error_info":explain_error(errs[0]) if errs else None}
