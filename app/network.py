"""Verified, bounded HTTP downloads shared by the online sources."""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
import time
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Timeout

MAX_BYTES = 50 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
_connections = threading.local()


@contextmanager
def connection_scope():
    """Reuse verified connections within one worker's batch; close them afterwards."""
    previous = getattr(_connections, 'clients', None)
    clients = {}
    _connections.clients = clients
    try:
        yield
    finally:
        for client in clients.values():
            client.close()
        _connections.clients = previous


def _client(label, factory):
    clients = getattr(_connections, 'clients', None)
    if clients is None:
        return factory()
    if label not in clients:
        clients[label] = factory()
    return nullcontext(clients[label])


class DownloadError(RuntimeError):
    pass


class RejectedResponse(DownloadError):
    """The server replied, but the reply is not usable tariff evidence."""


@dataclass
class Download:
    content: bytes
    url: str
    transport: str
    content_type: str = ""
    status_code: int = 200


class TLS12Adapter(HTTPAdapter):
    def __init__(self):
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.maximum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        # TLS to the carrier through a configured proxy uses the same bounds.
        kwargs["ssl_context"] = self.context
        return super().proxy_manager_for(proxy, **kwargs)


def decode_console(raw: bytes) -> str:
    for encoding in ("utf-8", "cp866" if os.name == "nt" else "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            pass
    return raw.decode("utf-8", "replace")


def validate_reply(result: Download, expected: str = "") -> Download:
    if result.status_code >= 400:
        raise RejectedResponse(f"HTTP {result.status_code}: {urlsplit(result.url).hostname}")
    data = result.content
    if not data:
        raise RejectedResponse("Пустой ответ источника")
    if len(data) > MAX_BYTES:
        raise RejectedResponse("Прайс превышает допустимый размер 50 МБ")
    # A redirected login page or a WAF challenge must not become a tariff.
    prefix = data[:12000].decode("utf-8", "replace").lower()
    if any(x in prefix for x in ("<title>access denied", "<title>just a moment", "<title>доступ ограничен", "<title>401", "<title>403", "cf-chl-", "checking your browser")):
        raise RejectedResponse("Сайт вернул страницу проверки доступа вместо прайса")
    kind = expected.upper()
    valid = True
    if kind == "PDF":
        valid = data.startswith(b"%PDF")
    elif kind == "XLSX":
        valid = data.startswith(b"PK\x03\x04")
    elif kind == "XLS":
        valid = data.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    elif kind == "JSON":
        try:
            json.loads(data)
        except (ValueError, UnicodeError):
            valid = False
    if not valid:
        raise RejectedResponse(f"Вместо {kind} источник вернул другой формат ответа")
    return result


def _requests_download(url, headers, seconds, tls12=False):
    start = time.monotonic()
    def create_session():
        session = requests.Session()
        if tls12:
            session.mount("https://", TLS12Adapter())
        return session
    with _client('requests-tls12' if tls12 else 'requests', create_session) as session:
        with session.get(url, headers=headers, timeout=Timeout(total=seconds, connect=min(8, seconds), read=min(15, seconds)), stream=True) as response:
            if response.status_code >= 400:
                raise RejectedResponse(f"HTTP {response.status_code}: {urlsplit(response.url).hostname}")
            chunks = []
            total = 0
            for chunk in response.iter_content(16384):
                if time.monotonic() - start > seconds:
                    raise TimeoutError("Истекло время загрузки прайса")
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RejectedResponse("Прайс превышает допустимый размер 50 МБ")
                chunks.append(chunk)
            return Download(b"".join(chunks), response.url, "requests-tls12" if tls12 else "requests", response.headers.get("Content-Type", ""), response.status_code)


def _cffi_download(url, headers, seconds):
    # Independent TLS implementation, including on Windows. No browser/WAF bypass,
    # disabled certificate checks, changed proxies or modified DNS settings.
    from curl_cffi import requests as cr, CurlOpt, CurlSslVersion, CurlHttpVersion
    data = bytearray()
    def receive(chunk):
        if len(data) + len(chunk) > MAX_BYTES:
            raise RejectedResponse("Прайс превышает допустимый размер 50 МБ")
        data.extend(chunk)
    def create_session():
        return cr.Session(curl_options={CurlOpt.SSLVERSION: CurlSslVersion.TLSv1_2 | (int(CurlSslVersion.TLSv1_2) << 16),
                                      CurlOpt.IPRESOLVE: 1, CurlOpt.HTTP_VERSION: CurlHttpVersion.V1_1})
    with _client('curl-cffi-tls12', create_session) as session:
        response = session.get(url, headers=headers, timeout=seconds, verify=True, content_callback=receive)
        return Download(bytes(data), response.url, "curl-cffi-tls12", response.headers.get("Content-Type", ""), response.status_code)


def _curl_download(url, headers, seconds):
    with tempfile.TemporaryDirectory(prefix="tariff_http_") as td:
        dest = Path(td) / "response.bin"
        cmd = ["curl.exe" if os.name == "nt" else "curl", "--location", "--silent", "--show-error", "--globoff",
               "--connect-timeout", str(min(8, seconds)), "--max-time", str(seconds), "--max-filesize", str(MAX_BYTES),
               "--ipv4", "--http1.1", "--tlsv1.2", "--tls-max", "1.2", "--proto", "=http,https", "--proto-redir", "=https",
               "--output", str(dest), "--write-out", "%{http_code}\n%{url_effective}\n%{content_type}"]
        for name, value in headers.items():
            cmd += ["--header", f"{name}: {value}"]
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 1, check=False)
        if proc.returncode:
            raise DownloadError(decode_console(proc.stderr)[-1000:])
        meta = proc.stdout.decode("utf-8", "replace").split("\n", 2)
        return Download(dest.read_bytes(), meta[1], "curl-tls12", meta[2] if len(meta)>2 else "", int(meta[0]))


