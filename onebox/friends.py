"""Close friends: who Ben has caught up with, and how long ago.

Ben's Friends pillar (2026-11-08): free time is short and planning feels hard, so long gaps happen. Pocket Pal
lists his close friends with "last caught up 3 weeks ago" and gently puts the ones he'd like to see next at the
top. Nothing here is ever "overdue" or "failed": a friend past their rhythm is just "ready for a catch-up".

A catch-up comes from three places:
  - Ben taps "Saw" or "Spoke" on a friend in Pocket Pal (POST /api/friends/<id>/saw|spoke), saved here so it
    works without the Mac;
  - a calendar event with the friend's name in it ("Dinner with Sara"): the Mac sends the ones it finds, and
    the Pi matches events from any calendar feed it fetches itself ("friend_calendars" in the calendars file).
    A past event counts as seeing them; a future one shows as a plan;
  - iMessage: only the Mac can read Messages, so when it's awake it sends the last message date for each
    friend with phone numbers or emails ("handles"). A bonus, never needed.

Tables (made here, beside the store's own):
  friends(id, name, every_days, handles, also, created_at, updated_at, gone)
  friend_contacts(friend_id, how, at, source, title)   how: saw | spoke | calendar | message
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS friends (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  every_days INTEGER NOT NULL,
  handles TEXT NOT NULL DEFAULT '[]',
  also TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS friend_contacts (
  friend_id TEXT NOT NULL,
  how TEXT NOT NULL,
  at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'app',
  title TEXT,
  PRIMARY KEY (friend_id, how, at)
);
"""

HOWS = ("saw", "spoke", "calendar", "message")
TAPS = ("saw", "spoke")
DEFAULT_EVERY = 21
EVERY_MIN, EVERY_MAX = 3, 365
NAME_MAX = 60
HANDLES_MAX = 6
ALSO_MAX = 6
FRIENDS_MAX = 60
PLAN_DAYS = 30  # how far ahead a calendar event shows as a plan
EVENT_SOURCES = ("mac", "pi")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(store) -> None:
    with store.lock:
        store.db.executescript(SCHEMA)
        store.db.commit()


# --- checking what the phone sends ------------------------------------------

def _clean_list(v, most: int, each: int, what: str) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, str):
        v = [p for p in re.split(r"[,\n]", v)]
    if not isinstance(v, list):
        raise ValueError(f"{what} must be a list")
    out = []
    for p in v:
        p = str(p).strip()[:each]
        if p and p.lower() not in (o.lower() for o in out):
            out.append(p)
    if len(out) > most:
        raise ValueError(f"up to {most} {what}")
    return out


def clean(body: dict, new: bool) -> dict:
    """A friend from the phone, checked. Raises ValueError (a 400)."""
    if not isinstance(body, dict):
        raise ValueError("send a JSON object")
    out: dict = {}
    if new or "name" in body:
        name = str(body.get("name") or "").strip()
        if not name or len(name) > NAME_MAX:
            raise ValueError(f"name must be 1 to {NAME_MAX} characters")
        out["name"] = name
    if new or "every_days" in body:
        try:
            v = body.get("every_days")
            every = DEFAULT_EVERY if v in (None, "") else int(v)
        except (TypeError, ValueError):
            raise ValueError("every_days must be a number of days") from None
        if not EVERY_MIN <= every <= EVERY_MAX:
            raise ValueError(f"every_days must be {EVERY_MIN} to {EVERY_MAX}")
        out["every_days"] = every
    if "handles" in body or new:
        out["handles"] = _clean_list(body.get("handles"), HANDLES_MAX, 80, "phone numbers or emails")
    if "also" in body or new:
        out["also"] = _clean_list(body.get("also"), ALSO_MAX, 40, "other names")
    return out


# --- the list ---------------------------------------------------------------

def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "every_days": r["every_days"],
            "handles": json.loads(r["handles"] or "[]"), "also": json.loads(r["also"] or "[]")}


