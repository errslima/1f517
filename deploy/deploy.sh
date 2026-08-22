#!/usr/bin/env bash
# Pull + restart one environment: ./deploy.sh prod|dev
set -euo pipefail
env=${1:?usage: deploy.sh prod|dev}
sudo git -C /srv/qoc/$env pull --ff-only
sudo /srv/qoc/$env/venv/bin/pip install -q -r /srv/qoc/$env/requirements.txt
sudo chmod -R a+rX /srv/qoc/$env
sudo cp /srv/qoc/$env/deploy/qoc-$env.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart qoc-$env
sudo cp /srv/qoc/prod/deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
echo "deployed $env"