def download(url: str, *, referer=None, expected="", timeout=50, preferred_transport="") -> Download:
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("Требуется HTTP/HTTPS адрес официального источника")
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6", "Referer": referer or url}
    deadline = time.monotonic() + timeout
    errors = []
    attempts = [("requests", lambda s: _requests_download(url, headers, s)),
                ("requests-tls12", lambda s: _requests_download(url, headers, s, tls12=True)),
                ("curl-cffi-tls12", lambda s: _cffi_download(url, headers, s)),
                ("curl-tls12", lambda s: _curl_download(url, headers, s))]
    if preferred_transport:
        attempts.sort(key=lambda attempt: attempt[0] != preferred_transport)
    for label, fetch in attempts:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        try:
            result = validate_reply(fetch(min(16, remaining)), expected)
            start_host = (urlsplit(url).hostname or "").removeprefix("www.")
            end_host = (urlsplit(result.url).hostname or "").removeprefix("www.")
            if end_host != start_host and not end_host.endswith("." + start_host):
                raise RejectedResponse("Источник перенаправил запрос на посторонний сайт")
            if urlsplit(url).scheme == "https" and urlsplit(result.url).scheme != "https":
                raise RejectedResponse("Источник перенаправил HTTPS на незащищённый адрес")
            return result
        except RejectedResponse:
            # Do not retry an authorization refusal or parse an error page.
            raise
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:1400]}")
    raise DownloadError("; ".join(errors) or "Истекло время загрузки источника")


def _range_piece(url, start, end, etag, seconds):
    from curl_cffi import requests as cr, CurlOpt, CurlSslVersion, CurlHttpVersion
    headers={'Range':f'bytes={start}-{end}', 'Accept-Encoding':'identity', 'User-Agent':USER_AGENT}
    if etag:headers['If-Range']=etag
    data=bytearray()
    def receive(chunk):
        if len(data)+len(chunk)>end-start+1:
            raise RejectedResponse('Сервер не выполнил запрос части файла')
        data.extend(chunk)
    with cr.Session(curl_options={CurlOpt.SSLVERSION:CurlSslVersion.TLSv1_2|(int(CurlSslVersion.TLSv1_2)<<16),
                                  CurlOpt.HTTP_VERSION:CurlHttpVersion.V1_1, CurlOpt.IPRESOLVE:1}) as session:
        response=session.get(url,headers=headers,timeout=seconds,verify=True,content_callback=receive)
    return response,bytes(data)


