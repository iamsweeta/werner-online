import io,json,os,subprocess,sys,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from app import v42_engine as e,v42_main as main,price_library as lib,document_imports as imp,bulk_refresh as bulk,storage

ROUTE=('Казань','Уфа')
ROOT=Path(__file__).resolve().parent.parent

def document(rate=12,reverse=True):
    rows=['Компания;Откуда;Куда;Вес от, кг;Вес до, кг;Тариф;Единица;Минимум, руб',
          'Werner;Казань;Уфа;0;50;600;руб;',f'Werner;Казань;Уфа;50;1000;{rate};руб/кг;600']
    if reverse:rows+=['Werner;Уфа;Казань;0;50;800;руб;',f'Werner;Уфа;Казань;50;1000;{rate+1};руб/кг;800']
    return ('\n'.join(rows)).encode('utf-8')

class SharedLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(e,'RUNTIME_DIR',self.root),patch.object(e,'ROUTE_CONFIG',{})]
        for p in self.patches:p.start()
        self.client=TestClient(main.app)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def save(self,rate=12,single=False):
        if single:
            p=imp.preview(document(rate),'route.csv','Werner',*ROUTE)
            return imp.commit(p['token'])['meta']
        p=lib.start_preview(document(rate),'network.csv','Werner')
        deadline=time.monotonic()+20
        while p['status']=='parsing' and time.monotonic()<deadline:time.sleep(.01);p=lib.status(p['token'])
        self.assertEqual(p['status'],'ready',p)
        return lib.commit(p['token'])
    def online(self):
        a=e.begin_live_attempt('Werner',*ROUTE)
        e.save_live_update('Werner',*ROUTE,{'w100':{'kind':'exact','price':2000,'rate_per_kg':20}}, {},a)
        e.finish_live_attempt('Werner',*ROUTE,a,rows=1)
    def choose(self,ident,route=ROUTE,company='Werner'):
        return self.client.post('/api/route-documents/select',json={'company':company,'origin':route[0],'destination':route[1],'document_id':ident})
    def test_library_upload_immediately_drives_route_and_both_excel_files(self):
        self.online();saved=self.save()
        data=self.client.get('/api/route-documents',params=dict(zip(('origin','destination'),ROUTE))).json()
        self.assertTrue(data['files'][0]['selected']);self.assertEqual(data['files'][0]['id'],saved['id'])
        rows=self.client.get('/api/profile-matrix',params={'origin':ROUTE[0],'destination':ROUTE[1],'companies':'Werner'}).json()['profiles']
        q=next(r['items'][0] for r in rows if r['profile']['id']=='w100')
        self.assertEqual(q['price'],1200);self.assertTrue(q['document_selected']);self.assertFalse(q['online'])
        for endpoint,sheet in [('/api/export/route','Маршрут'),('/api/export/excel','WernerNEW')]:
            response=self.client.get(endpoint,params={'origin':ROUTE[0],'destination':ROUTE[1],'companies':'Werner'})
            self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
            wb=load_workbook(io.BytesIO(response.content),data_only=True)
            if sheet=='Маршрут':self.assertTrue(any(1200 in r for r in wb[sheet].values))
            else:self.assertEqual(wb[sheet]['N2'].value,12)
            wb.close()
        payload=bulk.capture_company('Werner',*ROUTE,{'ok':True})
        filled=bulk.fill_document_gaps([{'profile':p,'items':[payload['items'][i]]} for i,p in enumerate(e.COMMON_PROFILES)],imp.pack(*ROUTE),*ROUTE)
        self.assertEqual(next(r['items'][0]['published_rate_per_kg'] for r in filled if r['profile']['id']=='w100'),12)
        self.assertEqual(self.choose(None).status_code,200);self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],2000)
    def test_single_route_upload_is_in_the_same_library(self):
        self.save(single=True);files=self.client.get('/api/price-documents').json()['files']
        self.assertEqual(len(files),1);self.assertEqual(files[0]['route_count'],1)
        data=self.client.get('/api/price-documents/'+files[0]['id']+'/routes').json()
        self.assertEqual([(r['origin'],r['destination']) for r in data['routes']],[ROUTE])
        self.assertIsNone(e.quote('Werner',*ROUTE[::-1],'w100')['price'])
        self.assertEqual(self.client.get('/api/import-file/'+files[0]['source_file']).content,document())
    def test_choosing_older_file_is_route_scoped_persistent_and_invalidates_excel(self):
        older=self.save(12);newer=self.save(15)
        revision=imp.revision();self.assertEqual(self.choose(older['id']).status_code,200)
        self.assertGreater(imp.revision(),revision)
        self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],1200)
        self.assertEqual(e.quote('Werner',*ROUTE[::-1],'w100')['price'],1600)
        code="import json;from app import v42_engine as e;print(json.dumps(e.quote('Werner','Казань','Уфа','w100')))"
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env={**os.environ,'TARIFF_DATA_DIR':str(self.root)},capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['document_id'],older['id'])
        with zipfile.ZipFile(storage.backup()) as z:self.assertIn('imports/documents.sqlite3',z.namelist())
        self.assertEqual(self.choose(older['id'],company='ДЛ').status_code,400)
        self.assertEqual(self.choose(older['id'],route=('Казань','Москва')).status_code,400)
        lib.remove(older['id']);self.assertEqual(self.choose(older['id']).status_code,400)
        self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],1500)
    def test_old_route_json_migrates_without_duplicate_or_reviving_disabled_file(self):
        ident='a'*32;filename=ident+'.csv';(imp.root()/'files').mkdir(parents=True)
        (imp.root()/'files'/filename).write_bytes(document())
        meta={'source_file':filename,'original_filename':'old.csv','extension':'.csv','company':'Werner','origin':ROUTE[0],'destination':ROUTE[1],'import_revision':1,'uploaded_at':'2026-01-01T00:00:00+00:00'}
        value={**meta,'kind':'exact','price':1200,'rate_per_kg':12}
        e._robust_json_write(imp.route_path(*ROUTE),{'companies':{'Werner':meta},'profiles':{'w100':{'Werner':value}}})
        self.assertEqual(len(lib.list_files()),1);self.assertEqual(len(lib.list_files()),1)
        self.assertEqual(e.quote('Werner',*ROUTE,'w100')['price'],1200)
        self.assertEqual(self.choose(ident).status_code,200)
        lib.remove(ident);lib._MIGRATED.clear()
        self.assertEqual(lib.list_files(),[]);self.assertIsNone(e.quote('Werner',*ROUTE,'w100')['price'])

if __name__=='__main__':unittest.main()
