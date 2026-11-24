#!/usr/bin/env python3
"""Book alias mapping and bidirectional KOReader sidecar progress sync.

Python 3.10+, standard library only. Defaults to preview; --apply writes changes.
Does not execute Lua or modify ebook content. See docs/SIDECAR-IMPORT.md.
"""

import argparse
import base64
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

import sync_storage
from scan_cache import ScanCache

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
        self.spans = {}
        self.root_start = None

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
        if depth == 0:
            self.root_start = self.pos
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
            self.space()
            value_start = self.pos
            value = self.value(depth + 1)
            if depth == 0 and isinstance(key, bytes) and key in FIELDS:
                if key in result:
                    self.error(f"duplicate field {key!r}")
                result[key] = value
                self.spans[key] = (value_start, self.pos)
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


def read_sidecar(path, raw=False):
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
    parsed = LuaData(data).parse()
    return (parsed, before, data) if raw else (parsed, before)


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


def encode_fields(fields):
    return {key.decode('ascii'): {'bytes': base64.b64encode(value).decode('ascii')}
            if isinstance(value, bytes) else value for key, value in fields.items()}


def decode_fields(payload):
    if not isinstance(payload, dict):
        raise ValueError('invalid cached metadata')
    fields = {}
    for key, value in payload.items():
        field = key.encode('ascii')
        if field not in FIELDS:
            raise ValueError('unknown cached field')
        if isinstance(value, dict):
            if set(value) != {'bytes'} or not isinstance(value['bytes'], str):
                raise ValueError('invalid cached byte string')
            value = base64.b64decode(value['bytes'], validate=True)
        elif value is not None and type(value) not in (bool, int, float):
            raise ValueError('invalid cached scalar')
        fields[field] = value
    return fields