def download_ranges(url, *, expected='XLSX', timeout=90, block_size=8000):
    """Recover interrupted static files using server-supported byte ranges.

    All pieces must have the same strong ETag and exact Content-Range. A partial
    response or a changed file is rejected, never used as tariff evidence.
    """
    from concurrent.futures import ThreadPoolExecutor
    deadline=time.monotonic()+timeout
    block=max(1024,min(14000,int(block_size)))
    host=(urlsplit(url).hostname or '').removeprefix('www.')
    if urlsplit(url).scheme!='https':raise ValueError('Для загрузки частями требуется HTTPS')
    def piece(start,end,etag=None,total=None):
        error=None
        for attempt in range(2):
            remaining=deadline-time.monotonic()
            if remaining<1:raise DownloadError('Истекло время загрузки файла частями')
            try:
                response,body=_range_piece(url,start,end,etag,min(12,remaining))
                if response.status_code!=206:raise RejectedResponse(f'HTTP {response.status_code}: сервер не подтвердил загрузку частями')
                final_host=(urlsplit(response.url).hostname or '').removeprefix('www.')
                if final_host!=host or urlsplit(response.url).scheme!='https':raise RejectedResponse('Часть файла перенаправлена на другой источник')
                found=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                tag=response.headers.get('ETag','')
                if not found or not tag.startswith('"') or not tag.endswith('"'):raise RejectedResponse('Нет точных границ или версии файла')
                lo,hi,size=map(int,found.groups())
                if size>MAX_BYTES or size<=0 or lo!=start or hi!=min(end,size-1) or len(body)!=hi-lo+1:raise RejectedResponse('Неполная или неверная часть прайса')
                if (etag is not None and tag!=etag) or (total is not None and size!=total):raise RejectedResponse('Прайс изменился во время загрузки; требуется новая попытка')
                return body,tag,size,response.url,response.headers.get('Content-Type','')
            except RejectedResponse:raise
            except Exception as exc:error=exc
        raise DownloadError(str(error))
    first,tag,size,final_url,content_type=piece(0,block-1)
    starts=list(range(block,size,block))
    with ThreadPoolExecutor(max_workers=4 if size>128000 else 2) as pool:
        chunks=list(pool.map(lambda start:piece(start,min(start+block-1,size-1),tag,size)[0],starts))
    return validate_reply(Download(first+b''.join(chunks),final_url,'curl-cffi-ranges',content_type),expected)


def explain_error(error):
    detail = str(error)
    low = detail.lower()
    code, summary, hint = "source", "Не удалось получить прайс", "Откройте официальный источник и повторите загрузку этой компании."
    if 'нет опубликованного тарифа для выбранного маршрута:' in low:
        code,summary,hint='route_unpublished','В источнике нет прайса этого маршрута','Выберите другое направление или проверьте возможность перевозки у компании. Цена другого маршрута не подставляется.'
    elif low.startswith('не все веса загружены:'):
        code, summary, hint = 'partial', 'Часть весовой таблицы не загружена', 'Полученные цены доступны. Нажмите «Повторить загрузку», чтобы проверить остальные веса.'
    elif any(x in low for x in ("http 401", "http 403", "проверки доступа", "access denied")):
        code, summary = "access", "Сайт ограничил доступ к ответу"
        hint = "Проверьте страницу источника. Для точного расчёта ДЛ можно указать свой API-ключ в «Доп. API»."
    elif any(x in low for x in ("certificate_verify_failed", "certificate verify failed", "certificate problem")):
        code, summary = "certificate", "Не подтверждён сертификат сайта"
        hint = "Проверьте дату компьютера и доверенные сертификаты Windows; проверка HTTPS остаётся включённой."
    elif any(x in low for x in ("ssl", "tls", "schannel", "eof", "connection_closed", "connection reset")):
        code, summary = "tls", "Соединение с сайтом оборвалось при подключении"
        hint = "Испробованы совместимые способы HTTPS. Проверьте, открывается ли источник в браузере на этом компьютере."
    elif any(x in low for x in ("timeout", "timed out", "время загрузки", "истекло время", "readtimeout")):
        code, summary = "timeout", "Сайт не ответил за отведённое время"
        hint = "Повторите загрузку этой компании позже; доступные прайсы уже сохранены."
    elif any(x in low for x in ("getaddrinfo", "nameresolution", "resolve host")):
        code, summary = "dns", "Не удалось найти адрес сайта"
        hint = "Проверьте подключение к интернету и открытие официального сайта."
    elif any(x in low for x in ("маршрут", "направлен", "другой формат", "диапазон", "распознан", "вместо")):
        code, summary = "format", "Источник ответил, но тариф не подтверждён"
        hint = "Проверьте направление и формат на странице источника. Неподтверждённая цена не считается LIVE."
    elif "конкретный вес" in low:
        code, summary, hint = "unsupported", "Калькулятор не публикует минимум", "Выберите конкретный вес."
    return {"code": code, "summary": summary, "hint": hint, "technical": detail[:5000]}
