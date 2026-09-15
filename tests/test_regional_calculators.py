import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import proline,v42_collectors as c,legacy,carrier_catalogs as cat
from app.cities import UnpublishedTariff
FIX=Path(__file__).parent/'fixtures'

class RegionalCalculators(unittest.TestCase):
    def test_proline_real_response_and_inconsistent_totals(self):
        data=json.loads((FIX/'proline_spb_krd_100.json').read_bytes())
        self.assertEqual(proline.response_price(data),2410)
        data['price_real']=655
        with self.assertRaises(ValueError):proline.response_price(data)
    def test_generic_default_price_is_not_accepted_for_unpublished_pair(self):
        with patch('curl_cffi.requests.Session') as session:
            with self.assertRaises(UnpublishedTariff):proline.collect('Краснодар','Москва')
            session.assert_not_called()
    def test_proline_uses_actual_city_ids_weight_and_csrf_without_saving_token(self):
        calls=[]
        class Session:
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def request(self,method,url,**kwargs):
                calls.append((method,url,kwargs))
                body=(FIX/('proline_region_form.html' if method=='GET' else 'proline_spb_krd_100.json')).read_bytes()
                kwargs['content_callback'](body)
                return SimpleNamespace(url=url,status_code=200,headers={})
        with tempfile.TemporaryDirectory() as tmp,patch.object(legacy,'DOWNLOAD_DIR',Path(tmp)),patch('curl_cffi.requests.Session',Session):
            values,meta=proline.collect('Санкт-Петербург','Краснодар','w100')
            self.assertEqual(values['w100']['price'],2410)
            fields=calls[1][2]['data'];self.assertEqual(fields['data[from]'],'2');self.assertEqual(fields['data[to]'],'8')
            self.assertEqual(fields['data[w]'],'100');self.assertEqual(fields['data[v]'],'0.5');self.assertEqual(fields['_csrf'],'test-token')
            self.assertTrue(calls[1][2]['verify'])
            evidence=(Path(tmp)/meta['source_file']).read_text()
            self.assertNotIn('test-token',evidence);self.assertNotIn('_csrf',evidence);self.assertNotIn('dadata',evidence)
    def test_flat_response_for_all_heavy_weights_is_not_live(self):
        def point(company,o,d,p,**kwargs):return {p:{'kind':'exact','price':100}},{'source_url':'https://example.test'}
        with patch.object(c,'_collect_live_calculator',side_effect=point):
            with self.assertRaisesRegex(ValueError,'одна и та же сумма'):c._collect_keyless_api_grid('Werner','Казань','Екатеринбург','w100')
    def test_off_city_kit_terminal_requires_current_branch_confirmation(self):
        raw=(FIX/'kit_krasnodar_header.pdf').read_bytes()
        with patch.object(cat,'catalog_bytes',return_value=(FIX/'kit_krasnodar_branch.html').read_bytes()):
            self.assertEqual(cat.kit_pdf_origin('Краснодар',raw),'Новая Адыгея')
        with patch.object(cat,'catalog_bytes',return_value=b'<h1>Other branch</h1>'):
            with self.assertRaises(ValueError):cat.kit_pdf_origin('Краснодар',raw)
        with self.assertRaises(ValueError):cat.kit_pdf_origin('Москва',raw)

if __name__=='__main__':unittest.main()
