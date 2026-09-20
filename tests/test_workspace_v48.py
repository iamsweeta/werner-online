"""v48: multi-route documents and the primary reference report. Prices are test fixtures."""
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from app import price_library as library, document_imports as imports, tariff_documents as docs
from app import v42_engine as e, v42_main as main, bulk_refresh as b

HEADER='Компания;Откуда;Куда;Вес, кг;Цена, руб\r\n'
ROWS=[('Werner','Казань','Уфа',100,1234),('Werner','Казань','Уфа',200,2345),
      ('Werner','Уфа','Казань',100,5678),('ДЛ','Казань','Уфа',100,9999)]

def csv(rows=ROWS):return ('\ufeff'+HEADER+''.join(';'.join(map(str,r))+'\r\n' for r in rows)).encode('utf-8')

def xlsx(rows=ROWS):
    wb=Workbook();ws=wb.active;ws.append(docs.GENERIC_HEADER)
    for row in rows:ws.append(row)
    out=io.BytesIO();wb.save(out);return out.getvalue()

class WorkspaceDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.ctx=patch.object(e,'RUNTIME_DIR',self.root);self.ctx.start()
        self.routes=patch.object(e,'ROUTE_CONFIG',{});self.routes.start()
        self.client=TestClient(main.app);self.managers=[]
    def tearDown(self):
        for m in self.managers:
            if m.worker:m.worker.join(30)
            if m.export_worker:m.export_worker.join(40)
        self.routes.stop();self.ctx.stop();self.tmp.cleanup()
    def preview(self,raw=None,name='test.csv',company='Werner',**kwargs):
        r=self.client.post('/api/price-documents/preview',data={'company':company,**kwargs},files={'file':(name,csv() if raw is None else raw)})
        self.assertEqual(r.status_code,200,r.text);job=r.json();deadline=time.monotonic()+20
        while job['status']=='parsing' and time.monotonic()<deadline:
            time.sleep(.01);job=self.client.get('/api/price-documents/preview/'+job['token']).json()
        self.assertNotEqual(job['status'],'parsing',job)
        return job
    def save(self,**kwargs):
        job=self.preview(**kwargs);self.assertEqual(job['status'],'ready',job)
        r=self.client.post('/api/price-documents/commit',json={'token':job['token']})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def manager(self):
        def no_network(*a,**k):raise AssertionError('Saved report must never call collectors')
        m=b.BulkManager(self.root/'bulk',collector=no_network);self.managers.append(m);return m
    def done(self,m):
        m.worker.join(20);self.assertFalse(m.worker.is_alive())
        if m.export_worker:m.export_worker.join(40);self.assertFalse(m.export_worker.is_alive())
        job=m.status();self.assertEqual(job['status'],'done',job);self.assertEqual(job['export_status'],'ready',job)
        return job
    def test_many_routes_preview_confirm_source_download_and_company_isolation(self):
        job=self.preview();self.assertEqual(job['status'],'ready',job);self.assertEqual(job['matched'],2)
        self.assertEqual(job['values_count'],3);self.assertEqual(library.list_files(),[])
        self.assertIsNone(e.quote('Werner','Казань','Уфа','w100')['price'])
        saved=self.client.post('/api/price-documents/commit',json={'token':job['token']}).json()
        self.assertEqual(saved['route_count'],2);self.assertEqual(imports.revision(),1)
        for route,price in [(('Казань','Уфа'),1234),(('Уфа','Казань'),5678)]:
            q=e.quote('Werner',*route,'w100');self.assertEqual(q['price'],price);self.assertTrue(q['uploaded']);self.assertFalse(q['online'])
        self.assertIsNone(e.quote('ДЛ','Казань','Уфа','w100')['price'])
        self.assertEqual(self.client.get('/api/import-file/'+saved['source_file']).content,csv())
        self.assertEqual(self.client.post('/api/price-documents/commit',json={'token':job['token']}).status_code,400)
        e._cached_json.cache_clear();self.assertEqual(imports.pack('Уфа','Казань')['profiles']['w100']['Werner']['price'],5678)
        self.assertEqual(self.client.delete('/api/price-documents/'+saved['id']).status_code,200)
        self.assertIsNone(e.quote('Werner','Казань','Уфа','w100')['price']);self.assertEqual(imports.revision(),2)
    def test_xlsx_csv_company_and_origin_filters(self):
        for raw,name in [(xlsx(),'test.xlsx'),(csv(),'test.csv')]:
            job=self.preview(raw,name,origin='Казань');self.assertEqual(job['matched'],1,job)
            self.assertEqual(job['routes'][0]['values']['w100']['price'],1234)
            job=self.preview(raw,name,company='Пролайн');self.assertEqual(job['status'],'error',job)
        job=self.preview(csv().decode('utf-8-sig').encode('cp1251'),origin='Уфа')
        self.assertEqual(job['routes'][0]['values']['w100']['price'],5678)
    def test_replacement_never_reuses_omitted_weights_from_previous_file(self):
        self.save();new=self.save(raw=csv([ROWS[0][:4]+(777,)]))
        p=imports.pack('Казань','Уфа');self.assertEqual(p['profiles']['w100']['Werner']['price'],777)
        self.assertNotIn('Werner',p['profiles'].get('w200',{}))
        library.remove(new['id']);self.assertEqual(library.pack('Казань','Уфа')['profiles'],{})
    def test_legacy_route_import_and_multi_import_replace_whole_carrier(self):
        old=imports.preview(xlsx(ROWS[:2]),'old.xlsx','Werner','Казань','Уфа');imports.commit(old['token'])
        self.save(raw=csv([ROWS[0][:4]+(777,)]));p=imports.pack('Казань','Уфа')
        self.assertEqual(p['profiles']['w100']['Werner']['price'],777);self.assertNotIn('Werner',p['profiles'].get('w200',{}))
        single=imports.preview(xlsx([ROWS[1]]),'single.xlsx','Werner','Казань','Уфа');imports.commit(single['token'])
        p=imports.pack('Казань','Уфа');self.assertNotIn('Werner',p['profiles'].get('w100',{}));self.assertEqual(p['profiles']['w200']['Werner']['price'],2345)
    def test_invalid_format_future_dates_duplicate_weights_and_unconfirmed_data(self):
        for raw,name in [(b'%PDF-broken','x.pdf'),(b'PKbad','x.xlsx'),(b'bad','x.exe'),(b'','x.csv')]:
            r=self.client.post('/api/price-documents/preview',data={'company':'Werner'},files={'file':(name,raw)})
            self.assertEqual(r.status_code,400,r.text)
        r=self.client.post('/api/price-documents/preview',data={'company':'Werner','document_date':'2099-01-01'},files={'file':('x.csv',csv())})
        self.assertEqual(r.status_code,400)
        for rows in [[ROWS[0],ROWS[0][:4]+(888,)],[ROWS[0][:4]+(-1,)],[ROWS[0][:4]+('=1+2',)]]:
            job=self.preview(csv(rows));self.assertEqual(job['status'],'error',job)
        self.assertEqual(library.list_files(),[])
    def test_zip_multiple_documents_rejects_ambiguous_versions(self):
        for duplicate in [False,True]:
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w') as z:
                z.writestr('a.csv',csv(ROWS[:2]));z.writestr('b.xlsx',xlsx(ROWS[:2] if duplicate else ROWS[2:3]))
            job=self.preview(out.getvalue(),'test.zip');self.assertEqual(job['status'],'ready',job)
            self.assertFalse(job['conflicts'])
    def test_multiple_files_merge_complementary_weights_and_require_conflict_choices(self):
        first=csv([ROWS[0]]);second=csv([ROWS[1],ROWS[0][:4]+(777,)])
        response=self.client.post('/api/price-documents/preview',data={'company':'Werner'},files=[('files',('first.csv',first)),('files',('second.csv',second))])
        self.assertEqual(response.status_code,200,response.text);job=response.json()
        for _ in range(500):
            if job['status']!='parsing':break
            time.sleep(.01);job=library.status(job['token'])
        self.assertEqual(job['status'],'ready',job);self.assertEqual(job['matched'],1)
        self.assertEqual(len(job['conflicts']),1);conflict=job['conflicts'][0]
        self.assertEqual([v['price'] for v in conflict['options']],[1234,777])
        self.assertEqual(self.client.post('/api/price-documents/commit',json={'token':job['token']}).status_code,400)
        self.assertEqual(library.list_files(),[])
        for choices in [{conflict['id']:100},{'fake':0}]:
            self.assertEqual(self.client.post('/api/price-documents/commit',json={'token':job['token'],'resolutions':choices}).status_code,400)
        response=self.client.post('/api/price-documents/commit',json={'token':job['token'],'resolutions':{conflict['id']:1}})
        self.assertEqual(response.status_code,200,response.text)
        q=e.quote('Werner','Казань','Уфа','w100');self.assertEqual(q['price'],777)
        self.assertEqual(q['original_filename'],'02_second.csv')
        self.assertEqual(e.quote('Werner','Казань','Уфа','w200')['price'],2345)
        raw=self.client.get('/api/import-file/'+q['source_file']).content
        with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertEqual(z.read('02_second.csv'),second)
    def test_multiple_file_limits_and_zip_combination_validation(self):
        for files in [[('files',('one.zip',b'PKbad')),('files',('two.csv',csv()))],[('files',(str(i)+'.csv',csv())) for i in range(31)]]:
            r=self.client.post('/api/price-documents/preview',data={'company':'Werner'},files=files)
            self.assertEqual(r.status_code,400,r.text)
    def test_blank_multi_route_template_has_reference_routes_and_no_prices(self):
        r=self.client.get('/api/price-documents/template',params={'company':'Werner','kind':'points'})
        self.assertEqual(r.status_code,200)
        wb=load_workbook(io.BytesIO(r.content),data_only=True);rows=list(wb['Прайс-лист'].values)
        self.assertEqual(list(rows[0]),docs.GENERIC_HEADER)
        self.assertEqual(len(rows)-1,254*29);self.assertTrue(all(r[4] is None for r in rows[1:]))
        self.assertEqual(len({(r[1],r[2]) for r in rows[1:]}),254);wb.close()
    def test_generic_pdf_multi_detection(self):
        raw=(Path(__file__).parent/'fixtures/generic_import.pdf').read_bytes()
        job=self.preview(raw,'test.pdf');self.assertEqual(job['status'],'ready',job)
        self.assertEqual(job['matched'],1);self.assertEqual(job['routes'][0]['values']['w100']['price'],1234.5)
    def test_saved_report_populates_all_companies_docs_only_and_requires_rebuild(self):
        saved=self.save();m=self.manager();m.start('origins',['Казань'],['Уфа'],mode='saved');job=self.done(m)
        self.assertEqual(job['completed_checks'],17);self.assertEqual(job['outcomes'],{'saved':17})
        coverage=next(c for c in job['coverage'] if c['company']=='Werner')
        self.assertEqual(coverage,{'company':'Werner','online':0,'saved':0,'document':0,'missing':28})
        with io.BytesIO(m.download(job['job_id']).read_bytes()) as buffer:
            wb=load_workbook(buffer,data_only=True)
            self.assertEqual(wb['WernerNEW']['B2'].value,'Казань');self.assertEqual(wb['WernerNEW']['N2'].value,None)
            self.assertIsNone(wb['WernerNEW']['M2'].value);self.assertEqual(wb['ДЛ']['N2'].value,None)
            self.assertTrue(any(r[5]=='Файл пользователя' and r[9]=='test.csv' for r in list(wb['Источники'].values)[1:]));wb.close()
        self.save(raw=csv([ROWS[0][:4]+(2222,)]));self.assertTrue(m.status()['export_outdated']);self.assertIsNone(m.status()['download_url'])
        with self.assertRaisesRegex(ValueError,'Пересоберите'):m.download(job['job_id'])
        m.prepare_export(job['job_id']);m.export_worker.join(40);self.assertFalse(m.status()['export_outdated'])
        wb=load_workbook(m.download(job['job_id']),data_only=True);self.assertEqual(wb['WernerNEW']['N2'].value,None);wb.close()
    def test_online_exact_has_priority_and_minimum_is_lowest_shipment(self):
        self.save(raw=csv([('Werner','Казань','Уфа',1,600),ROWS[0],ROWS[1]]))
        library.select_document('Werner','Казань','Уфа',None)
        aid=e.begin_live_attempt('Werner','Казань','Уфа')
        e.save_live_update('Werner','Казань','Уфа',{'w100':{'kind':'exact','price':3000}},{'source_url':'https://example.invalid/test'},aid)
        e.finish_live_attempt('Werner','Казань','Уфа',aid,rows=1)
        m=self.manager();m.start('origins',['Казань'],['Уфа'],mode='saved');job=self.done(m)
        rows=m._route_rows(job['job_id'],0,'Казань','Уфа',imports.pack('Казань','Уфа'))
        values={r['profile']['id']:next(i for i in r['items'] if i['company']=='Werner') for r in rows}
        self.assertEqual(values['w100']['price'],3000);self.assertTrue(values['w100']['collected_online'])
        self.assertEqual(values['w200']['price'],2345);self.assertTrue(values['w200']['uploaded'])
        self.assertEqual(values['min']['price'],600);self.assertTrue(values['min']['uploaded'])
    def test_selected_route_export_does_not_include_other_document_routes(self):
        self.save();response=self.client.get('/api/export/excel',params={'origin':'Казань','destination':'Уфа','companies':'Werner'})
        self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
        wb=load_workbook(io.BytesIO(response.content),data_only=True)
        self.assertEqual(wb['склейка']['B2'].value,'Казань');self.assertIsNone(wb['склейка']['B3'].value)
        self.assertEqual(wb['WernerNEW']['N2'].value,None);wb.close()

if __name__=='__main__':unittest.main()
