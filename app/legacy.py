
from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = BASE_DIR / "runtime"
DOWNLOAD_DIR = RUNTIME_DIR / "downloads"
EXPORT_DIR = RUNTIME_DIR / "exports"
CACHE_DIR = RUNTIME_DIR / "cache"
FALLBACK_DIR = DATA_DIR / "fallback"
RESULTS_PATH = RUNTIME_DIR / "results.json"
SOURCES_PATH = DATA_DIR / "sources.json"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FALLBACK_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Тарифы конкурентов", version="5.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

SESSION = requests.Session()
DELLIN_RUN_CACHE: dict[int, dict[str, Any]] = {}
_RESULTS_MEMORY_STAMP: tuple[int, int] | None = None
_RESULTS_MEMORY_DATA: dict[str, Any] | None = None
_SOURCES_MEMORY_DATA: dict[str, Any] | None = None

SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LogisticsTariffsBot/2.0",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8",
})


def _curl_response(url: str, headers: dict[str, str] | None = None, timeout: int = 35):
    """Fetch an official public source through the OS curl as a TLS fallback.

    On Windows Python/urllib3 and a few Russian carrier CDNs occasionally fail
    the TLS handshake with ``UNEXPECTED_EOF_WHILE_READING`` while the same URL
    opens normally in Edge.  Windows' bundled curl.exe uses the system TLS stack
    and is a useful read-only fallback.  Certificate verification remains on.
    """
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("Системный curl не найден")
    stamp = f"curl_{int(time.time() * 1000)}_{os.getpid()}_{abs(hash(url)) & 0xfffffff:x}.bin"
    tmp = CACHE_DIR / stamp
    cmd = [
        curl, "-L", "-sS", "--connect-timeout", "8", "--max-time", str(int(timeout)),
        "--retry", "2", "--retry-delay", "1", "-o", str(tmp),
        "-w", "%{http_code}\\n%{content_type}\\n%{url_effective}",
    ]
    for key, value in (headers or {}).items():
        if value:
            cmd.extend(["-H", f"{key}: {value}"])
    cmd.append(url)
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 8, check=False)
        meta = (completed.stdout or "").splitlines()
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or f"curl exit {completed.returncode}").strip())
        body = tmp.read_bytes() if tmp.exists() else b""
        status_code = int(meta[0]) if meta and str(meta[0]).isdigit() else 200
        content_type = meta[1].strip() if len(meta) > 1 else ""
        final_url = meta[2].strip() if len(meta) > 2 and meta[2].strip() else url
        return SimpleNamespace(
            content=body,
            headers={"content-type": content_type},
            url=final_url,
            status_code=status_code,
            ok=200 <= status_code < 400,
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass



def _powershell_response(url: str, headers: dict[str, str] | None = None, timeout: int = 45):
    """Fetch a file through Windows WinHTTP/.NET as an independent TLS stack."""
    exe = shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        raise RuntimeError("PowerShell is unavailable")
    fd, temp_name = tempfile.mkstemp(prefix="tariff_ps_", suffix=".bin")
    os.close(fd)
    env = dict(os.environ)
    env["TARIFF_FETCH_URL"] = str(url)
    env["TARIFF_FETCH_OUT"] = temp_name
    env["TARIFF_FETCH_REFERER"] = str((headers or {}).get("Referer") or "")
    script = r"""
    $ProgressPreference='SilentlyContinue';
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13 } catch {}
    $h=@{}; if($env:TARIFF_FETCH_REFERER){$h['Referer']=$env:TARIFF_FETCH_REFERER};
    Invoke-WebRequest -UseBasicParsing -Uri $env:TARIFF_FETCH_URL -Headers $h -MaximumRedirection 8 -TimeoutSec 35 -OutFile $env:TARIFF_FETCH_OUT;
    """
    try:
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(15, int(timeout)+8), check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or f"PowerShell exit {proc.returncode}")
        body = Path(temp_name).read_bytes()
        if not body:
            raise RuntimeError("PowerShell downloaded an empty response")
        return SimpleNamespace(content=body, headers={"content-type": ""}, url=url, status_code=200, ok=True)
    finally:
        try: Path(temp_name).unlink(missing_ok=True)
        except Exception: pass

def _browser_response(url: str, headers: dict[str, str] | None = None, timeout: int = 45):
    """Fetch an official file with the installed Edge/Chrome as a last network fallback.

    Some carrier CDNs complete normally in the user's browser but reset TLS for
    Python/OpenSSL and curl. Selenium is already an optional dependency for the
    public calculators, so reuse the real browser network stack and fetch the
    file from a same-origin page. No certificate checks are disabled.
    """
    from .public_web import _make_driver

    parsed = urlparse(url)
    origin_root = f"{parsed.scheme}://{parsed.netloc}/"
    referer = str((headers or {}).get("Referer") or origin_root)
    if urlparse(referer).netloc != parsed.netloc:
        referer = origin_root
    driver = None
    try:
        driver, _ = _make_driver(headless=True)
        driver.set_script_timeout(max(20, int(timeout)))
        try:
            driver.get(referer)
        except Exception:
            driver.get(origin_root)
        script = r"""
        const url = arguments[0], done = arguments[arguments.length - 1];
        fetch(url, {credentials:'include', cache:'no-store'}).then(async r => {
          const buf = new Uint8Array(await r.arrayBuffer());
          let binary = '';
          const step = 0x8000;
          for (let i=0; i<buf.length; i+=step) binary += String.fromCharCode(...buf.subarray(i, i+step));
          done({ok:r.ok,status:r.status,url:r.url,type:r.headers.get('content-type')||'',body:btoa(binary)});
        }).catch(e => done({ok:false,status:0,url:url,type:'',error:String(e)}));
        """
        data = driver.execute_async_script(script, url)
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(str((data or {}).get("error") if isinstance(data, dict) else data) or "браузер не скачал файл")
        body = base64.b64decode(str(data.get("body") or ""))
        status_code = int(data.get("status") or 200)
        return SimpleNamespace(
            content=body,
            headers={"content-type": str(data.get("type") or "")},
            url=str(data.get("url") or url),
            status_code=status_code,
            ok=200 <= status_code < 400,
        )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_sources() -> dict[str, Any]:
    global _SOURCES_MEMORY_DATA
    if isinstance(_SOURCES_MEMORY_DATA, dict):
        return _SOURCES_MEMORY_DATA
    _SOURCES_MEMORY_DATA = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return _SOURCES_MEMORY_DATA


def empty_results() -> dict[str, Any]:
    data = load_sources()
    company_source_counts = Counter(s.get("company") for s in data.get("sources", []))
    block_source_counts = Counter(s.get("block_title") for s in data.get("sources", []))
    source_type_counts = Counter(
        "Прямые файлы" if "скачать файл" in (s.get("source_type") or "").lower() else "Веб-страницы"
        for s in data.get("sources", [])
    )
    return {
        "collected_at": None,
        "source_excel": data.get("source_excel"),
        "blocks": data.get("blocks", []),
        "companies": data.get("companies", []),
        "sources": [],
        "rows": [],
        "files": [],
        "errors": [],
        "summary": {
            "companies_total": len(data.get("companies", [])),
            "sources_total": len(data.get("sources", [])),
            "sources_done": 0,
            "files_downloaded": 0,
            "rows_extracted": 0,
            "errors": 0,
            "status_counts": {},
            "company_rows": {},
            "block_rows": {},
            "format_counts": {},
            "company_source_counts": dict(company_source_counts),
            "block_source_counts": dict(block_source_counts),
            "source_type_counts": dict(source_type_counts),
            "company_amount_stats": {},
            "block_amount_stats": {},
        },
    }


