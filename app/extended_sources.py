from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "runtime" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "text/html,application/json,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
})


def norm_city(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    key = text.lower().replace("ё", "е").replace("–", "-").replace("—", "-")
    aliases = {
        "москва": "Москва",
        "санкт петербург": "Санкт-Петербург",
        "санкт-петербург": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "питер": "Санкт-Петербург",
        "нижний новгород": "Нижний Новгород",
        "ростов на дону": "Ростов-на-Дону",
        "ростов-на-дону": "Ростов-на-Дону",
    }
    return aliases.get(key, text)


def _money(value: str | float | int | None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else None
    s = str(value or "").replace("\xa0", " ").replace("₽", "").replace("руб.", "").replace("руб", "")
    m = re.search(r"\d[\d\s]*(?:[,.]\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _numbers(text: str) -> list[float]:
    out = []
    for token in re.findall(r"\d+(?:[\s\u00a0]\d{3})*(?:[,.]\d+)?", str(text or "")):
        try:
            out.append(float(token.replace("\u00a0", "").replace(" ", "").replace(",", ".")))
        except Exception:
            pass
    return out


def _fetch_text(url: str, cache_name: str, ttl_hours: int = 8) -> tuple[str, str]:
    path = CACHE_DIR / cache_name
    if path.exists() and time.time() - path.stat().st_mtime < ttl_hours * 3600:
        return path.read_text(encoding="utf-8", errors="ignore"), "cached"
    r = SESSION.get(url, timeout=(6, 22))
    r.raise_for_status()
    path.write_text(r.text, encoding="utf-8")
    return r.text, "live"


def _result(price: float, source_type: str, source_url: str, formula: str, message: str,
            freshness: str = "live", days: tuple[int | None, int | None] = (None, None),
            price_is_minimum: bool = False) -> dict[str, Any]:
    return {
        "status": "ok",
        "price": round(float(price)),
        "currency": "RUB",
        "delivery_days_min": days[0],
        "delivery_days_max": days[1],
        "source_type": source_type,
        "source_url": source_url,
        "freshness": freshness,
        "formula": formula,
        "message": message,
        "price_is_minimum": bool(price_is_minimum),
    }


def _select_index(value: float, thresholds: list[float]) -> int:
    for i, threshold in enumerate(thresholds):
        if value <= threshold + 1e-9:
            return i
    return len(thresholds) - 1


# ---------------------------------------------------------------------------
# ПРОЛАЙН. Официальные маршрутные страницы содержат одновременно руб/кг и руб/м3.
# Встроенные снимки нужны только при временной недоступности сайта.
# ---------------------------------------------------------------------------
PROLINE_ROUTES = {}  # v50: embedded prices disabled; use current collectors or user imports.


def _parse_proline(html: str, route: dict[str, Any]) -> dict[str, Any] | None:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).replace("\xa0", " ")
    kg = re.search(r"Вес,\s*кг(.{0,500}?)Цена за кг,\s*руб(.{0,500}?)(?:По объему|По объёму)", text, re.I | re.S)
    m3 = re.search(r"Объем,\s*м\s*3(.{0,500}?)Цена за м\s*3,\s*руб(.{0,500}?)(?:Точные цены|Особенности|По весу|##)", text, re.I | re.S)
    if not kg or not m3:
        return None
    kg_limits = _numbers(kg.group(1))[:10]
    kg_rates = _numbers(kg.group(2))[:10]
    m3_limits = _numbers(m3.group(1))[:10]
    m3_rates = _numbers(m3.group(2))[:10]
    if len(kg_limits) >= 8 and len(kg_rates) >= 8 and len(m3_limits) >= 8 and len(m3_rates) >= 8:
        return {**route, "kg_limits": kg_limits, "kg_rates": kg_rates, "m3_limits": m3_limits, "m3_rates": m3_rates}
    return None


def proline_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    key = (norm_city(origin), norm_city(destination))
    row = PROLINE_ROUTES.get(key)
    if not row:
        return None
    wi = _select_index(float(weight), list(map(float, row["kg_limits"])))
    vi = _select_index(float(volume), list(map(float, row["m3_limits"])))
    kg_rate = float(row["kg_rates"][wi]); m3_rate = float(row["m3_rates"][vi])
    kg_cost = float(weight) * kg_rate
    m3_cost = float(volume) * m3_rate
    price = max(float(row.get("minimum") or 0), kg_cost, m3_cost)
    return _result(price, "Официальная маршрутная таблица Пролайн", row["url"],
                   f"max(минимум {row['minimum']} ₽; {weight:g} кг × {kg_rate:g}; {volume:g} м³ × {m3_rate:g})",
                   "Цена рассчитана по подтверждённому снимку опубликованных ставок за кг и м³; обновление исходника выполняется отдельной кнопкой.", "official_snapshot")


# ---------------------------------------------------------------------------
# ФАСТТРАНС. Статическая HTML-матрица. Снимок для Москва <-> Санкт-Петербург.
# ---------------------------------------------------------------------------
FASTTRANS_SNAPSHOTS = {}  # v50: embedded prices disabled; use current collectors or user imports.


def _parse_fasttrans_route(html: str, origin: str, destination: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    target = re.sub(r"\s+", " ", f"{origin} {destination}").strip().lower().replace("ё", "е")
    # Most current FastTrans pages render each route as a heading followed by a table.
    for node in soup.find_all(string=True):
        txt = re.sub(r"\s+", " ", str(node)).strip().lower().replace("ё", "е")
        if txt != target:
            continue
        parent = node.parent
        table = parent.find_next("table") if parent else None
        if not table:
            continue
        rows = []
        for tr in table.find_all("tr"):
            vals = []
            for cell in tr.find_all(["th", "td"]):
                nums = _numbers(cell.get_text(" ", strip=True))
                if nums:
                    vals.append(nums[0])
            if vals:
                rows.append(vals)
        if len(rows) >= 4:
            kg_limits, kg_rates, m3_limits, m3_rates = rows[:4]
            if len(kg_limits) >= 8 and len(kg_rates) >= 8 and len(m3_limits) >= 8 and len(m3_rates) >= 8:
                fixed=[]
                cursor=table
                for _ in range(6):
                    cursor=cursor.find_next() if cursor else None
                    if not cursor: break
                    t=cursor.get_text(" ",strip=True)
                    m=re.search(r"До\s*(\d+)\s*кг.*?До\s*([\d,.]+)\s*м.*?:\s*([\d\s]+)\s*₽",t,re.I)
                    if m: fixed.append((float(m.group(1)),float(m.group(2).replace(',','.')),float(m.group(3).replace(' ',''))))
                    if len(fixed)>=3: break
                return {"kg_limits":kg_limits,"kg_rates":kg_rates,"m3_limits":m3_limits,"m3_rates":m3_rates,"fixed":fixed}
    return None


def fasttrans_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    origin, destination = norm_city(origin), norm_city(destination)
    row = FASTTRANS_SNAPSHOTS.get((origin, destination))
    if not row:
        return None
    for max_w, max_v, fixed_price in row.get("fixed", []):
        if float(weight) <= float(max_w) and float(volume) <= float(max_v):
            return _result(fixed_price, "Официальная тарифная таблица ФастТранс", row["url"],
                           f"фиксированный тариф до {max_w:g} кг / {max_v:g} м³",
                           "Подтверждённый снимок опубликованного фиксированного межтерминального тарифа.", "official_snapshot")
    wi = _select_index(float(weight), list(map(float, row["kg_limits"])))
    vi = _select_index(float(volume), list(map(float, row["m3_limits"])))
    kg_rate = float(row["kg_rates"][wi]); m3_rate = float(row["m3_rates"][vi])
    kg_cost = float(weight) * kg_rate; m3_cost = float(volume) * m3_rate
    return _result(max(kg_cost, m3_cost), "Официальная тарифная таблица ФастТранс", row["url"],
                   f"max({weight:g} кг × {kg_rate:g} ₽/кг; {volume:g} м³ × {m3_rate:g} ₽/м³)",
                   "Подтверждённый снимок: стоимость считается по весу или объёму, берётся большее значение.", "official_snapshot")


# ---------------------------------------------------------------------------
# АТЭК. На текущей открытой странице Москва -> СПб опубликованы фиксированные
# ставки и руб/кг. Для сопоставимости применяем официальную плотность 250 кг/м3.
# ---------------------------------------------------------------------------
ATEK_MSK_SPB = {}  # v50: embedded prices disabled; use current collectors or user imports.


def atek_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    # Current ATEK prices are parsed by online_tariffs; no embedded fallback.
    return None


# ---------------------------------------------------------------------------
# БСК из пользовательской таблицы соответствует сайту 123789.ru — БСД.
# Сайт публикует полную матрицу: минимум + пары руб/кг / руб/м3 для диапазонов.
# ---------------------------------------------------------------------------
BSD_MSK_SPB = {}  # v50: embedded prices disabled; use current collectors or user imports.

# Официальная строка Санкт-Петербург → Москва с текущей таблицы 123789.ru,
# проверена 04.09.2026. Это отдельная сетка направления, она не зеркалится
# из московского прайса.
BSD_SPB_MSK = {}  # v50: embedded prices disabled; use current collectors or user imports.


def _parse_bsd(html: str, destination: str) -> dict[str, Any] | None:
    soup=BeautifulSoup(html,"lxml")
    target=norm_city(destination).lower().replace("ё","е")
    for tr in soup.find_all("tr"):
        cells=[re.sub(r"\s+"," ",c.get_text(" ",strip=True)).strip() for c in tr.find_all(["th","td"])]
        if not cells or norm_city(cells[0]).lower().replace("ё","е")!=target:
            continue
        if len(cells)<5: continue
        nums=[_numbers(c) for c in cells]
        try:
            minimum=float(nums[2][0])
            days=int(nums[3][0]) if nums[3] else None
        except Exception:
            continue
        kg_rates=[]; m3_rates=[]
        for c in cells[4:11]:
            pair=_numbers(c)
            if len(pair)>=2:
                kg_rates.append(pair[0]); m3_rates.append(pair[1])
        if len(kg_rates)>=5:
            return {"minimum":minimum,"days":days,"kg_rates":kg_rates,"m3_rates":m3_rates}
    return None


def bsk_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    origin, destination = norm_city(origin), norm_city(destination)
    row = {
        ("Москва", "Санкт-Петербург"): BSD_MSK_SPB,
        ("Санкт-Петербург", "Москва"): BSD_SPB_MSK,
    }.get((origin, destination))
    if not row:
        return None
    wi = _select_index(float(weight), row["kg_limits"]); vi = _select_index(float(volume), row["m3_limits"])
    kg_rate = float(row["kg_rates"][min(wi, len(row["kg_rates"])-1)])
    m3_rate = float(row["m3_rates"][min(vi, len(row["m3_rates"])-1)])
    kg_cost = float(weight) * kg_rate; m3_cost = float(volume) * m3_rate
    price = max(float(row["minimum"]), kg_cost, m3_cost)
    days = row.get("days")
    return _result(price, "Официальная тарифная таблица БСД (БСК в исходной таблице)", row["url"],
                   f"max(минимум {row['minimum']} ₽; {weight:g} кг × {kg_rate:g}; {volume:g} м³ × {m3_rate:g})",
                   "Подтверждённый снимок таблицы 123789.ru; в исходном Excel компания подписана «БСК».", "official_snapshot",
                   (days, days) if days else (None, None))


# ---------------------------------------------------------------------------
# ГЛАВТРАССА — официальный API без ключа.
# ---------------------------------------------------------------------------
def _flatten_city(data: Any) -> list[tuple[str,str]]:
    out=[]
    if isinstance(data,dict):
        # Common forms: {id:name}, or nested objects.
        for k,v in data.items():
            if isinstance(v,str) and re.search(r"[А-Яа-яЁё]",v):
                out.append((str(k),norm_city(v)))
            else:
                out.extend(_flatten_city(v))
    elif isinstance(data,list):
        for item in data:
            if isinstance(item,dict):
                name=item.get("name") or item.get("city") or item.get("title")
                ident=item.get("id") or item.get("code") or item.get("value")
                if name is not None and ident is not None:
                    out.append((str(ident),norm_city(str(name))))
                out.extend(_flatten_city(item))
    return out


def _city_id(catalog:list[tuple[str,str]],city:str)->str|None:
    target=norm_city(city).lower().replace("ё","е")
    for ident,name in catalog:
        if name.lower().replace("ё","е")==target:
            return ident
    return None


def glavtrassa_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    base = "https://glavtrassa.ru/api/calc/"
    try:
        # Для двух основных городов ID подтверждены опубликованным примером API.
        # Это позволяет не ждать отдельный справочник городов при каждом запуске.
        known_ids = {"Москва": "35", "Санкт-Петербург": "36"}
        origin_n = norm_city(origin)
        destination_n = norm_city(destination)
        dep = known_ids.get(origin_n)
        arr = known_ids.get(destination_n)
        freshness = "live"

        if not dep or not arr:
            city_cache = CACHE_DIR / "glavtrassa_cities.json"
            data = None
            if city_cache.exists() and time.time() - city_cache.stat().st_mtime < 24 * 3600:
                data = json.loads(city_cache.read_text(encoding="utf-8"))
                freshness = "cached"
            else:
                r = SESSION.get(base, params={"method": "api_city", "responseFormat": "json"}, timeout=(3, 6))
                r.raise_for_status()
                data = r.json()
                city_cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            catalog = _flatten_city(data)
            dep = dep or _city_id(catalog, origin)
            arr = arr or _city_id(catalog, destination)
        if not dep or not arr:
            return None

        side = max(0.1, float(volume)) ** (1 / 3) if float(volume) > 0 else 0.1
        params = {
            "method": "api_calc", "responseFormat": "json", "depPoint": dep, "arrPoint": arr,
            "cargoMest[1]": 1, "cargoKg[1]": float(weight), "cargoL[1]": round(side, 4),
            "cargoW[1]": round(side, 4), "cargoH[1]": round(side, 4), "cargoCalculation[1]": 0,
        }
        r = SESSION.get(base, params=params, timeout=(3, 7))
        r.raise_for_status()
        payload = r.json()
        price = None
        if isinstance(payload, dict):
            if str(payload.get("status") or "").upper() == "ERROR":
                return None
            price = _money(payload.get("price"))
            if price is None:
                stack = [payload]
                while stack and price is None:
                    obj = stack.pop()
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if str(k).lower() == "price":
                                price = _money(v)
                                if price is not None:
                                    break
                            if isinstance(v, (dict, list)):
                                stack.append(v)
                    elif isinstance(obj, list):
                        stack.extend(obj)
        if price is None:
            return None
        result = _result(
            price, "Официальный публичный API Главтрассы", base,
            f"1 место; {float(weight):g} кг; {float(volume):g} м³",
            "Расчёт получен напрямую из открытого метода Главтрассы без API-ключа.", freshness,
        )
        result["calculator_total_only"] = True
        return result
    except Exception:
        return None


MAGIC_ROUTE_TARIFFS = {}  # v50: embedded prices disabled; use current collectors or user imports.

# Оставляем имя для обратной совместимости с main.py и старым экспортом,
# но теперь это именно опубликованный минимум из полной тарифной таблицы,
# а не рекламная цена маршрута «от».
MAGIC_ROUTE_MINIMUMS = {
    route: {
        "price": row["minimum"],
        "url": row["url"],
        "note": "Минимальная стоимость из официальной тарифной таблицы Мейджик Транс.",
    }
    for route, row in MAGIC_ROUTE_TARIFFS.items()
}


def magic_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    row = MAGIC_ROUTE_TARIFFS.get((norm_city(origin), norm_city(destination)))
    if not row:
        return None
    w = max(0.0, float(weight))
    v = max(0.0, float(volume))

    # В исходной таблице есть отдельные фиксированные тарифы до 1 и до 5 кг.
    if w <= 1.0 + 1e-9 and v <= 0:
        result = _result(
            row["fixed_1kg"], "Официальная тарифная таблица Мейджик Транс", row["url"],
            "фиксированный тариф до 1 кг",
            "Фиксированная стоимость из официальной таблицы; цены указаны с НДС.", row["freshness"],
        )
        result["pricing_mode"] = "fixed_shipment"
        return result
    if w <= 5.0 + 1e-9 and v <= 0:
        result = _result(
            row["fixed_5kg"], "Официальная тарифная таблица Мейджик Транс", row["url"],
            "фиксированный тариф до 5 кг",
            "Фиксированная стоимость из официальной таблицы; цены указаны с НДС.", row["freshness"],
        )
        result["pricing_mode"] = "fixed_shipment"
        return result

    wi = _select_index(w, row["kg_limits"])
    vi = _select_index(v, row["m3_limits"])
    kg_rate = float(row["kg_rates"][wi])
    m3_rate = float(row["m3_rates"][vi])
    kg_cost = w * kg_rate
    m3_cost = v * m3_rate
    price = max(float(row["minimum"]), kg_cost, m3_cost)
    formula = f"max(минимум {row['minimum']:g} ₽; {w:g} кг × {kg_rate:g} ₽/кг"
    if v > 0:
        formula += f"; {v:g} м³ × {m3_rate:g} ₽/м³"
    formula += ")"
    result = _result(
        price, "Официальная тарифная таблица Мейджик Транс", row["url"], formula,
        "Ставка выбрана из явного весового диапазона официальной таблицы; интерполяции нет.", row["freshness"],
    )
    result["rate_per_kg"] = kg_rate
    result["minimum_charge"] = float(row["minimum"])
    return result


# ---------------------------------------------------------------------------
# ЭКСПЕДИЦИЯ ПЛЮС — официальный XLSX, действующий с 30.07.2026.
# В снимке 187 межтерминальных направлений из 9 городов отправления. Для каждого
# направления опубликованы минимум, ставки руб/кг и руб/м3. Никакой интерполяции:
# выбирается только явный диапазон исходной таблицы.
# ---------------------------------------------------------------------------
_EXPEDITION_SNAPSHOT_PATH = BASE_DIR / "data" / "expeditionplus_tariffs_snapshot.json"
_CTS_SNAPSHOT_PATH = BASE_DIR / "data" / "ctsgroup_tariffs_snapshot.json"
_FORTUNA_SNAPSHOT_PATH = BASE_DIR / "data" / "snapshots" / "fortuna_official_2026-07-27.json"
_NEWLINE_MSK_SNAPSHOT_PATH = BASE_DIR / "data" / "snapshots" / "newline_moscow_official_2026-08-22.json"
_SNAPSHOT_JSON_CACHE: dict[str, Any] = {}


def _snapshot_json(path: Path) -> dict[str, Any]:
    key = str(path)
    cached = _SNAPSHOT_JSON_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _SNAPSHOT_JSON_CACHE[key] = data
            return data
    except Exception:
        pass
    return {}


def _expedition_kg_index(weight: float) -> int:
    limits = [100, 300, 500, 800, 1500, 2000, 3000, 5000]
    for i, limit in enumerate(limits):
        if weight <= limit + 1e-9:
            return i
    return 8


def _expedition_m3_index(volume: float) -> int:
    limits = [0.4, 1.2, 2.0, 3.2, 6.0, 8.0, 12.0, 20.0]
    for i, limit in enumerate(limits):
        if volume <= limit + 1e-9:
            return i
    return 8


def expeditionplus_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    data = _snapshot_json(_EXPEDITION_SNAPSHOT_PATH)
    o, d = norm_city(origin), norm_city(destination)
    row = (data.get("routes") or {}).get(f"{o}|{d}")
    if not isinstance(row, dict):
        return None
    kg_rates = row.get("kg_rates") or []
    m3_rates = row.get("m3_rates") or []
    wi, vi = _expedition_kg_index(float(weight)), _expedition_m3_index(float(volume))
    try:
        kg_rate = float(kg_rates[wi]); m3_rate = float(m3_rates[vi]); minimum = float(row["minimum"])
    except Exception:
        return None
    kg_cost = float(weight) * kg_rate
    m3_cost = float(volume) * m3_rate
    price = max(minimum, kg_cost, m3_cost)
    url = str(data.get("source_url") or "https://nevatk.ru/tarif/")
    effective = str(data.get("effective_date") or "2026-07-30")
    return _result(
        price,
        "Официальный XLSX ЭкспедицияПлюс",
        url,
        f"max(минимум {minimum:g} ₽; {weight:g} кг × {kg_rate:g}; {volume:g} м³ × {m3_rate:g})",
        f"Межтерминальный тариф из официального прайс-листа ЭкспедицияПлюс, действует с {effective}.",
        f"official_xlsx_{effective}",
    )


# ---------------------------------------------------------------------------
# CTS GROUP — официальный XLSX Москвы, действующий с 01.04.2026.
# В файле одновременно опубликованы руб/кг и руб/м3, поэтому итог считается как
# максимум из минимума, весовой и объемной стоимости. В исходной таблице
# пользователя также есть более новый Yandex-viewer «01.07.26»; ссылка сохранена
# в каталоге источников, но автоматическая расшифровка ya-browser URL ненадежна.
# ---------------------------------------------------------------------------
def _cts_kg_index(weight: float) -> int:
    limits = [100, 200, 300, 500, 800, 1000, 1200, 1500, 2000, 3000]
    for i, limit in enumerate(limits):
        if weight <= limit + 1e-9:
            return i
    return 10


def _cts_m3_index(volume: float) -> int:
    limits = [0.4, 0.8, 1.2, 2.0, 3.2, 4.0, 4.8, 6.0, 8.0, 12.0]
    for i, limit in enumerate(limits):
        if volume <= limit + 1e-9:
            return i
    return 10


def ctsgroup_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    data = _snapshot_json(_CTS_SNAPSHOT_PATH)
    o, d = norm_city(origin), norm_city(destination)
    row = (data.get("routes") or {}).get(f"{o}|{d}")
    if not isinstance(row, dict):
        return None
    wi, vi = _cts_kg_index(float(weight)), _cts_m3_index(float(volume))
    kg_rates = row.get("kg_rates") or []
    m3_rates = row.get("m3_rates") or []
    if wi >= len(kg_rates) or vi >= len(m3_rates):
        return None
    if kg_rates[wi] is None or m3_rates[vi] is None:
        return None  # в официальном XLSX для этого диапазона стоит «договор»
    try:
        kg_rate = float(kg_rates[wi]); m3_rate = float(m3_rates[vi]); minimum = float(row["minimum"])
    except Exception:
        return None
    price = max(minimum, float(weight) * kg_rate, float(volume) * m3_rate)
    days_text = str(row.get("days") or "")
    day_nums = [int(x) for x in re.findall(r"\d+", days_text)]
    days = (min(day_nums), max(day_nums)) if day_nums else (None, None)
    effective = str(data.get("effective_date") or "2026-04-01")
    return _result(
        price,
        "Официальный XLSX CTS Group",
        str(data.get("source_page") or "https://cts-group.ru/prices"),
        f"max(минимум {minimum:g} ₽; {weight:g} кг × {kg_rate:g}; {volume:g} м³ × {m3_rate:g})",
        f"Тариф из открытого XLSX CTS Group от {effective}; для диапазонов «договор» цена не рассчитывается.",
        f"official_xlsx_{effective}",
        days,
    )


# ---------------------------------------------------------------------------
# FORTUNA EXPRESS — официальный XLSX с тарифами, опубликованный 27.07.2026.
# Локальный снимок содержит направления из листов Москва и Санкт-Петербург.
# В весовом режиме приложения используем фиксированную цену для грузов до 40 кг,
# затем опубликованную ставку руб/кг. Объемные ставки не подмешиваются скрыто.
# ---------------------------------------------------------------------------
def fortuna_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    data = _snapshot_json(_FORTUNA_SNAPSHOT_PATH)
    o, d = norm_city(origin), norm_city(destination)
    row = (data.get("routes") or {}).get(f"{o}|{d}")
    if not isinstance(row, dict):
        return None
    w = float(weight)
    fixed = row.get("fixed") or []
    minimum = _money(row.get("minimum"))
    if w <= 0 or minimum is None:
        return None
    fixed_limits = [0.99, 4.99, 19.9, 40.0]
    fixed_price = None
    for i, limit in enumerate(fixed_limits):
        if w <= limit + 1e-9:
            if i < len(fixed):
                fixed_price = _money(fixed[i])
            break
    if fixed_price is not None:
        price = fixed_price
        formula = f"фиксированный тариф до {w:g} кг: {fixed_price:g} ₽"
    else:
        rates = row.get("kg_rates") or []
        # XLSX: от 3000; 2000–2999; 1500–1999; 1000–1499; 500–999; 200–499; до 199.
        if w >= 3000: idx = 0
        elif w >= 2000: idx = 1
        elif w >= 1500: idx = 2
        elif w >= 1000: idx = 3
        elif w >= 500: idx = 4
        elif w >= 200: idx = 5
        else: idx = 6
        if idx >= len(rates) or rates[idx] is None:
            return None
        rate = float(rates[idx])
        price = max(float(minimum), w * rate)
        formula = f"max(минимум {minimum:g} ₽; {w:g} кг × {rate:g} ₽/кг)"
    nums = [int(x) for x in re.findall(r"\d+", str(row.get("days") or ""))]
    days = (min(nums), max(nums)) if nums else (None, None)
    effective = str(data.get("effective_date") or "2026-07-27")
    return _result(
        price,
        "Официальный XLSX Фортуна Экспресс",
        str(data.get("source_page") or "https://fte.ru/price/"),
        formula,
        f"Тариф из официального прайс-листа Фортуна Экспресс от {effective}; сравнение выполняется только по опубликованной весовой части.",
        f"official_xlsx_{effective}",
        days,
    )


# ---------------------------------------------------------------------------
# НОВАЯ ЛИНИЯ — официальная PDF-выгрузка из Москва-Север, полученная 22.08.2026.
# В документе для каждого направления опубликованы минимум и руб/кг до 5000 кг.
# Диапазоны выше последней опубликованной ставки не экстраполируются.
# ---------------------------------------------------------------------------
def newline_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    data = _snapshot_json(_NEWLINE_MSK_SNAPSHOT_PATH)
    o, d = norm_city(origin), norm_city(destination)
    row = (data.get("routes") or {}).get(f"{o}|{d}")
    if not isinstance(row, dict):
        return None
    w = float(weight)
    limits = [float(x) for x in (row.get("kg_limits") or [])]
    rates = row.get("kg_rates") or []
    minimum = _money(row.get("minimum"))
    if w <= 0 or minimum is None or not limits:
        return None
    idx = next((i for i, limit in enumerate(limits) if w <= limit + 1e-9), None)
    if idx is None or idx >= len(rates) or rates[idx] is None:
        return None
    rate = float(rates[idx])
    price = max(float(minimum), w * rate)
    day_nums = [int(x) for x in re.findall(r"\d+", str(row.get("days") or ""))]
    days = (min(day_nums), max(day_nums)) if day_nums else (None, None)
    captured = str(data.get("captured_at") or "2026-08-22")
    return _result(
        price,
        "Официальный PDF Новая Линия",
        str(data.get("source_url") or "https://tknl.ru/price/"),
        f"max(минимум {minimum:g} ₽; {w:g} кг × {rate:g} ₽/кг)",
        f"Ставка распознана из официальной PDF-выгрузки Москва-Север, сохранённой {captured}; диапазоны выше опубликованного предела не интерполируются.",
        f"official_pdf_{captured}",
        days,
    )
