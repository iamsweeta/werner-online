from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "data" / "document_sources.json"
RESULTS_PATH = BASE_DIR / "runtime" / "results.json"
_RESULTS_CACHE_STAMP: tuple[int, int] | None = None
_RESULTS_CACHE_DATA: dict[str, Any] | None = None
_CATALOG_CACHE_STAMP: tuple[int, int] | None = None
_CATALOG_CACHE_DATA: dict[str, dict[str, Any]] | None = None

COVERAGE_LABELS = {
    "full_matrix": "Полная тарифная сетка",
    "limited_service": "Прайс отдельного сервиса",
    "examples_only": "Отдельные тарифные примеры",
    "conditions_only": "Только условия и доплаты",
}
COVERAGE_STATUS = {
    "full_matrix": "documents_full",
    "limited_service": "documents_limited",
    "examples_only": "documents_partial",
    "conditions_only": "documents_conditions",
}
COVERAGE_PRIORITY = {
    "full_matrix": 4,
    "limited_service": 3,
    "examples_only": 2,
    "conditions_only": 1,
}

# Встроенные подтверждённые снимки нужны не как «резервная оценка», а как
# воспроизводимое доказательство того, откуда приложение взяло тариф, когда
# официальный сайт временно не скачивается (403/DNS/тайм-аут). Эти количества
# показываются отдельно от строк, реально скачанных пользователем в текущем сеансе.
EMBEDDED_EVIDENCE = {
    "Werner": {"rows": 5, "kind": "official_html_file_plus_api", "label": "Официальная HTML-таблица/тарифный файл wernerus.ru — источник ставок; контрольные API-ответы являются только итогами расчёта и не превращаются в ₽/кг"},
    "ДЛ": {"rows": 85, "kind": "official_snapshot", "label": "Официальный PDF-снимок Москвы"},
    "ПЭК": {"rows": 3, "kind": "historical_control", "label": "3 исторических контрольных ответа вес+объём; для новой матрицы нужен XLS/XLSX или явная ₽/кг"},
    "КИТ": {"rows": 2, "kind": "official_snapshot", "label": "Опубликованные маршрутные ставки"},
    "Возовоз": {"rows": 14, "kind": "historical_control", "label": "опубликованные минимумы и старые контрольные ответы; строгая матрица использует только нормализованный прайс"},
    "Мейджик": {"rows": 2, "kind": "published_lower_bound", "label": "Официальные маршрутные цены «от» Москва ↔ Санкт-Петербург; точный калькулятор отдельно"},
    "Главтрасса": {"rows": 0, "kind": "official_table_plus_api", "label": "Официальная тарифная страница/файл — источник весовых ставок; API возвращает только итог расчёта"},
    "Пролайн": {"rows": 20, "kind": "official_snapshot", "label": "Официальные маршрутные ставки Москва ↔ Санкт-Петербург"},
    "Фортуна": {"rows": 135, "kind": "official_snapshot", "label": "Официальный XLSX от 27.07.2026: направления из Москвы и Санкт-Петербурга"},
    "Грузопоток": {"rows": 0, "kind": "none", "label": "Официальный сайт; стабильная полная сетка не подтверждена"},
    "ФастТранс": {"rows": 24, "kind": "official_snapshot", "label": "Официальные HTML-матрицы Москва ↔ Санкт-Петербург"},
    "АТЭК": {"rows": 6, "kind": "official_snapshot", "label": "Официальный тариф Москва → Санкт-Петербург"},
    "Новая Линия": {"rows": 56, "kind": "official_snapshot", "label": "Официальная PDF-выгрузка Москва-Север: 56 распознанных направлений"},
    "БСК": {"rows": 7, "kind": "official_snapshot", "label": "Официальная таблица БСД/123789 Москва → Санкт-Петербург"},
    "ЭкспедицияПлюс": {"rows": 187, "kind": "official_snapshot", "label": "Официальный XLSX с 30.07.2026: 187 межтерминальных направлений из 9 городов отправления"},
    "CTSgroup": {"rows": 15, "kind": "official_snapshot", "label": "Официальный XLSX CTS из Москвы: ставки руб/кг и руб/м³, действует с 01.04.2026"},
    "Байкал Сервис": {"rows": 7, "kind": "official_snapshot", "label": "Официальная таблица популярных направлений"},
    "СДЭК": {"rows": 0, "kind": "conditions", "label": "Открытый PDF содержит условия, но не маршрутную сетку"},
    "DPD": {"rows": 4, "kind": "examples", "label": "Официальные публичные примеры до 1 кг"},
    "Рейл Континент": {"rows": 14, "kind": "official_table", "label": "Официальная весовая таблица руб/кг от 17.07.2026 + публичный API"},
    "Pony Express": {"rows": 10, "kind": "official_snapshot", "label": "Маршрутные строки Стандарт+ из официального PDF"},
}


