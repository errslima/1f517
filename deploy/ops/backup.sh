#!/usr/bin/env bash
# Online SQLite snapshot of both environments. Uses .backup, which is safe
# against a live WAL database - never copy the .db file directly while the
# service is running.
set -euo pipefail
DEST=/srv/qoc/backups
RETAIN_DAYS=14
mkdir -p "$DEST"
stamp=$(date -u +%Y%m%dT%H%M%SZ)

for env in prod dev; do
  src="/srv/qoc/data-$env/qoc.db"
  [ -f "$src" ] || continue
  out="$DEST/qoc-$env-$stamp.db"
  sqlite3 "$src" ".backup '$out'"
  # integrity-check the snapshot, not the live db: a corrupt backup that
  # reports success is worse than no backup
  if [ "$(sqlite3 "$out" 'PRAGMA integrity_check;')" != "ok" ]; then
    echo "BACKUP FAILED integrity_check: $out" >&2
    rm -f "$out"
    exit 1
  fi
  gzip -f "$out"
  echo "snapshot: $out.gz ($(du -h "$out.gz" | cut -f1))"
done

find "$DEST" -name 'qoc-*.db.gz' -mtime +$RETAIN_DAYS -delete
ln -sfn "$DEST/qoc-prod-$stamp.db.gz" "$DEST/latest-prod.db.gz"
echo "retained: $(find "$DEST" -name 'qoc-*.db.gz' | wc -l) snapshots"
