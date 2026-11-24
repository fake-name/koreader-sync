# Manual KOReader sidecar import

Adds a `kosync-import` command to the Python/SQLite server installed by
`setup-koreader-sync.sh`. No packages, network access, service restart, timer,
password, or new account are needed. The existing HTTP API is unchanged.

## Install and run

Inside the LXC container, as root:

```sh
git clone https://github.com/fake-name/koreader-sync.git
cd koreader-sync
bash install-kosync-import.sh
runuser -u herp -- kosync-import /mnt/data/Reader_Sync
runuser -u herp -- kosync-import /mnt/data/Reader_Sync --apply
```

The first command previews; the second imports. Both default to that directory,
so `runuser -u herp -- kosync-import --apply` also works. Run with the OS user
`herp` so the books and database are accessible and new database files retain
the right owner. If invoking from a sudo-enabled login, `sudo -u herp` works too.

If there is exactly one KOReader sync account, it is selected automatically.
Otherwise add `--user YOUR_KOREADER_USERNAME`. This is the sync account, not the
OS user or OPDS login. No credentials are needed because this local command
already has direct access to the server database.

Close the book on the tablet, allow Syncthing to finish, then import. Unsaved
positions from a currently open book cannot be recovered from a sidecar.

## What it imports

Recursively scans `*.sdr/metadata.<extension>.lua`, including EPUB and PDF
sidecars. Copies `last_xpointer` (reflowable books), or `last_page` (paged books),
and `percent_finished` into the existing `progress` table under both identifiers:

* Content ID: the saved `partial_md5_checksum`, exactly as KOReader sends it.
* Filename ID: MD5 of the UTF-8 basename, **including its extension**. Case and
  spelling matter; directories are not part of the ID.

The basename comes from the conventional sidecar directory/metadata suffix.
For hash-named sidecar directories without an adjacent book, `doc_path` supplies
the basename; the Android absolute path does not have to exist on the server.
When the saved content hash is missing, the adjacent book is required and the
KOReader partial-MD5 algorithm is calculated (not whole-file MD5).

The saved hash is deliberately preferred over recalculating it: if a web serial
EPUB was replaced after its last reading session, the saved position and hash
describe the previously read version. Filename matching still addresses the
same filename. This does not translate an old XPointer to a changed EPUB layout.

This is a manual snapshot import. **It does not permanently link the two IDs:**
subsequent normal client uploads still update only the ID that client uses.
Run the importer again after the tablet's newer sidecars arrive. It reads books
and sidecars without editing either, and does not send data to outside services.

## Conflict handling

* Defaults to a read-only preview; use `--apply` to write.
* Uses sidecar modification time as a proxy for the position's saved time. This
  is not an exact page-turn timestamp. Preserved Syncthing mtimes and correct
  clocks matter; a later metadata-only edit or `touch` can make old progress
  look newer. Files timestamped in the future are skipped.
* If **either** matching server record has different progress with a newer or
  equal timestamp, skips that book entirely. This also avoids putting stale
  progress under a previously missing partner ID. The comparison and write
  happen in the same SQLite transaction, serialized with normal API writers.
* Repeated imports leave identical progress unchanged. A newer sidecar can
  legitimately move progress backward (for example, rereading a book).
* `--force` intentionally lets the sidecar replace newer server positions,
  timestamped at import time. Preview it first with `--force`, then use
  `--force --apply` if that is the desired outcome.
* Duplicate filenames in different scanned locations skip the filename ID and
  still import unambiguous content IDs. Even identical copies are conservatively
  treated this way. Collisions outside the scanned tree cannot be detected.
* Copies sharing a content hash use the most recently saved sidecar. Conflicting
  positions with the same newest timestamp are skipped, rather than guessed.
* Missing positions/percentages, invalid hashes, unreadable/changing files, and
  malformed Lua data are reported and skipped. There is no invented position
  or completion percentage based on a book's “finished” label.

Only current `metadata.*.lua` files are considered. `.old` backups, legacy
sidecars, Syncthing conflict files, `.stversions`, `.stfolder`, and symlinked
directories are not scanned. Sidecar symlinks are rejected. The Lua reader
accepts literal data only; it never executes the files. Size, nesting, and token
limits keep malformed sidecars bounded. This supports normal KOReader dumps,
not arbitrary executable Lua programs.

## Backups and recovery

Before a batch with changes, the command makes a consistent SQLite backup in:

```
/var/lib/koreader-sync/import-backups/
```

The exact filename is printed after a successful import. Backups are private
and retained; remove old ones manually when no longer needed. The whole batch
is committed atomically. Fatal write failures roll it back; per-book scan
errors allow the remaining valid books to import.

To restore, stop `koreader-sync.service`, replace `sync.sqlite3` with the chosen
backup owned by `herp` and mode `0600`, then start the service. Restoring a backup
also rolls back all users/progress changes made after it was taken.

Exit status: 0 = success (including protected newer progress or no changes),
1 = completed with skipped malformed/unreadable files or filename collisions,
2 = fatal error. `--database PATH` supports another database with this same
schema. A nonexistent path is rejected, never created.

Tests: install `requirements.txt`, then run `python3 -m unittest discover -s tests -v`
from the repository root. Tests use temporary databases and include integration
with this repository's Flask sync API.

## KOReader references

Verified against KOReader commit `55471ca62fd5f405d2facdf2a6c6eb940092db50` on 2026-11-23:

* [Filename/content IDs](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/plugins/kosync.koplugin/main.lua): `getFileNameDigest`, `getFileDigest`.
* [Sidecar naming](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/docsettings.lua): `getSidecarDir`, `getSidecarFilename`.
* [Partial MD5](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/util.lua): `partialMD5`.
* [EPUB position](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/apps/reader/modules/readerrolling.lua): `onSaveSettings`.
* [Paged position](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/apps/reader/modules/readerpaging.lua): `onSaveSettings`.

Validation includes the Flask sync API, rollback, backups, account isolation,
collision handling, and newer-progress protection. The parser was also checked against a fixture emitted
by KOReader's actual `dump.lua` serializer. Installation has not been run on your
LXC container.
