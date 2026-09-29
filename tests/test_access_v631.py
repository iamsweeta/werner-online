"""Public access is explicit and independent of storage; catalogs survive DB errors."""
import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import access, cloud_db, v42_main as main


class AccessPolicy(unittest.TestCase):
    def test_public_mode_allows_cloud_without_password(self):
        with patch.dict(os.environ, {'APP_AUTH_MODE': 'public', 'APP_PASSWORD': ''}):
            access.validate(cloud=True)
            self.assertEqual(access.password(), '')

    def test_protected_cloud_cannot_silently_become_public(self):
        with patch.dict(os.environ, {'APP_AUTH_MODE': 'password', 'APP_PASSWORD': ''}):
            with self.assertRaises(RuntimeError):
                access.validate(cloud=True)
        with patch.dict(os.environ, {'APP_AUTH_MODE': 'publci'}):
            with self.assertRaises(RuntimeError):
                access.validate(cloud=False)

    def test_password_and_public_modes_cover_page_static_and_catalog(self):
        with patch.dict(os.environ, {'APP_AUTH_MODE': 'password', 'APP_PASSWORD': 'private-test-password', 'APP_USERNAME': 'manager'}):
            client = TestClient(main.app)
            for path in ('/', '/static/app.js', '/api/options?include_status=false'):
                response = client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()['code'], 'authentication_required')
                self.assertIn('Basic', response.headers['WWW-Authenticate'])
            self.assertEqual(client.get('/health').json()['access_mode'], 'password')
            self.assertEqual(client.get('/', auth=('manager', 'wrong')).status_code, 401)
            self.assertEqual(client.get('/', auth=('manager', 'private-test-password')).status_code, 200)
            # Existing APP_PASSWORD need not be deleted to enable public mode.
            with patch.dict(os.environ, {'APP_AUTH_MODE': 'public'}):
                for path in ('/', '/static/app.js', '/api/options?include_status=false'):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('WWW-Authenticate', response.headers)
                self.assertEqual(client.get('/health').json()['access_mode'], 'public')
                self.assertEqual(client.post('/api/storage/check', headers={'Origin': 'https://other.example'}).status_code, 403)

    def test_catalog_is_available_without_price_storage(self):
        with patch.dict(os.environ, {'APP_AUTH_MODE': 'public'}), patch.object(main, 'coverage', side_effect=cloud_db.StorageUnavailable('База недоступна')) as coverage:
            client = TestClient(main.app)
            for origin in ('Москва', 'Санкт-Петербург'):
                response = client.get('/api/options', params={'origin': origin, 'include_status': 'false'})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertNotIn(origin, data['destinations'])
                self.assertGreaterEqual(len(data['destinations']), 127)
                self.assertEqual(len(data['companies']), 17)
                self.assertEqual(len(data['profiles']), 29)
            coverage.assert_not_called()
            self.assertEqual(client.get('/api/options').status_code, 503)


if __name__ == '__main__':
    unittest.main()
