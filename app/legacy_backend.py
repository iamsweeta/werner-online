from __future__ import annotations

import json
import hashlib
import math
import os
import re
import statistics
import subprocess
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import legacy as core
from .public_web import browser_runtime_status, calculate_from_public_site, calculate_many_from_public_site
from .document_sources import company_catalog, public_document_result
from .open_source_tariffs import baikal_public_tariff, baikal_snapshot_tariff, dpd_public_reference, pony_public_pdf, pek_public_route_page, BAIKAL_REFERENCE
from .extended_sources import (
    proline_tariff, fasttrans_tariff, atek_tariff, bsk_tariff, glavtrassa_tariff,
    magic_tariff, fortuna_tariff, newline_tariff, expeditionplus_tariff, ctsgroup_tariff,
    PROLINE_ROUTES, FASTTRANS_SNAPSHOTS, ATEK_MSK_SPB, BSD_MSK_SPB, BSD_SPB_MSK, MAGIC_ROUTE_MINIMUMS,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = Path(os.environ.get("TARIFF_DATA_DIR") or BASE_DIR / "runtime").expanduser().resolve()
CACHE_DIR = RUNTIME_DIR / "cache"
EXPORT_DIR = RUNTIME_DIR / "exports"
SETTINGS_PATH = RUNTIME_DIR / "settings.json"
COMPARISON_CACHE_PATH = RUNTIME_DIR / "comparison_cache.json"

for p in [CACHE_DIR, EXPORT_DIR]:
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Сравнение тарифов", version="36.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

HTTP = requests.Session()
_FAILURE_BACKOFF: dict[str, dict[str, Any]] = {}
_DELLIN_PARSED_CACHE: dict[str, dict[str, Any]] = {}
_RESULT_CACHE_LOCK = threading.Lock()
_COLLECT_RUN_LOCK = threading.Lock()
_COLLECT_JOBS_LOCK = threading.Lock()
_COLLECT_JOBS: dict[str, dict[str, Any]] = {}

HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TariffComparison/36.0",
    "Accept": "application/json,text/plain,*/*",
})

# Официальный тестовый ключ опубликован на странице разработчиков Возовоза.
VOZOVOZ_DEMO_TOKEN = "G934UM29wXG2IKYS9iX9oKQCeojkApHSEWAxOC5v"
VOZOVOZ_EXACT_CACHE_PATH = CACHE_DIR / "vozovoz_exact_prices.json"

# Официальные публичные минимальные цены со страницы тарифов Возовоза.
# Они используются только как резервная нижняя граница, если демо API недоступен.
VOZOVOZ_PUBLIC_FALLBACK = {}  # v50: embedded prices disabled; use current collectors or user imports.

# Verified public route minima for the reverse direction.  These values are
# intentionally marked as lower bounds and NEVER promoted to an exact weight
# tariff.  They keep the UI truthful when a carrier's PDF/API is temporarily
# unavailable on the user's Windows network.  Verified on 2026-09-05 from the
# carriers' own public route pages.
SPB_MSK_PUBLIC_ROUTE_MINIMUMS = {}  # v50: embedded prices disabled; use current collectors or user imports.

def spb_msk_public_route_minimum(company: str, origin: str, destination: str) -> dict[str, Any] | None:
    if (normalize_city(origin), normalize_city(destination)) != ("Санкт-Петербург", "Москва"):
        return None
    row = SPB_MSK_PUBLIC_ROUTE_MINIMUMS.get(company)
    if not row:
        return None
    return {
        "company": company, "company_label": COMPANY_LABELS.get(company, company),
        "status": "ok", "price": float(row["price"]), "currency": "RUB",
        "delivery_days_min": None, "delivery_days_max": None,
        "source_type": row["source_type"], "source_url": row["url"],
        "freshness": row["freshness"],
        "formula": "официально опубликованная минимальная цена маршрута; фактическая стоимость зависит от параметров груза",
        "message": "Точная весовая строка временно недоступна; показана только официальная цена «от». Она не участвует в графике как точный тариф.",
        "price_is_minimum": True,
    }

# Открытые опубликованные ставки КИТ используются как резерв, если маршрутная
# страница временно недоступна. Значения относятся к официальным страницам
# маршрутов и не являются персональным договорным тарифом.
KIT_PUBLIC_FALLBACK = {}  # v50: embedded prices disabled; use current collectors or user imports.


# ПЭК публикует на официальных маршрутных страницах весовую ставку для
# тяжёлых отправок «от 3000 кг». Используем её только в тех диапазонах,
# где опубликованная ставка действительно применима; меньшие веса не
# интерполируем. Проверено 21.08.2026.
PEK_HEAVY_WEIGHT_RATES = {}  # v50: embedded prices disabled; use current collectors or user imports.

# Рейл Континент публикует отдельную весовую таблицу руб/кг.
# Для Москва ↔ Санкт-Петербург тарифы актуальны с 17.07.2026.
# Это более подходящий источник для строгого режима «только ₽/кг», чем старые
# контрольные расчёты вес+объём.
RAIL_WEIGHT_RATES = {}  # v50: embedded prices disabled; use current collectors or user imports.

def rail_weight_tariff(origin: str, destination: str, weight: float) -> dict[str, Any] | None:
    row = RAIL_WEIGHT_RATES.get((normalize_city(origin), normalize_city(destination)))
    if not row or float(weight) <= 0:
        return None
    idx = next((i for i, limit in enumerate(row["limits"]) if float(weight) <= float(limit)), len(row["rates"]) - 1)
    rate = float(row["rates"][idx])
    price = max(float(row["minimum"]), float(weight) * rate)
    prev_limit = 0.0 if idx == 0 else float(row["limits"][idx - 1])
    low = 0.0 if idx == 0 else prev_limit + 1.0
    high_raw = float(row["limits"][idx])
    high = None if math.isinf(high_raw) else high_raw
    return {
        "company": "Рейл Континент", "company_label": COMPANY_LABELS["Рейл Континент"], "status": "ok",
        "price": round(price, 2), "currency": "RUB", "delivery_days_min": 1, "delivery_days_max": 1,
        "source_type": "Официальная весовая тарифная таблица Рейл Континент",
        "source_url": str(row["url"]), "freshness": f"official_weight_table_{row['effective']}",
        "formula": f"max(минимум {row['minimum']:g} ₽; {float(weight):g} кг × {rate:g} ₽/кг)",
        "message": f"Весовой тариф руб/кг из официальной таблицы, действует с {row['effective']}. Объём в сравнении не участвует.",
        "price_is_minimum": False, "rate_per_kg": rate,
        "tariff_range_low_kg": low, "tariff_range_high_kg": high, "range_value_repeated": True,
    }

# Фиксированные тарифы Werner Москва → Санкт-Петербург, сверенные по
# официальной тарифной сетке на рабочем созвоне 25.08.2026. Эти суммы
# относятся к отправке целиком и НЕ делятся на килограммы.
WERNER_FIXED_MSK_SPB = []  # v50: embedded prices disabled; use current collectors or user imports.

def werner_fixed_tariff(origin: str, destination: str, weight: float) -> dict[str, Any] | None:
    if (normalize_city(origin), normalize_city(destination)) != ("Москва", "Санкт-Петербург"):
        return None
    w = float(weight)
    for low, high, price in WERNER_FIXED_MSK_SPB:
        # Контрольная точка строки должна попадать в опубликованный широкий
        # диапазон. Границы могут совпадать; в спорной общей границе приоритет
        # имеет более ранняя строка исходной сетки (например, 5 кг остаётся 196 ₽).
        if w >= low - 1e-9 and w <= high + 1e-9:
            return {
                "company": "Werner", "company_label": COMPANY_LABELS.get("Werner", "Werner"),
                "status": "ok", "price": price, "currency": "RUB",
                "delivery_days_min": None, "delivery_days_max": None,
                "source_type": "Официальная фиксированная тарифная сетка Werner",
                "source_url": "https://wernerus.ru/clients/prices/",
                "freshness": "official_tariff_verified_2026-08-28",
                "formula": f"фиксированная стоимость отправки {price:g} ₽ для исходного диапазона {low:g}–{high:g} кг",
                "message": "Один исходный весовой диапазон повторяется во всех строках единой сетки, контрольный вес которых в него попадает; фиксированная стоимость не переводится в ₽/кг.",
                "price_is_minimum": False, "pricing_mode": "fixed_shipment",
                "tariff_range_low_kg": low, "tariff_range_high_kg": high,
                "range_value_repeated": True,
            }
    return None


# Werner публикует полноценную тарифную таблицу прямо на официальной странице
# https://wernerus.ru/clients/prices/ и также даёт скачиваемые файлы. В v29
# HTML-таблица является таким же первичным источником, как XLS/XLSX: мы читаем
# исходные колонки 41-99, 100-199, 200-499, 500-999, 1000-1999, 2000-2999,
# «от 3000» и повторяем одну ставку во всех строках клиентской сетки, которые
# попадают в соответствующую колонку. API используется только как резерв.
WERNER_PUBLISHED_KG_SLABS: list[tuple[float, float | None, float]] = [
    (41.0, 99.0, 99.0),
    (100.0, 199.0, 100.0),
    (200.0, 499.0, 300.0),
    (500.0, 999.0, 500.0),
    (1000.0, 1999.0, 1000.0),
    (2000.0, 2999.0, 2000.0),
    (3000.0, None, 3000.0),
]

def _werner_published_kg_slab(weight: float) -> tuple[float, float | None, float] | None:
    w = float(weight)
    for low, high, representative in WERNER_PUBLISHED_KG_SLABS:
        if w >= low - 1e-9 and (high is None or w <= high + 1e-9):
            return low, high, representative
    return None

_WERNER_SOURCE_LOCK = threading.Lock()
_WERNER_SOURCE_ATTEMPTED = False
_WERNER_TABLE_CACHE: dict[str, list[dict[str, Any]]] = {}
_DIRECT_FILE_CANDIDATE_CACHE: dict[str, list[dict[str, Any]]] = {}


BUNDLED_SOURCE_FILES = {}  # v50: embedded prices disabled; use current collectors or user imports.


def _bundled_source_files(source_ids: set[str], extensions: set[str] | None = None) -> list[Path]:
    """Return last-known-good official files shipped with the application.

    Older builds stored valid official downloads under ``data/source_files`` but
    the calculator only searched ``runtime/results.json`` / ``runtime/downloads``.
    On another PC those runtime paths point to the build machine and every such
    tariff becomes empty. Bundled source files are therefore first-class local
    evidence and are used whenever their source id matches.
    """
    extensions = extensions or {".xlsx", ".xls", ".csv", ".html", ".htm", ".bin", ".pdf", ".txt"}
    root = DATA_DIR / "source_files"
    found: list[Path] = []
    for source_id in source_ids:
        for name in BUNDLED_SOURCE_FILES.get(str(source_id), ()):
            path = root / name
            if path.exists() and path.is_file() and path.suffix.lower() in extensions:
                found.append(path)
    return found


def _file_confirms_origin(path: Path, origin: str) -> bool:
    """Reject a city-specific tariff file when its printed origin is different."""
    wanted = normalize_city(origin)
    name = path.name.lower()
    if "moscow" in name or "москва" in name:
        return wanted == "Москва"
    if "spb" in name or "peter" in name or "петербург" in name:
        return wanted == "Санкт-Петербург"
    if path.suffix.lower() == ".txt":
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:12000]
            m = re.search(r"из\s+города\s+([^*\n\r]+)", head, flags=re.I)
            if m:
                return normalize_city(m.group(1).strip()) == wanted
        except Exception:
            pass
    # Generic consolidated workbooks may contain many origins, so lack of a
    # city marker is not evidence of a mismatch.
    return True


def _werner_source_files() -> list[Path]:
    """Return downloaded official Werner tariff files, newest first."""
    try:
        results = core.load_results()
    except Exception:
        results = {}
    found: list[Path] = []
    for meta in results.get("files", []):
        if not isinstance(meta, dict) or str(meta.get("source_id") or "") != "W001":
            continue
        raw_path = str(meta.get("path") or "").strip()
        candidates: list[Path] = []
        if raw_path:
            candidates.append(Path(raw_path))
        if meta.get("file"):
            candidates.append(core.DOWNLOAD_DIR / str(meta["file"]))
        for path in candidates:
            if path.exists() and path.is_file() and path.suffix.lower() in {".xlsx", ".xls", ".csv", ".html", ".htm", ".bin", ".pdf"}:
                found.append(path)
            if path.exists() and path.is_file() and path.suffix.lower() == ".zip":
                extracted = core.DOWNLOAD_DIR / (path.stem + "_unzipped")
                if extracted.exists():
                    found.extend(x for x in extracted.rglob("*") if x.is_file() and x.suffix.lower() in {".xlsx", ".xls", ".csv"})
    found.extend(_bundled_source_files({"W001"}))
    unique = {str(x.resolve()): x for x in found}
    return sorted(unique.values(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)


def _source_files_for_ids(source_ids: set[str], extensions: set[str] | None = None) -> list[Path]:
    """Return locally collected official files for the requested source ids."""
    extensions = extensions or {".xlsx", ".xls", ".csv", ".html", ".htm", ".bin", ".pdf", ".txt"}
    try:
        results = core.load_results()
    except Exception:
        results = {}
    found: list[Path] = []
    for meta in results.get("files", []):
        if not isinstance(meta, dict) or str(meta.get("source_id") or "") not in source_ids:
            continue
        paths: list[Path] = []
        if meta.get("path"):
            paths.append(Path(str(meta["path"])))
        if meta.get("file"):
            paths.append(core.DOWNLOAD_DIR / str(meta["file"]))
        for path in paths:
            if path.exists() and path.is_file() and path.suffix.lower() in extensions:
                found.append(path)
            if path.exists() and path.is_file() and path.suffix.lower() == ".zip":
                extracted = core.DOWNLOAD_DIR / (path.stem + "_unzipped")
                if extracted.exists():
                    found.extend(x for x in extracted.rglob("*") if x.is_file() and x.suffix.lower() in extensions)
    # Results.json is only an index. If it was reset while downloaded files
    # remained on disk, recover source-owned files by the collector filename
    # prefix (source id is always the first component of the saved filename).
    try:
        prefixes = tuple((core.slugify(str(sid)) + "_").lower() for sid in source_ids)
        for path in core.DOWNLOAD_DIR.iterdir():
            if not path.is_file() or not path.name.lower().startswith(prefixes):
                continue
            if path.suffix.lower() in extensions:
                found.append(path)
            if path.suffix.lower() == ".zip":
                extracted = core.DOWNLOAD_DIR / (path.stem + "_unzipped")
                if extracted.exists():
                    found.extend(x for x in extracted.rglob("*") if x.is_file() and x.suffix.lower() in extensions)
    except Exception:
        pass
    found.extend(_bundled_source_files(source_ids, extensions))
    unique = {str(x.resolve()): x for x in found}
    return sorted(unique.values(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)


def ensure_werner_source_collected() -> list[Path]:
    """Fetch Werner's official tariff page once when no tariff file is cached."""
    global _WERNER_SOURCE_ATTEMPTED
    files = _werner_source_files()
    if files:
        return files
    with _WERNER_SOURCE_LOCK:
        files = _werner_source_files()
        if files or _WERNER_SOURCE_ATTEMPTED:
            return files
        _WERNER_SOURCE_ATTEMPTED = True
        try:
            core.collect_sources(selected_companies=["Werner"], limit=1)
        except Exception:
            # The live API below is still a valid official fallback; a temporary
            # download failure must not break the whole comparison matrix.
            pass
        return _werner_source_files()


def _werner_cell_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    text = str(value).replace("\xa0", " ").strip()
    if not text or text in {"-", "—", "–"}:
        return None
    match = re.search(r"(?<!\d)(\d{1,3}(?:[ ]\d{3})+|\d+(?:[,.]\d+)?)(?!\d)", text)
    if not match:
        return None
    try:
        n = float(match.group(1).replace(" ", "").replace(",", "."))
        return n if n > 0 else None
    except Exception:
        return None


def _werner_city_key(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"\b(?:г\.?|город)\b", " ", text)
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def _werner_range_from_text(value: Any) -> tuple[float | None, float | None] | None:
    """Parse a weight slab from a Werner header cell."""
    text = str(value or "").lower().replace("ё", "е").replace("\xa0", " ")
    text = text.replace("–", "-").replace("—", "-")
    # Ignore volume-only headers and obvious dates/years unless kg is explicit.
    if re.search(r"(?:м\s*[³3]|куб)", text) and "кг" not in text:
        return None
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*-\s*(\d+(?:[,.]\d+)?)", text)
    if m:
        low, high = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        if 0 <= low < high <= 100000:
            return low, high
    m = re.search(r"\bдо\s*(\d+(?:[,.]\d+)?)", text)
    if m:
        high = float(m.group(1).replace(",", "."))
        if 0 < high <= 100000:
            return 0.0, high
    m = re.search(r"\bот\s*(\d+(?:[,.]\d+)?)", text)
    if m:
        low = float(m.group(1).replace(",", "."))
        if 0 < low <= 100000:
            return low, None
    # Tier labels in spreadsheets are often just "100", "300", ... .
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"\d+(?:[,.]\d+)?(?:кг)?", compact):
        high = float(re.sub(r"кг$", "", compact).replace(",", "."))
        if 0 < high <= 100000 and not (1900 <= high <= 2100):
            return 0.0, high
    return None


def _werner_unit_from_context(parts: list[Any], slab: tuple[float | None, float | None] | None) -> str | None:
    joined = " | ".join(str(x or "") for x in parts).lower().replace("ё", "е").replace("\xa0", " ")
    joined = joined.replace("₽", "руб").replace("³", "3")
    if "миним" in joined or re.search(r"\bмин\.?\b", joined):
        return "minimum"
    # Weight and volume tariffs are in one wide Werner table. Reject the volume
    # block before generic range inference; labels such as 0,2-0,99 are m³, not kg.
    if re.search(r"руб\s*\.?\s*/\s*м\s*3", joined) or re.search(r"руб[^|]{0,20}за\s*м\s*3", joined):
        return "volume"
    if re.search(r"руб\s*\.?\s*/\s*кг", joined) or re.search(r"руб[^|]{0,15}за\s*кг", joined) or "р/кг" in joined:
        return "per_kg"
    if "за отправ" in joined or "стоимость отправ" in joined or re.search(r"руб[^|]{0,15}отправ", joined):
        return "fixed"
    if slab:
        low, high = slab
        bound = high if high is not None else low
        if bound is not None and bound <= 50:
            return "fixed"
        if bound is not None and bound >= 100:
            return "per_kg"
    return None


def _werner_sheet_rows(path: Path) -> list[tuple[str, list[list[Any]]]]:
    """Load spreadsheet values and expand merged headers for tariff parsing."""
    cache_key = f"{path.resolve()}|{path.stat().st_mtime_ns}|{path.stat().st_size}"
    cached = _WERNER_TABLE_CACHE.get(cache_key)
    if cached is not None:
        # Type is intentionally stored as a list of sheet dictionaries below.
        return [(str(x["name"]), x["rows"]) for x in cached]
    sheets: list[tuple[str, list[list[Any]]]] = []
    ext = path.suffix.lower()
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=False)
        for ws in wb.worksheets:
            max_row = min(int(ws.max_row or 0), 1500)
            max_col = min(int(ws.max_column or 0), 160)
            matrix = [[ws.cell(r, c).value for c in range(1, max_col + 1)] for r in range(1, max_row + 1)]
            for merged in ws.merged_cells.ranges:
                if merged.min_row > max_row or merged.min_col > max_col:
                    continue
                value = ws.cell(merged.min_row, merged.min_col).value
                for rr in range(merged.min_row, min(merged.max_row, max_row) + 1):
                    for cc in range(merged.min_col, min(merged.max_col, max_col) + 1):
                        matrix[rr - 1][cc - 1] = value
            sheets.append((ws.title, matrix))
        wb.close()
    elif ext == ".xls":
        import xlrd
        book = xlrd.open_workbook(str(path), formatting_info=True, on_demand=True)
        for sh in book.sheets():
            max_row, max_col = min(sh.nrows, 1500), min(sh.ncols, 160)
            matrix = [[sh.cell_value(r, c) for c in range(max_col)] for r in range(max_row)]
            for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []):
                if rlo >= max_row or clo >= max_col:
                    continue
                value = matrix[rlo][clo]
                for rr in range(rlo, min(rhi, max_row)):
                    for cc in range(clo, min(chi, max_col)):
                        matrix[rr][cc] = value
            sheets.append((sh.name, matrix))
        book.release_resources()
    elif ext == ".csv":
        import csv, io
        raw = path.read_bytes()
        text = ""
        for enc in ("utf-8-sig", "cp1251", "utf-8"):
            try:
                text = raw.decode(enc); break
            except Exception:
                pass
        if text:
            try:
                dialect = csv.Sniffer().sniff(text[:2000])
            except Exception:
                dialect = csv.excel
            matrix = [list(row)[:160] for row in csv.reader(io.StringIO(text), dialect)][:1500]
            sheets.append(("CSV", matrix))
    elif ext == ".txt":
        # Some official XLS/XLSX files are retained as a text/Markdown export
        # when the remote binary later becomes unavailable. The export preserves
        # the printed table row order, which is sufficient for exact tariff use.
        text = path.read_text(encoding="utf-8", errors="ignore")
        header = [
            "Пункт назначения", "до 5 кг", "до 20 кг", "до 40 кг",
            "от 3000 кг", "2000-2999 кг", "1000-1999 кг", "500-999 кг",
            "200-499 кг", "100-199 кг", "41-99 кг",
            "от 15 м3", "10-14.9 м3", "5-9.9 м3", "3-4.9 м3", "1-2.9 м3", "0.2-0.99 м3",
        ]
        units = ["Пункт назначения", "Фиксированные тарифы", "Фиксированные тарифы", "Фиксированные тарифы"] + ["Стоимость, руб./кг"] * 7 + ["Стоимость, руб./м³"] * 6
        matrix: list[list[Any]] = [units, header]
        row_re = re.compile(r"^(.+?)\s+((?:\d+(?:[\s\u00a0]\d{3})*(?:[,.]\d+)?\s+){15}\d+(?:[\s\u00a0]\d{3})*(?:[,.]\d+)?)\s*$")
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line or line.startswith("**") or line.startswith("#"):
                continue
            m = row_re.match(line)
            if not m:
                continue
            city = m.group(1).strip()
            # The official text export separates tariff cells by whitespace.
            # Do not treat three adjacent cells such as ``196 403 460`` as one
            # thousands-grouped number.
            nums = m.group(2).replace("\u00a0", " ").split()
            if len(nums) != 16:
                continue
            values: list[Any] = [city]
            for token in nums:
                try:
                    values.append(float(token.replace("\u00a0", "").replace(" ", "").replace(",", ".")))
                except Exception:
                    values.append(token)
            matrix.append(values)
        if len(matrix) > 2:
            sheets.append(("Official text export", matrix[:1500]))
    elif ext in {".html", ".htm", ".bin"}:
        raw = path.read_bytes()
        html = ""
        for enc in ("utf-8", "utf-8-sig", "cp1251", "windows-1251"):
            try:
                html = raw.decode(enc, errors="ignore")
                if html:
                    break
            except Exception:
                pass
        if html:
            soup = BeautifulSoup(html, "lxml")

            def expand_table(table) -> list[list[Any]]:
                grid: list[list[Any]] = []
                spans: dict[int, tuple[int, Any]] = {}
                for tr in table.find_all("tr"):
                    row: list[Any] = []
                    col = 0
                    cells = tr.find_all(["th", "td"], recursive=False)
                    ci = 0
                    while ci < len(cells) or spans:
                        while col in spans:
                            remaining, value = spans[col]
                            while len(row) <= col:
                                row.append(None)
                            row[col] = value
                            if remaining <= 1:
                                spans.pop(col, None)
                            else:
                                spans[col] = (remaining - 1, value)
                            col += 1
                        if ci >= len(cells):
                            future = [x for x in spans if x >= col]
                            if not future:
                                break
                            col = min(future)
                            continue
                        cell = cells[ci]; ci += 1
                        text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
                        try:
                            colspan = max(1, int(cell.get("colspan") or 1))
                        except Exception:
                            colspan = 1
                        try:
                            rowspan = max(1, int(cell.get("rowspan") or 1))
                        except Exception:
                            rowspan = 1
                        for off in range(colspan):
                            target = col + off
                            while len(row) <= target:
                                row.append(None)
                            row[target] = text
                            if rowspan > 1:
                                spans[target] = (rowspan - 1, text)
                        col += colspan
                    if any(str(v or "").strip() for v in row):
                        grid.append(row[:160])
                width = min(160, max((len(r) for r in grid), default=0))
                return [r + [None] * (width - len(r)) for r in grid]

            for ti, table in enumerate(soup.find_all("table")[:40], start=1):
                matrix = expand_table(table)
                if matrix:
                    sheets.append((f"HTML table {ti}", matrix[:1500]))
    # Keep several parsed official files. A route may use a page plus multiple
    # linked spreadsheets; clearing the whole cache here made the parser reopen
    # every file for every one of the 29 matrix rows.
    if len(_WERNER_TABLE_CACHE) >= 24:
        _WERNER_TABLE_CACHE.pop(next(iter(_WERNER_TABLE_CACHE)))
    _WERNER_TABLE_CACHE[cache_key] = [{"name": name, "rows": rows} for name, rows in sheets]
    return sheets


