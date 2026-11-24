from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import koreader_sync as server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / 'sync.sqlite3'
        self.old_database = server.USERSDB
        server.USERSDB = str(self.database)
        server.initDb()
        self.client = server.app.test_client()
        self.headers = {'x-auth-user': 'reader', 'x-auth-key': 'test-key'}
        self.client.post('/users/create', json={'username': 'reader', 'password': 'test-key'})

    def tearDown(self):
        server.USERSDB = self.old_database
        self.temporary.cleanup()

    def test_authentication_and_health(self):
        self.assertEqual(self.client.get('/healthcheck').json, {'state': 'OK'})
        self.assertEqual(self.client.get('/users/auth').status_code, 401)
        self.assertEqual(self.client.get('/users/auth', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/users/auth', headers={**self.headers, 'x-auth-key': 'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/users/create', json={'username': 'reader', 'password': 'test-key'}).status_code, 409)
        self.assertEqual(self.client.get('/syncs/progress/book').status_code, 401)
        self.assertEqual(self.client.put('/syncs/progress', json={}).status_code, 401)

    def test_registration_and_progress_are_isolated_per_user(self):
        self.assertEqual(self.client.post('/users/create', json={'username': 'other', 'password': 'other-key'}).status_code, 201)
        position = {'document': 'book', 'progress': '/body/p[3]', 'percentage': .2,
                    'device': 'Tablet', 'device_id': 'tablet'}
        self.assertEqual(self.client.put('/syncs/progress', headers=self.headers, json=position).status_code, 200)
        restored = self.client.get('/syncs/progress/book', headers=self.headers).json
        for key, value in position.items():
            self.assertEqual(restored[key], value)
        self.assertIsInstance(restored['timestamp'], int)
        other = {'x-auth-user': 'other', 'x-auth-key': 'other-key'}
        self.assertEqual(self.client.get('/syncs/progress/book', headers=other).json, {})

    def test_reinitialization_preserves_existing_installation(self):
        server.updatePosition('reader', 'book', {'progress': '/body/p[3]', 'percentage': .2})
        before = server.getPosition('reader', 'book')
        server.initDb()
        self.assertEqual(server.getPosition('reader', 'book'), before)
        self.assertEqual(server.getUser('reader')['userkey'], 'test-key')

    def test_concurrent_api_writes_and_database_integrity(self):
        def write(number):
            with server.app.test_client() as client:
                position = {'document': str(number), 'progress': str(number), 'percentage': number / 100}
                return client.put('/syncs/progress', headers=self.headers, json=position).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(write, range(30))), [200] * 30)
        for number in range(30):
            self.assertEqual(server.getPosition('reader', str(number))['progress'], str(number))
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_existing_json_is_not_overwritten(self):
        original = b'{"reader": {"userkey": "test-key"}}'
        self.database.write_bytes(original)
        with self.assertRaises(sqlite3.DatabaseError):
            server.initDb()
        self.assertEqual(self.database.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
