import tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from app import v42_engine as e,v42_collectors as c,online_tariffs as online

class StopCollection(unittest.TestCase):
 def test_stopping_starts_no_new_companies_and_keeps_completed_prices(self):
  stop=threading.Event();gate=threading.Event();started=[];outputs=[];lock=threading.Lock();seven=threading.Event()
  companies=[x for x in e.COMPANIES if x not in {'ПЭК','КИТ'}]
  def fetch(company,*a):
   with lock:
    started.append(company)
    if len(started)==7:seven.set()
   self.assertTrue(gate.wait(10))
   return {'w001':{'kind':'exact','price':719}},{'source_url':'https://example.invalid/FIXTURE'}
  with tempfile.TemporaryDirectory() as directory,patch.object(e,'RUNTIME_DIR',Path(directory)),patch.object(e,'ROUTE_CONFIG',{}),patch.object(online,'ADAPTERS',dict.fromkeys(companies)),patch.object(online,'collect',side_effect=fetch):
   worker=threading.Thread(target=lambda:outputs.extend(c.collect_selected(companies,'Москва','Санкт-Петербург',on_progress=lambda r:None,should_stop=stop.is_set)))
   worker.start()
   try:
    self.assertTrue(seven.wait(5));stop.set();gate.set();worker.join(15);self.assertFalse(worker.is_alive())
    self.assertEqual(len(started),7);self.assertEqual(len(outputs),7);self.assertTrue(all(x['ok'] for x in outputs))
    self.assertEqual(e.quote(started[0],'Москва','Санкт-Петербург','w001')['price'],719)
    for company in set(companies)-set(started):self.assertEqual(e.live_company_state('Москва','Санкт-Петербург',company),{})
   finally:gate.set();worker.join(15)
if __name__=='__main__':unittest.main()