def _published_text_candidates(path: Path, destination: str) -> list[dict[str, Any]]:
    """Parse a retained text export of a Werner/Glavtrassa official workbook."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    dest_key = _werner_city_key(normalize_city(destination))
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or not dest_key:
            continue
        m = re.match(r"^(.+?)\s+([0-9][0-9\s,.]*)$", line)
        if not m or _werner_city_key(m.group(1)) != dest_key:
            continue
        tokens = m.group(2).replace("\u00a0", " ").split()
        if len(tokens) != 16:
            continue
        try:
            values = [float(x.replace(",", ".")) for x in tokens]
        except Exception:
            continue
        fixed_ranges = [(0.0, 5.0), (0.0, 20.0), (0.0, 40.0)]
        kg_ranges = [(3000.0, None), (2000.0, 2999.0), (1000.0, 1999.0), (500.0, 999.0), (200.0, 499.0), (100.0, 199.0), (41.0, 99.0)]
        out: list[dict[str, Any]] = []
        for i, (low, high) in enumerate(fixed_ranges):
            out.append({"mode":"fixed","low":low,"high":high,"value":values[i],"header":f"до {int(high)} кг","sheet":"Official text export","column":i+2,"file":path.name,"path":str(path)})
        for j, (low, high) in enumerate(kg_ranges, start=3):
            label = f"от {int(low)} кг" if high is None else f"{int(low)}-{int(high)} кг"
            out.append({"mode":"per_kg","low":low,"high":high,"value":values[j],"header":label,"sheet":"Official text export","column":j+2,"file":path.name,"path":str(path)})
        return out
    return []


def _werner_candidates_from_file(path: Path, destination: str) -> list[dict[str, Any]]:
    dest_key = _werner_city_key(normalize_city(destination))
    try:
        candidate_key = f"{path.resolve()}|{path.stat().st_mtime_ns}|{path.stat().st_size}|{dest_key}"
    except Exception:
        candidate_key = f"{path}|{dest_key}"
    cached_candidates = _DIRECT_FILE_CANDIDATE_CACHE.get(candidate_key)
    if cached_candidates is not None:
        return [dict(x) for x in cached_candidates]
    candidates: list[dict[str, Any]] = []
    if path.suffix.lower() == ".txt":
        candidates = _published_text_candidates(path, destination)
        if len(_DIRECT_FILE_CANDIDATE_CACHE) >= 64:
            _DIRECT_FILE_CANDIDATE_CACHE.pop(next(iter(_DIRECT_FILE_CANDIDATE_CACHE)))
        _DIRECT_FILE_CANDIDATE_CACHE[candidate_key] = [dict(x) for x in candidates]
        return candidates
    try:
        sheets = _werner_sheet_rows(path)
    except Exception:
        return []
    for sheet_name, rows in sheets:
        for ri, row in enumerate(rows):
            if not row:
                continue
            dest_cols = [ci for ci, value in enumerate(row[:8]) if _werner_city_key(value) == dest_key]
            if not dest_cols:
                # Destination is usually the first column, but allow a decorated
                # value such as "Санкт-Петербург (терминал)".
                dest_cols = [ci for ci, value in enumerate(row[:8]) if dest_key and _werner_city_key(value).startswith(dest_key)]
            if not dest_cols:
                continue
            header_start = max(0, ri - 14)
            width = len(row)
            for ci in range(width):
                value = _werner_cell_number(row[ci])
                if value is None:
                    continue
                header_parts = [rows[h][ci] if ci < len(rows[h]) else None for h in range(header_start, ri)]
                # Pick the closest header fragment that actually looks like a weight slab.
                slab = None
                slab_text = ""
                for part in reversed(header_parts):
                    parsed = _werner_range_from_text(part)
                    if parsed is not None:
                        slab = parsed; slab_text = str(part); break
                unit = _werner_unit_from_context(header_parts, slab)
                if unit is None or unit == "volume":
                    continue
                if unit != "minimum" and slab is None:
                    continue
                candidates.append({
                    "mode": unit, "low": slab[0] if slab else None, "high": slab[1] if slab else None,
                    "value": value, "header": slab_text, "sheet": sheet_name, "column": ci + 1,
                    "file": path.name, "path": str(path),
                })

        # Some Werner exports may be transposed: cities are columns while weight
        # slabs are rows.  Support that layout as well so the source parser does
        # not depend on one particular Excel presentation.
        for header_ri, header_row in enumerate(rows):
            dest_columns = [ci for ci, value in enumerate(header_row) if _werner_city_key(value) == dest_key]
            if not dest_columns:
                continue
            for ci in dest_columns:
                for rj in range(header_ri + 1, min(len(rows), header_ri + 120)):
                    current = rows[rj]
                    if ci >= len(current):
                        continue
                    value = _werner_cell_number(current[ci])
                    if value is None:
                        continue
                    row_parts = current[:min(len(current), 10)]
                    slab = None
                    slab_text = ""
                    for part in row_parts:
                        parsed = _werner_range_from_text(part)
                        if parsed is not None:
                            slab = parsed; slab_text = str(part); break
                    context = row_parts + [header_row[ci] if ci < len(header_row) else None]
                    unit = _werner_unit_from_context(context, slab)
                    if unit is None or unit == "volume" or (unit != "minimum" and slab is None):
                        continue
                    candidates.append({
                        "mode": unit, "low": slab[0] if slab else None, "high": slab[1] if slab else None,
                        "value": value, "header": slab_text, "sheet": sheet_name, "column": ci + 1,
                        "file": path.name, "path": str(path),
                    })
    # PDF tariff files keep the same horizontal table layout. Use pypdf layout
    # extraction so we map printed weight headers to the value in the city row
    # instead of guessing from a calculator total.
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
        except Exception:
            reader = None
        if reader is not None:
            target = dest_key
            for page_no, page in enumerate(reader.pages[:120], start=1):
                try:
                    try:
                        text = page.extract_text(extraction_mode="layout") or ""
                    except TypeError:
                        text = page.extract_text() or ""
                except Exception:
                    continue
                lines = [line.rstrip("\r\n") for line in text.splitlines()]
                for li, line in enumerate(lines):
                    if not target or target not in _werner_city_key(line):
                        continue
                    header = lines[max(0, li - 22):li]
                    range_line = None
                    ranges = []
                    for hline in reversed(header):
                        rr = _all_weight_ranges_with_positions(hline)
                        if len(rr) >= 2:
                            range_line = hline; ranges = rr; break
                    if not ranges or range_line is None:
                        continue
                    header_text = " | ".join(header)
                    unit = "per_kg" if _strict_unit_marker(header_text) == "per_kg" or re.search(r"(?i)руб\.?\s*/\s*кг|₽\s*/\s*кг", header_text) else None
                    if unit is None and _strict_unit_marker(header_text) == "fixed":
                        unit = "fixed"
                    if unit is None:
                        continue
                    nums = []
                    for m in re.finditer(r"(?<!\d)(\d+(?:[\s\u00a0]\d{3})*(?:[,.]\d+)?)(?!\d)", line):
                        try:
                            val = float(m.group(1).replace("\u00a0", "").replace(" ", "").replace(",", "."))
                        except Exception:
                            continue
                        if val > 0:
                            nums.append((m.start(), m.end(), val))
                    if len(nums) < 2:
                        continue
                    last_pos = -1
                    mapped = []
                    for rs, re_, low, high, label in ranges:
                        center = (rs + re_) / 2
                        choices = [n for n in nums if n[0] > last_pos]
                        if not choices:
                            break
                        n = min(choices, key=lambda x: abs(((x[0] + x[1]) / 2) - center))
                        if abs(((n[0] + n[1]) / 2) - center) > 20:
                            continue
                        mapped.append((low, high, label, n[2], n[0])); last_pos = n[0]
                    if len(mapped) >= 2:
                        for low, high, label, value, _ in mapped:
                            candidates.append({
                                "mode": unit, "low": low, "high": high, "value": value,
                                "header": label, "sheet": f"PDF page {page_no}", "column": 0,
                                "file": path.name, "path": str(path),
                            })
    if len(_DIRECT_FILE_CANDIDATE_CACHE) >= 64:
        _DIRECT_FILE_CANDIDATE_CACHE.pop(next(iter(_DIRECT_FILE_CANDIDATE_CACHE)))
    _DIRECT_FILE_CANDIDATE_CACHE[candidate_key] = [dict(x) for x in candidates]
    return candidates


def _werner_select_candidate(candidates: list[dict[str, Any]], weight: float, mode: str) -> dict[str, Any] | None:
    w = float(weight)
    matches: list[dict[str, Any]] = []
    for cand in candidates:
        if cand.get("mode") != mode:
            continue
        if mode == "minimum":
            matches.append(cand); continue
        low, high = cand.get("low"), cand.get("high")
        low_v = float(low) if isinstance(low, (int, float)) else 0.0
        high_v = float(high) if isinstance(high, (int, float)) else float("inf")
        if w >= low_v - 1e-9 and w <= high_v + 1e-9:
            matches.append(cand)
    if not matches:
        return None
    def key(c: dict[str, Any]) -> tuple[float, float, int]:
        low = float(c.get("low") or 0.0)
        high = float(c["high"]) if isinstance(c.get("high"), (int, float)) else 1e12
        return (high - low, high, int(c.get("column") or 9999))
    return sorted(matches, key=key)[0]


def werner_official_tariff(origin: str, destination: str, weight: float, minimum: bool = False) -> dict[str, Any] | None:
    """Read the published Werner intercity tariff table and map it to our grid."""
    # W001 is the published "из г. Москва" matrix.  Other origins continue to
    # use the official API until their corresponding table is explicitly parsed.
    if normalize_city(origin) != "Москва":
        return None
    files = _werner_source_files()
    if not files:
        files = ensure_werner_source_collected()
    all_candidates: list[dict[str, Any]] = []
    for path in files:
        if _file_confirms_origin(path, origin):
            all_candidates.extend(_werner_candidates_from_file(path, destination))
    mode = "minimum" if minimum else ("fixed" if float(weight) <= 50 else "per_kg")
    selected = _werner_select_candidate(all_candidates, float(weight), mode)
    if not selected and not minimum and float(weight) >= 41:
        mode = "per_kg"
        selected = _werner_select_candidate(all_candidates, float(weight), mode)
    if not selected:
        return None
    value = float(selected["value"])
    low, high = selected.get("low"), selected.get("high")
    range_label = str(selected.get("header") or "опубликованная ступень")
    try:
        mtime = datetime.fromtimestamp(Path(str(selected["path"])).stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        mtime = now_iso()
    if mode == "per_kg":
        return {
            "company": "Werner", "company_label": COMPANY_LABELS.get("Werner", "Werner"), "status": "ok",
            "price": round(float(weight) * value, 2), "currency": "RUB", "rate_per_kg": value,
            "delivery_days_min": None, "delivery_days_max": None,
            "source_type": "Официальный тарифный файл Werner", "source_url": "https://wernerus.ru/clients/prices/",
            "freshness": f"official_file_{mtime}", "formula": f"{float(weight):g} кг × {value:g} ₽/кг; ступень {range_label}",
            "message": "Ставка прочитана из опубликованной тарифной таблицы Werner; одна исходная ступень повторяется во всех строках единой сетки, которые она покрывает.",
            "price_is_minimum": False, "pricing_mode": "per_kg", "range_value_repeated": True,
            "tariff_range_low_kg": low, "tariff_range_high_kg": high, "source_file": selected.get("file"),
        }
    return {
        "company": "Werner", "company_label": COMPANY_LABELS.get("Werner", "Werner"), "status": "ok",
        "price": round(value, 2), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": "Официальный тарифный файл Werner", "source_url": "https://wernerus.ru/clients/prices/",
        "freshness": f"official_file_{mtime}", "formula": f"фиксированная стоимость {value:g} ₽; ступень {range_label}",
        "message": "Цена прочитана из опубликованной тарифной таблицы Werner и повторена во всех строках единой сетки, которые покрывает исходный диапазон.",
        "price_is_minimum": bool(minimum), "pricing_mode": "fixed_shipment", "range_value_repeated": True,
        "tariff_range_low_kg": low, "tariff_range_high_kg": high, "source_file": selected.get("file"),
    }

def glavtrassa_official_tariff(origin: str, destination: str, weight: float, minimum: bool = False) -> dict[str, Any] | None:
    """Read the published Glavtrassa tariff table/file using the same layout parser.

    The Glavtrassa tariff page exposes downloadable service tables. Heavy rows
    must come from those printed ₽/kg columns; the calculator API total is never
    divided by weight.
    """
    if normalize_city(origin) not in {"Москва", "Санкт-Петербург"}:
        return None
    files = _source_files_for_ids({"GT001"})
    candidates: list[dict[str, Any]] = []
    for path in files:
        if _file_confirms_origin(path, origin):
            candidates.extend(_werner_candidates_from_file(path, destination))
    mode = "minimum" if minimum else ("fixed" if float(weight) <= 50 else "per_kg")
    selected = _werner_select_candidate(candidates, float(weight), mode)
    if not selected and not minimum and float(weight) >= 41:
        mode = "per_kg"
        selected = _werner_select_candidate(candidates, float(weight), mode)
    if not selected:
        return None
    value = float(selected["value"]); low = selected.get("low"); high = selected.get("high")
    header = str(selected.get("header") or "опубликованная ступень")
    base = {
        "company": "Главтрасса", "company_label": COMPANY_LABELS.get("Главтрасса", "Главтрасса"),
        "status": "ok", "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": "Официальный тарифный файл Главтрассы",
        "source_url": "https://glavtrassa.ru/clients/prices/",
        "freshness": "collected_official_file", "price_is_minimum": False,
        "tariff_range_low_kg": low, "tariff_range_high_kg": high, "range_value_repeated": True,
        "source_file": selected.get("file"), "source_sheet": selected.get("sheet"),
        "message": f"Значение прочитано непосредственно из опубликованной тарифной колонки Главтрассы ({header}) и повторено по покрываемым строкам единой сетки.",
    }
    if mode == "per_kg":
        base.update({"price": round(float(weight) * value, 2), "rate_per_kg": value, "pricing_mode": "per_kg", "formula": f"{float(weight):g} кг × {value:g} ₽/кг; колонка {header}"})
    else:
        base.update({"price": round(value, 2), "pricing_mode": "minimum" if minimum else "fixed_shipment", "formula": f"опубликованное значение {value:g} ₽; колонка {header}"})
    return base


# Проверенные ответы открытых онлайн-источников, зафиксированные 21.08.2026.
# Это не оценки: снимок используется только при точном совпадении маршрута и
# контрольной точки диапазона, если официальный онлайн-метод в данный момент
# недоступен. Живой ответ всегда имеет приоритет.
VERIFIED_PROFILE_SNAPSHOTS = {}  # v50: embedded prices disabled; use current collectors or user imports.

SNAPSHOT_SOURCE_META = {
    "Werner": ("Проверенный снимок публичного API Werner", "https://wernerus.ru/clients/prices/"),
    "ПЭК": ("Проверенный снимок открытого расчёта ПЭК", "https://pecom.ru/business/rates/trucking/moskva/"),
    "Возовоз": ("Проверенный снимок официального API Возовоза", "https://vozovoz.ru/tariffs/"),
    "Рейл Континент": ("Проверенный снимок публичного API Рейл Континента", "https://www.railcontinent.ru/services/prochie-gruzoperevozki/forshop/api-manual/"),
}

def verified_profile_snapshot(company: str, origin: str, destination: str, weight: float, volume: float) -> dict[str, Any] | None:
    origin = normalize_city(origin)
    destination = normalize_city(destination)
    rows = VERIFIED_PROFILE_SNAPSHOTS.get(company) or {}
    price = None
    reference_volume = None
    for (o, d, w, v), candidate in rows.items():
        if o != origin or d != destination or abs(float(weight) - w) >= 1e-6:
            continue
        # Historical control points remain exact evidence. They are returned here
        # only for their own weight; profile_matrix() may later use an exact point
        # as the upper boundary of a published weight tier when applying the
        # customer's range-copy rule. The original control volume stays visible.
        if abs(float(volume) - v) < 1e-6 or abs(float(volume)) < 1e-9:
            price = candidate
            reference_volume = v
            break
    if price is None:
        return None
    source_type, source_url = SNAPSHOT_SOURCE_META.get(company, ("Проверенный официальный снимок", ""))
    reference = f"{weight:g} кг / {reference_volume:g} м³" if reference_volume is not None else f"{weight:g} кг"
    slab = _werner_published_kg_slab(float(weight)) if company == "Werner" and float(weight) > 50 else None
    snapshot_date = "2026-09-05" if company == "Werner" and origin == "Санкт-Петербург" and destination == "Москва" else "2026-08-21"
    result = {
        "company": company,
        "company_label": COMPANY_LABELS.get(company, company),
        "status": "ok",
        "price": round(float(price)),
        "currency": "RUB",
        "delivery_days_min": None,
        "delivery_days_max": None,
        "source_type": source_type,
        "source_url": source_url,
        "freshness": f"official_snapshot_{snapshot_date}",
        "formula": f"зафиксированный официальный ответ для контрольной точки {reference}",
        "message": f"Проверенный ответ официального калькулятора от {snapshot_date} для контрольной точки {reference}. Это подтверждение полной стоимости конкретного расчёта, но не опубликованная ставка ₽/кг и не граница тарифной колонки.",
        "price_is_minimum": False,
        "reference_volume_m3": reference_volume,
        "allow_effective_rate": False,
        "calculator_total_only": True,
    }
    return result

# Активный список и порядок компаний зафиксированы по присланному заказчиком
# списку 25.08.2026. В интерфейсе, матрице и Excel используется один порядок.
# Внутренние ID сохранены совместимыми с уже подключёнными источниками.
COMPANIES = [
    "Werner", "ДЛ", "ПЭК", "КИТ", "Возовоз", "Мейджик", "Байкал Сервис",
    "Главтрасса", "Пролайн", "Фортуна", "ФастТранс", "Рейл Континент",
    "АТЭК", "Новая Линия", "БСК", "ЭкспедицияПлюс", "CTSgroup",
]
COMPANY_LABELS = {
    "Werner": "Werner",
    "ДЛ": "ДЛ",
    "ПЭК": "ПЭК",
    "КИТ": "КИТ",
    "Возовоз": "Возовоз",
    "Мейджик": "Мейджик",
    "Байкал Сервис": "Байкал Сервис",
    "Главтрасса": "Главтрасса",
    "Пролайн": "Пролайн",
    "Фортуна": "Фортуна",
    "ФастТранс": "ФастТранс",
    "Рейл Континент": "РейлКонтинент",
    "АТЭК": "АТЭК",
    "Новая Линия": "НоваяЛиния",
    "БСК": "БСК",
    "ЭкспедицияПлюс": "ЭкспедицияПлюс",
    "CTSgroup": "CTSGroup",

    # Неактивные/исторические ID оставлены только для совместимости старых
    # функций и сохранённых результатов; в COMPANIES они не входят.
    "Грузопоток": "Грузопоток",
    "СДЭК": "СДЭК",
    "DPD": "DPD",
    "Pony Express": "PONY EXPRESS",
}

SETTINGS_DEFAULTS = {
    "vozovoz_api_key": "VOZOVOZ_API_KEY",
    "kit_api_token": "KIT_API_TOKEN",
    "dellin_appkey": "DELLIN_APPKEY",
    "cdek_client_id": "CDEK_CLIENT_ID",
    "cdek_client_secret": "CDEK_CLIENT_SECRET",
    "dpd_client_number": "DPD_CLIENT_NUMBER",
    "dpd_client_key": "DPD_CLIENT_KEY",
    "baikal_api_key": "BAIKAL_API_KEY",
    "baikal_api_url": "BAIKAL_API_URL",
    "pony_api_key": "PONY_API_KEY",
    "pony_api_url": "PONY_API_URL",
}

INTEGRATION_DESCRIPTIONS = {
    "Werner": ("Официальные тарифные файлы", "Актуальные прайсы по городам и направлениям со страницы Werner"),
    "ДЛ": ("Официальные PDF-тарифы", "Полная сетка: минимум, фиксированная цена, ставки по кг и м³"),
    "ПЭК": ("Официальные XLS/XLSX-тарифы", "Файлы на перевозку из филиала и дополнительные услуги"),
    "КИТ": ("Официальные PDF-тарифы", "PDF-выгрузка тарифов по выбранному городу"),
    "Возовоз": ("Официальная Excel-выгрузка + API", "Онлайн-расчёт и опубликованные тарифные файлы; цена «от» не включается в рыночную статистику"),
    "Мейджик": ("Официальная тарифная страница", "Открытая таблица/калькулятор; цена появляется только при устойчивом машинном сопоставлении"),
    "Главтрасса": ("Официальный тарифный файл + API", "Весовые ставки читаются из опубликованного прайса; API используется только как точная полная стоимость отправки"),
    "Пролайн": ("Официальные маршрутные таблицы", "Для Москва ↔ Санкт-Петербург используются опубликованные ставки руб/кг и руб/м³"),
    "Фортуна": ("Официальный XLSX Фортуна Экспресс", "Актуальный официальный прайс-лист от 27.07.2026; Москва и Санкт-Петербург нормализованы в весовые диапазоны"),
    "Грузопоток": ("Официальный сайт", "Полная публичная маршрутная сетка в стабильном формате пока не подтверждена"),
    "ФастТранс": ("Официальная HTML-матрица", "Для Москва ↔ Санкт-Петербург используются опубликованные ставки по весу и объёму"),
    "АТЭК": ("Официальная тарифная страница", "Для Москва → Санкт-Петербург используется опубликованный межтерминальный тариф"),
    "Новая Линия": ("Официальные PDF-выгрузки", "Тарифы из Москва-Север распознаны в весовую сетку; другие PDF продолжают обновляться отдельно"),
    "БСК": ("Официальная таблица БСД", "В исходном Excel компания указана как БСК; тарифы берутся с 123789.ru"),
    "ЭкспедицияПлюс": ("Официальный XLSX межтерминальных тарифов", "187 направлений из 9 городов отправления; прайс действует с 30.07.2026"),
    "CTSgroup": ("Официальный XLSX CTS Group", "Из Москвы опубликованы ставки руб/кг и руб/м³; диапазоны с пометкой «договор» не рассчитываются"),
    "Байкал Сервис": ("Открытая тарифная таблица + API", "Без ключа — официальные ставки популярных направлений в опубликованных диапазонах; с ключом — REST API v2"),
    "СДЭК": ("Официальный PDF условий", "В PDF есть доплаты и ограничения, но нет полной маршрутной сетки"),
    "DPD": ("Официальные публичные тарифные примеры + API", "Без ключа используются только опубликованные DPD примеры при точном совпадении условий"),
    "Рейл Континент": ("Публичная тарифная методика + PDF", "Маршрутная цена из открытого источника, доплаты из официального PDF"),
    "Pony Express": ("Официальный PDF Стандарт+", "Маршрутная таблица PONY EXPRESS парсится из открытого PDF; отдельная топливная надбавка не угадывается"),
}

COMMON_PROFILES = [
    {"id": "w001", "label": "0–1 кг", "weight_kg": 1.0, "volume_m3": 0.0, "description": "0–1 кг", "range_weight": "0–1 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w003", "label": "1–3 кг", "weight_kg": 3.0, "volume_m3": 0.0, "description": "1–3 кг", "range_weight": "1–3 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w005", "label": "3–5 кг", "weight_kg": 5.0, "volume_m3": 0.0, "description": "3–5 кг", "range_weight": "3–5 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w010", "label": "5–10 кг", "weight_kg": 10.0, "volume_m3": 0.0, "description": "5–10 кг", "range_weight": "5–10 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w015", "label": "10–15 кг", "weight_kg": 15.0, "volume_m3": 0.0, "description": "10–15 кг", "range_weight": "10–15 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w020", "label": "15–20 кг", "weight_kg": 20.0, "volume_m3": 0.0, "description": "15–20 кг", "range_weight": "15–20 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w035", "label": "20–35 кг", "weight_kg": 35.0, "volume_m3": 0.0, "description": "20–35 кг", "range_weight": "20–35 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w040", "label": "35–40 кг", "weight_kg": 40.0, "volume_m3": 0.0, "description": "35–40 кг", "range_weight": "35–40 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w050", "label": "40–50 кг", "weight_kg": 50.0, "volume_m3": 0.0, "description": "40–50 кг", "range_weight": "40–50 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "min", "label": "МИН", "weight_kg": 1.0, "volume_m3": 0.0, "description": "МИН — минимальная стоимость отправки", "range_weight": "МИН", "range_volume": "", "unit": "₽", "is_minimum_profile": True, "comparison_mode": "minimum"},
    {"id": "w100", "label": "до 100 кг", "weight_kg": 100.0, "volume_m3": 0.0, "description": "до 100 кг", "range_weight": "до 100 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w200", "label": "до 200 кг", "weight_kg": 200.0, "volume_m3": 0.0, "description": "до 200 кг", "range_weight": "до 200 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w250", "label": "до 250 кг", "weight_kg": 250.0, "volume_m3": 0.0, "description": "до 250 кг", "range_weight": "до 250 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w300", "label": "до 300 кг", "weight_kg": 300.0, "volume_m3": 0.0, "description": "до 300 кг", "range_weight": "до 300 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w400", "label": "до 400 кг", "weight_kg": 400.0, "volume_m3": 0.0, "description": "до 400 кг", "range_weight": "до 400 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w500", "label": "до 500 кг", "weight_kg": 500.0, "volume_m3": 0.0, "description": "до 500 кг", "range_weight": "до 500 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w600", "label": "до 600 кг", "weight_kg": 600.0, "volume_m3": 0.0, "description": "до 600 кг", "range_weight": "до 600 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w700", "label": "до 700 кг", "weight_kg": 700.0, "volume_m3": 0.0, "description": "до 700 кг", "range_weight": "до 700 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w750", "label": "до 750 кг", "weight_kg": 750.0, "volume_m3": 0.0, "description": "до 750 кг", "range_weight": "до 750 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w800", "label": "до 800 кг", "weight_kg": 800.0, "volume_m3": 0.0, "description": "до 800 кг", "range_weight": "до 800 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w1000", "label": "до 1000 кг", "weight_kg": 1000.0, "volume_m3": 0.0, "description": "до 1000 кг", "range_weight": "до 1000 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w1200", "label": "до 1200 кг", "weight_kg": 1200.0, "volume_m3": 0.0, "description": "до 1200 кг", "range_weight": "до 1200 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w1500", "label": "до 1500 кг", "weight_kg": 1500.0, "volume_m3": 0.0, "description": "до 1500 кг", "range_weight": "до 1500 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w2000", "label": "до 2000 кг", "weight_kg": 2000.0, "volume_m3": 0.0, "description": "до 2000 кг", "range_weight": "до 2000 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w2500", "label": "до 2500 кг", "weight_kg": 2500.0, "volume_m3": 0.0, "description": "до 2500 кг", "range_weight": "до 2500 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w3000", "label": "до 3000 кг", "weight_kg": 3000.0, "volume_m3": 0.0, "description": "до 3000 кг", "range_weight": "до 3000 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w5000", "label": "до 5000 кг", "weight_kg": 5000.0, "volume_m3": 0.0, "description": "до 5000 кг", "range_weight": "до 5000 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w10000", "label": "до 10000 кг", "weight_kg": 10000.0, "volume_m3": 0.0, "description": "до 10000 кг", "range_weight": "до 10000 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
    {"id": "w20000", "label": "до 20000 кг", "weight_kg": 20000.0, "volume_m3": 0.0, "description": "до 20000 кг", "range_weight": "до 20000 кг", "range_volume": "", "unit": "₽", "comparison_mode": "shipment_total"},
]
# Customer-facing values use one unit everywhere: total shipment price in rubles.
# Source rates (RUB/kg, RUB/m3) are preserved in metadata/formulas, but the value
# shown in the comparison matrix is always the resulting shipment total in RUB.
for _profile in COMMON_PROFILES:
    if _profile.get("is_minimum_profile"):
        _profile["tariff_type"] = "Минимальная стоимость"
    else:
        _profile["tariff_type"] = "Стоимость отправки"
    _profile["unit"] = "₽"
    _profile["comparison_mode"] = "minimum" if _profile.get("is_minimum_profile") else "shipment_total"

PROFILE_BY_ID = {p["id"]: p for p in COMMON_PROFILES}

DEFAULT_DESTINATIONS = [
    "Санкт-Петербург", "Нижний Новгород", "Казань", "Самара", "Воронеж",
    "Ростов-на-Дону", "Краснодар", "Екатеринбург", "Новосибирск", "Челябинск",
]

KG_TIERS = [
    (5000, 10000), (3000, 4999), (2500, 2999), (2000, 2499),
    (1500, 1999), (1200, 1499), (1000, 1199), (800, 999),
    (500, 799), (300, 499), (200, 299), (150, 199), (100, 149), (36, 99),
]
M3_TIERS = [
    (25.00, 45.00), (15.00, 24.99), (12.50, 14.99), (10.00, 12.49),
    (7.00, 9.99), (5.80, 6.99), (5.00, 5.79), (4.00, 4.99),
    (2.30, 3.99), (1.30, 2.29), (0.80, 1.29), (0.60, 0.79),
    (0.40, 0.59), (0.11, 0.39),
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def matrix_control_volume(weight: float) -> float:
    """Canonical volume for calculator-only carriers in the customer matrix.

    The verified control points supplied with the project use 200 kg/m³
    (50 kg / 0.25 m³, 100 / 0.50, 300 / 1.50, ...).  When a carrier does not
    publish a pure weight table but does expose an exact official calculator,
    v23 uses the same density for every control weight instead of returning an
    artificial blank.  The value is kept in the source metadata.
    """
    return round(max(0.005, float(weight) / 200.0), 4)


def load_settings() -> dict[str, str]:
    defaults = {key: os.getenv(env_name, "") for key, env_name in SETTINGS_DEFAULTS.items()}
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            defaults.update({k: str(v or "") for k, v in stored.items() if k in defaults})
        except Exception:
            pass
    return defaults


def save_settings(payload: dict[str, Any]) -> dict[str, bool]:
    current = load_settings()
    for key in SETTINGS_DEFAULTS:
        if key in payload:
            current[key] = str(payload.get(key) or "").strip()
    SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: bool(v) for k, v in current.items()}


def parse_number(text: str) -> float | None:
    s = str(text or "").strip().replace("\xa0", " ")
    if not re.fullmatch(r"\d{1,3}(?: \d{3})*(?:[,.]\d+)?|\d+(?:[,.]\d+)?", s):
        return None
    try:
        return float(s.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def normalize_city(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    aliases = {
        "санкт петербург": "Санкт-Петербург",
        "санкт-петербург": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "нижний новгород": "Нижний Новгород",
        "ростов на дону": "Ростов-на-Дону",
        "ростов-на-дону": "Ростов-на-Дону",
        "москва": "Москва",
        # Исправления переносов строк в официальном PDF Деловых Линий.
        "амуре": "Комсомольск-на-Амуре",
        "камчатский": "Петропавловск-Камчатский",
        "кузнецкий": "Ленинск-Кузнецкий",
        "залесский": "Переславль-Залесский",
        "уральский": "Каменск-Уральский",
        "шахтинский": "Каменск-Шахтинский",
        "челны": "Набережные Челны",
    }
    key = text.lower().replace("ё", "е")
    from .cities import normalize_city as canonical_city
    return aliases.get(key, canonical_city(text))




def _file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def _json_cache_read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _json_cache_write(path: Path, data: Any) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass

def parse_dellin_tariffs(path: Path, origin: str) -> dict[str, dict[str, Any]]:
    from pypdf import PdfReader

    if not path.exists():
        return {}
    normalized_origin = normalize_city(origin)
    fingerprint = _file_sha1(path)
    cache_key = f"{path.resolve()}|{fingerprint}|{normalized_origin}"
    cached = _DELLIN_PARSED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    disk_cache = CACHE_DIR / f"dellin_{re.sub(r'[^a-zа-я0-9]+', '_', normalized_origin.lower())}_{fingerprint}.json"
    disk = _json_cache_read(disk_cache)
    if isinstance(disk, dict) and disk:
        _DELLIN_PARSED_CACHE.clear()
        _DELLIN_PARSED_CACHE[cache_key] = disk
        return disk
    reader = PdfReader(str(path))
    result: dict[str, dict[str, Any]] = {}
    skip_fragments = [
        "тарифы на межтерминальную", "направление", "фикс. стоимость",
        "стоимость, руб", "цены указаны", "все цены", "/9",
    ]

    def flush(city: str | None, values: list[float]) -> None:
        if not city or len(values) < 34:
            return
        vals = values[:34]
        destination = normalize_city(city)
        result[destination] = {
            "company": "ДЛ",
            "origin": normalize_city(origin),
            "destination": destination,
            "fixed": vals[:5],
            "minimum": vals[5],
            "kg_rates": vals[6:20],
            "m3_rates": vals[20:34],
            "source": path.name,
            "freshness": "cached" if "fallback" not in str(path).lower() else "fallback",
        }

    for page in reader.pages:
        text = page.extract_text() or ""
        low_page = text.lower()
        if "тарифы на доставку от адреса" in low_page or "тарифы на доставку от/до адреса" in low_page:
            break
        lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
        city: str | None = None
        values: list[float] = []
        for line in lines:
            number = parse_number(line)
            if number is not None:
                if city:
                    values.append(number)
                continue
            low = line.lower()
            if any(fragment in low for fragment in skip_fragments):
                continue
            if re.search(r"[А-Яа-яЁё]", line) and len(line) < 80:
                flush(city, values)
                city, values = line, []
        flush(city, values)
    _DELLIN_PARSED_CACHE.clear()
    _DELLIN_PARSED_CACHE[cache_key] = result
    if result:
        _json_cache_write(disk_cache, result)
    return result


def collected_source_file(source_id: str, suffixes: tuple[str, ...] = ()) -> Path | None:
    """Return the newest downloaded official file for a collector source id.

    The collector stores files in runtime/downloads with generated names. Earlier
    versions only looked for hard-coded cache filenames, so a successfully
    downloaded Saint-Petersburg Dellin PDF was visible in the source catalog but
    still ignored by the calculator. v20 resolves the actual collected file.
    """
    try:
        results = core.load_results()
    except Exception:
        return None
    candidates: list[Path] = []
    for row in results.get("files", []):
        if not isinstance(row, dict) or str(row.get("source_id") or "") != source_id:
            continue
        raw = str(row.get("path") or "").strip()
        path = Path(raw) if raw else core.DOWNLOAD_DIR / str(row.get("file") or "")
        if not path.is_absolute():
            path = BASE_DIR / path
        if path.exists() and path.is_file() and (not suffixes or path.suffix.lower() in suffixes):
            candidates.append(path)
    if not candidates:
        candidates.extend(_bundled_source_files({source_id}, set(suffixes) if suffixes else None))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.stat().st_mtime_ns)


def dellin_pdf_for_origin(origin: str) -> Path | None:
    origin = normalize_city(origin)
    if origin == "Москва":
        collected = collected_source_file("DL001", (".pdf",))
        candidates = [
            core.CACHE_DIR / "dellin_city_3.pdf",
            collected,
            DATA_DIR / "fallback" / "dellin_moscow.pdf",
        ]
    elif origin == "Санкт-Петербург":
        collected = collected_source_file("DL002", (".pdf",))
        candidates = [
            core.CACHE_DIR / "dellin_city_1.pdf",
            collected,
            DATA_DIR / "fallback" / "dellin_spb.pdf",
        ]
    else:
        return None
    return next((p for p in candidates if p is not None and p.exists()), None)


def select_tier(value: float, tiers: list[tuple[float, float]]) -> int | None:
    for idx, (low, high) in enumerate(tiers):
        if low <= value <= high:
            return idx
    return None


def calculate_dellin(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    path = dellin_pdf_for_origin(origin)
    if not path:
        return unavailable("ДЛ", "Нет тарифного PDF для выбранного города отправления")
    tariffs = parse_dellin_tariffs(path, origin)
    tariff = tariffs.get(normalize_city(destination))
    if not tariff:
        return unavailable("ДЛ", "Направление отсутствует в опубликованной тарифной таблице")

    # В опубликованной сетке Деловых Линий максимальная весовая ступень
    # заканчивается на 10 000 кг. Для 20 000 кг нельзя подставлять один
    # только минимум: это дало бы искусственно низкую ставку ₽/кг.
    if float(volume) <= 0 and float(weight) > max(high for _, high in KG_TIERS):
        return unavailable("ДЛ", "Вес выше максимального опубликованного диапазона 10 000 кг")

    price: float | None = None
    formula = ""
    if volume <= 0.1 and weight <= 35:
        if weight <= 1:
            idx = 0
        elif weight <= 3:
            idx = 1
        elif weight <= 5:
            idx = 2
        elif weight <= 15:
            idx = 3
        else:
            idx = 4
        price = tariff["fixed"][idx]
        formula = "фиксированный тариф для малогабаритного груза"
    else:
        kg_idx = select_tier(weight, KG_TIERS)
        m3_idx = select_tier(volume, M3_TIERS)
        if kg_idx is None and weight < 36:
            kg_idx = len(KG_TIERS) - 1
        if m3_idx is None and volume < 0.11:
            m3_idx = len(M3_TIERS) - 1
        candidates = [float(tariff["minimum"])]
        parts = [f"минимум {tariff['minimum']:.0f} ₽"]
        if kg_idx is not None:
            kg_cost = weight * float(tariff["kg_rates"][kg_idx])
            candidates.append(kg_cost)
            parts.append(f"{weight:g} кг × {tariff['kg_rates'][kg_idx]:g} ₽/кг")
        if m3_idx is not None:
            m3_cost = volume * float(tariff["m3_rates"][m3_idx])
            candidates.append(m3_cost)
            parts.append(f"{volume:g} м³ × {tariff['m3_rates'][m3_idx]:g} ₽/м³")
        price = max(candidates) if candidates else None
        formula = "максимум из: " + "; ".join(parts)

    if price is None:
        return unavailable("ДЛ", "Профиль выходит за опубликованные диапазоны")
    return {
        "company": "ДЛ",
        "company_label": COMPANY_LABELS["ДЛ"],
        "status": "ok",
        "price": round(price),
        "currency": "RUB",
        "delivery_days_min": None,
        "delivery_days_max": None,
        "source_type": "Официальный PDF",
        "source_url": "https://www.dellin.ru/pricelist_pdf/",
        "freshness": tariff.get("freshness", "cached"),
        "formula": formula,
        "message": "Расчёт по опубликованной межтерминальной тарифной сетке",
    }


def unavailable(company: str, message: str, status: str = "unavailable") -> dict[str, Any]:
    return {
        "company": company,
        "company_label": COMPANY_LABELS.get(company, company),
        "status": status,
        "price": None,
        "currency": "RUB",
        "delivery_days_min": None,
        "delivery_days_max": None,
        "source_type": "",
        "source_url": "",
        "freshness": "",
        "formula": "",
        "message": message,
    }


def open_source_result(company: str, partial: dict[str, Any] | None, prefix: str = "") -> dict[str, Any] | None:
    """Приводит результат открытого прайса/PDF/HTML к единому формату приложения."""
    if not isinstance(partial, dict):
        return None
    result = unavailable(company, partial.get("message") or "Открытый источник не содержит подходящего тарифа", partial.get("status") or "document_unavailable")
    result.update(partial)
    result["company"] = company
    result["company_label"] = COMPANY_LABELS.get(company, company)
    result.setdefault("currency", "RUB")
    result.setdefault("delivery_days_min", None)
    result.setdefault("delivery_days_max", None)
    if prefix:
        result["message"] = prefix + cleanTextForBackend(result.get("message") or "")
    return result


PUBLIC_SOURCE_URLS = {
    "Werner": "https://wernerus.ru/clients/prices/",
    "ДЛ": "https://www.dellin.ru/documents/",
    "ПЭК": "https://pecom.ru/business/rates/trucking/moskva/",
    "КИТ": "https://tk-kit.ru/rates-new",
    "Возовоз": "https://vozovoz.ru/tariffs/",
    "Мейджик": "https://magic-trans.ru/tarify/",
    "Главтрасса": "https://glavtrassa.ru/clients/prices/",
    "Пролайн": "https://proline.su/dostavka/msk-spb/",
    "Фортуна": "https://fte.ru/price/",
    "Грузопоток": "https://gruzopotok.com/",
    "ФастТранс": "https://fastrans.ru/page/price/",
    "АТЭК": "https://atec-logistic.ru/tariffs/",
    "Новая Линия": "https://tknl.ru/price/dev.php",
    "БСК": "https://123789.ru/terminals-addresses/moskva",
    "ЭкспедицияПлюс": "https://nevatk.ru/tarif/",
    "CTSgroup": "https://cts-group.ru/prices",
    "Байкал Сервис": "https://www.baikalsr.ru/business/prices/",
    "СДЭК": "https://www.cdek.ru/storage/source/docs/cdek_services.pdf",
    "DPD": "https://dpd.ru/calc",
    "Рейл Континент": "https://www.railcontinent.ru/",
    "Pony Express": "https://www.ponyexpress.ru/support/servisy-samoobsluzhivaniya/tariff/",
}


def flatten_city_catalog(data: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and re.search(r"[А-Яа-яЁёA-Za-z]", value):
                found.append((str(key), normalize_city(value)))
            else:
                found.extend(flatten_city_catalog(value))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("city") or item.get("title") or item.get("location")
                ident = item.get("id") or item.get("code") or item.get("value")
                if name is not None and ident is not None:
                    found.append((str(ident), normalize_city(str(name))))
                found.extend(flatten_city_catalog(item))
    return found


def best_city_id(catalog: list[tuple[str, str]], city: str) -> str | None:
    target = normalize_city(city).lower().replace("ё", "е")
    exact = [ident for ident, name in catalog if name.lower().replace("ё", "е") == target]
    if exact:
        return exact[0]
    partial = [ident for ident, name in catalog if target in name.lower().replace("ё", "е")]
    return partial[0] if partial else None


def cached_json(name: str, url: str, params: dict[str, Any] | None = None, ttl_hours: int = 24) -> Any:
    path = CACHE_DIR / name
    if path.exists() and (datetime.now().timestamp() - path.stat().st_mtime) < ttl_hours * 3600:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    response = HTTP.get(url, params=params, timeout=(3, 8))
    response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        data = json.loads(response.content.decode("utf-8-sig", errors="replace"))
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def recursive_price(data: Any) -> float | None:
    preferred = ["price", "total", "cost", "sum", "amount"]
    if isinstance(data, dict):
        for key in preferred:
            value = data.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
            if isinstance(value, str):
                n = parse_number(value)
                if n and n > 0:
                    return n
        for value in data.values():
            found = recursive_price(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = recursive_price(value)
            if found is not None:
                return found
    return None


def calculate_werner(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    try:
        # Public Werner API documentation gives stable IDs for the two capital
        # cities (Москва=35, Санкт-Петербург=36).  Using them directly avoids an
        # unnecessary city-directory request on the most important route.
        known_ids = {"Москва": "35", "Санкт-Петербург": "36"}
        dep = known_ids.get(normalize_city(origin))
        arr = known_ids.get(normalize_city(destination))
        if not dep or not arr:
            cities = cached_json(
                "werner_cities.json",
                "https://wernerus.ru/api/calc/",
                {"method": "api_city", "responseFormat": "json"},
            )
            catalog = flatten_city_catalog(cities)
            dep = dep or best_city_id(catalog, origin)
            arr = arr or best_city_id(catalog, destination)
        if not dep or not arr:
            return unavailable("Werner", "Город не найден в справочнике Werner")
        side = max(0.01, volume) ** (1 / 3)
        params = [
            ("method", "api_calc"), ("responseFormat", "json"),
            ("depPoint", dep), ("arrPoint", arr),
            ("cargoMest[1]", "1"), ("cargoKg[1]", f"{weight:g}"),
            ("cargoL[1]", f"{side:.4f}"), ("cargoW[1]", f"{side:.4f}"),
            ("cargoH[1]", f"{side:.4f}"), ("cargoCalculation[1]", "0"),
        ]
        data = None
        errors = []
        for endpoint in ("https://wernerus.ru/api/calc/", "http://wernerus.ru/api/calc/"):
            for attempt in range(3):
                try:
                    response = HTTP.get(
                        endpoint, params=params, timeout=(5, 18),
                        headers={
                            "Accept": "application/json,text/plain,*/*",
                            "Referer": "https://wernerus.ru/clients/prices/",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                        },
                        allow_redirects=True,
                    )
                    response.raise_for_status()
                    data = response.json()
                    break
                except Exception as api_exc:
                    errors.append(f"{endpoint} попытка {attempt + 1}: {api_exc}")
                    if attempt < 2:
                        time.sleep(0.35 * (attempt + 1))
            if data is not None:
                break
        if data is None:
            raise RuntimeError(" | ".join(errors[-4:]) or "API Werner не ответил")
        price = recursive_price(data)
        if price is None:
            return unavailable("Werner", "API вернул ответ без стоимости")
        return {
            "company": "Werner", "company_label": COMPANY_LABELS["Werner"], "status": "ok",
            "price": round(price), "currency": "RUB", "delivery_days_min": None,
            "delivery_days_max": None, "source_type": "Публичный API", "source_url": "https://wernerus.ru/api/calc/",
            "freshness": "live", "formula": "онлайн-калькулятор по весу и объёму",
            "message": "Актуальный расчёт через публичный API Werner",
            "calculator_total_only": True,
            "control_volume_m3": float(volume),
        }
    except Exception as exc:
        return unavailable("Werner", f"Не удалось получить онлайн-расчёт: {exc}", "error")


def sum_service_array(value: Any) -> float:
    total = 0.0
    if isinstance(value, list):
        if len(value) >= 3 and isinstance(value[2], (int, float)):
            return float(value[2])
        for item in value:
            if isinstance(item, list) and len(item) >= 3:
                try:
                    total += float(item[2])
                except Exception:
                    pass
    return total


def calculate_pek(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    api_error = ""
    try:
        towns = cached_json("pek_towns.json", "https://pecom.ru/ru/calc/towns.php")
        catalog = flatten_city_catalog(towns)
        dep = best_city_id(catalog, origin)
        arr = best_city_id(catalog, destination)
        if not dep or not arr:
            api_error = "город не найден в публичном справочнике ПЭК"
        else:
            side = max(0.01, volume) ** (1 / 3)
            params = [
                ("places[0][]", f"{side:.4f}"), ("places[0][]", f"{side:.4f}"),
                ("places[0][]", f"{side:.4f}"), ("places[0][]", f"{volume:g}"),
                ("places[0][]", f"{weight:g}"), ("places[0][]", "0"), ("places[0][]", "0"),
                ("take[town]", dep), ("deliver[town]", arr),
            ]
            response = HTTP.get(
                "https://calc.pecom.ru/bitrix/components/pecom/calc/ajax.php",
                params=params, timeout=(3, 8),
            )
            response.raise_for_status()
            data = response.json()
            price = sum_service_array(data.get("auto")) or recursive_price(data.get("auto"))
            if price:
                return {
                    "company": "ПЭК", "company_label": COMPANY_LABELS["ПЭК"], "status": "ok",
                    "price": round(price), "currency": "RUB", "delivery_days_min": None,
                    "delivery_days_max": None, "source_type": "Публичный API", "source_url": "https://pecom.ru/business/developers/api_public/",
                    "freshness": "live", "formula": "сумма услуг автоперевозки из ответа API",
                    "message": str(data.get("periods") or "Актуальный расчёт через публичный API ПЭК"),
                    "calculator_total_only": True,
                    "control_volume_m3": float(volume),
                }
            api_error = "API не вернул автотариф" + (f": {data.get('error')}" if data.get("error") else "")
    except Exception as exc:
        api_error = str(exc)

    public = open_source_result("ПЭК", pek_public_route_page(origin, destination, weight, volume))
    if public:
        if api_error:
            public["message"] = f"Публичный API недоступен ({cleanTextForBackend(api_error)}). " + public["message"]
        return public
    return unavailable("ПЭК", "Открытые источники ПЭК не дали сопоставимый тариф" + (f": {cleanTextForBackend(api_error)}" if api_error else ""), "error" if api_error else "document_unavailable")


def city_slug(value: str) -> str:
    table = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
        "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
        "х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    text = normalize_city(value).lower()
    slug = "".join(table.get(ch, ch if ch.isalnum() else "-") for ch in text)
    return re.sub(r"-+", "-", slug).strip("-")


def cached_text(name: str, url: str, ttl_hours: int = 12, timeout: tuple[int, int] = (3, 8)) -> tuple[str, str]:
    path = CACHE_DIR / name
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600:
        try:
            return path.read_text(encoding="utf-8"), "cached"
        except Exception:
            pass
    response = HTTP.get(url, timeout=timeout, headers={"Accept": "text/html,application/xhtml+xml"})
    response.raise_for_status()
    text = response.text
    path.write_text(text, encoding="utf-8")
    return text, "live"


def parse_public_money(value: str) -> float | None:
    try:
        return float(re.sub(r"[^0-9,.]", "", value).replace(",", "."))
    except Exception:
        return None


def kit_public_rates(origin: str, destination: str) -> tuple[float | None, float | None, str, str]:
    origin = normalize_city(origin)
    destination = normalize_city(destination)
    minimum = None
    rate_per_kg = None
    freshness = "live"
    used_urls: list[str] = []

    pair_url = f"https://tk-kit.ru/route/{city_slug(origin)}-{city_slug(destination)}"
    destination_url = f"https://tk-kit.ru/route/{city_slug(destination)}"

    try:
        html, state = cached_text(f"kit_pair_{city_slug(origin)}_{city_slug(destination)}.html", pair_url)
        freshness = state
        used_urls.append(pair_url)
        text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))
        patterns = [
            rf"{re.escape(origin)}\s*[-—]\s*{re.escape(destination)}[^0-9]{{0,50}}от\s*([0-9 \.,]+)\s*руб",
            rf"Из города\s+{re.escape(origin)}.*?{re.escape(destination)}[^0-9]{{0,50}}от\s*([0-9 \.,]+)\s*руб",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                minimum = parse_public_money(m.group(1))
                if minimum:
                    break
    except Exception:
        pass

    try:
        html, state = cached_text(f"kit_city_{city_slug(destination)}.html", destination_url)
        if freshness != "live":
            freshness = state
        used_urls.append(destination_url)
        text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))
        patterns = [
            rf"В город\s+{re.escape(destination)}.*?{re.escape(origin)}[^0-9]{{0,45}}от\s*([0-9]+(?:[,.][0-9]+)?)\s*руб",
            rf"{re.escape(origin)}[^0-9]{{0,45}}от\s*([0-9]+(?:[,.][0-9]+)?)\s*руб",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                candidate = parse_public_money(m.group(1))
                # Малые значения на городских страницах КИТ опубликованы как ставка за кг.
                if candidate and candidate < 200:
                    rate_per_kg = candidate
                    break
    except Exception:
        pass

    fallback = KIT_PUBLIC_FALLBACK.get((origin, destination))
    if fallback:
        if minimum is None:
            minimum = fallback["minimum"]
            freshness = "published_fallback"
        if rate_per_kg is None:
            rate_per_kg = fallback["rate_per_kg"]
            freshness = "published_fallback"

    return minimum, rate_per_kg, freshness, (used_urls[-1] if used_urls else pair_url)


def load_vozovoz_exact_cache() -> dict[str, Any]:
    try:
        if VOZOVOZ_EXACT_CACHE_PATH.exists():
            data = json.loads(VOZOVOZ_EXACT_CACHE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_vozovoz_exact_cache(cache: dict[str, Any]) -> None:
    try:
        VOZOVOZ_EXACT_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def vozovoz_cache_key(origin: str, destination: str, weight: float, volume: float) -> str:
    return f"{normalize_city(origin)}|{normalize_city(destination)}|{float(weight):.3f}|{float(volume):.4f}"


def vozovoz_public_minimum(origin: str, destination: str) -> tuple[float | None, str, str]:
    origin = normalize_city(origin)
    destination = normalize_city(destination)
    route_url = f"https://vozovoz.ru/order/create/{city_slug(origin)}_{city_slug(destination)}/"
    cache_name = f"vozovoz_route_{city_slug(origin)}_{city_slug(destination)}.html"
    backoff_key = f"vozovoz_public:{origin}:{destination}"
    blocked_until = float((_FAILURE_BACKOFF.get(backoff_key) or {}).get("until") or 0)
    try:
        if blocked_until > time.time():
            raise RuntimeError("public route page is temporarily in backoff")
        html, freshness = cached_text(cache_name, route_url, ttl_hours=12, timeout=(3, 6))
        _FAILURE_BACKOFF.pop(backoff_key, None)
        text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))
        # Важно соблюдать приоритет. На странице есть блоки популярных маршрутов,
        # поэтому выбор максимального числа мог подхватить чужую цену.
        patterns = [
            r'Цена за услугу\s*[«"]?Перевозка между городами[»"]?\s*на маршруте[^0-9]{0,120}от\s*([0-9\s.,]+)\s*[₽Рр]',
            r'Оптимальные тарифы[^0-9]{0,80}стоимость\s*[—-]\s*от\s*([0-9\s.,]+)\s*[₽Рр]',
            r'Стоимость перевозки[^0-9]{0,100}от\s*([0-9\s.,]+)\s*[₽Рр]',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                value = parse_public_money(match.group(1))
                if value and 100 <= value <= 1_000_000:
                    return value, freshness, route_url
    except Exception as exc:
        if "backoff" not in str(exc).lower():
            _FAILURE_BACKOFF[backoff_key] = {"until": time.time() + 15 * 60, "error": str(exc)}

    fallback = VOZOVOZ_PUBLIC_FALLBACK.get((origin, destination))
    if fallback is not None:
        return float(fallback), "published_fallback", "https://vozovoz.ru/tariffs/"
    return None, "unavailable", route_url


def _curl_json_post(url: str, payload: dict[str, Any], timeout: int = 18) -> dict[str, Any]:
    """POST JSON through the OS curl stack when Python/OpenSSL is reset by a carrier CDN."""
    exe = shutil.which("curl.exe") or shutil.which("curl")
    if not exe:
        raise RuntimeError("system curl is unavailable")
    proc = subprocess.run(
        [exe, "-sS", "-L", "--fail-with-body", "--connect-timeout", "6", "--max-time", str(int(timeout)),
         "-H", "Accept: application/json", "-H", "Content-Type: application/json", "--data-binary", json.dumps(payload, ensure_ascii=False), url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(10, int(timeout) + 5), check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or f"curl exit {proc.returncode}")
    text = proc.stdout.decode("utf-8-sig", errors="replace")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError("curl returned non-object JSON")
    return data


def vozovoz_api_result(
    api_root: str,
    token: str,
    payload: dict[str, Any],
    timeout: tuple[int, int] = (3, 6),
) -> tuple[dict[str, Any] | None, str | None]:
    endpoint = api_root.rstrip("/") + "/"
    errors: list[str] = []
    data: dict[str, Any] | None = None
    try:
        response = HTTP.post(
            endpoint,
            params={"token": token},
            json=payload,
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except Exception as exc:
        errors.append(f"requests: {exc}")

    # The user's Windows log shows the same SSL EOF pattern on several carrier
    # hosts.  Unlike the GET/download path, Vozovoz POST previously had no OS-TLS
    # fallback at all, so a valid route silently degraded to the public minimum.
    if data is None:
        try:
            joiner = "&" if "?" in endpoint else "?"
            data = _curl_json_post(f"{endpoint}{joiner}token={token}", payload, timeout=max(timeout))
        except Exception as exc:
            errors.append(f"curl: {exc}")

    if data is None:
        return None, "; ".join(errors) or "API did not answer"
    node = data.get("response") or data.get("result") or {}
    price = (node.get("price") or node.get("basePrice")) if isinstance(node, dict) else None
    if isinstance(price, str):
        price = parse_public_money(price)
    if isinstance(price, (int, float)) and float(price) > 0:
        if "response" not in data:
            data = {**data, "response": node}
        return data, None
    error = data.get("error") or data.get("message") or (node.get("error") if isinstance(node, dict) else None)
    return None, cleanTextForBackend(error or "Ответ без стоимости")


def calculate_vozovoz(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    origin = normalize_city(origin)
    destination = normalize_city(destination)
    configured_token = load_settings().get("vozovoz_api_key", "")
    token = configured_token or VOZOVOZ_DEMO_TOKEN
    api_roots = (["https://vozovoz.ru/api/", "https://vozovoz.org/api/"] if configured_token
                 else ["https://vozovoz.org/api/", "https://vozovoz.ru/api/"])
    payload = {
        "object": "price",
        "action": "get",
        "params": {
            "cargo": {"dimension": {"quantity": 1, "volume": float(volume), "weight": float(weight)}},
            "gateway": {
                "dispatch": {"point": {"location": origin, "terminal": "default"}},
                "destination": {"point": {"location": destination, "terminal": "default"}},
            },
        },
    }

    data = None
    errors: list[str] = []
    active_roots: list[str] = []
    for api_root in api_roots:
        backoff_key = f"vozovoz:{api_root}"
        blocked_until = float((_FAILURE_BACKOFF.get(backoff_key) or {}).get("until") or 0)
        if blocked_until > time.time():
            errors.append(f"{api_root}: {str((_FAILURE_BACKOFF.get(backoff_key) or {}).get('error') or 'API временно пропущен')}")
        else:
            active_roots.append(api_root)
    if active_roots:
        with ThreadPoolExecutor(max_workers=len(active_roots)) as pool:
            futures = {pool.submit(vozovoz_api_result, root, token, payload): root for root in active_roots}
            for future in as_completed(futures):
                root = futures[future]
                candidate, root_error = future.result()
                backoff_key = f"vozovoz:{root}"
                if candidate and data is None:
                    data = candidate
                    _FAILURE_BACKOFF.pop(backoff_key, None)
                elif not candidate:
                    errors.append(f"{root}: {root_error or 'нет ответа'}")
                    _FAILURE_BACKOFF[backoff_key] = {"until": time.time() + 15 * 60, "error": root_error or "нет ответа"}
    api_error = "; ".join(errors)

    if data:
        node = data.get("response") or {}
        price = float(node.get("price") or node.get("basePrice"))
        delivery = node.get("deliveryTime") or {}
        is_demo = not bool(configured_token)
        result = {
            "company": "Возовоз",
            "company_label": COMPANY_LABELS["Возовоз"],
            "status": "ok",
            "price": round(price),
            "currency": "RUB",
            "delivery_days_min": delivery.get("from"),
            "delivery_days_max": delivery.get("to"),
            "source_type": "Официальный демо API" if is_demo else "Официальный API",
            "source_url": "https://vozovoz.ru/dev/api/",
            "freshness": "demo_live" if is_demo else "live",
            "formula": "итоговая стоимость из объекта price для заданных веса, объёма и маршрута",
            "message": "Точный расчёт через официальный API Возовоза",
            "price_is_minimum": False,
            "calculator_total_only": True,
            "control_volume_m3": float(volume),
        }
        cache = load_vozovoz_exact_cache()
        cache[vozovoz_cache_key(origin, destination, weight, volume)] = {**result, "saved_at": now_iso()}
        save_vozovoz_exact_cache(cache)
        return result

    cache = load_vozovoz_exact_cache()
    cached = cache.get(vozovoz_cache_key(origin, destination, weight, volume))
    if isinstance(cached, dict) and isinstance(cached.get("price"), (int, float)):
        return {
            **cached,
            "status": "cached",
            "freshness": "last_successful_api",
            "source_type": "Последний успешный API-расчёт",
            "message": "API временно недоступен; показан последний точный расчёт Возовоза",
            "price_is_minimum": False,
        }

    minimum, freshness, source_url = vozovoz_public_minimum(origin, destination)
    if minimum is not None:
        return {
            "company": "Возовоз",
            "company_label": COMPANY_LABELS["Возовоз"],
            "status": "ok",
            "price": round(float(minimum)),
            "currency": "RUB",
            "delivery_days_min": None,
            "delivery_days_max": None,
            "source_type": "Официальная публичная цена «от»",
            "source_url": source_url,
            "freshness": freshness,
            "formula": "нижняя граница стоимости с официальной страницы маршрута; точная цена зависит от груза",
            "message": "API временно недоступен. Используется опубликованная Возовозом минимальная цена маршрута.",
            "price_is_minimum": True,
            "api_error": api_error,
        }

    return unavailable(
        "Возовоз",
        f"Не удалось получить API-расчёт и публичную цену маршрута: {api_error or 'нет ответа'}",
        "error",
    )


def cleanTextForBackend(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return re.sub(r"\s+", " ", str(value or "")).strip()


def calculate_kit(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    try:
        minimum, rate_per_kg, freshness, source_url = kit_public_rates(origin, destination)
        if rate_per_kg is None:
            return unavailable("КИТ", "На открытой маршрутной странице не найдена сопоставимая ставка за кг", "unavailable")
        # Для объёмных грузов используем прозрачный эквивалент 250 кг/м³.
        # Это нормализационное правило приложения, а не скрытая ставка перевозчика.
        volumetric_weight = max(0.0, float(volume)) * 250.0
        chargeable_weight = max(float(weight), volumetric_weight)
        variable_cost = chargeable_weight * float(rate_per_kg)
        price = max(float(minimum or 0), variable_cost)
        formula_parts = [f"max({weight:g} кг, {volume:g} м³ × 250 кг/м³) = {chargeable_weight:g} кг", f"{chargeable_weight:g} × {rate_per_kg:g} ₽/кг"]
        if minimum:
            formula_parts.append(f"минимум {minimum:g} ₽")
        return {
            "company": "КИТ", "company_label": COMPANY_LABELS["КИТ"], "status": "ok",
            "price": round(price), "currency": "RUB",
            "delivery_days_min": None, "delivery_days_max": None,
            "source_type": "Открытые тарифы КИТ", "source_url": source_url,
            "freshness": freshness,
            "formula": "; ".join(formula_parts),
            "message": "Расчёт по опубликованной ставке официальной маршрутной страницы КИТ; персональные скидки не учитываются",
        }
    except Exception as exc:
        return unavailable("КИТ", f"Не удалось прочитать открытые тарифы КИТ: {exc}", "error")


def cargo_side_m(volume: float) -> float:
    return max(0.001, float(volume)) ** (1 / 3)


def positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and float(value) > 0:
        return float(value)
    if isinstance(value, str):
        number = parse_public_money(value)
        return number if number and number > 0 else None
    return None


def collect_named_numbers(data: Any, names: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    wanted = {name.lower() for name in names}
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in wanted:
                number = positive_number(value)
                if number is not None:
                    values.append(number)
            values.extend(collect_named_numbers(value, names))
    elif isinstance(data, list):
        for item in data:
            values.extend(collect_named_numbers(item, names))
    return values


def calculate_rail_continent(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    endpoints = [
        "http://railcontinent.ru/ajax/api.php",
        "https://railcontinent.ru/ajax/api.php",
        "https://www.railcontinent.ru/ajax/api.php",
    ]
    side = cargo_side_m(volume)
    errors: list[str] = []
    for endpoint in endpoints:
        try:
            response = HTTP.get(
                endpoint,
                params={
                    "city_from": normalize_city(origin), "city_to": normalize_city(destination),
                    "weight": f"{float(weight):g}", "volume": f"{float(volume):g}",
                    "length": f"{side:.4f}", "width": f"{side:.4f}", "height": f"{side:.4f}",
                    "mode": "auto",
                },
                timeout=(5, 15),
                headers={"Accept": "application/json,text/plain,*/*"},
                allow_redirects=True,
            )
            response.raise_for_status()
            data = response.json()
            if str(data.get("result", "")).lower() not in {"success", "ok", "1", "true"}:
                errors.append(cleanTextForBackend(data.get("message") or data.get("error") or "API не выполнил расчёт"))
                continue
            services = data.get("data") or {}
            options = []
            if isinstance(services, dict):
                for code, service in services.items():
                    if not isinstance(service, dict):
                        continue
                    price = positive_number(service.get("priceTotal") or service.get("price") or service.get("cost"))
                    if price:
                        options.append((price, code, service))
            if not options:
                errors.append(f"{endpoint}: нет доступного тарифа")
                continue
            price, code, service = min(options, key=lambda item: item[0])
            duration = service.get("duration")
            return {
                "company": "Рейл Континент", "company_label": COMPANY_LABELS["Рейл Континент"], "status": "ok",
                "price": round(price), "currency": "RUB", "delivery_days_min": duration, "delivery_days_max": duration,
                "source_type": "Официальный публичный API", "source_url": endpoint,
                "freshness": "live", "formula": f"минимальный доступный режим из ответа API ({code})",
                "message": cleanTextForBackend(service.get("type") or "Онлайн-расчёт Рейл Континент"),
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    return unavailable("Рейл Континент", "Публичный API недоступен: " + " | ".join(errors[-3:]), "error")


def public_web_result(company: str, origin: str, destination: str, weight: float, volume: float, api_error: str = "") -> dict[str, Any]:
    result = calculate_from_public_site(company, normalize_city(origin), normalize_city(destination), weight, volume)
    if not result.get("ok"):
        prefix = f"API: {api_error}. " if api_error else ""
        return unavailable(
            company,
            prefix + "Открытый калькулятор не отдал цену: " + cleanTextForBackend(result.get("message") or "неизвестная ошибка"),
            "error",
        )
    price = positive_number(result.get("price"))
    if price is None:
        return unavailable(company, "Открытый калькулятор вернул пустую стоимость", "error")
    return {
        "company": company,
        "company_label": COMPANY_LABELS[company],
        "status": "ok",
        "price": round(price),
        "currency": "RUB",
        "delivery_days_min": None,
        "delivery_days_max": None,
        "source_type": "Официальный открытый онлайн-калькулятор",
        "source_url": result.get("source_url") or PUBLIC_SOURCE_URLS.get(company, ""),
        "freshness": "live_web",
        "formula": "стоимость считана из результата публичного калькулятора в установленном Edge/Chrome",
        "message": cleanTextForBackend(result.get("message") or "Онлайн-расчёт с официального сайта"),
        "price_is_minimum": bool(result.get("price_is_minimum")),
        "browser": result.get("browser"),
    }


def cdek_access_token(settings: dict[str, str]) -> str:
    cache_path = CACHE_DIR / "cdek_oauth.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("access_token") and float(cached.get("expires_at") or 0) > time.time() + 60:
                return str(cached["access_token"])
        except Exception:
            pass
    response = HTTP.post(
        "https://api.cdek.ru/v2/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings["cdek_client_id"],
            "client_secret": settings["cdek_client_secret"],
        },
        timeout=(5, 15),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError(cleanTextForBackend(data.get("error_description") or data.get("error") or "СДЭК не вернул access_token"))
    cache_path.write_text(json.dumps({
        "access_token": token,
        "expires_at": time.time() + max(300, int(data.get("expires_in") or 3600)),
    }, ensure_ascii=False), encoding="utf-8")
    return token


def calculate_cdek(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    settings = load_settings()
    api_error = ""
    if settings.get("cdek_client_id") and settings.get("cdek_client_secret"):
        try:
            token = cdek_access_token(settings)
            side_cm = max(1, round(cargo_side_m(volume) * 100))
            payload = {
                "from_location": {"city": normalize_city(origin), "country_code": "RU"},
                "to_location": {"city": normalize_city(destination), "country_code": "RU"},
                "packages": [{
                    "weight": max(1, round(float(weight) * 1000)),
                    "length": side_cm, "width": side_cm, "height": side_cm,
                }],
            }
            response = HTTP.post(
                "https://api.cdek.ru/v2/calculator/tarifflist",
                json=payload, timeout=(5, 18),
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            tariffs = data.get("tariff_codes") or data.get("tariffs") or []
            options = []
            for tariff in tariffs if isinstance(tariffs, list) else []:
                price = positive_number(tariff.get("delivery_sum") or tariff.get("total_sum") or tariff.get("price")) if isinstance(tariff, dict) else None
                if price:
                    options.append((price, tariff))
            if options:
                price, tariff = min(options, key=lambda item: item[0])
                return {
                    "company": "СДЭК", "company_label": COMPANY_LABELS["СДЭК"], "status": "ok",
                    "price": round(price), "currency": str(tariff.get("currency") or "RUB"),
                    "delivery_days_min": tariff.get("period_min"), "delivery_days_max": tariff.get("period_max"),
                    "source_type": "Официальный API v2", "source_url": "https://apidoc.cdek.ru/",
                    "freshness": "live", "formula": "минимальный доступный тариф из calculator/tarifflist",
                    "message": cleanTextForBackend(tariff.get("tariff_name") or f"Тариф {tariff.get('tariff_code', '')}"),
                }
            api_error = cleanTextForBackend(data.get("errors") or data.get("message") or "API не вернул тариф")
        except Exception as exc:
            api_error = str(exc)
    if api_error:
        result = public_document_result("СДЭК", COMPANY_LABELS["СДЭК"], origin, destination, weight, volume)
        result["message"] = f"API недоступен: {cleanTextForBackend(api_error)}. " + result["message"]
        return result
    return public_document_result("СДЭК", COMPANY_LABELS["СДЭК"], origin, destination, weight, volume)


def calculate_dpd(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    settings = load_settings()
    api_error = ""
    if settings.get("dpd_client_number") and settings.get("dpd_client_key"):
        try:
            from zeep import Client
            from zeep.helpers import serialize_object
            from zeep.transports import Transport

            transport = Transport(session=HTTP, timeout=18, operation_timeout=18)
            client = Client("https://ws.dpd.ru/services/calculator2?wsdl", transport=transport)
            request = {
                "auth": {"clientNumber": settings["dpd_client_number"], "clientKey": settings["dpd_client_key"]},
                "pickup": {"cityName": normalize_city(origin), "countryCode": "RU"},
                "delivery": {"cityName": normalize_city(destination), "countryCode": "RU"},
                "selfPickup": True, "selfDelivery": True,
                "weight": float(weight), "volume": float(volume),
            }
            raw = client.service.getServiceCost2(request)
            data = serialize_object(raw)
            rows = data if isinstance(data, list) else [data]
            options = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                price = positive_number(row.get("cost") or row.get("totalCost") or row.get("price"))
                if price:
                    options.append((price, row))
            if options:
                price, row = min(options, key=lambda item: item[0])
                days = row.get("days") or row.get("deliveryPeriod")
                return {
                    "company": "DPD", "company_label": COMPANY_LABELS["DPD"], "status": "ok",
                    "price": round(price), "currency": "RUB", "delivery_days_min": days, "delivery_days_max": days,
                    "source_type": "Официальный SOAP API", "source_url": "https://dpd.ru/integration",
                    "freshness": "live", "formula": "минимальная услуга из getServiceCost2, терминал–терминал",
                    "message": cleanTextForBackend(row.get("serviceName") or row.get("serviceCode") or "Онлайн-расчёт DPD"),
                }
            api_error = "SOAP API не вернул тариф"
        except Exception as exc:
            api_error = str(exc)
    public = open_source_result("DPD", dpd_public_reference(origin, destination, weight, volume))
    if public:
        if api_error:
            public["message"] = f"SOAP API недоступен ({cleanTextForBackend(api_error)}). " + public["message"]
        return public
    result = public_document_result("DPD", COMPANY_LABELS["DPD"], origin, destination, weight, volume)
    if api_error:
        result["message"] = f"SOAP API недоступен: {cleanTextForBackend(api_error)}. " + result["message"]
    else:
        result["message"] = "Для этого веса/маршрута открытой маршрутной тарифной сетки DPD не найдено. " + result["message"]
    return result


def calculate_generic_partner_api(
    company: str,
    url: str,
    api_key: str,
    origin: str,
    destination: str,
    weight: float,
    volume: float,
    source_url: str,
) -> dict[str, Any]:
    if not api_key:
        return unavailable(company, "Добавьте API-ключ в настройках", "needs_key")
    if not url:
        return unavailable(company, "Добавьте URL метода расчёта из документации перевозчика", "needs_setup")
    side = cargo_side_m(volume)
    payload = {
        "origin": normalize_city(origin),
        "destination": normalize_city(destination),
        "from": {"city": normalize_city(origin)},
        "to": {"city": normalize_city(destination)},
        "cargo": {
            "quantity": 1, "weight": float(weight), "volume": float(volume),
            "length": side, "width": side, "height": side,
        },
    }
    try:
        response = HTTP.post(
            url, json=payload, timeout=(5, 20),
            headers={
                "Authorization": f"Bearer {api_key}", "X-API-Key": api_key,
                "Accept": "application/json", "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
        prices = collect_named_numbers(data, ("total", "totalPrice", "priceTotal", "price", "cost", "amount", "deliverySum"))
        if not prices:
            return unavailable(company, f"API ответил без распознанной стоимости: {cleanTextForBackend(data)}")
        price = min(prices)
        min_days = next(iter(collect_named_numbers(data, ("periodMin", "daysMin", "deliveryDaysMin", "duration"))), None)
        max_days = next(iter(collect_named_numbers(data, ("periodMax", "daysMax", "deliveryDaysMax"))), min_days)
        return {
            "company": company, "company_label": COMPANY_LABELS[company], "status": "ok",
            "price": round(price), "currency": "RUB", "delivery_days_min": min_days, "delivery_days_max": max_days,
            "source_type": "Партнёрский REST API", "source_url": source_url,
            "freshness": "live", "formula": "минимальная итоговая стоимость из ответа настроенного API",
            "message": "Онлайн-расчёт по настроенному партнёрскому API",
        }
    except Exception as exc:
        return unavailable(company, f"Не удалось получить расчёт партнёрского API: {exc}", "error")


def baikal_api_urls(configured_url: str) -> tuple[str, str]:
    """Return (base_url, calculator_url) for a configured Baikal API URL."""
    value = str(configured_url or "").strip().rstrip("/")
    if value.endswith("/v2/calculator"):
        return value[:-len("/v2/calculator")], value
    if value.endswith("/v2"):
        return value[:-len("/v2")], value + "/calculator"
    return value, value + "/v2/calculator"


def baikal_city_guid(base_url: str, api_key: str, city: str) -> str | None:
    response = HTTP.get(
        base_url.rstrip("/") + "/v2/fias/cities",
        params={"text": normalize_city(city)},
        auth=(api_key, ""),
        timeout=(5, 15),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        rows = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        nested = data.get("data") or data.get("result") or data.get("cities")
        if isinstance(nested, list):
            rows = [item for item in nested if isinstance(item, dict)]
        elif data.get("guid"):
            rows = [data]
        else:
            rows = [item for item in data.values() if isinstance(item, dict)]
    target = normalize_city(city).lower()
    exact = next((item for item in rows if normalize_city(str(item.get("name") or "")).lower() == target), None)
    chosen = exact or (rows[0] if rows else None)
    return str(chosen.get("guid")) if chosen and chosen.get("guid") else None


def calculate_baikal(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    settings = load_settings()
    api_key = settings.get("baikal_api_key", "")
    configured_url = settings.get("baikal_api_url", "")
    api_error = ""
    if api_key and configured_url:
        try:
            base_url, calculator_url = baikal_api_urls(configured_url)
            origin_guid = baikal_city_guid(base_url, api_key, origin)
            destination_guid = baikal_city_guid(base_url, api_key, destination)
            if not origin_guid or not destination_guid:
                raise RuntimeError("город не найден в справочнике ФИАС")
            side = cargo_side_m(volume)
            payload = {
                "Departure": {"CityGuid": origin_guid},
                "Destination": {"CityGuid": destination_guid},
                "Cargo": {"SummaryCargo": {
                    "Length": round(side, 4), "Width": round(side, 4), "Height": round(side, 4),
                    "Volume": float(volume), "Weight": float(weight), "Units": 1,
                    "Oversized": 0, "EstimatedCost": 0,
                }},
                "Preference": {"AuthKey": api_key, "PartnerGUID": ""},
            }
            response = HTTP.post(
                calculator_url,
                json=payload,
                auth=(api_key, ""),
                timeout=(5, 20),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            price = positive_number(data.get("total")) if isinstance(data, dict) else None
            if price is not None:
                transit = data.get("transit") or {}
                days = transit.get("int") if isinstance(transit, dict) else None
                return {
                    "company": "Байкал Сервис", "company_label": COMPANY_LABELS["Байкал Сервис"], "status": "ok",
                    "price": round(price), "currency": "RUB", "delivery_days_min": days, "delivery_days_max": days,
                    "source_type": "Официальный REST API v2", "source_url": "https://www.baikalsr.ru/dev/api/",
                    "freshness": "live", "formula": "поле total из ответа /v2/calculator для терминальной перевозки",
                    "message": cleanTextForBackend((data.get("cargo") or {}).get("description") or "Онлайн-расчёт Байкал Сервис"),
                    "calculator_total_only": True,
                    "control_volume_m3": float(volume),
                }
            api_error = cleanTextForBackend((data.get("errors") or data.get("error") or "API не вернул стоимость") if isinstance(data, dict) else data)
        except Exception as exc:
            api_error = str(exc)
    public = open_source_result("Байкал Сервис", baikal_public_tariff(origin, destination, weight, volume))
    if public:
        if api_error:
            public["message"] = f"REST API недоступен ({cleanTextForBackend(api_error)}). " + public["message"]
        return public
    # The public price table is intentionally small and does not list every
    # direction.  The official site calculator does, and unlike the REST API it
    # does not require the user to provision an integration key.  Use it as the
    # last exact online source before falling back to a document-unavailable row.
    browser = public_web_result("Байкал Сервис", origin, destination, weight, volume, api_error)
    if browser.get("status") == "ok" and not browser.get("price_is_minimum"):
        browser["calculator_total_only"] = True
        browser["control_volume_m3"] = float(volume)
        return browser
    result = public_document_result("Байкал Сервис", COMPANY_LABELS["Байкал Сервис"], origin, destination, weight, volume)
    if api_error:
        result["message"] = f"REST API недоступен: {cleanTextForBackend(api_error)}. " + result["message"]
    else:
        result["message"] = "В открытой таблице Байкал Сервис этот маршрут не опубликован. " + result["message"]
    return result


def calculate_pony(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    settings = load_settings()
    api_url = settings.get("pony_api_url", "")
    api_key = settings.get("pony_api_key", "")
    api_error = ""
    if api_url and api_key:
        result = calculate_generic_partner_api(
            "Pony Express", api_url, api_key,
            origin, destination, weight, volume, "https://www.ponyexpress.ru/support/servisy-samoobsluzhivaniya/tariff/",
        )
        if result.get("status") == "ok":
            return result
        api_error = result.get("message") or "партнёрский API недоступен"
    public = open_source_result("Pony Express", pony_public_pdf(origin, destination, weight, volume))
    if public:
        if api_error:
            public["message"] = f"Партнёрский API недоступен ({cleanTextForBackend(api_error)}). " + public["message"]
        return public
    result = public_document_result("Pony Express", COMPANY_LABELS["Pony Express"], origin, destination, weight, volume)
    if api_error:
        result["message"] = f"Партнёрский API недоступен: {cleanTextForBackend(api_error)}. " + result["message"]
    else:
        result["message"] = "Официальный PDF PONY не содержит распознанной маршрутной строки для этих условий либо временно недоступен. " + result["message"]
    return result


def result_cache_key(company: str, origin: str, destination: str, weight: float, volume: float) -> str:
    return "|".join([
        company,
        normalize_city(origin).lower(),
        normalize_city(destination).lower(),
        f"{float(weight):.3f}",
        f"{float(volume):.4f}",
    ])


def load_result_cache() -> dict[str, Any]:
    if not COMPARISON_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(COMPARISON_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def save_result_cache(cache: dict[str, Any]) -> None:
    try:
        COMPARISON_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_cached_company_result(key: str, max_age_seconds: int = 6 * 3600) -> dict[str, Any] | None:
    cache = load_result_cache()
    node = cache.get(key)
    if not isinstance(node, dict):
        return None
    saved_at = float(node.get("saved_at") or 0)
    result = node.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("price"), (int, float)):
        return None
    age = max(0, time.time() - saved_at)
    if age > max_age_seconds:
        return None
    result = dict(result)
    result["cache_age_minutes"] = round(age / 60)
    return result


def cache_successful_result(key: str, result: dict[str, Any]) -> None:
    if result.get("status") != "ok" or not isinstance(result.get("price"), (int, float)):
        return
    with _RESULT_CACHE_LOCK:
        cache = load_result_cache()
        cache[key] = {"saved_at": time.time(), "result": result}
        # Ограничиваем размер локального кэша.
        if len(cache) > 500:
            ordered = sorted(cache.items(), key=lambda kv: float(kv[1].get("saved_at") or 0), reverse=True)[:400]
            cache = dict(ordered)
        save_result_cache(cache)


def enrich_market_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    exact = [float(x["price"]) for x in items if x.get("status") in {"ok", "cached"} and isinstance(x.get("price"), (int, float)) and not x.get("price_is_minimum") and not x.get("is_estimate")]
    estimates = [float(x["price"]) for x in items if x.get("status") == "estimated" and isinstance(x.get("price"), (int, float)) and not x.get("price_is_minimum")]
    lower_bounds = [float(x["price"]) for x in items if isinstance(x.get("price"), (int, float)) and x.get("price_is_minimum")]
    valid = exact or estimates
    if not valid:
        return {"count": 0, "exact_count": 0, "estimate_count": 0, "lower_bound_count": len(lower_bounds), "available_count": len(lower_bounds), "min": None, "median": None, "avg": None, "max": None, "spread": None, "spread_pct": None}
    min_price = min(valid)
    max_price = max(valid)
    median = statistics.median(valid)
    return {
        "count": len(valid),
        "exact_count": len(exact),
        "estimate_count": len(estimates),
        "lower_bound_count": len(lower_bounds),
        "available_count": len(exact) + len(estimates) + len(lower_bounds),
        "min": round(min_price),
        "median": round(median),
        "avg": round(statistics.mean(valid)),
        "max": round(max_price),
        "spread": round(max_price - min_price),
        "spread_pct": round((max_price / min_price - 1) * 100, 1) if min_price else None,
    }



def _published_rate_per_kg(item: dict[str, Any], weight: float) -> float | None:
    """Return only a rate explicitly published by the carrier.

    v30 deliberately forbids ``price / weight``. A calculator/API may provide a
    perfectly valid shipment total, but that total is not silently converted into
    a tariff column in ₽/kg. Heavy rows are populated only from a source field or
    formula that explicitly contains the published kg rate.
    """
    direct = item.get("rate_per_kg")
    if isinstance(direct, (int, float)) and float(direct) > 0:
        item["rate_kind"] = "published"
        return round(float(direct), 4)
    formula = str(item.get("formula") or "").replace(",", ".")
    matches = re.findall(r"(?:\d+(?:\.\d+)?\s*)?кг\s*[×x*]\s*(\d+(?:\.\d+)?)", formula, flags=re.I)
    if matches:
        try:
            item["rate_kind"] = "published"
            return round(float(matches[0]), 4)
        except Exception:
            pass
    return None


def decorate_weight_only_item(item: dict[str, Any], weight: float, minimum_profile: bool = False) -> dict[str, Any]:
    """Normalize every customer-facing value to a shipment total in rubles.

    Carrier source semantics are preserved in metadata: a published RUB/kg rate
    remains available as ``rate_per_kg`` and in ``formula``.  The comparison UI,
    matrix and Excel export, however, always use the resulting total shipment
    price in RUB.  No currency/rate unit is mixed in the comparison column.
    """
    result = dict(item)
    price = result.get("price")
    is_lower_bound = bool(result.get("price_is_minimum"))

    official_value = result.get("official_comparison_value")
    if not minimum_profile and isinstance(official_value, (int, float)):
        official_unit = str(result.get("official_comparison_unit") or "₽")
        if official_unit in {"₽/кг", "RUB/kg", "руб/кг"}:
            total = float(official_value) * float(weight)
            result.setdefault("rate_per_kg", float(official_value))
        else:
            total = float(official_value)
        result["price"] = round(total, 2)
        result["comparison_value"] = round(total, 2)
        result["comparison_unit"] = "₽"
        result["comparison_basis"] = "shipment_total"
        result["price_per_kg"] = _published_rate_per_kg(result, float(weight))
        result["market_position"] = "Стоимость отправки"
        return result

    official_condition = str(result.get("official_condition") or "").strip()
    if official_condition:
        result["comparison_value"] = None
        result["comparison_unit"] = "₽"
        result["comparison_basis"] = "official_condition"
        result["price_per_kg"] = None
        result["market_position"] = "Условие прайса"
        result["display_text"] = official_condition
        return result

    if minimum_profile:
        result["comparison_value"] = float(price) if isinstance(price, (int, float)) and not is_lower_bound else None
        result["comparison_unit"] = "₽"
        result["comparison_basis"] = "minimum"
        result["price_per_kg"] = None
        result["market_position"] = "Минимальная стоимость" if result["comparison_value"] is not None else ("Цена от" if is_lower_bound else "Нет данных")
        if result["comparison_value"] is None and not is_lower_bound:
            msg = str(result.get("message") or "").lower()
            if "договор" in msg:
                result["display_text"] = "договор"
            elif "выше максим" in msg or "нет тарифа" in msg:
                result["display_text"] = "нет тарифа"
            elif result.get("status") == "document_unavailable" or "не загруж" in msg or "не нормализ" in msg:
                result["display_text"] = "прайс не загружен"
            else:
                result["display_text"] = "минимум не опубликован"
        return result

    # For every non-MIN row the carrier adapter has already converted a published
    # rate/table rule into an exact shipment total in ``price``.  Show that total.
    if isinstance(price, (int, float)) and not is_lower_bound:
        result["comparison_value"] = round(float(price), 2)
        result["comparison_unit"] = "₽"
        result["comparison_basis"] = "shipment_total"
        result["price_per_kg"] = _published_rate_per_kg(result, float(weight))
        result["market_position"] = "Стоимость отправки"
        return result

    result["comparison_value"] = None
    result["comparison_unit"] = "₽"
    result["comparison_basis"] = "shipment_total"
    result["price_per_kg"] = None
    result["market_position"] = "Цена от" if is_lower_bound else "Нет данных"
    if not is_lower_bound:
        msg = str(result.get("message") or "").lower()
        if "договор" in msg:
            result["display_text"] = "договор"
        elif "выше максим" in msg or "нет тарифа" in msg:
            result["display_text"] = "нет тарифа"
        elif result.get("status") == "document_unavailable" or "не загруж" in msg or "не нормализ" in msg:
            result["display_text"] = "прайс не загружен"
        else:
            result["display_text"] = "нет опубликованной цены"
    return result



def _numeric_comparison(item: dict[str, Any]) -> bool:
    return isinstance(item.get("comparison_value"), (int, float)) and float(item.get("comparison_value")) > 0


def _clone_range_value(donor: dict[str, Any], target_weight: float, target_profile: dict[str, Any], *, inferred_low: float | None = None, inferred_high: float | None = None) -> dict[str, Any]:
    """Copy one explicit source tariff tier to another covered control weight.

    For a published per-kg tier we keep the same source rate but recalculate the
    shipment total for the target control weight. Fixed shipment tiers are copied
    unchanged. The customer-facing comparison value is always RUB.
    """
    out = dict(donor)
    out.pop("display_text", None)
    out["comparison_unit"] = "₽"
    out["range_value_repeated"] = True
    out["normalized_from_weight_kg"] = donor.get("normalized_from_weight_kg") or donor.get("control_weight_kg") or donor.get("source_weight_kg")
    out["control_weight_kg"] = float(target_weight)
    low = donor.get("tariff_range_low_kg") if donor.get("tariff_range_low_kg") is not None else inferred_low
    high = donor.get("tariff_range_high_kg") if donor.get("tariff_range_high_kg") is not None else inferred_high
    if low is not None:
        out["tariff_range_low_kg"] = float(low)
    if high is not None:
        out["tariff_range_high_kg"] = float(high)

    rate = donor.get("rate_per_kg")
    if isinstance(rate, (int, float)) and float(rate) > 0:
        total = float(target_weight) * float(rate)
        floor = donor.get("minimum_charge")
        if isinstance(floor, (int, float)) and float(floor) > 0:
            total = max(total, float(floor))
        out["rate_per_kg"] = float(rate)
        out["price_per_kg"] = float(rate)
        out["price"] = round(total, 2)
        out["comparison_value"] = round(total, 2)
        out["comparison_basis"] = "shipment_total"
        out["market_position"] = "Стоимость отправки"
    else:
        value = donor.get("price") if isinstance(donor.get("price"), (int, float)) else donor.get("comparison_value")
        if not isinstance(value, (int, float)):
            return out
        out["price"] = round(float(value), 2)
        out["comparison_value"] = round(float(value), 2)
        out["price_per_kg"] = None
        out["comparison_basis"] = "shipment_total"
        out["market_position"] = "Стоимость отправки"

    source_note = "повтор опубликованной тарифной ступени"
    if low is not None or high is not None:
        left = f"{float(low):g}" if low is not None else "0"
        right = f"{float(high):g}" if high is not None else "∞"
        source_note += f" {left}–{right} кг"
    old = str(donor.get("message") or "").rstrip(". ")
    out["message"] = (old + ". " if old else "") + "Значение не интерполировано: " + source_note + " применён ко всем строкам единой сетки, которые в него попадают; итог пересчитан в рублях для контрольного веса."
    return out



def apply_customer_range_copy_rule(company: str, column: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand only explicit carrier tariff intervals over the customer grid.

    v30 intentionally does *not* infer a tariff interval from neighbouring control
    points. A value may be copied only when the source parser itself supplied
    ``tariff_range_low_kg`` / ``tariff_range_high_kg``. This prevents calculator
    totals and historical control points from being mistaken for published tariff
    columns. MIN is also left untouched unless the source publishes a minimum.
    """
    if len(column) != len(COMMON_PROFILES):
        return column
    out = [dict(x) for x in column]

    def donor_ok(i: int) -> bool:
        return _numeric_comparison(out[i]) and not bool(out[i].get("price_is_minimum"))

    groups = [list(range(0, 9)), list(range(10, len(COMMON_PROFILES)))]
    for indices in groups:
        donors = [i for i in indices if donor_ok(i) and (out[i].get("tariff_range_low_kg") is not None or out[i].get("tariff_range_high_kg") is not None)]
        for i in indices:
            if donor_ok(i):
                out[i]["control_weight_kg"] = float(COMMON_PROFILES[i]["weight_kg"])
                continue
            w = float(COMMON_PROFILES[i]["weight_kg"])
            covering: list[tuple[float, int]] = []
            for j in donors:
                low = out[j].get("tariff_range_low_kg")
                high = out[j].get("tariff_range_high_kg")
                low_f = float(low) if low is not None else 0.0
                high_f = float(high) if high is not None else float("inf")
                if w >= low_f - 1e-9 and w <= high_f + 1e-9:
                    covering.append((high_f - low_f, j))
            if covering:
                _, j = min(covering, key=lambda x: (x[0], x[1]))
                out[i] = _clone_range_value(out[j], w, COMMON_PROFILES[i])
    return out

