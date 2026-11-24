from collections import Counter
import hashlib
import os
import time
import unittest
from unittest.mock import patch

import test_import as fixtures
import import_sidecars as imp
import sync_storage


class SidecarSyncTests(unittest.TestCase):
    setUp = fixtures.ImportTests.setUp
    tearDown = fixtures.ImportTests.tearDown
    book = fixtures.ImportTests.book
    run_import = fixtures.ImportTests.run_import
    rows = fixtures.ImportTests.rows
    write_progress = fixtures.ImportTests.write_progress

    def server_position(self, position='/body/DocFragment[3]/p[42]', percentage=.75):
        timestamp = int(time.time()) - 10
        self.write_progress('a' * 32, timestamp, position, percentage)
        return timestamp

    def test_preview_then_pull_with_backup_mtime_and_no_bounce(self):
        _, sidecar = self.book()
        original = sidecar.read_bytes()
        timestamp = self.server_position()
        before = self.database.read_bytes()
        code, output = self.run_import(pull=True)
        self.assertEqual(code, 0)
        self.assertIn('WOULD PULL', output)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertFalse((self.base / 'sidecar-backups').exists())
        self.assertEqual(self.run_import(apply=True, pull=True)[0], 0)
        fields, info = imp.read_sidecar(sidecar)
        self.assertEqual(fields[b'last_xpointer'], b'/body/DocFragment[3]/p[42]')
        self.assertEqual(fields[b'percent_finished'], .75)
        self.assertEqual(info.st_mtime_ns, timestamp * 1_000_000_000)
        backups = list((self.base / 'sidecar-backups').rglob('*.lua'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        rows, updated = self.rows(), sidecar.read_bytes()
        self.assertIn('Wrote 0 IDs', self.run_import(apply=True, pull=True)[1])
        self.assertEqual(self.rows(), rows)
        self.assertEqual(sidecar.read_bytes(), updated)
        self.assertEqual(len(list((self.base / 'sidecar-backups').rglob('*.lua'))), 1)

    def test_cover_without_percentage_preserves_unrelated_metadata(self):
        _, sidecar = self.book()
        annotation = b'    ["annotations"] = {[1]={text="a note", pos="/body/p[5]"}}, -- keep this exactly\n'
        original = (b'-- custom header\nreturn {\n' + annotation +
                    b'    ["last_xpointer"] = "/body/DocFragment[1]/body/div/img.0",\n' +
                    b'    ["partial_md5_checksum"] = "' + b'a' * 32 + b'",\n' +
                    b'    ["summary"] = {status="reading"},\n}\n-- trailing comment\n')
        sidecar.write_bytes(original)
        sidecar.chmod(0o640)
        os.utime(sidecar, (self.saved, self.saved))
        self.server_position()
        self.assertEqual(self.run_import(apply=True, pull=True)[0], 0)
        updated = sidecar.read_bytes()
        self.assertIn(annotation, updated)
        self.assertIn(b'["summary"] = {status="reading"}', updated)
        self.assertTrue(updated.startswith(b'-- custom header\n'))
        self.assertTrue(updated.endswith(b'-- trailing comment\n'))
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o640)
        self.assertEqual(imp.LuaData(updated).parse()[b'percent_finished'], .75)

    def test_newer_server_can_pull_a_lower_percentage(self):
        _, sidecar = self.book(percentage=.9)
        self.server_position('/body/p[2]', .1)
        self.run_import(apply=True, pull=True)
        fields, _ = imp.read_sidecar(sidecar)
        self.assertEqual(fields[b'percent_finished'], .1)
        self.assertEqual(fields[b'last_xpointer'], b'/body/p[2]')

    def test_newer_sidecar_pushes_without_rewriting_it(self):
        _, sidecar = self.book(position='/body/p[8]', percentage=.8)
        original = sidecar.read_bytes()
        self.write_progress('a' * 32, self.saved - 10, '/body/p[2]', .2)
        self.run_import(apply=True, pull=True)
        self.assertTrue(all(row['progress'] == '/body/p[8]' for row in self.rows().values()))
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertFalse((self.base / 'sidecar-backups').exists())

    def test_mapping_scan_only_pulls_and_never_pushes_sidecar_progress(self):
        _, sidecar = self.book(position='/body/p[9]', percentage=.9)
        self.server_position('/body/p[2]', .2)
        self.assertEqual(self.run_import(apply=True, mappings_only=True, pull=True)[0], 0)
        self.assertTrue(all(row['progress'] == '/body/p[2]' for row in self.rows().values()))
        self.assertEqual(imp.read_sidecar(sidecar)[0][b'percent_finished'], .2)

    def test_pdf_page_is_written_as_numeric_lua_value(self):
        _, sidecar = self.book('Paper.pdf', position=2, percentage=.02)
        self.server_position('42', .42)
        self.run_import(apply=True, pull=True)
        self.assertEqual(imp.read_sidecar(sidecar)[0][b'last_page'], 42)

    def test_no_sidecar_is_created_for_an_unopened_book(self):
        book = self.root / 'Unread.epub'
        book.write_bytes(b'new ebook')
        self.write_progress(hashlib.md5(b'new ebook').hexdigest(), int(time.time()), '/body/p[3]', .3)
        self.run_import(apply=True, mappings_only=True, pull=True)
        self.assertFalse(book.with_suffix('.sdr').exists())
        self.assertEqual(book.read_bytes(), b'new ebook')

    def test_changed_sidecar_is_not_overwritten(self):
        _, sidecar = self.book()
        self.server_position()
        books, _, _ = imp.scan(self.root, lambda _: None, int(time.time()))
        external = sidecar.read_bytes() + b'\n-- concurrently changed\n'
        sidecar.write_bytes(external)
        with sync_storage.database(self.database) as db:
            positions = sync_storage.snapshot(db, 'reader')
        counts = Counter()
        imp.sync_sidecars(books, positions, self.database, True, lambda _: None, counts)
        self.assertEqual(counts['errors'], 1)
        self.assertEqual(sidecar.read_bytes(), external)

    def test_unsupported_server_pointer_does_not_corrupt_sidecar(self):
        _, sidecar = self.book()
        original = sidecar.read_bytes()
        self.server_position('epubcfi(/6/8)', .5)
        code, output = self.run_import(apply=True, pull=True)
        self.assertEqual(code, 1)
        self.assertIn('not a KOReader XPointer', output)
        self.assertEqual(sidecar.read_bytes(), original)

    def test_atomic_replace_failure_leaves_original_and_backup(self):
        _, sidecar = self.book()
        original = sidecar.read_bytes()
        self.server_position()
        with patch.object(imp.os, 'replace', side_effect=OSError('simulated write failure')):
            self.assertEqual(self.run_import(apply=True, pull=True)[0], 1)
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertEqual(next((self.base / 'sidecar-backups').rglob('*.lua')).read_bytes(), original)
        self.assertEqual(list(sidecar.parent.glob('.kosync-*.tmp')), [])

    def test_lua_escaping_and_missing_fields(self):
        position = '/body/p["café\\quoted"]'
        original = b'return { ["note"] = "retain", }'
        updated = imp.patch_sidecar(original, 'Book.epub', dict(progress=position, percentage=.4))
        parsed = imp.LuaData(updated).parse()
        self.assertEqual(parsed[b'last_xpointer'].decode(), position)
        self.assertEqual(parsed[b'percent_finished'], .4)
        self.assertIn(b'["note"] = "retain"', updated)
