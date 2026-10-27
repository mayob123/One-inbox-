# Setting up One Inbox

Three parts, about 20 minutes in total:

1. **The Pi** runs the server: the triage page, the watch feed and the database.
2. **The Mac** runs the agent every 2 minutes: reads Mail and Things, sends results to the Pi, adds your picks to Things.
3. **Names and access**: Pi-hole gives the Pi a home name, Tailscale makes it reachable when you're out.

Nothing here is open to the internet. Mail and Things are only ever read, never changed, except that the agent adds the to-dos you picked.

## 1. The Pi

Needs Python 3.9 or newer (Raspberry Pi OS Bookworm has 3.11). No other packages.

```bash
git clone https://github.com/mayob123/one-inbox-.git
cd one-inbox-
sudo ./deploy/install-pi.sh
```

The script creates a `onebox` system user, puts the app in `/opt/one-inbox`, writes three random keys to `/etc/onebox.env` and starts the `onebox` service on port 8787. See the keys with `sudo cat /etc/onebox.env`:

| Key | Used by |
| --- | --- |
| `ONEBOX_UI_KEY` | You, once per phone or browser, on the login page |
| `ONEBOX_AGENT_TOKEN` | The Mac agent |
| `ONEBOX_WATCH_TOKEN` | The watch face, sent as `X-OneInbox-Token` |

Update later with `git pull && sudo ./deploy/install-pi.sh` (keys are kept). Logs: `journalctl -u onebox -f`.

Pi-hole uses ports 80 and 53, so One Inbox stays on 8787 and does not clash.

## 2. A home name with Pi-hole

So you can type `inbox.home` instead of an IP address.

- **Pi-hole v6:** Settings → Local DNS Records → Domain `inbox.home`, IP = the Pi's LAN address → Add.
- **Pi-hole v5:** Local DNS → DNS Records → same.

At home, open `http://inbox.home:8787`.

## 3. Away from home with Tailscale

Install Tailscale on the Pi, your iPhone and your Mac (`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up` on the Pi).

Give One Inbox a proper HTTPS address on your tailnet:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8787
tailscale serve status   # shows https://<pi-name>.<tailnet>.ts.net
```

That `https://…ts.net` address works from anywhere your phone has Tailscale on, and it is the address the watch face needs (Garmin only accepts real HTTPS).

**Pi-hole everywhere:** in the Tailscale admin console → DNS → add the Pi's Tailscale IP as a global nameserver and turn on "Override local DNS". Your phone then gets Pi-hole blocking on mobile data too. In Pi-hole, allow queries from the Tailscale interface: Settings → DNS → Interface settings → "Permit all origins" (safe here because only your tailnet can reach it).

If you only want the HTTPS name and not the extra open port, set `ONEBOX_HOST=127.0.0.1` in `/etc/onebox.env` and use the `ts.net` address at home as well.

## 4. The Mac agent

```bash
git clone https://github.com/mayob123/one-inbox-.git ~/one-inbox
mkdir -p ~/.config/onebox
cat > ~/.config/onebox/agent.json <<'JSON'
{
  "server": "https://<pi-name>.<tailnet>.ts.net",
  "token": "<ONEBOX_AGENT_TOKEN from the Pi>",
  "days": 21,
  "account_labels": {},
  "friends": [
    {"name": "Tom", "every_days": 21, "handles": ["+45 12 34 56 78"]}
  ]
}
JSON
chmod 600 ~/.config/onebox/agent.json
```

`handles` are the phone numbers or emails exactly as Messages shows them. `friends` can stay empty for now.

**Full Disk Access.** Mail, Messages and Things keep their data in protected folders. Open System Settings → Privacy & Security → Full Disk Access → `+`, press Cmd-Shift-G, type `/usr/bin/python3`, add it and switch it on.

Try it without sending anything:

```bash
cd ~/one-inbox && python3 -m onebox.agent --dry-run
```

You should see counts for `decide`, `done`, `scam` and your Things numbers. If it says the Mail index was not found, Full Disk Access is not on yet.

Then run it every 2 minutes, so a tick in Things reaches the phone quickly:

```bash
sed "s#__REPO__#$HOME/one-inbox#" deploy/com.oneinbox.agent.plist > ~/Library/LaunchAgents/com.oneinbox.agent.plist
launchctl load ~/Library/LaunchAgents/com.oneinbox.agent.plist
tail -f /tmp/onebox-agent.log
```

Mail must have your accounts added (mac.com and Gmail already are). Spark can stay your reading app; the agent reads Mail's index, which syncs in the background.

## 5. On your iPhone

Open the `ts.net` address in Safari, enter your UI key once, then Share → Add to Home Screen. It now opens like an app.

Picks you mark **Add to Things** appear in your Things Inbox within 2 minutes (the Mac must be awake).

## Turn things off

- Stop the Mac agent: `launchctl unload ~/Library/LaunchAgents/com.oneinbox.agent.plist`
- Stop the Pi server: `sudo systemctl disable --now onebox`
- Revoke the watch or agent: change the token in `/etc/onebox.env`, then `sudo systemctl restart onebox`
