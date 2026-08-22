#!/usr/bin/env bash
# Run from your OWN machine to pull the newest prod snapshot off the server.
# "Off-box" is only real once a copy lives somewhere the VPS cannot reach;
# schedule this locally (Task Scheduler / cron) to make that true.
set -euo pipefail
HOST=${HOST:-ubuntu@54.37.204.161}
KEY=${KEY:-$HOME/.ssh/qoc_vps_ed25519}
DEST=${1:?usage: pull-backup.sh <local-destination-dir>}
mkdir -p "$DEST"
latest=$(ssh -i "$KEY" -o BatchMode=yes "$HOST" \
  "sudo ls -1t /srv/qoc/backups/qoc-prod-*.db.gz 2>/dev/null | head -1")
[ -n "$latest" ] || { echo "no snapshot found on server" >&2; exit 1; }
name=$(basename "$latest")
ssh -i "$KEY" -o BatchMode=yes "$HOST" "sudo cat '$latest'" > "$DEST/$name"
echo "pulled $name -> $DEST/$name ($(du -h "$DEST/$name" | cut -f1))"
