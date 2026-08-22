#!/usr/bin/env bash
# Periodic health check. Writes findings to the journal; if a topic is
# configured in /srv/qoc/secrets/alerts.env it also pushes to ntfy.sh.
# Exits non-zero when anything is CRITICAL so systemd marks the unit failed.
set -uo pipefail
[ -f /srv/qoc/secrets/alerts.env ] && . /srv/qoc/secrets/alerts.env
NTFY_TOPIC="${NTFY_TOPIC:-}"

problems=(); critical=0

note() { problems+=("$1"); }
crit() { problems+=("CRITICAL: $1"); critical=1; }

# services
for unit in qoc-prod qoc-dev caddy warden-drain.timer; do
  systemctl is-active --quiet "$unit" || crit "$unit is not active"
done

# prod answers over the public name, with its framing notice intact
body=$(curl -s --max-time 15 https://1f517.com/api/pulse || true)
case "$body" in
  *'"notice"'*) ;;
  *) crit "https://1f517.com/api/pulse did not return a notice-bearing body" ;;
esac

# disk
use=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${use:-0}" -ge 85 ] && note "disk at ${use}% on /"

# moderation queue depth: the Warden falling behind is the signal that
# submissions are arriving faster than screening drains them
depth=$(sqlite3 -readonly /srv/qoc/data-prod/qoc.db \
  "SELECT COUNT(*) FROM findings WHERE status='screening';" 2>/dev/null || echo 0)
[ "${depth:-0}" -ge 50 ] && note "moderation queue depth ${depth}"

# backup freshness
newest=$(find /srv/qoc/backups -name 'qoc-prod-*.db.gz' -mmin -2160 2>/dev/null | head -1)
[ -z "$newest" ] && note "no prod backup newer than 36h"

# TLS expiry
days_left=$(echo | openssl s_client -connect 1f517.com:443 -servername 1f517.com 2>/dev/null \
  | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 \
  | { read -r d; [ -n "$d" ] && echo $(( ($(date -d "$d" +%s) - $(date +%s)) / 86400 )) || echo 99; })
[ "${days_left:-99}" -lt 14 ] && note "TLS certificate expires in ${days_left}d"

if [ ${#problems[@]} -eq 0 ]; then
  echo "OK: services up, pool answering, queue ${depth}, disk ${use}%, cert ${days_left}d"
  exit 0
fi

msg=$(printf '%s; ' "${problems[@]}")
echo "HEALTH: $msg" >&2
if [ -n "$NTFY_TOPIC" ]; then
  curl -s --max-time 10 -H "Title: 1f517 health" \
    -H "Priority: $([ $critical -eq 1 ] && echo urgent || echo default)" \
    -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || true
fi
exit $critical
