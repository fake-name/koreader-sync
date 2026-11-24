"""SQLite book identities and document aliases shared by the server and scanner."""
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path
import sqlite3
import uuid

POSITION_FIELDS = ("progress", "percentage", "device", "device_id", "timestamp")


@contextmanager
def database(path, readonly=False, create=False):
    mode = "ro" if readonly else ("rwc" if create else "rw")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=" + mode, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db(path):
    with database(path, create=True) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] > 1:
            raise sqlite3.DatabaseError("Database schema is newer than this server")
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY NOT NULL, userkey TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS books (
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            book_id TEXT NOT NULL,
            progress TEXT, percentage REAL, device TEXT, device_id TEXT,
            timestamp INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (username, book_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS document_aliases (
            username TEXT NOT NULL, document TEXT NOT NULL, book_id TEXT NOT NULL,
            PRIMARY KEY (username, document),
            FOREIGN KEY (username, book_id) REFERENCES books(username, book_id)
                ON DELETE CASCADE)""")
        db.execute("CREATE INDEX IF NOT EXISTS aliases_by_book ON document_aliases(username, book_id)")
        old = db.execute("SELECT type FROM sqlite_master WHERE name = 'progress'").fetchone()
        if old and old[0] == "table":
            # Each previously unrelated key starts as its own identity. Only
            # the importer may establish that two keys describe the same book.
            for row in db.execute("SELECT * FROM progress").fetchall():
                book_id = uuid.uuid4().hex
                db.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (row["username"], book_id, *(row[key] for key in POSITION_FIELDS)))
                db.execute("INSERT INTO document_aliases VALUES (?, ?, ?)",
                           (row["username"], row["document"], book_id))
            # Retain the original rows for recovery, in addition to setup's
            # consistent backup. This archive is never read for live sync.
            db.execute("ALTER TABLE progress RENAME TO progress_legacy")
        db.execute("""CREATE VIEW IF NOT EXISTS progress AS
            SELECT a.username, a.document, b.progress, b.percentage, b.device,
                   b.device_id, b.timestamp
            FROM document_aliases AS a JOIN books AS b
                ON a.username = b.username AND a.book_id = b.book_id""")
        db.execute("PRAGMA user_version = 1")


def snapshot(db, username):
    return {row["document"]: dict(row) for row in db.execute("""
        SELECT a.document, b.book_id, b.progress, b.percentage, b.device, b.device_id, b.timestamp
        FROM document_aliases AS a JOIN books AS b
            ON a.username = b.username AND a.book_id = b.book_id
        WHERE a.username = ? ORDER BY a.document
    """, (username,))}


def get_position(db, username, document):
    row = db.execute("""SELECT document, progress, percentage, device, device_id, timestamp
        FROM progress WHERE username = ? AND document = ?
        AND progress IS NOT NULL AND percentage IS NOT NULL""", (username, document)).fetchone()
    return dict(row) if row else {}


def put_position(db, username, document, position, timestamp):
    # Caller holds a write transaction, serializing alias creation/merging
    # with uploads and periodic scans. Unknown keys remain independent.
    row = db.execute("SELECT book_id FROM document_aliases WHERE username = ? AND document = ?",
                     (username, document)).fetchone()
    if row:
        book_id = row[0]
    else:
        book_id = uuid.uuid4().hex
        db.execute("INSERT INTO books(username, book_id) VALUES (?, ?)", (username, book_id))
        db.execute("INSERT INTO document_aliases VALUES (?, ?, ?)", (username, document, book_id))
    db.execute("""UPDATE books SET progress=?, percentage=?, device=?, device_id=?, timestamp=?
        WHERE username=? AND book_id=?""",
        (position.get("progress"), position.get("percentage"), position.get("device"),
         position.get("device_id"), timestamp, username, book_id))


def apply_link(db, username, operation):
    """Apply one reviewed identity merge and optional position update."""
    book_id = operation["book_id"]
    db.execute("INSERT OR IGNORE INTO books(username, book_id) VALUES (?, ?)", (username, book_id))
    for previous in operation["previous_ids"]:
        if previous != book_id:
            db.execute("UPDATE document_aliases SET book_id=? WHERE username=? AND book_id=?",
                       (book_id, username, previous))
            db.execute("DELETE FROM books WHERE username=? AND book_id=?", (username, previous))
    for document in operation["aliases"]:
        db.execute("INSERT OR IGNORE INTO document_aliases VALUES (?, ?, ?)",
                   (username, document, book_id))
    position = operation["position"]
    if position is not None:
        db.execute("""UPDATE books SET progress=?, percentage=?, device=?, device_id=?, timestamp=?
            WHERE username=? AND book_id=?""",
            (*(position[key] for key in POSITION_FIELDS), username, book_id))


def project_links(existing, operations):
    """Project a preview without modifying the database or its metadata."""
    projected = {key: dict(row) for key, row in existing.items()}
    positions = {}
    keys_by_book = defaultdict(set)
    for key, row in existing.items():
        positions[row["book_id"]] = row
        keys_by_book[row["book_id"]].add(key)
    for operation in operations:
        target = operation["book_id"]
        current = positions.get(target)
        position = operation["position"] or current or dict(
            progress=None, percentage=None, device=None, device_id=None, timestamp=0)
        aliases = set(operation["aliases"]).union(
            *(keys_by_book[book_id] for book_id in operation["previous_ids"]))
        for document in aliases:
            projected[document] = dict(document=document, book_id=target,
                                      **{key: position[key] for key in POSITION_FIELDS})
        positions[target] = position
        keys_by_book[target] = aliases
    return projected
