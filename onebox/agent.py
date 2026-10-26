"""The Mac side of One Inbox. Runs every 15 minutes from launchd.

Each run it:
  1. reads mail headers from Mail's local index (read-only, no bodies),
  2. sorts them (triage.py) and sends the result to the Pi,
  3. reads Things 3 (read-only) for today's count, next item, bills and XP,
     and Calendar (read-only) for the next few events,
  4. adds anything Ben marked "Add" on his phone to Things, via the Things URL scheme,
  5. adds study sessions and deadlines from his calendar feeds to Things (studycal.py).

Needs Full Disk Access for the Python that runs it (System Settings > Privacy & Security).
Config: ~/.config/onebox/agent.json (see docs/setup.md).
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import studycal
from .triage import Message, summary, triage

HOME = Path.home()
MAIL_GLOB = str(HOME / "Library/Mail/V*/MailData/Envelope Index")
THINGS_GLOB = str(HOME / "Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/ThingsData-*/"
                  "Things Database.thingsdatabase/main.sqlite")
MESSAGES_DB = str(HOME / "Library/Messages/chat.db")
CALENDAR_DBS = (  # newer macOS first
    str(HOME / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"),
    str(HOME / "Library/Calendars/Calendar.sqlitedb"),
)
CALENDAR_SKIP = ("Birthdays", "Fødselsdage", "Holidays in Denmark", "Helligdage i Danmark", "Siri Suggestions")
CONFIG_PATH = Path(os.environ.get("ONEBOX_AGENT_CONFIG", HOME / ".config/onebox/agent.json"))
CALENDAR_SEEN = CONFIG_PATH.parent / "calendar-seen.json"

SKIP_MAILBOXES = ("junk", "spam", "trash", "deleted", "drafts", "blocked", "outbox")
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def ro_connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def newest(pattern: str) -> str | None:
    found = sorted(glob.glob(pattern), key=os.path.getmtime)
    return found[-1] if found else None


# --- Mail ----------------------------------------------------------------

def mailbox_name(url: str) -> str:
    return urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1]) if url else ""


def read_mail(db_path: str, days: int = 21, account_labels: dict | None = None,
              now: datetime | None = None) -> list[Message]:
    now = now or datetime.now(timezone.utc)
    since = int((now - timedelta(days=days)).timestamp())
    labels = account_labels or {}
    con = ro_connect(db_path)
    try:
        rows = con.execute(
            """SELECT m.ROWID AS rowid, m.date_received, m.read,
                      COALESCE(m.subject_prefix, '') || COALESCE(s.subject, '') AS subject,
                      COALESCE(a.address, '') AS address, COALESCE(a.comment, '') AS name,
                      mb.url AS url
               FROM messages m
               LEFT JOIN subjects s ON s.ROWID = m.subject
               LEFT JOIN addresses a ON a.ROWID = m.sender
               JOIN mailboxes mb ON mb.ROWID = m.mailbox
               WHERE m.deleted = 0 AND m.date_received >= ?""",
            (since,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        box = mailbox_name(r["url"])
        if any(s in box.lower() for s in SKIP_MAILBOXES):
            continue
        account_key = urllib.parse.urlparse(r["url"]).netloc
        out.append(Message(
            msg_id=f"{account_key}:{r['rowid']}",
            sender_name=r["name"], sender_addr=r["address"], subject=r["subject"],
            received=datetime.fromtimestamp(r["date_received"], timezone.utc),
            mailbox="Sent" if "sent" in box.lower() else box,
            account=labels.get(account_key, "mail"), unread=not r["read"],
        ))
    return out


# --- Things --------------------------------------------------------------

def things_date(d: date) -> int:
    """Things 3 stores dates as (year << 16) | (month << 12) | (day << 7)."""
    return (d.year << 16) | (d.month << 12) | (d.day << 7)


def from_things_date(v: int) -> date:
    return date(v >> 16, (v >> 12) & 0xF, (v >> 7) & 0x1F)


KIND_WORDS = [("bill", "bill"), ("pay", "bill"), ("betal", "bill"), ("school", "school"), ("kids", "school"),
              ("aula", "school"), ("york", "msc"), ("msc", "msc"), ("job", "job"), ("danish", "danish"),
              ("dansk", "danish"), ("friend", "friend"), ("travel", "travel"), ("trip", "travel"),
              ("admin", "admin")]


def kind_of(*texts: str) -> str:
    hay = " ".join(t for t in texts if t).lower()
    for word, kind in KIND_WORDS:
        if word in hay:
            return kind
    return "other"


TODAY_MAX = 40  # enough for any real Today list, and keeps the phone feed small


def things_status(db_path: str, now: datetime) -> dict:
    today = now.date()
    t_today = things_date(today)
    midnight = datetime.combine(today, time.min, tzinfo=now.tzinfo).timestamp()
    con = ro_connect(db_path)
    try:
        open_today = con.execute(
            """SELECT t.uuid, t.title, t.deadline, COALESCE(a.title, pa.title, '') AS area, COALESCE(p.title, '') AS project
               FROM TMTask t
               LEFT JOIN TMArea a ON a.uuid = t.area
               LEFT JOIN TMTask p ON p.uuid = t.project
               LEFT JOIN TMArea pa ON pa.uuid = p.area
               WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0 AND t.start = 1
                 AND t.startDate IS NOT NULL AND t.startDate <= ?
               ORDER BY t.todayIndex""",
            (t_today,),
        ).fetchall()
        overdue = con.execute(
            """SELECT COUNT(*) FROM TMTask WHERE type = 0 AND status = 0 AND trashed = 0
               AND deadline IS NOT NULL AND deadline < ?""", (t_today,)).fetchone()[0]
        done_today = con.execute(
            "SELECT COUNT(*) FROM TMTask WHERE type = 0 AND status = 3 AND trashed = 0 AND stopDate >= ?",
            (midnight,)).fetchone()[0]
        completed_ids = [r[0] for r in con.execute(
            "SELECT uuid FROM TMTask WHERE type = 0 AND status = 3 AND stopDate >= ?",
            (midnight - 3 * 86400,))]
        week = things_date(today + timedelta(days=7))
        bills = con.execute(
            """SELECT t.title, t.deadline, COALESCE(a.title, pa.title, '') AS area
               FROM TMTask t
               LEFT JOIN TMArea a ON a.uuid = t.area
               LEFT JOIN TMTask p ON p.uuid = t.project
               LEFT JOIN TMArea pa ON pa.uuid = p.area
               WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0
                 AND t.deadline IS NOT NULL AND t.deadline <= ?
               ORDER BY t.deadline""", (week,)).fetchall()
    finally:
        con.close()
    bill = None
    for b in bills:
        if kind_of(b["title"], b["area"]) == "bill":
            d = from_things_date(b["deadline"])
            bill = d.strftime("%a") if 0 <= (d - today).days < 7 else d.strftime("%-d %b")
            break
    first = open_today[0] if open_today else None
    today_list = [{"title": t["title"], "kind": kind_of(t["title"], t["area"], t["project"]),
                   "due": from_things_date(t["deadline"]).isoformat() if t["deadline"] else None}
                  for t in open_today[:TODAY_MAX]]
    return {
        "at": now.isoformat(timespec="seconds"),
        "left": len(open_today),
        "done_today": done_today,
        "completed_ids": completed_ids,
        "overdue": overdue,
        "next": first["title"] if first else None,
        "next_kind": kind_of(first["title"], first["area"], first["project"]) if first else None,
        "bill": bill,
        "today": today_list,
    }


# --- Calendar ------------------------------------------------------------

def next_events(db_path: str, now: datetime, hours: int = 48, skip: tuple = CALENDAR_SKIP,
                limit: int = 5) -> list[dict]:
    """Timed events starting in the next `hours`, soonest first. Repeating events come from
    Calendar's own occurrence cache. All-day events and skipped calendars are left out."""
    start = (now - APPLE_EPOCH).total_seconds()
    con = ro_connect(db_path)
    try:
        rows = con.execute(
            """SELECT ci.summary AS title, COALESCE(oc.occurrence_start_date, oc.occurrence_date) AS start,
                      COALESCE(c.title, '') AS calendar
               FROM OccurrenceCache oc
               JOIN CalendarItem ci ON ci.ROWID = oc.event_id
               LEFT JOIN Calendar c ON c.ROWID = ci.calendar_id
               WHERE COALESCE(ci.all_day, 0) = 0
                 AND COALESCE(oc.occurrence_start_date, oc.occurrence_date) >= ?
                 AND COALESCE(oc.occurrence_start_date, oc.occurrence_date) < ?
               ORDER BY 2""",
            (start, start + hours * 3600),
        ).fetchall()
    finally:
        con.close()
    out, seen = [], set()
    for r in rows:
        if not r["title"] or r["calendar"] in skip or (r["title"], r["start"]) in seen:
            continue
        seen.add((r["title"], r["start"]))
        when = (APPLE_EPOCH + timedelta(seconds=r["start"])).astimezone(now.tzinfo)
        out.append({"title": r["title"], "start": when.isoformat(timespec="minutes")})
        if len(out) == limit:
            break
    return out


