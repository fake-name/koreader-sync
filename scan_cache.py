"""Disposable file-content cache; progress and aliases live in a separate DB."""
from collections import Counter
import json
from pathlib import Path
import sqlite3

APPLICATION_ID = 0x4B534343  # KSCC, deliberately distinct from the sync database.
VERSION = 1  # Bump when cached parser/hash semantics change.


class ScanCache:
    def __init__(self, path, writable, report=print):
        self.path = Path(path)
        self.writable = writable
        self.report = report
        self.db = None
        self.counts = Counter()
        self.warned = False
        try:
            if not writable and not self.path.exists():
                return
            mode = 'rwc' if writable else 'ro'
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=' + mode, uri=True, timeout=5)
            app_id = self.db.execute('PRAGMA application_id').fetchone()[0]
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            empty = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()
            if empty and app_id == 0 and version == 0 and writable:
                with self.db:
                    self.db.execute('CREATE TABLE cached_files ('
                                    'path TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL, '
                                    'payload TEXT NOT NULL, PRIMARY KEY(path, kind))')
                    self.db.execute(f'PRAGMA application_id = {APPLICATION_ID}')
                    self.db.execute(f'PRAGMA user_version = {VERSION}')
            elif app_id != APPLICATION_ID or version != VERSION:
                raise ValueError('not a compatible scan cache; leave it alone and read source files')
        except (OSError, ValueError, sqlite3.Error) as error:
            self.disable(error)

    def disable(self, error):
        if self.db is not None:
            self.db.close()  # Rolls back any unfinished cache writes only.
            self.db = None
        if not self.warned:
            self.report(f'WARNING: scan cache {str(self.path)!r} unavailable ({error}); reading source files as needed')
            self.warned = True

    def get(self, path, kind, signature):
        if self.db is None:
            return None
        try:
            row = self.db.execute('SELECT signature, payload FROM cached_files WHERE path=? AND kind=?',
                                  (str(path), kind)).fetchone()
            if row and json.loads(row[0]) == list(signature):
                return json.loads(row[1])
        except (ValueError, sqlite3.Error) as error:
            self.disable(error)
        return None

    def put(self, path, kind, signature, payload):
        if self.db is None or not self.writable:
            return
        try:
            self.db.execute('INSERT INTO cached_files VALUES (?, ?, ?, ?) '
                            'ON CONFLICT(path, kind) DO UPDATE SET signature=excluded.signature, payload=excluded.payload',
                            (str(path), kind, json.dumps(signature), json.dumps(payload)))
        except (ValueError, sqlite3.Error) as error:
            self.disable(error)

    def flush(self):
        if self.db is not None:
            try:
                self.db.commit()
            except sqlite3.Error as error:
                self.disable(error)

    def close(self):
        # Explicit flushes mark successful cache batches. Exceptions may lose
        # cached work, but must never change the source files or progress DB.
        if self.db is not None:
            self.db.close()
            self.db = None

    def summary(self):
        return (f"Cache: {self.counts['sidecar_hit']} sidecars reused, {self.counts['sidecar_read']} read; "
                f"{self.counts['hash_hit']} book hashes reused, {self.counts['hash_read']} computed.")
