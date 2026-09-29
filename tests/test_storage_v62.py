import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import runtime_paths as paths, storage, v42_engine as e, document_imports as documents
from app import manual_prices as manual
from restore_data import restore


class Storage(unittest.TestCase):
    def test_render_path_alone_is_not_a_persistent_disk(self):
        with patch.dict(os.environ,{'RENDER':'true','TARIFF_DATA_DIR':'/var/data/prices'},clear=True),patch.object(paths.os.path,'ismount',return_value=False):
            self.assertEqual(paths.runtime_directory('/app'),Path('/var/data/prices'))
            self.assertTrue(paths.persistence_info('/var/data/prices')['needs_attention'])
        with patch.dict(os.environ,{'RENDER':'true'},clear=True),patch.object(paths.os.path,'ismount',side_effect=lambda p:str(p)=='/var/data'):
            self.assertEqual(paths.runtime_directory('/app'),Path('/var/data/tariff-app'))
            info=paths.persistence_info('/var/data/tariff-app')
            self.assertFalse(info['needs_attention']);self.assertEqual(info['mount_path'],'/var/data')
        with patch.dict(os.environ,{'RENDER':'true'},clear=True),patch.object(paths.os.path,'ismount',return_value=False):
            self.assertEqual(paths.runtime_directory('/app'),Path('/app/runtime'))

    def test_complete_backup_restore_and_new_process_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);runtime=root/'runtime';route=('Казань','Уфа')
            with patch.object(e,'RUNTIME_DIR',runtime):
                preview=documents.preview('Компания;Откуда;Куда;Вес, кг;Цена, руб\nДЛ;Казань;Уфа;1;650\n'.encode(),'monthly.csv','ДЛ',*route)
                documents.commit(preview['token'])
                manual.put('Werner',*route,'w100',15,'rub_per_kg')
                attempt=e.begin_live_attempt('ПЭК',*route)
                e.save_live_update('ПЭК',*route,{'w100':{'kind':'exact','price':1600,'rate_per_kg':16}},{},attempt)
                e.finish_live_attempt('ПЭК',*route,attempt,rows=1)
                (runtime/'settings.json').write_text('{"TEST_SECRET":"not for backup"}')
                backup=storage.backup()
                with zipfile.ZipFile(backup) as archive:
                    manifest=json.loads(archive.read('manifest.json'))
                    self.assertEqual(manifest['schema'],2)
                    self.assertNotIn('settings.json',archive.namelist())
                    self.assertIn('imports/documents.sqlite3',archive.namelist())
                    self.assertTrue(any(name.startswith('routes/') for name in archive.namelist()))
                    for name,sha in manifest['sha256'].items():self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),sha)
                destination=root/'restored'
                result=restore(backup,destination);self.assertGreater(result['files'],4)
                with self.assertRaisesRegex(ValueError,'пустой'):restore(backup,destination)
                code="from app import v42_engine as e; import json; print(json.dumps([e.quote(c,'Казань','Уфа',p)['price'] for c,p in [('ДЛ','w001'),('Werner','w100'),('ПЭК','w100')]]))"
                process=subprocess.run([sys.executable,'-c',code],env={**os.environ,'TARIFF_DATA_DIR':str(destination)},capture_output=True,text=True,check=True)
                self.assertEqual(json.loads(process.stdout),[650,1500,1600])
                self.assertTrue(list((destination/'imports/files').glob('*.csv')))

    def test_corrupted_or_unsafe_archives_do_not_create_target(self):
        cases=[('../escape.json',b'{}',True),('routes/one.json',b'changed',False)]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for number,(name,data,correct_hash) in enumerate(cases):
                archive=root/f'bad{number}.zip';target=root/f'restore{number}'
                with zipfile.ZipFile(archive,'w') as out:
                    out.writestr(name,data)
                    out.writestr('manifest.json',json.dumps({'schema':2,'type':'tariff_data_backup','sha256':{name:hashlib.sha256(data if correct_hash else b'original').hexdigest()}}))
                with self.assertRaises(ValueError):restore(archive,target)
                self.assertFalse(target.exists())

    def test_older_document_backup_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);archive=root/'old.zip'
            with zipfile.ZipFile(archive,'w') as out:
                out.writestr('imports/files/old.csv','original')
                out.writestr('manifest.json',json.dumps({'schema':1,'type':'user_documents_backup'}))
            restore(archive,root/'restored')
            self.assertEqual((root/'restored/imports/files/old.csv').read_text(),'original')


if __name__=='__main__':unittest.main()
