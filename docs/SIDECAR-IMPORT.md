# Book aliases and sidecar synchronization

`kosync-import` is the manual tool. `kosync-map-scan` is its scheduled wrapper.
Both use Python 3.10+'s standard library; no Lua interpreter or network service
is needed to scan local files. Install/update from the whole repository using
`sudo bash setup-koreader-sync.sh` first. This migrates the earlier SQLite schema
and installs the shared storage module with the server and importer.

## Commands

Run as the OS user `herp`, which can access both the book tree and database:

```sh
# Preview bidirectional progress sync and new alias relationships:
sudo -u herp kosync-import /mnt/data/Reader_Sync
# Apply the previewed type of operation, rechecking current files/database:
sudo -u herp kosync-import /mnt/data/Reader_Sync --apply
# Only build/refresh mappings; never import or write sidecar positions:
sudo -u herp kosync-import /mnt/data/Reader_Sync --mappings-only --apply
# Refresh mappings and pull newer server positions; no sidecar position uploads:
sudo -u herp kosync-import /mnt/data/Reader_Sync --mappings-only --pull --apply
# Import positions and mappings, leaving sidecar files alone:
sudo -u herp kosync-import /mnt/data/Reader_Sync --no-pull --apply
```

The directory defaults to `/mnt/data/Reader_Sync`. The database defaults to
`/var/lib/koreader-sync/sync.sqlite3`, overridable with `--database PATH`.
The sole existing sync account is selected automatically; otherwise specify
`--user YOUR_KOREADER_USERNAME`. This is not the OS user or OPDS Basic Auth login.
No password is needed for a local tool already granted access to the database.

No changes occur without `--apply`. Preview reports both database changes and
sidecar pulls, and creates no backup files. A nonexistent database is rejected.
An older schema is rejected with instructions to run full setup first.

The file cache defaults to `scan-cache.sqlite3` beside the progress database.
Use `--cache PATH` to override it, or `--no-cache` to bypass it completely for
that invocation. Previews never create or update cache files. An installation
already using shared book IDs only needs `git pull --ff-only` followed by
`sudo bash install-kosync-import.sh` to add caching to its existing timer.

## Identity matching

The database has one position per internal `book_id`, scoped to a user. The
`document_aliases` table connects protocol document keys to those IDs:

* Saved content key: the sidecar's `partial_md5_checksum`, as KOReader sends it.
* Current content key: KOReader's partial MD5 calculated from the book during
  a mapping scan, sampling 1 KiB at offsets 0, 1 KiB, 4 KiB, ... 1 GiB.
* Filename key: MD5 of the exact UTF-8 basename, including its extension.
  Directories are not part of this ID; spelling and case matter.

A saved and current hash can differ for an updated web serial. Both become
aliases. Same-name copies with different hashes are treated as versions of the
same book by default. Copies with different names can also be linked by a shared
hash or an already registered identity. Old aliases are retained when files are
renamed, replaced, or removed.

The HTTP server does not infer relationships from an unknown key. It starts an
independent identity until a scan supplies the association. Once linked,
uploading through any key updates the one live position immediately.

Different unrelated books sharing the same basename will be joined by default.
`--strict-filenames` skips a filename association when copies have different or
unverifiable content identities. Its unambiguous content keys still work. This
option is intentionally off for libraries that expect filename-based hash churn.

## Sidecar handling

Scans `*.sdr/metadata.<extension>.lua` recursively. Conventional sidecar names
supply the filename. Hash-named sidecar directories can use `doc_path`'s basename;
the absolute Android path need not exist on the server.

A normal pass imports `last_xpointer` for reflowable books or `last_page` for
paged books, together with `percent_finished` in 0..1. Missing/invalid percentages
or locations do not reject identity mapping. For example, a cover-only sidecar
can be linked and later receive a server position, without inventing an initial
percentage. The separate `summary.status` finished/reading label is preserved
and is not converted into a position.

The mapping-only pass also finds supported files without sidecars: EPUB, PDF,
DjVu, CBZ, CBT, MOBI, AZW/AZW3, and FB2. It does not create settings sidecars for
those books. It calculates current hashes even when cached sidecar hashes exist.

`.old` backups, legacy sidecars, Syncthing conflict files, `.stversions`,
`.stfolder`, and symlinked directories are excluded. Sidecar symlinks are rejected.
Lua files are parsed as literal data, never executed. Size/nesting/token limits
bound malformed inputs. Unsupported executable Lua is reported and skipped.

## Progress decisions

* Duplicate sidecars use the furthest percentage. Equal progress is merged;
  modification time is not a position tiebreaker. If equally far copies contain
  different exact location strings, the first encountered location is retained.
* When previously independent server records are joined, the furthest existing
  progress is retained. Normal client uploads after linking retain the usual
  latest-update behavior and may intentionally move backward.
* For a manual position upload, a different server position with a newer or equal
  timestamp is protected. Alias relationships are still registered. `--force`
  explicitly lets sidecar positions win, stamped with import time.
* Server progress is pulled into an existing sidecar only if the server timestamp
  is strictly newer than that file's modification time. Matching positions are
  left untouched, even if timestamps differ. `--no-pull` disables file updates.
* A normal manual pass pulls by default. `--mappings-only` pulls only with
  `--pull`; it never uploads sidecar positions, and rejects `--force`.

Sidecar modification times are proxies for saved reading times, not exact page
turn timestamps. Preserved Syncthing mtimes and correct clocks matter. A metadata
edit can make an old location appear newer. Invalid/future sidecar times prevent
position uploads but do not prevent identity mapping. Wait for tablet saves and
Syncthing to finish before a manual sync.