def explicit_minimum_charge(company: str, origin: str, destination: str) -> dict[str, Any]:
    """Return only an explicitly published minimum charge; never invent one."""
    o, d = normalize_city(origin), normalize_city(destination)
    collected_minimum = collected_published_tariff(company, o, d, 1.0, minimum=True)
    if collected_minimum and isinstance(collected_minimum.get("price"), (int, float)):
        return collected_minimum
    value: float | None = None
    source_type = ""
    source_url = ""
    message = ""
    try:
        if company == "Werner":
            published = werner_official_tariff(o, d, 1.0, minimum=True)
            if published and isinstance(published.get("price"), (int, float)):
                value = float(published["price"]); source_type = str(published.get("source_type") or "Официальный тарифный файл Werner"); source_url = str(published.get("source_url") or "https://wernerus.ru/clients/prices/"); message = "Минимальная стоимость взята только из отдельной опубликованной колонки/строки МИН Werner."
        elif company == "ДЛ":
            path = dellin_pdf_for_origin(o)
            tariff = (parse_dellin_tariffs(path, o).get(d) if path else None)
            if tariff and isinstance(tariff.get("minimum"), (int, float)):
                value = float(tariff["minimum"]); source_type = "Официальный PDF"; source_url = "https://www.dellin.ru/pricelist_pdf/"
        elif company == "ПЭК":
            row = PEK_HEAVY_WEIGHT_RATES.get((o, d))
            if row:
                value = float(row["minimum"]); source_type = "Официальная маршрутная страница ПЭК"; source_url = str(row.get("url") or "")
        elif company == "КИТ":
            row = KIT_PUBLIC_FALLBACK.get((o, d))
            if row:
                value = float(row["minimum"]); source_type = "Опубликованная ставка КИТ"; source_url = "https://tk-kit.ru/rates-new"
        elif company == "Главтрасса":
            published = glavtrassa_official_tariff(o, d, 1.0, minimum=True)
            if published and isinstance(published.get("price"), (int, float)):
                value = float(published["price"]); source_type = str(published.get("source_type") or "Официальный тарифный файл Главтрассы"); source_url = str(published.get("source_url") or "https://glavtrassa.ru/clients/prices/")
        elif company == "Пролайн":
            row = PROLINE_ROUTES.get((o, d))
            if row and row.get("minimum") is not None:
                value = float(row["minimum"]); source_type = "Официальная маршрутная таблица Пролайн"; source_url = str(row.get("url") or "")
        elif company == "ФастТранс":
            row = FASTTRANS_SNAPSHOTS.get((o, d))
            fixed = (row or {}).get("fixed") or []
            if fixed:
                value = float(min(x[2] for x in fixed)); source_type = "Официальная тарифная таблица ФастТранс"; source_url = str(row.get("url") or "")
        elif company == "АТЭК" and (o, d) in {("Москва", "Санкт-Петербург"), ("Санкт-Петербург", "Москва")}:
            value = float(min(x[1] for x in ATEK_MSK_SPB.get("fixed", []))); source_type = "Официальная тарифная страница АТЭК"; source_url = str(ATEK_MSK_SPB.get("url") or "")
        elif company == "БСК" and (o, d) in {("Москва", "Санкт-Петербург"), ("Санкт-Петербург", "Москва")}:
            row = BSD_MSK_SPB if (o, d) == ("Москва", "Санкт-Петербург") else BSD_SPB_MSK
            value = float(row["minimum"]); source_type = "Официальная тарифная таблица БСД"; source_url = str(row.get("url") or "")
        elif company == "ЭкспедицияПлюс":
            data = json.loads((DATA_DIR / "expeditionplus_tariffs_snapshot.json").read_text(encoding="utf-8"))
            row = (data.get("routes") or {}).get(f"{o}|{d}")
            if isinstance(row, dict) and isinstance(row.get("minimum"), (int, float)):
                value = float(row["minimum"]); source_type = "Официальный XLSX ЭкспедицияПлюс"; source_url = str(data.get("source_url") or data.get("source_page") or "")
        elif company == "CTSgroup":
            data = json.loads((DATA_DIR / "ctsgroup_tariffs_snapshot.json").read_text(encoding="utf-8"))
            row = (data.get("routes") or {}).get(f"{o}|{d}")
            if isinstance(row, dict) and isinstance(row.get("minimum"), (int, float)):
                value = float(row["minimum"]); source_type = "Официальный XLSX CTS Group"; source_url = str(data.get("source_url") or data.get("source_page") or "")
        elif company == "Фортуна":
            data = json.loads((DATA_DIR / "snapshots" / "fortuna_official_2026-07-27.json").read_text(encoding="utf-8"))
            row = (data.get("routes") or {}).get(f"{o}|{d}")
            if isinstance(row, dict) and isinstance(row.get("minimum"), (int, float)):
                value = float(row["minimum"]); source_type = "Официальный XLSX Фортуна Экспресс"; source_url = str(data.get("source_page") or "https://fte.ru/price/")
        elif company == "Новая Линия":
            data = json.loads((DATA_DIR / "snapshots" / "newline_moscow_official_2026-08-22.json").read_text(encoding="utf-8"))
            row = (data.get("routes") or {}).get(f"{o}|{d}")
            if isinstance(row, dict) and isinstance(row.get("minimum"), (int, float)):
                value = float(row["minimum"]); source_type = "Официальный PDF Новая Линия"; source_url = str(data.get("source_url") or "https://tknl.ru/price/")
        elif company == "Мейджик":
            row = MAGIC_ROUTE_MINIMUMS.get((o, d))
            if row:
                value = float(row["price"]); source_type = "Официальная маршрутная цена Мейджик Транс «от»"; source_url = str(row.get("url") or ""); message = str(row.get("note") or "")
        elif company == "Возовоз":
            row = VOZOVOZ_PUBLIC_FALLBACK.get((o, d))
            if row is not None:
                value = float(row); source_type = "Официальная публичная цена «от»"; source_url = "https://vozovoz.ru/tariffs/"; message = "Опубликованная нижняя граница маршрута, а не точный договорной минимум."
        elif company == "Рейл Континент":
            row = RAIL_WEIGHT_RATES.get((o, d))
            if row:
                value = float(row["minimum"]); source_type = "Официальная весовая тарифная таблица Рейл Континент"; source_url = str(row["url"]); message = f"Минимальная стоимость из весовой таблицы, действует с {row['effective']}."
    except Exception as exc:
        return unavailable(company, f"Не удалось прочитать минимальную стоимость: {exc}", "error")
    if value is None:
        return unavailable(company, "В открытом источнике не опубликована отдельная минимальная стоимость для этого маршрута.", "unavailable")
    return {
        "company": company, "company_label": COMPANY_LABELS.get(company, company), "status": "ok",
        "price": round(value, 2), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": source_type or "Официальный источник", "source_url": source_url,
        "freshness": "official_snapshot", "formula": "явно опубликованная минимальная стоимость отправки",
        "message": message or "Минимальная стоимость взята напрямую из опубликованного тарифа.",
        "price_is_minimum": company == "Возовоз",
    }

