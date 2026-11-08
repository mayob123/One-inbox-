"""The York calendar and study plan, fetched by the Pi itself so they don't wait for the Mac to be awake.

Ben wants Pocket Pal to run without the Mac (2026-11-08). The Mac agent used to fetch the Canvas ICS feed, the
typed-in deadlines and the week-by-week study items (studycal) and send them on. Now the Pi reads the same
settings from a JSON file of its own (ONEBOX_CALENDARS, default /etc/onebox-calendars.json):

    {"calendars": [{"name": "York MSc", "url": "https://...ics", "list": "York MSc", "tags": ["MSc"]}],
     "deadlines": [...], "study": [...]}

and puts the to-dos straight on its own list. What it has added is remembered in the database (meta
"calendar_seen"), and a to-do with the same title that the Mac already added is not added twice.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from . import studycal
from .store import Store

EVERY = 30 * 60  # the feeds change rarely; half-hourly is plenty
SEEN_KEY = "calendar_seen"


def load(path: str) -> dict:
    """The settings file, or {} when there isn't one (then the Pi does nothing and the Mac carries on)."""
    try:
        cfg = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{path} must hold a JSON object")
    out = {k: cfg[k] for k in ("calendars", "deadlines", "study") if cfg.get(k)}
    for c in out.get("calendars") or []:
        url = str(c.get("url") or "")
        if url and not url.startswith(("http://", "https://", "webcal://")):
            # a file on the Mac (~/.config/onebox/york-blocks.ics) lives beside the settings on the Pi:
            # /etc/onebox-york-blocks.ics
            c["url"] = str(Path(path).parent / ("onebox-" + Path(url).name))
    return out


def sync(store: Store, cfg: dict, now: datetime, fetcher=studycal.fetch) -> dict:
    """Add what's due now from the feeds onto the Pi's list. Returns {"added": n, "errors": [...]}."""
    if not cfg:
        return {}
    events, errors = studycal.collect(cfg, now, fetcher)
    seen = store.get_meta(SEEN_KEY, {}) or {}
    new = studycal.due_now(events, seen, now)
    tidy_doubles(store)
    # any to-do with the same title, whatever made it: the Mac's earlier ones went to Things and came back
    have = {same(t["title"], t.get("when")) for status in ("open", "done", "gone") for t in store.tasks(status)}
    todos = [a for ev in new for a in studycal.todos_for(ev, now.date())
             if same(a["title"], str(a.get("when") or "")[:10] or None) not in have]
    made = store.add_todos(todos, "calendar", now.date())
    today = now.date().isoformat()
    for ev in new:
        seen[ev.key()] = today
    keep = (now.date().toordinal() - 60)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v).toordinal() >= keep}
    store.set_meta(SEEN_KEY, seen)
    store.set_meta("calendar_sync", {"at": now.isoformat(timespec="seconds"), "added": len(made), "errors": errors})
    return {"added": len(made), "errors": errors}


def same(title: str, when: str | None) -> tuple:
    """When two to-dos are the same one. A deadline's title names its due day ("Essay (due Fri 13 Nov)") while
    its start day moves with when it was added; a session's title repeats weekly, so its day counts."""
    return (title, None) if "(due " in title else (title, when)


def tidy_doubles(store: Store) -> int:
    """Close open calendar to-dos that repeat the title and day of an older to-do (the first Pi run, 2026-11-08, re-added
    York items that had reached the list through Things). Returns how many were closed."""
    seen, closed = set(), 0
    every = [t for status in ("open", "done", "gone") for t in store.tasks(status)]
    for t in sorted(every, key=lambda t: t.get("created_at") or ""):
        key = same(t["title"], t.get("when"))
        if key in seen and t["status"] == "open" and t.get("source") == "calendar":
            closed += store.close_task(t["id"])
        seen.add(key)
    return closed


def loop(path: str, store: Store, tz, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    while True:
        try:
            got = sync(store, load(path), datetime.now(tz))
            if got.get("added") or got.get("errors"):
                print(f"One Inbox calendars: {got}", flush=True)
        except Exception as e:  # a broken feed or file mustn't take the server down
            print(f"One Inbox calendars failed: {e}", flush=True)
        if stop.wait(EVERY):
            return