def read_calendar(cfg: dict, now: datetime) -> tuple[list[dict], str]:
    path = cfg.get("calendar_db") or next((p for p in CALENDAR_DBS if os.path.exists(p)), None)
    if not path:
        return [], "Calendar database not found. Is Full Disk Access on?"
    try:
        events = next_events(path, now, skip=tuple(cfg.get("calendar_skip", CALENDAR_SKIP)))
    except sqlite3.Error as e:  # Apple changes this schema now and then; never let it stop a sync
        return [], f"Could not read Calendar: {e}"
    return events, f"{len(events)} events in the next 48 h"


# --- Friends (Messages) ---------------------------------------------------

def friends_overdue(chat_db: str, friends: list[dict], now: datetime) -> int:
    """friends: [{"name": "Tom", "every_days": 21, "handles": ["+4512345678", "tom@x.com"]}]"""
    if not friends or not os.path.exists(chat_db):
        return 0
    con = ro_connect(chat_db)
    late = 0
    try:
        for f in friends:
            handles = f.get("handles") or []
            if not handles:
                continue
            q = ",".join("?" * len(handles))
            last = con.execute(
                f"SELECT MAX(m.date) FROM message m JOIN handle h ON h.ROWID = m.handle_id WHERE h.id IN ({q})",
                handles).fetchone()[0]
            if last is None:
                late += 1
                continue
            secs = last / 1e9 if last > 1e12 else last  # newer macOS stores nanoseconds
            last_dt = APPLE_EPOCH + timedelta(seconds=secs)
            if (now - last_dt).days > int(f.get("every_days", 30)):
                late += 1
    finally:
        con.close()
    return late


