"""Route-scoped OCR, strict matching, caching and stalled-worker regressions."""
import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app import scan_ocr as o,tariff_documents as t,ocr_cache,v42_engine as e
from app.scan_ocr_worker import route_bands

class RouteScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=(Path(__file__).parent/'fixtures/dellin_scan_two_rows.pdf').read_bytes()
        with tempfile.TemporaryDirectory() as directory,patch.object(e,'RUNTIME_DIR',Path(directory)):
            cls.result=o.prepare(cls.raw,'ДЛ',origin='Санкт-Петербург',destination='Абакан')
    def test_actual_raster_reads_only_selected_row(self):
        r=self.result
        self.assertEqual([v['destination'] for v in r['rows']],['Абакан'])
        self.assertEqual(r['ocr_scope'],'route');self.assertEqual(r['ocr_pages'],[1])
        self.assertEqual(r['document_date'],'2026-08-03');self.assertEqual(r['tax_basis'],'С НДС (по источнику)')
        values,_=o.parse(r,'Санкт-Петербург','Абакан')
        self.assertEqual(values['w001']['price'],680);self.assertEqual(values['min']['price'],1520)
        self.assertEqual(values['w100']['price'],4360);self.assertNotIn('w20000',values)
    def test_route_cache_never_masks_full_index_or_another_route(self):
        with t.parsing_session():
            cache=t._SESSION.get();cache[('ocr',self.raw,'ДЛ','Санкт-Петербург','Абакан')]=self.result
            with patch.object(ocr_cache,'get',return_value=None),patch.object(o.subprocess,'Popen',side_effect=RuntimeError('worker requested')):
                self.assertIs(o.prepare(self.raw,'ДЛ','Санкт-Петербург','Абакан'),self.result)
                with self.assertRaisesRegex(RuntimeError,'worker requested'):o.prepare(self.raw,'ДЛ')
                with self.assertRaisesRegex(RuntimeError,'worker requested'):o.prepare(self.raw,'ДЛ','Санкт-Петербург','Альметьевск')
    def test_exact_city_and_wrapped_name_not_substrings(self):
        names=[[0,10,30,15,'Пермь'],[0,20,40,25,'Переславль-'],[0,26,40,31,'Залесский'],[0,36,30,41,'Петрозаводск']]
        self.assertEqual(route_bands(names,'Переславль-Залесский',5,45),[(17.5,33.5)])
        self.assertEqual(route_bands(names,'Москва',5,45),[])
        names=[[0,10,30,15,'Москва'],[0,20,30,25,'Москва']]
        self.assertEqual(len(route_bands(names,'Москва',5,45)),2)
        self.assertEqual(route_bands([[0,10,30,15,'Московский']],'Москва',5,45),[])
    def test_stage_watchdog_kills_worker_even_without_progress_listener(self):
        class Hung:
            returncode=None;killed=False;waited=False
            def poll(self):return None
            def kill(self):self.killed=True
            def wait(self,timeout):self.waited=True
        process=Hung()
        def launch(args,**kwargs):
            Path(args[5]).write_text(json.dumps({'done':1,'total':9,'message':'Чтение чисел','sequence':1}))
            return process
        context=o._PROGRESS.set(None)
        try:
            with patch.object(o.subprocess,'Popen',side_effect=launch),patch.object(o,'STAGE_TIMEOUT',.05):
                start=time.monotonic()
                with self.assertRaisesRegex(ValueError,'Чтение чисел'):o.prepare(b'isolated watchdog fixture','ДЛ')
                self.assertLess(time.monotonic()-start,2)
            self.assertTrue(process.killed);self.assertTrue(process.waited)
        finally:o._PROGRESS.reset(context)

if __name__=='__main__':unittest.main()
