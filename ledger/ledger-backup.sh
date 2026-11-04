#!/bin/bash
# 家庭账本每日备份
#
# 用 SQLite 在线备份 API 取一致性快照（比直接 tar 正在写的 db + wal 安全），
# 再连同程序一起打包，恢复时一个包就够。
set -euo pipefail

SRC=/opt/ledger
DEST=/opt/backups
KEEP_DAYS=14
TS=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/ledger-$TS.tar.gz"

mkdir -p "$DEST"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

python3 - "$TMP" <<'PY'
import sqlite3, sys, pathlib
tmp = pathlib.Path(sys.argv[1])
src = sqlite3.connect("file:/opt/ledger/data/ledger.db?mode=ro", uri=True)
dst = sqlite3.connect(str(tmp / "ledger.db"))
src.backup(dst)
dst.close()
src.close()
PY

cp -a "$SRC/server.py" "$SRC/www/index.html" "$TMP/" 2>/dev/null || true

tar czf "$OUT" -C "$TMP" .
chmod 600 "$OUT"

find "$DEST" -name 'ledger-*.tar.gz' -type f -mtime +"$KEEP_DAYS" -delete

echo "$(date '+%F %T') backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
