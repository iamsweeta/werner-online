import io,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import v42_engine as e,v42_main as main,document_imports as imports,route_import_jobs as jobs

RAW='Компания;Откуда;Куда;Вес, кг;Цена, руб\nWerner;Казань;Уфа;100;1234\n'.encode()
# Use the existing parser's explicit template, no live pricing fixture.
from app.tariff_documents import GENERIC_HEADER
RAW=(';'.join(GENERIC_HEADER)+'\nWerner;Казань;Уфа;100;1234\n').encode()

class RouteJobs(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name);self.ctx=patch.object(e,'RUNTIME_DIR',self.folder);self.ctx.start();self.client=TestClient(main.app)
    def tearDown(self):self.ctx.stop();self.tmp.cleanup()
    def start(self,key='1'*32,raw=RAW):
        return self.client.post('/api/import/jobs',data={'job_id':key,'company':'Werner','origin':'Казань','destination':'Уфа'},files={'file':('route.csv',raw)})
    def wait(self,key='1'*32):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            r=self.client.get('/api/import/jobs/'+key);self.assertEqual(r.status_code,200,r.text);j=r.json()
            if j['status'] not in ('queued','parsing'):return j
            time.sleep(.02)
        self.fail('job did not finish')
    def test_ack_and_status_do_not_wait_for_parser_and_duplicate_does_not_restart(self):
        entered=threading.Event();release=threading.Event();original=imports.preview
        def slow(*args):entered.set();release.wait(5);return original(*args)
        with patch.object(imports,'preview',side_effect=slow) as parse:
            try:
                start=time.monotonic();r=self.start();self.assertEqual(r.status_code,202,r.text);self.assertLess(time.monotonic()-start,1)
                self.assertTrue(entered.wait(1));j=self.client.get('/api/import/jobs/'+'1'*32).json();self.assertEqual(j['status'],'parsing')
                self.assertEqual(self.start().status_code,202);self.assertEqual(parse.call_count,1)
                self.assertEqual(self.start(raw=RAW.replace(b'1234',b'9999')).status_code,400)
                self.assertEqual(self.client.get('/health').status_code,200)
            finally:release.set()
            j=self.wait();self.assertEqual(j['status'],'ready',j)
        self.assertIsNone(e.quote('Werner','Казань','Уфа','w100')['price'])
        self.assertEqual(j['preview']['rows'][0]['price'],1234)
    def test_result_visible_from_another_process_and_commit_stays_explicit(self):
        self.start();j=self.wait();self.assertEqual(j['status'],'ready',j)
        script="from app.route_import_jobs import status;import json;print(json.dumps(status('"+'1'*32+"')))"
        r=subprocess.run([sys.executable,'-c',script],cwd=Path(__file__).resolve().parent.parent,env={**os.environ,'TARIFF_DATA_DIR':str(self.folder)},capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(r.stdout)['preview']['token'],j['preview']['token'])
        r=self.client.post('/api/import/commit',json={'token':j['preview']['token']});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(e.quote('Werner','Казань','Уфа','w100')['price'],1234)
        self.assertEqual(jobs.status('1'*32)['status'],'committed')
        self.assertIsNone(e.quote('Werner','Уфа','Казань','w100')['price'])
    def test_errors_come_from_job_without_http_gateway_error(self):
        with patch.object(imports,'preview',side_effect=ValueError('Город отправления не совпадает')):
            self.assertEqual(self.start().status_code,202);j=self.wait();self.assertEqual(j['status'],'error');self.assertIn('Город',j['message'])
        self.assertIsNone(e.quote('Werner','Казань','Уфа','w100')['price'])
    def test_stale_worker_and_expiration_have_explicit_states(self):
        self.start();self.wait()
        with jobs.database() as c:c.execute("UPDATE jobs SET status='parsing',updated=? WHERE id=?",(time.time()-100,'1'*32))
        j=jobs.status('1'*32);self.assertEqual(j['status'],'interrupted');self.assertIn('перезапустился',j['message'])
        self.start('2'*32);self.wait('2'*32)
        with jobs.database() as c:c.execute('UPDATE jobs SET updated=? WHERE id=?',(time.time()-1900,'2'*32))
        self.assertEqual(jobs.status('2'*32)['status'],'expired')
        jobs.cleanup();self.assertEqual(self.client.get('/api/import/jobs/'+'2'*32).status_code,404)
    def test_queue_capacity_validation_and_no_duplicate_application(self):
        entered=threading.Event();release=threading.Event();original=imports.preview
        def slow(*args):entered.set();release.wait(5);return original(*args)
        with patch.object(imports,'preview',side_effect=slow):
            try:
                self.assertEqual(self.start().status_code,202);self.assertTrue(entered.wait(1))
                self.assertEqual(self.start('2'*32).status_code,202);self.assertEqual(self.start('3'*32).status_code,400)
            finally:release.set()
            self.wait();self.wait('2'*32)
        self.assertEqual(self.start('not-an-id').status_code,400)
        self.assertEqual(self.start(raw=b'').status_code,400)
        self.assertEqual(self.client.get('/api/import/jobs/'+'a'*32).status_code,404)
    def test_progress_is_stored_and_preview_is_not_in_backups(self):
        original=imports.preview
        def progress(*args):
            from app import scan_ocr
            scan_ocr._PROGRESS.get()({'done':2,'total':9,'message':'Страница 2 из 9'})
            return original(*args)
        with patch.object(imports,'preview',side_effect=progress):
            self.start();j=self.wait();self.assertEqual(j['ocr_page'],2);self.assertEqual(j['ocr_total'],9)
        from app.storage import backup
        import zipfile
        with zipfile.ZipFile(backup()) as z:self.assertTrue(all('route_jobs' not in x for x in z.namelist()))

if __name__=='__main__':unittest.main()
