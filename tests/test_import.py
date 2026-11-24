import contextlib
import hashlib
import importlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_sidecars as imp
import sync_storage

SCHEMA = """
CREATE TABLE users (username TEXT PRIMARY KEY NOT NULL, userkey TEXT NOT NULL);
CREATE TABLE progress (
 username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
 document TEXT NOT NULL, progress TEXT, percentage REAL, device TEXT,
 device_id TEXT, timestamp INTEGER NOT NULL, PRIMARY KEY(username, document));
INSERT INTO users VALUES ('reader', 'test-key');
"""


def lua_string(value):
    # Decimal escapes also exercise Lua's byte semantics for UTF-8 characters.
    return '"' + ''.join('\\%03d' % byte for byte in value.encode()) + '"'


class ParserTests(unittest.TestCase):
    def test_nested_metadata_cannot_override_top_level(self):
        data = br'''-- /some/path/Book.sdr/metadata.epub.lua
        return {
          ["annotations"] = { [1] = { ["last_xpointer"] = "FAKE", ["text"] = "quotes: \"hi\"" } },
          ["last_xpointer"] = "/body/p[3]/text().4",
          ["percent_finished"] = 3.1e-1,
          ["doc_path"] = "/books/Caf\195\169.epub",
          ["extra"] = {true, false, nil; [0x10] = -2.5},
        } -- trailing comment
        '''
        parsed = imp.LuaData(data).parse()
        self.assertEqual(parsed[b"last_xpointer"], b"/body/p[3]/text().4")
        self.assertEqual(parsed[b"percent_finished"], .31)
        self.assertEqual(parsed[b"doc_path"].decode(), "/books/Café.epub")
        self.assertNotIn(b"annotations", parsed)

    def test_long_strings_and_escaped_newlines(self):
        data = b'--[=[ comment ]=]\nreturn {doc_path=[==[\n/book.epub]==], last_xpointer="a\\\nb\\x43\\z  d", percent_finished=0}'
        parsed = imp.LuaData(data).parse()
        self.assertEqual(parsed[b"doc_path"], b"/book.epub")
        self.assertEqual(parsed[b"last_xpointer"], b"a\nbCd")

    def test_code_and_malformed_data_rejected(self):
        for data in [b'return {last_xpointer=os.execute("touch /tmp/no")}',
                     b'return {}; os.execute("no")', b'while true do end',
                     b'return {last_page=1+2}', b'return {x="\\999"}',
                     b'return {last_page=1, last_page=2}',
                     b'return {x=[=[unfinished}', b'return {x="unterminated}',
                     b'return {x=' + b'{' * 110 + b'}' * 110 + b'}']:
            with self.subTest(data=data[:70]), self.assertRaises(imp.Invalid):
                imp.LuaData(data).parse()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "books"
        self.root.mkdir()
        self.database = self.base / "sync.sqlite3"
        with sqlite3.connect(self.database) as db:
            db.executescript(SCHEMA)
        sync_storage.init_db(self.database)
        self.saved = int(time.time()) - 1000

    def tearDown(self):
        self.temp.cleanup()

    def book(self, name="Book.epub", folder="deep/section", content_id="a" * 32,
             position="/body/p[12]/text().4", percentage=.31, saved=None, content=b"ebook"):
        book = self.root / folder / name
        book.parent.mkdir(parents=True, exist_ok=True)
        book.write_bytes(content)
        sidecar = book.with_suffix(".sdr") / ("metadata." + book.suffix[1:] + ".lua")
        sidecar.parent.mkdir(exist_ok=True)
        fields = [f'last_xpointer={lua_string(position)}' if isinstance(position, str) else f'last_page={position}',
                  f'percent_finished={percentage}', f'doc_path={lua_string("/android/" + name)}']
        if content_id is not None:
            fields.append(f'partial_md5_checksum={lua_string(content_id)}')
        sidecar.write_text("return {" + ",".join(fields) + "}")
        stamp = saved if saved is not None else self.saved
        os.utime(sidecar, (stamp, stamp))
        return book, sidecar

    def run_import(self, apply=False, force=False, user=None, mappings_only=False, strict_filenames=False, pull=False):
        args = type("Args", (), dict(directory=str(self.root), database=str(self.database),
                                     user=user, apply=apply, force=force, mappings_only=mappings_only, strict_filenames=strict_filenames, pull=pull))()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = imp.run(args)
        return code, output.getvalue()

    def rows(self):
        with imp.open_database(self.database) as db:
            return {(row["username"], row["document"]): dict(row) for row in db.execute("SELECT * FROM progress")}

    def write_progress(self, document, timestamp, position="phone", percentage=.8, user="reader"):
        with sync_storage.database(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            sync_storage.put_position(db, user, document, dict(progress=position, percentage=percentage,
                                      device="phone", device_id="test-device"), timestamp)

    def clear_progress(self):
        with sync_storage.database(self.database) as db:
            db.execute("DELETE FROM books")

    def test_preview_dual_ids_backup_and_idempotence(self):
        self.book(name="Café - 01.epub")
        before = self.database.read_bytes()
        code, text = self.run_import()
        self.assertEqual(code, 0)
        self.assertIn("Would write 2 IDs", text)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.base / "import-backups").exists())
        self.assertEqual(self.run_import(apply=True)[0], 0)
        rows = self.rows()
        name_id = hashlib.md5("Café - 01.epub".encode()).hexdigest()
        self.assertEqual(set(rows), {("reader", "a" * 32), ("reader", name_id)})
        self.assertTrue(all(row["progress"] == "/body/p[12]/text().4" for row in rows.values()))
        self.assertTrue(all(row["timestamp"] == self.saved for row in rows.values()))
        backups = list((self.base / "import-backups").glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM progress").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT username FROM users").fetchone()[0], "reader")
        self.assertIn("Wrote 0 IDs", self.run_import(apply=True)[1])
        self.assertEqual(self.rows(), rows)
        self.assertEqual(len(list((self.base / "import-backups").glob("*.bak"))), 1)

    def test_newer_or_equal_server_under_either_id_protects_both(self):
        self.book()
        for document in ("a" * 32, hashlib.md5(b"Book.epub").hexdigest()):
            for stamp in (self.saved, self.saved + 10):
                self.clear_progress()
                self.write_progress(document, stamp)
                before = self.rows()
                self.assertIn("KEEP SERVER", self.run_import(apply=True)[1])
                self.assertEqual(len(self.rows()), 2)
                self.assertTrue(all(row["progress"] == "phone" for row in self.rows().values()))

    def test_missing_partner_of_newer_identical_progress(self):
        self.book()
        self.write_progress("a" * 32, self.saved + 10, "/body/p[12]/text().4", .31)
        self.run_import(apply=True)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["timestamp"] == self.saved + 10 for r in self.rows().values()))

    def test_force_and_newer_sidecar_can_move_backwards(self):
        self.book()
        self.write_progress("a" * 32, self.saved + 10)
        self.run_import(apply=True, force=True)
        self.assertTrue(all(row["percentage"] == .31 for row in self.rows().values()))
        self.assertTrue(all(row["timestamp"] > self.saved for row in self.rows().values()))
        self.clear_progress()
        self.write_progress("a" * 32, self.saved - 1)
        self.run_import(apply=True)
        self.assertEqual(self.rows()[("reader", "a" * 32)]["percentage"], .31)

    def test_duplicate_filename_skips_name_but_keeps_hashes(self):
        self.book(folder="one")
        self.book(folder="two", content_id="b" * 32)
        code, text = self.run_import(apply=True, strict_filenames=True)
        self.assertEqual(code, 1)
        self.assertIn("COLLISION", text)
        self.assertEqual({key[1] for key in self.rows()}, {"a" * 32, "b" * 32})

    def test_duplicate_filename_without_sidecar_is_detected(self):
        self.book()
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "Book.epub").write_bytes(b"unread different book")
        self.assertEqual(self.run_import(apply=True, strict_filenames=True)[0], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_same_content_different_names_uses_furthest_even_when_older(self):
        self.book("Old Name.epub", percentage=.8, position="furthest")
        self.book("New Name.epub", position="new-position", percentage=.2, saved=self.saved + 1)
        self.run_import(apply=True)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(r["progress"] == "furthest" for r in self.rows().values()))
        self.assertTrue(all(r["percentage"] == .8 for r in self.rows().values()))

    def test_identical_copies_share_filename_id_and_merge_equal_positions(self):
        self.book(folder="one")
        self.book(folder="two", saved=self.saved + 1)
        code, text = self.run_import(apply=True)
        self.assertEqual(code, 0)
        self.assertIn("2 copies", text)
        self.assertNotIn("COLLISION", text)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["progress"] == "/body/p[12]/text().4" for r in self.rows().values()))
        self.assertTrue(all(r["timestamp"] == self.saved + 1 for r in self.rows().values()))
        self.assertIn("Wrote 0 IDs", self.run_import(apply=True)[1])

    def test_duplicate_filename_same_hash_uses_furthest_for_both_ids(self):
        self.book(folder="one", position="ahead", percentage=.9)
        self.book(folder="two", position="behind", percentage=.2, saved=self.saved + 10)
        self.assertEqual(self.run_import(apply=True)[0], 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["progress"] == "ahead" for r in self.rows().values()))

    def test_equal_percentage_keeps_first_position_without_time_tiebreaker(self):
        self.book("First.epub", position="first-location")
        self.book("Second.epub", position="other-location", saved=self.saved + 1)
        self.assertEqual(self.run_import(apply=True)[0], 0)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(r["progress"] == "first-location" for r in self.rows().values()))

    def test_equal_time_different_progress_uses_furthest(self):
        self.book("First.epub", position="behind", percentage=.1)
        self.book("Second.epub", position="ahead", percentage=.8)
        self.assertEqual(self.run_import(apply=True)[0], 0)
        self.assertTrue(all(r["progress"] == "ahead" for r in self.rows().values()))

    def test_identical_copy_without_sidecar_shares_filename_id(self):
        actual_hash = hashlib.md5(b"ebook").hexdigest()
        self.book(content_id=actual_hash)
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "Book.epub").write_bytes(b"ebook")
        self.assertEqual(self.run_import(apply=True)[0], 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertIn(("reader", hashlib.md5(b"Book.epub").hexdigest()), self.rows())

    def test_pdf_page_and_partial_md5_fallback(self):
        content = bytes(range(251)) * 300
        book, sidecar = self.book("Paper.pdf", content_id=None, position=42, content=content)
        expected = hashlib.md5(b"".join(content[start:start + 1024]
                                       for start in (0, 1024, 4096, 16384, 65536))).hexdigest()
        self.assertEqual(imp.partial_md5(book)[0], expected)
        self.run_import(apply=True)
        self.assertEqual(self.rows()[("reader", expected)]["progress"], "42")

    def test_saved_hash_is_preferred_and_missing_book_supported(self):
        book, _ = self.book()
        book.unlink()
        self.run_import(apply=True)
        self.assertIn(("reader", "a" * 32), self.rows())
        self.assertIn(("reader", hashlib.md5(b"Book.epub").hexdigest()), self.rows())

    def test_hashed_sidecar_location_uses_doc_path(self):
        book, sidecar = self.book()
        target = self.root / ("a" * 32 + ".sdr")
        target.mkdir()
        sidecar.rename(target / sidecar.name)
        book.unlink()
        self.run_import(apply=True)
        self.assertIn(("reader", hashlib.md5(b"Book.epub").hexdigest()), self.rows())

    def test_ignore_old_versions_conflicts_and_symlink_dirs(self):
        _, sidecar = self.book()
        (sidecar.parent / "metadata.epub.lua.old").write_text('return {last_page=1}')
        self.book("Skipped.epub", folder=".stversions")
        self.book("Conflict.epub", folder="conflict.sync-conflict-123.sdr")
        (self.root / "alias").symlink_to(self.root / "deep", target_is_directory=True)
        self.assertEqual(self.run_import(apply=True)[0], 0)
        self.assertEqual(len(self.rows()), 2)

    def test_bad_sidecar_and_symlink_do_not_prevent_valid_imports(self):
        self.book()
        _, bad = self.book("Bad.epub", content_id="b" * 32)
        bad.write_text('return { last_page = os.execute("never") }')
        _, symlink = self.book("Link.epub", content_id="c" * 32)
        symlink.unlink()
        symlink.symlink_to(bad)
        self.assertEqual(self.run_import(apply=True)[0], 1)
        self.assertEqual(len(self.rows()), 2)

    def test_missing_hash_and_book_is_reported(self):
        book, _ = self.book(content_id=None)
        book.unlink()
        self.assertEqual(self.run_import(apply=True)[0], 1)
        self.assertFalse(self.rows())

    def test_future_timestamp_maps_without_importing_position(self):
        self.book(saved=int(time.time()) + 3600)
        code, text = self.run_import(apply=True)
        self.assertEqual(code, 0)
        self.assertIn("future sidecar", text)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["progress"] is None for row in self.rows().values()))

    def test_changed_sidecar_skipped_between_scan_and_write(self):
        _, sidecar = self.book()
        books, names, counts = imp.scan(self.root, lambda _: None, int(time.time()))
        sidecar.write_text("return {}")
        changes = imp.plan(books, names, {}, False, int(time.time()), lambda _: None, counts)
        self.assertFalse(changes)
        self.assertEqual(counts["errors"], 1)

    def test_accounts_preserved_and_separate(self):
        self.book()
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO users VALUES ('second', 'other-key')")
        self.write_progress("a" * 32, self.saved + 10, user="second")
        with self.assertRaises(imp.Invalid):
            self.run_import(apply=True)
        self.run_import(apply=True, user="reader")
        self.assertEqual(self.rows()[("second", "a" * 32)]["progress"], "phone")
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT userkey FROM users WHERE username='second'").fetchone()[0], "other-key")

    def test_missing_database_does_not_get_created(self):
        self.database.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            self.run_import()
        self.assertFalse(self.database.exists())

    def test_backup_failure_rolls_back(self):
        self.book()
        (self.base / "import-backups").write_text("not a directory")
        with self.assertRaises(OSError):
            self.run_import(apply=True)
        self.assertFalse(self.rows())

    def test_batch_write_failure_rolls_back(self):
        self.book()
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON document_aliases WHEN NEW.document != '" + "a" * 32 + "' BEGIN SELECT RAISE(ABORT, 'test failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_import(apply=True)
        self.assertFalse(self.rows())

    def test_wal_database_backup(self):
        self.book()
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA journal_mode=WAL")
        self.run_import(apply=True)
        self.assertEqual(len(self.rows()), 2)
        backup = next((self.base / "import-backups").glob("*.bak"))
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_writer_waits_for_transaction_then_wins(self):
        self.book()
        self.run_import(apply=True)
        acquired, done = threading.Event(), threading.Event()
        with imp.open_database(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            def update():
                acquired.set()
                self.write_progress("a" * 32, int(time.time()), "new-phone")
                done.set()
            thread = threading.Thread(target=update)
            thread.start()
            acquired.wait(5)
            self.assertFalse(done.wait(.05))
            db.commit()
        thread.join(5)
        self.assertTrue(done.is_set())
        self.run_import(apply=True)
        self.assertEqual(self.rows()[("reader", "a" * 32)]["progress"], "new-phone")

    def test_original_server_api_sees_both_ids_and_preserves_new_upload(self):
        server = importlib.import_module("koreader_sync")
        server.USERSDB = str(self.database)
        server.initDb()
        self.book()
        self.run_import(apply=True)
        client = server.app.test_client()
        headers = {"x-auth-user": "reader", "x-auth-key": "test-key"}
        for document in ("a" * 32, hashlib.md5(b"Book.epub").hexdigest()):
            response = client.get("/syncs/progress/" + document, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["progress"], "/body/p[12]/text().4")
            self.assertEqual(response.json["percentage"], .31)
        response = client.put("/syncs/progress", headers=headers, json={
            "document": "a" * 32, "progress": "new-phone", "percentage": .9,
            "device": "Phone", "device_id": "phone"})
        self.assertEqual(response.status_code, 200)
        self.run_import(apply=True)
        self.assertEqual(client.get("/syncs/progress/" + "a" * 32, headers=headers).json["progress"], "new-phone")


if __name__ == "__main__":
    unittest.main()
