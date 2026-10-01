"""Integration tests against a disposable PostgreSQL endpoint and mocked S3.

Run with TEST_DATABASE_URL pointing to a NEW LOCAL test database only.
The suite clears tariff_* schemas; it must never target a production database.
"""
import io,json,os,shutil,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

URL=os.environ.get('TEST_DATABASE_URL','')


@unittest.skipUnless(URL,'TEST_DATABASE_URL is required for cloud integration tests')
class CloudStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if urlsplit(URL).hostname not in {'127.0.0.1','localhost'}:
            raise RuntimeError('Integration tests only accept a local disposable PostgreSQL server')
        from moto.server import ThreadedMotoServer
        cls.s3=ThreadedMotoServer(ip_address='127.0.0.1',port=56366,verbose=False);cls.s3.start()
        cls.temp=tempfile.TemporaryDirectory()
        cls.directory=Path(cls.temp.name)
        cls.env=patch.dict(os.environ,{'TARIFF_STORAGE':'cloud','DATABASE_URL':URL,
            'TARIFF_DATA_DIR':str(cls.directory/'runtime'),'S3_ENDPOINT_URL':'http://127.0.0.1:56366',
            'S3_BUCKET':'tariff-tests','S3_ACCESS_KEY_ID':'testing','S3_SECRET_ACCESS_KEY':'testing',
            'S3_REGION':'us-east-1','APP_AUTH_MODE':'password','APP_PASSWORD':'test-password-63','APP_USERNAME':'manager'})
        cls.env.start()
        from app import cloud_db as db,data_store as store,v42_engine as e
        cls.db=db;cls.store=store;cls.e=e
        cls.path=patch.object(e,'RUNTIME_DIR',cls.directory/'runtime');cls.path.start()
        cls.config=patch.object(e,'ROUTE_CONFIG',{});cls.config.start()
        e.RUNTIME_DIR.mkdir(parents=True,exist_ok=True)
        s3,bucket=store.client();s3.create_bucket(Bucket=bucket)
        db.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.db.close();cls.s3.stop();cls.config.stop();cls.path.stop();cls.env.stop();cls.temp.cleanup()

    def setUp(self):
        from app import price_library as lib
        lib.JOBS.clear();lib._MIGRATED.clear()
        with self.db.transaction() as c:
            for kind,tables in self.db.TABLES.items():
                for table in tables:c.execute(f'TRUNCATE tariff_{kind}.{table} RESTART IDENTITY')
            c.execute('TRUNCATE tariff_state.json_data,tariff_state.objects')
        self.route=('Казань','Уфа')
        self.csv='Компания;Откуда;Куда;Вес от, кг;Вес до, кг;Тариф;Единица;Минимум, руб\nWerner;Казань;Уфа;0;50;650;руб;\nWerner;Казань;Уфа;50;20000;13;руб/кг;650\n'.encode()

    def single(self):
        from app import document_imports as imp
        preview=imp.preview(self.csv,'monthly.csv','Werner',*self.route)
        return preview,imp.commit(preview['token'])

    def test_prices_original_and_excel_survive_empty_runtime_and_new_process(self):
        from app import manual_prices as manual,document_imports as imp,storage
        from app.bulk_refresh import BulkManager
        from openpyxl import load_workbook
        preview,saved=self.single()
        manual.put('ДЛ',*self.route,'w100',19,'rub_per_kg')
        attempt=self.e.begin_live_attempt('ПЭК',*self.route)
        self.e.save_live_update('ПЭК',*self.route,{'w100':{'kind':'exact','price':1700,'rate_per_kg':17}},{},attempt)
        self.e.finish_live_attempt('ПЭК',*self.route,attempt,rows=1)
        manager=BulkManager();job=manager.start('origins',['Казань'],['Уфа'],mode='saved')
        manager.worker.join(30)
        if manager.export_worker:manager.export_worker.join(30)
        current=manager.status(job['job_id']);self.assertEqual(current['export_status'],'ready',current)
        before=manager.download(job['job_id']).read_bytes()
        with self.db.transaction() as conn:
            conn.execute("UPDATE tariff_state.json_data SET updated_at=now()-interval '40 days'")
        shutil.rmtree(self.e.RUNTIME_DIR);self.e.RUNTIME_DIR.mkdir()
        self.assertEqual(imp.source_file(saved['meta']['source_file']).read_bytes(),self.csv)
        restarted=BulkManager();self.assertEqual(restarted.download(job['job_id']).read_bytes(),before)
        code="from app import v42_engine as e,document_imports as d; import json; print(json.dumps([e.quote(c,'Казань','Уфа','w100')['price'] for c in ['Werner','ДЛ','ПЭК']])); print(len(d.source_file('"+saved['meta']['source_file']+"').read_bytes()))"
        result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'TARIFF_DATA_DIR':str(self.directory/'new-worker')},capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        lines=result.stdout.splitlines();self.assertEqual(json.loads(lines[0]),[1300,1900,1700]);self.assertEqual(int(lines[1]),len(self.csv))
        wb=load_workbook(io.BytesIO(before),data_only=True);self.assertEqual(wb['WernerNEW']['N2'].value,13);wb.close()
        self.assertEqual(storage.summary()['files'],1)

    def test_cloud_failure_cannot_apply_prices_and_commit_is_idempotent(self):
        from app import document_imports as imp,price_library as lib,manual_prices as manual
        preview=imp.preview(self.csv,'monthly.csv','Werner',*self.route)
        manual.put('Werner',*self.route,'w100',21,'rub_per_kg')
        s3,_=self.store.client()
        with patch.object(s3,'put_object',side_effect=RuntimeError('private test credential must not leak')):
            with self.assertRaises(self.db.StorageUnavailable) as failure:imp.commit(preview['token'])
        self.assertNotIn('credential',str(failure.exception))
        self.assertEqual(lib.list_files(),[]);self.assertEqual(self.e.quote('Werner',*self.route,'w100')['price'],2100)
        imp.commit(preview['token']);again=imp.commit(preview['token'])
        self.assertTrue(again['already_applied']);self.assertEqual(len(lib.list_files()),1)
        self.assertEqual(self.e.quote('Werner',*self.route,'w100')['price'],1300)

    def test_reuse_original_new_route_is_atomic_and_survives_cold_restart(self):
        from app import document_imports as imp,route_import_jobs as jobs,price_library as lib
        from tests.test_library_v53 import document
        preview=imp.preview(document(),'two-routes.csv','Werner',*self.route)
        saved=imp.commit(preview['token']);ident=saved['document_id']
        job=jobs.start_saved(ident,*self.route[::-1]);end=time.monotonic()+20
        while job['status'] in {'queued','parsing'} and time.monotonic()<end:
            time.sleep(.05);job=jobs.status(job['job_id'])
        self.assertEqual(job['status'],'ready',job);token=job['preview']['token']
        # A failed final transaction cannot leave an applied route or receipt.
        with patch('app.manual_prices.clear_covered',side_effect=RuntimeError('test interruption')):
            with self.assertRaises(RuntimeError):imp.commit(token)
        self.assertIsNone(self.e.quote('Werner',*self.route[::-1],'w100')['price'])
        self.assertEqual(lib.list_files()[0]['route_count'],1)
        result=imp.commit(token);self.assertTrue(result['reused_document'])
        self.assertTrue(imp.commit(token)['already_applied'])
        shutil.rmtree(self.e.RUNTIME_DIR);self.e.RUNTIME_DIR.mkdir()
        self.assertEqual(lib.list_files()[0]['route_count'],2);self.assertEqual(len(lib.list_files()),1)
        self.assertEqual(imp.source_file(saved['meta']['source_file']).read_bytes(),document())
        code="from app import v42_engine as e;import json;print(json.dumps([e.quote('Werner','Казань','Уфа','w100')['price'],e.quote('Werner','Уфа','Казань','w100')['price']]))"
        child=subprocess.run([sys.executable,'-c',code],env={**os.environ,'TARIFF_DATA_DIR':str(self.directory/'reused-cold')},capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr);self.assertEqual(json.loads(child.stdout),[1200,1300])

    def test_multi_preview_and_confirmation_survive_restart(self):
        from app import price_library as lib
        job=lib.start_preview(self.csv,'monthly.csv','Werner','Казань')
        end=time.monotonic()+30
        while job['status']=='parsing' and time.monotonic()<end:time.sleep(.1);job=lib.status(job['token'])
        self.assertEqual(job['status'],'ready',job)
        # Ensure the final status transaction has completed before simulating restart.
        lib._save_job(job['token'],True);lib.JOBS.clear()
        shutil.rmtree(self.e.RUNTIME_DIR);self.e.RUNTIME_DIR.mkdir()
        self.assertEqual(lib.status(job['token'])['status'],'ready')
        saved=lib.commit(job['token']);self.assertEqual(saved['route_count'],1)
        self.assertTrue(lib.commit(job['token'])['already_applied'])
        self.assertEqual(lib.pack(*self.route)['profiles']['w100']['Werner']['price'],1300)

    def test_backup_roundtrip_atomic_migration_and_nonempty_guard(self):
        from app import storage,manual_prices as manual,cloud_transfer,price_library as lib
        from restore_data import restore
        self.single();manual.put('ДЛ',*self.route,'w100',18,'rub_per_kg')
        backup=storage.backup();archive=self.directory/'keep.zip';shutil.copyfile(backup,archive)
        restore(archive,self.directory/'local-restored')
        with self.assertRaises(ValueError):cloud_transfer.migrate(archive)
        self.setUp()
        # A file failure after table rows have been imported rolls those rows
        # back as well. Retrying into the still-empty DB must then succeed.
        with patch.object(self.store,'store_file',side_effect=self.db.StorageUnavailable('Upload failed')):
            with self.assertRaises(self.db.StorageUnavailable):cloud_transfer.migrate(archive)
        cloud_transfer.assert_empty()
        result=cloud_transfer.migrate(archive)
        self.assertTrue(result['ok']);self.assertEqual(len(lib.list_files()),1)
        self.assertEqual(self.e.quote('ДЛ',*self.route,'w100')['price'],1800)
        self.assertEqual(self.e.quote('Werner',*self.route,'w100')['price'],1300)
        with self.assertRaises(ValueError):cloud_transfer.migrate(archive)

    def test_http_auth_storage_and_database_error_are_explicit(self):
        from fastapi.testclient import TestClient
        from app import v42_main as main
        client=TestClient(main.app)
        self.assertEqual(client.get('/api/price-documents').status_code,401)
        self.assertEqual(client.get('/health').status_code,200)
        client.auth=('manager','test-password-63')
        self.assertEqual(client.post('/api/storage/check').status_code,200)
        self.assertEqual(client.post('/api/storage/check',headers={'Origin':'https://other.example'}).status_code,403)
        with patch.object(self.store,'read_json',side_effect=self.db.StorageUnavailable('База временно недоступна')):
            response=client.get('/api/profile-matrix',params={'origin':'Казань','destination':'Уфа'})
        self.assertEqual(response.status_code,503);self.assertEqual(response.json()['code'],'storage_unavailable')

    def test_public_cloud_without_password_can_use_catalog_and_save_prices(self):
        from app import v42_main as main,access
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {'APP_AUTH_MODE':'public','APP_PASSWORD':''}):
            access.validate(cloud=True)
            client=TestClient(main.app)
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/health').json()['storage_mode'],'cloud')
            self.assertEqual(client.get('/api/options?include_status=false').status_code,200)
            response=client.post('/api/manual-price',json={'company':'Werner','origin':'Казань','destination':'Уфа','profile':'w100','value':17,'unit':'rub_per_kg'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.e.quote('Werner',*self.route,'w100')['price'],1700)
            self.assertEqual(client.post('/api/storage/check').status_code,200)

    def test_route_view_uses_bounded_queries_under_network_latency(self):
        import psycopg
        from fastapi.testclient import TestClient
        from app import v42_main as main,manual_prices
        client=TestClient(main.app);client.auth=('manager','test-password-63')
        manual_prices.put('Werner',*self.route,'w100',17,'rub_per_kg')
        self.e.route_packs(*self.route)
        execute=psycopg.Cursor.execute;queries=[]
        def delayed(cursor,query,*args,**kwargs):
            queries.append(str(query));time.sleep(.02)
            return execute(cursor,query,*args,**kwargs)
        with patch.object(psycopg.Cursor,'execute',delayed):
            started=time.monotonic()
            response=client.get('/api/route-view',params={'origin':self.route[0],'destination':self.route[1]})
            elapsed=time.monotonic()-started
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertEqual(len(data['comparison']['items']),17)
        self.assertEqual(len(data['matrix']['profiles']),29)
        self.assertEqual(data['comparison']['items'][0]['price'],1700)
        self.assertLessEqual(len(queries),8,queries)
        self.assertLess(elapsed,2.5)
        self.assertFalse(any('pg_advisory' in q for q in queries))
        print('ROUTE_VIEW_METRICS',json.dumps({'queries':len(queries),'seconds':round(elapsed,3),'simulated_query_latency_ms':20}))
        manual_prices.put('Werner',*self.route,'w100',19,'rub_per_kg')
        updated=client.get('/api/route-view',params={'origin':self.route[0],'destination':self.route[1]}).json()
        self.assertEqual(updated['comparison']['items'][0]['price'],1900,'no stale process cache after edits')

    def test_slow_original_upload_does_not_block_saved_prices_or_edits(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from app import manual_prices
        aid=self.e.begin_live_attempt('Werner',*self.route)
        self.e.save_live_update('Werner',*self.route,{'w100':{'kind':'exact','price':1000}},{},aid)
        original=self.e.RUNTIME_DIR/'downloads'/'slow-original.pdf'
        original.parent.mkdir(parents=True,exist_ok=True);original.write_bytes(b'controlled original')
        client,_=self.store.client();put=client.put_object
        entered=threading.Event();release=threading.Event()
        def delayed(**kwargs):
            entered.set()
            if not release.wait(8):raise RuntimeError('test upload wait expired')
            return put(**kwargs)
        with ThreadPoolExecutor(max_workers=2) as executor,patch.object(client,'put_object',delayed):
            upload=executor.submit(self.e.save_live_update,'Werner',*self.route,{'w100':{'kind':'exact','price':2000}}, {'source_file':original.name},aid)
            try:
                self.assertTrue(entered.wait(3))
                def read_and_edit():
                    before=self.e.quote('Werner',*self.route,'w100')['price']
                    manual_prices.put('ДЛ',*self.route,'w100',18,'rub_per_kg')
                    return before
                self.assertEqual(executor.submit(read_and_edit).result(timeout=3),1000)
                self.assertIsNone(self.store.metadata(original),'original not published before successful upload')
            finally:release.set()
            upload.result(timeout=5)
        self.assertEqual(self.e.quote('Werner',*self.route,'w100')['price'],2000)
        self.assertEqual(self.e.quote('ДЛ',*self.route,'w100')['price'],1800)
        original.unlink()
        self.assertEqual(self.store.read_bytes(original),b'controlled original')

    def test_nested_database_context_rolls_back_together(self):
        key=self.e.RUNTIME_DIR/'transaction-test.json'
        with self.assertRaises(ValueError):
            with self.db.transaction() as outer:
                self.store.write_json(key,{'saved':False})
                with self.db.transaction() as inner:self.assertIs(inner,outer)
                raise ValueError('rollback')
        self.assertEqual(self.store.read_json(key,{}),{})
        with self.db.transaction() as outer:
            with self.e.STATE_LOCK:self.store.write_json(key,{'saved':True})
            with self.db.transaction() as inner:self.assertIs(inner,outer)
        self.assertEqual(self.store.read_json(key,{}),{'saved':True})

    def test_browser_migration_and_completed_marker(self):
        from app import storage,cloud_migration,price_library as lib
        from app import v42_main as main
        from fastapi.testclient import TestClient
        self.single();content=storage.backup().read_bytes();self.setUp()
        client=TestClient(main.app);client.auth=('manager','test-password-63')
        response=client.post('/api/storage/migration',files={'file':('backup.zip',content,'application/zip')})
        self.assertEqual(response.status_code,200,response.text)
        cloud_migration.WORKER.join(30)
        result=client.get('/api/storage/migration').json()
        self.assertEqual(result['status'],'done',result)
        self.assertEqual(len(lib.list_files()),1)
        # The completion marker commits with prices, even if the worker dies
        # before it manages to publish a final progress message.
        self.store.write_json(self.e.RUNTIME_DIR/'system/migration.json',{'status':'running','worker':'old'})
        self.assertEqual(cloud_migration.status()['status'],'done')

    def test_route_job_restart_can_retry_saved_upload(self):
        from app import route_import_jobs as jobs,document_imports as imp
        with patch.object(jobs,'_run'):
            result=jobs.start(self.csv,'monthly.csv','Werner',*self.route)
        key=result['job_id'];jobs.recover()
        self.assertTrue(jobs.status(key)['can_retry'])
        shutil.rmtree(self.e.RUNTIME_DIR);self.e.RUNTIME_DIR.mkdir()
        result=jobs.retry(key);end=time.monotonic()+20
        while result['status'] in {'queued','parsing'} and time.monotonic()<end:time.sleep(.1);result=jobs.status(key)
        self.assertEqual(result['status'],'ready',result)
        imp.commit(result['preview']['token'])
        self.assertEqual(self.e.quote('Werner',*self.route,'w100')['price'],1300)


if __name__=='__main__':unittest.main()
