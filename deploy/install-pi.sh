#!/usr/bin/env bash
# Install or update One Inbox on the Pi. Run from the repo folder: sudo ./deploy/install-pi.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 9)' || { echo "Needs Python 3.9 or newer."; exit 1; }

id onebox >/dev/null 2>&1 || useradd --system --home /var/lib/onebox --shell /usr/sbin/nologin onebox
install -d -o onebox -g onebox -m 700 /var/lib/onebox
install -d /opt/one-inbox
cp -r onebox /opt/one-inbox/

if [ ! -f /etc/onebox.env ]; then
  gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
  cat > /etc/onebox.env <<ENV
ONEBOX_DB=/var/lib/onebox/onebox.sqlite3
ONEBOX_HOST=0.0.0.0
ONEBOX_PORT=8787
ONEBOX_TZ=Europe/Copenhagen
ONEBOX_UI_KEY=$(gen)
ONEBOX_AGENT_TOKEN=$(gen)
ONEBOX_WATCH_TOKEN=$(gen)
ENV
  chmod 600 /etc/onebox.env
  echo "Created /etc/onebox.env with new keys. Read them with: sudo cat /etc/onebox.env"
fi

install -m 644 deploy/onebox.service /etc/systemd/system/onebox.service
systemctl daemon-reload
systemctl enable --now onebox
systemctl restart onebox
sleep 1
curl -fsS http://127.0.0.1:8787/healthz && echo " One Inbox is running."
