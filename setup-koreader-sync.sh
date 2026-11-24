#!/usr/bin/env bash
# Debian 12+/Ubuntu 22.04+: install this checkout as user herp.
# Run as root: bash setup-koreader-sync.sh
# HTTP on port 8082, native KOReader authentication, SQLite storage.
# Existing users/progress are retained; the importer remains manual.
set -Eeuo pipefail
umask 022

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
app_dir=/opt/koreader-sync
data_dir=/var/lib/koreader-sync
unit_file=/etc/systemd/system/koreader-sync.service

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "Setup stopped at line %s. Check: journalctl -u koreader-sync -n 50\n" "$LINENO" >&2' ERR

[[ $EUID -eq 0 ]] || die 'Run this script as root or with sudo bash.'
[[ -d /run/systemd/system ]] || die 'Run inside the LXC container with systemd running.'
command -v apt-get >/dev/null || die 'This script is for Debian/Ubuntu.'
id herp >/dev/null 2>&1 || die 'The service account herp must already exist.'
service_group=$(id -gn herp)
[[ "$source_dir" != "$app_dir" ]] || die 'Run from a separate checkout, not /opt/koreader-sync.'
for file in koreader_sync.py sync_storage.py koreader-flask.py wsgi.py requirements.txt \
            import_sidecars.py install-kosync-import.sh bin/kosync-import \
            bin/kosync-map-scan systemd/koreader-sync-mappings.service \
            systemd/koreader-sync-mappings.timer systemd/koreader-sync-mappings.default \
            systemd/koreader-sync.service docs/SIDECAR-IMPORT.md; do
    [[ -f "$source_dir/$file" ]] || die "Missing $file; download or clone the entire repository."
done

apt-get update
apt-get install -y python3 python3-venv curl ca-certificates
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die 'Python 3.10 or newer is required.'
python3 - "$source_dir" <<'CHECK_SOURCE'
import pathlib, sys
source = pathlib.Path(sys.argv[1])
for name in ('koreader_sync.py', 'sync_storage.py', 'koreader-flask.py', 'wsgi.py', 'import_sidecars.py'):
    compile((source / name).read_text(encoding='utf-8'), name, 'exec')
CHECK_SOURCE

install -d -o root -g root -m 0755 "$app_dir"
install -d -o herp -g "$service_group" -m 0700 "$data_dir"
if [[ -f "$data_dir/sync.sqlite3" ]]; then
    # SQLite's backup API works while the existing service is still running.
    # It also rejects an incompatible JSON database rather than overwriting it.
    python3 - "$data_dir/sync.sqlite3" <<'BACKUP'
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import uuid

os.umask(0o077)
path = Path(sys.argv[1])
directory = path.parent / 'setup-backups'
directory.mkdir(mode=0o700, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
target = directory / f'{path.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak'
with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
    if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        sys.exit('Database integrity check failed; setup has not stopped the service.')
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
print(f'Existing database backed up to {target}')
BACKUP
    chown herp:"$service_group" "$data_dir/sync.sqlite3"
    chmod 0600 "$data_dir/sync.sqlite3"
fi

# Resolve dependencies before stopping the service. The pinned versions are
# the same as the earlier installer, so its existing environment can be reused.
python3 -m venv "$app_dir/venv"
"$app_dir/venv/bin/python" -m pip install --disable-pip-version-check -r "$source_dir/requirements.txt"

if systemctl cat koreader-sync.service >/dev/null 2>&1; then
    systemctl stop koreader-sync.service
fi
if systemctl cat koreader-sync-mappings.timer >/dev/null 2>&1; then
    systemctl stop koreader-sync-mappings.timer koreader-sync-mappings.service
fi
for file in koreader_sync.py sync_storage.py koreader-flask.py wsgi.py requirements.txt; do
    install -o root -g root -m 0644 "$source_dir/$file" "$app_dir/$file"
done
install -o root -g root -m 0644 "$source_dir/systemd/koreader-sync.service" "$unit_file"

# Migrate independent document keys into book identities, preserving accounts
# and positions. The original progress table is retained as progress_legacy.
runuser -u herp -- env PYTHONPATH="$app_dir" KOSYNC_DATABASE="$data_dir/sync.sqlite3" \
    "$app_dir/venv/bin/python" -c 'import koreader_sync; koreader_sync.initDb()'
chmod 0600 "$data_dir/sync.sqlite3"
bash "$source_dir/install-kosync-import.sh"
for unit in koreader-sync-mappings.service koreader-sync-mappings.timer; do
    install -o root -g root -m 0644 "$source_dir/systemd/$unit" "/etc/systemd/system/$unit"
done
install -o root -g root -m 0755 "$source_dir/bin/kosync-map-scan" /usr/local/bin/kosync-map-scan
if [[ ! -e /etc/default/koreader-sync-mappings ]]; then
    install -o root -g root -m 0644 "$source_dir/systemd/koreader-sync-mappings.default" /etc/default/koreader-sync-mappings
fi
"$app_dir/venv/bin/python" -m py_compile "$app_dir/koreader_sync.py" "$app_dir/wsgi.py" "$app_dir/import_sidecars.py"

systemd-analyze verify "$unit_file" /etc/systemd/system/koreader-sync-mappings.service /etc/systemd/system/koreader-sync-mappings.timer
systemctl daemon-reload
systemctl enable koreader-sync.service
systemctl restart koreader-sync.service

ready=false
for attempt in {1..20}; do
    if curl --noproxy '*' --silent --fail --max-time 2 \
        http://127.0.0.1:8082/healthcheck >/dev/null; then
        ready=true
        break
    fi
    sleep 1
done
[[ "$ready" == true ]] || die 'The sync server did not become healthy on port 8082.'
systemctl is-active --quiet koreader-sync.service
status=$(curl --noproxy '*' --silent --show-error --max-time 5 \
    --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8082/users/auth)
[[ "$status" == 401 ]] || die "Expected native authentication to reject an anonymous request; got $status."
systemctl enable --now koreader-sync-mappings.timer

container_addr=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
printf '\nReady: http://%s:8082\n' "${container_addr:-CONTAINER-IP}"
printf 'KOReader: set the custom Progress Sync server URL; use your existing account or register a new one.\n'
printf 'Sync data: /var/lib/koreader-sync/sync.sqlite3\n'
printf 'Preview sidecar import: runuser -u herp -- kosync-import /mnt/data/Reader_Sync\n'
printf 'Apply sidecar import:   runuser -u herp -- kosync-import /mnt/data/Reader_Sync --apply\n'
printf 'Logs: journalctl -u koreader-sync -n 50 --no-pager\n'
printf 'Scheduled scan: every 15 minutes; refreshes aliases and pulls newer server progress into sidecars.\n'
printf 'Sidecar progress uploads remain manual. Sidecar backups: /var/lib/koreader-sync/sidecar-backups/\n'
printf 'Mapping settings: /etc/default/koreader-sync-mappings\n'
printf 'Mapping scan logs: journalctl -u koreader-sync-mappings -n 50 --no-pager\n'
