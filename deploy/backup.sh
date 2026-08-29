#!/bin/bash
# sqlite online backup of a2a-hub; keep 14 days
set -e
SRC=/home/user/web_try/a2a/data.sqlite3
DIR=/home/user/web_try/a2a/backups
STAMP=$(date +%Y%m%d-%H%M%S)
python3 - "$SRC" "$DIR/backup-$STAMP.sqlite3" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
find "$DIR" -name "backup-*.sqlite3" -mtime +14 -delete
echo "backup ok: $DIR/backup-$STAMP.sqlite3"