def rows(store) -> list[dict]:
    with store.lock:
        got = store.db.execute("SELECT * FROM friends WHERE gone=0 ORDER BY name COLLATE NOCASE").fetchall()
    return [_row(r) for r in got]


def add(store, fields: dict) -> dict:
    with store.lock:
        n = store.db.execute("SELECT COUNT(*) FROM friends WHERE gone=0").fetchone()[0]
        if n >= FRIENDS_MAX:
            raise ValueError(f"up to {FRIENDS_MAX} friends")
        fid = secrets.token_hex(6)
        now = _now_iso()
        store.db.execute(
            "INSERT INTO friends (id, name, every_days, handles, also, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (fid, fields["name"], fields["every_days"], json.dumps(fields.get("handles", [])),
             json.dumps(fields.get("also", [])), now, now))
        store.db.commit()
    return {"id": fid, **fields}


def edit(store, fid: str, fields: dict) -> bool:
    sets, args = [], []
    for k in ("name", "every_days"):
        if k in fields:
            sets.append(f"{k}=?")
            args.append(fields[k])
    for k in ("handles", "also"):
        if k in fields:
            sets.append(f"{k}=?")
            args.append(json.dumps(fields[k]))
    sets.append("updated_at=?")
    args.append(_now_iso())
    with store.lock:
        cur = store.db.execute(f"UPDATE friends SET {', '.join(sets)} WHERE id=? AND gone=0", (*args, fid))
        store.db.commit()
    return cur.rowcount > 0


def remove(store, fid: str) -> bool:
    """Off the list. Kept in the database (gone=1) so an accidental remove can be put back by hand."""
    with store.lock:
        cur = store.db.execute("UPDATE friends SET gone=1, updated_at=? WHERE id=? AND gone=0", (_now_iso(), fid))
        store.db.commit()
    return cur.rowcount > 0


def log(store, fid: str, how: str, at: datetime, source: str = "app", title: str | None = None) -> bool:
    if how not in HOWS:
        raise ValueError("how must be one of " + ", ".join(HOWS))
    with store.lock:
        if not store.db.execute("SELECT 1 FROM friends WHERE id=? AND gone=0", (fid,)).fetchone():
            return False
        store.db.execute(
            "INSERT OR IGNORE INTO friend_contacts (friend_id, how, at, source, title) VALUES (?,?,?,?,?)",
            (fid, how, _utc(at), source, title))
        store.db.commit()
    return True


def undo(store, fid: str) -> bool:
    """Takes back the latest Saw/Spoke tap, for a tap on the wrong friend."""
    with store.lock:
        r = store.db.execute(
            "SELECT how, at FROM friend_contacts WHERE friend_id=? AND how IN ('saw','spoke') ORDER BY at DESC LIMIT 1",
            (fid,)).fetchone()
        if not r:
            return False
        store.db.execute("DELETE FROM friend_contacts WHERE friend_id=? AND how=? AND at=?", (fid, r["how"], r["at"]))
        store.db.commit()
    return True


# --- calendar and messages --------------------------------------------------

def _names(f: dict) -> list[str]:
    """The words that pick a friend out of a calendar title: their name and any others (a nickname, "Sara M")."""
    return [n for n in [f["name"], *f.get("also", [])] if len(n.strip()) >= 2]


def matches(title: str, f: dict) -> bool:
    t = title or ""
    return any(re.search(r"(?<!\w)" + re.escape(n.strip()) + r"(?!\w)", t, re.IGNORECASE) for n in _names(f))