def load_definitions() -> dict[str, Any]:
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"companies": [], "sources": []}
    except Exception:
        return {"companies": [], "sources": []}


def load_collection_results() -> dict[str, Any]:
    """Читает результаты загрузчика; при первом старте подхватывает bootstrap ДЛ.

    В v11 catalog читал только runtime/results.json. Если пользователь ещё не нажимал
    «Обновить прайсы», файл отсутствовал, хотя встроенный официальный PDF ДЛ уже
    использовался в расчёте. Из-за этого интерфейс показывал «0 строк». Здесь каталог
    и расчёт используют один и тот же bootstrap.
    """
    global _RESULTS_CACHE_STAMP, _RESULTS_CACHE_DATA
    if RESULTS_PATH.exists():
        try:
            stat = RESULTS_PATH.stat()
            stamp = (int(stat.st_mtime_ns), int(stat.st_size))
            if stamp == _RESULTS_CACHE_STAMP and isinstance(_RESULTS_CACHE_DATA, dict):
                return _RESULTS_CACHE_DATA
            data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _RESULTS_CACHE_STAMP = stamp
                _RESULTS_CACHE_DATA = data
                return data
            return {}
        except Exception:
            return {}
    try:
        from . import legacy as core
        data = core.load_results()
        if isinstance(data, dict):
            # Persist the bootstrap once. Otherwise every source card would parse
            # the bundled Dellin PDF again during the same page render.
            try:
                RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                RESULTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            return data
        return {}
    except Exception:
        return {}


