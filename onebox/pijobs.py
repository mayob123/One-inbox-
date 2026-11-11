"""The job tracker, read by the Pi itself, so job leads, interviews and "Apply:" to-dos don't wait for the Mac.

Ben wants Pocket Pal to run without the Mac (2026-11-11: "the pocket pal still seems to need the mac"). The job
tracker moved to the Pi on 2026-11-08 (~ben/job-tracker), but the Mac agent still read it, from a copy it pulled
over, and sent the result on. That copy stopped updating, and a sleeping Mac sent nothing at all.

The server can't read Ben's home folder, so a small timer (deploy/onebox-jobcopy.*, run as Ben) copies the
tracker's database every 10 minutes to ONEBOX_JOB_TRACKER (default /var/lib/onebox-jobs/job_tracker.db), where
the server may read it. Every 10 minutes the server reads that copy, read-only, for:

- the Job hunt pillar's extras (good leads, the lead list, the next deadline, applications this week),
- upcoming interviews, as events,
- "Apply: …" to-dos a week before a strong lead's deadline, straight onto the Pi's list.

The result is kept in meta "job_sync"; server._status() uses it in place of what the Mac last sent.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

from . import agent, picalendar, studycal
from .store import Store

EVERY = 10 * 60
SEEN_KEY = "job_seen"
FRESH = timedelta(hours=2)  # older than this (copy timer stopped, file gone) and the Mac's report is used again


def sync(store: Store, path: str, now: datetime) -> dict:
    """Read the tracker copy. Returns {} when there is none (then the Mac's report stands)."""
    if not path or not os.path.exists(path):
        return {}
    events, extra = agent.job_tracker(path, now)
    deadlines = agent.job_deadlines({"job_tracker_db": path}, now)
    added = add_due(store, deadlines, now)
    report = {"at": now.isoformat(timespec="seconds"), "events": events, "extra": extra, "added": added}
    store.set_meta("job_sync", report)
    return report


def add_due(store: Store, events: list[studycal.Event], now: datetime) -> int:
    """'Apply: …' to-dos that are due now, unless one with the same title is already on the list (the Mac's)."""
    seen = store.get_meta(SEEN_KEY, {}) or {}
    new = studycal.due_now(events, seen, now)
    have = {picalendar.same(t["title"], t.get("when")) for s in ("open", "done", "gone") for t in store.tasks(s)}
    todos = [a for ev in new for a in studycal.todos_for(ev, now.date())
             if picalendar.same(a["title"], str(a.get("when") or "")[:10] or None) not in have]
    made = store.add_todos(todos, "calendar", now.date())
    today = now.date().isoformat()
    for ev in new:
        seen[ev.key()] = today
    keep = now.date().toordinal() - 60
    store.set_meta(SEEN_KEY, {k: v for k, v in seen.items() if datetime.fromisoformat(v).toordinal() >= keep})
    return len(made)


def fresh(store: Store, now: datetime) -> dict | None:
    """The last read, if recent enough to trust over the Mac's."""
    got = store.get_meta("job_sync") or {}
    try:
        at = datetime.fromisoformat(got["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return got if now - at <= FRESH else None


def apply(status: dict, got: dict | None) -> dict:
    """status with the Pi's own job tracker read in place of the Mac's: job pillar extras, interviews, and the
    sync time (so the app doesn't say job leads are waiting for the Mac)."""
    if not got:
        return status
    out = dict(status)
    out["events"] = sorted([e for e in status.get("events") or [] if not e.get("title", "").startswith("Interview: ")]
                           + got.get("events", []), key=lambda e: e["start"])
    pillars = dict(out.get("pillars") or {})
    job = dict(pillars.get("job") or {"done_week": 0, "open": 0, "last_done": None, "next_due": None})
    extra = got.get("extra") or {}
    job.update(extra)
    if extra.get("job_due"):
        job["next_due"] = min(job.get("next_due") or extra["job_due"], extra["job_due"])
    pillars["job"] = job
    out["pillars"] = pillars
    out["at"] = max(filter(None, [status.get("at"), got.get("at")]), key=_when)
    return out


def _when(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def loop(path: str, store: Store, tz, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    first = True
    while True:
        try:
            got = sync(store, path, datetime.now(tz))
            if got and (first or got.get("added")):
                # counts only, never a company or title
                print(f"One Inbox jobs: {got['extra'].get('leads')} good leads, {len(got['events'])} interviews, "
                      f"{got['added']} new Apply to-dos", flush=True)
                first = False
        except Exception as e:  # a tracker schema change mustn't take the server down
            print(f"One Inbox jobs failed: {e}", flush=True)
        if stop.wait(EVERY):
            return
