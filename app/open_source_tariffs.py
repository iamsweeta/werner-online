from __future__ import annotations

import os
import math
import re
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TARIFF_DATA_DIR") or BASE_DIR / "runtime").expanduser().resolve()
CACHE_DIR = RUNTIME_DIR / "cache"
DOWNLOAD_DIR = RUNTIME_DIR / "downloads"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
})

PONY_CURRENT_PDF = "https://www.ponyexpress.ru/upload/docs/2024/%D0%A2%D0%A1%20%D0%92%D0%BD%D1%83%D1%82%D1%80%D0%B8%D1%80%D0%BE%D1%81%D1%81%D0%B8%D0%B9%D1%81%D0%BA%D0%B8%D0%B5%20%D1%82%D0%B0%D1%80%D0%B8%D1%84%D1%8B%20%D0%BD%D0%B0%20%D1%81%D0%B0%D0%B9%D1%82%20%D1%81%2019_05_2025%20%D0%B4%D0%BB%D1%8F%20%D0%BA%D0%BB%D0%B8%D0%B5%D0%BD%D1%82%D0%BE%D0%B2%2C%20%D0%B7%D0%B0%D0%BA%D0%BB%D1%8E%D1%87%D0%B8%D0%B2%D1%88%D0%B8%D1%85%20%D0%B4%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80%20%D1%81%2019_05_2025_.pdf"

BAIKAL_REFERENCE = {}  # v50: embedded prices disabled; use current collectors or user imports.

DPD_DOCUMENT_REFERENCE = {}  # v50: embedded prices disabled; use current collectors or user imports.

# Строки Стандарт+ из действующего с 19.05.2025 официального тарифного справочника.
# Используются как локальный официальный снимок, если сайт PONY блокирует прямое скачивание PDF (403).
# Порядок: 100 кг, +1; 200 кг, +1; 500 кг, +1; 1000 кг, +1; 1500 кг, +1.
PONY_STANDARD_PLUS_REFERENCE = {}  # v50: embedded prices disabled; use current collectors or user imports.


