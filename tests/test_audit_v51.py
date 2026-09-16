import io,json,sqlite3,tempfile,time,unittest,zipfile
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import v42_engine as e,document_imports as imports,price_library,storage,v42_main
from app.source_conditions import tax_basis,document_conditions
import launch

ROUTE=('Казань','Екатеринбург')


class PricePolicy(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.patch=patch.object(e,'RUNTIME_DIR',Path(self.tmp.name));self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def online(self,values):
        aid=e.begin_live_attempt('Werner',*ROUTE)
        e.save_live_update('Werner',*ROUTE,values,{'source_url':'https://example.invalid/official'},aid)
        e.finish_live_attempt('Werner',*ROUTE,aid,rows=len(values))
    def test_current_online_wins_and_document_fills_only_missing_weights(self):
        e._robust_json_write(imports.route_path(*ROUTE),{'profiles':{
            'w100':{'Werner':{'kind':'exact','price':999,'rate_per_kg':9.99}},
            'w200':{'Werner':{'kind':'exact','price':2222,'rate_per_kg':11.11}}}})
        self.online({'w100':{'kind':'exact','price':1500,'rate_per_kg':15}})
        self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],1500)
        self.assertTrue(e.quote('Werner',*ROUTE,'w100')['online'])
        self.assertTrue(e.quote('Werner',*ROUTE,'w200')['uploaded'])
        # A failed new attempt makes a file eligible again, never a fake LIVE.
        aid=e.begin_live_attempt('Werner',*ROUTE);e.finish_live_attempt('Werner',*ROUTE,aid,rows=0,error='offline')
        self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],999)
        self.assertFalse(e.quote('Werner',*ROUTE,'w100')['online'])
    def test_heavy_quote_and_teaser_never_prove_minimum(self):
        self.online({'w100':{'kind':'exact','price':3090}})
        self.assertIsNone(e.quote('Werner',*ROUTE,'min')['comparison_value'])
        self.online({'w001':{'kind':'lower_bound','price':250}})
        self.assertIsNone(e.quote('Werner',*ROUTE,'min')['comparison_value'])
        self.online({'min':{'kind':'exact','price':600}})
        self.assertEqual(e.quote('Werner',*ROUTE,'min')['comparison_value'],600)
    def test_archive_first_weight_cannot_certify_a_new_heavy_minimum(self):
        self.online({'w001':{'kind':'exact','price':900}})
        self.online({'w100':{'kind':'exact','price':100}})
        q=e.quote('Werner',*ROUTE,'min')
        self.assertFalse(q['online']);self.assertEqual(q['price'],900)
    def test_tax_wording_is_extracted_without_any_price_conversion(self):
        self.assertEqual(tax_basis('Тарифы без учета НДС'),'Без НДС (по источнику)')
        self.assertEqual(tax_basis('Цены с учетом НДС 22%'),'С НДС (по источнику)')
        self.assertIsNone(tax_basis('Перевозка с НДС; упаковка без НДС'))
        self.assertIsNone(tax_basis('Ставка 10 руб/кг'))
        self.online({'w100':{'kind':'exact','price':1000,'rate_per_kg':10,'tax_basis':'Без НДС (по источнику)'}})
        q=e.quote('Werner',*ROUTE,'w100')
        self.assertEqual(q['price'],1000);self.assertEqual(q['published_rate_per_kg'],10)
        self.assertEqual(q['tax_basis'],'Без НДС (по источнику)')

    def test_future_online_document_is_rejected_before_saving_prices(self):
        aid=e.begin_live_attempt('Werner',*ROUTE)
        with self.assertRaisesRegex(ValueError,'будущие тарифы'):
            e.save_live_update('Werner',*ROUTE,{'w100':{'kind':'exact','price':999}}, {'document_date':'2099-01-01'},aid)
        self.assertIsNone(e.quote('Werner',*ROUTE,'w100')['price'])

    def test_customer_rate_fallback_does_not_change_online_shipment_total(self):
        from openpyxl import load_workbook
        from app.excel_export import export_bytes
        e._robust_json_write(imports.route_path(*ROUTE),{'profiles':{
            'w100':{'Werner':{'kind':'exact','price':1000,'rate_per_kg':10,'tax_basis':'Без НДС (по источнику)'}}}})
        self.online({'w100':{'kind':'exact','price':1200}})
        wb=load_workbook(io.BytesIO(export_bytes(*ROUTE,['Werner'],all_loaded=False,live_only=True)),data_only=True)
        self.assertEqual(wb['WernerNEW']['N2'].value,10)
        self.assertTrue(any(r[3]=='до 100 кг' and r[5]=='Файл пользователя' and r[16]=='Без НДС (по источнику)' for r in wb['Источники'].values));wb.close()
        self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],1200)


