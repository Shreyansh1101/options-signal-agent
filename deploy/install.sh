#!/bin/bash
# Installs the live options signal agent on a fresh Ubuntu server (Oracle Cloud Always Free works).
# Called by the setup script you paste when creating the server. Safe to run again.
#   needs env: REPO_URL, APCA_API_KEY_ID, APCA_API_SECRET_KEY, NTFY_TOPIC
set -euo pipefail
: "${REPO_URL:?}" "${APCA_API_KEY_ID:?}" "${APCA_API_SECRET_KEY:?}" "${NTFY_TOPIC:?}"
APP=/opt/signal-agent
DATA=/var/lib/signal-agent
notify() { curl -fsS -m 15 -H "Title: $1" -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null || true; }
trap 'notify "Setup failed" "Server setup stopped at line $LINENO. Tell Claude: install.sh failed at line $LINENO."' ERR

export DEBIAN_FRONTEND=noninteractive
echo "== swap (the free server has 1 GB RAM)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== packages"
apt-get -o DPkg::Lock::Timeout=900 update -y
apt-get -o DPkg::Lock::Timeout=900 install -y git python3-venv python3-pip curl tzdata

echo "== code"
if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q --depth 1 "$REPO_URL" "$APP"; fi
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== service user, secrets, state"
id agent >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin agent
mkdir -p "$DATA/state"
chown -R agent:agent "$DATA"
umask 077
cat > /etc/signal-agent.env <<ENV
APCA_API_KEY_ID=${APCA_API_KEY_ID}
APCA_API_SECRET_KEY=${APCA_API_SECRET_KEY}
NTFY_TOPIC=${NTFY_TOPIC}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
STATE_PATH=${DATA}/state/state.json
ENV
chmod 600 /etc/signal-agent.env
umask 022

echo "== service + auto-update"
install -m 644 "$APP/deploy/signal-agent.service" /etc/systemd/system/signal-agent.service
chmod +x "$APP/deploy/update.sh"
echo "*/15 * * * * root $APP/deploy/update.sh >> /var/log/signal-agent-update.log 2>&1" > /etc/cron.d/signal-agent
systemctl daemon-reload
systemctl enable signal-agent
systemctl restart signal-agent
echo "== done — the agent sends a 'Signal agent is running' notification when it starts"
