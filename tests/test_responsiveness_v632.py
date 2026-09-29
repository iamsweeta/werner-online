import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from app import v42_main as main,v42_engine as e


class Responsiveness(unittest.TestCase):
    def test_slow_progress_checkpoint_does_not_block_status(self):
        route=('Москва','Санкт-Петербург');key='|'.join(route)
        entered=threading.Event();release=threading.Event()
        job={'job_id':'controlled-test','status':'queued','origin':route[0],'destination':route[1],'profile':'w100','requested_companies':['Werner']}
        def write(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('test wait expired')
        def collector(*args,on_progress,**kwargs):
            row={'company':'Werner','ok':True,'rows':1,'message':'saved'}
            on_progress(row)
            return [row]
        with patch.dict(main.COLLECT_JOBS,{key:job},clear=True),patch.object(main,'collect_selected',collector),patch.object(e,'_robust_json_write',write),ThreadPoolExecutor(max_workers=2) as pool:
            worker=pool.submit(main._run_collect,key,['Werner'],*route)
            try:
                self.assertTrue(entered.wait(2))
                status=pool.submit(main.collect_status,*route).result(timeout=1)
                self.assertEqual(status['completed_companies'],1)
                self.assertEqual(status['status'],'running')
                with patch.object(main.BULK,'active',side_effect=AssertionError('progress must not query bulk DB')):
                    self.assertEqual(pool.submit(main.active_collect).result(timeout=1)['job_id'],'controlled-test')
            finally:release.set()
            worker.result(timeout=2)

    def test_comparison_and_matrix_share_one_route_snapshot(self):
        packs=({'profiles':{},'companies':{},'_manual':{'profiles':{}}},{'profiles':{},'companies':{}},{'profiles':{},'companies':{}})
        with patch.object(main,'route_packs',return_value=packs) as load:
            result=main.route_view('Москва','Санкт-Петербург')
        load.assert_called_once()
        self.assertEqual(len(result['comparison']['items']),17)
        self.assertEqual(len(result['matrix']['profiles']),29)


if __name__=='__main__':unittest.main()
