import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import baikal as b, v42_collectors as col, v42_engine as e
from app.cities import UnpublishedTariff
from app.tariff_model import tariff_value

FIX = Path(__file__).parent / 'fixtures'


def fixture(name):
    return json.loads((FIX / ('baikal-' + name + '.json')).read_text())


def request(weight=100):
    a = b.select_city(fixture('cities-0'), 'Москва')
    z = b.select_city(fixture('cities-1'), 'Санкт-Петербург')
    fields = b.make_request(a, z, b.select_terminal(fixture('station-departure'), a, 'departure'),
                            b.select_terminal(fixture('station-destination'), z, 'destination'), weight)
    fields['id'] = 'fixture-request'
    return fields


class BaikalParser(unittest.TestCase):
    def test_real_response_shipping_only_fuel_not_added_twice(self):
        row = b.parse_reply(fixture('calculator-100'), request())
        self.assertEqual(row['price'], 3029.26)
        self.assertNotEqual(row['price'], 3275.21)  # total includes other services
        self.assertEqual(row['volume_m3'], .5)
        self.assertNotIn('rate_per_kg', row)
        self.assertIn('Топливный сбор уже включён', row['calculation_basis'])

    def test_wrong_route_weight_volume_attempt_and_duplicate_service_rejected(self):
        for mutation in ('route', 'weight', 'volume', 'units', 'id', 'duplicate', 'negative', 'nan', 'bool', 'oversized'):
            with self.subTest(mutation=mutation):
                data = fixture('calculator-100'); auto = data['transport']['auto']
                if mutation == 'route': auto['departure']['guid'] = auto['destination']['guid']
                elif mutation in {'weight', 'volume', 'units'}:
                    a, z = {'weight': ('100 кг', '200 кг'), 'volume': ('0.5 м³', '1 м³'), 'units': ('Мест 1', 'Мест 2')}[mutation]
                    auto['cargo']['description'] = auto['cargo']['description'].replace(a, z)
                elif mutation == 'id': data['id'] = 'stale'
                elif mutation == 'duplicate': auto['cargo']['services'].append(copy.deepcopy(auto['cargo']['services'][0]))
                elif mutation == 'oversized': auto['cargo']['oversized'] = 1
                else: auto['cargo']['services'][0]['cost'] = {'negative': -10, 'nan': float('nan'), 'bool': True}[mutation]
                with self.assertRaises(ValueError): b.parse_reply(data, request())

    def test_individual_and_error_never_become_prices(self):
        data = fixture('calculator-100'); data['transport']['auto']['cargo']['services'][0]['individual'] = 1
        with self.assertRaises(UnpublishedTariff): b.parse_reply(data, request())
        with self.assertRaises(ValueError): b.parse_reply({'error': [{'description': 'failed'}]}, request())

    def test_city_does_not_select_village_or_ambiguous_names(self):
        cities = fixture('cities-0')
        self.assertEqual(b.select_city(cities[::-1], 'МСК')['type'], 3)
        duplicate = {**cities[0], 'guid': 'another-city'}
        with self.assertRaises(UnpublishedTariff): b.select_city(cities + [duplicate], 'Москва')
        with self.assertRaises(UnpublishedTariff): b.select_city(cities, 'Казань')
        with self.assertRaises(UnpublishedTariff): b.select_city([cities[-1]], 'Москва')

    def test_terminals_exact_route_and_direction(self):
        city = b.select_city(fixture('cities-0'), 'Москва')
        data = fixture('station-departure')
        chosen = b.select_terminal(data, city, 'departure')
        self.assertEqual(chosen['base'], 1)
        data['onRoute'] = 2
        with self.assertRaises(UnpublishedTariff): b.select_terminal(data, city, 'departure')
        data['city']['guid'] = 'wrong'
        with self.assertRaises(ValueError): b.select_terminal(data, city, 'destination')

    def test_heavy_weight_is_multiple_standard_pieces(self):
        fields = request(20000)
        self.assertEqual(fields['cargo[summarycargo][units]'], '20')
        self.assertEqual(fields['cargo[summarycargo][volume]'], '100.0')
        self.assertFalse(any('pickup' in k or 'delivery' in k or 'services' in k for k in fields))


class FakeResponse:
    def __init__(self, content=b'', status=200, headers=None):
        self.content, self.status_code, self.headers = content, status, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def close(self): pass
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError('HTTP error')
    def iter_content(self, size): yield self.content