def cached_sidecar(path, cache):
    if cache is None:
        return read_sidecar(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise Invalid('not a regular file or larger than 32 MiB')
    payload = cache.get(path, 'sidecar', signature(info))
    if payload is not None:
        try:
            fields = decode_fields(payload)
        except (ValueError, UnicodeError):
            pass  # A disposable bad entry is replaced from its source.
        else:
            cache.counts['sidecar_hit'] += 1
            return fields, info
    fields, info = read_sidecar(path)
    cache.counts['sidecar_read'] += 1
    cache.put(path, 'sidecar', signature(info), encode_fields(fields))
    return fields, info


def cached_partial_md5(path, cache):
    if cache is None:
        return partial_md5(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise Invalid('book is not a regular file')
    stamp = signature(info)
    digest = cache.get(path, 'hash', stamp)
    if isinstance(digest, str) and MD5.fullmatch(digest):
        cache.counts['hash_hit'] += 1
        return digest, stamp
    digest, stamp = partial_md5(path)
    cache.counts['hash_read'] += 1
    cache.put(path, 'hash', stamp, digest)
    return digest, stamp


@dataclass
class Book:
    sidecar: Path | None
    sidecar_signature: tuple | None
    book_path: Path
    book_signature: tuple | None
    filename: str
    content_id: str
    name_id: str
    progress: str | None
    percentage: float | None
    timestamp: int
    current_id: str | None = None
    position_issue: str | None = None


def decode(value, field):
    if not isinstance(value, bytes) or not value or b"\0" in value:
        raise Invalid(f"missing/invalid {field}")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Invalid(f"{field} is not UTF-8") from error


def load_book(path, now, mappings_only=False, cache=None):
    data, info = cached_sidecar(path, cache)
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
    progress = None
    issue = None
    # Retain the parsed position even on mapping-only passes so an identical
    # server position can be skipped without reopening the sidecar. plan()
    # still forbids position uploads when mappings_only is true.
    try:
        if percentage is None:
            raise Invalid("no percentage saved; mappings only")
        if type(percentage) not in (int, float) or not math.isfinite(percentage) or not 0 <= percentage <= 1:
            raise Invalid(f"invalid percent_finished {percentage!r}; mappings only")
        if data.get(b"last_xpointer"):
            progress = decode(data[b"last_xpointer"], "last_xpointer")
        else:
            page = data.get(b"last_page")
            if type(page) not in (int, float) or not math.isfinite(page) or page < 1 or int(page) != page:
                raise Invalid("no saved reading location; mappings only")
            progress = str(int(page))
        if len(progress) > 65536:
            raise Invalid("reading position is too long; mappings only")
        percentage = float(percentage)
    except (Invalid, OverflowError) as error:
        progress, percentage, issue = None, None, str(error)
    content_id = data.get(b"partial_md5_checksum")
    book_signature = None
    if content_id is not None:
        content_id = decode(content_id, "partial_md5_checksum").lower()
        if not MD5.fullmatch(content_id):
            raise Invalid("invalid partial_md5_checksum")
    else:
        # Prefer the saved hash: that is what KOReader actually sends, and it
        # describes the edition whose reading position is saved in this sidecar.
        content_id, book_signature = cached_partial_md5(book_path, cache)
    current_id = None
    if mappings_only and book_path.is_file():
        current_id, book_signature = cached_partial_md5(book_path, cache)
    timestamp = info.st_mtime_ns // 1_000_000_000
    if timestamp <= 0 or timestamp > now:
        progress, percentage = None, None
        issue = "invalid/future sidecar modification time; mappings only"
    return Book(path, signature(info), book_path, book_signature, filename,
                content_id, hashlib.md5(filename.encode("utf-8")).hexdigest(),
                progress, percentage, timestamp, current_id, issue)


def scan(root, report, now, mappings_only=False, cache=None):
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
                    book = load_book(base / name, now, mappings_only, cache)
                    books.append(book)
                    names[book.filename].add(book.book_path)
                    if book.position_issue and not mappings_only:
                        counts["no_position"] += 1
                        report(f"INFO: {str(book.sidecar)!r}: {book.position_issue}")
                except (OSError, ValueError, OverflowError) as error:
                    counts["errors"] += 1
                    report(f"SKIP: {str(base / name)!r}: {error}")
        else:
            for name in files:
                if not name.startswith(".syncthing.") and ".sync-conflict-" not in name:
                    names[name].add(base / name)
    if mappings_only:
        seen = {book.book_path for book in books}
        extensions = {".epub", ".pdf", ".djvu", ".cbz", ".cbt", ".mobi", ".azw", ".azw3", ".fb2"}
        for name, paths in sorted(names.items()):
            if Path(name).suffix.lower() not in extensions:
                continue
            for path in sorted(paths - seen):
                try:
                    content_id, info = cached_partial_md5(path, cache)
                    books.append(Book(None, None, path, info, name, content_id,
                                      hashlib.md5(name.encode("utf-8")).hexdigest(), None, None, 0))
                except (OSError, ValueError) as error:
                    counts["errors"] += 1
                    report(f"SKIP: {str(path)!r}: {error}")
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
    if not expected <= actual or db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise Invalid("run setup-koreader-sync.sh to upgrade the server/database before using this importer")
    if username is None:
        if len(users) != 1:
            raise Invalid(f"found {len(users)} sync accounts; specify the KOReader account with --user")
        return users[0]
    if username not in users:
        raise Invalid(f"no existing KOReader sync account {username!r}")
    return username


def unchanged(book):
    try:
        if book.sidecar is not None and signature(book.sidecar.stat(follow_symlinks=False)) != book.sidecar_signature:
            return False
        if book.book_signature and signature(book.book_path.stat(follow_symlinks=False)) != book.book_signature:
            return False
        return True
    except OSError:
        return False


class IdGroups:
    def __init__(self):
        self.parents = {}

    def root(self, value):
        self.parents.setdefault(value, value)
        root = value
        while self.parents[root] != root:
            root = self.parents[root]
        while self.parents[value] != value:
            parent = self.parents[value]
            self.parents[value] = root
            value = parent
        return root

    def join(self, values):
        values = list(values)
        root = self.root(values[0])
        for value in values[1:]:
            self.parents[self.root(value)] = root


def plan(books, names, existing, force, now, report, counts, mappings_only=False, strict_filenames=False, cache=None):
    identities = IdGroups()
    known_paths = defaultdict(set)
    existing_books = {}
    existing_aliases = defaultdict(set)
    for key, row in existing.items():
        existing_books[row["book_id"]] = row
        existing_aliases[row["book_id"]].add(key)
    for book in books:
        keys = {book.content_id}
        if book.current_id:
            keys.add(book.current_id)
        identities.join(sorted(keys))
        known_paths[book.book_path].update(keys)
        for key in keys:
            if key in existing:
                identities.join([key, "book:" + existing[key]["book_id"]])
    ambiguous = set()
    for name in sorted({book.filename for book in books}) if strict_filenames else ():
        if len(names[name]) < 2:
            continue
        hashes = set()
        for path in sorted(names[name]):
            if path in known_paths:
                hashes.update(known_paths[path])
            else:
                try:
                    digest, _ = cached_partial_md5(path, cache)
                    hashes.add(digest)
                except (OSError, ValueError):
                    ambiguous.add(name)
                    break
        if len({identities.root(key) for key in hashes}) != 1:
            ambiguous.add(name)
        if name in ambiguous:
            counts["collisions"] += 1
            report(f"COLLISION: {name!r} has different or unverifiable content IDs; filename ID will be skipped")
    keys_by_path = {}
    for book in books:
        keys = set(known_paths[book.book_path])
        if book.filename not in ambiguous:
            keys.add(book.name_id)
        identities.join(sorted(keys))
        for key in sorted(keys):
            if key in existing:
                identities.join([key, "book:" + existing[key]["book_id"]])
        keys_by_path[book.book_path] = keys
    groups = defaultdict(list)
    for book in books:
        groups[identities.root(book.content_id)].append(book)
    operations = []
    for _, copies in sorted(groups.items()):
        if not all(unchanged(book) for book in copies):
            counts["errors"] += 1
            report(f"SKIP: files for {copies[0].filename!r} changed during scanning; rerun")
            continue
        aliases = set().union(*(keys_by_path[book.book_path] for book in copies))
        # Include the whole existing identity, including historical keys that
        # no longer appear anywhere in the scanned directory.
        previous = {existing[key]["book_id"] for key in aliases if key in existing}
        rows = {book_id: existing_books[book_id] for book_id in previous}
        all_aliases = aliases.union(*(existing_aliases[book_id] for book_id in previous))
        target = min(previous) if previous else uuid.uuid4().hex
        valid_rows = [row for _, row in sorted(rows.items())
                      if row["progress"] is not None and row["percentage"] is not None]
        winner = max(valid_rows, key=lambda row: row["percentage"], default=None)
        selected = {key: winner[key] for key in sync_storage.POSITION_FIELDS} if winner else None
        readable = [book for book in copies if book.progress is not None and book.percentage is not None]
        if not mappings_only and readable:
            source = max(readable, key=lambda book: book.percentage)
            position = (source.progress, source.percentage)
            saved_at = max(book.timestamp for book in readable if (book.progress, book.percentage) == position)
            protected = not force and any(row["timestamp"] >= saved_at and
                (row["progress"], row["percentage"]) != position for row in valid_rows)
            if protected:
                counts["protected"] += 1
                report(f"KEEP SERVER: {source.filename!r}: newer/equal-time server progress; aliases will still be linked")
            elif force or len(previous) < 2 or winner is None or source.percentage > winner["percentage"]:
                # When consolidating independent server records, furthest wins.
                # Once already linked, normal freshness/force import rules apply.
                timestamp = now if force else saved_at
                if not force:
                    timestamp = max([timestamp] + [row["timestamp"] for row in valid_rows
                                    if (row["progress"], row["percentage"]) == position])
                if not winner or (winner["progress"], winner["percentage"]) != position:
                    selected = dict(progress=source.progress, percentage=source.percentage,
                                    device=DEVICE, device_id=DEVICE_ID, timestamp=timestamp)
        current = rows.get(target)
        position_changed = selected is not None and (not current or
            any(current[key] != selected[key] for key in sync_storage.POSITION_FIELDS))
        new_aliases = {key for key in aliases if key not in existing}
        merged = max(0, len(previous) - 1)
        if not new_aliases and not merged and not position_changed:
            counts["unchanged_ids"] += len(aliases)
            continue
        counts["changed_books"] += 1
        counts["new_aliases"] += len(new_aliases)
        counts["merged_books"] += merged
        counts["written_ids"] += len(all_aliases) if position_changed or merged else len(new_aliases)
        label = copies[0].filename
        detail = f" -> {selected['percentage']:.2%}" if selected else " (no reading position)"
        report(f"LINK: {label!r}: {len(all_aliases)} keys, {len(copies)} copies{detail}")
        operations.append(dict(book_id=target, previous_ids=sorted(previous),
                               aliases=sorted(aliases), position=selected if position_changed else None))
    return operations

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


def lua_quote(value):
    out = bytearray(b'"')
    for byte in value.encode('utf-8'):
        if byte in (34, 92):
            out.extend(b'\\' + bytes([byte]))
        elif byte < 32 or byte == 127:
            out.extend(('\\%03d' % byte).encode())
        else:
            out.append(byte)
    return bytes(out) + b'"'


def patch_sidecar(data, filename, position):
    parser = LuaData(data)
    fields = parser.parse()
    percentage = position['percentage']
    progress = position['progress']
    if type(percentage) not in (int, float) or not math.isfinite(percentage) or not 0 <= percentage <= 1:
        raise Invalid('server percentage is invalid; sidecar left alone')
    if not isinstance(progress, str) or not progress:
        raise Invalid('server location is missing; sidecar left alone')
    paged = Path(filename).suffix.lower() in {'.pdf', '.djvu', '.cbz', '.cbt', '.cbr'}
    if paged or (b'last_page' in fields and b'last_xpointer' not in fields):
        if not re.fullmatch(r'[1-9][0-9]*', progress):
            raise Invalid('server location is not a page number; sidecar left alone')
        key, value, encoded = b'last_page', int(progress), progress.encode('ascii')
    else:
        if not progress.startswith('/') or '\x00' in progress:
            raise Invalid('server location is not a KOReader XPointer; sidecar left alone')
        key, value, encoded = b'last_xpointer', progress.encode('utf-8'), lua_quote(progress)
    if fields.get(key) == value and fields.get(b'percent_finished') == percentage:
        return None
    replacements = {key: encoded, b'percent_finished': repr(float(percentage)).encode('ascii')}
    edits, insertions = [], []
    for field, replacement in replacements.items():
        if field in parser.spans:
            start, end = parser.spans[field]
            edits.append((start, end, replacement))
        else:
            insertions.append(b'\n    ["' + field + b'"] = ' + replacement + b',')
    if insertions:
        edits.append((parser.root_start, parser.root_start, b''.join(insertions) + b'\n'))
    updated = data
    for start, end, replacement in sorted(edits, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    check = LuaData(updated).parse()
    if check.get(key) != value or check.get(b'percent_finished') != percentage:
        raise Invalid('sidecar rewrite validation failed')
    return updated


def write_sidecar(book, info, original, updated, timestamp, database):
    # Backup the exact original outside the book tree, so it is neither
    # imported on the next pass nor confused with KOReader's own .old file.
    directory = database.parent / 'sidecar-backups' / hashlib.sha256(os.fsencode(book.sidecar)).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / 'source-path.txt').write_text(str(book.sidecar) + '\n')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = directory / f'{stamp}.{uuid.uuid4().hex[:8]}.lua'
    with backup.open('xb') as file:
        file.write(original)
        file.flush()
        os.fsync(file.fileno())
    for parent in (directory, directory.parent, database.parent):
        backup_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)
    fd, temporary_name = tempfile.mkstemp(prefix='.kosync-', suffix='.tmp', dir=book.sidecar.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(updated)
            file.flush()
            current_owner = os.fstat(file.fileno())
            if (current_owner.st_uid, current_owner.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(file.fileno(), info.st_uid, info.st_gid)
            shutil.copystat(book.sidecar, temporary, follow_symlinks=False)
            os.utime(temporary, ns=(info.st_atime_ns, int(timestamp) * 1_000_000_000))
            os.fsync(file.fileno())
        if not unchanged(book):
            raise Invalid('sidecar/book changed before replacement; will retry on a later scan')
        os.replace(temporary, book.sidecar)
        directory_fd = os.open(book.sidecar.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def sync_sidecars(books, positions, database, apply, report, counts, cache=None):
    for book in books:
        if book.sidecar is None:
            continue  # Do not manufacture KOReader settings for unopened books.
        position = positions.get(book.content_id)
        if not position or position['progress'] is None or position['percentage'] is None:
            continue
        if position['timestamp'] <= book.timestamp:
            continue
        if (book.progress, book.percentage) == (position['progress'], position['percentage']):
            continue
        try:
            if not unchanged(book):
                raise Invalid('sidecar/book changed since scanning; will retry')
            _, info, original = read_sidecar(book.sidecar, raw=True)
            if cache is not None:
                cache.counts['sidecar_read'] += 1
            if signature(info) != book.sidecar_signature:
                raise Invalid('sidecar changed since scanning; will retry')
            updated = patch_sidecar(original, book.filename, position)
            if updated is None:
                continue
            backup = write_sidecar(book, info, original, updated, position['timestamp'], database) if apply else None
            counts['pulled_sidecars'] += 1
            report(f"{'PULL' if apply else 'WOULD PULL'}: {str(book.sidecar)!r} <- {position['percentage']:.2%}")
            if backup:
                report(f'Sidecar backup: {backup}')
        except (OSError, ValueError) as error:
            counts['errors'] += 1
            report(f'SKIP PULL: {str(book.sidecar)!r}: {error}')


def run(args):
    os.umask(0o077)
    root, database = Path(args.directory).resolve(), Path(args.database).resolve()
    if not root.is_dir():
        raise Invalid(f"book directory does not exist: {root}")
    # Validate the account/schema before spending time scanning a large library.
    db = open_database(database, readonly=not args.apply)
    cache = None
    try:
        mappings_only = getattr(args, "mappings_only", False)
        pull = getattr(args, "pull", None)
        if pull is None:
            pull = not mappings_only
        if mappings_only and args.force:
            raise Invalid("--force does not apply to --mappings-only")
        if mappings_only and args.user is None and db.execute("SELECT count(*) FROM users").fetchone()[0] == 0:
            print("No sync accounts yet; register an account before the next mapping scan.")
            return 0
        username = select_user(db, args.user)
        print(f"{'APPLY' if args.apply else 'PREVIEW'}: account={username!r}, directory={str(root)!r}")
        if args.force:
            print("FORCE: sidecar positions may replace newer server progress.")
        if not getattr(args, 'no_cache', False):
            cache_path = Path(getattr(args, 'cache', None) or database.with_name('scan-cache.sqlite3')).resolve()
            if cache_path == database or (cache_path.exists() and cache_path.samefile(database)):
                raise Invalid('the scan cache must be separate from the progress database')
            cache = ScanCache(cache_path, writable=args.apply)
        now = int(time.time())
        books, names, counts = scan(root, print, now, mappings_only, cache)
        if cache is not None:
            cache.flush()
        # Readers remain available. BEGIN IMMEDIATE serializes this batch with
        # API writers, so no update can race our freshness check and commit.
        db.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
        select_user(db, username)
        existing = sync_storage.snapshot(db, username)
        changes = plan(books, names, existing, args.force, now, print, counts, mappings_only,
                       getattr(args, "strict_filenames", False), cache)
        backup = None
        if args.apply and changes:
            backup = backup_database(database)
            for operation in changes:
                sync_storage.apply_link(db, username, operation)
        db.commit()
        if pull:
            positions = sync_storage.snapshot(db, username) if args.apply else sync_storage.project_links(existing, changes)
            sync_sidecars(books, positions, database, args.apply, print, counts, cache)
        verb = "Wrote" if args.apply else "Would write"
        print(f"{verb} {counts['written_ids']} IDs for {counts['changed_books']} books; "
              f"{counts['sidecars']} sidecars scanned, {counts['unchanged_ids']} IDs unchanged, "
              f"{counts['protected']} books protected, {counts['collisions']} filename collisions, "
              f"{counts['errors']} errors; {counts['new_aliases']} new aliases, "
              f"{counts['merged_books']} identities merged, {counts['no_position']} without usable sidecar progress, "
              f"{counts['pulled_sidecars']} sidecars {'updated' if args.apply else 'would be updated'}.")
        if backup:
            print(f"Backup: {backup}")
        if cache is not None:
            cache.flush()
            print(cache.summary())
        if not args.apply:
            print("Preview only. Add --apply to write these updates.")
        return 1 if counts["errors"] or counts["collisions"] else 0
    finally:
        db.close()
        if cache is not None:
            cache.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", nargs="?", default="/mnt/data/Reader_Sync",
                        help="book tree to scan recursively (default: /mnt/data/Reader_Sync)")
    parser.add_argument("--database", default="/var/lib/koreader-sync/sync.sqlite3")
    parser.add_argument("--cache", help="file-content cache (default: scan-cache.sqlite3 beside the progress database)")
    parser.add_argument("--no-cache", action="store_true", help="reread sidecars and recompute needed hashes without reading/writing the cache")
    parser.add_argument("--user", help="existing KOReader sync username; auto-selected if exactly one exists")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes (default is preview only)")
    mode.add_argument("--dry-run", action="store_true", help="explicit preview; no database changes or backup")
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--pull", dest="pull", action="store_true", help="also pull newer server progress into existing sidecars (default for a full import)")
    direction.add_argument("--no-pull", dest="pull", action="store_false", help="leave sidecars unchanged")
    parser.set_defaults(pull=None)
    parser.add_argument("--strict-filenames", action="store_true", help="skip filename links when same-name copies have different/unverifiable content IDs (off by default)")
    parser.add_argument("--mappings-only", action="store_true", help="register keys without importing sidecar positions; also scan supported books without sidecars")
    parser.add_argument("--force", action="store_true", help="make sidecars win even over newer server positions")
    args = parser.parse_args()
    try:
        return run(args)
    except (Invalid, OSError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