Pulls require a KOReader-style XPointer beginning with `/`, or a positive integer
page number for paged documents. An incompatible position such as a raw EPUB CFI
is reported and leaves the sidecar alone. Linking keys cannot translate an old
position to a modified EPUB layout. The scanner does not update book content,
annotations, highlights, font settings, or the reading/finished status label.

## Scheduled operation

Full setup enables `koreader-sync-mappings.timer`. It runs two minutes after boot
and 15 minutes after each completed scan, with no overlapping executions of the
same service. Each pass refreshes identities and, by default, pulls newer server
positions into existing sidecars. It never uploads stale sidecar positions.

`/etc/default/koreader-sync-mappings` configures `KOSYNC_BOOK_DIRECTORY`,
`KOSYNC_DATABASE`, and `KOSYNC_SYNC_USER`. Empty username selects the sole account.
`KOSYNC_PULL_SIDECARS=0` disables scheduled pulls. `KOSYNC_STRICT_FILENAMES=1`
restores conservative collision handling. Existing configuration is preserved
on upgrades. No registered accounts means waiting for the next run; multiple
accounts require choosing the intended one.

```sh
sudo systemctl start koreader-sync-mappings.service
journalctl -u koreader-sync-mappings -n 50 --no-pager
systemctl list-timers koreader-sync-mappings.timer
```

## Writes, backups, and recovery

Applied database changes are a single transaction, serialized with API writes.
Before a changed batch, a consistent database backup is made in
`/var/lib/koreader-sync/import-backups/`. No-change scans make no database backup.
Setup also creates `setup-backups/` before an upgrade and retains the old table
as `progress_legacy` when migrating from independent document records.

Sidecars are updated after the database transaction, individually. Each original
is backed up under `/var/lib/koreader-sync/sidecar-backups/<path-digest>/`, with
`source-path.txt` identifying the original path. Only top-level position values
are replaced/inserted; the remaining Lua text stays intact. The new file is
validated, fsynced, and atomically renamed into place. Owner/mode are retained,
and mtime becomes the server timestamp to prevent upload loops.

Files changed during the scan are skipped instead of overwritten. Database and
filesystem writes cannot be one atomic transaction: if a sidecar pull fails,
the committed server changes remain, and a later pass retries the file. A server
update arriving during a pull can be delivered by the next pass. No background
process can observe an unsaved reading position still held only in a reader's
memory.

Backups are private and retained until manually removed. To restore the database,
stop `koreader-sync.service` and both mapping units, replace `sync.sqlite3` with
the desired backup owned by `herp` with mode `0600`, then restart the server/timer.
That rolls back changes made after the backup. For a sidecar, stop the timer and
avoid simultaneous reader/Syncthing changes while restoring the saved original
at its recorded path with its original owner/mode.

Exit status: 0 = success; 1 = completed with individual file/collision errors;
2 = fatal error. Missing saved progress is informational, not an error. The
oneshot service accepts status 1 while keeping the warnings in its journal.

## Skipping unchanged file contents

The timer remains a 15-minute oneshot job. Each pass enumerates the directory
tree to detect additions/deletions and checks file metadata. It **does not**
use a directory's mtime as evidence that all descendants are unchanged.

Successful parsed sidecar fields and partial book hashes are cached separately
by absolute path. A cache hit requires the same nanosecond mtime, file size,
device, inode, and ctime. Mtime comparisons are for equality, so a replacement
carrying an older source timestamp is still detected. Inode/ctime also catch
ordinary atomic replacements with the same size and mtime. An EPUB change
rehashes that EPUB; an unrelated sidecar change does not rehash the book.
Unopened books are cached too. Failed reads are retried on subsequent scans.

The cache persists across timer invocations and reboots. It contains only the
sidecar fields used for matching/progress, not annotations or raw Lua content.
No-change scans do not rewrite it. Private permissions match the importer data
directory. A corrupt/unavailable cache falls back to source reads with a warning;
it is disposable and can be removed while the scanner is stopped. A one-off
`--no-cache` run leaves an existing cache untouched.

Progress/aliases are always checked against the live SQLite database; there are
no HTTP fetches. A newer, different server position still opens the affected
sidecar for its exact backup and atomic rewrite. The next scan refreshes that
changed sidecar's cached parse once. Newer timestamps with an identical position
do not reopen it. File signatures are rechecked before committing mappings and
before replacing a sidecar, just as with uncached scans.

This works with local filesystems (including filesystems on zvols) and NFS book
trees. It uses ordinary directory/stat operations, not inotify notifications,
so remote NFS changes are found once visible through the mount's attribute
cache. Metadata polling still incurs directory/stat traffic over NFS; avoiding
that too would require notifications from the machine writing the files. Keep
the SQLite progress database and scan cache on local storage.

Syncthing [preserves file modification times](https://docs.syncthing.net/users/faq.html#what-things-are-synced)
and [uses file metadata to decide when to rehash](https://docs.syncthing.net/users/syncing.html#scanning).
Directory mtimes are not preserved. As with any metadata-based cache, changes
that leave every checked attribute identical cannot be detected without reading
the contents; `--no-cache` provides that explicit bypass.

## References

Verified against KOReader commit `55471ca62fd5f405d2facdf2a6c6eb940092db50`:

* [Document keys](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/plugins/kosync.koplugin/main.lua).
* [Sidecar naming](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/docsettings.lua).
* [Partial MD5](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/util.lua).
* [Reflowable positions](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/apps/reader/modules/readerrolling.lua).
* [Paged positions](https://github.com/koreader/koreader/blob/55471ca62fd5f405d2facdf2a6c6eb940092db50/frontend/apps/reader/modules/readerpaging.lua).