class FakeSession:
    calls = []
    csrf = 'fixture-csrf-never-save'
    guid = '11111111-2222-3333-4444-555555555555'
    fail_weight = None
    def __init__(self): self.headers = {}; self.closed = False
    def close(self): self.closed = True
    def request(self, method, url, **kw):
        type(self).calls.append((method, url, copy.deepcopy(kw), dict(self.headers)))
        assert kw['verify'] is True
        assert kw['allow_redirects'] is False
        if url.endswith('/calculator/'):
            return FakeResponse(status=302, headers={'Location': 'http://request.baikalsr.ru/calculator/?guid=' + self.guid})
        if '/calculator/?guid=' in url:
            raw = '<meta name="csrf-token" content="' + self.csrf + '"><script>var tabRequests = {"guid":"' + self.guid + '"};</script>'
            return FakeResponse(raw.encode())
        if url.endswith('/fias-cities'):
            data = fixture('cities-0' if kw['params']['text'] == 'Москва' else 'cities-1')
        elif url.endswith('/fias-station-data'):
            which = 'station-departure' if kw['data']['guid'] == fixture('cities-0')[0]['guid'] else 'station-destination'
            data = fixture(which)
        elif url.endswith('/calculator-initialization/'):
            data = {'guid': kw['data']['guid'], 'active': kw['data']['active']}
        else:
            fields = kw['data']; weight = float(fields['cargo[summarycargo][weight]'])
            if weight == self.fail_weight: raise TimeoutError('fixture network timeout')
            data = fixture('calculator-100'); data['id'] = fields['id']
            cargo = data['transport']['auto']['cargo']
            cargo['description'] = f'Мест {fields["cargo[summarycargo][units]"]}, вес {weight:g} кг, объем {fields["cargo[summarycargo][volume]"]} м³'
            cargo['services'][0]['cost'] = 1000 + weight  # controlled test response, not a production tariff
        return FakeResponse(json.dumps(data).encode())


class BaikalCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(e, 'RUNTIME_DIR', Path(self.tmp.name)); self.patch.start()
        self.session = patch.object(b.requests, 'Session', FakeSession); self.session.start()
        FakeSession.calls = []; FakeSession.fail_weight = None
    def tearDown(self):
        self.session.stop(); self.patch.stop(); self.tmp.cleanup()

    def test_grid_uses_28_distinct_requests_one_session_and_sanitized_evidence(self):
        values, meta = b.collect('Москва', 'Санкт-Петербург')
        self.assertEqual(len(values), 28)
        calculations = [c for c in FakeSession.calls if c[1] == b.ENDPOINT]
        self.assertEqual(len(calculations), 28)
        self.assertEqual(len({c[2]['data']['id'] for c in calculations}), 28)
        self.assertEqual(sum(c[1] == b.PAGE for c in FakeSession.calls), 1)
        self.assertTrue(all(c[1].startswith('https://') for c in FakeSession.calls))
        self.assertEqual(calculations[0][3]['Origin'], b.BASE)
        self.assertEqual(values['w20000']['price'], 21000)
        raw = (Path(self.tmp.name) / 'downloads' / meta['source_file']).read_text()
        self.assertNotIn(FakeSession.csrf, raw); self.assertNotIn(FakeSession.guid, raw)
        self.assertNotIn('Cookie', raw); self.assertFalse(meta['partial_errors'])

    def test_failure_keeps_completed_points_and_does_not_hammer_28_times(self):
        FakeSession.fail_weight = 5
        values, meta = b.collect('Москва', 'Санкт-Петербург')
        self.assertEqual(set(values), {'w001', 'w003'})
        self.assertEqual(len(meta['missing_profile_errors']), 26)
        self.assertEqual(sum(c[1] == b.ENDPOINT for c in FakeSession.calls), 3)

    def test_cancellation_retains_finished_points(self):
        stop = lambda: sum(c[1] == b.ENDPOINT for c in FakeSession.calls) >= 2
        values, meta = b.collect('Москва', 'Санкт-Петербург', should_stop=stop)
        self.assertEqual(len(values), 2); self.assertTrue(meta['partial_errors'])

    def test_one_route_and_bulk_share_adapter_and_saved_prices_survive_failure(self):
        with patch.object(e, 'ROUTE_CONFIG', {}), patch.object(col, '_log'):
            result = col.collect_selected(['Байкал Сервис'], 'Москва', 'Санкт-Петербург')
            self.assertEqual(result[0]['rows'], 28)
            one = e.quote('Байкал Сервис', 'Москва', 'Санкт-Петербург', 'w100')
            self.assertEqual(one['price'], 1100); self.assertTrue(one['online'])
            minimum = e.quote('Байкал Сервис', 'Москва', 'Санкт-Петербург', 'min')
            self.assertEqual(minimum['price'], 1001)
            # A quotient must never impersonate a published weight tariff.
            self.assertIsNone(tariff_value(one, e.PROFILE_BY_ID['w100']))
            FakeSession.fail_weight = 1
            result = col.collect_selected(['Байкал Сервис'], 'Москва', 'Санкт-Петербург', full_grid=True)
            self.assertFalse(result[0]['ok'])
            saved = e.quote('Байкал Сервис', 'Москва', 'Санкт-Петербург', 'w100')
            self.assertEqual(saved['price'], 1100); self.assertFalse(saved['online'])
            self.assertTrue(saved['retained_previous'])

    def test_blocked_site_and_foreign_redirect_are_rejected(self):
        for response in [FakeResponse(status=403), FakeResponse(status=429),
                         FakeResponse(status=302, headers={'Location': 'https://example.invalid/login'})]:
            with self.subTest(status=response.status_code), patch.object(FakeSession, 'request', return_value=response):
                with self.assertRaises((ValueError, RuntimeError)):
                    with b.PublicClient(): pass


if __name__ == '__main__': unittest.main()