def _norm_city(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    aliases = {
        "москва": "Москва", "санкт петербург": "Санкт-Петербург", "санкт-петербург": "Санкт-Петербург",
        "спб": "Санкт-Петербург", "нижний новгород": "Нижний Новгород", "ростов на дону": "Ростов-на-Дону",
        "ростов-на-дону": "Ростов-на-Дону",
    }
    return aliases.get(text.lower().replace("ё", "е"), text)


def _money(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else None
    s = str(value or "").replace("\xa0", " ")
    m = re.search(r"\d[\d\s]*(?:[,.]\d+)?", s)
    if not m:
        return None
    try:
        val = float(m.group(0).replace(" ", "").replace(",", "."))
        return val if val > 0 else None
    except Exception:
        return None


def _cached_text(url: str, name: str, ttl_hours: int = 12, referer: str | None = None) -> tuple[str, str]:
    path = CACHE_DIR / name
    if path.exists() and time.time() - path.stat().st_mtime < ttl_hours * 3600:
        return path.read_text(encoding="utf-8", errors="ignore"), "cached"
    headers = {"Referer": referer} if referer else None
    r = SESSION.get(url, timeout=(6, 24), headers=headers)
    r.raise_for_status()
    path.write_text(r.text, encoding="utf-8")
    return r.text, "live"


def _cached_binary(url: str, name: str, ttl_hours: int = 72, referer: str | None = None) -> tuple[Path, str]:
    path = CACHE_DIR / name
    if path.exists() and path.stat().st_size > 1000 and time.time() - path.stat().st_mtime < ttl_hours * 3600:
        return path, "cached"
    headers = {"Referer": referer} if referer else None
    r = SESSION.get(url, timeout=(8, 35), headers=headers)
    r.raise_for_status()
    body = r.content or b""
    if len(body) < 1000:
        raise RuntimeError("источник вернул слишком маленький файл")
    path.write_bytes(body)
    return path, "live"


def baikal_public_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    origin, destination = _norm_city(origin), _norm_city(destination)
    url = "https://www.baikalsr.ru/business/prices/"
    row = None
    freshness = "live"
    try:
        html, freshness = _cached_text(url, "baikal_business_prices.html", ttl_hours=8)
        soup = BeautifulSoup(html, "lxml")
        for tr in soup.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in tr.find_all(["th", "td"])]
            if len(cells) < 5:
                continue
            route = cells[0]
            route_key = re.sub(r"\s*[—–]\s*", "|", route.lower().replace("ё", "е"))
            target_key = f"{origin}|{destination}".lower().replace("ё", "е")
            if target_key not in route_key:
                continue
            nums = [_money(x) for x in cells[1:5]]
            if all(x is not None for x in nums):
                row = tuple(float(x) for x in nums)
                break
    except Exception:
        pass
    if row is None:
        row = BAIKAL_REFERENCE.get((origin, destination))
        freshness = "official_snapshot_2026-08-21" if row else freshness
    if not row:
        return None

    kg_u100, kg_o3000, m3_u1, m3_o25 = row
    # Страница публикует две крайние весовые/объёмные группы. Не интерполируем между ними.
    if weight <= 100 and volume <= 1:
        by_weight = float(weight) * kg_u100
        by_volume = float(volume) * m3_u1
        price = max(by_weight, by_volume)
        return {
            "price": round(price), "status": "ok", "source_type": "Открытая таблица тарифов Байкал Сервис",
            "source_url": url, "freshness": freshness, "rate_per_kg": float(kg_u100),
            "tariff_range_low_kg": 0.0, "tariff_range_high_kg": 100.0, "range_value_repeated": True,
            "formula": f"max({weight:g}×{kg_u100:g} ₽/кг; {volume:g}×{m3_u1:g} ₽/м³)",
            "message": "Тариф с официальной страницы популярных направлений; весовая ставка опубликована для диапазона до 100 кг. Для строк 0–50 кг эта ставка сама по себе не подменяет фиксированную стоимость отправки.",
            "price_is_minimum": False,
        }
    if weight >= 3000:
        # Customer comparison is weight-only. The public page prints an explicit
        # weight rate for the open-ended "от 3000 кг" band, so volume must not
        # be required merely to use that published weight column. When volume is
        # supplied by another caller we still preserve the carrier's max(weight,
        # volume) economics; for the customer matrix volume is exactly zero.
        by_weight = float(weight) * kg_o3000
        by_volume = float(volume) * m3_o25 if float(volume) > 0 else 0.0
        return {
            "price": round(max(by_weight, by_volume)), "status": "ok", "source_type": "Открытая таблица тарифов Байкал Сервис",
            "source_url": url, "freshness": freshness, "rate_per_kg": float(kg_o3000),
            "tariff_range_low_kg": 3000.0, "tariff_range_high_kg": None, "range_value_repeated": True,
            "formula": f"{weight:g}×{kg_o3000:g} ₽/кг" + (f"; контроль объёма {volume:g}×{m3_o25:g} ₽/м³" if float(volume) > 0 else ""),
            "message": "Использована явная весовая колонка официальной страницы: от 3000 кг. Для весовой матрицы объём не является обязательным условием применения этой ставки.",
            "price_is_minimum": False,
        }
    return {
        "price": None, "status": "document_unavailable", "source_type": "Открытая таблица тарифов Байкал Сервис",
        "source_url": url, "freshness": freshness,
        "formula": "Интерполяция между опубликованными диапазонами отключена.",
        "message": "Маршрут найден, но для этого веса/объёма официальный открытый прайс публикует только крайние диапазоны: до 100 кг/до 1 м³ и от 3000 кг/от 25 м³.",
    }


def dpd_public_reference(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    origin, destination = _norm_city(origin), _norm_city(destination)
    row = DPD_DOCUMENT_REFERENCE.get((origin, destination))
    if not row or weight > float(row["max_weight"]):
        return None
    # 2026 основная ставка НДС РФ — 22%; на странице DPD пример указан без НДС.
    total = float(row["price_without_vat"]) * 1.22
    return {
        "price": round(total), "status": "ok", "source_type": "Официальный публичный пример DPD",
        "source_url": "https://dpd.ru/dostavka-dokumentov", "freshness": "public_page",
        "formula": f"{row['price_without_vat']:g} ₽ без НДС × 1,22",
        "message": "Применено только потому, что вес попадает в опубликованный пример DPD до 1 кг; для более тяжёлых грузов этот источник не используется.",
        "delivery_days_min": row["days"][0], "delivery_days_max": row["days"][1],
    }


def _pony_pdf_path() -> tuple[Path, str]:
    # Сначала уже скачанный пользователем официальный файл.
    candidates = sorted(DOWNLOAD_DIR.glob("*Pony*")) + sorted(DOWNLOAD_DIR.glob("*PONY*")) + sorted(DOWNLOAD_DIR.glob("*.pdf"))
    for path in candidates:
        if path.is_file() and path.stat().st_size > 5000:
            try:
                from pypdf import PdfReader
                sample = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages[:3])
                if "PONY" in sample.upper():
                    return path, "downloaded"
            except Exception:
                continue
    return _cached_binary(PONY_CURRENT_PDF, "pony_current_tariffs.pdf", ttl_hours=168, referer="https://www.ponyexpress.ru/")


def _pony_text(path: Path) -> str:
    from pypdf import PdfReader
    parts: list[str] = []
    for p in PdfReader(str(path)).pages:
        try:
            txt = p.extract_text(extraction_mode="layout") or ""
        except TypeError:
            txt = p.extract_text() or ""
        parts.append(txt)
    return "\n".join(parts)


def _pony_find_standard_plus_row(text: str, origin: str, destination: str) -> list[float] | None:
    # Стандарт+ имеет маршрутную таблицу: 100 кг, +1 кг, 200 кг, +1 кг, 500 кг, +1 кг, 1000 кг, +1 кг, 1500 кг, +1 кг.
    norm = text.replace("\xa0", " ").replace("Санкт- Петербург", "Санкт-Петербург")
    lines = [re.sub(r"\s+", " ", x).strip() for x in norm.splitlines() if x.strip()]
    o = origin.lower().replace("ё", "е")
    d = destination.lower().replace("ё", "е")
    for line in lines:
        low = line.lower().replace("ё", "е")
        if o not in low or d not in low:
            continue
        # Отсекаем таблицы, где маршрут не стоит в начале строки.
        first_pos, second_pos = low.find(o), low.find(d)
        if first_pos < 0 or second_pos <= first_pos:
            continue
        nums = []
        tail = line[second_pos + len(destination):]
        for m in re.finditer(r"(?<!\d)(\d{1,6}(?:[,.]\d+)?)(?!\d)", tail):
            try:
                nums.append(float(m.group(1).replace(",", ".")))
            except Exception:
                pass
        if len(nums) >= 10:
            return nums[:10]
    # Иногда pypdf склеивает строку с индексами столбцов; ищем regex по всему тексту.
    pat = re.compile(re.escape(origin) + r"\s+" + re.escape(destination) + r"(?P<body>(?:\s+\d+(?:[,.]\d+)?){10,14})", re.I)
    m = pat.search(norm)
    if m:
        return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[,.]\d+)?", m.group("body"))[:10]]
    return None


