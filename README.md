# koreader-sync

A small, self-hosted KOReader reading-progress server using Flask, Gunicorn,
and Python's built-in SQLite. This fork of
[myelsukov/koreader-sync](https://github.com/myelsukov/koreader-sync) replaces
whole-file JSON storage with transactional SQLite and adds a manual importer
for KOReader sidecar files.

The server retains KOReader's native account authentication and progress API.
The sidecar importer can publish a saved position under both the content-hash
and filename-based document IDs. It does not distribute ebook files.

## Install or update the systemd service

The setup script targets Debian 12+/Ubuntu 22.04+, including an LXC container
running systemd. Python 3.10+ is required. It uses the existing OS account
`herp`, which must have access to your book tree.

Clone or download the **entire repository** into a working directory separate
from `/opt/koreader-sync`. Then run the setup script as root:

```sh
git clone https://github.com/fake-name/koreader-sync.git
cd koreader-sync
sudo bash setup-koreader-sync.sh
```

For subsequent updates:

```sh
git pull --ff-only
sudo bash setup-koreader-sync.sh
```

The installer uses the files in this checkout. It installs the service and the
manual importer together, preserves existing SQLite accounts/progress, and
backs up an existing database before updating. It briefly restarts the service;
no import is run automatically. If setup fails after the service is stopped,
fix the reported error and rerun setup to finish installation/startup.

| Setting | Default |
| --- | --- |
| Service | `koreader-sync.service` |
| OS account | `herp` |
| HTTP listener | `0.0.0.0:8082` |
| Application and virtualenv | `/opt/koreader-sync` |
| SQLite database | `/var/lib/koreader-sync/sync.sqlite3` |
| Setup backups | `/var/lib/koreader-sync/setup-backups/` |
| Import backups | `/var/lib/koreader-sync/import-backups/` |
| Manual import command | `/usr/local/bin/kosync-import` |

These defaults match the earlier `setup-koreader-sync.sh` installation, which
can be upgraded directly. Upstream's `users.json` is **not** automatically
migrated; it is a different format and must not be passed as a SQLite database.

In KOReader, set the custom Progress Sync server to `http://SERVER-IP:8082`,
then register an account or log into an existing one. Use that same sync
account in other compatible clients. This account is separate from the OS
account and any OPDS Basic Auth login.

```sh
systemctl status koreader-sync
journalctl -u koreader-sync -n 50 --no-pager
curl http://127.0.0.1:8082/healthcheck
```

## Manually load reading progress from sidecars

If the SQLite server is already installed and you only want the importer,
run `sudo bash install-kosync-import.sh` from this checkout. This does not
restart the service or modify the server/database.

Close the book on the tablet and allow Syncthing to finish copying its sidecar.
Preview a recursive import, then explicitly apply it:

```sh
sudo -u herp kosync-import /mnt/data/Reader_Sync
sudo -u herp kosync-import /mnt/data/Reader_Sync --apply
```

`/mnt/data/Reader_Sync` is the default if the directory is omitted. The command
selects the sole sync account automatically; with multiple accounts, add
`--user YOUR_KOREADER_USERNAME`. On a root shell, `runuser -u herp --` can replace
`sudo -u herp`.

For each usable sidecar it writes the saved position under:

* **Content ID:** `partial_md5_checksum` from the sidecar, or KOReader's partial
  MD5 calculated from the adjacent book when the saved hash is absent.
* **Filename ID:** MD5 of the exact UTF-8 basename, including its extension.

The command defaults to a read-only preview. An applied batch is transactional
and backed up. Sidecar modification times are used to protect newer server
progress; `--force` lets sidecars win when explicitly desired. Ambiguous
filename IDs and invalid sidecars are reported and skipped. Books and sidecars
are never edited, and Lua files are parsed as data without executing them.

This is a **manual snapshot**. It does not permanently link the two IDs or mirror
subsequent client uploads between them. See
[the importer guide](docs/SIDECAR-IMPORT.md) for collision handling, timestamps,
limitations, exit codes, and recovery.

## Run without systemd

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
KOSYNC_DATABASE="$PWD/sync.sqlite3" .venv/bin/gunicorn \
    --bind 0.0.0.0:8082 --workers 1 --threads 2 --worker-class gthread wsgi:app
```

`KOSYNC_DATABASE` selects the database for WSGI. If omitted, WSGI uses the
system-service database path above. The compatibility entry point
`python koreader-flask.py --help` remains available; it launches Flask's
development server and defaults to `./sync.sqlite3` (or `KOSYNC_DATABASE`).

To use the importer without installing its command wrapper:

```sh
.venv/bin/python import_sidecars.py /path/to/books --database ./sync.sqlite3
```

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
bash -n setup-koreader-sync.sh
bash -n install-kosync-import.sh
```

Tests use temporary databases. They cover authentication, user isolation,
concurrent writes, persistence, importer/parser behavior, rollback, backups,
and retrieving imported progress through the server API.

The original project's invitation to modify it was: “Do whatever you want to”.
