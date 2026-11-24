import os
import koreader_sync

koreader_sync.USERSDB = os.environ.get(
    "KOSYNC_DATABASE", "/var/lib/koreader-sync/sync.sqlite3"
)
koreader_sync.initDb()
app = koreader_sync.app
