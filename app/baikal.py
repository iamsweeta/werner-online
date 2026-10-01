"""Baikal Service's public calculator. No prices or city IDs are bundled."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .business_time import tariff_today
from .cities import normalize_city, UnpublishedTariff

BASE = 'https://request.baikalsr.ru'
PAGE = BASE + '/calculator/'
ENDPOINT = BASE + '/json/calculator'
_LIMIT = threading.BoundedSemaphore(2)


def select_city(data, name):
    """Match a city, never an identically named village or the first suggestion."""
    if not isinstance(data, list):
        raise ValueError('Байкал Сервис: изменился формат справочника городов')
    matches = {row['guid']: row for row in data if isinstance(row, dict)
               and row.get('guid') and normalize_city(row.get('name')) == normalize_city(name)
               and str(row.get('type')) == '3'}
    if len(matches) != 1:
        raise UnpublishedTariff(f'Байкал Сервис: город «{name}» не найден однозначно среди городов с терминалом')
    return next(iter(matches.values()))


def select_terminal(data, city, side):
    if not isinstance(data, dict) or (data.get('city') or {}).get('guid') != city['guid']:
        raise ValueError('Байкал Сервис: терминалы относятся к другому городу')
    allowed = {1, 3} if side == 'departure' else {2, 3}
    if data.get('onRoute') not in allowed:
        raise UnpublishedTariff(f'Байкал Сервис: выбранное направление недоступно для {city["name"]}')
    flag = 'start' if side == 'departure' else 'finish'
    terminals = [t for t in (data.get('affiliate') or {}).get('terminals', [])
                 if isinstance(t, dict) and t.get('id') and t.get(flag) == 1]
    if not terminals:
        raise UnpublishedTariff(f'Байкал Сервис: нет доступного терминала для {city["name"]}')
    # This is also the official form's default-selection rule.
    defaults = [t for t in terminals if t.get('base') == 1]
    if len(defaults) > 1:
        raise ValueError('Байкал Сервис: неоднозначный основной терминал')
    return defaults[0] if defaults else terminals[0]


def make_request(origin, destination, origin_terminal, destination_terminal, weight):
    weight = float(weight)
    if not math.isfinite(weight) or not 0 < weight <= 20000:
        raise ValueError('Байкал Сервис: вес вне диапазона 0–20000 кг')
    volume = round(max(.005, weight / 200), 4)
    # Use a group of standard pieces at large weights, not a 20-tonne single item.
    units = max(1, math.ceil(weight / 1000))
    fields = {
        'id': ''.join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex[:4]) + '_' + str(time.time_ns() // 1000000),
        'departure[cityguid]': origin['guid'],
        'departure[terminal]': str(origin_terminal['id']),
        'departure[date]': tariff_today().isoformat() + 'T00:00:00',
        'destination[cityguid]': destination['guid'],
        'destination[terminal]': str(destination_terminal['id']),
        'transport[]': 'auto',
    }
    cargo = dict(length=0, width=0, height=0, maxweight=0, volume=volume,
                 weight=weight, units=units, type='', typename='', oversized=0, estimatedcost=0)
    fields.update({f'cargo[summarycargo][{key}]': str(value) for key, value in cargo.items()})
    return fields


class PublicClient:
    """One regular HTTPS session for the whole weight grid, including CSRF."""
    def __init__(self, timeout=45):
        self.timeout = max(1, min(45, float(timeout)))
        self.session = requests.Session()

    def __enter__(self):
        try:
            page = self._request('GET', '/calculator/', is_json=False, page_redirects=2)
            token = BeautifulSoup(page, 'lxml').select_one('meta[name="csrf-token"]')
            if not token or not token.get('content'):
                raise ValueError('Байкал Сервис: публичная форма не вернула токен расчёта')
            bootstrap = re.search(rb'var\s+tabRequests\s*=\s*(\{[^;]+\})\s*;', page)
            state = json.loads(bootstrap.group(1)) if bootstrap else {}
            self.guid = state.get('guid')
            if not isinstance(self.guid, str) or not re.fullmatch(r'[a-f0-9-]{36}', self.guid):
                raise ValueError('Байкал Сервис: форма не вернула номер текущего расчёта')
            self.session.headers.update({'X-CSRF-Token': token['content'],
                                         'Referer': PAGE + '?guid=' + self.guid, 'Origin': BASE,
                                         'X-Requested-With': 'XMLHttpRequest',
                                         'Accept': 'application/json'})
            return self
        except BaseException:
            self.session.close()
            raise

    def __exit__(self, *args):
        self.session.close()

    def _request(self, method, path, *, is_json=True, page_redirects=0, **kwargs):
        # No redirects to logins, TLS exceptions, CAPTCHA solvers or retry storms.
        with self.session.request(method, BASE + path, timeout=self.timeout,
                                  allow_redirects=False, verify=True, stream=True, **kwargs) as response:
            if method == 'GET' and page_redirects and response.status_code in {301, 302, 303, 307, 308}:
                target = urlsplit(urljoin(BASE + path, response.headers.get('Location', '')))
                if target.scheme not in {'http', 'https'} or target.netloc != 'request.baikalsr.ru' or target.path.rstrip('/') != '/calculator':
                    raise ValueError('Байкал Сервис: калькулятор перенаправил запрос на другую страницу')
                # The site currently emits an http Location for its quote ID.
                # Reuse only its path on BASE (HTTPS), never downgrade TLS.
                response.close()
                return self._request('GET', target.path + ('?' + target.query if target.query else ''),
                                     is_json=False, page_redirects=page_redirects - 1)
            if response.status_code in {401, 403, 429}:
                raise RuntimeError(f'Байкал Сервис: официальный сайт отклонил запрос (HTTP {response.status_code}). Сохранённые цены остаются доступны; можно применить прайс из файла.')
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError(f'Байкал Сервис: неожиданный HTTP {response.status_code}')
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError('Байкал Сервис: слишком большой ответ калькулятора')
        if not is_json:
            return bytes(body)
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError('Байкал Сервис: вместо расчёта получена страница или повреждённый ответ') from exc

    def route(self, origin, destination):
        result = []
        for name, side in ((origin, 'departure'), (destination, 'destination')):
            city = select_city(self._request('GET', '/json/fias-cities', params={'text': normalize_city(name)}), name)
            data = self._request('POST', '/json/fias-station-data', data={'guid': city['guid']})
            terminal = select_terminal(data, city, side)
            result.append((city, terminal))
        return result

    def calculate(self, fields):
        # The calculation endpoint accepts the complete form directly. No quote
        # save, order submission or personal/contact details are needed.
        return self._request('POST', '/json/calculator', data=fields)


def parse_reply(data, fields):
    """Extract only the interterminal auto service; retain fuel already in cost."""
    if not isinstance(data, dict):
        raise ValueError('Байкал Сервис: изменился формат ответа калькулятора')
    errors = data.get('error') or data.get('errors')
    if errors:
        errors = errors if isinstance(errors, list) else [errors]
        detail = '; '.join(str(x.get('description', '') if isinstance(x, dict) else x) for x in errors)
        detail = BeautifulSoup(detail, 'lxml').get_text(' ', strip=True)[:400]
        if all(isinstance(x, dict) and str(x.get('section', '')).startswith('[cargo]') for x in errors) and 'превышают допустимые значения' in detail:
            raise UnpublishedTariff('Байкал Сервис: ' + detail.strip() + '. Цена для этого веса не получена.')
        raise ValueError('Байкал Сервис: калькулятор отклонил параметры. ' + detail)
    if data.get('id') != fields['id']:
        raise ValueError('Байкал Сервис: ответ относится к другой попытке расчёта')
    auto = (data.get('transport') or {}).get('auto')
    if not isinstance(auto, dict) or auto.get('error') or auto.get('errors'):
        raise UnpublishedTariff('Байкал Сервис: автомобильная перевозка не рассчитана')
    for side in ('departure', 'destination'):
        if (auto.get(side) or {}).get('guid') != fields[side + '[cityguid]']:
            raise ValueError('Байкал Сервис: ответ относится к другому направлению')
    cargo = auto.get('cargo') or {}
    description = str(cargo.get('description') or '').lower().replace('ё', 'е')
    weight = re.search(r'\bвес\s+(\d+(?:[.,]\d+)?)\s*кг\b', description)
    volume = re.search(r'\bобъем\s+(\d+(?:[.,]\d+)?)\s*м', description)
    units = re.search(r'\bмест\s+(\d+)\b', description)
    for match, key in ((weight, 'weight'), (volume, 'volume'), (units, 'units')):
        if not match or not math.isclose(float(match[1].replace(',', '.')), float(fields[f'cargo[summarycargo][{key}]']), abs_tol=.00001):
            raise ValueError('Байкал Сервис: ответ не подтвердил вес, объём или число мест')
    if cargo.get('oversized') not in (0, '0', False):
        raise ValueError('Байкал Сервис: получен тариф негабаритного груза')
    services = cargo.get('services')
    if not isinstance(services, list):
        raise ValueError('Байкал Сервис: отсутствует детализация межтерминальной перевозки')
    rows = [s for s in services if isinstance(s, dict) and str(s.get('id')) == '1'
            and str(s.get('name') or '').casefold().startswith('межтерминальная перевозка')]
    if len(rows) != 1:
        raise ValueError('Байкал Сервис: межтерминальная стоимость не выделена однозначно')
    service = rows[0]
    if service.get('individual') in (1, '1', True):
        raise UnpublishedTariff('Байкал Сервис: этот вес рассчитывается индивидуально')
    price = service.get('cost')
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError('Байкал Сервис: некорректная стоимость перевозки')
    weight = float(fields['cargo[summarycargo][weight]'])
    volume = float(fields['cargo[summarycargo][volume]'])
    return {'kind': 'exact', 'price': round(price, 2), 'volume_m3': volume,
            'tariff_kind': 'calculator_shipment',
            'calculation_basis': f'Межтерминальная автоперевозка: {weight:g} кг, {volume:g} м³, '
                f'{fields["cargo[summarycargo][units]"]} мест; плотность 200 кг/м³. '
                'Топливный сбор уже включён. Без страхования, информирования, въезда и доставки до адреса. '
                'Калькулятор публикует сумму; опубликованная ставка за кг отсутствует.'}


def collect(origin, destination, profile_ids=None, *, should_stop=None, timeout=45):
    from .v42_engine import COMMON_PROFILES, PROFILE_BY_ID, RUNTIME_DIR
    origin, destination = normalize_city(origin), normalize_city(destination)
    if origin == destination:
        raise ValueError('Байкал Сервис: города отправления и назначения совпадают')
    selected = list(dict.fromkeys(profile_ids or [p['id'] for p in COMMON_PROFILES if p['id'] != 'min']))
    if any(pid not in PROFILE_BY_ID for pid in selected):
        raise ValueError('Байкал Сервис: неизвестный диапазон')
    # The engine derives the minimum from the confirmed first weight, never 100 kg.
    selected = list(dict.fromkeys('w001' if pid == 'min' else pid for pid in selected))
    should_stop = should_stop or (lambda: False)
    values, missing, responses = {}, {}, []
    deadline = time.monotonic() + 240
    with _LIMIT, PublicClient(timeout=timeout) as client:
        start, end = client.route(origin, destination)
        origin_city, origin_terminal = start
        destination_city, destination_terminal = end
        for pid in selected:
            if should_stop() or time.monotonic() >= deadline:
                for remaining in selected:
                    if remaining not in values and remaining not in missing:
                        missing[remaining] = 'Обновление остановлено; ранее полученные цены сохранены.'
                break
            client.timeout = max(1, min(timeout, deadline - time.monotonic()))
            fields = make_request(origin_city, destination_city, origin_terminal, destination_terminal,
                                  PROFILE_BY_ID[pid]['weight_kg'])
            try:
                data = client.calculate(fields)
                # Keep failed validation responses too, for an inspectable cause.
                responses.append({'profile_id': pid, 'request': {k: v for k, v in fields.items() if k != 'guid'},
                                  'response': data})
                row = parse_reply(data, fields)
                row['captured_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                values[pid] = row
                # No cookies, CSRF tokens, quote GUIDs or API keys in evidence.
            except Exception as exc:
                missing[pid] = str(exc)
                if not isinstance(exc, UnpublishedTariff):
                    # A network/form error applies to the whole session: do not
                    # send the same failing request another 27 times.
                    for remaining in selected:
                        if remaining not in values and remaining not in missing:
                            missing[remaining] = str(exc)
                    break
    if not values:
        raise RuntimeError(next(iter(missing.values()), 'Байкал Сервис: не получено ни одной цены'))
    captured = datetime.now().astimezone().isoformat(timespec='seconds')
    evidence = json.dumps({'origin': origin, 'destination': destination, 'captured_at': captured,
                           'source_url': PAGE, 'endpoint': ENDPOINT, 'responses': responses}, ensure_ascii=False).encode()
    digest = hashlib.sha256(evidence).hexdigest()
    directory = RUNTIME_DIR / 'downloads'
    directory.mkdir(parents=True, exist_ok=True)
    name = 'baikal-calculator-' + digest[:24] + '.json'
    (directory / name).write_bytes(evidence)
    meta = {'origin': origin, 'destination': destination, 'source_url': PAGE,
            'source_type': 'Официальный публичный калькулятор Байкал Сервис',
            'captured_at': captured, 'transport': 'HTTPS/JSON', 'source_file': name, 'sha256': digest,
            'origin_terminal': origin_terminal.get('name'), 'destination_terminal': destination_terminal.get('name'),
            'partial_errors': list(dict.fromkeys(missing.values()))[:5], 'missing_profile_errors': missing}
    return {pid: {**meta, **row} for pid, row in values.items()}, meta
