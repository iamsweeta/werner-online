"""Regression cases for the merged 43.x carriers. No network calls in this suite."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import online_tariffs as a, v42_collectors as c, v42_engine as e

FIXTURES=Path(__file__).parent/'fixtures'


def rail_fixture(origin='Москва', destination='С-Петербург', inbound_first=True):
    headings=['В город','Скоростной режим','Мин. цена','Срок*','Температ. режим','до 99','100-299','300-499','500-999','1000-1999','2000-2999','3000-4999','5000-6999','7000-9999','10000-14999','15000-19999','20000-24999','25000 и более']
    rates=[18.8,18.3,17.9,17.5,17.1,16.7,16.5,16.3,16.1,15.9,15.7,15.5,15.5]
    def table(amounts):
        cells=[destination,'авто',990,1,'Нет']+amounts
        return '<table><tr>'+''.join('<th>'+x+'</th>' for x in headings)+'</tr><tr>'+''.join('<td>'+str(x)+'</td>' for x in cells)+'</tr></table>'
    old='<h2>Тарифы в город '+origin+' по весу руб/кг</h2>'+table([99]*13)
    current='<h2>Тарифы из города '+origin+' по весу руб/кг</h2>'+table(rates)
    volume='<h2>Тарифы из города '+origin+' по объему руб/м3</h2>'+table([999]*13)
    return ('<meta charset="utf-8"><main>'+ (old if inbound_first else '')+current+volume+'</main>').encode()


class Carriers43Tests(unittest.TestCase):
    def test_rail_actual_export_both_directions_and_volume_exclusion(self):
        for name,origin,dest,expected in [('rail_msk.xlsx','Москва','Санкт-Петербург',1830),('rail_spb.xlsx','Санкт-Петербург','Москва',1760)]:
            vals=a.parse_railcontinent_workbook((FIXTURES/name).read_bytes(),origin,dest)
            self.assertEqual(len(vals),29);self.assertEqual(vals['w100']['price'],expected)
            with self.assertRaises(ValueError):a.parse_railcontinent_workbook((FIXTURES/name).read_bytes(),dest,origin)

    def test_rail_strict_outbound_weight_table_and_printed_boundaries(self):
        raw=rail_fixture()
        vals=a.parse_railcontinent(raw,'Москва','Санкт-Петербург')
        self.assertEqual(len(vals),29);self.assertEqual(vals['min']['price'],990)
        self.assertEqual(vals['w100']['price'],1830);self.assertEqual(vals['w20000']['price'],310000)
        with self.assertRaises(ValueError):a.parse_railcontinent(raw,'Санкт-Петербург','Москва')
        with self.assertRaises(ValueError):a.parse_railcontinent(raw.replace('из города'.encode(),'в город'.encode()),'Москва','Санкт-Петербург')

    def test_rail_supports_site_city_alias_without_guessing_transport(self):
        raw=rail_fixture('С-Петербург','Москва')
        self.assertEqual(a.parse_railcontinent(raw,'Санкт-Петербург','Москва')['w100']['price'],1830)
        with self.assertRaises(ValueError):a.parse_railcontinent(raw.replace('авто'.encode(),'жд'.encode()),'Санкт-Петербург','Москва')

    def test_cts_resolves_current_form_origin_id(self):
        raw=(FIXTURES/'cts_origin_form.html').read_bytes()
        self.assertEqual(c._cts_origin_url(raw,'https://cts-group.ru/prices','Санкт-Петербург'),'https://cts-group.ru/prices?from=2')
        self.assertEqual(c._cts_origin_url(raw,'https://cts-group.ru/prices','Москва'),'https://cts-group.ru/prices?from=1')
        with self.assertRaises(RuntimeError):c._cts_origin_url(raw.replace(b'action="/prices"',b'action="https://other.test/prices"'),'https://cts-group.ru/prices','Москва')

    def test_cts_reads_actual_spb_destination_row(self):
        from bs4 import BeautifulSoup
        raw=(FIXTURES/'cts_spb_row.html').read_bytes()
        _,cells,_=c._find_route_table(BeautifulSoup(raw,'lxml'),'Санкт-Петербург','Москва')
        vals,_=c._cts_values_from_cells(cells)
        self.assertEqual(len(vals),29)
        # Price comes from the captured official SPB row, distinct from Moscow's default.
        self.assertEqual(vals['w100']['price'],890)
        with self.assertRaises(RuntimeError):c._find_route_table(BeautifulSoup(raw,'lxml'),'Москва','Санкт-Петербург')

    def test_api_grid_preserves_real_per_weight_source_and_volume(self):
        def point(company,origin,destination,pid,**kwargs):
            w=e.PROFILE_BY_ID[pid]['weight_kg']
            return {pid:{'kind':'exact','price':w*10}},{'source_url':f'https://carrier.test/?weight={w}','volume_m3':w/200,'origin':origin,'destination':destination,'calculation_basis':f'Actual API point {w}'}
        with patch.object(c,'_collect_live_calculator',side_effect=point):
            vals,meta=c._collect_keyless_api_grid('Werner','Санкт-Петербург','Москва','w100')
        self.assertEqual(len(vals),28);self.assertNotIn('min',vals)
        self.assertEqual(vals['w200']['price'],2000);self.assertEqual(vals['w200']['volume_m3'],1)
        with tempfile.TemporaryDirectory() as td,patch.dict(e.ROUTE_CONFIG[('Санкт-Петербург','Москва')],{'live':Path(td)/'live.json'}):
            aid=e.begin_live_attempt('Werner','Санкт-Петербург','Москва','w100')
            e.save_live_update('Werner','Санкт-Петербург','Москва',vals,meta,aid)
            e.finish_live_attempt('Werner','Санкт-Петербург','Москва',aid,rows=28)
            item=e.quote('Werner','Санкт-Петербург','Москва','w200')
        self.assertIn('weight=200',item['source_url']);self.assertEqual(item['volume_m3'],1)

    def test_failed_api_profile_is_missing_instead_of_interpolated(self):
        def point(company,origin,destination,pid,**kwargs):
            if pid=='w200':raise RuntimeError('Site unavailable at this weight')
            return {pid:{'kind':'exact','price':e.PROFILE_BY_ID[pid]['weight_kg']*10}},{'origin':origin,'destination':destination}
        with patch.object(c,'_collect_live_calculator',side_effect=point):
            vals,meta=c._collect_keyless_api_grid('Werner','Санкт-Петербург','Москва','w100')
        self.assertEqual(len(vals),27);self.assertNotIn('w200',vals);self.assertIn('w200',meta['partial_errors'][0])

if __name__=='__main__':unittest.main()