def _utc(d: datetime) -> str:
    """Every time is stored in UTC, so comparing the text compares the times."""
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def match_events(store, events: list[dict], now: datetime, source: str,
                 window: tuple[datetime, datetime] | None = None) -> int:
    """Calendar events → catch-ups (past) and plans (future), for every friend named in a title.
    Events from this source inside `window` that are no longer sent (moved or cancelled) are dropped, so a
    cancelled dinner neither shows as a plan nor counts once its day comes. Returns how many matched."""
    if source not in EVENT_SOURCES:
        raise ValueError("source must be mac or pi")
    friends = rows(store)
    found = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        start = _parse(ev.get("start"))
        title = str(ev.get("title") or "")[:200]
        if not start or not title:
            continue
        for f in friends:
            if matches(title, f):
                found.append((f["id"], _utc(start), title))
    with store.lock:
        if window:
            lo, hi = (_utc(w) for w in window)
            # Only events still to come are re-checked: a past catch-up stays even when the feed stops sending it.
            lo = max(lo, _utc(now))
            store.db.execute("DELETE FROM friend_contacts WHERE how='calendar' AND source=? AND at>=? AND at<=?",
                             (source, lo, hi))
        store.db.executemany(
            "INSERT OR REPLACE INTO friend_contacts (friend_id, how, at, source, title) VALUES (?,'calendar',?,?,?)",
            [(fid, at, source, title) for fid, at, title in found])
        store.db.commit()
    return len(found)


def record_messages(store, messages: list[dict]) -> int:
    """The Mac's last iMessage per friend: [{"id": friend id, "at": iso}]. Only the newest is kept."""
    n = 0
    known = {f["id"] for f in rows(store)}
    with store.lock:
        for m in messages or []:
            if not isinstance(m, dict) or m.get("id") not in known:
                continue
            at = _parse(m.get("at"))
            if not at:
                continue
            store.db.execute("DELETE FROM friend_contacts WHERE friend_id=? AND how='message' AND at<?",
                             (m["id"], _utc(at)))
            store.db.execute(
                "INSERT OR IGNORE INTO friend_contacts (friend_id, how, at, source) VALUES (?,'message',?,'mac')",
                (m["id"], _utc(at)))
            n += 1
        store.db.commit()
    return n


def seed(store, friends: list[dict]) -> int:
    """The friends already in the Mac's agent.json, added once while the Pi's list is empty."""
    if rows(store) or not isinstance(friends, list):
        return 0
    n = 0
    for f in friends[:FRIENDS_MAX]:
        try:
            add(store, clean({"name": f.get("name"), "every_days": f.get("every_days"),
                              "handles": f.get("handles")}, new=True))
            n += 1
        except (ValueError, AttributeError):
            continue
    return n


# --- what Pocket Pal shows ----------------------------------------------------

def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    if days < 365:
        return f"{days // 30} months ago"
    return "over a year ago"


def listing(store, now: datetime) -> dict:
    """Every friend with their last catch-up and next plan, the ones ready for a catch-up first
    (longest past their rhythm first), then everyone else, most recently seen last."""
    friends = rows(store)
    with store.lock:
        contacts = store.db.execute(
            "SELECT friend_id, how, at, title FROM friend_contacts ORDER BY at").fetchall()
    by: dict[str, list] = {}
    for c in contacts:
        by.setdefault(c["friend_id"], []).append(c)
    now_s = _utc(now)
    plan_until = _utc(now + timedelta(days=PLAN_DAYS))
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    out, caught_week = [], 0
    for f in friends:
        past = [c for c in by.get(f["id"], []) if c["at"] <= now_s]
        future = [c for c in by.get(f["id"], []) if now_s < c["at"] <= plan_until and c["how"] == "calendar"]
        last = past[-1] if past else None
        last_by = {how: max((c["at"] for c in past if c["how"] == how), default=None) for how in HOWS}
        days = None
        if last:
            days = (now.date() - _parse(last["at"]).astimezone(now.tzinfo).date()).days
        ready = days is None or days >= f["every_days"]
        if last and _parse(last["at"]) >= week_start:
            caught_week += 1
        plan = future[0] if future else None
        out.append({
            **{k: f[k] for k in ("id", "name", "every_days", "handles", "also")},
            "last_at": last["at"] if last else None,
            "last_how": last["how"] if last else None,
            "last_seen": max(filter(None, [last_by["saw"], last_by["calendar"]]), default=None),
            "last_spoke": max(filter(None, [last_by["spoke"], last_by["message"]]), default=None),
            "days": days,
            "ago": _ago(days) if days is not None else None,
            "ready": ready and plan is None,  # a plan in the diary is already the catch-up
            "plan": {"at": plan["at"], "title": plan["title"]} if plan else None,
            "can_undo": any(c["how"] in TAPS for c in past),
        })

    def order(f):
        if f["ready"]:
            over = 10**6 if f["days"] is None else f["days"] / f["every_days"]
            return (0, -over, f["name"].lower())
        return (1, -(f["days"] if f["days"] is not None else 0), f["name"].lower())

    out.sort(key=order)
    return {"friends": out, "ready": sum(f["ready"] for f in out), "caught_up_week": caught_week,
            "plans": sum(1 for f in out if f["plan"])}


