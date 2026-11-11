#!/usr/bin/env bash
# Install or update One Inbox on the Pi. Run from the repo folder: sudo ./deploy/install-pi.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 9)' || { echo "Needs Python 3.9 or newer."; exit 1; }

id onebox >/dev/null 2>&1 || useradd --system --home /var/lib/onebox --shell /usr/sbin/nologin onebox
install -d -o onebox -g onebox -m 700 /var/lib/onebox
install -d /opt/one-inbox
cp -r onebox /opt/one-inbox/

gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
if [ ! -f /etc/onebox.env ]; then
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
if ! grep -q '^ONEBOX_CAPTURE_TOKEN=' /etc/onebox.env; then
  echo "ONEBOX_CAPTURE_TOKEN=$(gen)" >> /etc/onebox.env
  echo "Added ONEBOX_CAPTURE_TOKEN for the iPhone Shortcut. Read it with: sudo cat /etc/onebox.env"
fi

# The York calendar link and the mail app passwords are private: only root and the server may read them.
for f in /etc/onebox-calendars.json /etc/onebox-mail.json; do
  if [ -f "$f" ]; then chown root:onebox "$f" && chmod 640 "$f"; fi
done

install -m 644 deploy/onebox.service /etc/systemd/system/onebox.service

# The job tracker lives in Ben's home folder, which the server can't read: a timer run as Ben copies its
# database to /var/lib/onebox-jobs every 10 minutes (onebox/pijobs.py reads it there).
owner="${SUDO_USER:-ben}"
home="$(getent passwd "$owner" | cut -d: -f6)"
if [ -f "$home/job-tracker/data/job_tracker.db" ]; then
  install -d -o "$owner" -g onebox -m 2750 /var/lib/onebox-jobs
  install -m 755 deploy/copy-job-tracker.py /opt/one-inbox/copy-job-tracker.py
  sed -e "s#@USER@#$owner#" -e "s#@HOME@#$home#" deploy/onebox-jobcopy.service > /etc/systemd/system/onebox-jobcopy.service
  install -m 644 deploy/onebox-jobcopy.timer /etc/systemd/system/onebox-jobcopy.timer
  systemctl daemon-reload
  systemctl enable --now onebox-jobcopy.timer
  systemctl start onebox-jobcopy.service && echo "Job tracker copied for One Inbox."
fi

systemctl daemon-reload
systemctl enable --now onebox
systemctl restart onebox
sleep 1
curl -fsS http://127.0.0.1:8787/healthz && echo " One Inbox is running."
