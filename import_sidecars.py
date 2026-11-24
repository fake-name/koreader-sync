#!/usr/bin/env python3
"""Manual sidecar import for the SQLite myelsukov/koreader-sync installation.

Python 3.10+, standard library only. Does not execute Lua, modify books, or
start a background process. See docs/SIDECAR-IMPORT.md for conflict semantics.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid

MAX_BYTES = 32 * 1024 * 1024
FIELDS = {b"doc_path", b"partial_md5_checksum", b"last_xpointer",
          b"last_page", b"percent_finished"}
MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
METADATA = re.compile(r"metadata\.([^.]+)\.lua\Z")
DEVICE = "KOReader sidecar import"
DEVICE_ID = "kosync-sidecar-import-v1"


class Invalid(ValueError):
    pass


class LuaData:
    """Read a literal `return {...}`; discard unrelated/nested metadata.

    Strings are bytes, matching Lua's string/decimal-escape semantics. No eval,
    interpreter, function calls, expressions, or arbitrary Lua identifiers.
    """

    number = re.compile(rb"[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")
    identifier = re.compile(rb"[A-Za-z_][A-Za-z_0-9]*")
    long_open = re.compile(rb"\[(=*)\[")
    whitespace = re.compile(rb"\s*")

    def __init__(self, data):
        self.data, self.pos, self.tokens = data, 0, 0

    def error(self, message):
        raise Invalid(f"{message} at byte {self.pos}")

    def long_string(self):
        match = self.long_open.match(self.data, self.pos)
        if not match:
            return None
        start = match.end()
        closing = b"]" + match[1] + b"]"
        end = self.data.find(closing, start)
        if end < 0:
            self.error("unterminated long string/comment")
        self.pos = end + len(closing)
        value = self.data[start:end].replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return value[1:] if value.startswith(b"\n") else value

    def space(self):
        while True:
            self.pos = self.whitespace.match(self.data, self.pos).end()
            if not self.data.startswith(b"--", self.pos):
                return
            self.pos += 2
            if self.long_string() is None:
                end = self.data.find(b"\n", self.pos)
                self.pos = len(self.data) if end < 0 else end + 1

    def take(self, token):
        self.space()
        if self.data.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def expect(self, token):
        if not self.take(token):
            self.error(f"expected {token!r}")

    def quoted(self):
        quote = self.data[self.pos]
        self.pos += 1
        out = bytearray()
        escapes = {97: 7, 98: 8, 102: 12, 110: 10, 114: 13,
                   116: 9, 118: 11, 92: 92, 34: 34, 39: 39}
        while self.pos < len(self.data):
            char = self.data[self.pos]
            self.pos += 1
            if char == quote:
                return bytes(out)
            if char in (10, 13):
                self.error("unescaped newline in string")
            if char != 92:
                out.append(char)
                continue
            if self.pos == len(self.data):
                self.error("unfinished escape")
            char = self.data[self.pos]
            self.pos += 1
            if char in escapes:
                out.append(escapes[char])
            elif 48 <= char <= 57:
                digits = bytes([char])
                for _ in range(2):
                    if self.pos < len(self.data) and 48 <= self.data[self.pos] <= 57:
                        digits += self.data[self.pos:self.pos + 1]
                        self.pos += 1
                value = int(digits)
                if value > 255:
                    self.error("decimal escape exceeds 255")
                out.append(value)
            elif char == 120:  # LuaJIT/Lua 5.2 hexadecimal byte escape
                digits = self.data[self.pos:self.pos + 2]
                if not re.fullmatch(rb"[0-9a-fA-F]{2}", digits):
                    self.error("invalid hex escape")
                out.append(int(digits, 16))
                self.pos += 2
            elif char == 122:  # \z removes following whitespace
                self.pos = self.whitespace.match(self.data, self.pos).end()
            elif char in (10, 13):
                if char == 13 and self.data[self.pos:self.pos + 1] == b"\n":
                    self.pos += 1
                out.append(10)
            else:
                self.error("unsupported escape")
        self.error("unterminated string")

    def value(self, depth=0):
        self.space()
        self.tokens += 1
        if depth > 100 or self.tokens > 2_000_000:
            self.error("metadata nesting/token limit exceeded")
        char = self.data[self.pos:self.pos + 1]
        if char == b"{":
            return self.table(depth)
        if char in (b"'", b'"'):
            return self.quoted()
        if char == b"[":
            value = self.long_string()
            if value is not None:
                return value
        match = self.number.match(self.data, self.pos)
        if match:
            self.pos = match.end()
            token = match[0].decode("ascii")
            if "x" in token.lower():
                return int(token, 16)
            return float(token) if any(c in token for c in ".eE") else int(token)
        match = self.identifier.match(self.data, self.pos)
        if match and match[0] in (b"true", b"false", b"nil"):
            self.pos = match.end()
            return {b"true": True, b"false": False, b"nil": None}[match[0]]
        self.error("expected a literal value (Lua code is not supported)")

    def table(self, depth):
        self.expect(b"{")
        result = {}
        while not self.take(b"}"):
            self.space()
            key = None
            if self.data[self.pos:self.pos + 1] == b"[" and not self.long_open.match(self.data, self.pos):
                self.pos += 1
                key = self.value(depth + 1)
                self.expect(b"]")
                self.expect(b"=")
            else:
                start = self.pos
                match = self.identifier.match(self.data, self.pos)
                if match:
                    self.pos = match.end()
                    if self.take(b"="):
                        key = match[0]
                    else:
                        self.pos = start
            value = self.value(depth + 1)
            if depth == 0 and isinstance(key, bytes) and key in FIELDS:
                if key in result:
                    self.error(f"duplicate field {key!r}")
                result[key] = value
            if not self.take(b",") and not self.take(b";"):
                self.expect(b"}")
                break
        return result if depth == 0 else None

    def parse(self):
        self.space()
        match = self.identifier.match(self.data, self.pos)
        if not match or match[0] != b"return":
            self.error("expected return")
        self.pos = match.end()
        result = self.table(0)
        self.take(b";")
        self.space()
        if self.pos != len(self.data):
            self.error("unexpected content after table")
        return result


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_sidecar(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise Invalid("not a regular file or larger than 32 MiB")
        data = file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise Invalid("larger than 32 MiB")
        if signature(before) != signature(os.fstat(file.fileno())):
            raise Invalid("file changed while being read; rerun after Syncthing finishes")
    if signature(before) != signature(path.stat(follow_symlinks=False)):
        raise Invalid("file was replaced while being read; rerun after Syncthing finishes")
    return LuaData(data).parse(), before


def partial_md5(path):
    """KOReader util.partialMD5: 1 KiB at 0, 1 KiB, 4 KiB, ... 1 GiB."""
    digest = hashlib.md5()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Invalid("book is not a regular file")
        for offset in [0] + [1024 << (2 * i) for i in range(11)]:
            file.seek(offset)
            sample = file.read(1024)
            if not sample:
                break
            digest.update(sample)
        if signature(before) != signature(os.fstat(file.fileno())):
            raise Invalid("book changed while hashing")
    if signature(before) != signature(path.stat(follow_symlinks=False)):
        raise Invalid("book was replaced while hashing")
    return digest.hexdigest(), signature(before)


@dataclass
class Book:
    sidecar: Path
    sidecar_signature: tuple
    book_path: Path
    book_signature: tuple | None
    filename: str
    content_id: str
    name_id: str
    progress: str
    percentage: float
    timestamp: int


def decode(value, field):
    if not isinstance(value, bytes) or not value or b"\0" in value:
        raise Invalid(f"missing/invalid {field}")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Invalid(f"{field} is not UTF-8") from error


def load_book(path, now):
    data, info = read_sidecar(path)
    suffix = METADATA.fullmatch(path.name)[1]
    stem = path.parent.name[:-4]
    filename = stem if suffix == "_" else stem + "." + suffix
    book_path = path.parent.parent / filename
    # Hashed metadata storage uses the original doc_path's basename. An adjacent
    # real book takes precedence; Android's absolute path need not exist here.
    if not book_path.is_file() and MD5.fullmatch(stem):
        filename = decode(data.get(b"doc_path"), "doc_path").rsplit("/", 1)[-1]
        if filename in ("", ".", ".."):
            raise Invalid("invalid basename in doc_path")
        book_path = path.parent.parent / filename
    filename.encode("utf-8")
    percentage = data.get(b"percent_finished")
    if type(percentage) not in (int, float) or not math.isfinite(percentage) or not 0 <= percentage <= 1:
        raise Invalid("missing/invalid percent_finished (expected 0..1)")
    if data.get(b"last_xpointer"):
        progress = decode(data[b"last_xpointer"], "last_xpointer")
    else:
        page = data.get(b"last_page")
        if type(page) not in (int, float) or not math.isfinite(page) or page < 1 or int(page) != page:
            raise Invalid("no saved last_xpointer or valid last_page")
        progress = str(int(page))
    if len(progress) > 65536:
        raise Invalid("reading position is too long")
    content_id = data.get(b"partial_md5_checksum")
    book_signature = None
    if content_id is not None:
        content_id = decode(content_id, "partial_md5_checksum").lower()
        if not MD5.fullmatch(content_id):
            raise Invalid("invalid partial_md5_checksum")
    else:
        # Prefer the saved hash: that is what KOReader actually sends, and it
        # describes the edition whose reading position is saved in this sidecar.
        content_id, book_signature = partial_md5(book_path)
    timestamp = info.st_mtime_ns // 1_000_000_000
    if timestamp <= 0 or timestamp > now:
        raise Invalid("sidecar modification time is invalid/in the future; check clocks")
    return Book(path, signature(info), book_path, book_signature, filename,
                content_id, hashlib.md5(filename.encode("utf-8")).hexdigest(),
                progress, float(percentage), timestamp)


def scan(root, report, now):
    books = []
    names = defaultdict(set)
    counts = Counter()

    def walk_error(error):
        counts["errors"] += 1
        report(f"ERROR: cannot scan {error.filename!r}: {error.strerror}")

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in (".stversions", ".stfolder", ".git")
                         and ".sync-conflict-" not in d and not (base / d).is_symlink())
        if base.name.endswith(".sdr"):
            dirs[:] = []
            for name in sorted(files):
                if not METADATA.fullmatch(name):
                    continue
                counts["sidecars"] += 1
                try:
                    book = load_book(base / name, now)
                    books.append(book)
                    names[book.filename].add(book.book_path)
                except (OSError, ValueError, OverflowError) as error:
                    counts["errors"] += 1
                    report(f"SKIP: {str(base / name)!r}: {error}")
        else:
            for name in files:
                if not name.startswith(".syncthing.") and ".sync-conflict-" not in name:
                    names[name].add(base / name)
    return books, names, counts


def open_database(path, readonly=False):
    connection = sqlite3.connect(path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def select_user(db, username):
    users = [row[0] for row in db.execute("SELECT username FROM users ORDER BY username")]
    expected = {"username", "document", "progress", "percentage", "device", "device_id", "timestamp"}
    actual = {row[1] for row in db.execute("PRAGMA table_info(progress)")}
    if not expected <= actual:
        raise Invalid("database is not the SQLite sync server installed by setup-koreader-sync.sh")
    if username is None:
        if len(users) != 1:
            raise Invalid(f"found {len(users)} sync accounts; specify the KOReader account with --user")
        return users[0]
    if username not in users:
        raise Invalid(f"no existing KOReader sync account {username!r}")
    return username


def unchanged(book):
    try:
        if signature(book.sidecar.stat(follow_symlinks=False)) != book.sidecar_signature:
            return False
        if book.book_signature and signature(book.book_path.stat(follow_symlinks=False)) != book.book_signature:
            return False
        return True
    except OSError:
        return False


def plan(books, names, existing, force, now, report, counts):
    groups = defaultdict(list)
    known_paths = defaultdict(set)
    for book in books:
        groups[book.content_id].append(book)
        known_paths[book.book_path].add(book.content_id)
    ambiguous = set()
    for name in sorted({book.filename for book in books}):
        paths = names[name]
        if len(paths) < 2:
            continue
        content_ids = set()
        for path in sorted(paths):
            if path in known_paths:
                content_ids.update(known_paths[path])
            else:
                # An unread copy may have no usable sidecar. Confirm its identity
                # with the same partial hash instead of treating all copies as a
                # filename collision merely because their directories differ.
                try:
                    content_id, _ = partial_md5(path)
                    content_ids.add(content_id)
                except (OSError, ValueError):
                    ambiguous.add(name)
                    break
        if len(content_ids) != 1:
            ambiguous.add(name)
        if name in ambiguous:
            counts["collisions"] += 1
            report(f"COLLISION: {name!r} has different or unverifiable content IDs; filename ID will be skipped")
    changes = []
    for content_id, copies in sorted(groups.items()):
        # max() keeps the first encountered copy when percentages are equal.
        # Timestamps do not decide the winning position. Equal saved positions
        # are duplicates, and equal percentages need no artificial time ranking.
        source = max(copies, key=lambda book: book.percentage)
        position = (source.progress, source.percentage)
        # Keep the newest evidence of this exact position for the existing
        # server freshness check; this does not choose a different position.
        saved_at = max(book.timestamp for book in copies
                       if (book.progress, book.percentage) == position)
        if not all(unchanged(book) for book in copies):
            counts["errors"] += 1
            report(f"SKIP: files for {source.filename!r} changed during scanning; rerun")
            continue
        ids = {content_id} | {book.name_id for book in copies if book.filename not in ambiguous}
        # A practically impossible MD5 collision should still not cause cross-book writes.
        if any(document in groups and document != content_id for document in ids):
            counts["errors"] += 1
            report(f"SKIP: filename/content hash collision for {source.filename!r}")
            continue
        rows = [existing[document] for document in ids if document in existing]
        if not force and any(row["timestamp"] >= saved_at and
                             (row["progress"], row["percentage"]) != position for row in rows):
            counts["protected"] += 1
            report(f"KEEP SERVER: {source.filename!r}: newer/equal-time server progress; both IDs left alone")
            continue
        timestamp = now if force else saved_at
        # If a newer server record already holds this very same position, use its
        # time when filling in its missing partner; do not turn it into an older update.
        if not force:
            timestamp = max([timestamp] + [row["timestamp"] for row in rows])
        affected = []
        for document in sorted(ids):
            row = existing.get(document)
            if row and (row["progress"], row["percentage"]) == position:
                counts["unchanged_ids"] += 1
                continue
            changes.append((document, source.progress, source.percentage,
                            DEVICE, DEVICE_ID, timestamp))
            affected.append("content" if document == content_id else "filename")
        if affected:
            counts["changed_books"] += 1
            if len(copies) > 1:
                report(f"MERGE: {len(copies)} copies -> {source.percentage:.2%} from {str(source.sidecar)!r}")
            report(f"UPDATE ({', '.join(affected)}): {source.filename!r} -> {source.percentage:.2%}")
    return changes


def backup_database(path):
    directory = path.parent / "import-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{path.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(open_database(path, readonly=True)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def run(args):
    os.umask(0o077)
    root, database = Path(args.directory).resolve(), Path(args.database).resolve()
    if not root.is_dir():
        raise Invalid(f"book directory does not exist: {root}")
    # Validate the account/schema before spending time scanning a large library.
    db = open_database(database, readonly=not args.apply)
    try:
        username = select_user(db, args.user)
        print(f"{'APPLY' if args.apply else 'PREVIEW'}: account={username!r}, directory={str(root)!r}")
        if args.force:
            print("FORCE: sidecar positions may replace newer server progress.")
        now = int(time.time())
        books, names, counts = scan(root, print, now)
        # Readers remain available. BEGIN IMMEDIATE serializes this batch with
        # API writers, so no update can race our freshness check and commit.
        db.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
        select_user(db, username)
        existing = {row["document"]: row for row in db.execute(
            "SELECT document, progress, percentage, timestamp FROM progress WHERE username = ?", (username,))}
        changes = plan(books, names, existing, args.force, now, print, counts)
        backup = None
        if args.apply and changes:
            # A second read connection makes a consistent backup while this
            # connection holds the write reservation, before any rows change.
            backup = backup_database(database)
            db.executemany("""
                INSERT INTO progress (username, document, progress, percentage, device, device_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, document) DO UPDATE SET
                    progress=excluded.progress, percentage=excluded.percentage,
                    device=excluded.device, device_id=excluded.device_id, timestamp=excluded.timestamp
            """, [(username, *change) for change in changes])
        db.commit()
        verb = "Wrote" if args.apply else "Would write"
        print(f"{verb} {len(changes)} IDs for {counts['changed_books']} books; "
              f"{counts['sidecars']} sidecars scanned, {counts['unchanged_ids']} IDs unchanged, "
              f"{counts['protected']} books protected, {counts['collisions']} filename collisions, "
              f"{counts['errors']} errors.")
        if backup:
            print(f"Backup: {backup}")
        if not args.apply:
            print("Preview only. Add --apply to write these updates.")
        return 1 if counts["errors"] or counts["collisions"] else 0
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", nargs="?", default="/mnt/data/Reader_Sync",
                        help="book tree to scan recursively (default: /mnt/data/Reader_Sync)")
    parser.add_argument("--database", default="/var/lib/koreader-sync/sync.sqlite3")
    parser.add_argument("--user", help="existing KOReader sync username; auto-selected if exactly one exists")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes (default is preview only)")
    mode.add_argument("--dry-run", action="store_true", help="explicit preview; no database changes or backup")
    parser.add_argument("--force", action="store_true", help="make sidecars win even over newer server positions")
    args = parser.parse_args()
    try:
        return run(args)
    except (Invalid, OSError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
