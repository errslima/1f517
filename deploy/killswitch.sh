#!/usr/bin/env bash
# Flip the pool read-only (or back) without restarting anything.
#   ./killswitch.sh on      writes refused with 503, reads unaffected
#   ./killswitch.sh off     normal operation
#   ./killswitch.sh status
# The flag is a file the proxy tests per request, so on/off takes effect
# immediately with no reload and survives service restarts.
set -euo pipefail
FLAG=/srv/qoc/flags/readonly

case "${1:-status}" in
  on)
    sudo mkdir -p "$(dirname "$FLAG")"
    printf 'pool set read-only at %s by %s\n' "$(date -Is)" "${SUDO_USER:-$USER}" | sudo tee "$FLAG" >/dev/null
    sudo chmod 644 "$FLAG"
    echo "KILL SWITCH ON - writes now refused with 503, reads unaffected."
    ;;
  off)
    sudo rm -f "$FLAG"
    echo "KILL SWITCH OFF - writes accepted normally."
    ;;
  status)
    if [ -f "$FLAG" ]; then
      echo "ON (read-only) since: $(cat "$FLAG")"
    else
      echo "OFF (writes accepted)"
    fi
    ;;
  *)
    echo "usage: $0 on|off|status" >&2; exit 2 ;;
esac
