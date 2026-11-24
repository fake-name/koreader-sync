# koreader-sync

A small self-hosted KOReader progress server using Flask, Gunicorn, and SQLite.
This fork of [myelsukov/koreader-sync](https://github.com/myelsukov/koreader-sync)
adds persistent book identities, filename/hash aliases, a sidecar sync tool, and
native systemd installation. KOReader's account authentication and HTTP API
remain compatible. Ebook distribution is handled separately, for example by
OPDS and Syncthing.

## One book, multiple keys

Each user has internal book IDs. Any number of content hashes and filename
hashes can point to one book's progress record. An upload through **any linked
key** immediately becomes visible through every other linked key.

The scanner establishes these relationships from your local files. Normal HTTP
sync requests never guess: an unknown key starts independently until a scan
links it. Matching basenames (including extension and case) identify the same
book even when its content changes. Older hashes and filenames are retained.
This supports web serial EPUBs that are replaced as chapters are added.

Different books with exactly the same basename will therefore be joined by
default, regardless of directory. For a library that cannot use this convention,
`--strict-filenames` retains the more conservative collision checks.

When existing independent records or duplicate sidecars are consolidated,
furthest percentage wins. Equal progress needs no timestamp tiebreaker. Once
linked, ordinary client uploads keep KOReader's normal latest-update behavior,
including intentional backward moves when rereading.

## Install or upgrade

For Debian 12+/Ubuntu 22.04+, including an LXC container with systemd. The
existing OS user `herp` must have access to the book tree. Clone/download the
**whole repository** somewhere separate from `/opt/koreader-sync`:

```sh
git clone https://github.com/fake-name/koreader-sync.git
cd koreader-sync
sudo bash setup-koreader-sync.sh
```

For updates:

```sh
git pull --ff-only
sudo bash setup-koreader-sync.sh
```

Run the **full setup once for the shared-ID upgrade**. It backs up the existing
SQLite database, migrates users/progress transactionally, restarts the server,
and enables the scheduled scanner. The old progress table is retained as
`progress_legacy`; live queries use the `books` and `document_aliases` tables.
A read-only `progress` view exposes progress by document key. Older importers
that write that table directly must be updated along with the server.

| Setting | Default |
| --- | --- |
| Service | `koreader-sync.service` |
| OS account | `herp` |
| HTTP listener | `0.0.0.0:8082` |
| Application and virtualenv | `/opt/koreader-sync` |
| SQLite database | `/var/lib/koreader-sync/sync.sqlite3` |
| Manual command | `/usr/local/bin/kosync-import` |
| Scheduled scanner | `koreader-sync-mappings.timer` |
| Scanner settings | `/etc/default/koreader-sync-mappings` |
| Default book tree | `/mnt/data/Reader_Sync` |

These paths match the previous SQLite installer. Upstream's `users.json` is
not migrated automatically; do not pass JSON as a SQLite database. If setup
fails after stopping the service, fix the reported error and rerun it.

In KOReader, set the Progress Sync custom server to `http://SERVER-IP:8082`,
then register/login with your sync account. This is separate from `herp` and
any OPDS Basic Auth login. Compatible clients use the same server and account.

```sh
systemctl status koreader-sync
journalctl -u koreader-sync -n 50 --no-pager
curl http://127.0.0.1:8082/healthcheck
```

## Manual progress sync

Close the book on the tablet and let Syncthing finish copying its sidecar.
Preview first, then apply:

```sh
sudo -u herp kosync-import /mnt/data/Reader_Sync
sudo -u herp kosync-import /mnt/data/Reader_Sync --apply
```

The sole sync account is selected automatically. If there are multiple, add
`--user YOUR_KOREADER_USERNAME`. The default directory can be omitted. From a
root shell, `runuser -u herp --` is equivalent to `sudo -u herp` (`runuser` is
usually under `/usr/sbin`).

This pass links IDs and synchronizes progress in both directions:

* Duplicate sidecars use the furthest saved percentage.
* Newer sidecar progress can update the server. A newer/equal-time server
  position is protected unless `--force` is specified.
* A strictly newer server position is pulled into an existing sidecar.
  Only reading-position fields are patched; other metadata is preserved.
* A sidecar without a usable percentage still contributes its mappings. It
  can receive server progress, but does not invent a 0% or 100% position.

Use `--no-pull` for a server-only import. `--force` deliberately lets sidecar
progress replace newer server progress. Files that change while being scanned
are skipped for a later retry.

After the full shared-ID upgrade, `sudo bash install-kosync-import.sh` updates
just the importer/shared storage helper without restarting the service or
changing timer configuration. `git pull` alone does not update installed files.

## Scheduled mappings and pulls

Setup enables a scan two minutes after boot and 15 minutes after each completed
scan. It runs as `herp` and does **not upload sidecar positions**. It:

1. Scans sidecars and supported ebook files, including unopened books.
2. Checks file metadata, reuses cached sidecar data/hashes for unchanged files,
   and registers new filename/hash aliases while preserving historical aliases.
3. Pulls newer server positions into existing sidecars for Syncthing to deliver.

It neither creates settings sidecars for unopened books nor modifies EPUB/PDF
content. Initial progress uploads remain manual; normal KOReader auto-sync can
continue handling subsequent tablet uploads.

Scans use a persistent, disposable SQLite cache at
`/var/lib/koreader-sync/scan-cache.sqlite3`. The first applied scan fills it.
Later scans still list directories and stat files, but do not reopen unchanged
sidecars or rehash unchanged ebooks. Cache entries match nanosecond mtime, size,
device/inode, and ctime; they survive service runs and reboots. The journal
reports how many sidecars/hashes were reused versus read/computed.

This also works with an NFS book tree, without depending on remote inotify
events. NFS attribute caching may delay when a change becomes visible. Keep
the cache and progress database on the container's local storage. Server
progress is checked independently on every pass, so a newer server position
can still update an otherwise unchanged sidecar.

For a one-off scan that bypasses the cache, add `--no-cache` to `kosync-import`.
`--cache PATH` overrides its location. Previews may read an existing cache but
never create or update it. An unavailable/corrupt cache produces a warning and
falls back to source reads. To rebuild it, stop the mapping service/timer,
remove **only** `scan-cache.sqlite3`, and restart the timer; it contains no
authoritative progress or aliases.

Edit `/etc/default/koreader-sync-mappings` to change the directory or account:

```sh
KOSYNC_BOOK_DIRECTORY="/mnt/data/Reader_Sync"
KOSYNC_SYNC_USER=""            # auto-select if there is exactly one account
KOSYNC_PULL_SIDECARS=1         # 0 makes scheduled scans strictly mapping-only
KOSYNC_STRICT_FILENAMES=0      # 1 rejects ambiguous same-name/different-hash copies
```

Existing configuration is preserved on setup reruns. With no registered account,
the scanner waits for a later run. With multiple accounts, set the intended one.

```sh
sudo systemctl start koreader-sync-mappings.service   # run now
systemctl list-timers koreader-sync-mappings.timer
journalctl -u koreader-sync-mappings -n 50 --no-pager
sudo systemctl disable --now koreader-sync-mappings.timer  # disable scheduling
```

To change the interval, use `sudo systemctl edit koreader-sync-mappings.timer`:

```ini
[Timer]
OnUnitInactiveSec=
OnUnitInactiveSec=30min
```

Then run `sudo systemctl daemon-reload` and
`sudo systemctl restart koreader-sync-mappings.timer`.

## Backups and behavior

Setup backups live in `setup-backups/`, importer database backups in
`import-backups/`, and original sidecar backups in `sidecar-backups/`, all under
`/var/lib/koreader-sync`. They are retained until manually removed.

Database changes are transactional. Sidecars are backed up and atomically
replaced individually, retaining their mode/owner and unrelated Lua content.
Their modification time is set to the server timestamp to avoid update loops.
A sidecar failure leaves the server update committed; a later scan can retry it.

See [the importer guide](docs/SIDECAR-IMPORT.md) for matching rules, timestamps,
limitations, and recovery. Changing EPUB content can invalidate an old XPointer;
linking hashes does not translate positions between different document layouts.

## Run without systemd

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
KOSYNC_DATABASE="$PWD/sync.sqlite3" .venv/bin/gunicorn \
    --bind 0.0.0.0:8082 --workers 1 --threads 2 --worker-class gthread wsgi:app
```

WSGI uses `KOSYNC_DATABASE`, defaulting to the system-service database path.
`python koreader-flask.py --help` remains available for the development server.
To sync without installing command wrappers:

```sh
.venv/bin/python import_sidecars.py /path/to/books --database ./sync.sqlite3
.venv/bin/python import_sidecars.py /path/to/books --database ./sync.sqlite3 --apply
.venv/bin/python import_sidecars.py /path/to/books --database ./sync.sqlite3 --mappings-only --pull --apply
```

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
bash -n setup-koreader-sync.sh
bash -n install-kosync-import.sh
sh -n bin/kosync-map-scan
```

Tests use temporary databases/files. They cover API authentication and isolation,
concurrency, migration, alias linking, hash churn, backups, parser behavior,
bidirectional progress, atomic sidecar writes, and preservation of annotations.
Cache tests verify that unchanged scans do not read sidecar/ebook contents,
file replacement invalidates cached data, and server-only changes still pull.

The original project's invitation to modify it was: “Do whatever you want to”.