def save_results(results: dict[str, Any]) -> None:
    """Persist collector state atomically.

    The browser polls while collection is running. Writing directly to
    ``results.json`` allowed readers to observe a half-written JSON document,
    which made a successful source look empty until the next full refresh.
    """
    global _RESULTS_MEMORY_STAMP, _RESULTS_MEMORY_DATA
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, RESULTS_PATH)
    stat = RESULTS_PATH.stat()
    _RESULTS_MEMORY_STAMP = (int(stat.st_mtime_ns), int(stat.st_size))
    _RESULTS_MEMORY_DATA = results


def load_results() -> dict[str, Any]:
    global _RESULTS_MEMORY_STAMP, _RESULTS_MEMORY_DATA
    if RESULTS_PATH.exists():
        stat = RESULTS_PATH.stat()
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
        if stamp == _RESULTS_MEMORY_STAMP and isinstance(_RESULTS_MEMORY_DATA, dict):
            return _RESULTS_MEMORY_DATA
        data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        _RESULTS_MEMORY_STAMP = stamp
        _RESULTS_MEMORY_DATA = data
        return data
    # Деловые Линии доступны сразу из резервного снимка официального PDF Москвы.
    # Это не заменяет онлайн-обновление, но не оставляет интерфейс пустым при первом запуске.
    snapshot = build_dellin_bootstrap_results()
    return snapshot if snapshot else empty_results()


def slugify(value: str, limit: int = 80) -> str:
    value = str(value or "")
    value = value.replace(" ", "_")
    value = re.sub(r"[^\wа-яА-ЯёЁ.\-]+", "_", value, flags=re.IGNORECASE)
    value = re.sub(r"_+", "_", value).strip("._-")
    return (value or "file")[:limit]


def guess_extension(url: str, content_type: str = "", body: bytes | None = None) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    if "." in name:
        ext = "." + name.split(".")[-1].lower()
        if ext in [".pdf", ".xls", ".xlsx", ".zip", ".html", ".htm", ".docx", ".doc", ".csv"]:
            return ext
    qs = parse_qs(parsed.query)
    for key in ["name", "filename", "file"]:
        if key in qs and qs[key]:
            n = unquote(qs[key][0])
            if "." in n:
                ext = "." + n.split(".")[-1].lower()
                if ext in [".pdf", ".xls", ".xlsx", ".zip", ".html", ".htm", ".docx", ".doc", ".csv"]:
                    return ext
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "spreadsheetml" in ct:
        return ".xlsx"
    if "ms-excel" in ct or "excel" in ct:
        return ".xls"
    if "zip" in ct:
        return ".zip"
    if "html" in ct:
        return ".html"
    if "csv" in ct:
        return ".csv"
    if body:
        if body[:4] == b"%PDF":
            return ".pdf"
        if body[:2] == b"PK":
            return ".zip"
    return ".bin"


def is_yandex_viewer(url: str) -> bool:
    parsed = urlparse(url)
    return "docs.yandex.ru" in parsed.netloc and "/docs/view" in parsed.path


def should_scan_page(source_type: str, ext: str, content_type: str) -> bool:
    s = (source_type or "").lower()
    return ext in [".html", ".htm", ".bin"] or "страница" in s or "html" in (content_type or "").lower()


def amount_candidates(text: str) -> list[float]:
    if not text:
        return []
    candidates = []
    # numbers like 1 234,50 ₽ or 1234 руб
    for m in re.finditer(r"(?<!\d)(\d{1,3}(?:[\s\u00A0]\d{3})+|\d{3,7})(?:[,.](\d{1,2}))?(?=\s*(?:₽|руб|р\.|RUB|\b))?", text, re.IGNORECASE):
        raw = m.group(1).replace("\u00A0", " ").replace(" ", "")
        dec = m.group(2) or ""
        try:
            val = float(raw + (("." + dec) if dec else ""))
            if 0 < val < 100_000_000:
                candidates.append(val)
        except ValueError:
            pass
    return candidates[:6]


def as_text_row(values: list[Any]) -> str:
    return " | ".join([str(v).strip() for v in values if v not in (None, "")]).strip()


