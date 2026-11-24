import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_import as fixtures
import import_sidecars as imp


class CacheTests(unittest.TestCase):
    setUp = fixtures.ImportTests.setUp
    tearDown = fixtures.ImportTests.tearDown
    book = fixtures.ImportTests.book
    run_import = fixtures.ImportTests.run_import
    rows = fixtures.ImportTests.rows
    write_progress = fixtures.ImportTests.write_progress

    @property
    def cache_path(self):
        return self.base / 'scan-cache.sqlite3'

    def warm(self):
        self.assertEqual(self.run_import(apply=True, mappings_only=True, pull=True)[0], 0)

    def test_unchanged_scan_reopens_neither_sidecars_nor_books_and_does_not_write_cache(self):
        self.book()
        self.book('Second.epub', content_id='b' * 32, content=b'second book')
        self.warm()
        cache_bytes = self.cache_path.read_bytes()
        cache_stamp = imp.signature(self.cache_path.stat())
        with patch.object(imp, 'read_sidecar', side_effect=AssertionError('unchanged sidecar read')), \
                patch.object(imp, 'partial_md5', side_effect=AssertionError('unchanged book hashed')):
            code, output = self.run_import(apply=True, mappings_only=True, pull=True)
        self.assertEqual(code, 0)
        self.assertIn('2 sidecars reused, 0 read; 2 book hashes reused, 0 computed', output)
        self.assertEqual(self.cache_path.read_bytes(), cache_bytes)
        self.assertEqual(imp.signature(self.cache_path.stat()), cache_stamp)
        self.assertEqual(self.cache_path.stat().st_mode & 0o777, 0o600)

    def test_cache_survives_a_fresh_process(self):
        self.book()
        self.warm()
        result = subprocess.run([sys.executable, str(Path(imp.__file__)), str(self.root),
                                 '--database', str(self.database), '--mappings-only', '--pull', '--apply'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('1 sidecars reused, 0 read; 1 book hashes reused, 0 computed', result.stdout)

    def test_only_modified_epub_is_rehashed_and_unchanged_sidecars_are_not_read(self):
        book, _ = self.book()
        self.book('Second.epub', content_id='b' * 32, content=b'second')
        self.warm()
        book.write_bytes(b'new chapters')
        # Replacements can carry an older source mtime: compare for inequality,
        # not only timestamps newer than the last run.
        os.utime(book, (self.saved - 100, self.saved - 100))
        with patch.object(imp, 'read_sidecar', side_effect=AssertionError('unchanged sidecar read')), \
                patch.object(imp, 'partial_md5', wraps=imp.partial_md5) as hashes:
            self.warm()
        self.assertEqual(hashes.call_args_list, [unittest.mock.call(book)])
        self.assertIn(('reader', hashlib.md5(b'new chapters').hexdigest()), self.rows())

    def test_atomic_replacement_with_same_mtime_and_size_invalidates_hash(self):
        book, _ = self.book()
        self.warm()
        original = book.stat()
        replacement = book.with_name('incoming.tmp')
        replacement.write_bytes(b'other')  # Same length as the original b'ebook'.
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, book)
        self.warm()
        self.assertIn(('reader', hashlib.md5(b'other').hexdigest()), self.rows())

    def test_only_modified_sidecar_is_read_and_does_not_rehash_book(self):
        _, sidecar = self.book()
        self.book('Second.epub', content_id='b' * 32, content=b'second')
        self.warm()
        sidecar.write_bytes(sidecar.read_bytes().replace(b'percent_finished=0.31', b'percent_finished=0.75'))
        with patch.object(imp, 'partial_md5', side_effect=AssertionError('unchanged book hashed')), \
                patch.object(imp, 'read_sidecar', wraps=imp.read_sidecar) as reads:
            self.warm()
        self.assertEqual(reads.call_args_list, [unittest.mock.call(sidecar)])
        # Scheduled mapping scans never upload that newly parsed percentage.
        self.assertTrue(all(row['progress'] is None for row in self.rows().values()))

    def test_new_server_progress_still_pulls_when_all_input_files_are_cached(self):
        _, sidecar = self.book()
        self.warm()
        self.write_progress('a' * 32, int(time.time()) - 1, '/body/p[99]', .9)
        with patch.object(imp, 'partial_md5', side_effect=AssertionError('unchanged book hashed')), \
                patch.object(imp, 'read_sidecar', wraps=imp.read_sidecar) as reads:
            self.warm()
        self.assertEqual(reads.call_args_list, [unittest.mock.call(sidecar, raw=True)])
        self.assertEqual(imp.read_sidecar(sidecar)[0][b'percent_finished'], .9)
        self.assertEqual(len(list((self.base / 'sidecar-backups').rglob('*.lua'))), 1)
        self.warm()  # The pull changed this file; refresh its cached parse once.
        with patch.object(imp, 'read_sidecar', side_effect=AssertionError('sidecar read again')):
            self.warm()

    def test_identical_server_position_with_newer_timestamp_does_not_reopen_sidecar(self):
        self.book()
        self.warm()
        self.write_progress('a' * 32, int(time.time()) - 1, '/body/p[12]/text().4', .31)
        with patch.object(imp, 'read_sidecar', side_effect=AssertionError('identical position read')):
            self.warm()
        self.assertFalse((self.base / 'sidecar-backups').exists())

    def test_new_and_deleted_files_are_discovered_without_directory_mtime_shortcut(self):
        book, sidecar = self.book()
        self.warm()
        old_directory = book.parent.stat()
        book.unlink()
        sidecar.unlink()
        sidecar.parent.rmdir()
        self.book('Added.epub', content_id='c' * 32, content=b'added')
        os.utime(book.parent, ns=(old_directory.st_atime_ns, old_directory.st_mtime_ns))
        self.warm()
        self.assertIn(('reader', hashlib.md5(b'Added.epub').hexdigest()), self.rows())
        # Removed files' registered aliases remain useful to old devices.
        self.assertIn(('reader', 'a' * 32), self.rows())

    def test_preview_neither_creates_nor_changes_cache(self):
        book, _ = self.book()
        self.run_import(mappings_only=True)
        self.assertFalse(self.cache_path.exists())
        self.warm()
        previous = self.cache_path.read_bytes()
        previous_stamp = imp.signature(self.cache_path.stat())
        book.write_bytes(b'changed')
        self.run_import(mappings_only=True)
        self.assertEqual(self.cache_path.read_bytes(), previous)
        self.assertEqual(imp.signature(self.cache_path.stat()), previous_stamp)

    def test_no_cache_reads_sources_and_leaves_existing_cache_alone(self):
        self.book()
        self.warm()
        before = self.cache_path.read_bytes()
        with patch.object(imp, 'read_sidecar', wraps=imp.read_sidecar) as reads, \
                patch.object(imp, 'partial_md5', wraps=imp.partial_md5) as hashes:
            self.run_import(apply=True, mappings_only=True, no_cache=True)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(hashes.call_count, 1)
        self.assertEqual(self.cache_path.read_bytes(), before)

    def test_corrupt_cache_falls_back_without_preventing_progress_import(self):
        self.book()
        self.cache_path.write_bytes(b'not sqlite')
        code, output = self.run_import(apply=True)
        self.assertEqual(code, 0)
        self.assertIn('WARNING: scan cache', output)
        self.assertTrue(all(row['percentage'] == .31 for row in self.rows().values()))
        self.assertEqual(self.cache_path.read_bytes(), b'not sqlite')

    def test_invalid_cache_payload_is_rebuilt_from_source(self):
        _, sidecar = self.book()
        self.warm()
        with sqlite3.connect(self.cache_path) as db:
            db.execute('UPDATE cached_files SET payload=? WHERE kind=?', (json.dumps({'unknown': 'bad'}), 'sidecar'))
        with patch.object(imp, 'read_sidecar', wraps=imp.read_sidecar) as reads:
            self.warm()
        self.assertEqual(reads.call_args_list, [unittest.mock.call(sidecar)])

    def test_changed_file_during_cache_lookup_is_not_used_to_update_progress(self):
        _, sidecar = self.book()
        self.warm()
        previous = self.rows()
        original_get = imp.ScanCache.get

        def lookup(cache, path, kind, signature):
            result = original_get(cache, path, kind, signature)
            if path == sidecar and kind == 'sidecar':
                sidecar.write_bytes(sidecar.read_bytes() + b'\n-- changed during lookup\n')
            return result

        with patch.object(imp.ScanCache, 'get', lookup):
            code, output = self.run_import(apply=True)
        self.assertEqual(code, 1)
        self.assertIn('changed during scanning', output)
        self.assertEqual(self.rows(), previous)

    def test_progress_database_cannot_be_used_as_cache_even_via_hardlink(self):
        self.book()
        os.link(self.database, self.cache_path)
        before = self.database.read_bytes()
        with self.assertRaisesRegex(imp.Invalid, 'must be separate'):
            self.run_import(apply=True)
        self.assertEqual(self.database.read_bytes(), before)

    def test_cached_path_replaced_with_symlink_is_rejected(self):
        book, sidecar = self.book()
        self.warm()
        external = self.base / 'outside.lua'
        sidecar.rename(external)
        sidecar.symlink_to(external)
        code, output = self.run_import(apply=True, mappings_only=True)
        self.assertEqual(code, 1)
        self.assertIn('not a regular file', output)
