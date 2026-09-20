from __future__ import annotations

import json
import ssl
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

from app import network as net, v42_collectors as col, v42_engine as engine, v42_main as main


class DownloadTests(unittest.TestCase):
    def test_range_download_assembles_one_complete_version(self):
        from types import SimpleNamespace
        payload=b'PK\x03\x04'+b'x'*17600
        def piece(url,start,end,etag,seconds):
            end=min(end,len(payload)-1)
            response=SimpleNamespace(status_code=206,url=url,headers={'Content-Range':f'bytes {start}-{end}/{len(payload)}','ETag':'"version-1"'})
            return response,payload[start:end+1]
        with patch.object(net,'_range_piece',side_effect=piece):
            result=net.download_ranges('https://carrier.test/price.xlsx')
        self.assertEqual(result.content,payload);self.assertEqual(result.transport,'curl-cffi-ranges')

    def test_range_download_rejects_changed_file_wrong_offset_or_denial(self):
        from types import SimpleNamespace
        for mode in ['version','offset','denied','foreign']:
            def piece(url,start,end,etag,seconds):
                end=min(end,9999)
                headers={'Content-Range':f'bytes {start}-{end}/10000','ETag':'"v1"'}
                if start and mode=='version':headers['ETag']='"v2"'
                if start and mode=='offset':headers['Content-Range']='bytes 0-1999/10000'
                return SimpleNamespace(status_code=403 if mode=='denied' else 206,url='https://other.test/' if mode=='foreign' else url,headers=headers),b'x'*(end-start+1)
            with self.subTest(mode=mode),patch.object(net,'_range_piece',side_effect=piece),self.assertRaises(net.RejectedResponse):net.download_ranges('https://carrier.test/price.xlsx')

    def test_connection_scope_reuses_and_closes_session_after_batch(self):
        from unittest.mock import MagicMock
        session=MagicMock()
        response=session.get.return_value.__enter__.return_value
        response.status_code=200;response.url='https://carrier.test';response.iter_content.side_effect=lambda *a:iter([b'current'])
        with patch.object(net.requests,'Session',return_value=session) as create:
            with net.connection_scope():
                net._requests_download(response.url,{},10)
                net._requests_download(response.url,{},10)
                self.assertEqual(create.call_count,1);session.close.assert_not_called()
            session.close.assert_called_once()

    def test_tls_failure_uses_verified_tls12_transport(self):
        good=net.Download(b'%PDF-tariff','https://carrier.test/price.pdf','requests-tls12')
        with patch.object(net,'_requests_download',side_effect=[requests.exceptions.SSLError('UNEXPECTED_EOF'),good]) as fetch:
            response=net.download(good.url,expected='PDF')
        self.assertEqual(response.content,b'%PDF-tariff')
        self.assertTrue(fetch.call_args.kwargs['tls12'])
        adapter=net.TLS12Adapter()
        self.assertEqual(adapter.context.minimum_version,ssl.TLSVersion.TLSv1_2)
        self.assertEqual(adapter.context.maximum_version,ssl.TLSVersion.TLSv1_2)
        self.assertEqual(adapter.context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(adapter.context.check_hostname)

    def test_independent_cffi_backend_recovers_when_requests_fails(self):
        result=net.Download(b'{"price":100}','https://carrier.test/api/','curl-cffi-tls12')
        with patch.object(net,'_requests_download',side_effect=TimeoutError('timeout')) as req, patch.object(net,'_cffi_download',return_value=result) as independent:
            self.assertEqual(net.download(result.url,expected='JSON').transport,'curl-cffi-tls12')
        self.assertEqual(req.call_count,2);independent.assert_called_once()

    def test_shared_budget_stops_further_transports(self):
        clock=[100.0]
        def slow(*args,**kwargs):clock[0]+=16;raise TimeoutError('timeout')
        with patch.object(net.time,'monotonic',side_effect=lambda:clock[0]),patch.object(net,'_requests_download',side_effect=slow) as req,patch.object(net,'_cffi_download') as cffi:
            with self.assertRaises(net.DownloadError):net.download('https://carrier.test',timeout=16)
        self.assertEqual(req.call_count,1);cffi.assert_not_called()

    def test_authorization_failure_does_not_try_other_transports(self):
        rejected=net.Download(b'Unauthorized','https://carrier.test','requests',status_code=401)
        with patch.object(net,'_requests_download',return_value=rejected) as req,patch.object(net,'_cffi_download') as other:
            with self.assertRaisesRegex(net.RejectedResponse,'401'):net.download(rejected.url)
        self.assertEqual(req.call_count,1);other.assert_not_called()

    def test_wrong_content_or_redirect_is_not_tariff_evidence(self):
        cases=[(net.Download(b'<html>login</html>','https://carrier.test/price','requests'),'XLSX'),
               (net.Download(b'<title>Just a moment</title>','https://carrier.test/price','requests'),'HTML'),
               (net.Download(b'%PDF-file','https://other.test/price','requests'),'PDF'),
               (net.Download(b'%PDF-file','http://carrier.test/price','requests'),'PDF')]
        for result,fmt in cases:
            with self.subTest(fmt=fmt,url=result.url),patch.object(net,'_requests_download',return_value=result):
                with self.assertRaises(net.RejectedResponse):net.download('https://carrier.test/price',expected=fmt)

    def test_download_size_limit_and_curl_does_not_disable_verification(self):
        with patch.object(net,'MAX_BYTES',4):
            with self.assertRaisesRegex(net.RejectedResponse,'50 МБ'):net.validate_reply(net.Download(b'12345','https://carrier.test','mock'))
        def run(cmd,**kwargs):
            self.assertNotIn('--insecure',cmd);self.assertNotIn('--ssl-no-revoke',cmd)
            self.assertIn('--tls-max',cmd);self.assertEqual(cmd[cmd.index('--tls-max')+1],'1.2')
            Path(cmd[cmd.index('--output')+1]).write_bytes(b'current')
            return type('Result',(),{'returncode':0,'stdout':b'200\nhttps://carrier.test/final\ntext/plain','stderr':b''})()
        with patch.object(net.subprocess,'run',side_effect=run):
            result=net._curl_download('https://carrier.test',{},10)
        self.assertEqual(result.url,'https://carrier.test/final');self.assertEqual(result.content,b'current')

    def test_windows_cyrillic_console_and_error_categories(self):
        with patch.object(net.os,'name','nt'):
            self.assertEqual(net.decode_console('Не удалось установить соединение'.encode('cp866')),'Не удалось установить соединение')
        for raw,code in [('HTTP 401','access'),('SSLError: UNEXPECTED_EOF','tls'),('ReadTimeout: timed out','timeout'),('certificate_verify_failed','certificate'),('маршрут не подтверждён','format')]:
            self.assertEqual(net.explain_error(raw)['code'],code)


class RouteTests(unittest.TestCase):
    route=('Санкт-Петербург','Москва')
    def test_baikal_rejects_inbound_card_before_outbound_card(self):
        # Minimal excerpt reproduces the order of cards in the actual official page.
        raw=(Path(__file__).parent/'fixtures/baikal_spb_direction.html').read_bytes()
        url=col._route_page_url('Байкал Сервис',*self.route)
        with patch.object(col,'_download_html_bytes',return_value=(raw,'requests',url)):
            result=col._live_route_minimum('Байкал Сервис',*self.route)
        self.assertEqual(result['price'],588);self.assertTrue(result['price_is_minimum'])
        raw=raw.replace('в Москва'.encode(),'из Москва'.encode())
        with patch.object(col,'_download_html_bytes',return_value=(raw,'requests',url)):
            with self.assertRaises(RuntimeError):col._live_route_minimum('Байкал Сервис',*self.route)

    def test_route_checks_order_declension_charset_and_redirect(self):
        url=col._route_page_url('ДЛ',*self.route)
        raw='<meta charset="windows-1251"><h1>Перевозки из Санкт-Петербурга в Москву</h1>'.encode('cp1251')
        self.assertIsNotNone(col._route_soup(raw,url,url,*self.route))
        for html,final in [('<h1>Москва — Санкт-Петербург</h1>',url),('<nav>Санкт-Петербург Москва</nav><h1>Проверка</h1>',url),('<h1>Санкт-Петербург Москва</h1>','https://www.dellin.ru/')]:
            with self.subTest(html=html),self.assertRaises(RuntimeError):col._route_soup(html.encode(),final,url,*self.route)

    def test_default_lower_bound_does_not_start_browser_or_claim_control_volume(self):
        lower={'price':588,'status':'ok','price_is_minimum':True,'source_type':'Официальная маршрутная страница','source_url':'https://www.baikalsr.ru/city/spb__moscow/','browser':'requests'}
        with patch.object(col,'_browser_enabled',return_value=False),patch('app.legacy_backend.load_settings',return_value={}),patch('app.public_web.calculate_from_public_site') as browser,patch.object(col,'_live_route_minimum',return_value=lower):
            values,meta=col._collect_live_calculator('Байкал Сервис',*self.route,'w100')
        browser.assert_not_called();self.assertEqual(values['w100']['kind'],'lower_bound')
        self.assertIsNone(meta['volume_m3']);self.assertNotIn('0.5',meta['calculation_basis'])

    def test_fast_document_is_published_before_slow_carrier(self):
        from app import legacy as core,legacy_backend as lb
        fast_ready=threading.Event();slow_released=threading.Event();observed=[]
        sources=[{'id':'fast','company':'ПЭК'},{'id':'slow','company':'КИТ'}]
        def download(source):
            if source['id']=='slow':
                if not fast_ready.wait(2):raise AssertionError('Fast company was blocked')
                slow_released.set()
            return {'status':source,'files':[],'rows':[],'errors':[]}
        def ready(company):
            observed.append(company)
            if company=='ПЭК':fast_ready.set()
        with patch.object(core,'load_sources',return_value={'sources':sources}),patch.object(lb,'_source_ids_for_origin',return_value={'fast','slow'}),patch.object(col,'_refresh_one_document_source',side_effect=download),patch.object(col,'_merge_document_chunks',side_effect=lambda catalog,chunks:[x['status'] for x in chunks]):
            col._collect_document_batch(['ПЭК','КИТ'],*self.route,on_company=ready)
        self.assertEqual(observed,['ПЭК','КИТ']);self.assertTrue(slow_released.is_set())


class AppRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old={k:v['live'] for k,v in engine.ROUTE_CONFIG.items()}
        for i,v in enumerate(engine.ROUTE_CONFIG.values()):v['live']=Path(self.tmp.name)/f'{i}.json'
        self.route=('Москва','Санкт-Петербург');self.client=TestClient(main.app);main.COLLECT_JOBS.clear()
    def tearDown(self):
        for k,v in self.old.items():engine.ROUTE_CONFIG[k]['live']=v
        main.COLLECT_JOBS.clear();self.tmp.cleanup()

    def test_failure_auto_backoff_across_weights_but_manual_retry_is_immediate(self):
        aid=engine.begin_live_attempt('ПЭК',*self.route,'w100');engine.finish_live_attempt('ПЭК',*self.route,aid,rows=0,error='SSLError: EOF')
        params={'origin':self.route[0],'destination':self.route[1],'companies':['ПЭК'],'profile':'w200'}
        with patch.object(main,'threading') as thread_module:
            response=self.client.post('/api/collect',json={**params,'force':False}).json()
            self.assertEqual(response['status'],'fresh');thread_module.Thread.assert_not_called()
            response=self.client.post('/api/collect',json={**params,'force':True}).json()
            self.assertEqual(response['status'],'queued');thread_module.Thread.assert_called_once()

    def test_auto_backoff_expires(self):
        aid=engine.begin_live_attempt('КИТ',*self.route,'w100');engine.finish_live_attempt('КИТ',*self.route,aid,rows=0,error='timeout')
        live=engine._live_pack(*self.route);live['companies']['КИТ']['last_attempt_at']=(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat();engine._write_live(*self.route,live)
        with patch.object(main,'threading'):
            r=self.client.post('/api/collect',json={'origin':self.route[0],'destination':self.route[1],'companies':['КИТ'],'force':False}).json()
        self.assertEqual(r['status'],'queued')

    def test_settings_can_enable_and_disable_browser_mode(self):
        config=Path(self.tmp.name)/'settings.json'
        with patch.object(main,'SETTINGS_PATH',config),patch('app.legacy_backend.SETTINGS_PATH',config):
            for enabled in [True,False]:
                self.assertEqual(self.client.post('/api/settings',json={'public_browser_enabled':enabled}).status_code,200)
                self.assertEqual(col._browser_enabled(),enabled)
                self.assertEqual(self.client.get('/api/settings').json()['public_browser_enabled'],enabled)
            self.assertEqual(self.client.post('/api/settings',json={'public_browser_enabled':'false'}).status_code,400)

    def test_downloadable_diagnostics_uses_selected_weight_and_redacts_key(self):
        secret='private-test-key-42'
        aid=engine.begin_live_attempt('ДЛ',*self.route,'w200');engine.finish_live_attempt('ДЛ',*self.route,aid,rows=0,error='HTTP 401 '+secret)
        with patch.object(main,'_settings',return_value={'dellin_appkey':secret}):
            r=self.client.get('/api/diagnostics/download',params={'origin':self.route[0],'destination':self.route[1],'profile':'w200'})
        self.assertEqual(r.status_code,200);self.assertNotIn(secret,r.text)
        self.assertIn('attachment',r.headers['content-disposition']);self.assertEqual(r.json()['profile'],'w200')
        self.assertIn('environment',r.json());self.assertEqual(r.json()['version'],'53.0')


if __name__=='__main__':unittest.main()
