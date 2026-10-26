# One Inbox

One place for everything that needs Ben: mail, e-Boks and Aula alerts, bills, plans and friends. It reads it all, works out what you already dealt with, flags scams, and asks only about the rest. Your picks go into Things 3, which stays the one list you trust.

```
 Mail (mac.com, Gmail) ─┐                       ┌─> iPhone: triage page (Add / Done / Later / Not mine)
 Things 3 ──────────────┼─> Mac agent ─> Pi ────┼─> Garmin: buddy watch face feed
 Messages (friends) ────┘   every 15 min        └─> Things 3: your picks, added by the Mac
```

- **Local.** Runs on your Pi and Mac. Reached at home as `inbox.home` (Pi-hole local DNS) and away over Tailscale. Nothing is public.
- **Read-only.** Reads mail headers, Things and Messages. Never sends, pays, deletes or logs in to MitID.
- **No dependencies.** Python standard library only, on both the Pi and the Mac.

## What v1 does

| Mail that is… | Goes to |
| --- | --- |
| An e-Boks, Digital Post or Aula alert | Straight to **Things Today**, so it stays on the watch until done |
| Needs you (bill, letter, Danish message, invite) | **Needs your call**, with a suggested task |
| Already handled (you replied, accepted, or a later email confirms it) | **Looks done**, with the evidence |
| Pretending to be a brand from the wrong domain, or asking for card details | **Likely scams** |
| Newsletters, receipts, sign-in notices | Folded away |

Study sessions and deadlines from your uni calendar feed go into Things too: sessions on the day with a reminder, deadlines a week ahead with a nudge two days before ([setup](docs/setup.md#7-study-calendar-sessions-and-deadlines-into-things)).

Anything that isn't an email can be added from the box on the page or from Siri with an iPhone Shortcut ([setup](docs/setup.md#6-capture-anything-that-isnt-an-email)). It goes to Things Today too.

The Pi also serves `/feed/watch.json` for the Pokémon buddy watch face: items left, next item, next bill, XP and the buddy's mood. Contract: [docs/watch-feed.md](docs/watch-feed.md).

## Set up

See [docs/setup.md](docs/setup.md): Pi install script, Pi-hole name, Tailscale HTTPS, Mac agent. No internet on the Pi network? Use [docs/offline-setup.md](docs/offline-setup.md), which also covers fixing a Pi-hole that blocks all traffic.

## Develop

```bash
python3 -m unittest discover -s tests -t .
python3 -m onebox.agent --dry-run    # on the Mac, prints what it would send
```

| File | What it is |
| --- | --- |
| `onebox/triage.py` | Rules that sort mail: done detection, scams, noise, areas |
| `onebox/agent.py` | Mac agent: Mail and Things readers, sync with the Pi, adds to Things |
| `onebox/studycal.py` | Calendar feeds and typed-in deadlines into Things |
| `onebox/server.py` | Pi server: page, API, watch feed |
| `onebox/store.py` | SQLite storage on the Pi |
| `onebox/feed.py` | Watch feed and buddy mood |
| `onebox/web/` | The phone page |

## Next

- Danish translation and one-line summaries for school and e-Boks messages
- Morning brief notification at 07:30
- Friends page and Pi dashboard from the design