# --- Talking to the Pi and to Things --------------------------------------

def call(server: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        server.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def things_add_url(items: list[dict]) -> str:
    todos = []
    for it in items:
        attrs = {
            "title": it["task"],
            "notes": f"{it['title']}\nFrom {it.get('sender', '')}, {it['received'][:10]}\n{it.get('why', '')}\n"
                     f"Area: {it.get('area', '')}\n(added by One Inbox)",
        }
        if it.get("auto"):  # e-Boks, Aula and quick captures go to Today, so the watch shows them
            attrs["when"] = "today"
        todos.append({"type": "to-do", "attributes": attrs})
    return "things:///json?data=" + urllib.parse.quote(json.dumps(todos))


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg.setdefault("server", os.environ.get("ONEBOX_SERVER", "http://inbox.home:8787"))
    cfg.setdefault("token", os.environ.get("ONEBOX_AGENT_TOKEN", ""))
    cfg.setdefault("days", 21)
    return cfg


def run_once(cfg: dict, now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or datetime.now().astimezone()
    report: dict = {}
    mail_db = cfg.get("mail_db") or newest(MAIL_GLOB)
    items = []
    if mail_db:
        msgs = read_mail(mail_db, cfg["days"], cfg.get("account_labels"), now)
        triaged = triage(msgs, now)
        items = [i.to_dict() for i in triaged]
        report["mail"] = summary(triaged)
    else:
        report["mail"] = "Mail index not found. Is Full Disk Access on?"
    payload: dict = {"items": items}
    things_db = cfg.get("things_db") or newest(THINGS_GLOB)
    if things_db:
        status = things_status(things_db, now)
        status["friends_overdue"] = friends_overdue(cfg.get("messages_db", MESSAGES_DB), cfg.get("friends", []), now)
        status["events"], report["calendar"] = read_calendar(cfg, now)
        payload["things"] = status
        report["things"] = {k: v for k, v in status.items() if k != "completed_ids"}
    if dry_run:
        report["would_send"] = len(items)
        if cfg.get("calendars") or cfg.get("deadlines"):
            report["calendar"] = calendar_preview(cfg, now)
        return report
    call(cfg["server"], cfg["token"], "POST", "/api/ingest", payload)
    outbox = call(cfg["server"], cfg["token"], "GET", "/api/outbox").get("items", [])
    if outbox:
        subprocess.run(["open", "-g", things_add_url(outbox)], check=True)
        call(cfg["server"], cfg["token"], "POST", "/api/outbox/ack", {"ids": [i["id"] for i in outbox]})
    report["added_to_things"] = len(outbox)
    report["calendar"] = sync_calendars(cfg, now)
    return report


def calendar_preview(cfg: dict, now: datetime) -> dict:
    events, errors = studycal.collect(cfg, now)
    seen = studycal.load_seen(Path(cfg.get("calendar_seen", CALENDAR_SEEN)))
    new = studycal.due_now(events, seen, now)
    return {"events": len(events), "would_add": [t["attributes"]["title"] for t in studycal.things_json(new, now.date())],
            "errors": errors}


def sync_calendars(cfg: dict, now: datetime) -> dict:
    if not cfg.get("calendars") and not cfg.get("deadlines"):
        return {}
    seen_path = Path(cfg.get("calendar_seen", CALENDAR_SEEN))
    events, errors = studycal.collect(cfg, now)
    seen = studycal.load_seen(seen_path)
    new = studycal.due_now(events, seen, now)
    todos = studycal.things_json(new, now.date())
    if todos:
        subprocess.run(["open", "-g", "things:///json?data=" + urllib.parse.quote(json.dumps(todos))], check=True)
        for ev in new:
            seen[ev.key()] = now.date().isoformat()
    studycal.save_seen(seen_path, seen, now.date())
    return {"added": len(todos), "errors": errors}


def main(argv: list[str] = sys.argv[1:]) -> None:
    cfg = load_config()
    if not cfg["token"] and "--dry-run" not in argv:
        raise SystemExit(f"No token. Add it to {CONFIG_PATH} (see docs/setup.md).")
    print(json.dumps(run_once(cfg, dry_run="--dry-run" in argv), indent=2, default=str))


if __name__ == "__main__":
    main()
