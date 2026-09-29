"""No network: verify the local setup helper keeps secrets out of output."""
import contextlib,io,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import configure_neon as setup

class NeonSetup(unittest.TestCase):
    def test_personal_key_http400_opens_exact_project_and_finishes_setup(self):
        calls=[]
        responses=[(400,{'message':'org_id is required; do not print test-management-secret'}),
                   (200,{'project':{'id':'project-123','name':'werner-online'}}),
                   (200,{'branches':[{'id':'branch-prod','name':'production'}]}),
                   (200,{'s3_endpoint':'https://storage.example.invalid','region':'eu-central-1'}),
                   (200,{'name':'created'}),
                   (200,{'token_id':'test-storage-id','s3_secret_access_key':'test-storage-secret'})]
        class Session:
            headers={}
            def request(self,method,url,json=None,timeout=None):
                calls.append((method,url,json));status,data=responses.pop(0)
                return type('Response',(),{'ok':status==200,'status_code':status,'json':lambda self:data})()
            def close(self):pass
        database='postgresql://owner:test-db-secret@db.example.invalid/neondb?sslmode=require'
        out=io.StringIO()
        with tempfile.TemporaryDirectory() as folder,patch.object(setup,'__file__',str(Path(folder)/'configure_neon.py')),patch('requests.Session',return_value=Session()),patch('getpass.getpass',side_effect=[database,'test-management-secret']),patch('builtins.input',side_effect=['project-123','1']),contextlib.redirect_stdout(out):
            setup.main()
            from dotenv import dotenv_values
            values=dotenv_values(Path(folder)/'render.env')
            self.assertEqual(values['DATABASE_URL'],database)
            self.assertEqual(values['S3_ACCESS_KEY_ID'],'test-storage-id')
        self.assertEqual(calls[1][:2],('GET',setup.API+'/projects/project-123'))
        self.assertTrue(all('/projects/project-123/' in url for _,url,_ in calls[2:]))
        self.assertEqual(len([c for c in calls if c[0]=='POST']),2)
        for secret in ['test-management-secret','test-db-secret','test-storage-secret']:self.assertNotIn(secret,out.getvalue())

    def test_invalid_project_id_never_becomes_an_api_path(self):
        with patch('builtins.input',return_value='../other-project'),contextlib.redirect_stdout(io.StringIO()):
            with patch.object(setup,'choose') as choose:
                calls=[]
                def api(method,path):
                    calls.append(path);raise setup.NeonAPIError(400,method,path)
                with self.assertRaisesRegex(ValueError,'Некорректный Project ID'):setup.select_project(api)
                self.assertEqual(calls,['/projects']);choose.assert_not_called()

    def test_unauthorized_key_reports_stage_without_asking_for_project(self):
        with patch('builtins.input') as prompt:
            def api(method,path):raise setup.NeonAPIError(401,method,path)
            with self.assertRaisesRegex(setup.NeonAPIError,r'HTTP 401.*GET /projects'):setup.select_project(api)
            prompt.assert_not_called()

    def test_exact_project_lookup_still_enforces_access(self):
        calls=[]
        def api(method,path):
            calls.append(path);raise setup.NeonAPIError(403,method,path)
        with patch('builtins.input',return_value='project-123'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(setup.NeonAPIError,r'GET /projects/project-123'):setup.select_project(api)
        self.assertEqual(calls,['/projects','/projects/project-123'])

    def test_private_bucket_and_complete_environment_without_secret_output(self):
        calls=[]
        responses=[{'projects':[{'id':'project1','name':'Tariffs'}]},
                   {'branches':[{'id':'branch1','name':'main'}]},
                   {'s3_endpoint':'https://storage.example.invalid','region':'eu-central-1'},
                   {'name':'created'}, {'token_id':'test-storage-id','s3_secret_access_key':'test-storage-secret'}]
        class Session:
            headers={}
            def request(self,method,url,json=None,timeout=None):
                calls.append((method,url,json));data=responses.pop(0)
                return type('Response',(),{'ok':True,'json':lambda self:data})()
            def close(self):pass
        database='postgresql://owner:test-db-secret@db.example.invalid/neondb?sslmode=require'
        output=io.StringIO()
        with tempfile.TemporaryDirectory() as folder,patch.object(setup,'__file__',str(Path(folder)/'configure_neon.py')),patch('requests.Session',return_value=Session()),patch('getpass.getpass',side_effect=[database,'test-management-secret']),patch('builtins.input',side_effect=['1','1']),contextlib.redirect_stdout(output):
            setup.main()
            from dotenv import dotenv_values
            values=dotenv_values(Path(folder)/'render.env')
            self.assertEqual(values['DATABASE_URL'],database)
            self.assertEqual(values['TARIFF_STORAGE'],'cloud')
            self.assertEqual(values['S3_ACCESS_KEY_ID'],'test-storage-id')
            self.assertEqual(values['S3_SECRET_ACCESS_KEY'],'test-storage-secret')
            self.assertEqual(values['S3_REGION'],'eu-central-1')
            self.assertGreaterEqual(len(values['APP_PASSWORD']),12)
            self.assertNotIn('test-management-secret',(Path(folder)/'render.env').read_text())
            if os.name!='nt':self.assertEqual((Path(folder)/'render.env').stat().st_mode&0o777,0o600)
        self.assertEqual(calls[3][2]['access_level'],'private')
        self.assertEqual(calls[4][2],{'scopes':['storage:read','storage:write'],'principal_type':'user'})
        for secret in ['test-db-secret','test-storage-secret','test-management-secret',values['APP_PASSWORD']]:
            self.assertNotIn(secret,output.getvalue())

    def test_rejects_non_tls_database_before_creating_resources(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(setup,'__file__',str(Path(folder)/'configure_neon.py')),patch('requests.Session') as session,patch('getpass.getpass',return_value='postgresql://u:p@db.invalid/name?sslmode=disable'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):setup.main()
            session.assert_not_called()

    def test_existing_secrets_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'render.env';target.write_text('original')
            with patch.object(setup,'__file__',str(Path(folder)/'configure_neon.py')),patch('requests.Session') as session:
                with self.assertRaises(ValueError):setup.main()
                session.assert_not_called();self.assertEqual(target.read_text(),'original')

if __name__=='__main__':unittest.main()
