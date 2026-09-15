from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = BASE_DIR / "runtime"
ROUTE_CONFIG = {
    ("Санкт-Петербург", "Москва"): {
        "slug": "spb_moscow",
        "pack": DATA_DIR / "v41" / "spb_moscow.json",
        "live": RUNTIME_DIR / "v41_spb_moscow_live.json",
    },
    ("Москва", "Санкт-Петербург"): {
        "slug": "moscow_spb",
        "pack": DATA_DIR / "v41" / "moscow_spb.json",
        "live": RUNTIME_DIR / "v41_moscow_spb_live.json",
    },
}
STATE_LOCK = threading.RLock()

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
    COMMON_PROFILES.append({"id":f"w{w}","label":f"до {w} кг","weight_kg":float(w),"volume_m3":0.0,"description":f"до {w} кг","range_weight":f"до {w} кг","range_volume":"","unit":"₽","comparison_mode":"shipment_total","tariff_type":"Стоимость отправки"})
PROFILE_BY_ID = {p["id"]: p for p in COMMON_PROFILES}

ALIASES = {
    "москва":"Москва", "moscow":"Москва", "санкт-петербург":"Санкт-Петербург",
    "санкт петербург":"Санкт-Петербург", "спб":"Санкт-Петербург", "saint petersburg":"Санкт-Петербург",
}
def normalize_city(value: str) -> str:
    s = " ".join(str(value or "").strip().replace("ё","е").split())
    return ALIASES.get(s.lower(), s)

def route_pair(origin: str, destination: str) -> tuple[str, str]:
    return normalize_city(origin), normalize_city(destination)

def is_supported_route(origin: str, destination: str) -> bool:
    return route_pair(origin, destination) in ROUTE_CONFIG

def paired_destination(origin: str) -> str | None:
    origin = normalize_city(origin)
    if origin == "Санкт-Петербург": return "Москва"
    if origin == "Москва": return "Санкт-Петербург"
    return None

def _route_cfg(origin: str, destination: str) -> dict[str, Any]:
    key=route_pair(origin,destination)
    if key not in ROUTE_CONFIG:
        raise ValueError("Поддерживаются только маршруты Санкт-Петербург → Москва и Москва → Санкт-Петербург")
    return ROUTE_CONFIG[key]

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)

def _pack(origin: str, destination: str) -> dict[str, Any]:
    cfg=_route_cfg(origin,destination)
    base=_read_json(cfg["pack"], {"profiles":{},"companies":{},"route":{"origin":origin,"destination":destination}})
    live=_read_json(cfg["live"], {"profiles":{},"companies":{}})
    for company, meta in (live.get("companies") or {}).items():
        merged={**base.get("companies",{}).get(company,{}), **meta}
        merged["data_origin"]="online"
        base.setdefault("companies",{})[company]=merged
    for pid,row in (live.get("profiles") or {}).items():
        out=base.setdefault("profiles",{}).setdefault(pid,{})
        for company,item in (row or {}).items():
            if isinstance(item,dict) and item.get("kind") in {"exact","lower_bound"} and item.get("price") is not None:
                out[company]=item
    return base

def live_path_for(origin: str, destination: str) -> Path:
    return _route_cfg(origin,destination)["live"]

def save_live_update(company: str, origin: str, destination: str, profile_values: dict[str, dict[str, Any]], meta: dict[str, Any]) -> None:
    with STATE_LOCK:
        cfg=_route_cfg(origin,destination); path=cfg["live"]
        o,d=route_pair(origin,destination)
        live=_read_json(path,{"schema":2,"route":{"origin":o,"destination":d},"companies":{},"profiles":{}})
        live.setdefault("companies",{})[company]={**meta,"data_origin":"online"}
        for pid,item in profile_values.items():
            live.setdefault("profiles",{}).setdefault(pid,{})[company]=item
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live,ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.replace(path)

def profile_for_weight(weight: float, is_minimum: bool=False) -> dict[str, Any] | None:
    if is_minimum: return PROFILE_BY_ID["min"]
    w=float(weight)
    return min((p for p in COMMON_PROFILES if not p.get("is_minimum_profile")), key=lambda p: abs(float(p["weight_kg"])-w), default=None)