def _tiered_price(weight: float, nums: list[float]) -> float | None:
    if len(nums) < 10 or weight < 100:
        return None
    bases = [(100.0, nums[0], nums[1]), (200.0, nums[2], nums[3]), (500.0, nums[4], nums[5]), (1000.0, nums[6], nums[7]), (1500.0, nums[8], nums[9])]
    if weight <= 100:
        return nums[0]
    if weight <= 200:
        return nums[0] + math.ceil(weight - 100) * nums[1]
    if weight <= 500:
        return nums[2] + math.ceil(weight - 200) * nums[3]
    if weight <= 1000:
        return nums[4] + math.ceil(weight - 500) * nums[5]
    if weight <= 1500:
        return nums[6] + math.ceil(weight - 1000) * nums[7]
    return nums[8] + math.ceil(weight - 1500) * nums[9]


def pony_public_pdf(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    origin, destination = _norm_city(origin), _norm_city(destination)
    # PONY тарифицирует по большему из физического и объёмного веса (делитель 5000),
    # а у Стандарт+ минимальный оплачиваемый вес — 100 кг. 1 м³ = 200 кг объёмного веса.
    chargeable_weight = max(100.0, float(weight), float(volume) * 200.0)
    # Для уже подтверждённых строк сначала используем локальный официальный снимок —
    # это убирает зависимость от 403 на прямой PDF-ссылке PONY. Для других маршрутов
    # пробуем скачать/разобрать сам PDF.
    nums = PONY_STANDARD_PLUS_REFERENCE.get((origin, destination))
    freshness = "official_snapshot_2025-05-19"
    if not nums:
        try:
            path, freshness = _pony_pdf_path()
            text = _pony_text(path)
            nums = _pony_find_standard_plus_row(text, origin, destination)
        except Exception:
            nums = None
    if not nums:
        return None
    base = _tiered_price(chargeable_weight, nums)
    if base is None:
        return None
    # Справочник указывает тариф без НДС и топливной надбавки. В 2026 основная ставка НДС 22%.
    # Топливную надбавку не угадываем, поэтому это доказуемая нижняя граница, а не полный итог.
    with_vat = base * 1.22
    return {
        "price": round(with_vat), "status": "ok", "source_type": "Официальный PDF PONY EXPRESS · Стандарт+",
        "source_url": PONY_CURRENT_PDF, "freshness": freshness,
        "formula": f"оплачиваемый вес {chargeable_weight:g} кг; база Стандарт+ {base:.0f} ₽ + НДС 22%; топливная надбавка не включена",
        "message": "Рассчитано по маршрутной строке Стандарт+ официального справочника с 19.05.2025. Топливная надбавка публикуется отдельно, поэтому сумма отмечена как нижняя граница.",
        "price_is_minimum": True,
    }


def pek_public_route_page(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    # Официальная маршрутная страница публикует минимум и ставку 'от 3000 кг'. Используем только когда вес действительно >= 3000.
    origin, destination = _norm_city(origin), _norm_city(destination)
    slug = {
        "Москва": "moskva", "Санкт-Петербург": "sankt-peterburg", "Екатеринбург": "ekaterinburg",
        "Нижний Новгород": "nizhniy-novgorod", "Казань": "kazan", "Самара": "samara", "Воронеж": "voronezh",
        "Ростов-на-Дону": "rostov-na-donu", "Краснодар": "krasnodar", "Новосибирск": "novosibirsk", "Челябинск": "chelyabinsk",
    }
    if origin not in slug or destination not in slug:
        return None
    url = f"https://pecom.ru/all-dir/{slug[origin]}/{slug[origin]}_{slug[destination]}/"
    try:
        html, freshness = _cached_text(url, f"pek_route_{slug[origin]}_{slug[destination]}.html", ttl_hours=8)
        text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))
        rate_m = re.search(r"От\s*3000\s*кг.{0,250}?Стоимость доставки за 1 кг[^0-9]{0,40}([0-9]+(?:[,.][0-9]+)?)", text, re.I)
        min_m = re.search(r"Минимальная стоимость доставки по выбранному направлению:\s*([0-9\s]+)\s*руб", text, re.I)
        rate, minimum = (_money(rate_m.group(1)) if rate_m else None), (_money(min_m.group(1)) if min_m else None)
        if weight >= 3000 and rate:
            price = max(float(minimum or 0), float(weight) * rate)
            return {
                "price": round(price), "status": "ok", "source_type": "Официальная маршрутная страница ПЭК",
                "source_url": url, "freshness": freshness, "formula": f"max({minimum or 0:g}; {weight:g}×{rate:g} ₽/кг)",
                "message": "Применена опубликованная ПЭК ставка для грузов от 3000 кг.",
            }
    except Exception:
        return None
    return None


