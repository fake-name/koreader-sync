#!/usr/bin/env bash
# Add a manual importer to the existing SQLite server; no restart or network needed.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root: bash install-kosync-import.sh' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
app_dir=/opt/koreader-sync
database=/var/lib/koreader-sync/sync.sqlite3
[[ -f "$app_dir/koreader_sync.py" && -f "$database" ]] || {
    echo 'The existing SQLite KOReader sync installation was not found.' >&2; exit 1;
}
id herp >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
python3 - "$source_dir/import_sidecars.py" <<'CHECK'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
for source in (path, path.with_name('sync_storage.py'), path.with_name('scan_cache.py')):
    compile(source.read_text(), str(source), 'exec')
CHECK
runuser -u herp -- python3 - "$database" <<'CHECK_DB'
import pathlib, sqlite3, sys
db = sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True)
try:
    db.execute('SELECT username FROM users LIMIT 1').fetchall()
    expected = {'username', 'document', 'progress', 'percentage', 'device', 'device_id', 'timestamp'}
    actual = {row[1] for row in db.execute('PRAGMA table_info(progress)')}
    if not expected <= actual:
        sys.exit('This is not the expected SQLite progress schema.')
    if db.execute('PRAGMA user_version').fetchone()[0] != 1:
        sys.exit('Run setup-koreader-sync.sh first to upgrade the server and database for shared book identities.')
finally:
    db.close()
CHECK_DB
temporary=$(mktemp "$app_dir/.import-sidecars.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
install -o root -g root -m 0644 "$source_dir/import_sidecars.py" "$temporary"
install -o root -g root -m 0644 "$source_dir/sync_storage.py" "$app_dir/sync_storage.py"
install -o root -g root -m 0644 "$source_dir/scan_cache.py" "$app_dir/scan_cache.py"
mv -f -- "$temporary" "$app_dir/import_sidecars.py"
install -o root -g root -m 0644 "$source_dir/docs/SIDECAR-IMPORT.md" "$app_dir/SIDECAR-IMPORT.md"
install -o root -g root -m 0755 "$source_dir/bin/kosync-import" /usr/local/bin/kosync-import
printf '\nInstalled manual importer; no import has been performed.\n\n'
printf 'Preview:\n  runuser -u herp -- kosync-import /mnt/data/Reader_Sync\n\n'
printf 'Import:\n  runuser -u herp -- kosync-import /mnt/data/Reader_Sync --apply\n\n'
printf 'The sole KOReader account is selected automatically. For multiple accounts, add --user NAME.\n'
printf 'The next applied scan populates scan-cache.sqlite3; later scans reuse unchanged sidecars and book hashes.\n'