class StorageBackup(unittest.TestCase):
    def test_consistent_backup_restores_prices_disabled_history_and_originals(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(e,'RUNTIME_DIR',Path(tmp)):
            root=imports.root();(root/'files').mkdir(parents=True)
            token='a'*32;filename=token+'.csv';(root/'files'/filename).write_text('original')
            meta={'source_file':filename,'company':'Werner','import_revision':1}
            with price_library.db() as db:
                db.execute('INSERT INTO files VALUES (?,?,?,1)',(token,'Werner',json.dumps(meta)))
                db.execute('INSERT INTO prices VALUES (?,?,?,?,?)',(token,*ROUTE,'w100',json.dumps({'kind':'exact','price':1400})))
            price_library.remove(token)
            self.assertFalse(price_library.pack(*ROUTE)['profiles'])
            self.assertTrue((root/'files'/filename).exists())
            (root/'pending').mkdir();(root/'pending'/'old.json').write_text('{}')
            import os
            os.utime(root/'pending'/'old.json',(time.time()-1900,)*2)
            info=storage.summary();self.assertEqual(info['files'],1)
            self.assertFalse((root/'pending'/'old.json').exists())
            response=TestClient(v42_main.app).get('/api/storage/backup')
            self.assertEqual(response.status_code,200)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(archive.read('imports/files/'+filename),b'original')
                self.assertFalse(any('pending' in n or 'settings' in n for n in archive.namelist()))
                restore=Path(tmp)/'restored';archive.extractall(restore)
            db=sqlite3.connect(restore/'imports/documents.sqlite3')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT active FROM files').fetchone()[0],0)
            self.assertEqual(json.loads(db.execute('SELECT payload FROM prices').fetchone()[0])['price'],1400);db.close()
            self.assertFalse(list((Path(tmp)/'exports').glob('*.zip')))


class LaunchIsolation(unittest.TestCase):
    def test_old_or_other_installation_is_not_opened_as_this_build(self):
        from unittest.mock import MagicMock
        busy=MagicMock();busy.__enter__.return_value=busy;busy.bind.side_effect=OSError('busy')
        free=MagicMock();free.__enter__.return_value=free
        with patch.object(launch.socket,'socket',side_effect=[busy]+[free]*20),patch.object(launch,'health',return_value={'version':'50.1'}):
            self.assertEqual(launch.select_port(),(8424,False))
        with patch.object(launch.socket,'socket',return_value=busy),patch.object(launch,'health',return_value={'version':launch.VERSION,'installation_id':launch.INSTALLATION_ID}):
            self.assertEqual(launch.select_port(),(8423,True))
        with patch.object(launch.socket,'socket',side_effect=[free,busy]),patch.object(launch,'health',return_value={'version':launch.VERSION,'installation_id':launch.INSTALLATION_ID}):
            self.assertEqual(launch.select_port(),(8424,True))


class BulkDocumentCache(unittest.TestCase):
    def test_reuse_preserves_timestamp_and_route_validation_is_not_skipped(self):
        from app import document_cache,online_tariffs,tariff_documents
        raw=(Path(__file__).parent/'fixtures/vozovoz_original_example.xls').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'price.xls';path.write_bytes(raw)
            result={'status':'downloaded','path':str(path),'file':'price.xls','connector':'test'}
            with patch('app.v42_collectors._bounded_source_download',return_value=result) as download:
                with document_cache.session():
                    first,meta=online_tariffs.fetch('Возовоз','Москва','Санкт-Петербург','https://example.invalid/price.xls','XLS')
                    again,other=online_tariffs.fetch('Возовоз','Москва','Казань','https://example.invalid/price.xls','XLS')
                    self.assertEqual(download.call_count,1);self.assertEqual(meta['captured_at'],other['captured_at'])
                    self.assertEqual(other['destination'],'Казань');self.assertEqual(first,again)
                    with self.assertRaises(ValueError):tariff_documents.parse_document(again,'price.xls','Возовоз','Москва','Казань')
                online_tariffs.fetch('Возовоз','Москва','Санкт-Петербург','https://example.invalid/price.xls','XLS')
                self.assertEqual(download.call_count,2)
    def test_cache_expires_and_never_leaks_between_runs(self):
        from app import document_cache as cache
        with cache.session(),patch.object(cache.time,'monotonic',return_value=0):cache.put('key',b'data',{'captured_at':'original'})
        with cache.session():self.assertIsNone(cache.get('key'))
        with cache.session():
            with patch.object(cache.time,'monotonic',return_value=0):cache.put('key',b'data',{})
            with patch.object(cache.time,'monotonic',return_value=301):self.assertIsNone(cache.get('key'))


if __name__=='__main__':unittest.main()
