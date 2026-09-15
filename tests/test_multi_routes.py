"""Route isolation and real source excerpts beyond Moscow / Saint Petersburg."""
import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import cities,carrier_catalogs as cat,v42_engine as e,v42_main as main,v42_collectors as c,online_tariffs as a,legacy_backend as lb

FIX=Path(__file__).parent/'fixtures'
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ctx=patch.object(e,'RUNTIME_DIR',Path(self.tmp.name));self.ctx.start();self.client=TestClient(main.app)
    def tearDown(self):self.ctx.stop();self.tmp.cleanup()
    def test_city_selection_and_invalid_routes(self):
        opts=self.client.get('/api/options',params={'origin':'казань','destination':'Екатеринбург'}).json()
        self.assertEqual(opts['selected_origin'],'Казань');self.assertEqual(opts['selected_destination'],'Екатеринбург')
        self.assertNotIn('Казань',opts['destinations']);self.assertIn('Самара',opts['origins']);self.assertGreater(len(opts['origins']),200)
        for origin,destination in [('Казань','Казань'),('../../Москва','Казань'),('нет такого города','Москва')]:
            self.assertEqual(self.client.get('/api/compare',params={'origin':origin,'destination':destination}).status_code,400)
    def test_new_routes_do_not_borrow_old_prices_or_reverse_cache(self):
        routes=[('Казань','Екатеринбург'),('Екатеринбург','Казань'),('Казань','Самара'),('Москва','Казань')]
        for route in routes:
            q=self.client.get('/api/compare',params=dict(zip(['origin','destination'],route))).json()
            self.assertEqual(q['online_count'],0);self.assertTrue(all(x['price'] is None for x in q['items']))
        route=routes[0];aid=e.begin_live_attempt('Мейджик',*route)
        e.save_live_update('Мейджик',*route,{'w100':{'kind':'exact','price':1111}},{'origin':route[0],'destination':route[1]},aid)
        e.finish_live_attempt('Мейджик',*route,aid,rows=1)
        self.assertTrue(e.quote('Мейджик',*route,'w100')['online'])
        for other in routes[1:]:self.assertIsNone(e.quote('Мейджик',*other,'w100')['price'])
        self.assertEqual(len({e.live_path_for(*r) for r in routes}),4)
        with self.assertRaises(ValueError):e.save_live_update('Мейджик',*route,{'w100':{'kind':'exact','price':1111}},{'origin':route[1]},aid)
    def test_unpublished_route_is_distinct_from_network_failure(self):
        route=('Казань','Самара');aid=e.begin_live_attempt('АТЭК',*route)
        e.finish_live_attempt('АТЭК',*route,aid,rows=0,error=str(cities.UnpublishedTariff('нет строки')))
        row=e.quote('АТЭК',*route,'w100')
        self.assertEqual(row['refresh_status'],'unavailable');self.assertEqual(row['error_info']['code'],'route_unpublished');self.assertIsNone(row['price'])
    def test_new_route_export_has_requested_heading_and_no_old_values(self):
        from openpyxl import load_workbook
        r=self.client.get('/api/export/excel',params={'layout':'matrix','origin':'Казань','destination':'Екатеринбург'})
        self.assertEqual(r.status_code,200);w=load_workbook(io.BytesIO(r.content),data_only=True)
        self.assertEqual(w.active['B1'].value,'Казань → Екатеринбург');w.close()

