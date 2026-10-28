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
| `ONEBOX_CAPTURE_TOKEN` | The "Add to One Inbox" iPhone Shortcut (section 6). Added to older installs on update. |
| `PIHOLE_PASSWORD` | Optional. An app password from Pi-hole's web page (Settings → Web interface / API), so Pocket Pal's PC can show Pi-hole's numbers through `GET /api/pihole`. Add it to /etc/onebox.env by hand; `PIHOLE_URL` defaults to `http://127.0.0.1`. |

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

**Full Disk Access.** Mail, Messages, Things and Calendar keep their data in protected folders. Open System Settings → Privacy & Security → Full Disk Access → `+`, press Cmd-Shift-G, type `/usr/bin/python3`, add it and switch it on.

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

Mail only downloads new mail while it's open, so the agent starts it hidden in the background whenever it finds it closed (turn that off with `"keep_mail_open": false` in `agent.json`). Mail must have your accounts added (mac.com and Gmail already are). Spark can stay your reading app; the agent reads Mail's index, which syncs in the background.

## 5. On your iPhone

Open the `ts.net` address in Safari, enter your UI key once, then Share → Add to Home Screen. It now opens like an app.

Picks you mark **Add to Things** appear in your Things Inbox within 2 minutes (the Mac must be awake).

## 6. Capture anything that isn't an email

Type into the **Add a to-do** box at the top of the page, or make an iPhone Shortcut so Siri, the share sheet and Back Tap can add things. Either way it goes into Things for **Today** on the next Mac sync, so the watch shows it.

In the Shortcuts app, make a new shortcut called **Add to One Inbox** (that name is what you say to Siri):

1. **Ask for Input** (Text), prompt "What do you need to do?". If you want it in the share sheet too: turn on *Show in Share Sheet* in the shortcut's details, then use **If Shortcut Input has any value**, otherwise Ask for Input.
2. **Get Contents of URL**: `https://<pi-name>.<tailnet>.ts.net/api/capture`, Method **POST**, Headers `Authorization` = `Bearer <ONEBOX_CAPTURE_TOKEN>`, Request Body **JSON** with one Text field `text` = the input from step 1.
3. **Show Notification** with the result's `message` (optional).

Try it: "Hey Siri, Add to One Inbox", then "Sign the Aula form for Friday". It appears on the page as "You added" with Add ticked. Untick it there before the next sync if you change your mind. Back Tap: Settings → Accessibility → Touch → Back Tap → Double Tap → Add to One Inbox.

Tailscale must be on for the phone to reach the Pi.

## 7. e-Boks, Digital Post and Aula reminders

One Inbox never logs in to these (they need MitID). It works from their alert emails, so turn email alerts on:

- **e-Boks** and **mit.dk / Digital Post**: in each app's settings, under notifications, turn on email notification for new post, to an address the Mac's Mail reads.
- **Aula**: Profile → Settings → Notifications, turn on email for messages, and for events and posts if you want those too.

A new alert from `e-boks.dk`, `mit.dk`, `digitalpost.dk`, `borger.dk` or `aula.dk` skips the yes/no step. It is not a to-do of its own, because the email only says something is waiting. Instead Pocket Pal counts unread alerts per source ("Aula 3 · since Mon"), and the Mac keeps **one** to-do per source in Things Today, "Check Aula (3 new)", with every alert listed in its notes. A new alert updates that to-do's count (needs `things_auth_token`). Tick it in Things, or tap **Checked them** in Pocket Pal, and the count goes back to zero. Lookalike senders are still flagged as scams, and alerts older than a week aren't counted, so the first sync doesn't flood Things.

The old one-per-email "Read in Aula: …" to-dos can be cleared in one go: add `"post_tidy": true` to `agent.json`. The Mac then cancels them in Things (they stay in the Logbook) on its next sync.

Quick captures can skip Today: start them with `errand:` (Anytime, tag Errands), `house:` (the Around the house project) or `someday:`. The phone can also send `when`, `tags`, `list` and `deadline` with `/api/capture`.

## 8. Calendar on the watch

The agent reads Calendar.app's database (read-only) for "up next" on the watch. Every calendar Calendar.app shows counts: iCloud, Google, Exchange, and subscribed ones. For MSc lectures from an ICS link, use Calendar → File → New Calendar Subscription and paste the link. Leave calendars out with `"calendar_skip": ["Birthdays", "Work"]` in `agent.json` (the default skips Birthdays, Holidays and Siri Suggestions). `--dry-run` shows how many events it found.

Interviews from the job tracker (`~/Documents/job-tracker/data/job_tracker.db`, read-only) join these events as "Interview: <company>". The job hunt pillar in `today.json` also gets `job_due` (the next deadline of a saved application or a Strong/Possible fit lead), `leads`, `applied_week` and `interview_at`. Point at another file with `"job_tracker_db"` in `agent.json`, or set it to `""` to turn it off.

## 9. Ticking things off from the watch or phone

When you press done on the watch or in the phone app, the Pi hides the item straight away and the Mac ticks it off in Things on its next sync. Things only lets other apps change to-dos with its own key: in Things on the Mac, Settings → General → Enable Things URLs → Manage, copy the authorization token and add it to `~/.config/onebox/agent.json` as `"things_auth_token": "<token>"`. Without it the Mac's report says how many are waiting.

## Turn things off

- Stop the Mac agent: `launchctl unload ~/Library/LaunchAgents/com.oneinbox.agent.plist`
- Stop the Pi server: `sudo systemctl disable --now onebox`
- Revoke the watch or agent: change the token in `/etc/onebox.env`, then `sudo systemctl restart onebox`

## 7. Study calendar: sessions and deadlines into Things

The agent can read your uni calendar feed and put it in Things, so the watch shows it:

- **Live sessions, lectures, seminars**: a to-do on that day, titled with the start time ("19:00 Live session: Week 1"), with a Things reminder 15 minutes before. Added up to 14 days ahead.
- **Deadlines**: a to-do that lands in Today 7 days before, with the Things deadline set to the due date, plus a "2 days left" nudge. Anything called due, deadline, submission, assignment, quiz or exam, or a zero-length event (how Moodle and Canvas export due dates), counts as a deadline.

Get the feed link from the VLE's calendar (look for **Export calendar**, **Calendar feed** or **Subscribe**). It ends in `.ics` or starts with `webcal://`. It is private to you, so keep it only in `agent.json`. Then add to `~/.config/onebox/agent.json`:

```json
"calendars": [
  {"name": "York MSc", "url": "webcal://…/calendar.ics", "tags": ["MSc"]}
],
"deadlines": [
  {"title": "Module 1 assignment", "due": "2026-12-14", "tags": ["MSc"]}
]
```

- `tags` are Things tag names, for example `["MSc"]` so the to-dos count toward the MSc pillar. The tag must already exist in Things. `list` (optional) is a Things area or project; leave it out to use the Things Inbox.
- `deadlines` is for dates that are only in a handbook or an email. Same heads-ups as the feed.

Check what it would add with `python3 -m onebox.agent --dry-run` (see `calendar.would_add`). Each event is added once; the agent remembers them in `~/.config/onebox/calendar-seen.json`. If a session moves, the new time is added as a new to-do; tick off or delete the old one.