def normalize_cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def extract_from_xlsx(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    from openpyxl import load_workbook

    rows: list[dict[str, Any]] = []
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
        for ws in wb.worksheets:
            empty_streak = 0
            row_no = 0
            for row in ws.iter_rows(values_only=True):
                row_no += 1
                values = [normalize_cell(v) for v in row]
                text = as_text_row(values)
                if not text:
                    empty_streak += 1
                    if empty_streak > 50:
                        break
                    continue
                empty_streak = 0
                has_num = bool(amount_candidates(text)) or any(isinstance(v, (int, float)) and v != 0 for v in values if v is not None)
                if row_no <= 15 or has_num or len([v for v in values if v not in (None, "")]) >= 3:
                    cols = values[:64]
                    rows.append(make_row(source, "xlsx", path.name, ws.title, row_no, cols, text, parent_file))
                if len(rows) > 5000:
                    break
    except Exception as e:
        raise RuntimeError(f"Excel XLSX parse error: {e}") from e
    return rows


def extract_from_xls(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    import xlrd

    rows: list[dict[str, Any]] = []
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
        for sheet in book.sheets():
            for row_no in range(sheet.nrows):
                vals = sheet.row_values(row_no)
                values = [normalize_cell(v) for v in vals]
                text = as_text_row(values)
                if not text:
                    continue
                has_num = bool(amount_candidates(text)) or any(isinstance(v, (int, float)) and v != 0 for v in values if v is not None)
                if row_no < 15 or has_num or len([v for v in values if v not in (None, "")]) >= 3:
                    rows.append(make_row(source, "xls", path.name, sheet.name, row_no + 1, values[:64], text, parent_file))
                if len(rows) > 5000:
                    break
    except Exception as e:
        raise RuntimeError(f"Excel XLS parse error: {e}") from e
    return rows


def extract_from_csv(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw = path.read_bytes()
    for enc in ["utf-8-sig", "cp1251", "utf-8"]:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            text = ""
    dialect = csv.Sniffer().sniff(text[:1000]) if text else csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    for i, vals in enumerate(reader, start=1):
        txt = as_text_row(vals)
        if txt:
            rows.append(make_row(source, "csv", path.name, "CSV", i, vals[:64], txt, parent_file))
        if len(rows) > 5000:
            break
    return rows


def extract_from_pdf(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    from pypdf import PdfReader

    rows: list[dict[str, Any]] = []
    try:
        reader = PdfReader(str(path))
        for pi, page in enumerate(reader.pages[:80], start=1):
            text = page.extract_text() or ""
            lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
            for li, line in enumerate(lines, start=1):
                if not line:
                    continue
                if li <= 15 or amount_candidates(line) or any(w in line.lower() for w in ["тариф", "руб", "москва", "санкт", "забор", "доставка"]):
                    rows.append(make_row(source, "pdf", path.name, f"page {pi}", li, [line], line, parent_file))
                if len(rows) > 5000:
                    break
            if len(rows) > 5000:
                break
    except Exception as e:
        raise RuntimeError(f"PDF parse error: {e}") from e
    return rows


def extract_from_docx(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    from docx import Document

    rows: list[dict[str, Any]] = []
    try:
        doc = Document(str(path))
        idx = 0
        for p in doc.paragraphs:
            idx += 1
            txt = re.sub(r"\s+", " ", p.text or "").strip()
            if txt and (idx <= 20 or amount_candidates(txt) or "тариф" in txt.lower()):
                rows.append(make_row(source, "docx", path.name, "paragraphs", idx, [txt], txt, parent_file))
        for ti, table in enumerate(doc.tables, start=1):
            for ri, row in enumerate(table.rows, start=1):
                vals = [c.text.strip() for c in row.cells]
                txt = as_text_row(vals)
                if txt:
                    rows.append(make_row(source, "docx", path.name, f"table {ti}", ri, vals[:16], txt, parent_file))
                if len(rows) > 5000:
                    break
    except Exception as e:
        raise RuntimeError(f"DOCX parse error: {e}") from e
    return rows


def extract_from_html(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw = path.read_bytes()
    for enc in ["utf-8", "cp1251", "windows-1251"]:
        try:
            html = raw.decode(enc, errors="ignore")
            break
        except Exception:
            html = ""
    soup = BeautifulSoup(html, "lxml")
    # tables first
    table_idx = 0
    for table in soup.find_all("table")[:30]:
        table_idx += 1
        for ri, tr in enumerate(table.find_all("tr"), start=1):
            vals = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            txt = as_text_row(vals)
            if txt:
                rows.append(make_row(source, "html", path.name, f"table {table_idx}", ri, vals[:16], txt, parent_file))
            if len(rows) > 5000:
                return rows
    # then lines that look like tariffs
    text = soup.get_text("\n", strip=True)
    for li, line in enumerate([re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()], start=1):
        if li <= 20 or amount_candidates(line) or any(w in line.lower() for w in ["тариф", "руб", "забор", "доставка"]):
            rows.append(make_row(source, "html", path.name, "text", li, [line], line, parent_file))
        if len(rows) > 5000:
            break
    return rows


def make_row(source: dict[str, Any], fmt: str, filename: str, sheet: str, row_index: int, cols: list[Any], text: str, parent_file: str | None) -> dict[str, Any]:
    amounts = amount_candidates(text)
    row = {
        "company": source["company"],
        "block_key": source["block_key"],
        "block_title": source["block_title"],
        "source_id": source["id"],
        "source_type": source.get("source_type", ""),
        "url": source.get("url", ""),
        "source_title": source.get("title", ""),
        "document_format": source.get("document_format", ""),
        "coverage": source.get("coverage", ""),
        "usable_for_price": bool(source.get("usable_for_price")),
        "effective_date": source.get("effective_date"),
        "file": filename,
        "parent_file": parent_file or "",
        "format": fmt,
        "sheet": sheet,
        "row_index": row_index,
        "raw_text": text[:2000],
        # Keep the complete source row as well as the legacy col_1..col_16 fields.
        # Werner and several other official tariff files place weight tiers farther
        # to the right; truncating them to 16 columns made the source look
        # "collected" while the calculator could not actually see all prices.
        "columns": [normalize_cell(v) for v in cols],
        "amount_min": min(amounts) if amounts else None,
        "amount_max": max(amounts) if amounts else None,
        "amount_candidates": amounts,
    }
    for i in range(16):
        row[f"col_{i+1}"] = cols[i] if i < len(cols) else None
    return row


def discover_download_links(html_path: Path, base_url: str) -> list[str]:
    """Find downloadable tariff files even when the page hides them in JS/data attributes.

    Several carrier pages render the visible «Скачать» button with JavaScript rather
    than a plain <a href>. The old collector inspected anchors only, so it could
    download the page successfully and still report «прайс не загружен». Scan common
    URL-bearing attributes and raw HTML as well, then rank real files/tariff endpoints.
    """
    from urllib.parse import urljoin

    html = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    keywords = ["тариф", "tarif", "price", "прайс", "mosk", "моск", "санкт", "spb", "доп", "экспед", "перевоз", "download", "export"]
    exts = [".xls", ".xlsx", ".pdf", ".zip", ".docx", ".doc", ".csv"]
    pos = 0

    def add_candidate(raw_url: str, context: str = "") -> None:
        nonlocal pos
        pos += 1
        raw_url = (raw_url or "").strip().strip("\"'")
        if not raw_url or raw_url.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        raw_url = raw_url.replace("\\/", "/").replace("&amp;", "&")
        full = urljoin(base_url, raw_url)
        if not full.startswith("http") or full in seen:
            return
        low = (context + " " + raw_url).lower().replace("ё", "е")
        has_ext = any(ext in raw_url.lower().split("?")[0] for ext in exts)
        has_keyword = any(k in low for k in keywords)
        if not (has_ext or has_keyword):
            return
        seen.add(full)
        score = 0
        if "сборн" in low and "моск" in low and ("из г" in low or "из моск" in low):
            score += 140
        elif "сборн" in low and "моск" in low:
            score += 100
        if "сборн" in low and ("санкт" in low or "spb" in low):
            score += 70
        if has_ext:
            score += 50
        if any(ext in raw_url.lower() for ext in [".xlsx", ".xls", ".csv"]):
            score += 25
        if "download" in low or "export" in low or "скач" in low:
            score += 20
        if "тариф" in low or "tarif" in low or "прайс" in low or "price" in low:
            score += 15
        if any(x in low for x in ["упаков", "страх", "хранен", "курьер", "погруз", "прр"]):
            score -= 25
        ranked.append((-score, pos, full))

    url_attrs = ("href", "src", "data-url", "data-href", "data-download", "data-file", "data-src", "action", "formaction")
    for el in soup.find_all(True):
        context = " ".join(filter(None, [el.get_text(" ", strip=True), str(el.get("title") or ""), str(el.get("aria-label") or ""), str(el.get("class") or "")]))
        for attr in url_attrs:
            if el.has_attr(attr):
                add_candidate(str(el.get(attr) or ""), context)
        onclick = str(el.get("onclick") or "")
        if onclick:
            for m in re.finditer(r'''["']([^"']+(?:\.xlsx?|\.pdf|\.zip|\.csv|download|export|tarif|price)[^"']*)["']''', onclick, re.I):
                add_candidate(m.group(1), context + " " + onclick)

    raw_patterns = [
        r'''["']((?:https?:)?//[^"'<>\s]+(?:\.xlsx?|\.pdf|\.zip|\.csv)(?:\?[^"'<>\s]*)?)["']''',
        r'''["'](/[^"'<>\s]+(?:\.xlsx?|\.pdf|\.zip|\.csv)(?:\?[^"'<>\s]*)?)["']''',
        r'''["'](/[^"'<>\s]*(?:download|export|tarif|price)[^"'<>\s]*)["']''',
    ]
    for pattern in raw_patterns:
        for m in re.finditer(pattern, html, re.I):
            snippet = html[max(0, m.start() - 180):min(len(html), m.end() + 180)]
            add_candidate(m.group(1), re.sub(r"\s+", " ", snippet))

    ranked.sort()
    return [url for _, _, url in ranked[:20]]


def _valid_pdf(body: bytes, content_type: str = "") -> bool:
    return bool(body and len(body) > 1500 and (body[:4] == b"%PDF" or "pdf" in (content_type or "").lower()))


def _dellin_city_id(source: dict[str, Any]) -> int:
    try:
        return int(source.get("city_id") or (3 if source.get("block_key") != "spb_outbound" else 1))
    except Exception:
        return 3


def download_dellin_pdf(source: dict[str, Any]) -> dict[str, Any]:
    """Надёжный коннектор официальных PDF Деловых Линий.

    Использует несколько вариантов URL, повторные попытки, проверку сигнатуры PDF
    и локальный кэш. Для Москвы в дистрибутив включён официальный резервный снимок.
    """
    started = time.time()
    city_id = _dellin_city_id(source)
    cache_path = CACHE_DIR / f"dellin_city_{city_id}.pdf"
    fallback_path = FALLBACK_DIR / f"dellin_{'moscow' if city_id == 3 else 'spb'}.pdf"
    candidates = [
        f"https://www.dellin.ru/pricelist_pdf/?city={city_id}&is_future=0&is_region=0",
        f"https://dellin.ru/pricelist_pdf/?city={city_id}&is_future=0&is_region=0",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.7",
        "Referer": "https://www.dellin.ru/documents/",
        "Cache-Control": "no-cache",
    }
    # Один PDF используется сразу для нескольких тарифных блоков; не скачиваем его повторно.
    cached_run = DELLIN_RUN_CACHE.get(city_id)
    if cached_run:
        fname = f"dellin_{city_id}_{slugify(source['block_key'])}_{cached_run['freshness']}.pdf"
        fpath = DOWNLOAD_DIR / fname
        fpath.write_bytes(cached_run["body"])
        return {
            "status": "downloaded" if cached_run["freshness"] == "live" else "cached",
            "status_code": 200,
            "url": source.get("url") or candidates[0],
            "final_url": cached_run.get("final_url") or candidates[0],
            "content_type": "application/pdf",
            "file": fname,
            "path": str(fpath),
            "size_bytes": len(cached_run["body"]),
            "extension": ".pdf",
            "duration_sec": round(time.time() - started, 2),
            "error": cached_run.get("message", ""),
            "connector": "dellin_pdf",
            "freshness": cached_run["freshness"],
        }

    errors: list[str] = []
    # Точный официальный endpoint. Второй URL — резерв без www.
    for url in candidates:
        try:
            try:
                resp = SESSION.get(url, timeout=(8, 22), allow_redirects=True, headers=headers)
            except Exception as requests_exc:
                try:
                    resp = _curl_response(url, headers=headers, timeout=30)
                except Exception as curl_exc:
                    try:
                        resp = _powershell_response(url, headers=headers, timeout=40)
                    except Exception as ps_exc:
                        try:
                            resp = _browser_response(url, headers=headers, timeout=45)
                        except Exception as browser_exc:
                            raise RuntimeError(f"requests: {requests_exc}; curl: {curl_exc}; powershell: {ps_exc}; browser: {browser_exc}") from requests_exc
            body = resp.content or b""
            ct = resp.headers.get("content-type", "")
            # A CDN/WAF can answer requests with an HTML challenge rather than
            # raising an exception.  Retry that case through the OS TLS stack as
            # well before declaring the official PDF unavailable.
            if not (resp.ok and _valid_pdf(body, ct)):
                try:
                    curl_resp = _curl_response(url, headers=headers, timeout=30)
                    curl_body = curl_resp.content or b""
                    curl_ct = curl_resp.headers.get("content-type", "")
                    if curl_resp.ok and _valid_pdf(curl_body, curl_ct):
                        resp, body, ct = curl_resp, curl_body, curl_ct
                except Exception:
                    pass
                if not (resp.ok and _valid_pdf(body, ct)):
                    for fallback in (_powershell_response, _browser_response):
                        try:
                            alt_resp = fallback(url, headers=headers, timeout=45)
                            alt_body = alt_resp.content or b""
                            alt_ct = alt_resp.headers.get("content-type", "")
                            if alt_resp.ok and _valid_pdf(alt_body, alt_ct):
                                resp, body, ct = alt_resp, alt_body, alt_ct
                                break
                        except Exception:
                            pass
            if resp.ok and _valid_pdf(body, ct):
                cache_path.write_bytes(body)
                DELLIN_RUN_CACHE[city_id] = {"body": body, "freshness": "live", "final_url": resp.url, "message": ""}
                fname = f"dellin_{city_id}_{slugify(source['block_key'])}.pdf"
                fpath = DOWNLOAD_DIR / fname
                fpath.write_bytes(body)
                return {
                    "status": "downloaded", "status_code": resp.status_code,
                    "url": source.get("url") or url, "final_url": resp.url,
                    "content_type": "application/pdf", "file": fname, "path": str(fpath),
                    "size_bytes": len(body), "extension": ".pdf",
                    "duration_sec": round(time.time() - started, 2), "error": "",
                    "connector": "dellin_pdf", "freshness": "live",
                }
            errors.append(f"{resp.status_code} {ct or 'unknown content'}")
        except Exception as exc:
            errors.append(str(exc))

    # Последний успешный кэш важнее временной сетевой ошибки.
    reserve = cache_path if cache_path.exists() and cache_path.stat().st_size > 1500 else fallback_path
    if reserve.exists() and _valid_pdf(reserve.read_bytes()[:4096], "application/pdf"):
        body = reserve.read_bytes()
        DELLIN_RUN_CACHE[city_id] = {"body": body, "freshness": "snapshot", "final_url": source.get("url") or candidates[0], "message": "Онлайн-источник временно недоступен — использован последний официальный снимок."}
        fname = f"dellin_{city_id}_{slugify(source['block_key'])}_snapshot.pdf"
        fpath = DOWNLOAD_DIR / fname
        fpath.write_bytes(body)
        return {
            "status": "cached",
            "status_code": 200,
            "url": source.get("url") or candidates[0],
            "final_url": source.get("url") or candidates[0],
            "content_type": "application/pdf",
            "file": fname,
            "path": str(fpath),
            "size_bytes": fpath.stat().st_size,
            "extension": ".pdf",
            "duration_sec": round(time.time() - started, 2),
            "error": "Онлайн-источник временно недоступен — использован последний официальный снимок.",
            "connector": "dellin_pdf",
            "freshness": "snapshot",
        }
    return {
        "status": "temporarily_unavailable",
        "url": source.get("url") or candidates[0],
        "duration_sec": round(time.time() - started, 2),
        "error": "Официальный PDF временно недоступен. Повторите обновление позже; приложение продолжает работу.",
        "connector": "dellin_pdf",
        "diagnostics": errors[-4:],
    }


def extract_dellin_section(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    """Разбирает PDF Деловых Линий по смысловым разделам, а не по одиночным числам."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            page_text = page.extract_text() or ""
        pages.append(page_text)

    city_page = next((i for i, txt in enumerate(pages) if "Тарифы на доставку от адреса" in txt), None)
    block = source.get("block_key")
    if city_page is None:
        selected = list(range(len(pages)))
        section_name = "Тарифы Деловых Линий"
    elif block in ("moscow_outbound", "spb_outbound"):
        selected = list(range(0, city_page))
        section_name = "Межтерминальная перевозка"
    elif block == "moscow_city":
        selected = [city_page]
        section_name = "Доставка от адреса и до адреса"
    else:
        selected = list(range(city_page + 1, len(pages)))
        section_name = "Дополнительные услуги"

    rows: list[dict[str, Any]] = []
    for page_index in selected:
        raw_lines = [re.sub(r"\s+", " ", line).strip() for line in pages[page_index].splitlines()]
        for line_index, line in enumerate(raw_lines, start=1):
            if not line:
                continue
            looks_like_heading = any(word in line.lower() for word in ["тариф", "стоимость", "упаков", "страхован", "хранение", "простой", "направление"])
            looks_like_row = bool(amount_candidates(line)) or len(line.split()) >= 4
            if not (looks_like_heading or looks_like_row):
                continue
            row = make_row(source, "dellin_pdf", path.name, f"{section_name} · стр. {page_index + 1}", line_index, [section_name, line], line, parent_file)
            row["section"] = section_name
            row["connector"] = "dellin_pdf"
            rows.append(row)
            if len(rows) >= 7000:
                return rows
    return rows


def build_dellin_bootstrap_results() -> dict[str, Any] | None:
    fallback = FALLBACK_DIR / "dellin_moscow.pdf"
    if not fallback.exists():
        return None
    data = load_sources()
    results = empty_results()
    results["snapshot"] = True
    results["snapshot_message"] = "Показан резервный снимок официального PDF Деловых Линий для Москвы. Нажмите «Обновить данные» для онлайн-сбора."
    results["collected_at"] = None
    snapshot_copy = DOWNLOAD_DIR / "dellin_moscow_official_snapshot.pdf"
    if not snapshot_copy.exists():
        shutil.copy2(fallback, snapshot_copy)
    results["files"].append({
        "status": "cached", "url": "https://www.dellin.ru/pricelist_pdf/?city=3&is_future=0&is_region=0",
        "final_url": "https://www.dellin.ru/pricelist_pdf/?city=3&is_future=0&is_region=0",
        "content_type": "application/pdf", "file": snapshot_copy.name, "path": str(snapshot_copy),
        "size_bytes": snapshot_copy.stat().st_size, "extension": ".pdf", "error": "",
        "connector": "dellin_pdf", "freshness": "snapshot",
    })
    for source in data.get("sources", []):
        if source.get("company") != "ДЛ" or _dellin_city_id(source) != 3:
            continue
        try:
            rows = extract_dellin_section(fallback, source)
        except Exception:
            rows = []
        results["rows"].extend(rows)
        results["sources"].append({
            "id": source["id"], "company": "ДЛ", "block_key": source["block_key"],
            "block_title": source["block_title"], "source_type": source.get("source_type", ""),
            "url": source.get("url", ""), "instruction": source.get("instruction", ""),
            "status": "snapshot", "message": "Резервный снимок официального PDF", "files": [snapshot_copy.name],
            "rows_extracted": len(rows), "freshness": "snapshot",
        })
    results["summary"] = build_summary(results)
    return results

def download_url(url: str, source: dict[str, Any], suffix: str = "") -> dict[str, Any]:
    if source.get("connector") == "dellin_pdf" or source.get("company") == "ДЛ":
        return download_dellin_pdf(source)
    started = time.time()
    if not url:
        return {"status": "missing", "error": "Нет ссылки", "url": url, "duration_sec": 0}

    if url.strip().upper() == "НЕТУ":
        return {"status": "missing", "error": "В таблице указано НЕТУ", "url": url, "duration_sec": 0}

    if is_yandex_viewer(url):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        name = unquote(qs.get("name", ["yandex_docs_viewer"])[0])
        return {
            "status": "manual_needed",
            "error": f"Yandex Docs viewer, нужна прямая ссылка на файл: {name}",
            "url": url,
            "duration_sec": round(time.time() - started, 2),
            "filename_hint": name,
        }

    try:
        request_headers = {
            "Referer": source.get("reference_url") or source.get("url") or url,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        }
        try:
            resp = SESSION.get(url, timeout=18, allow_redirects=True, headers=request_headers)
        except Exception as direct_exc:
            # Try OS curl first, then the installed browser network stack.
            curl_error = None
            try:
                resp = _curl_response(url, headers=request_headers, timeout=35)
            except Exception as curl_exc:
                curl_error = curl_exc
                try:
                    resp = _powershell_response(url, headers=request_headers, timeout=40)
                    curl_error = None
                except Exception as ps_exc:
                    try:
                        resp = _browser_response(url, headers=request_headers, timeout=45)
                        curl_error = None
                    except Exception as browser_exc:
                        curl_error = RuntimeError(f"curl: {curl_exc}; powershell: {ps_exc}; browser: {browser_exc}")

            if curl_error is not None and (urlparse(url).hostname or '').lower() == 'wernerus.ru':
                # Werner-specific DNS bypass kept as a final fallback.
                parsed = urlparse(url)
                ip_url = parsed._replace(netloc='109.172.112.110').geturl()
                ip_headers = dict(request_headers)
                ip_headers['Host'] = 'wernerus.ru'
                try:
                    resp = SESSION.get(ip_url, timeout=18, allow_redirects=True, headers=ip_headers, verify=False)
                    curl_error = None
                except Exception:
                    relay = 'https://r.jina.ai/https://wernerus.ru' + (parsed.path or '/')
                    if parsed.query:
                        relay += '?' + parsed.query
                    try:
                        resp = SESSION.get(relay, timeout=25, allow_redirects=True, headers=request_headers)
                        curl_error = None
                    except Exception as relay_exc:
                        curl_error = RuntimeError(f"{curl_error}; relay: {relay_exc}")
            if curl_error is not None:
                raise RuntimeError(f"requests: {direct_exc}; fallbacks: {curl_error}") from direct_exc
        body = resp.content or b""
        ct = resp.headers.get("content-type", "")
        expected = str(source.get("document_format") or "").upper()
        def expected_ok(payload: bytes, content_type: str) -> bool:
            if expected == "PDF":
                return _valid_pdf(payload, content_type)
            if expected == "XLSX":
                return payload.startswith(b"PK\x03\x04") or "spreadsheetml" in content_type.lower()
            if expected == "XLS":
                return payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1")) or "ms-excel" in content_type.lower()
            return True

        # A WAF can return HTTP 200 with an HTML challenge for a PDF/XLSX URL.
        # Treat that as a failed download and retry with the alternate network
        # stacks instead of feeding the challenge page to the tariff parser.
        if expected in {"PDF", "XLS", "XLSX"} and not expected_ok(body, ct):
            for fallback in (_curl_response, _powershell_response, _browser_response):
                try:
                    alt = fallback(url, headers=request_headers, timeout=45)
                    alt_body = alt.content or b""
                    alt_ct = alt.headers.get("content-type", "")
                    if alt.ok and expected_ok(alt_body, alt_ct):
                        resp, body, ct = alt, alt_body, alt_ct
                        break
                except Exception:
                    continue
        ext = guess_extension(resp.url or url, ct, body)
        safe_company = slugify(source["company"])
        safe_block = slugify(source["block_title"])
        safe_id = slugify(source["id"])
        fname = f"{safe_id}_{safe_company}_{safe_block}{suffix}{ext}"
        fpath = DOWNLOAD_DIR / fname
        fpath.write_bytes(body)
        return {
            "status": "downloaded" if resp.ok else "http_error",
            "status_code": resp.status_code,
            "url": url,
            "final_url": resp.url,
            "content_type": ct,
            "file": fname,
            "path": str(fpath),
            "size_bytes": len(body),
            "extension": ext,
            "duration_sec": round(time.time() - started, 2),
            "error": "" if resp.ok else f"HTTP {resp.status_code}",
        }
    except Exception as e:
        return {
            "status": "error",
            "url": url,
            "duration_sec": round(time.time() - started, 2),
            "error": str(e),
        }


def extract_file(path: Path, source: dict[str, Any], parent_file: str | None = None) -> list[dict[str, Any]]:
    if source.get("connector") == "dellin_pdf" or source.get("company") == "ДЛ":
        return extract_dellin_section(path, source, parent_file)
    ext = path.suffix.lower()
    if ext == ".xlsx":
        return extract_from_xlsx(path, source, parent_file)
    if ext == ".xls":
        return extract_from_xls(path, source, parent_file)
    if ext == ".csv":
        return extract_from_csv(path, source, parent_file)
    if ext == ".pdf":
        return extract_from_pdf(path, source, parent_file)
    if ext in [".html", ".htm", ".bin"]:
        return extract_from_html(path, source, parent_file)
    if ext == ".docx":
        return extract_from_docx(path, source, parent_file)
    if ext == ".zip":
        out_rows: list[dict[str, Any]] = []
        extract_dir = DOWNLOAD_DIR / (path.stem + "_unzipped")
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(extract_dir)
        for child in extract_dir.rglob("*"):
            if child.is_file() and child.suffix.lower() in [".xlsx", ".xls", ".csv", ".pdf", ".html", ".htm", ".docx"]:
                try:
                    out_rows.extend(extract_file(child, source, parent_file=path.name))
                except Exception:
                    pass
        return out_rows
    return []


def build_summary(results: dict[str, Any]) -> dict[str, Any]:
    data = load_sources()
    statuses = Counter(s.get("status", "unknown") for s in results.get("sources", []))
    company_rows = Counter(r.get("company") for r in results.get("rows", []))
    block_rows = Counter(r.get("block_title") for r in results.get("rows", []))
    format_counts = Counter(r.get("format") for r in results.get("rows", []))
    company_source_counts = Counter(s.get("company") for s in data.get("sources", []))
    block_source_counts = Counter(s.get("block_title") for s in data.get("sources", []))
    source_type_counts = Counter(
        "Прямые файлы" if "скачать файл" in (s.get("source_type") or "").lower() else "Веб-страницы"
        for s in data.get("sources", [])
    )

    company_amounts: dict[str, list[float]] = defaultdict(list)
    block_amounts: dict[str, list[float]] = defaultdict(list)
    for row in results.get("rows", []):
        values = row.get("amount_candidates") or []
        # Dashboard values are heuristic; remove obvious tiny counters and extreme IDs.
        clean = [float(v) for v in values if isinstance(v, (int, float)) and 50 <= float(v) <= 10_000_000]
        if not clean:
            continue
        company_amounts[row.get("company")].extend(clean)
        block_amounts[row.get("block_title")].extend(clean)

    def stats_map(groups: dict[str, list[float]]) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        for key, values in groups.items():
            if not key or not values:
                continue
            ordered = sorted(values)
            # Winsorize the dashboard average to limit the effect of IDs and table totals.
            trim = max(0, int(len(ordered) * 0.05))
            core = ordered[trim:len(ordered)-trim] if len(ordered) > 20 and len(ordered)-2*trim > 0 else ordered
            out[key] = {
                "min": round(min(ordered), 2),
                "avg": round(sum(core) / len(core), 2),
                "max": round(max(ordered), 2),
                "count": len(ordered),
            }
        return out

    manual_count = sum(1 for s in results.get("sources", []) if s.get("status") == "manual_needed")
    return {
        "companies_total": len(data.get("companies", [])),
        "sources_total": len(data.get("sources", [])),
        "sources_done": len(results.get("sources", [])),
        "files_downloaded": len([f for f in results.get("files", []) if f.get("status") == "downloaded"]),
        "rows_extracted": len(results.get("rows", [])),
        "errors": len(results.get("errors", [])) + manual_count,
        "status_counts": dict(statuses),
        "company_rows": dict(company_rows),
        "block_rows": dict(block_rows),
        "format_counts": dict(format_counts),
        "company_source_counts": dict(company_source_counts),
        "block_source_counts": dict(block_source_counts),
        "source_type_counts": dict(source_type_counts),
        "company_amount_stats": stats_map(company_amounts),
        "block_amount_stats": stats_map(block_amounts),
    }


def collect_sources(selected_companies: list[str] | None = None, selected_blocks: list[str] | None = None, limit: int | None = None, selected_source_ids: list[str] | None = None) -> dict[str, Any]:
    """Collect official sources without erasing previously collected companies.

    v11 recreated results.json from scratch on every partial refresh. If only one
    company was refreshed, all previously parsed rows for the other companies
    disappeared from the catalog. v12 merges refreshed source IDs into the
    existing registry and keeps untouched rows/files/statuses.
    """
    DELLIN_RUN_CACHE.clear()
    data = load_sources()
    selected_companies = selected_companies or []
    selected_blocks = selected_blocks or []
    selected_source_ids = selected_source_ids or []

    # Bootstrap contains the bundled official Dellin snapshot on a fresh install.
    previous = load_results()
    results = empty_results()
    results["collected_at"] = now_iso()

    target_sources = []
    processed = 0
    for source in data["sources"]:
        if source.get("collect_enabled") is False:
            continue
        if selected_companies and source["company"] not in selected_companies:
            continue
        if selected_blocks and source["block_key"] not in selected_blocks:
            continue
        if selected_source_ids and str(source.get("id")) not in {str(x) for x in selected_source_ids}:
            continue
        if limit and processed >= limit:
            break
        target_sources.append(source)
        processed += 1

    target_ids = {str(s.get("id")) for s in target_sources}

    # Keep all untouched prior evidence. This matters for per-company refreshes.
    results["sources"] = [
        row for row in previous.get("sources", [])
        if isinstance(row, dict) and str(row.get("id")) not in target_ids
    ]
    results["rows"] = [
        row for row in previous.get("rows", [])
        if isinstance(row, dict) and str(row.get("source_id")) not in target_ids
    ]
    results["files"] = [
        row for row in previous.get("files", [])
        if not (isinstance(row, dict) and str(row.get("source_id", "")) in target_ids)
    ]
    results["errors"] = [
        row for row in previous.get("errors", [])
        if isinstance(row, dict) and str(row.get("source_id")) not in target_ids
    ]

    for source in target_sources:
        status = {
            "id": source["id"],
            "company": source["company"],
            "block_key": source["block_key"],
            "block_title": source["block_title"],
            "source_type": source.get("source_type", ""),
            "url": source.get("url", ""),
            "instruction": source.get("instruction", ""),
            "xlsx_row": source.get("xlsx_row"),
            "title": source.get("title", ""),
            "document_format": source.get("document_format", ""),
            "coverage": source.get("coverage", ""),
            "usable_for_price": bool(source.get("usable_for_price")),
            "effective_date": source.get("effective_date"),
            "reference_url": source.get("reference_url", source.get("url", "")),
            "started_at": now_iso(),
            "status": "pending",
            "message": "",
            "files": [],
            "rows_extracted": 0,
        }

        if not source.get("url"):
            status["status"] = "manual_needed" if source.get("instruction") else "missing"
            status["message"] = source.get("instruction") or "Нет ссылки"
            results["sources"].append(status)
            results["summary"] = build_summary(results)
            save_results(results)
            continue

        dl = download_url(source["url"], source)
        status.update({
            "status": dl.get("status", "unknown"),
            "message": dl.get("error", ""),
            "status_code": dl.get("status_code"),
            "duration_sec": dl.get("duration_sec"),
            "content_type": dl.get("content_type", ""),
            "freshness": dl.get("freshness", ""),
            "connector": dl.get("connector", source.get("connector", "")),
        })

        if dl.get("file"):
            dl["source_id"] = source["id"]
            results["files"].append(dl)
            status["files"].append(dl["file"])
            fpath = Path(dl["path"])
            try:
                extracted = extract_file(fpath, source)
                results["rows"].extend(extracted)
                status["rows_extracted"] += len(extracted)
                if not extracted and should_scan_page(source.get("source_type", ""), dl.get("extension", ""), dl.get("content_type", "")):
                    status["message"] = (status.get("message", "") + " HTML скачан, но таблицы/строки не распознаны.").strip()
                status["status"] = "parsed" if extracted else status["status"]
            except Exception as e:
                status["status"] = "parse_error"
                status["message"] = str(e)
                results["errors"].append({
                    "source_id": source["id"], "company": source["company"],
                    "block_title": source["block_title"], "url": source.get("url", ""),
                    "stage": "parse", "error": str(e),
                })

            if should_scan_page(source.get("source_type", ""), dl.get("extension", ""), dl.get("content_type", "")) and fpath.exists():
                try:
                    links = discover_download_links(fpath, dl.get("final_url") or source["url"])
                    status["discovered_links"] = links[:12]
                    # v11 stopped at five links; PЭК pages can expose several tariff files.
                    link_limit = 6 if source.get("company") in {"Werner", "Главтрасса"} else 12
                    for i, link in enumerate(links[:link_limit], start=1):
                        sub = download_url(link, source, suffix=f"_found{i}")
                        if sub.get("file"):
                            sub["source_id"] = source["id"]
                            results["files"].append(sub)
                            status["files"].append(sub["file"])
                            try:
                                sub_rows = extract_file(Path(sub["path"]), source)
                                results["rows"].extend(sub_rows)
                                status["rows_extracted"] += len(sub_rows)
                                if sub_rows:
                                    status["status"] = "parsed"
                            except Exception as e:
                                results["errors"].append({
                                    "source_id": source["id"], "company": source["company"],
                                    "block_title": source["block_title"], "url": link,
                                    "stage": "parse_discovered", "error": str(e),
                                })
                except Exception as e:
                    status["message"] = (status.get("message", "") + f" Link scan error: {e}").strip()

        bad_status = status["status"] in ["error", "http_error", "parse_error", "manual_needed", "missing", "temporarily_unavailable"]
        new_rows_for_source = [r for r in results.get("rows", []) if isinstance(r, dict) and str(r.get("source_id")) == str(source["id"])]
        new_files_for_source = [f for f in results.get("files", []) if isinstance(f, dict) and str(f.get("source_id")) == str(source["id"])]
        prior_rows = [r for r in previous.get("rows", []) if isinstance(r, dict) and str(r.get("source_id")) == str(source["id"])]
        prior_files = [f for f in previous.get("files", []) if isinstance(f, dict) and str(f.get("source_id", "")) == str(source["id"])]
        prior_sources = [r for r in previous.get("sources", []) if isinstance(r, dict) and str(r.get("id")) == str(source["id"])]

        # A failed refresh must never erase a previously parsed official price.
        # Restore last-good rows/files and expose the refresh failure only as a
        # warning on the preserved source status.
        if (bad_status or not new_rows_for_source) and prior_rows:
            results["rows"] = [r for r in results.get("rows", []) if not (isinstance(r, dict) and str(r.get("source_id")) == str(source["id"]))]
            results["rows"].extend(prior_rows)
            results["files"] = [f for f in results.get("files", []) if not (isinstance(f, dict) and str(f.get("source_id", "")) == str(source["id"]))]
            results["files"].extend(prior_files)
            old = dict(prior_sources[-1]) if prior_sources else dict(status)
            old["status"] = "stale_preserved"
            old["refresh_warning"] = status.get("message") or f"Обновление не удалось ({status.get('status')})"
            old["message"] = "Последний успешно распознанный официальный прайс сохранён; новое обновление не заменило его из-за временной ошибки источника."
            old["last_refresh_attempt"] = now_iso()
            status = old
            bad_status = False

        if bad_status:
            results["errors"].append({
                "source_id": source["id"], "company": source["company"],
                "block_title": source["block_title"], "url": source.get("url", ""),
                "stage": status["status"], "error": status.get("message", ""),
            })

        results["sources"].append(status)
        # Save every completed carrier immediately. A slow or broken site later
        # in the queue must not hide tariffs already downloaded successfully.
        results["summary"] = build_summary(results)
        save_results(results)

    results["summary"] = build_summary(results)
    save_results(results)
    return results


def create_excel_export(results: dict[str, Any]) -> Path:
    """Экспорт на базе исходной Excel-матрицы пользователя.

    Первый рабочий лист повторяет структуру присланного документа. Дополнительные
    листы содержат сводку, статусы сбора и распознанные строки.
    """
    from openpyxl import load_workbook
    from openpyxl.chart import BarChart, DoughnutChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    base_path = DATA_DIR / "source_links_5_companies.xlsx"
    wb = load_workbook(base_path)
    matrix = wb[wb.sheetnames[0]]
    matrix.title = "Матрица источников"

    # Сводка ставится первой, но исходная матрица остаётся без изменения структуры.
    summary_ws = wb.create_sheet("Сводка", 0)
    status_ws = wb.create_sheet("Статус сбора")
    rows_ws = wb.create_sheet("Распознанные данные")
    errors_ws = wb.create_sheet("Ошибки")

    colors = {
        "dark": "20242A",
        "accent": "2563EB",
        "accent_soft": "EAF0FF",
        "green": "16845B",
        "green_soft": "EAF7F1",
        "amber": "A36813",
        "amber_soft": "FFF6E6",
        "red": "C53D4C",
        "red_soft": "FFF0F2",
        "muted": "6F7782",
        "border": "E1E5EA",
        "surface": "F7F8FA",
        "white": "FFFFFF",
    }
    thin = Side(style="thin", color=colors["border"])
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def header_style(cell, fill=None):
        cell.fill = PatternFill("solid", fgColor=fill or colors["dark"])
        cell.font = Font(color=colors["white"], bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    def body_style(cell):
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        cell.border = border

    # --- Сводка ---
    summary_ws.sheet_view.showGridLines = False
    summary_ws["A1"] = "Тарифы конкурентов"
    summary_ws["A1"].font = Font(size=20, bold=True, color=colors["dark"])
    summary_ws["A2"] = "Структура: 5 компаний × 4 тарифных блока"
    summary_ws["A2"].font = Font(size=10, color=colors["muted"])
    summary_ws["A3"] = f"Последнее обновление: {results.get('collected_at') or 'ещё не запускали'}"
    summary_ws["A3"].font = Font(size=10, color=colors["muted"])

    kpis = [
        ("Компании", results.get("summary", {}).get("companies_total", 5)),
        ("Тарифные блоки", 4),
        ("Обработано источников", results.get("summary", {}).get("sources_done", 0)),
        ("Распознано строк", results.get("summary", {}).get("rows_extracted", 0)),
        ("Ошибки / ручные действия", results.get("summary", {}).get("errors", 0)),
    ]
    for i, (label, value) in enumerate(kpis, start=1):
        col = 1 + (i - 1) * 2
        summary_ws.cell(5, col, label)
        summary_ws.cell(6, col, value)
        summary_ws.merge_cells(start_row=5, start_column=col, end_row=5, end_column=col+1)
        summary_ws.merge_cells(start_row=6, start_column=col, end_row=7, end_column=col+1)
        for row in range(5, 8):
            for c in range(col, col+2):
                cell = summary_ws.cell(row, c)
                cell.fill = PatternFill("solid", fgColor=colors["white"])
                cell.border = border
                cell.alignment = Alignment(horizontal="left", vertical="center")
        summary_ws.cell(5, col).font = Font(size=9, color=colors["muted"])
        summary_ws.cell(6, col).font = Font(size=22, bold=True, color=colors["dark"])

    # Строки по компаниям
    summary_ws["A10"] = "Компания"
    summary_ws["B10"] = "Распознано строк"
    header_style(summary_ws["A10"], colors["dark"])
    header_style(summary_ws["B10"], colors["dark"])
    company_rows = results.get("summary", {}).get("company_rows", {})
    for idx, company in enumerate(load_sources().get("companies", []), start=11):
        summary_ws.cell(idx, 1, company)
        summary_ws.cell(idx, 2, company_rows.get(company, 0))
        body_style(summary_ws.cell(idx, 1))
        body_style(summary_ws.cell(idx, 2))

    chart = BarChart()
    chart.type = "bar"
    chart.style = 10
    chart.title = "Распознанные строки по компаниям"
    chart.height = 7.5
    chart.width = 14
    chart.add_data(Reference(summary_ws, min_col=2, min_row=10, max_row=15), titles_from_data=True)
    chart.set_categories(Reference(summary_ws, min_col=1, min_row=11, max_row=15))
    chart.legend = None
    summary_ws.add_chart(chart, "D10")

    # Статусы
    status_counts = results.get("summary", {}).get("status_counts", {})
    summary_ws["A18"] = "Статус"
    summary_ws["B18"] = "Количество"
    header_style(summary_ws["A18"], colors["accent"])
    header_style(summary_ws["B18"], colors["accent"])
    row = 19
    for status, count in status_counts.items():
        summary_ws.cell(row, 1, status)
        summary_ws.cell(row, 2, count)
        body_style(summary_ws.cell(row, 1))
        body_style(summary_ws.cell(row, 2))
        row += 1
    if row > 19:
        donut = DoughnutChart()
        donut.title = "Статусы источников"
        donut.height = 7
        donut.width = 11
        donut.add_data(Reference(summary_ws, min_col=2, min_row=18, max_row=row-1), titles_from_data=True)
        donut.set_categories(Reference(summary_ws, min_col=1, min_row=19, max_row=row-1))
        summary_ws.add_chart(donut, "D19")

    for col in range(1, 11):
        summary_ws.column_dimensions[get_column_letter(col)].width = 15
    summary_ws.column_dimensions["A"].width = 24
    summary_ws.freeze_panes = "A10"

    # --- Статус сбора ---
    status_headers = ["Компания", "Тарифный блок", "Источник", "Статус", "Строк", "Файлы", "Сообщение", "URL", "Инструкция"]
    status_ws.append(status_headers)
    for c in status_ws[1]:
        header_style(c)
    source_by_id = {s.get("id"): s for s in load_sources().get("sources", [])}
    result_by_id = {s.get("id"): s for s in results.get("sources", [])}
    for source_id, source in source_by_id.items():
        result = result_by_id.get(source_id, {})
        status_ws.append([
            source.get("company"), source.get("block_title"), source.get("source_type"),
            result.get("status", "не запускали"), result.get("rows_extracted", 0),
            "\n".join(result.get("files", [])), result.get("message", ""),
            source.get("url", ""), source.get("instruction", "")
        ])
    for row_cells in status_ws.iter_rows(min_row=2):
        for c in row_cells:
            body_style(c)
    widths = [18, 28, 26, 18, 10, 24, 35, 48, 65]
    for idx, width in enumerate(widths, start=1):
        status_ws.column_dimensions[get_column_letter(idx)].width = width
    status_ws.freeze_panes = "A2"
    status_ws.auto_filter.ref = status_ws.dimensions

    # --- Распознанные данные ---
    row_headers = ["Компания", "Тарифный блок", "Формат", "Файл", "Лист / страница", "№ строки", "Минимальная сумма", "Максимальная сумма", "Исходная строка"] + [f"Колонка {i}" for i in range(1, 17)]
    rows_ws.append(row_headers)
    for c in rows_ws[1]:
        header_style(c)
    for item in results.get("rows", [])[:30000]:
        rows_ws.append([
            item.get("company"), item.get("block_title"), item.get("format"), item.get("file"),
            item.get("sheet"), item.get("row_index"), item.get("amount_min"), item.get("amount_max"),
            item.get("raw_text"),
        ] + [item.get(f"col_{i}") for i in range(1, 17)])
    for row_cells in rows_ws.iter_rows(min_row=2):
        for c in row_cells:
            body_style(c)
    for idx in range(1, len(row_headers)+1):
        rows_ws.column_dimensions[get_column_letter(idx)].width = 16
    rows_ws.column_dimensions["I"].width = 70
    rows_ws.freeze_panes = "A2"
    rows_ws.auto_filter.ref = rows_ws.dimensions

    # --- Ошибки ---
    errors_ws.append(["Компания", "Тарифный блок", "Этап", "Сообщение", "URL"])
    for c in errors_ws[1]:
        header_style(c, colors["red"])
    for item in results.get("errors", []):
        errors_ws.append([item.get("company"), item.get("block_title"), item.get("stage"), item.get("error"), item.get("url")])
    for source in results.get("sources", []):
        if source.get("status") in {"manual_needed", "missing", "temporarily_unavailable"}:
            errors_ws.append([source.get("company"), source.get("block_title"), source.get("status"), source.get("message"), source.get("url")])
    for row_cells in errors_ws.iter_rows(min_row=2):
        for c in row_cells:
            body_style(c)
    for idx, width in enumerate([18, 28, 20, 65, 48], start=1):
        errors_ws.column_dimensions[get_column_letter(idx)].width = width
    errors_ws.freeze_panes = "A2"

    # Небольшая подпись на исходной матрице, не ломая её расположение.
    matrix["B18"] = "Приложение использует эту матрицу как основную структуру: 5 компаний × 4 блока."
    matrix["B18"].font = Font(size=9, color=colors["muted"], italic=True)

    export_path = EXPORT_DIR / f"tariffs_competitors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(export_path)
    return export_path


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/sources")
def api_sources():
    return load_sources()


@app.get("/api/results")
def api_results():
    return load_results()


@app.post("/api/collect")
def api_collect(payload: dict[str, Any] = Body(default_factory=dict)):
    companies = payload.get("companies") or []
    blocks = payload.get("blocks") or []
    limit = payload.get("limit")
    try:
        limit = int(limit) if limit else None
    except Exception:
        limit = None
    results = collect_sources(companies, blocks, limit)
    return results


@app.post("/api/reset")
def api_reset():
    if RESULTS_PATH.exists():
        RESULTS_PATH.unlink()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@app.get("/api/export/excel")
def api_export_excel():
    results = load_results()
    path = create_excel_export(results)
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/downloads/{filename}")
def api_download(filename: str):
    safe = Path(filename).name
    path = DOWNLOAD_DIR / safe
    if not path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(path, filename=safe)


@app.get("/health")
def health():
    return {"ok": True, "time": now_iso()}