class MultiCitySources(unittest.TestCase):
    def test_keyless_catalogs_are_carrier_specific_and_exact(self):
        def catalog(url,**kwargs):return (FIX/('werner_cities.json' if 'wernerus' in url else 'glav_cities.json')).read_bytes()
        with patch.object(cat,'catalog_bytes',side_effect=catalog):
            self.assertEqual(cat.keyless_city_ids('Werner','Казань','Екатеринбург'),('600','43'))
            self.assertEqual(cat.keyless_city_ids('Главтрасса','Казань','Екатеринбург'),('600','43'))
            with self.assertRaises(cities.UnpublishedTariff):cat.keyless_city_ids('Главтрасса','Омск','Москва')
        with self.assertRaises(cities.UnpublishedTariff):cat.exact_id([('1','Казань'),('2','Казань')],'Казань','test')
    def test_wrong_corridor_cannot_be_promoted_to_new_city(self):
        for company,parser,fixture in [('Пролайн',a.parse_proline,'PR002.html'),('АТЭК',a.parse_atec,'AT001.html')]:
            with self.subTest(company=company),self.assertRaises(cities.UnpublishedTariff):parser((FIX/fixture).read_bytes(),'Москва','Казань')
    def test_bsk_blank_weight_cells_do_not_discard_other_published_weights(self):
        vals=a.parse_bsk((FIX/'bsk_kazan.html').read_bytes(),'Казань','Екатеринбург')
        self.assertEqual(vals['min']['price'],1350);self.assertEqual(vals['w20000']['price'],386000)
        self.assertNotIn('w100',vals);self.assertNotIn('w10000',vals)
        self.assertEqual(a.parse_bsk((FIX/'bsk_kazan.html').read_bytes(),'Казань','Москва')['w100']['price'],1250)
    def test_kit_pdf_checks_origin_and_reads_new_destination(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(lb,'CACHE_DIR',Path(tempfile.gettempdir())):
            # Index is isolated in a writable temporary directory.
            with patch.object(lb,'CACHE_DIR',Path(tmp)):
                vals=c._kit_pdf_values(FIX/'kit_kazan_excerpt.pdf','Казань','Екатеринбург')
                self.assertEqual(vals['w100']['price'],1990);self.assertEqual(vals['w001']['price'],650)
                with self.assertRaises(ValueError):c._kit_pdf_values(FIX/'kit_kazan_excerpt.pdf','Москва','Екатеринбург')
    def test_pek_index_keeps_the_requested_origin(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(lb,'CACHE_DIR',Path(tmp)):
            forward=lb._pek_route_index(FIX/'pek_cities.xlsx','Казань')['routes']
            reverse=lb._pek_route_index(FIX/'pek_cities.xlsx','Екатеринбург')['routes']
            self.assertIn('Казань|Екатеринбург',forward);self.assertNotIn('Екатеринбург|Казань',forward)
            self.assertIn('Екатеринбург|Казань',reverse);self.assertNotEqual(forward,reverse)
    def test_regional_workbook_sections_use_current_outbound_values(self):
        for company,fixture,expected in [('Фортуна','fortuna_cities.xlsx',4260),('ЭкспедицияПлюс','expedition_cities.xlsx',3778.12)]:
            values=a.parse_workbook(company,(FIX/fixture).read_bytes(),'Казань','Екатеринбург')
            self.assertEqual(values['w100']['price'],expected)
            with self.assertRaises(cities.UnpublishedTariff):a.parse_workbook(company,(FIX/fixture).read_bytes(),'Якутск','Казань')

    def test_current_city_ids_and_route_links(self):
        with patch.object(cat,'catalog_bytes',return_value=(FIX/'newline_cities.html').read_bytes()):
            self.assertEqual(cat.newline_origin('Краснодар'),('177','Краснодар'))
            with self.assertRaises(cities.UnpublishedTariff):cat.newline_origin('Казань')
        with patch.object(cat,'catalog_bytes',return_value=(FIX/'baikal_index_cities.html').read_bytes()):
            self.assertEqual(cat.baikal_route_url('Казань','Екатеринбург'),'https://www.baikalsr.ru/city/kazan__ekaterinburg/')
        self.assertEqual(c._vozovoz_route_url('Казань','Самара'),'https://vozovoz.ru/order/create/kazan_samara/')
    def test_inflected_city_headings_do_not_accept_other_origin(self):
        for o,d,heading in [('Казань','Екатеринбург','Перевозка из Казани в Екатеринбург'),('Нижний Новгород','Ростов-на-Дону','Нижнего Новгорода в Ростов-на-Дону')]:
            raw=('<meta charset="utf-8"><h1>'+heading+'</h1>').encode();url='https://example.test/route/'
            c._route_soup(raw,url,url,o,d)
            with self.assertRaises(RuntimeError):c._route_soup(raw,url,url,'Самара',d)

if __name__=='__main__':unittest.main()
