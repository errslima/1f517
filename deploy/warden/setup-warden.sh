#!/usr/bin/env bash
# One-time Warden setup. Run as ubuntu (passwordless sudo) on the VPS.
# Sandboxing per spec: dedicated service account, no sudo, writes only
# inside /srv/warden. The only elevated thing it holds is the pool's
# mod-API token; the Claude subscription token arrives separately.
set -euo pipefail

id -u warden >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /srv/warden --shell /usr/sbin/nologin warden
sudo mkdir -p /srv/warden/secrets /srv/warden/state
sudo chmod 700 /srv/warden/secrets

# runner + prompt from the deployed repo
sudo cp /srv/qoc/prod/deploy/warden/drain.py /srv/warden/drain.py
sudo cp /srv/qoc/prod/deploy/warden/screening-prompt.md /srv/warden/screening-prompt.md

# warden-readable copy of the mod-API token
sudo cp /srv/qoc/secrets/warden-prod.token /srv/warden/secrets/mod-api.token
sudo chmod 400 /srv/warden/secrets/mod-api.token

# Claude Code CLI, native installer, as the warden user
if [ ! -x /srv/warden/.local/bin/claude ]; then
  sudo -u warden env HOME=/srv/warden bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi

# env file placeholder (token added later); chown everything
if ! sudo test -f /srv/warden/secrets/env; then
  echo "# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-..." | sudo tee /srv/warden/secrets/env >/dev/null
fi
sudo chmod 600 /srv/warden/secrets/env
sudo chown -R warden:warden /srv/warden

sudo cp /srv/qoc/prod/deploy/warden/warden-drain.service /etc/systemd/system/
sudo cp /srv/qoc/prod/deploy/warden/warden-drain.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now warden-drain.timer
echo "warden setup complete; timer active (idle until token configured)"
