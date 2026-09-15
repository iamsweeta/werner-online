import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import v42_engine as e
from app import v42_main as main


class ReliabilityV427Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old={k:v['live'] for k,v in e.ROUTE_CONFIG.items()}
        for key,cfg in e.ROUTE_CONFIG.items():
            cfg['live']=Path(self.tmp.name)/(cfg['slug']+'.json')
        e._cached_json.cache_clear()
        main.COLLECT_JOBS.clear(); main.EXACT_REVISION=0
        self.client=TestClient(main.app)

    def tearDown(self):
        for k,p in self.old.items(): e.ROUTE_CONFIG[k]['live']=p
        e._cached_json.cache_clear(); self.tmp.cleanup()

    def test_fresh_install_options_has_no_retry_sleep(self):
        with patch.object(e.time,'sleep',side_effect=AssertionError('missing LIVE file must not sleep')):
            r=self.client.get('/api/options',params={'origin':'Санкт-Петербург'})
        self.assertEqual(r.status_code,200)
        d=r.json(); self.assertEqual(d['origins'],['Москва','Санкт-Петербург']); self.assertGreater(len(d['all_origins']),200); self.assertEqual(len(d['companies']),17); self.assertEqual(len(d['profiles']),29)

    def test_concurrent_live_writes_keep_valid_json(self):
        route=('Санкт-Петербург','Москва')
        companies=e.COMPANIES[:10]
        errors=[]
        def worker(i,c):
            try:
                aid=e.begin_live_attempt(c,*route,'w100')
                e.save_live_update(c,*route,{'w100':{'kind':'exact','price':1000+i}}, {'source_url':f'https://example.test/{i}'}, aid)
                e.finish_live_attempt(c,*route,aid,rows=1)
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=worker,args=(i,c)) for i,c in enumerate(companies)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertFalse(errors)
        path=e.live_path_for(*route); payload=json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(set(companies).issubset(set(payload.get('companies',{}))))

    def test_windows_style_replace_lock_is_retried(self):
        target=Path(self.tmp.name)/'locked.json'
        real=e.os.replace; calls={'n':0}
        def flaky(src,dst):
            calls['n']+=1
            if calls['n']<3: raise PermissionError('locked')
            return real(src,dst)
        with patch.object(e.os,'replace',side_effect=flaky), patch.object(e.time,'sleep',return_value=None):
            e._robust_json_write(target,{'ok':True})
        self.assertGreaterEqual(calls['n'],3); self.assertEqual(json.loads(target.read_text()),{'ok':True})

    def test_static_and_api_are_no_cache(self):
        for url in ['/?build=42.9-fullgrid','/static/app.js?v=429-fullgrid','/api/options?origin=Москва']:
            r=self.client.get(url); self.assertEqual(r.status_code,200); self.assertIn('no-store',r.headers.get('cache-control',''))

    def test_permanent_windows_lock_preserves_previous_cache(self):
        target=Path(self.tmp.name)/'preserved.json'
        target.write_text('{"old":true}')
        with patch.object(e.os,'replace',side_effect=PermissionError('locked')), patch.object(e.time,'sleep',return_value=None):
            with self.assertRaises(PermissionError):e._robust_json_write(target,{'new':True})
        self.assertEqual(json.loads(target.read_text()),{'old':True})


if __name__=='__main__': unittest.main()