def calculate_open_tariff(company: str, fn, origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    try:
        partial = fn(origin, destination, weight, volume)
    except Exception as exc:
        partial = None
        error = f"Ошибка открытого источника: {cleanTextForBackend(str(exc))}"
    else:
        error = ""
    result = open_source_result(company, partial)
    if result:
        return result
    doc = public_document_result(company, COMPANY_LABELS[company], origin, destination, weight, volume)
    if error:
        doc["message"] = error + ". " + str(doc.get("message") or "")
    return doc


def calculate_glavtrassa(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    """Exact terminal-to-terminal quote from the carrier's documented keyless API."""
    o, d = normalize_city(origin), normalize_city(destination)
    ids = {"Москва": "35", "Санкт-Петербург": "36"}
    dep, arr = ids.get(o), ids.get(d)
    if not dep or not arr:
        # Keep the existing general adapter for other cities.
        try:
            partial = glavtrassa_tariff(o, d, weight, volume)
        except Exception:
            partial = None
        result = open_source_result("Главтрасса", partial)
        return result or unavailable("Главтрасса", "Город не найден в публичном справочнике Главтрассы", "error")

    side = max(0.1, float(volume)) ** (1 / 3) if float(volume) > 0 else 0.1
    params = {
        "method": "api_calc", "responseFormat": "json", "depPoint": dep, "arrPoint": arr,
        "cargoMest[1]": 1, "cargoKg[1]": float(weight), "cargoL[1]": round(side, 4),
        "cargoW[1]": round(side, 4), "cargoH[1]": round(side, 4), "cargoCalculation[1]": 1,
    }
    endpoint = "https://glavtrassa.ru/api/calc/"
    data = None
    errors: list[str] = []
    try:
        response = HTTP.get(endpoint, params=params, timeout=(4, 12), headers={"Accept": "application/json", "Referer": "https://glavtrassa.ru/clients/calc/", "User-Agent": HTTP.headers.get("User-Agent", "Mozilla/5.0")})
        response.raise_for_status()
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except Exception as exc:
        errors.append(f"requests: {exc}")
    prepared = requests.Request("GET", endpoint, params=params).prepare().url
    if data is None:
        try:
            alt = core._curl_response(prepared, headers={"Accept": "application/json", "Referer": "https://glavtrassa.ru/"}, timeout=18)
            parsed = json.loads((alt.content or b"").decode("utf-8-sig", errors="replace"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            errors.append(f"curl: {exc}")
    if data is None:
        try:
            alt = core._powershell_response(prepared, headers={"Accept": "application/json", "Referer": "https://glavtrassa.ru/clients/calc/"}, timeout=18)
            parsed = json.loads((alt.content or b"").decode("utf-8-sig", errors="replace"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            errors.append(f"powershell: {exc}")
    if data is None:
        try:
            alt = core._browser_response(prepared, headers={"Referer": "https://glavtrassa.ru/clients/calc/"}, timeout=18)
            parsed = json.loads((alt.content or b"").decode("utf-8-sig", errors="replace"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            errors.append(f"browser-fetch: {exc}")
    if not isinstance(data, dict) or str(data.get("status") or "").upper() == "ERROR":
        return unavailable("Главтрасса", "Официальный API Главтрассы не вернул точную стоимость" + (": " + " | ".join(errors[-2:]) if errors else ""), "error")
    price = recursive_price(data)
    if price is None:
        return unavailable("Главтрасса", "Официальный API Главтрассы вернул ответ без стоимости", "error")
    return {
        "company": "Главтрасса", "company_label": COMPANY_LABELS["Главтрасса"], "status": "ok",
        "price": round(float(price)), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": "Официальный публичный API Главтрассы", "source_url": endpoint, "freshness": "live",
        "formula": f"1 место; {float(weight):g} кг; {float(volume):g} м³",
        "message": "Расчёт получен напрямую из открытого метода Главтрассы без API-ключа.",
        "price_is_minimum": False, "calculator_total_only": True, "control_volume_m3": float(volume),
    }


def calculate_proline(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("Пролайн", proline_tariff, origin, destination, weight, volume)


def calculate_fasttrans(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("ФастТранс", fasttrans_tariff, origin, destination, weight, volume)


def calculate_atek(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("АТЭК", atek_tariff, origin, destination, weight, volume)


def calculate_bsk(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("БСК", bsk_tariff, origin, destination, weight, volume)


def calculate_magic(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("Мейджик", magic_tariff, origin, destination, weight, volume)


def calculate_newline(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("Новая Линия", newline_tariff, origin, destination, weight, volume)


def calculate_document_only(company: str, origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return public_document_result(company, COMPANY_LABELS[company], origin, destination, weight, volume)


def calculate_fortuna(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("Фортуна", fortuna_tariff, origin, destination, weight, volume)


def calculate_gruzopotok(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_document_only("Грузопоток", origin, destination, weight, volume)


def calculate_expeditionplus(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    return calculate_open_tariff("ЭкспедицияПлюс", expeditionplus_tariff, origin, destination, weight, volume)


def calculate_ctsgroup(origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    try:
        partial = ctsgroup_tariff(origin, destination, weight, volume)
    except Exception as exc:
        partial = None
        source_error = str(exc)
    else:
        source_error = ""
    result = open_source_result("CTSgroup", partial)
    if result and result.get("status") == "ok" and isinstance(result.get("price"), (int, float)):
        return result
    # CTS publishes an interactive official calculator for directions that are
    # not present in the bundled Moscow-origin workbook.  This is especially
    # important for Санкт-Петербург → Москва.
    browser = public_web_result("CTSgroup", origin, destination, weight, volume, source_error)
    if browser.get("status") == "ok" and not browser.get("price_is_minimum"):
        browser["calculator_total_only"] = True
        browser["control_volume_m3"] = float(volume)
        return browser
    return result or browser


def calculate_with_cache(company: str, calculator, origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    key = result_cache_key(company, origin, destination, weight, volume)
    # Для стабильных табличных тарифов ДЛ кэш сетевого ответа не нужен.
    if company == "ДЛ":
        return calculator(origin, destination, weight, volume)
    snapshot = verified_profile_snapshot(company, origin, destination, weight, volume)
    # Для стандартных контрольных точек приоритет у уже проверенного официального
    # снимка: пользователь получает таблицу мгновенно и воспроизводимо, без ожидания
    # DNS/тайм-аутов внешнего сайта. Для произвольной точки, где снимка нет,
    # по-прежнему выполняется живой открытый запрос.
    if snapshot:
        return snapshot
    # Матрица запрашивает одни и те же контрольные точки при каждом выборе
    # диапазона. Не дёргаем официальный сайт повторно, если есть свежий точный
    # ответ этой же точки; это и быстрее, и бережнее к API перевозчика.
    cached = get_cached_company_result(key, max_age_seconds=30 * 60)
    if cached:
        cached["status"] = "cached"
        cached["freshness"] = "recent_exact_cache"
        cached["message"] = (str(cached.get("message") or "").rstrip(". ") + f". Точный ответ этой контрольной точки сохранён {cached.get('cache_age_minutes', 0)} мин. назад.").strip()
        return cached
    recent_failure = _FAILURE_BACKOFF.get(company)
    if recent_failure and time.time() - float(recent_failure.get("at") or 0) < 120:
        cached = get_cached_company_result(key)
        if cached:
            cached["status"] = "cached"
            cached["freshness"] = "last_success"
            cached["message"] = f"Последний успешный расчёт ({cached.get('cache_age_minutes', 0)} мин. назад). Онлайн-источник временно недоступен."
            return cached
        if snapshot:
            return snapshot
        if company == "Возовоз":
            minimum, freshness, source_url = vozovoz_public_minimum(origin, destination)
            if minimum is not None:
                return {
                    "company": "Возовоз", "company_label": COMPANY_LABELS["Возовоз"], "status": "ok",
                    "price": round(float(minimum)), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
                    "source_type": "Официальная публичная цена «от»", "source_url": source_url, "freshness": freshness,
                    "formula": "нижняя граница стоимости маршрута; точная цена зависит от параметров груза",
                    "message": "Точный API временно недоступен; показана только официальная нижняя граница маршрута.",
                    "price_is_minimum": True,
                }
        return unavailable(company, str(recent_failure.get("message") or "Онлайн-источник временно недоступен"), "error")
    result = calculator(origin, destination, weight, volume)
    if result.get("status") == "ok" and not result.get("price_is_minimum"):
        _FAILURE_BACKOFF.pop(company, None)
        cache_successful_result(key, result)
        return result
    # Если живой источник смог дать только рекламную нижнюю границу, а для этой
    # контрольной точки есть ранее проверенный точный официальный ответ, используем
    # точный снимок и явно маркируем его датой.
    if result.get("status") == "ok" and result.get("price_is_minimum") and snapshot:
        return snapshot
    if result.get("status") == "ok" and result.get("price_is_minimum"):
        # Нижняя граница Возовоза появляется именно как fallback после неудачи
        # точного API. Запоминаем этот сбой, чтобы матрица не повторяла по два
        # сетевых запроса для каждой из оставшихся 20+ контрольных точек.
        if company == "Возовоз" and result.get("api_error"):
            _FAILURE_BACKOFF[company] = {"at": time.time(), "message": result.get("api_error")}
        return result
    if result.get("status") == "ok":
        _FAILURE_BACKOFF.pop(company, None)
        cache_successful_result(key, result)
        return result
    if result.get("status") == "error":
        _FAILURE_BACKOFF[company] = {"at": time.time(), "message": result.get("message")}
    cached = get_cached_company_result(key)
    if cached:
        cached["status"] = "cached"
        cached["freshness"] = "last_success"
        cached["message"] = f"Последний успешный расчёт ({cached.get('cache_age_minutes', 0)} мин. назад). Онлайн-источник сейчас недоступен."
        return cached
    if snapshot:
        return snapshot
    return result


def compare_prices(origin: str, destination: str, weight: float, volume: float, profile_id: str | None = None, selected_companies: list[str] | None = None) -> dict[str, Any]:
    """Fast customer-facing comparison.

    v30 does not wait for external web sites when the user presses
    Compare. It uses normalized official documents, embedded tariff snapshots and
    exact cached answers. Network collection/update is a separate explicit action.
    """
    origin = normalize_city(origin)
    destination = normalize_city(destination)
    profile = PROFILE_BY_ID.get(profile_id or "")
    minimum_profile = bool(profile and profile.get("is_minimum_profile"))
    requested = [c for c in COMPANIES if c in (selected_companies or COMPANIES)]

    if profile:
        # Use the same normalized 29-row matrix as the table. The route-level
        # cache makes the simultaneous compare+matrix browser requests share one
        # computation and guarantees that both views show exactly the same row.
        full = profile_matrix(origin, destination, requested)
        target = next((r for r in full.get("profiles", []) if (r.get("profile") or {}).get("id") == profile.get("id")), None)
        items = list((target or {}).get("items") or [])
    elif minimum_profile:
        items = [decorate_weight_only_item(explicit_minimum_charge(c, origin, destination), weight, True) for c in requested]
    else:
        items = [
            decorate_weight_only_item(fast_matrix_item(c, origin, destination, weight, 0.0), weight, False)
            for c in requested
        ]

    exact = [x for x in items if isinstance(x.get("comparison_value"), (int, float)) and not x.get("price_is_minimum")]
    lower = [x for x in items if isinstance(x.get("price"), (int, float)) and x.get("price_is_minimum")]
    # Порядок строк не сортируем по цене: он должен совпадать с утверждённым
    # списком компаний и с колонками Excel. Сравнение цен выполняется по значениям.
    return {
        "calculated_at": now_iso(), "origin": origin, "destination": destination,
        "weight_kg": weight, "volume_m3": 0.0, "profile_id": profile_id,
        "profile_label": profile.get("description") if profile else f"до {weight:g} кг",
        "range_weight": profile.get("range_weight") if profile else f"до {weight:g} кг",
        "range_volume": "", "calculation_point": profile.get("description") if profile else f"{weight:g} кг",
        "comparison_unit": "₽",
        "minimum_profile": minimum_profile,
        "items": items,
        "available_count": len(exact) + len(lower), "exact_count": len(exact), "lower_bound_count": len(lower),
        "selected_companies": requested,
        "methodology": "Единица сравнения во всех диапазонах — рубли за отправку. Опубликованные ставки ₽/кг сохраняются как исходные данные и умножаются на контрольный вес; фиксированные тарифы и результаты официальных калькуляторов остаются суммой в ₽. МИН — только явно опубликованный минимум. Интерполяции и усреднения нет.",
    }



# ---------------------------------------------------------------------------
# Strict parser for tariff tables collected by the "Обновить прайсы" action.
#
# The customer comparison grid is deliberately finer than many carrier tables.
# A source value is therefore allowed to repeat across several grid rows, but
# only when the official table itself exposes BOTH:
#   1) a weight interval, and
#   2) the economic unit of that column (RUB/kg or RUB per shipment).
#
# This parser never derives a kg rate from a calculator total and never infers
# missing range boundaries from neighbouring numeric points.
# ---------------------------------------------------------------------------
_COLLECTED_TARIFF_CACHE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
_COLLECTED_TARIFF_CACHE_STAMP: tuple[float, int] | None = None
_KIT_PDF_CANDIDATE_CACHE: dict[str, list[dict[str, Any]]] = {}


def _clear_collected_tariff_cache() -> None:
    global _COLLECTED_TARIFF_CACHE_STAMP
    _COLLECTED_TARIFF_CACHE.clear()
    _COLLECTED_TARIFF_CACHE_STAMP = None
    if "_PROFILE_MATRIX_CACHE" in globals():
        with _PROFILE_MATRIX_CACHE_LOCK:
            _PROFILE_MATRIX_CACHE.clear()
            _PROFILE_MATRIX_BUILD_LOCKS.clear()
    _PEK_INDEX_CACHE.clear() if "_PEK_INDEX_CACHE" in globals() else None
    _KIT_ROUTE_INDEX_CACHE.clear() if "_KIT_ROUTE_INDEX_CACHE" in globals() else None


def _strict_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ").strip())


def _strict_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    text = _strict_text(value)
    if not text or text in {"-", "—", "–"}:
        return None
    # Cell must be essentially numeric/currency.  Do not pull a random number
    # out of a descriptive sentence such as "до 5 дней".
    cleaned = re.sub(r"(?i)(?:руб(?:\.|лей|ля)?|₽|р\.)", "", text)
    cleaned = cleaned.replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    try:
        n = float(cleaned)
        return n if math.isfinite(n) and n > 0 else None
    except Exception:
        return None


def _strict_weight_range(text: Any) -> tuple[float, float | None] | None:
    """Parse an explicitly printed kg interval from a header cell."""
    raw = _strict_text(text).lower().replace("ё", "е")
    if not raw:
        return None
    raw = raw.replace("−", "-").replace("–", "-").replace("—", "-")
    raw = raw.replace("кг.", "кг")

    # "от 3000", "свыше 3000", "3000 и более"
    m = re.search(r"(?:^|\b)(?:от|свыше)\s*(\d[\d\s]*)\s*(?:кг)?\b", raw)
    if m:
        low = float(m.group(1).replace(" ", ""))
        return (low, None)
    m = re.search(r"\b(\d[\d\s]*)\s*(?:кг)?\s*(?:и\s*более|и\s*выше|\+)\b", raw)
    if m:
        low = float(m.group(1).replace(" ", ""))
        return (low, None)

    # "от 100 до 199"
    m = re.search(r"\bот\s*(\d[\d\s]*)\s*(?:кг)?\s*до\s*(\d[\d\s]*)\s*(?:кг)?\b", raw)
    if m:
        low = float(m.group(1).replace(" ", "")); high = float(m.group(2).replace(" ", ""))
        if high >= low:
            return (low, high)

    # "100-199" / "100 - 199 кг"
    m = re.search(r"(?<!\d)(\d[\d\s]*)\s*-\s*(\d[\d\s]*)(?:\s*кг)?(?!\d)", raw)
    if m:
        low = float(m.group(1).replace(" ", "")); high = float(m.group(2).replace(" ", ""))
        if high >= low:
            return (low, high)

    # "до 100 кг".  Require either an explicit kg marker or a compact header
    # cell, so a phrase such as "доставка до 3 дней" cannot become a weight tier.
    m = re.search(r"(?:^|\b)до\s*(\d[\d\s]*)\s*(кг)?\b", raw)
    if m and (m.group(2) or len(raw) <= 24):
        high = float(m.group(1).replace(" ", ""))
        return (0.0, high)
    return None


def _strict_unit_marker(text: Any) -> str | None:
    """Return per_kg/fixed/minimum/volume only from explicit source wording."""
    t = _strict_text(text).lower().replace("ё", "е")
    if not t:
        return None
    compact = t.replace(" ", "")
    if re.search(r"(?:миним|мин\.?\s*стоим|минимальная\s+стоимость)", t) and ("руб" in t or "₽" in t or "р." in t):
        return "minimum"
    if any(x in compact for x in ("руб/кг", "руб./кг", "₽/кг", "р./кг", "р/кг", "rub/kg")) or re.search(r"руб(?:\.|лей)?\s*(?:за|/)\s*кг", t):
        return "per_kg"
    if any(x in compact for x in ("руб/м3", "руб./м3", "₽/м3", "р/м3", "руб/м³", "₽/м³")) or re.search(r"руб(?:\.|лей)?\s*(?:за|/)\s*м[³3]", t):
        return "volume"
    # Fixed-shipment money columns must say that this is a cost/price/tariff in
    # rubles and must NOT contain a per-unit denominator.
    if ("руб" in t or "₽" in t or "р." in t) and not re.search(r"/[а-яa-z³3]+|за\s+(?:кг|м[³3])", t):
        if any(k in t for k in ("стоим", "цена", "тариф", "перевоз")):
            return "fixed"
    return None


def _source_ids_for_origin(company: str, origin: str) -> set[str] | None:
    """Return known direction-specific source IDs when the catalog has them."""
    o = normalize_city(origin)
    if company=="ПЭК":return {"PEK004"}
    shared = {
        "Мейджик": {"MGC001"},
        "Фортуна": {"FRT001"}, "Рейл Континент": {"RAIL002"},
        "АТЭК": {"AT001"},
        "ЭкспедицияПлюс": {"EP000"},
    }
    mapping: dict[str, dict[str, set[str]]] = {
        company_name: {"Москва": set(source_ids), "Санкт-Петербург": set(source_ids)}
        for company_name, source_ids in shared.items()
    }
    mapping.update({
        "Рейл Континент": {"Москва": {"RAIL002"}, "Санкт-Петербург": {"RAIL003"}},
        "ДЛ": {"Москва": {"DL001"}, "Санкт-Петербург": {"DL002"}},
        # PEK004 is the official consolidated workbook and covers both origins.
        "ПЭК": {"Москва": {"PEK004"}, "Санкт-Петербург": {"PEK004"}},
        "КИТ": {"Москва": {"KIT001"}, "Санкт-Петербург": {"KIT002"}},
        "Пролайн": {"Москва": {"PR001"}, "Санкт-Петербург": {"PR002"}},
        "ФастТранс": {"Москва": {"FT001"}, "Санкт-Петербург": {"FT002"}},
        "Новая Линия": {"Москва": {"NL001"}, "Санкт-Петербург": {"NL002"}},
        # City-specific tariff exports must never be reused in the reverse direction.
        # VOZ001 and CTS000 are Moscow-origin files; BSD has separate city pages.
        "Возовоз": {"Москва": {"VOZ001"}, "Санкт-Петербург": set()},
        "CTSgroup": {"Москва": {"CTS000"}, "Санкт-Петербург": set()},
        "БСК": {"Москва": {"BSD001"}, "Санкт-Петербург": {"BSD002"}},
        # These catalog pages render Moscow by default. Reverse-route figures
        # come from their official calculators instead of reusing Moscow HTML.
        "Werner": {"Москва": {"W001"}, "Санкт-Петербург": set()},
        "Главтрасса": {"Москва": {"GT001"}, "Санкт-Петербург": set()},
        "Байкал Сервис": {"Москва": {"BAI001"}, "Санкт-Петербург": set()},
    })
    return mapping.get(company, {}).get(o)


def _collected_results_stamp() -> tuple[float, int]:
    try:
        stat = core.RESULTS_PATH.stat()
        return (float(stat.st_mtime), int(stat.st_size))
    except Exception:
        return (0.0, 0)


def _collected_sheet_groups(company: str, origin: str) -> list[tuple[dict[str, Any], list[list[Any]]]]:
    """Rebuild table matrices from rows extracted by legacy.collect_sources."""
    global _COLLECTED_TARIFF_CACHE_STAMP
    stamp = _collected_results_stamp()
    if stamp != _COLLECTED_TARIFF_CACHE_STAMP:
        _COLLECTED_TARIFF_CACHE.clear()
        _COLLECTED_TARIFF_CACHE_STAMP = stamp
    try:
        results = core.load_results()
    except Exception:
        return []
    allowed_ids = _source_ids_for_origin(company, origin)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in results.get("rows", []):
        if not isinstance(row, dict) or str(row.get("company") or "") != company:
            continue
        if not bool(row.get("usable_for_price", True)):
            continue
        if str(row.get("coverage") or "") not in {"", "full_matrix", "limited_service"}:
            continue
        sid = str(row.get("source_id") or "")
        if allowed_ids is not None and sid not in allowed_ids:
            continue
        fmt = str(row.get("format") or "").lower()
        # A collector may additionally store a flattened whole-page HTML text
        # sheet. It has no table geometry, so using it as a matrix can associate
        # a tariff from a neighbouring route with the selected city. Only real
        # HTML tables (or spreadsheets) are eligible for strict tariff parsing.
        if fmt == "html" and str(row.get("sheet") or "").strip().lower() == "text":
            continue
        if fmt not in {"xlsx", "xls", "csv", "html"}:
            # PDF text is intentionally excluded from this generic parser.  A
            # flattened PDF line loses column geometry; guessing its positions
            # would recreate the same class of fake rates we are removing.
            continue
        key = (sid, str(row.get("file") or ""), str(row.get("sheet") or ""))
        groups[key].append(row)

    out: list[tuple[dict[str, Any], list[list[Any]]]] = []
    for rows in groups.values():
        rows.sort(key=lambda r: int(r.get("row_index") or 0))
        if not rows:
            continue
        max_index = max(int(r.get("row_index") or 0) for r in rows)
        # Keep original row numbers because header/destination proximity matters.
        matrix: list[list[Any]] = [[] for _ in range(max_index + 1)]
        meta = rows[0]
        for row in rows:
            idx = int(row.get("row_index") or 0)
            vals = row.get("columns")
            if not isinstance(vals, list):
                vals = [row.get(f"col_{i}") for i in range(1, 17)]
            if idx >= 0:
                matrix[idx] = vals[:96]
        out.append((meta, matrix))
    return out


def _unit_for_column(matrix: list[list[Any]], header_start: int, data_row: int, ci: int) -> str | None:
    """Resolve the nearest explicit unit-group heading for one data column."""
    # Same-column labels have the highest confidence.
    same: list[str] = []
    for h in range(header_start, data_row):
        row = matrix[h] if h < len(matrix) else []
        if ci < len(row) and _strict_text(row[ci]):
            same.append(_strict_text(row[ci]))
    for part in reversed(same):
        marker = _strict_unit_marker(part)
        if marker:
            return marker

    # For merged Excel headers only the leftmost cell contains text.  Select the
    # nearest explicit unit marker at or to the left of the target column.  If a
    # later marker starts before our column (e.g. the m3 group), it naturally wins.
    candidates: list[tuple[int, int, str]] = []
    for h in range(header_start, data_row):
        row = matrix[h] if h < len(matrix) else []
        for k, value in enumerate(row[:ci + 1]):
            marker = _strict_unit_marker(value)
            if marker:
                candidates.append((k, h, marker))
    if candidates:
        k, h, marker = max(candidates, key=lambda x: (x[0], x[1]))
        return marker
    return None


def _range_for_column(matrix: list[list[Any]], header_start: int, data_row: int, ci: int) -> tuple[tuple[float, float | None], str] | None:
    for h in range(data_row - 1, header_start - 1, -1):
        row = matrix[h] if h < len(matrix) else []
        if ci >= len(row):
            continue
        text = _strict_text(row[ci])
        rng = _strict_weight_range(text)
        if rng:
            return rng, text
    return None


def _strict_collected_candidates(company: str, origin: str, destination: str) -> list[dict[str, Any]]:
    key = (company, normalize_city(origin), normalize_city(destination), *_collected_results_stamp())
    cached = _COLLECTED_TARIFF_CACHE.get(key)
    if cached is not None:
        return [dict(x) for x in cached]

    dest_key = _werner_city_key(normalize_city(destination))
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for meta, matrix in _collected_sheet_groups(company, origin):
        for ri, row in enumerate(matrix):
            if not row:
                continue
            # Horizontal layout: destination is a row, tariff tiers are columns.
            dest_cols = [ci for ci, value in enumerate(row[:14]) if dest_key and _werner_city_key(value) == dest_key]
            if not dest_cols:
                dest_cols = [ci for ci, value in enumerate(row[:14]) if dest_key and _werner_city_key(value).startswith(dest_key)]
            if dest_cols:
                header_start = max(0, ri - 24)
                for ci, raw_value in enumerate(row):
                    value = _strict_number(raw_value)
                    if value is None:
                        continue
                    unit = _unit_for_column(matrix, header_start, ri, ci)
                    if unit == "minimum":
                        cand = {"mode": "minimum", "low": None, "high": None, "value": value, "header": "МИН"}
                    elif unit in {"per_kg", "fixed"}:
                        parsed = _range_for_column(matrix, header_start, ri, ci)
                        if not parsed:
                            continue
                        (low, high), header = parsed
                        cand = {"mode": unit, "low": low, "high": high, "value": value, "header": header}
                    else:
                        continue
                    # Plausibility is intentionally broad; semantics come from
                    # explicit headers, not from this numeric guard.
                    if cand["mode"] == "per_kg" and not (0 < value < 10000):
                        continue
                    sig = (meta.get("source_id"), meta.get("file"), meta.get("sheet"), cand["mode"], cand.get("low"), cand.get("high"), value)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    candidates.append({**cand, "source_id": meta.get("source_id"), "file": meta.get("file"), "sheet": meta.get("sheet"), "source_url": meta.get("url"), "source_title": meta.get("source_title")})

            # Transposed layout: destination is a column header, tariff tiers are rows.
            dest_header_cols = [ci for ci, value in enumerate(row) if dest_key and _werner_city_key(value) == dest_key]
            for ci in dest_header_cols:
                for rj in range(ri + 1, min(len(matrix), ri + 180)):
                    data = matrix[rj]
                    if not data or ci >= len(data):
                        continue
                    value = _strict_number(data[ci])
                    if value is None:
                        continue
                    left_context = data[:min(ci, 14)]
                    rng_part = next(((rng, _strict_text(v)) for v in left_context if (rng := _strict_weight_range(v)) is not None), None)
                    context_text = " | ".join(_strict_text(v) for v in left_context if _strict_text(v))
                    marker = _strict_unit_marker(context_text)
                    if marker is None:
                        # The table's column group may be stated in a heading above.
                        marker = _unit_for_column(matrix, max(0, ri - 16), ri + 1, ci)
                    if marker == "minimum":
                        cand = {"mode": "minimum", "low": None, "high": None, "value": value, "header": "МИН"}
                    elif marker in {"per_kg", "fixed"} and rng_part:
                        rng, header = rng_part
                        cand = {"mode": marker, "low": rng[0], "high": rng[1], "value": value, "header": header}
                    else:
                        continue
                    sig = (meta.get("source_id"), meta.get("file"), meta.get("sheet"), cand["mode"], cand.get("low"), cand.get("high"), value)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    candidates.append({**cand, "source_id": meta.get("source_id"), "file": meta.get("file"), "sheet": meta.get("sheet"), "source_url": meta.get("url"), "source_title": meta.get("source_title")})

    # Route switching can touch hundreds of destinations in one browser session.
    # Keep this destination-level parser cache bounded so a long session cannot
    # accumulate thousands of candidate lists and degrade into swapping.
    if len(_COLLECTED_TARIFF_CACHE) >= 512:
        _COLLECTED_TARIFF_CACHE.pop(next(iter(_COLLECTED_TARIFF_CACHE)))
    _COLLECTED_TARIFF_CACHE[key] = [dict(x) for x in candidates]
    return candidates


def _select_strict_candidate(candidates: list[dict[str, Any]], weight: float, mode: str) -> dict[str, Any] | None:
    w = float(weight)
    matches: list[dict[str, Any]] = []
    for c in candidates:
        if c.get("mode") != mode:
            continue
        if mode == "minimum":
            matches.append(c); continue
        low = float(c.get("low") or 0.0)
        high = float(c["high"]) if isinstance(c.get("high"), (int, float)) else float("inf")
        if w >= low - 1e-9 and w <= high + 1e-9:
            matches.append(c)
    if not matches:
        return None
    return min(matches, key=lambda c: ((float(c["high"]) if isinstance(c.get("high"), (int, float)) else 1e12) - float(c.get("low") or 0), str(c.get("file") or "")))



def _all_weight_ranges_with_positions(text: str) -> list[tuple[int, int, float, float | None, str]]:
    """Return every explicitly printed weight interval with its text position."""
    raw = str(text or "").replace("−", "-").replace("–", "-").replace("—", "-")
    out: list[tuple[int, int, float, float | None, str]] = []
    # A weight number may contain a thousands separator ("3 000"), but ordinary
    # layout spaces between neighbouring columns must not be swallowed as part of
    # the same number.
    num = r"(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)"
    patterns = [
        (rf"(?i)(?:от|свыше)\s*({num})\s*(?:кг)?", "open"),
        (rf"(?i)({num})\s*(?:кг)?\s*(?:и\s*более|и\s*выше|\+)", "open"),
        (rf"(?i)от\s*({num})\s*(?:кг)?\s*до\s*({num})\s*(?:кг)?", "closed"),
        (rf"(?i)(?<!\d)({num})\s*-\s*({num})(?:\s*кг)?(?!\d)", "closed"),
        (rf"(?i)до\s*({num})\s*кг", "upto"),
    ]
    occupied: list[tuple[int,int]] = []
    for pat, kind in patterns:
        for m in re.finditer(pat, raw):
            if any(not (m.end() <= a or m.start() >= b) for a,b in occupied):
                continue
            try:
                if kind == "open":
                    low=float(m.group(1).replace('\u00a0','').replace(' ','')); high=None
                elif kind == "closed":
                    low=float(m.group(1).replace('\u00a0','').replace(' ','')); high=float(m.group(2).replace('\u00a0','').replace(' ',''))
                else:
                    low=0.0; high=float(m.group(1).replace('\u00a0','').replace(' ',''))
            except Exception:
                continue
            if high is not None and high < low:
                continue
            occupied.append((m.start(),m.end()))
            out.append((m.start(),m.end(),low,high,m.group(0)))
    return sorted(out, key=lambda x:x[0])

def _kit_source_files(origin: str) -> list[Path]:
    sid = "KIT001" if normalize_city(origin) == "Москва" else ("KIT002" if normalize_city(origin) == "Санкт-Петербург" else "")
    if not sid:
        return []
    try:
        results=core.load_results()
    except Exception:
        results={}
    out=[]
    for meta in results.get('files',[]):
        if not isinstance(meta,dict) or str(meta.get('source_id') or '') != sid:
            continue
        raw=str(meta.get('path') or '')
        paths=[Path(raw)] if raw else []
        if meta.get('file'): paths.append(core.DOWNLOAD_DIR/str(meta['file']))
        for path in paths:
            if path.exists() and path.suffix.lower()=='.pdf': out.append(path)
    out.extend(_bundled_source_files({sid}, {".pdf"}))
    unique={str(x.resolve()):x for x in out}
    return sorted(unique.values(),key=lambda x:x.stat().st_mtime,reverse=True)


_KIT_ROUTE_INDEX_CACHE: dict[str, dict[str, Any]] = {}


def _kit_route_index(path: Path, origin: str) -> dict[str, Any]:
    """Build/load all destination rows from one official KIT PDF in one pass."""
    fingerprint = _file_sha1(path)
    o = normalize_city(origin)
    key = f"{path.resolve()}|{fingerprint}|{o}"
    cached = _KIT_ROUTE_INDEX_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    from .cities import route_slug
    disk_path = CACHE_DIR / f"kit44_routes_{route_slug(o,o)}_{fingerprint}.json"
    disk = _json_cache_read(disk_path)
    if isinstance(disk, dict) and isinstance(disk.get("routes"), dict):
        _KIT_ROUTE_INDEX_CACHE[key] = disk
        return disk
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
    except Exception:
        return {"routes": {}, "file": path.name, "fingerprint": fingerprint}
    fixed_ranges = [(0.0, 1.0), (1.0, 5.0), (5.0, 15.0), (15.0, 35.0)]
    kg_ranges = [
        (3000.0, None), (2000.0, 2999.999), (1500.0, 1999.999),
        (1000.0, 1499.999), (750.0, 999.999), (500.0, 749.999),
        (250.0, 499.999), (150.0, 249.999), (100.0, 149.999), (35.0, 99.999),
    ]
    num_re = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[,.]\d+)?)(?!\d)")
    routes: dict[str, list[dict[str, Any]]] = {}
    for page_no, page in enumerate(reader.pages[:80], start=1):
        try:
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                text = page.extract_text() or ""
        except Exception:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            first_number = re.search(r"\d", line)
            if not first_number:
                continue
            label = line[:first_number.start()].strip()
            if not re.search(r"[А-Яа-яЁё]", label):
                continue
            tail = line[first_number.start():]
            toks = num_re.findall(tail)
            if len(toks) < 15:
                continue
            try:
                vals = [float(x.replace("\u00a0", "").replace(" ", "").replace(",", ".")) for x in toks]
            except Exception:
                continue
            d = normalize_city(label)
            if not d or d == o or d in routes:
                continue
            candidates: list[dict[str, Any]] = []
            for i, (low, high) in enumerate(fixed_ranges):
                candidates.append({"mode": "fixed", "low": low, "high": high, "value": vals[i], "header": f"{low:g}-{high:g} кг", "file": path.name, "page": page_no})
            candidates.append({"mode": "minimum", "low": None, "high": None, "value": vals[4], "header": "МИН", "file": path.name, "page": page_no})
            for j, (low, high) in enumerate(kg_ranges):
                label2 = f"от {low:g} кг" if high is None else f"{low:g}-{high:g} кг"
                candidates.append({"mode": "per_kg", "low": low, "high": high, "value": vals[5 + j], "header": label2, "file": path.name, "page": page_no})
            routes[d] = candidates
    result = {"routes": routes, "file": path.name, "fingerprint": fingerprint}
    _KIT_ROUTE_INDEX_CACHE[key] = result
    _json_cache_write(disk_path, result)
    return result


def kit_official_pdf_tariff(origin: str, destination: str, weight: float, minimum: bool=False) -> dict[str,Any] | None:
    """Read KIT's printed PDF grid using its stable 5 + 10 + 10 column schema.

    KIT splits several weight headers across two PDF lines, so the previous
    generic header-alignment parser often found the destination row but rejected
    every numeric cell. The official PDF has a stable row layout: four fixed
    shipment prices, minimum, ten RUB/kg tiers, then ten RUB/m3 tiers.
    """
    files = _kit_source_files(origin)
    if not files:
        return None
    o, d = normalize_city(origin), normalize_city(destination)
    candidates: list[dict[str, Any]] = []
    for path in files:
        if not _file_confirms_origin(path, o):
            continue
        index = _kit_route_index(path, o)
        candidates = [dict(x) for x in (index.get("routes") or {}).get(d, [])]
        if candidates:
            break

    mode = "minimum" if minimum else ("fixed" if float(weight) <= 35 else "per_kg")
    selected = _select_strict_candidate(candidates, float(weight), mode)
    if not selected:
        return None
    value = float(selected["value"])
    low, high = selected.get("low"), selected.get("high")
    header = str(selected.get("header") or "")
    base = {
        "company":"КИТ", "company_label":COMPANY_LABELS["КИТ"], "status":"ok", "currency":"RUB",
        "delivery_days_min":None, "delivery_days_max":None, "source_type":"Официальный PDF КИТ",
        "source_url":"https://tk-kit.ru/rates-new", "freshness":"collected_official_pdf",
        "tariff_range_low_kg":low, "tariff_range_high_kg":high, "range_value_repeated":True,
        "source_file":selected.get("file"), "source_page":selected.get("page"), "price_is_minimum":False,
        "message":f"Значение прочитано напрямую из официального PDF КИТ ({header}); весовые и объёмные колонки не смешиваются.",
    }
    if minimum:
        base.update({"price":round(value,2), "formula":"явно опубликованный минимум", "pricing_mode":"minimum"})
    elif mode == "per_kg":
        base.update({"price":round(float(weight)*value,2), "rate_per_kg":value, "formula":f"{float(weight):g} кг × {value:g} ₽/кг; колонка {header}", "pricing_mode":"per_kg"})
    else:
        base.update({"price":round(value,2), "formula":f"фиксированная стоимость {value:g} ₽; колонка {header}", "pricing_mode":"fixed_shipment"})
    return base


_NEWLINE_ROUTE_INDEX_CACHE: dict[str, dict[str, Any]] = {}


def _newline_source_files(origin: str) -> list[Path]:
    """Return only the New Line PDF that belongs to the selected origin.

    NL001 is Moscow, NL002 is Saint Petersburg.  Earlier builds downloaded
    NL002 successfully but never read it; they always consulted the bundled
    Moscow snapshot afterwards, making the reverse route permanently blank.
    """
    o = normalize_city(origin)
    sid = "NL001" if o == "Москва" else ("NL002" if o == "Санкт-Петербург" else "")
    if not sid:
        return []
    return _source_files_for_ids({sid}, {".pdf"})


def _newline_number(token: str) -> float | None:
    text = str(token or "").replace("\u00a0", " ").strip()
    text = re.sub(r"\s+", "", text).replace(",", ".")
    try:
        value = float(text)
    except Exception:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _newline_parse_layout_text(text: str, *, source_file: str = "", page_no: int = 1) -> dict[str, list[dict[str, Any]]]:
    """Parse New Line's published PDF row layout without flattening columns.

    The official PDF prints: destination, delivery time, minimum shipment and
    RUB/kg columns up to 250/750/1250/2500/5000 kg (some editions also expose
    10 000+ columns).  Volume totals printed on the following line are ignored.
    """
    routes: dict[str, list[dict[str, Any]]] = {}
    # The first numeric token after delivery time is the minimum. Every token
    # ending in "руб." after it on the same physical line is a RUB/kg rate.
    row_re = re.compile(
        r"^\s*(?P<city>.*?)\s{2,}(?P<days>(?:\d+(?:\s*[-–—]\s*\d+)?\s*дн\.|по\s+запросу))\s{2,}"
        r"(?P<minimum>\d[\d\s\u00a0]*(?:[,.]\d+)?)\s*руб\.\s{2,}(?P<tail>.+)$",
        flags=re.I,
    )
    rate_re = re.compile(r"(?<!\d)(\d+(?:[,.]\d+)?)\s*руб\.", flags=re.I)
    limits = [250.0, 750.0, 1250.0, 2500.0, 5000.0, 10000.0, None]
    pending_hyphen: tuple[str, list[dict[str, Any]]] | None = None

    for raw in str(text or "").splitlines():
        m = row_re.match(raw)
        if m:
            city_raw = re.sub(r"\s+", " ", m.group("city")).strip()
            minimum = _newline_number(m.group("minimum"))
            rate_values = [_newline_number(x) for x in rate_re.findall(m.group("tail"))]
            rates = [float(v) for v in rate_values if isinstance(v, (int, float)) and v > 0]
            if minimum is None or not rates:
                pending_hyphen = None
                continue
            candidates: list[dict[str, Any]] = [{
                "mode": "minimum", "low": None, "high": None, "value": float(minimum),
                "header": "МИН", "file": source_file, "page": page_no,
                "delivery_days": m.group("days").strip(),
            }]
            low = 0.0
            for idx, rate in enumerate(rates[:len(limits)]):
                high = limits[idx]
                label = f"до {high:g} кг" if high is not None else f"от {low:g} кг"
                candidates.append({
                    "mode": "per_kg", "low": low, "high": high, "value": float(rate),
                    "header": label, "file": source_file, "page": page_no,
                    "delivery_days": m.group("days").strip(),
                })
                if high is not None:
                    low = high
            if city_raw.endswith("-"):
                pending_hyphen = (city_raw, candidates)
                continue
            city = normalize_city(city_raw)
            if city:
                routes[city] = candidates
            pending_hyphen = None
            continue

        # PDF layout can split "Санкт-Петербург" over two physical lines.
        # The continuation line starts with the remainder and then volume totals.
        if pending_hyphen:
            prefix, candidates = pending_hyphen
            continuation = raw.strip()
            first_digit = re.search(r"\d", continuation)
            continuation_city = (continuation[:first_digit.start()] if first_digit else continuation).strip()
            if continuation_city and re.search(r"[А-Яа-яЁё]", continuation_city):
                city = normalize_city(prefix + continuation_city)
                if city:
                    routes[city] = candidates
            else:
                city = normalize_city(prefix.rstrip("-"))
                if city:
                    routes[city] = candidates
            pending_hyphen = None

    return routes


def _newline_route_index(path: Path, origin: str) -> dict[str, Any]:
    fingerprint = _file_sha1(path)
    o = normalize_city(origin)
    key = f"{path.resolve()}|{fingerprint}|{o}"
    cached = _NEWLINE_ROUTE_INDEX_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    disk_path = CACHE_DIR / f"newline_routes_{'msk' if o == 'Москва' else 'spb'}_{fingerprint}.json"
    disk = _json_cache_read(disk_path)
    if isinstance(disk, dict) and isinstance(disk.get("routes"), dict):
        _NEWLINE_ROUTE_INDEX_CACHE[key] = disk
        return disk
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
    except Exception:
        return {"routes": {}, "file": path.name, "fingerprint": fingerprint}
    routes: dict[str, list[dict[str, Any]]] = {}
    for page_no, page in enumerate(reader.pages[:80], start=1):
        try:
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                text = page.extract_text() or ""
        except Exception:
            continue
        routes.update(_newline_parse_layout_text(text, source_file=path.name, page_no=page_no))
    result = {"routes": routes, "file": path.name, "fingerprint": fingerprint}
    _NEWLINE_ROUTE_INDEX_CACHE[key] = result
    _json_cache_write(disk_path, result)
    return result


def newline_official_pdf_tariff(origin: str, destination: str, weight: float, minimum: bool = False) -> dict[str, Any] | None:
    """Read the correct direction-specific official New Line PDF (NL001/NL002)."""
    o, d, w = normalize_city(origin), normalize_city(destination), float(weight)
    candidates: list[dict[str, Any]] = []
    for path in _newline_source_files(o):
        index = _newline_route_index(path, o)
        candidates = [dict(x) for x in (index.get("routes") or {}).get(d, [])]
        if candidates:
            break
    if not candidates:
        return None
    mode = "minimum" if minimum else "per_kg"
    selected = _select_strict_candidate(candidates, w, mode)
    if not selected:
        return None
    value = float(selected["value"])
    base = {
        "company": "Новая Линия", "company_label": COMPANY_LABELS["Новая Линия"], "status": "ok",
        "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": "Официальный PDF Новая Линия", "source_url": "https://tknl.ru/price/",
        "freshness": "collected_official_pdf", "source_file": selected.get("file"),
        "source_page": selected.get("page"), "price_is_minimum": False,
        "tariff_range_low_kg": selected.get("low"), "tariff_range_high_kg": selected.get("high"),
        "range_value_repeated": True,
        "message": f"Строка {o} → {d} прочитана из отдельного официального PDF для города отправления {o}; московский прайс для обратного направления не используется.",
    }
    days = str(selected.get("delivery_days") or "")
    nums = [int(x) for x in re.findall(r"\d+", days)]
    if nums:
        base["delivery_days_min"], base["delivery_days_max"] = min(nums), max(nums)
    if minimum:
        base.update({"price": round(value, 2), "pricing_mode": "minimum", "formula": "опубликованная минимальная стоимость"})
    else:
        floor_row = _select_strict_candidate(candidates, w, "minimum")
        floor = float(floor_row["value"]) if floor_row else 0.0
        total = max(floor, w * value)
        base.update({
            "price": round(total, 2), "rate_per_kg": value, "minimum_charge": floor,
            "pricing_mode": "per_kg", "formula": f"max(минимум {floor:g} ₽; {w:g} кг × {value:g} ₽/кг)",
        })
    return base


DIRECT_TABLE_COMPANIES = {"Werner", "ПЭК", "Возовоз", "Байкал Сервис", "Главтрасса"}
_PEK_ROUTE_FILE_CACHE: dict[str, dict[str, Any]] = {}
_PEK_INDEX_CACHE: dict[str, dict[str, Any]] = {}


def _pek_route_index(path: Path, origin: str = "Москва") -> dict[str, Any]:
    """Build/load a compact route index for PEK's large consolidated workbook.

    The official workbook contains tens of thousands of rows. Reading it for
    every destination made route switching look frozen. We scan it once per file
    version, keep only the requested origin route rows, and persist the compact
    index in runtime/cache.
    """
    fingerprint = _file_sha1(path)
    from .cities import route_slug
    origin=normalize_city(origin)
    origin_key=route_slug(origin,origin)
    key = f"{path.resolve()}|{fingerprint}|{origin_key}"
    cached = _PEK_INDEX_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    disk_path = CACHE_DIR / f"pek44_routes_{origin_key}_{fingerprint}.json"
    disk = _json_cache_read(disk_path)
    if isinstance(disk, dict) and isinstance(disk.get("routes"), dict):
        _PEK_INDEX_CACHE[key] = disk
        return disk
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb["Перевозка"] if "Перевозка" in wb.sheetnames else wb[wb.sheetnames[0]]
        iterator = ws.iter_rows(min_row=20, max_col=21, values_only=True)
        group_row = list(next(iterator))
        headers = list(next(iterator))
        routes: dict[str, Any] = {}
        for row in iterator:
            if len(row) < 3:
                continue
            o = normalize_city(row[1])
            if o != origin:
                continue
            d = normalize_city(row[2])
            if not d:
                continue
            values = list(row)
            candidates: list[dict[str, Any]] = []
            active_group = ""
            for ci in range(3, min(21, len(values))):
                value = _strict_number(values[ci])
                if value is None:
                    continue
                header = _strict_text(headers[ci] if ci < len(headers) else "")
                printed_group = _strict_text(group_row[ci] if ci < len(group_row) else "")
                if printed_group:
                    active_group = printed_group
                group = active_group
                if ci == 8 or "мин" in group.lower():
                    candidates.append({"mode": "minimum", "value": value, "header": header or "МИН"})
                    continue
                rng = _strict_weight_range(header)
                if not rng:
                    continue
                mode = "per_kg" if "1 кг" in group.lower() or "за 1 кг" in group.lower() else "fixed"
                candidates.append({"mode": mode, "low": rng[0], "high": rng[1], "value": value, "header": header})
            if candidates:
                routes[f"{o}|{d}"] = candidates
        wb.close()
    except Exception:
        return {"routes": {}, "file": path.name, "fingerprint": fingerprint}
    result = {"routes": routes, "file": path.name, "fingerprint": fingerprint}
    _PEK_INDEX_CACHE[key] = result
    _json_cache_write(disk_path, result)
    return result


def pek_official_xlsx_tariff(origin: str, destination: str, weight: float, minimum: bool = False) -> dict[str, Any] | None:
    """Read one exact route row from PEK's official consolidated workbook.

    The workbook has explicit origin/destination columns, five fixed shipment
    columns, a minimum column and twelve RUB/kg columns. Values and boundaries
    are read from the downloaded file; no route tariff is embedded in code.
    """
    files = _source_files_for_ids({"PEK004"}, {".xlsx"})
    if not files:
        return None
    path = files[0]
    o, d = normalize_city(origin), normalize_city(destination)
    index = _pek_route_index(path, o)
    candidates = [dict(x) for x in (index.get("routes") or {}).get(f"{o}|{d}", [])]
    if not candidates:
        return None
    cached = {"candidates": candidates, "file": index.get("file") or path.name}
    candidates = list(cached.get("candidates") or [])
    minimum_candidate = _select_strict_candidate(candidates, float(weight), "minimum")
    if minimum:
        selected = minimum_candidate
        pricing_mode = "minimum"
    elif float(weight) <= 50:
        selected = _select_strict_candidate(candidates, float(weight), "fixed")
        pricing_mode = "fixed_shipment"
        if selected is None:
            selected = _select_strict_candidate(candidates, float(weight), "per_kg")
            pricing_mode = "shipment_total"
    else:
        selected = _select_strict_candidate(candidates, float(weight), "per_kg")
        pricing_mode = "per_kg"
    if not selected:
        return None
    value = float(selected["value"])
    base = {
        "company": "ПЭК", "company_label": COMPANY_LABELS["ПЭК"], "status": "ok", "currency": "RUB",
        "source_type": "Официальный XLSX ПЭК", "source_url": "https://pecom.ru/business/rates/trucking/",
        "freshness": "collected_official_file", "source_file": cached.get("file"),
        "tariff_range_low_kg": selected.get("low"), "tariff_range_high_kg": selected.get("high"),
        "range_value_repeated": True, "price_is_minimum": False,
        "message": f"Строка {o} → {d} прочитана из официальной сводной XLSX-таблицы ПЭК; колонка «{selected.get('header') or 'МИН'}».",
    }
    if pricing_mode == "minimum":
        base.update({"price": value, "pricing_mode": pricing_mode, "formula": "опубликованная минимальная стоимость"})
    elif pricing_mode == "fixed_shipment":
        base.update({"price": value, "pricing_mode": pricing_mode, "formula": f"фиксированная стоимость {value:g} ₽ за отправку"})
    elif pricing_mode == "shipment_total":
        floor = float(minimum_candidate["value"]) if minimum_candidate else 0.0
        total = max(floor, float(weight) * value)
        base.update({"price": round(total, 2), "pricing_mode": pricing_mode, "formula": f"max(минимум {floor:g} ₽; {float(weight):g} кг × {value:g} ₽/кг)"})
    else:
        base.update({"price": round(float(weight) * value, 2), "rate_per_kg": value, "pricing_mode": pricing_mode, "formula": f"{float(weight):g} кг × {value:g} ₽/кг"})
    return base


def direct_collected_table_tariff(company: str, origin: str, destination: str, weight: float, minimum: bool = False) -> dict[str, Any] | None:
    """Parse the downloaded official source file itself, preserving table geometry.

    This is the common path for carriers whose sites publish HTML/XLS/XLSX/PDF
    tariff matrices.  One printed source range is copied to every customer-grid
    row it covers; no calculator total is converted into a ₽/kg rate.
    """
    if company not in DIRECT_TABLE_COMPANIES:
        return None
    if company == "ПЭК":
        return pek_official_xlsx_tariff(origin, destination, weight, minimum=minimum)
    ids = _source_ids_for_origin(company, origin) or set()
    if not ids:
        return None
    # PDF layout needs a carrier-specific parser (KIT and Dellin have one).
    # Passing PDF/HTML error bodies to the spreadsheet parser caused repeated
    # "invalid pdf header" work and froze reverse-route rendering.
    files = [
        path for path in _source_files_for_ids(ids, {".xlsx", ".xls", ".csv", ".html", ".htm", ".bin"})
        if _file_confirms_origin(path, origin)
    ]
    candidates: list[dict[str, Any]] = []
    for path in files:
        candidates.extend(_werner_candidates_from_file(path, destination))
    mode = "minimum" if minimum else ("fixed" if float(weight) <= 50 else "per_kg")
    selected = _werner_select_candidate(candidates, float(weight), mode)
    if not selected:
        return None
    value = float(selected["value"]); low = selected.get("low"); high = selected.get("high")
    header = str(selected.get("header") or "опубликованная ступень")
    source_url = PUBLIC_SOURCE_URLS.get(company) or str(selected.get("source_url") or "")
    base = {
        "company": company, "company_label": COMPANY_LABELS.get(company, company), "status": "ok",
        "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": f"Официальный тарифный файл {COMPANY_LABELS.get(company, company)}",
        "source_url": source_url, "freshness": "collected_official_file",
        "price_is_minimum": False, "tariff_range_low_kg": low, "tariff_range_high_kg": high,
        "range_value_repeated": True, "source_file": selected.get("file"), "source_sheet": selected.get("sheet"),
        "message": f"Значение прочитано непосредственно из опубликованной тарифной колонки ({header}). Одна исходная ступень повторяется во всех строках единой сетки, которые она покрывает.",
    }
    if mode == "minimum":
        base.update({"price": round(value, 2), "pricing_mode": "minimum", "formula": "явно опубликованный минимум"})
    elif mode == "per_kg":
        base.update({"price": round(float(weight) * value, 2), "rate_per_kg": value, "pricing_mode": "per_kg", "formula": f"{float(weight):g} кг × {value:g} ₽/кг; колонка {header}"})
    else:
        base.update({"price": round(value, 2), "pricing_mode": "fixed_shipment", "formula": f"фиксированная стоимость {value:g} ₽; колонка {header}"})
    return base


def collected_published_tariff(company: str, origin: str, destination: str, weight: float, minimum: bool = False) -> dict[str, Any] | None:
    """Use a value only when a collected official table explicitly prints it."""
    # Large PEK XLSX and KIT PDFs have geometry-aware indexes. Running the
    # generic parser first needlessly scans thousands of cells on every new route.
    if company == "ПЭК":
        return pek_official_xlsx_tariff(origin, destination, weight, minimum=minimum)
    if company == "КИТ":
        return kit_official_pdf_tariff(origin, destination, weight, minimum=minimum)
    if company == "Новая Линия":
        exact_newline = newline_official_pdf_tariff(origin, destination, weight, minimum=minimum)
        if exact_newline:
            return exact_newline
    if company == "Возовоз" and normalize_city(origin) == "Санкт-Петербург":
        # The bundled VOZ001 export is explicitly a Moscow-origin price list.
        # Never parse it (or its flattened runtime rows) as the reverse route.
        return direct_collected_table_tariff(company, origin, destination, weight, minimum=minimum)
    candidates = _strict_collected_candidates(company, origin, destination)
    mode = "minimum" if minimum else ("fixed" if float(weight) <= 50 else "per_kg")
    selected = _select_strict_candidate(candidates, float(weight), mode)
    if not selected:
        direct = direct_collected_table_tariff(company, origin, destination, weight, minimum=minimum)
        if direct:
            return direct
        if company == "КИТ":
            return kit_official_pdf_tariff(origin, destination, weight, minimum=minimum)
        return None
    value = float(selected["value"])
    low, high = selected.get("low"), selected.get("high")
    source_url = str(selected.get("source_url") or PUBLIC_SOURCE_URLS.get(company) or "")
    header = str(selected.get("header") or "опубликованная ступень")
    base = {
        "company": company, "company_label": COMPANY_LABELS.get(company, company), "status": "ok",
        "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
        "source_type": "Официальный загруженный прайс", "source_url": source_url,
        "freshness": "collected_official_file", "price_is_minimum": False,
        "tariff_range_low_kg": low, "tariff_range_high_kg": high,
        "range_value_repeated": True, "source_file": selected.get("file"), "source_sheet": selected.get("sheet"),
        "message": f"Значение прочитано напрямую из официального прайса ({header}). Одна исходная весовая колонка повторяется во всех строках единой сетки, которые она покрывает.",
    }
    if mode == "minimum":
        base.update({"price": round(value, 2), "formula": "явно опубликованный минимум", "price_is_minimum": False, "pricing_mode": "minimum"})
    elif mode == "per_kg":
        base.update({"price": round(float(weight) * value, 2), "rate_per_kg": value, "formula": f"{float(weight):g} кг × {value:g} ₽/кг; опубликованная ступень {header}", "pricing_mode": "per_kg"})
    else:
        base.update({"price": round(value, 2), "formula": f"фиксированная стоимость {value:g} ₽; опубликованная ступень {header}", "pricing_mode": "fixed_shipment"})
    return base


def werner_bucket_fallback_tariff(origin: str, destination: str, target_weight: float) -> dict[str, Any]:
    """Do not manufacture a Werner kg rate from a calculator total.

    The official Werner table/file is the only source for heavy tariff columns.
    If it has not been loaded/parsing failed, the UI must say so instead of
    showing ``calculator price / weight`` as though it were printed in the tariff.
    """
    return {
        **public_document_result("Werner", COMPANY_LABELS["Werner"], origin, destination, float(target_weight), 0.0),
        "message": "У Werner опубликован полный тарифный прайс. Если здесь нет числа, соответствующая колонка файла ещё не загружена/не распознана: итог калькулятора не делится на вес и не подменяет цену из прайса. Нажмите «Обновить прайсы».",
        "source_url": "https://wernerus.ru/clients/prices/",
    }


_BAIKAL_MATRIX_SNAPSHOT_CACHE: dict[str, Any] | None = None


def _baikal_calculator_snapshot() -> dict[str, Any]:
    """Load exact public-calculator totals bundled with the project.

    These totals are useful only for the customer rows 0–50 kg, where the
    requested economic unit is RUB per shipment. They are never divided by the
    control weight to manufacture a RUB/kg tariff.
    """
    global _BAIKAL_MATRIX_SNAPSHOT_CACHE
    if _BAIKAL_MATRIX_SNAPSHOT_CACHE is not None:
        return dict(_BAIKAL_MATRIX_SNAPSHOT_CACHE)
    path = DATA_DIR / "source_files" / "baikal_moscow_spb_calculator_2026-09-01.json"
    if not path.exists():
        _BAIKAL_MATRIX_SNAPSHOT_CACHE = {}
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _BAIKAL_MATRIX_SNAPSHOT_CACHE = data if isinstance(data, dict) else {}
    except Exception:
        _BAIKAL_MATRIX_SNAPSHOT_CACHE = {}
    return dict(_BAIKAL_MATRIX_SNAPSHOT_CACHE)


def baikal_weight_matrix_tariff(origin: str, destination: str, weight: float) -> dict[str, Any]:
    """Strict Baikal Service adapter for the customer's weight-only matrix.

    Source semantics are deliberately split:
    * 0–50 kg: exact shipment totals from the official public calculator snapshot
      when the route/weight was actually captured;
    * 100 kg: the explicitly published RUB/kg column "до 100 кг";
    * 3000+ kg: the explicitly published RUB/kg column "от 3000 кг";
    * 200–2500 kg: blank unless an official source publishes a separate tier.

    This prevents two previous errors: requiring 25 m³ before the heavy weight
    column could be used, and reading flattened HTML text as if it were a table.
    """
    o, d, w = normalize_city(origin), normalize_city(destination), float(weight)
    source_url = "https://www.baikalsr.ru/business/prices/"

    if w <= 50.0 + 1e-9:
        snap = _baikal_calculator_snapshot()
        if normalize_city(str(snap.get("origin") or "")) == o and normalize_city(str(snap.get("destination") or "")) == d:
            totals = snap.get("totals") or {}
            key = str(int(w)) if abs(w - round(w)) < 1e-9 else f"{w:g}"
            raw = totals.get(key) if isinstance(totals, dict) else None
            if isinstance(raw, (int, float)) and float(raw) > 0:
                return {
                    "company": "Байкал Сервис", "company_label": COMPANY_LABELS["Байкал Сервис"], "status": "ok",
                    "price": round(float(raw), 2), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
                    "source_type": "Официальный публичный калькулятор Байкал Сервис — сохранённый точный ответ",
                    "source_url": str(snap.get("source") or "https://www.baikalsr.ru/tools/calculator/"),
                    "freshness": f"official_calculator_snapshot_{snap.get('captured_at') or '2026-09-01'}",
                    "pricing_mode": "shipment_total", "price_is_minimum": False,
                    "calculator_total_only": True, "control_volume_m3": w / 200.0,
                    "formula": f"точный итог официального калькулятора для {w:g} кг; без деления на вес",
                    "message": "Для диапазона до 50 кг используется сохранённый точный итог официального калькулятора в рублях за отправку. Значение не переводится в ₽/кг.",
                }
        return unavailable("Байкал Сервис", "Для этого маршрута/веса нет сохранённого точного итога официального калькулятора. Рекламная цена «от» не подменяет тариф диапазона.", "document_unavailable")

    row = BAIKAL_REFERENCE.get((o, d))
    if row and abs(w - 100.0) < 1e-9:
        rate = float(row[0])
        return {
            "company": "Байкал Сервис", "company_label": COMPANY_LABELS["Байкал Сервис"], "status": "ok",
            "price": round(w * rate, 2), "currency": "RUB", "rate_per_kg": rate,
            "source_type": "Официальная таблица тарифов Байкал Сервис", "source_url": source_url,
            "freshness": "official_public_table", "pricing_mode": "per_kg", "price_is_minimum": False,
            "tariff_range_low_kg": 0.0, "tariff_range_high_kg": 100.0, "range_value_repeated": True,
            "formula": f"{w:g} кг × {rate:g} ₽/кг; опубликованная колонка до 100 кг",
            "message": "Ставка прочитана из явной весовой колонки официальной таблицы «до 100 кг».",
        }
    if row and w >= 3000.0 - 1e-9:
        rate = float(row[1])
        return {
            "company": "Байкал Сервис", "company_label": COMPANY_LABELS["Байкал Сервис"], "status": "ok",
            "price": round(w * rate, 2), "currency": "RUB", "rate_per_kg": rate,
            "source_type": "Официальная таблица тарифов Байкал Сервис", "source_url": source_url,
            "freshness": "official_public_table", "pricing_mode": "per_kg", "price_is_minimum": False,
            "tariff_range_low_kg": 3000.0, "tariff_range_high_kg": None, "range_value_repeated": True,
            "formula": f"{w:g} кг × {rate:g} ₽/кг; опубликованная колонка от 3000 кг",
            "message": "Ставка прочитана из явной весовой колонки официальной таблицы «от 3000 кг». Объём не является условием для весовой матрицы.",
        }
    if row:
        return unavailable("Байкал Сервис", "Официальная публичная таблица публикует для этого маршрута только весовые колонки до 100 кг и от 3000 кг. Промежуточная ставка не интерполируется.", "document_unavailable")
    return unavailable("Байкал Сервис", "Выбранный маршрут отсутствует в открытой таблице весовых ставок Байкал Сервис; нужен официальный тарифный файл или настроенный REST API.", "document_unavailable")


def fast_matrix_item(company: str, origin: str, destination: str, weight: float, volume: float) -> dict[str, Any]:
    """Return the best exact/published value for one customer matrix point.

    v30 is strict about source semantics. Published tariff tables/rates have
    priority. Calculator/API totals may be shown as shipment totals in 0–50 kg
    rows, but are never divided by weight to manufacture a ₽/kg tariff.
    """
    w = float(weight)
    o, d = normalize_city(origin), normalize_city(destination)
    control_v = matrix_control_volume(w) if abs(float(volume)) < 1e-12 else float(volume)

    # Literal official conditions at the end of the published grid are data,
    # unlike technical placeholders. Do not invent a rate where the carrier
    # explicitly switches to a request/contract calculation.
    if (o, d) == ("Москва", "Санкт-Петербург"):
        if company == "ДЛ" and w > 10000:
            return {
                "company": company, "company_label": COMPANY_LABELS[company], "status": "official_condition",
                "price": None, "currency": "RUB", "official_condition": "расчёт по заявке",
                "source_type": "Официальный PDF Деловых Линий", "source_url": "https://www.dellin.ru/pricelist_pdf/",
                "freshness": "official_pdf", "formula": "межтерминальная весовая сетка опубликована до 10 000 кг",
                "message": "Для веса выше опубликованной межтерминальной сетки официальный PDF направляет в форму расчёта-заказа.",
            }
        if company == "Новая Линия" and w > 5000:
            return {
                "company": company, "company_label": COMPANY_LABELS[company], "status": "official_condition",
                "price": None, "currency": "RUB", "official_condition": "по запросу",
                "source_type": "Официальный PDF Новая Линия", "source_url": "https://tknl.ru/price/",
                "freshness": "official_pdf_2026-09-01", "formula": "в опубликованной строке ставки после 5000 кг не заполнены",
                "message": "В официальной PDF-строке Москва-Север → Санкт-Петербург числовая весовая ставка опубликована только до 5000 кг; следующие колонки оставлены перевозчиком без ставки.",
            }
        if company == "CTSgroup" and w > 3000:
            return {
                "company": company, "company_label": COMPANY_LABELS[company], "status": "official_condition",
                "price": None, "currency": "RUB", "official_condition": "договор",
                "source_type": "Официальный XLSX CTS Group", "source_url": "https://cts-group.ru/prices",
                "freshness": "official_xlsx_2026-04-01", "formula": "опубликованное условие «договор» для диапазона 3001+",
                "message": "В исходной ячейке официального XLSX для веса 3001+ буквально указано «договор».",
            }

    # Highest priority: a value literally printed in an official file/table
    # downloaded by «Обновить прайсы».  This path is shared by every company,
    # so Werner, ПЭК, КИТ, Возовоз and the remaining carriers follow the same
    # normalization rule instead of company-specific guesses.
    collected = collected_published_tariff(company, origin, destination, w)
    if collected:
        return collected

    # Route refresh may prefetch exact official calculator totals for carriers
    # that do not expose a convenient static reverse-direction tariff file.
    # These values are already complete shipment prices in RUB and are never
    # divided by weight to invent a per-kg tariff.
    if company in {"Werner", "Возовоз", "Главтрасса", "Байкал Сервис", "CTSgroup", "ДЛ", "Новая Линия"}:
        control_volume = matrix_control_volume(w)
        exact_cached = get_cached_company_result(
            result_cache_key(company, origin, destination, w, control_volume),
            max_age_seconds=24 * 3600,
        )
        if exact_cached and not exact_cached.get("price_is_minimum"):
            exact_cached = dict(exact_cached)
            exact_cached["status"] = "cached"
            exact_cached["freshness"] = "prefetched_exact_official_calculator"
            exact_cached["control_volume_m3"] = control_volume
            exact_cached["calculator_total_only"] = True
            exact_cached["message"] = (str(exact_cached.get("message") or "").rstrip(". ") + ". Точный итог официального калькулятора заранее загружен для этого направления.").strip()
            return exact_cached

    # The user's last successful Werner run on 2026-09-05 produced exact
    # official API control points. Ship those as a reproducible fallback so the
    # reverse route does not start blank before the first online refresh.
    if company == "Werner":
        verified = verified_profile_snapshot(company, origin, destination, w, control_volume)
        if verified:
            return verified

    # A verified route-level public minimum is better than the false statement
    # «прайс не загружен».  It remains a lower bound (price_is_minimum=True),
    # so counters, graph and Excel never confuse it with an exact weight tariff.
    route_minimum = spb_msk_public_route_minimum(company, origin, destination)
    if route_minimum and company in {"Возовоз", "Байкал Сервис"}:
        return route_minimum

    # Baikal has a compact public HTML table with route text plus two explicit
    # weight columns. Use its dedicated adapter only after the exact reverse-
    # route calculator cache above. Previously this early return made a valid
    # Санкт-Петербург → Москва calculator result impossible to display.
    if company == "Байкал Сервис":
        return baikal_weight_matrix_tariff(origin, destination, w)

    if company == "Werner":
        published = werner_official_tariff(origin, destination, w)
        if published:
            return published
        fixed = werner_fixed_tariff(origin, destination, w)
        if fixed:
            return fixed
        if w > 50:
            return werner_bucket_fallback_tariff(origin, destination, w)
        return public_document_result(company, COMPANY_LABELS[company], origin, destination, w, control_v)

    if company == "Рейл Континент":
        rail = rail_weight_tariff(origin, destination, w)
        if rail:
            return rail

    if company == "ДЛ":
        if dellin_pdf_for_origin(origin):
            return calculate_dellin(origin, destination, w, 0.0)
        route_minimum = spb_msk_public_route_minimum(company, origin, destination)
        if route_minimum:
            return route_minimum
        return public_document_result(company, COMPANY_LABELS[company], origin, destination, w, 0.0)

    if company == "ПЭК":
        row = PEK_HEAVY_WEIGHT_RATES.get((o, d))
        if row and w >= 3000:
            rate = float(row["rate_per_kg"]); minimum = float(row["minimum"]); price = max(minimum, w * rate)
            return {
                "company": "ПЭК", "company_label": COMPANY_LABELS["ПЭК"], "status": "ok",
                "price": round(price), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
                "source_type": "Официальная маршрутная ставка ПЭК", "source_url": str(row["url"]),
                "freshness": "published_2026-08-26", "rate_per_kg": rate,
                "formula": f"{w:g} кг × {rate:g} ₽/кг (опубликовано для грузов от 3000 кг; минимум {minimum:g} ₽)",
                "message": "Весовая ставка взята с официальной страницы маршрута ПЭК.", "price_is_minimum": False,
                "tariff_range_low_kg": 3000.0, "tariff_range_high_kg": None, "range_value_repeated": True,
            }
        if w > 50:
            doc = public_document_result(company, COMPANY_LABELS[company], origin, destination, w, 0.0)
            doc["message"] = "ПЭК публикует XLS/XLSX тарифы по филиалу. Для этой строки нужна ставка непосредственно из файла; после чтения она переводится в стоимость отправки в ₽ по контрольному весу. Нажмите «Обновить прайсы», если файл ещё не загружен."
            return doc
        return public_document_result(company, COMPANY_LABELS[company], origin, destination, w, control_v)

    if company == "КИТ":
        fallback = KIT_PUBLIC_FALLBACK.get((o, d))
        # The route page exposes a promotional/summary "от X ₽/кг" value, not
        # the boundaries of every source weight column.  It is acceptable as a
        # shipment calculation for the 0–50 kg section when combined with the
        # published minimum, but it must never be stretched over all heavy rows.
        if w <= 50 and fallback:
            rate = float(fallback["rate_per_kg"]); minimum = float(fallback["minimum"]); price = max(minimum, w * rate)
            return {
                "company": "КИТ", "company_label": COMPANY_LABELS["КИТ"], "status": "ok",
                "price": round(price), "currency": "RUB", "delivery_days_min": None, "delivery_days_max": None,
                "source_type": "Официальная маршрутная страница КИТ", "source_url": "https://tk-kit.ru/route/moskva",
                "freshness": "published_route_summary",
                "formula": f"max({w:g} кг × {rate:g} ₽/кг, опубликованный минимум {minimum:g} ₽)",
                "message": "Для лёгкой отправки используется опубликованный маршрутный ориентир и минимум. Для тяжёлых строк нужна соответствующая весовая колонка официального PDF-прайса, после чего показывается итог в ₽.",
                "price_is_minimum": False, "calculator_total_only": True,
            }
        if w > 50:
            doc = public_document_result(company, COMPANY_LABELS[company], origin, destination, w, 0.0)
            doc["message"] = "КИТ публикует полный PDF-прайс по городу отправления. Для тяжёлой строки берётся только ставка из соответствующей весовой колонки PDF и пересчитывается в итоговую стоимость отправки в ₽; значение «от 14 ₽/кг» с маршрутной страницы не размазывается на все веса."
            return doc

    if company == "Возовоз":
        if w > 50:
            doc = public_document_result(company, COMPANY_LABELS[company], origin, destination, w, 0.0)
            doc["message"] = "Возовоз формирует официальный тарифный Excel/архив. Для весовых строк используется только подтверждённая ставка из этого прайса и показывается стоимость отправки в ₽; итог API не используется для искусственного восстановления ставки."
            return doc
        return public_document_result(company, COMPANY_LABELS[company], origin, destination, w, control_v)
    if company == "Главтрасса":
        published = glavtrassa_official_tariff(origin, destination, w)
        if published:
            return published
        if w > 50:
            return unavailable(company, "Не удалось прочитать соответствующую весовую колонку опубликованного прайса Главтрассы. Неподтверждённое значение не подставляется; строка остаётся без цены.", "document_unavailable")
        return public_document_result(company, COMPANY_LABELS[company], origin, destination, w, control_v)
    if company == "Пролайн":
        return calculate_open_tariff(company, proline_tariff, origin, destination, w, 0.0)
    if company == "Фортуна":
        return calculate_open_tariff(company, fortuna_tariff, origin, destination, w, 0.0)
    if company == "ФастТранс":
        return calculate_open_tariff(company, fasttrans_tariff, origin, destination, w, 0.0)
    if company == "АТЭК":
        return calculate_open_tariff(company, atek_tariff, origin, destination, w, 0.0)
    if company == "БСК":
        return calculate_open_tariff(company, bsk_tariff, origin, destination, w, 0.0)
    if company == "ЭкспедицияПлюс":
        return calculate_open_tariff(company, expeditionplus_tariff, origin, destination, w, 0.0)
    if company == "CTSgroup":
        return calculate_open_tariff(company, ctsgroup_tariff, origin, destination, w, 0.0)
    if company == "Мейджик":
        return calculate_open_tariff(company, magic_tariff, origin, destination, w, 0.0)
    if company == "Новая Линия":
        return calculate_open_tariff(company, newline_tariff, origin, destination, w, 0.0)
    return unavailable(company, "Для этой контрольной точки не найден точный официальный тариф или расчёт.")


_PROFILE_MATRIX_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_PROFILE_MATRIX_CACHE_LOCK = threading.Lock()
_PROFILE_MATRIX_BUILD_LOCKS: dict[tuple[Any, ...], threading.Lock] = {}

def _profile_matrix_cache_key(origin: str, destination: str, requested: list[str]) -> tuple[Any, ...]:
    return (normalize_city(origin), normalize_city(destination), tuple(requested), *_collected_results_stamp())

def _profile_matrix_key_lock(key: tuple[Any, ...]) -> threading.Lock:
    with _PROFILE_MATRIX_CACHE_LOCK:
        lock = _PROFILE_MATRIX_BUILD_LOCKS.get(key)
        if lock is None:
            # Keep the auxiliary lock table bounded together with the data cache.
            if len(_PROFILE_MATRIX_BUILD_LOCKS) >= 48:
                _PROFILE_MATRIX_BUILD_LOCKS.pop(next(iter(_PROFILE_MATRIX_BUILD_LOCKS)))
            lock = threading.Lock()
            _PROFILE_MATRIX_BUILD_LOCKS[key] = lock
        return lock


def profile_matrix(origin: str, destination: str, selected_companies: list[str] | None = None) -> dict[str, Any]:
    origin = normalize_city(origin)
    destination = normalize_city(destination)
    requested = [c for c in COMPANIES if c in (selected_companies or COMPANIES)]
    cache_key = _profile_matrix_cache_key(origin, destination, requested)
    now = time.time()
    with _PROFILE_MATRIX_CACHE_LOCK:
        cached = _PROFILE_MATRIX_CACHE.get(cache_key)
        if cached and now - cached[0] <= 45:
            return cached[1]

    # Same-route /api/compare and /api/profile-matrix requests are often issued
    # together. Single-flight only this exact route key; another route gets its
    # own lock and is never forced to wait behind the previous selection.
    build_lock = _profile_matrix_key_lock(cache_key)
    with build_lock:
        now = time.time()
        with _PROFILE_MATRIX_CACHE_LOCK:
            cached = _PROFILE_MATRIX_CACHE.get(cache_key)
            if cached and now - cached[0] <= 45:
                return cached[1]

        def build_company_column(company: str) -> list[dict[str, Any]]:
            column: list[dict[str, Any]] = []
            for profile in COMMON_PROFILES:
                weight = float(profile["weight_kg"])
                if profile.get("is_minimum_profile"):
                    item = decorate_weight_only_item(explicit_minimum_charge(company, origin, destination), weight, True)
                else:
                    item = decorate_weight_only_item(fast_matrix_item(company, origin, destination, weight, 0.0), weight, False)
                item["control_weight_kg"] = weight
                column.append(item)
            return apply_customer_range_copy_rule(company, column)

        columns: dict[str, list[dict[str, Any]]] = {}
        # Matrix construction never performs network I/O. Official sites are fetched
        # by the route refresh endpoint, then all 29 rows are built from local files
        # and cached official snapshots.
        for company in requested:
            columns[company] = build_company_column(company)

        rows = []
        for idx, profile in enumerate(COMMON_PROFILES):
            items = [columns[c][idx] for c in requested]
            rows.append({
                "profile": profile,
                "items": items,
                "available_count": sum(1 for x in items if isinstance(x.get("comparison_value"), (int, float))),
            })
        result = {
            "origin": origin, "destination": destination,
            "profiles": rows, "selected_companies": requested,
            "methodology": "Единая схема по 29 диапазонам: во всех строках значение — стоимость отправки в ₽. Если источник публикует ставку ₽/кг, она сохраняется в метаданных и применяется к контрольному весу; фиксированные цены и калькуляторные итоги остаются рублёвыми суммами. МИН — только явно опубликованный минимум. Числовой тариф не интерполируется и не усредняется.",
        }
        with _PROFILE_MATRIX_CACHE_LOCK:
            if len(_PROFILE_MATRIX_CACHE) >= 32:
                oldest = min(_PROFILE_MATRIX_CACHE, key=lambda k: _PROFILE_MATRIX_CACHE[k][0])
                _PROFILE_MATRIX_CACHE.pop(oldest, None)
                _PROFILE_MATRIX_BUILD_LOCKS.pop(oldest, None)
            _PROFILE_MATRIX_CACHE[cache_key] = (time.time(), result)
        return result


_CUSTOMER_DESTINATION_CACHE: list[str] | None = None

def customer_destination_catalog() -> list[str]:
    """One destination catalog for both Moscow and Saint Petersburg.

    The customer's target matrix uses the same destination dictionary for both
    origins. Until the exact 127-city target workbook is supplied, we use the
    broad verified city dictionary extracted from the bundled official Dellin
    Moscow tariff and add Moscow itself for reverse (SPb -> Moscow) calculations.
    The origin city is removed by ``available_destinations``.
    """
    global _CUSTOMER_DESTINATION_CACHE
    if _CUSTOMER_DESTINATION_CACHE is not None:
        return list(_CUSTOMER_DESTINATION_CACHE)
    cities: list[str] = []
    custom_path = DATA_DIR / "customer_destinations.json"
    if custom_path.exists():
        try:
            custom = json.loads(custom_path.read_text(encoding="utf-8"))
            if isinstance(custom, list):
                cities.extend(str(x) for x in custom if str(x).strip())
        except Exception:
            pass
    if not cities:
        path = dellin_pdf_for_origin("Москва")
        if path:
            try:
                cities.extend(parse_dellin_tariffs(path, "Москва").keys())
            except Exception:
                pass
    cities = ["Москва", *DEFAULT_DESTINATIONS, *cities]
    seen: set[str] = set()
    out: list[str] = []
    for city in cities:
        name = normalize_city(city)
        if not name or name in seen:
            continue
        seen.add(name); out.append(name)
    _CUSTOMER_DESTINATION_CACHE = out
    return list(out)

def available_destinations(origin: str) -> list[str]:
    origin = normalize_city(origin)
    catalog = customer_destination_catalog()
    return [city for city in catalog if normalize_city(city) != origin]


def create_comparison_export(comparison: dict[str, Any]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    dark = "17202A"; blue = "2563EB"; border_color = "DDE3EA"; muted = "667085"
    green = "EAF8F2"; green_text = "147A58"; grey = "F4F6F8"; amber = "FFF7E6"
    thin = Side(style="thin", color=border_color)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def style_header(ws, row: int, start_col: int = 1, end_col: int | None = None) -> None:
        end_col = end_col or ws.max_column
        for col in range(start_col, end_col + 1):
            cell = ws.cell(row, col)
            cell.fill = PatternFill("solid", fgColor=dark)
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border

    def safe_sheet_name(company: str) -> str:
        name = "Исх_" + COMPANY_LABELS.get(company, company)
        name = re.sub(r"[\\/*?:\[\]]", "_", name)
        return name[:31]

    selected = [c for c in comparison.get("selected_companies", COMPANIES) if c in COMPANIES] or list(COMPANIES)
    matrix = profile_matrix(comparison["origin"], comparison["destination"], selected)
    raw_results = core.load_results()
    raw_rows = raw_results.get("rows") or []
    source_catalog = core.load_sources().get("sources", [])
    company_info = company_catalog()

    # 1) Long normalized table: one stable schema for every company and range.
    # This is the sheet intended for further analytics/import instead of manual
    # copy-paste from differently structured carrier price lists.
    unified = wb.create_sheet("Единый формат")
    unified.sheet_view.showGridLines = False
    unified["A1"] = "Единый формат тарифов конкурентов"
    unified["A1"].font = Font(size=18, bold=True, color=dark)
    unified["A2"] = f"{comparison['origin']} → {comparison['destination']} · 29 диапазонов × {len(selected)} компаний"
    unified["A2"].font = Font(size=11, bold=True, color=blue)
    unified["A3"] = "Одна схема для всех строк: Откуда / Куда / Компания / Диапазон / Тип тарифа / Ед. / Значение. Во всех строках значение — стоимость отправки в ₽; опубликованная ставка ₽/кг применяется к контрольному весу и сохраняется в метаданных."
    unified["A3"].font = Font(size=10, color=muted)
    unified_headers = ["Откуда", "Куда", "Компания", "Диапазон", "Тип тарифа", "Контрольный вес, кг", "Ед.", "Значение", "Статус", "Источник", "Свежесть", "URL", "Комментарий / формула"]
    unified.append([]); unified.append(unified_headers)
    unified_header_row = 5
    style_header(unified, unified_header_row)
    for row_obj in matrix["profiles"]:
        profile = row_obj["profile"]
        by_company = {x.get("company"): x for x in row_obj.get("items", [])}
        for company in selected:
            item = by_company.get(company) or {}
            value: Any = item.get("comparison_value")
            if value is None:
                if item.get("price_is_minimum") and isinstance(item.get("price"), (int, float)):
                    value = f"от {float(item['price']):g} ₽"
                else:
                    value = item.get("display_text") or "нет данных"
            unified.append([
                comparison["origin"], comparison["destination"], COMPANY_LABELS.get(company, company),
                profile.get("range_weight"), profile.get("tariff_type"),
                None if profile.get("is_minimum_profile") else profile.get("weight_kg"),
                profile.get("unit"), value, item.get("status"), item.get("source_type"),
                item.get("freshness"), item.get("source_url"),
                " · ".join(x for x in [item.get("message", ""), item.get("formula", "")] if x),
            ])
    for row in unified.iter_rows(min_row=unified_header_row + 1):
        for cell in row:
            cell.border = border; cell.alignment = Alignment(vertical="top", wrap_text=True)
        if isinstance(row[7].value, (int, float)):
            row[7].number_format = '#,##0.00 "₽"'
        else:
            row[7].fill = PatternFill("solid", fgColor=grey)
        if isinstance(row[11].value, str) and row[11].value.startswith("http"):
            row[11].hyperlink = row[11].value; row[11].style = "Hyperlink"
    unified.auto_filter.ref = f"A{unified_header_row}:M{unified.max_row}"
    unified.freeze_panes = "A6"
    for i, width in enumerate([18, 20, 24, 18, 24, 20, 10, 18, 20, 34, 22, 52, 72], 1):
        unified.column_dimensions[get_column_letter(i)].width = width

    # 2) Wide normalized result exactly in the customer's weight ranges.
    ws = wb.create_sheet("Нормализация")
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Нормализованные тарифы конкурентов"
    ws["A1"].font = Font(size=18, bold=True, color=dark)
    ws["A2"] = f"{comparison['origin']} → {comparison['destination']}"
    ws["A2"].font = Font(size=12, bold=True, color=blue)
    ws["A3"] = "Все диапазоны показывают полную стоимость отправки в ₽. МИН — только явно опубликованная минимальная стоимость. Если источник публикует ₽/кг, ставка умножается на контрольный вес строки и сохраняется в метаданных; неизвестные ступени не интерполируются."
    ws["A3"].font = Font(size=10, color=muted)
    headers = ["Диапазон", "Тип тарифа", "Контрольный вес, кг", "Ед."] + [COMPANY_LABELS[c] for c in selected]
    ws.append([]); ws.append(headers)
    header_row = 5
    style_header(ws, header_row)
    for row_obj in matrix["profiles"]:
        profile = row_obj["profile"]
        by_company = {x["company"]: x for x in row_obj["items"]}
        values = []
        for c in selected:
            item = by_company.get(c) or {}
            values.append(item.get("comparison_value") if item.get("comparison_value") is not None else item.get("display_text") or "нет данных")
        ws.append([profile["range_weight"], profile.get("tariff_type"), profile["weight_kg"] if not profile.get("is_minimum_profile") else None, profile.get("unit") or "₽", *values])
    for row in ws.iter_rows(min_row=header_row + 1):
        unit = row[3].value
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for cell in row[4:]:
            if isinstance(cell.value, (int, float)):
                cell.number_format = '#,##0.00 "₽"'
            else:
                cell.fill = PatternFill("solid", fgColor=grey)
                if cell.value is None:
                    cell.value = "нет данных"
    ws.freeze_panes = "E6"
    ws.column_dimensions["A"].width = 18; ws.column_dimensions["B"].width = 24; ws.column_dimensions["C"].width = 20; ws.column_dimensions["D"].width = 10
    for col in range(5, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18

    # 3) Current selected band — compact customer-facing slice.
    cur = wb.create_sheet("Выбранный диапазон")
    cur.sheet_view.showGridLines = False
    cur["A1"] = f"{comparison['origin']} → {comparison['destination']} · {comparison['range_weight']}"
    cur["A1"].font = Font(size=16, bold=True, color=dark)
    cur["A2"] = f"Единица сравнения: {comparison.get('comparison_unit', '₽')} · расчёт {comparison['calculated_at']}"
    cur["A2"].font = Font(size=10, color=muted)
    selected_profile = PROFILE_BY_ID.get(str(comparison.get("profile_id") or ""), {})
    cur_headers = ["Компания", "Тип тарифа", "Значение", "Ед.", "Статус", "Источник", "URL", "Комментарий / формула"]
    cur.append([]); cur.append(cur_headers); cur_header = 4
    style_header(cur, cur_header)
    for item in comparison["items"]:
        cur.append([
            item.get("company_label"), selected_profile.get("tariff_type"), item.get("comparison_value"), item.get("comparison_unit"),
            item.get("status"), item.get("source_type"), item.get("source_url"),
            " · ".join(x for x in [item.get("message", ""), item.get("formula", "")] if x),
        ])
    for row in cur.iter_rows(min_row=cur_header + 1):
        for cell in row:
            cell.border = border; cell.alignment = Alignment(vertical="top", wrap_text=True)
        if isinstance(row[2].value, (int, float)):
            row[2].number_format = '#,##0.00 "₽"'
        else:
            row[2].fill = PatternFill("solid", fgColor=grey)
        if isinstance(row[6].value, str) and row[6].value.startswith("http"):
            row[6].hyperlink = row[6].value; row[6].style = "Hyperlink"
    for i, width in enumerate([24, 24, 18, 10, 18, 34, 55, 75], 1): cur.column_dimensions[get_column_letter(i)].width = width
    cur.freeze_panes = "A5"

    # 4) One source/traceability sheet per company.
    for company in selected:
        sws = wb.create_sheet(safe_sheet_name(company))
        sws.sheet_view.showGridLines = False
        label = COMPANY_LABELS[company]
        info = company_info.get(company, {})
        sws["A1"] = label
        sws["A1"].font = Font(size=17, bold=True, color=dark)
        sws["A2"] = f"Маршрут нормализации: {comparison['origin']} → {comparison['destination']}"
        sws["A2"].font = Font(size=10, color=muted)
        sws["A3"] = f"Источник: {INTEGRATION_DESCRIPTIONS.get(company, ('—',''))[0]}"
        sws["A4"] = f"Покрытие: {info.get('coverage_label') or '—'}"
        if info.get("primary_url"):
            sws["A5"] = info.get("primary_url"); sws["A5"].hyperlink = info.get("primary_url"); sws["A5"].style = "Hyperlink"

        source_headers = ["Диапазон", "Тип тарифа", "Контрольный вес, кг", "Ед.", "Нормализованное значение", "Расчётная сумма, ₽", "Статус", "Источник", "Свежесть", "URL", "Формула / комментарий"]
        start_row = 7
        for col, value in enumerate(source_headers, 1): sws.cell(start_row, col, value)
        style_header(sws, start_row, 1, len(source_headers))
        r = start_row + 1
        for row_obj in matrix["profiles"]:
            profile = row_obj["profile"]
            item = next((x for x in row_obj["items"] if x.get("company") == company), {})
            sws.append([
                profile["range_weight"], profile.get("tariff_type"), None if profile.get("is_minimum_profile") else profile["weight_kg"],
                profile.get("unit"), item.get("comparison_value"), item.get("price"), item.get("status"),
                item.get("source_type"), item.get("freshness"), item.get("source_url"),
                " · ".join(x for x in [item.get("message", ""), item.get("formula", "")] if x),
            ])
            r += 1
        for row in sws.iter_rows(min_row=start_row + 1, max_row=r - 1):
            for cell in row:
                cell.border = border; cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(row[4].value, (int, float)):
                row[4].number_format = '#,##0.00 "₽"'
            if isinstance(row[5].value, (int, float)): row[5].number_format = '#,##0 "₽"'
            if isinstance(row[9].value, str) and row[9].value.startswith("http"):
                row[9].hyperlink = row[9].value; row[9].style = "Hyperlink"

        # Source catalog for the company.
        catalog_start = r + 2
        sws.cell(catalog_start, 1, "Каталог официальных источников")
        sws.cell(catalog_start, 1).font = Font(size=12, bold=True, color=dark)
        cat_headers = ["Документ", "Формат", "Покрытие", "Дата действия", "URL", "Инструкция"]
        for col, value in enumerate(cat_headers, 1): sws.cell(catalog_start + 1, col, value)
        style_header(sws, catalog_start + 1, 1, len(cat_headers))
        rr = catalog_start + 2
        for src in [x for x in source_catalog if x.get("company") == company]:
            sws.append([src.get("title"), src.get("document_format"), src.get("coverage"), src.get("effective_date"), src.get("reference_url") or src.get("url"), src.get("instruction")])
            if isinstance(sws.cell(rr, 5).value, str) and sws.cell(rr, 5).value.startswith("http"):
                sws.cell(rr, 5).hyperlink = sws.cell(rr, 5).value; sws.cell(rr, 5).style = "Hyperlink"
            rr += 1

        # Raw extracted document rows, when the collector has them.
        company_raw = [x for x in raw_rows if x.get("company") == company]
        raw_start = rr + 2
        sws.cell(raw_start, 1, "Извлечённые строки исходного файла")
        sws.cell(raw_start, 1).font = Font(size=12, bold=True, color=dark)
        raw_headers = ["Файл/лист", "Строка", "Исходный текст", "Мин. сумма", "Макс. сумма", "Формат", "URL"]
        for col, value in enumerate(raw_headers, 1): sws.cell(raw_start + 1, col, value)
        style_header(sws, raw_start + 1, 1, len(raw_headers))
        rr = raw_start + 2
        if company_raw:
            for raw in company_raw:
                sws.append([
                    " / ".join(x for x in [str(raw.get("file") or ""), str(raw.get("sheet") or "")] if x),
                    raw.get("row_index"), raw.get("raw_text"), raw.get("amount_min"), raw.get("amount_max"), raw.get("format"), raw.get("url"),
                ])
                if isinstance(sws.cell(rr, 7).value, str) and sws.cell(rr, 7).value.startswith("http"):
                    sws.cell(rr, 7).hyperlink = sws.cell(rr, 7).value; sws.cell(rr, 7).style = "Hyperlink"
                rr += 1
        else:
            sws.append(["—", None, "После «Обновить прайсы» здесь появятся строки, реально извлечённые из скачанного PDF/XLS/XLSX/HTML. Встроенная тарифная сетка выше уже используется для нормализации.", None, None, None, info.get("primary_url")])
        for row in sws.iter_rows(min_row=start_row, max_row=sws.max_row):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if cell.row not in {start_row, catalog_start + 1, raw_start + 1}: cell.border = border
        sws.freeze_panes = "A8"
        for i, width in enumerate([20, 24, 18, 12, 22, 20, 18, 34, 22, 55, 72], 1): sws.column_dimensions[get_column_letter(i)].width = width

    # 5) General source catalog.
    cat = wb.create_sheet("Каталог источников")
    cat_headers = ["Компания", "Документ", "Формат", "Покрытие", "Можно считать цену", "Дата действия", "Официальный URL", "Комментарий"]
    cat.append(cat_headers); style_header(cat, 1)
    coverage_names = core.load_sources().get("coverage_legend", {})
    for source in source_catalog:
        if source.get("company") not in selected:
            continue
        cat.append([
            COMPANY_LABELS.get(source.get("company"), source.get("company")), source.get("title"), source.get("document_format"),
            coverage_names.get(source.get("coverage"), source.get("coverage")), "Да" if source.get("usable_for_price") else "Нет",
            source.get("effective_date"), source.get("reference_url") or source.get("url"), source.get("instruction"),
        ])
    for row in cat.iter_rows(min_row=2):
        for cell in row: cell.border = border; cell.alignment = Alignment(vertical="top", wrap_text=True)
        if isinstance(row[6].value, str) and row[6].value.startswith("http"):
            row[6].hyperlink = row[6].value; row[6].style = "Hyperlink"
    for i, width in enumerate([22, 40, 18, 26, 18, 16, 58, 72], 1): cat.column_dimensions[get_column_letter(i)].width = width
    cat.freeze_panes = "A2"

    path = EXPORT_DIR / f"tariffs_weight_only_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(path)
    return path


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/document-sources")
def api_document_sources() -> dict[str, Any]:
    return {"companies": company_catalog(), "results_summary": core.load_results().get("summary", {})}


@app.get("/api/document-results")
def api_document_results() -> dict[str, Any]:
    return core.load_results()


@app.get("/api/documents/{filename}")
def api_document_file(filename: str):
    safe_name = Path(filename).name
    path = core.DOWNLOAD_DIR / safe_name
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "Файл не найден. Сначала нажмите «Обновить прайсы»."}, status_code=404)
    return FileResponse(path, filename=safe_name)


def _integration_for_origin(company: str, origin: str, document: dict[str, Any]) -> dict[str, Any]:
    """Return source-card metadata that belongs to the selected origin.

    Older builds mixed global/bundled Moscow evidence into the source counters for
    Санкт-Петербург.  That made a card say e.g. "56 rows" although the SPB PDF
    itself had not been collected.  The pricing engine already filters source IDs;
    the UI must apply the same rule so diagnostics describe the route being viewed.
    """
    view = dict(document or {})
    sources = list(view.get("sources") or [])
    allowed = _source_ids_for_origin(company, origin)
    if allowed is None:
        return view

    selected = [src for src in sources if str(src.get("id") or "") in allowed]
    if selected:
        view["sources"] = selected
        view["source_count"] = len(selected)
        primary = selected[0]
        view["primary_url"] = primary.get("reference_url") or primary.get("url") or view.get("primary_url")
        view["primary_title"] = primary.get("title") or view.get("primary_title")
        view["note"] = primary.get("instruction") or view.get("note")
        collected = sum(int(src.get("rows_extracted") or 0) for src in selected)
        view["collected_rows"] = collected
        # DL/NewLine bundled snapshots are Moscow-origin snapshots.  Never count
        # them as SPB evidence merely because the same company has another source.
        if normalize_city(origin) == "Санкт-Петербург" and company in {"ДЛ", "Новая Линия"}:
            view["embedded_rows"] = 0
            view["embedded_label"] = "Для Санкт-Петербурга учитывается только отдельный маршрутный источник"
            view["evidence_rows"] = collected
        else:
            view["evidence_rows"] = max(collected, int(view.get("embedded_rows") or 0))
        errors = [str(src.get("collection_message") or "").strip() for src in selected if str(src.get("collection_message") or "").strip()]
        view["collection_errors"] = errors
        return view

    # An explicit empty source-id set means the company must be obtained through
    # a route-aware API/browser/table.  Bundled Moscow rows must not inflate the
    # SPB source counter.
    view["collected_rows"] = 0
    view["embedded_rows"] = 0
    view["evidence_rows"] = 0
    view["embedded_label"] = "Для выбранного города отправления используется отдельный онлайн-источник"
    if company in PUBLIC_SOURCE_URLS:
        view["primary_url"] = PUBLIC_SOURCE_URLS[company]
    return view


@app.get("/api/options")
def api_options(origin: str = Query(default="Москва")) -> dict[str, Any]:
    settings = load_settings()
    documents = company_catalog()
    origin_documents = {company: _integration_for_origin(company, origin, documents.get(company) or {}) for company in COMPANIES}
    statuses = {company: (origin_documents.get(company) or {}).get("status", "needs_collect") for company in COMPANIES}
    dellin_path = dellin_pdf_for_origin(origin)
    if dellin_path:
        statuses["ДЛ"] = "snapshot_ready" if "fallback" in str(dellin_path).lower() else "documents_ready"
    if settings.get("vozovoz_api_key"):
        statuses["Возовоз"] = "configured"
    if settings.get("baikal_api_key") and settings.get("baikal_api_url"):
        statuses["Байкал Сервис"] = "configured"
    statuses["Рейл Континент"] = "ready"
    return {
        "origins": ["Москва", "Санкт-Петербург"],
        "destinations": available_destinations(origin),
        "profiles": COMMON_PROFILES,
        "companies": [{"id": c, "label": COMPANY_LABELS[c]} for c in COMPANIES],
        "integration_status": statuses,
        "integrations": [
            {
                "id": c,
                "label": COMPANY_LABELS[c],
                "method": INTEGRATION_DESCRIPTIONS[c][0],
                "note": INTEGRATION_DESCRIPTIONS[c][1],
                "status": statuses[c],
                "coverage": (origin_documents.get(c) or {}).get("coverage"),
                "coverage_label": (origin_documents.get(c) or {}).get("coverage_label"),
                "source_url": (origin_documents.get(c) or {}).get("primary_url"),
                "source_title": (origin_documents.get(c) or {}).get("primary_title"),
                "source_count": (origin_documents.get(c) or {}).get("source_count", 0),
                "sources": (origin_documents.get(c) or {}).get("sources", []),
                "collected_rows": (origin_documents.get(c) or {}).get("collected_rows", 0),
                "embedded_rows": (origin_documents.get(c) or {}).get("embedded_rows", 0),
                "evidence_rows": (origin_documents.get(c) or {}).get("evidence_rows", 0),
                "embedded_label": (origin_documents.get(c) or {}).get("embedded_label", ""),
                "embedded_kind": (origin_documents.get(c) or {}).get("embedded_kind", ""),
                "collection_errors": (origin_documents.get(c) or {}).get("collection_errors", []),
                "last_collected_at": (origin_documents.get(c) or {}).get("last_collected_at"),
            }
            for c in COMPANIES
        ],
        "document_mode": True,
        "public_browser": browser_runtime_status(),
    }


def parse_company_query(value: str | None) -> list[str]:
    if not value:
        return list(COMPANIES)
    requested = [part.strip() for part in value.split(",") if part.strip()]
    return [company for company in COMPANIES if company in requested]


@app.get("/api/compare")
def api_compare(
    origin: str = Query(default="Москва"), destination: str = Query(default="Санкт-Петербург"),
    profile: str = Query(default="w100"), weight_kg: float | None = Query(default=None), volume_m3: float | None = Query(default=None),
    companies: str = Query(default=""),
) -> dict[str, Any]:
    selected = PROFILE_BY_ID.get(profile, PROFILE_BY_ID["w100"])
    weight = float(weight_kg if weight_kg is not None else selected["weight_kg"])
    volume = 0.0
    selected_companies = parse_company_query(companies)
    if not selected_companies:
        return JSONResponse({"error": "Выберите хотя бы одну компанию для расчёта"}, status_code=400)
    return compare_prices(origin, destination, weight, volume, profile, selected_companies)


@app.get("/api/profile-matrix")
def api_profile_matrix(
    origin: str = Query(default="Москва"),
    destination: str = Query(default="Санкт-Петербург"),
    companies: str = Query(default=""),
) -> dict[str, Any]:
    selected_companies = parse_company_query(companies)
    if not selected_companies:
        return JSONResponse({"error": "Выберите хотя бы одну компанию для расчёта"}, status_code=400)
    return profile_matrix(origin, destination, selected_companies)


@app.get("/api/public-browser/status")
def api_public_browser_status() -> dict[str, Any]:
    return browser_runtime_status()


@app.get("/api/settings")
def api_settings() -> dict[str, bool]:
    return {k: bool(v) for k, v in load_settings().items()}


@app.post("/api/settings")
def api_settings_save(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return {"ok": True, "configured": save_settings(payload)}


def prefetch_exact_route_calculators(
    origin: str,
    destination: str,
    companies: list[str],
    *,
    priority_weight: float | None = None,
    progress_callback=None,
) -> dict[str, int]:
    """Warm exact official calculators for Санкт-Петербург → Москва.

    The selected UI weight is placed first, API and browser adapters run in
    parallel, and every successful browser point is cached immediately. This is
    deliberately incremental: the matrix can start showing newly parsed values
    before the complete 29-row grid has finished warming.
    """
    o, d = normalize_city(origin), normalize_city(destination)
    if (o, d) != ("Санкт-Петербург", "Москва"):
        return {"attempted": 0, "loaded": 0}

    # Only adapters backed by documented/public carrier endpoints stay in the
    # API pool. Главтрасса uses its own public calc endpoint and its documented
    # city ids; it is not inferred from another carrier.
    settings = load_settings()
    calculators = {
        "Werner": calculate_werner,
        "Главтрасса": calculate_glavtrassa,
    }
    # The token published in Vozovoz documentation is a TEST token.  Do not
    # present demo-environment totals as current production tariffs.  Exact
    # Vozovoz API calculations are enabled only when the user supplied a real
    # API key; otherwise the verified public route minimum is shown as «от».
    if settings.get("vozovoz_api_key"):
        calculators["Возовоз"] = calculate_vozovoz
    if settings.get("baikal_api_key") and settings.get("baikal_api_url"):
        calculators["Байкал Сервис"] = calculate_baikal

    points = [
        (float(profile["weight_kg"]), matrix_control_volume(float(profile["weight_kg"])))
        for profile in COMMON_PROFILES if not profile.get("is_minimum_profile")
    ]
    if priority_weight is not None:
        pw = float(priority_weight)
        points.sort(key=lambda item: (0 if abs(item[0] - pw) < 1e-9 else 1, abs(item[0] - pw), item[0]))

    selected_api = [(name, calculators[name]) for name in calculators if name in companies]
    # Interleave companies by control point so the selected weight for all API
    # carriers is attempted before the second weight of any one carrier.
    api_jobs: list[tuple[str, Any, float, float]] = [
        (company, fn, w, v)
        for w, v in points
        for company, fn in selected_api
    ]

    browser_companies: list[str] = []
    # Full-table browser adapters: one page load can populate the complete grid.
    if "CTSgroup" in companies:
        browser_companies.append("CTSgroup")
    # New Line is primarily parsed from its direction-specific NL002 PDF now.
    # Keep the live table as a fallback when the PDF host is temporarily blocked.
    if "Новая Линия" in companies:
        probe_weight = float(priority_weight if priority_weight is not None else 100.0)
        if newline_official_pdf_tariff(o, d, probe_weight) is None:
            browser_companies.append("Новая Линия")

    # Slow calculator-style sites are attempted only for the weight currently
    # selected by the user. This gives an exact DL/Baikal value when their web
    # calculator is reachable without making the UI wait for all 29 weights.
    priority_browser_companies: list[str] = []
    if priority_weight is not None:
        for name in ("ДЛ", "Байкал Сервис"):
            if name in companies:
                priority_browser_companies.append(name)

    attempted_total = len(api_jobs) + len(points) * len(browser_companies) + len(priority_browser_companies)
    counter_lock = threading.Lock()
    progress = {"completed": 0, "loaded": 0}

    def notify(company: str, weight: float, ok: bool) -> None:
        with counter_lock:
            progress["completed"] += 1
            progress["loaded"] += int(bool(ok))
            snapshot = {
                "attempted": attempted_total,
                "completed": progress["completed"],
                "loaded": progress["loaded"],
                "company": company,
                "weight": float(weight),
            }
        if progress_callback:
            try:
                progress_callback(snapshot)
            except Exception:
                pass

    def run_api_jobs() -> None:
        def run_one(job: tuple[str, Any, float, float]) -> tuple[str, float, bool, str]:
            company, fn, w, v = job
            message = ""
            try:
                result = calculate_with_cache(company, fn, o, d, w, v)
                ok = bool(
                    result.get("status") in {"ok", "cached"}
                    and isinstance(result.get("price"), (int, float))
                    and not result.get("price_is_minimum")
                )
                if not ok:
                    message = cleanTextForBackend(result.get("message") or result.get("error") or result.get("status") or "нет точного ответа")
            except Exception as exc:
                ok = False
                message = str(exc)
            return company, w, ok, message

        if not api_jobs:
            return
        with ThreadPoolExecutor(max_workers=min(6, len(api_jobs))) as pool:
            futures = [pool.submit(run_one, job) for job in api_jobs]
            for future in as_completed(futures):
                company, w, ok, message = future.result()
                if not ok and (priority_weight is None or abs(float(w) - float(priority_weight)) < 1e-9):
                    print(f"[v36.0 exact] {company} {w:g} кг: {message or 'точный ответ не получен'}", flush=True)
                notify(company, w, ok)

    def warm_browser(company: str) -> None:
        def consume(row: dict[str, Any]) -> None:
            w = float(row.get("weight") or 0.0)
            v = float(row.get("volume") or matrix_control_volume(w or 1.0))
            ok = bool(
                row.get("ok")
                and not row.get("price_is_minimum")
                and isinstance(row.get("price"), (int, float))
            )
            if ok:
                result = {
                    "company": company,
                    "company_label": COMPANY_LABELS[company],
                    "status": "ok",
                    "price": round(float(row["price"])),
                    "currency": "RUB",
                    "delivery_days_min": None,
                    "delivery_days_max": None,
                    "source_type": row.get("source_type") or "Официальный открытый онлайн-калькулятор",
                    "source_url": row.get("source_url") or PUBLIC_SOURCE_URLS.get(company, ""),
                    "freshness": "live_web",
                    "formula": row.get("formula") or "точный итог официального калькулятора по контрольному весу и объёму",
                    "message": cleanTextForBackend(row.get("message") or "Онлайн-расчёт с официального сайта"),
                    "price_is_minimum": False,
                    "calculator_total_only": not isinstance(row.get("rate_per_kg"), (int, float)),
                    "control_volume_m3": v,
                    "browser": row.get("browser"),
                }
                if isinstance(row.get("rate_per_kg"), (int, float)):
                    result["rate_per_kg"] = float(row["rate_per_kg"])
                if isinstance(row.get("minimum_charge"), (int, float)):
                    result["minimum_charge"] = float(row["minimum_charge"])
                if row.get("delivery_days"):
                    nums = [int(x) for x in re.findall(r"\d+", str(row.get("delivery_days")))]
                    if nums:
                        result["delivery_days_min"], result["delivery_days_max"] = min(nums), max(nums)
                cache_successful_result(result_cache_key(company, o, d, w, v), result)
                try:
                    with _PROFILE_MATRIX_CACHE_LOCK:
                        _PROFILE_MATRIX_CACHE.clear()
                        _PROFILE_MATRIX_BUILD_LOCKS.clear()
                except Exception:
                    pass
            notify(company, w, ok)

        try:
            rows = calculate_many_from_public_site(company, o, d, points, on_result=consume)
            # A browser startup failure may return one synthetic row without a
            # control weight. Account for the untouched points so progress can
            # still reach a terminal value instead of looking stuck forever.
            seen = sum(1 for row in rows if isinstance(row.get("weight"), (int, float)))
            for w, _ in points[seen:]:
                notify(company, w, False)
        except Exception:
            for w, _ in points:
                notify(company, w, False)

    def warm_priority_browser(company: str) -> None:
        if priority_weight is None:
            return
        w = float(priority_weight)
        v = matrix_control_volume(w)
        ok = False
        try:
            row = calculate_from_public_site(company, o, d, w, v, allow_visible_fallback=False)
            ok = bool(
                row.get("ok")
                and not row.get("price_is_minimum")
                and isinstance(row.get("price"), (int, float))
            )
            if ok:
                result = {
                    "company": company, "company_label": COMPANY_LABELS[company], "status": "ok",
                    "price": round(float(row["price"])), "currency": "RUB",
                    "delivery_days_min": None, "delivery_days_max": None,
                    "source_type": row.get("source_type") or "Официальный открытый онлайн-калькулятор",
                    "source_url": row.get("source_url") or PUBLIC_SOURCE_URLS.get(company, ""),
                    "freshness": "live_web",
                    "formula": row.get("formula") or "точный итог официального калькулятора по контрольному весу и объёму",
                    "message": cleanTextForBackend(row.get("message") or "Онлайн-расчёт с официального сайта"),
                    "price_is_minimum": False, "calculator_total_only": True,
                    "control_volume_m3": v, "browser": row.get("browser"),
                }
                cache_successful_result(result_cache_key(company, o, d, w, v), result)
                with _PROFILE_MATRIX_CACHE_LOCK:
                    _PROFILE_MATRIX_CACHE.clear()
                    _PROFILE_MATRIX_BUILD_LOCKS.clear()
        except Exception as exc:
            ok = False
            print(f"[v36.0 browser] {company} {w:g} кг: {exc}", flush=True)
        if not ok and 'row' in locals():
            message = cleanTextForBackend(row.get("message") or "точный ответ не получен") if isinstance(row, dict) else "точный ответ не получен"
            print(f"[v36.0 browser] {company} {w:g} кг: {message}", flush=True)
        notify(company, w, ok)

    # Start browser and API work together. The selected weight can therefore
    # appear in the UI while slower file/calculator work is still in progress.
    runners = 1 + len(browser_companies) + len(priority_browser_companies)
    with ThreadPoolExecutor(max_workers=max(1, runners)) as pool:
        futures = []
        if api_jobs:
            futures.append(pool.submit(run_api_jobs))
        for company in browser_companies:
            futures.append(pool.submit(warm_browser, company))
        for company in priority_browser_companies:
            futures.append(pool.submit(warm_priority_browser, company))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    try:
        with _PROFILE_MATRIX_CACHE_LOCK:
            _PROFILE_MATRIX_CACHE.clear()
            _PROFILE_MATRIX_BUILD_LOCKS.clear()
    except Exception:
        pass
    return {"attempted": attempted_total, "loaded": int(progress["loaded"])}


@app.post("/api/collect")
def api_collect(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    # Ручное обновление собирает все выбранные источники. Автообновление на
    # старте берёт только прайсы, которые нужны для строгой весовой матрицы,
    # чтобы не тратить время на дополнительные услуги и дублирующие страницы.
    companies = payload.get("companies") or []
    origin = normalize_city(payload.get("origin") or "Москва")
    destination = normalize_city(payload.get("destination") or "")
    source_ids = None
    if payload.get("strict_only"):
        chosen: set[str] = set()
        for company in companies:
            ids = _source_ids_for_origin(str(company), origin)
            if ids:
                chosen.update(ids)
        source_ids = sorted(chosen)

    job_key = f"{origin}|{destination or '*'}"

    def collect_now() -> dict[str, Any]:
        with _COLLECT_RUN_LOCK:
            result = core.collect_sources(
                companies,
                payload.get("blocks") or [],
                payload.get("limit"),
                selected_source_ids=source_ids,
            )
            _clear_collected_tariff_cache()
            _WERNER_TABLE_CACHE.clear()
            _PEK_ROUTE_FILE_CACHE.clear()
            _KIT_PDF_CANDIDATE_CACHE.clear()
            _NEWLINE_ROUTE_INDEX_CACHE.clear()
            return result

    if payload.get("background"):
        with _COLLECT_JOBS_LOCK:
            existing = _COLLECT_JOBS.get(job_key) or {}
            if existing.get("status") in {"queued", "running"}:
                return {"ok": True, "job_key": job_key, **existing}
            _COLLECT_JOBS[job_key] = {
                "status": "queued", "origin": origin, "destination": destination,
                "started_at": now_iso(), "exact_revision": 0,
                "exact_progress": {"attempted": 0, "completed": 0, "loaded": 0},
                "message": "Обновление официальных прайсов поставлено в очередь.",
            }

        def worker() -> None:
            with _COLLECT_JOBS_LOCK:
                _COLLECT_JOBS[job_key]["status"] = "running"
                _COLLECT_JOBS[job_key]["message"] = "Загружаются официальные прайсы перевозчиков. Таблица остаётся доступной."
            try:
                priority_profile = PROFILE_BY_ID.get(str(payload.get("profile") or "w100"), PROFILE_BY_ID["w100"])
                priority_weight = None if priority_profile.get("is_minimum_profile") else float(priority_profile.get("weight_kg") or 100.0)

                def exact_progress(update: dict[str, Any]) -> None:
                    with _COLLECT_JOBS_LOCK:
                        job = _COLLECT_JOBS.get(job_key)
                        if not job:
                            return
                        revision = int(job.get("exact_revision") or 0) + 1
                        job.update({
                            "exact_revision": revision,
                            "exact_progress": dict(update),
                            "message": f"Точные тарифы СПб → Москва: получено {update.get('loaded', 0)} из {update.get('completed', 0)} проверенных точек. Таблица обновляется по мере загрузки.",
                        })

                # File/PDF collection and exact online calculators are independent
                # official sources. Run them together so a slow PDF host cannot
                # postpone a working Werner/Возовоз/CTS/Baikal/Главтрасса result.
                with ThreadPoolExecutor(max_workers=2) as refresh_pool:
                    collect_future = refresh_pool.submit(collect_now)
                    exact_future = refresh_pool.submit(
                        prefetch_exact_route_calculators,
                        origin, destination, [str(x) for x in companies],
                        priority_weight=priority_weight,
                        progress_callback=exact_progress,
                    )
                    result = collect_future.result()
                    summary = result.get("summary", {})
                    exact_prefetch = exact_future.result()
                with _COLLECT_JOBS_LOCK:
                    _COLLECT_JOBS[job_key].update({
                        "status": "done", "finished_at": now_iso(), "summary": summary, "exact_prefetch": exact_prefetch,
                        "message": f"Официальные прайсы обновлены: файлов {summary.get('files_downloaded', 0)}, извлечено строк {summary.get('rows_extracted', 0)}, точных калькуляторных точек {exact_prefetch.get('loaded', 0)}.",
                    })
            except Exception as exc:
                with _COLLECT_JOBS_LOCK:
                    _COLLECT_JOBS[job_key].update({
                        "status": "error", "finished_at": now_iso(), "error": str(exc),
                        "message": "Часть сайтов не ответила; сохранены последние успешно загруженные официальные данные.",
                    })

        threading.Thread(target=worker, name=f"tariff-refresh-{origin}", daemon=True).start()
        return {"ok": True, "job_key": job_key, **_COLLECT_JOBS[job_key]}

    try:
        result = collect_now()
        summary = result.get("summary", {})
        return {
            "ok": True, "collected_at": result.get("collected_at"), "summary": summary,
            "message": f"Официальные прайсы обновлены: файлов {summary.get('files_downloaded', 0)}, извлечено строк {summary.get('rows_extracted', 0)}. Исходные тарифные диапазоны раскладываются на все покрываемые строки единой сетки без интерполяции цены.",
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/collect-status")
def api_collect_status(origin: str = Query(default="Москва"), destination: str = Query(default="")) -> dict[str, Any]:
    key = f"{normalize_city(origin)}|{normalize_city(destination) or '*'}"
    with _COLLECT_JOBS_LOCK:
        job = dict(_COLLECT_JOBS.get(key) or {"status": "idle", "message": "Обновление ещё не запускалось."})
    try:
        current = core.load_results()
        summary = current.get("summary") or {}
        job.setdefault("summary", summary)
        job["progress_rows"] = int(summary.get("rows_extracted") or 0)
        job["progress_files"] = int(summary.get("files_downloaded") or 0)
        job["results_updated_at"] = current.get("collected_at")
    except Exception:
        pass
    return {"ok": True, "job_key": key, **job}


@app.get("/api/export/excel")
def api_export_excel(
    origin: str = Query(default="Москва"), destination: str = Query(default="Санкт-Петербург"),
    profile: str = Query(default="w100"), weight_kg: float | None = Query(default=None), volume_m3: float | None = Query(default=None),
    companies: str = Query(default=""),
):
    selected = PROFILE_BY_ID.get(profile, PROFILE_BY_ID["w100"])
    weight = float(weight_kg if weight_kg is not None else selected["weight_kg"])
    volume = 0.0
    selected_companies = parse_company_query(companies)
    if not selected_companies:
        return JSONResponse({"error": "Выберите хотя бы одну компанию для экспорта"}, status_code=400)
    comparison = compare_prices(origin, destination, weight, volume, profile, selected_companies)
    path = create_comparison_export(comparison)
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": "36.0", "document_mode": True, "time": now_iso(), "dellin_routes": len(parse_dellin_tariffs(DATA_DIR / "fallback" / "dellin_moscow.pdf", "Москва")), "document_companies": len(company_catalog())}
