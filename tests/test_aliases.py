from pathlib import Path
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest

import test_import as fixtures
import koreader_sync as server
import sync_storage


class AliasTests(unittest.TestCase):
    setUp = fixtures.ImportTests.setUp
    tearDown = fixtures.ImportTests.tearDown
    book = fixtures.ImportTests.book
    run_import = fixtures.ImportTests.run_import
    rows = fixtures.ImportTests.rows
    write_progress = fixtures.ImportTests.write_progress
    def test_linked_keys_track_api_uploads_in_both_directions(self):
        self.book()
        self.run_import(apply=True)
        server.USERSDB = str(self.database)
        client = server.app.test_client()
        headers = {'x-auth-user': 'reader', 'x-auth-key': 'test-key'}
        keys = ['a' * 32, hashlib.md5(b'Book.epub').hexdigest()]
        for document, position, percentage in [(keys[0], 'from-tablet', .7), (keys[1], 'from-phone', .2)]:
            self.assertEqual(client.put('/syncs/progress', headers=headers, json=dict(
                document=document, progress=position, percentage=percentage,
                device='test', device_id='test')).status_code, 200)
            for key in keys:
                row = client.get('/syncs/progress/' + key, headers=headers).json
                self.assertEqual(row['document'], key)
                self.assertEqual(row['progress'], position)
                self.assertEqual(row['percentage'], percentage)
        with sync_storage.database(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM books').fetchone()[0], 1)

    def test_different_versions_same_filename_merge_at_furthest_position(self):
        self.book(folder='old', percentage=.8, position='ahead')
        self.book(folder='new', content_id='b' * 32, percentage=.2, position='behind', saved=self.saved + 1)
        code, output = self.run_import(apply=True)
        self.assertEqual(code, 0)
        self.assertNotIn('COLLISION', output)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(row['progress'] == 'ahead' for row in self.rows().values()))

    def test_mapping_scan_adds_hash_churn_without_replaying_progress(self):
        book, _ = self.book(percentage=.9, position='old-sidecar')
        self.run_import(apply=True)
        self.write_progress('a' * 32, int(time.time()), 'live-phone', .2)
        self.run_import(apply=True, mappings_only=True)
        first_hash = hashlib.md5(b'ebook').hexdigest()
        book.write_bytes(b'updated-web-serial')
        second_hash = hashlib.md5(b'updated-web-serial').hexdigest()
        self.run_import(apply=True, mappings_only=True)
        keys = {'a' * 32, hashlib.md5(b'Book.epub').hexdigest(), first_hash, second_hash}
        self.assertEqual({key[1] for key in self.rows()}, keys)
        self.assertTrue(all(row['progress'] == 'live-phone' for row in self.rows().values()))
        self.write_progress(second_hash, int(time.time()), 'new-edition', .6)
        self.assertTrue(all(row['progress'] == 'new-edition' for row in self.rows().values()))
        self.assertIn('Wrote 0 IDs', self.run_import(apply=True, mappings_only=True)[1])

    def test_renamed_file_adds_filename_alias_and_retains_old_keys(self):
        book, _ = self.book()
        self.run_import(apply=True, mappings_only=True)
        book.rename(book.with_name('New Name.epub'))
        self.run_import(apply=True, mappings_only=True)
        old_key = hashlib.md5(b'Book.epub').hexdigest()
        new_key = hashlib.md5(b'New Name.epub').hexdigest()
        self.write_progress(new_key, int(time.time()), 'renamed', .4)
        self.assertEqual(self.rows()[('reader', old_key)]['progress'], 'renamed')

    def test_cover_only_sidecar_still_registers_identity(self):
        _, sidecar = self.book()
        sidecar.write_text('return {doc_pages=5658, last_xpointer="/body/DocFragment[1]/body/div/img.0", '
                           'partial_md5_checksum="' + 'a' * 32 + '", summary={status="reading"}}')
        os.utime(sidecar, (self.saved, self.saved))
        code, output = self.run_import(apply=True)
        self.assertEqual(code, 0)
        self.assertIn('no percentage saved', output)
        self.assertNotIn('SKIP:', output)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row['progress'] is None for row in self.rows().values()))
        server.USERSDB = str(self.database)
        self.assertEqual(server.getPosition('reader', 'a' * 32), {})

    def test_finished_label_does_not_invent_percentage_or_location(self):
        _, sidecar = self.book()
        sidecar.write_text('return {partial_md5_checksum="' + 'a' * 32 + '", summary={status="complete"}}')
        os.utime(sidecar, (self.saved, self.saved))
        self.run_import(apply=True)
        self.assertTrue(all(row['percentage'] is None for row in self.rows().values()))

    def test_mapping_scan_discovers_books_without_any_sidecar(self):
        (self.root / 'Unread.epub').write_bytes(b'new book')
        before = self.database.read_bytes()
        self.assertEqual(self.run_import(mappings_only=True)[0], 0)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.base / 'import-backups').exists())
        self.assertEqual(self.run_import(apply=True, mappings_only=True)[0], 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row['progress'] is None for row in self.rows().values()))

    def test_linking_existing_separate_records_preserves_furthest(self):
        self.book(percentage=.1)
        name_key = hashlib.md5(b'Book.epub').hexdigest()
        self.write_progress('a' * 32, self.saved + 20, 'further', .8)
        self.write_progress(name_key, self.saved + 30, 'newer-but-behind', .3)
        code, _ = self.run_import(apply=True, mappings_only=True)
        self.assertEqual(code, 0)
        self.assertTrue(all(row['progress'] == 'further' for row in self.rows().values()))
        with sync_storage.database(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM books').fetchone()[0], 1)

    def test_unknown_api_key_stays_independent_until_mapped(self):
        self.book()
        name_key = hashlib.md5(b'Book.epub').hexdigest()
        self.write_progress('a' * 32, self.saved, 'tablet', .4)
        self.write_progress(name_key, self.saved, 'phone', .2)
        self.assertNotEqual(self.rows()[('reader', name_key)]['progress'],
                            self.rows()[('reader', 'a' * 32)]['progress'])
        self.run_import(apply=True, mappings_only=True)
        self.assertTrue(all(row['progress'] == 'tablet' for row in self.rows().values()))

    def test_mapping_scan_rejects_force(self):
        with self.assertRaises(ValueError):
            self.run_import(apply=True, mappings_only=True, force=True)

    def test_initial_mapping_scan_waits_for_account_registration(self):
        with sync_storage.database(self.database) as db:
            db.execute('DELETE FROM users')
        code, output = self.run_import(apply=True, mappings_only=True)
        self.assertEqual(code, 0)
        self.assertIn('No sync accounts yet', output)


class MigrationTests(unittest.TestCase):
    def test_legacy_accounts_and_progress_are_preserved_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sync.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript(fixtures.SCHEMA)
                db.execute("INSERT INTO progress VALUES ('reader', 'old-key', '/body/p[42]', .7, 'Tablet', 't', 100)")
            sync_storage.init_db(path)
            with sync_storage.database(path) as db:
                before = sync_storage.snapshot(db, 'reader')
                self.assertEqual(before['old-key']['progress'], '/body/p[42]')
                self.assertEqual(db.execute('SELECT userkey FROM users').fetchone()[0], 'test-key')
                self.assertEqual(db.execute('SELECT count(*) FROM progress_legacy').fetchone()[0], 1)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            sync_storage.init_db(path)
            with sync_storage.database(path) as db:
                self.assertEqual(sync_storage.snapshot(db, 'reader'), before)

    def test_bad_legacy_schema_rolls_back_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sync.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE progress (unexpected TEXT); INSERT INTO progress VALUES ('keep');")
            with self.assertRaises((IndexError, sqlite3.Error)):
                sync_storage.init_db(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT unexpected FROM progress').fetchone()[0], 'keep')
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='books'").fetchone())