def ready_count(store, now: datetime) -> int | None:
    """How many friends are ready for a catch-up, or None while the Pi has no friends list (the Mac's count stands)."""
    if not rows(store):
        return None
    return listing(store, now)["ready"]


def caught_up_days(store, now: datetime, since: date) -> list[str]:
    """Days since `since` with a catch-up, one per friend per day, for the Friends pillar's weekly count."""
    with store.lock:
        got = store.db.execute(
            "SELECT friend_id, at FROM friend_contacts c "
            "JOIN friends f ON f.id = c.friend_id AND f.gone = 0 WHERE at <= ?", (_utc(now),)).fetchall()
    days = {(r["friend_id"], _parse(r["at"]).astimezone(now.tzinfo).date()) for r in got}
    return sorted(d.isoformat() for _, d in days if d >= since)


def with_friends(status: dict, store, now: datetime) -> dict:
    """The status with the Pi's own friends: the ready count replaces the Mac's friends_overdue, and this
    week's catch-ups count towards the Friends pillar alongside friend to-dos ticked off."""
    ready = ready_count(store, now)
    if ready is None:
        return status
    monday = now.date() - timedelta(days=now.weekday())
    days = caught_up_days(store, now, monday)
    pillars = dict(status.get("pillars") or {})
    fr = dict({"done_week": 0, "open": 0, "last_done": None, "next_due": None}, **pillars.get("friend", {}))
    fr["done_week"] = int(fr.get("done_week") or 0) + len(days)
    if days:
        fr["last_done"] = max(fr["last_done"] or days[-1], days[-1])
    fr["friends_ready"] = ready
    pillars["friend"] = fr
    return {**status, "friends_overdue": ready, "pillars": pillars}


# --- calendars the Pi fetches itself ------------------------------------------

LOOK_BACK = 30  # days of past events to read from a feed


def sync_calendars(store, feeds: list[dict], now: datetime, fetcher=None) -> dict:
    """Friends named in the Pi's own calendar feeds ("friend_calendars" in the calendars file: an iCloud or
    Google calendar's private .ics link). Works with the Mac asleep. Returns {"matched": n, "errors": [...]}."""
    from . import studycal
    fetcher = fetcher or studycal.fetch
    ensure(store)
    if not feeds or not rows(store):
        return {}
    events, errors = [], []
    lo, hi = now - timedelta(days=LOOK_BACK), now + timedelta(days=PLAN_DAYS)
    for c in feeds:
        name = c.get("name", "calendar") if isinstance(c, dict) else "calendar"
        try:
            for ev in studycal.parse_ics(fetcher(c["url"]), name, "", now.tzinfo):
                start = ev.start if isinstance(ev.start, datetime) else \
                    datetime.combine(ev.start, datetime.min.time(), now.tzinfo).replace(hour=12)
                if lo <= start <= hi:
                    events.append({"title": ev.title, "start": start.isoformat()})
        except Exception as e:  # one broken feed shouldn't stop the rest
            errors.append(f"{name}: {e}")
    if errors and not events:
        return {"matched": 0, "errors": errors}  # don't wipe plans because a feed was down
    return {"matched": match_events(store, events, now, "pi", (lo, hi)), "errors": errors}