def _route_quote(company: str, origin: str, destination: str, profile_id: str) -> dict[str, Any]:
    o,d=route_pair(origin,destination); pack=_pack(o,d); p=PROFILE_BY_ID[profile_id]
    meta=(pack.get("companies") or {}).get(company,{})
    item=((pack.get("profiles") or {}).get(profile_id) or {}).get(company,{"kind":"missing","price":None})
    kind=item.get("kind") or "missing"; price=item.get("price")
    data_origin=meta.get("data_origin") or "validated_snapshot"
    base={
        "company":company,"company_label":COMPANY_LABELS.get(company,company),"comparison_unit":"₽",
        "source_type":meta.get("source_type") or "Официальный источник",
        "source_url":meta.get("source_url"),"captured_at":meta.get("captured_at"),
        "pricing_engine":"v41_two_route_engine","profile_id":profile_id,
        "destination_variant":meta.get("destination_variant"),"data_origin":data_origin,
        "online":data_origin=="online",
    }
    if kind=="exact" and price is not None:
        weight=float(p.get("weight_kg") or 0)
        effective_rate=(float(price)/weight) if weight>0 and not p.get("is_minimum_profile") else None
        where=(f" ({meta.get('destination_variant')})" if meta.get("destination_variant") else "")
        freshness="online_live" if data_origin=="online" else "validated_route_snapshot"
        msg=(f"Получено онлайн и проверено для {o} → {d}{where}." if data_origin=="online" else f"Последнее подтверждённое официальное значение для {o} → {d}{where}.")
        return {**base,"status":"ok","price":float(price),"comparison_value":float(price),"price_is_minimum":False,
                "freshness":freshness,"display_text":f"{float(price):.0f} ₽",
                "effective_rate_per_kg":round(effective_rate,4) if effective_rate is not None else None,
                "published_rate_per_kg":item.get("rate_per_kg"),"minimum_charge":item.get("minimum") or item.get("minimum_charge"),
                "message":msg+" Неудачное онлайн-обновление это значение не удаляет."}
    if kind=="lower_bound" and price is not None:
        return {**base,"status":"ok","price":float(price),"comparison_value":None,"price_is_minimum":True,
                "freshness":"official_route_lower_bound","display_text":"нет точной строки",
                "message":f"Официальный источник публикует для {o} → {d} цену от {float(price):.0f} ₽. Это не точный тариф выбранного веса."}
    detail=meta.get("note") or f"Для {o} → {d} пока нет подтверждённой строки этого перевозчика. Остальные данные сохраняются."
    return {**base,"status":"document_unavailable","price":None,"comparison_value":None,"price_is_minimum":False,
            "freshness":"missing","display_text":"прайс не загружен",
            "message":detail}

def quote(company: str, origin: str, destination: str, profile_id: str) -> dict[str, Any]:
    if company not in COMPANIES: raise KeyError(company)
    if profile_id not in PROFILE_BY_ID: raise KeyError(profile_id)
    if not is_supported_route(origin,destination):
        raise ValueError("Поддерживаются только два маршрута: Санкт-Петербург → Москва и Москва → Санкт-Петербург")
    return _route_quote(company,origin,destination,profile_id)

def matrix(origin: str, destination: str, companies: list[str]) -> list[dict[str, Any]]:
    return [{"profile":deepcopy(p),"items":[quote(c,origin,destination,p["id"]) for c in companies]} for p in COMMON_PROFILES]

def coverage(origin: str, destination: str, company: str) -> dict[str, Any]:
    items=[quote(company,origin,destination,p["id"]) for p in COMMON_PROFILES if not p.get("is_minimum_profile")]
    exact=sum(1 for x in items if x.get("comparison_value") is not None)
    lower=sum(1 for x in items if x.get("price_is_minimum") and x.get("price") is not None)
    first=items[0] if items else {}
    return {"id":company,"label":COMPANY_LABELS.get(company,company),"coverage_label":f"точных {exact}/{len(items)}" if exact else ("есть цена «от»" if lower else "нет валидированной строки"),
            "method":"v41: два фиксированных маршрута; online-результат валидируется, сохранённый официальный snapshot не стирается при сетевом сбое",
            "source_url":first.get("source_url"),"evidence_rows":exact,"embedded_rows":exact,"collected_rows":exact,"last_collected_at":first.get("captured_at"),"collection_errors":[]}
