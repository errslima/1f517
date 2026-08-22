#!/usr/bin/env bash
# One-time server setup. Run as ubuntu (passwordless sudo) on the VPS.
set -euo pipefail
REPO=${REPO:-https://github.com/errslima/quorum-of-clones.git}

id -u qoc >/dev/null 2>&1 || sudo useradd --system --home /srv/qoc --shell /usr/sbin/nologin qoc
sudo mkdir -p /srv/qoc/data-prod /srv/qoc/data-dev /srv/qoc/secrets
sudo chmod 700 /srv/qoc/secrets

for env in prod dev; do
  branch=$([ "$env" = prod ] && echo main || echo dev)
  if [ ! -d /srv/qoc/$env/.git ]; then
    sudo git clone -b "$branch" "$REPO" /srv/qoc/$env
  else
    sudo git -C /srv/qoc/$env pull --ff-only
  fi
  sudo python3 -m venv /srv/qoc/$env/venv
  sudo /srv/qoc/$env/venv/bin/pip install -q -r /srv/qoc/$env/requirements.txt
  # Warden service account (idempotent): token printed once, kept in secrets/
  if [ ! -f /srv/qoc/secrets/warden-$env.token ]; then
    (cd /srv/qoc/$env && sudo env QOC_DATA_DIR=/srv/qoc/data-$env ./venv/bin/python -m app.warden_cli warden) | sudo tee /srv/qoc/secrets/warden-$env.token >/dev/null
    sudo chmod 600 /srv/qoc/secrets/warden-$env.token
  fi
  sudo cp /srv/qoc/$env/deploy/qoc-$env.service /etc/systemd/system/
done

sudo chown -R qoc:qoc /srv/qoc/data-prod /srv/qoc/data-dev
sudo chmod -R a+rX /srv/qoc/prod /srv/qoc/dev

sudo mkdir -p /srv/qoc/flags /srv/qoc/backups

# ops timers: nightly snapshot, health check every 15 min
sudo cp /srv/qoc/prod/deploy/ops/qoc-backup.service /srv/qoc/prod/deploy/ops/qoc-backup.timer         /srv/qoc/prod/deploy/ops/qoc-healthcheck.service /srv/qoc/prod/deploy/ops/qoc-healthcheck.timer         /etc/systemd/system/
if ! sudo test -f /srv/qoc/secrets/alerts.env; then
  echo '# NTFY_TOPIC=pick-a-long-unguessable-topic   # https://ntfy.sh/<topic>'     | sudo tee /srv/qoc/secrets/alerts.env >/dev/null
  sudo chmod 600 /srv/qoc/secrets/alerts.env
fi

sudo cp /srv/qoc/prod/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now qoc-backup.timer qoc-healthcheck.timer
sudo systemctl enable --now qoc-prod qoc-dev
sudo systemctl reload caddy
echo "bootstrap complete"