def baikal_snapshot_tariff(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    """Быстрый локальный снимок официальной таблицы Байкал Сервис.

    Используется только в матрице диапазонов, чтобы не выполнять повторные сетевые
    запросы. Живой выбранный расчёт продолжает использовать baikal_public_tariff().
    """
    origin, destination = _norm_city(origin), _norm_city(destination)
    row = BAIKAL_REFERENCE.get((origin, destination))
    if not row:
        return None
    kg_u100, kg_o3000, m3_u1, m3_o25 = row
    if weight <= 100 and volume <= 1:
        price = max(float(weight) * kg_u100, float(volume) * m3_u1)
        return {
            "price": round(price), "status": "ok", "source_type": "Официальный снимок таблицы Байкал Сервис",
            "source_url": "https://www.baikalsr.ru/business/prices/", "freshness": "official_snapshot_2026-08-21",
            "formula": f"max({weight:g}×{kg_u100:g} ₽/кг; {volume:g}×{m3_u1:g} ₽/м³)",
            "message": "Проверенный снимок официальной таблицы популярных направлений от 21.08.2026; используется в быстрой матрице.",
            "price_is_minimum": False,
        }
    if weight >= 3000 and volume >= 25:
        price = max(float(weight) * kg_o3000, float(volume) * m3_o25)
        return {
            "price": round(price), "status": "ok", "source_type": "Официальный снимок таблицы Байкал Сервис",
            "source_url": "https://www.baikalsr.ru/business/prices/", "freshness": "official_snapshot_2026-08-21",
            "formula": f"max({weight:g}×{kg_o3000:g} ₽/кг; {volume:g}×{m3_o25:g} ₽/м³)",
            "message": "Проверенный снимок официальной таблицы популярных направлений от 21.08.2026; используется в быстрой матрице.",
            "price_is_minimum": False,
        }
    return None