def company_catalog() -> dict[str, dict[str, Any]]:
    global _CATALOG_CACHE_STAMP, _CATALOG_CACHE_DATA
    try:
        stat = RESULTS_PATH.stat()
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        stamp = (0, 0)
    if stamp == _CATALOG_CACHE_STAMP and isinstance(_CATALOG_CACHE_DATA, dict):
        return _CATALOG_CACHE_DATA
    definitions = load_definitions()
    results = load_collection_results()
    statuses_by_id = {str(row.get("id")): row for row in results.get("sources", []) if isinstance(row, dict)}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in definitions.get("sources", []):
        if isinstance(source, dict) and source.get("company"):
            item = dict(source)
            collected = statuses_by_id.get(str(item.get("id"))) or {}
            item["collection_status"] = collected.get("status") or "not_collected"
            item["collection_message"] = collected.get("message") or ""
            item["files"] = collected.get("files") or []
            item["rows_extracted"] = int(collected.get("rows_extracted") or 0)
            item["status_code"] = collected.get("status_code")
            item["freshness"] = collected.get("freshness") or ""
            grouped[str(item["company"])].append(item)

    catalog: dict[str, dict[str, Any]] = {}
    for company in definitions.get("companies", []):
        sources = grouped.get(company, [])
        best = max(sources, key=lambda x: COVERAGE_PRIORITY.get(str(x.get("coverage")), 0), default={})
        parsed = [s for s in sources if s.get("collection_status") in {"parsed", "downloaded", "cached", "snapshot"}]
        usable_parsed = [s for s in parsed if s.get("usable_for_price")]
        coverage = str(best.get("coverage") or "conditions_only")
        evidence = EMBEDDED_EVIDENCE.get(company, {"rows": 0, "kind": "none", "label": ""})
        embedded_rows = int(evidence.get("rows") or 0)
        collected_rows = sum(int(s.get("rows_extracted") or 0) for s in sources)

        status = COVERAGE_STATUS.get(coverage, "documents_partial")
        if usable_parsed:
            status = "documents_ready"
        elif parsed:
            status = "documents_collected"
        elif embedded_rows > 0:
            if evidence.get("kind") == "live_snapshot":
                status = "ready"
            else:
                status = "documents_partial" if coverage == "examples_only" else "snapshot_ready"
        elif coverage == "conditions_only":
            status = "documents_conditions"
        elif evidence.get("kind") == "live":
            status = "ready"
        elif coverage == "examples_only":
            status = "documents_partial"
        elif coverage == "limited_service":
            status = "documents_limited"
        elif sources:
            status = "needs_collect"

        errors = []
        for src in sources:
            st = str(src.get("collection_status") or "")
            msg = str(src.get("collection_message") or "").strip()
            if st in {"error", "http_error", "parse_error", "temporarily_unavailable", "manual_needed", "missing"} and msg:
                errors.append(msg)

        catalog[company] = {
            "status": status,
            "coverage": coverage,
            "coverage_label": COVERAGE_LABELS.get(coverage, coverage),
            "usable_for_price": any(bool(s.get("usable_for_price")) for s in sources),
            "collected_usable": bool(usable_parsed),
            "source_count": len(sources),
            "sources": sources,
            "primary_url": str(best.get("reference_url") or best.get("url") or ""),
            "primary_title": str(best.get("title") or "Официальный источник"),
            "note": str(best.get("instruction") or ""),
            "collected_rows": collected_rows,
            "embedded_rows": embedded_rows,
            "evidence_rows": max(collected_rows, embedded_rows),
            "embedded_label": str(evidence.get("label") or ""),
            "embedded_kind": str(evidence.get("kind") or "none"),
            "collection_errors": errors[:3],
            "last_collected_at": results.get("collected_at"),
        }
    _CATALOG_CACHE_STAMP = stamp
    _CATALOG_CACHE_DATA = catalog
    return catalog


def primary_source(company: str) -> dict[str, Any]:
    return company_catalog().get(company, {})


def public_document_result(
    company: str,
    label: str,
    origin: str,
    destination: str,
    weight: float,
    volume: float,
) -> dict[str, Any]:
    info = primary_source(company)
    coverage = info.get("coverage")
    if coverage == "conditions_only":
        message = (
            "Открытый официальный документ содержит условия и дополнительные сборы, "
            "но не содержит полной маршрутной цены. Без калькулятора/API точный тариф для "
            f"{origin} → {destination}, {weight:g} кг / {volume:g} м³ вычислить нельзя."
        )
    elif coverage == "examples_only":
        message = (
            "В открытом источнике опубликованы только отдельные примеры. Для заданного маршрута "
            "и диапазона точного совпадения не найдено; произвольная интерполяция отключена."
        )
    else:
        message = (
            "Тарифный документ опубликован, но строка для заданного маршрута/диапазона ещё не "
            "нормализована или отсутствует в текущем файле. Нажмите «Обновить прайсы» и проверьте статус источника."
        )
    return {
        "company": company,
        "company_label": label,
        "status": "document_unavailable",
        "price": None,
        "currency": "RUB",
        "delivery_days_min": None,
        "delivery_days_max": None,
        "source_type": info.get("coverage_label") or "Официальный открытый документ",
        "source_url": info.get("primary_url") or "",
        "freshness": "official_document",
        "formula": "Цена не рассчитывается, если в официальном документе нет точного сопоставимого тарифа.",
        "message": message,
        "document_coverage": coverage,
        "document_sources": info.get("sources") or [],
    }
