"""Study calendars into Things: live sessions on the day, deadlines with heads-ups.

The Mac agent reads each calendar feed (an .ics link from the uni VLE or timetable)
and any deadlines typed into the config, then adds to Things:

  - a session (lecture, seminar, live class): one to-do on that day, with a Things
    reminder 15 minutes before it starts, so the watch shows it that morning;
  - a deadline: a to-do that lands in Today 7 days before, with the Things deadline
    set to the due date, plus a short "due in 2 days" nudge.

Each event is added once. The state file remembers what was sent, keyed by the
event's UID and start time, so a moved session is added again at its new time.

Config (in ~/.config/onebox/agent.json):

  "calendars": [{"name": "York MSc", "url": "webcal://…/calendar.ics", "list": "York MSc"}],
  "deadlines": [{"title": "Module 1 assignment", "due": "2026-12-14", "list": "York MSc"}]

Standard library only. Handles the .ics that Moodle, Canvas, Blackboard and
MyTimetable export: single events with UTC, TZID or all-day dates. Repeating
events (RRULE) only show their first date, which none of those exports rely on.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

SESSION_AHEAD_DAYS = 14      # add sessions up to two weeks ahead
DEADLINE_AHEAD_DAYS = 120    # deadlines can be added a whole module ahead
HEADS_UP_DAYS = 7
NUDGE_DAYS = 2
REMIND_BEFORE = timedelta(minutes=15)
MORNING = time(9, 0)

DEADLINE_WORDS = ("due", "deadline", "submission", "submit", "closes", "hand in", "hand-in",
                  "assessment", "assignment", "exam", "quiz")


@dataclass
class Event:
    uid: str
    title: str
    start: datetime | date
    end: datetime | date | None = None
    location: str = ""
    url: str = ""
    description: str = ""
    calendar: str = ""
    list: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @property
    def is_deadline(self) -> bool:
        if self.extra.get("deadline"):
            return True
        text = self.title.lower()
        if any(w in text for w in DEADLINE_WORDS):
            return True
        # Moodle and Canvas export due dates as a zero-length event.
        return isinstance(self.start, datetime) and (self.end is None or self.end == self.start)

    def key(self) -> str:
        raw = f"{self.calendar}|{self.uid}|{self.start.isoformat()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --- Reading .ics ---------------------------------------------------------

def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def parse_dt(value: str, params: dict, local_tz) -> datetime | date:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        dt = dt.replace(tzinfo=timezone.utc)
    elif params.get("TZID") and ZoneInfo is not None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"].strip('"')))
        except Exception:
            dt = dt.replace(tzinfo=local_tz)
    else:
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(local_tz)


def parse_ics(text: str, calendar: str = "", list_name: str = "", local_tz=None) -> list[Event]:
    local_tz = local_tz or datetime.now().astimezone().tzinfo
    events: list[Event] = []
    cur: dict | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None and "DTSTART" in cur:
                status = cur.get("STATUS", ("", {}))[0].upper()
                if status != "CANCELLED":
                    start = parse_dt(*cur["DTSTART"], local_tz)
                    end = parse_dt(*cur["DTEND"], local_tz) if "DTEND" in cur else None
                    g = lambda k: unescape(cur.get(k, ("", {}))[0]).strip()
                    events.append(Event(
                        uid=g("UID") or f"{g('SUMMARY')}@{start.isoformat()}",
                        title=g("SUMMARY") or "(untitled)", start=start, end=end,
                        location=g("LOCATION"), url=g("URL"), description=g("DESCRIPTION"),
                        calendar=calendar, list=list_name,
                    ))
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *rest = head.split(";")
        params = dict(p.split("=", 1) for p in rest if "=" in p)
        cur.setdefault(name.upper(), (value, params))
    return events


def fetch(url: str) -> str:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if url.startswith("/") or url.startswith("~"):
        return Path(url).expanduser().read_text()
    req = urllib.request.Request(url, headers={"User-Agent": "OneInbox/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def config_deadlines(items: list[dict], local_tz) -> list[Event]:
    out = []
    for d in items or []:
        due = datetime.strptime(d["due"], "%Y-%m-%d").date()
        out.append(Event(uid=f"cfg:{d['title']}", title=d["title"], start=due, calendar="config",
                         list=d.get("list", ""), description=d.get("notes", ""), extra={"deadline": True}))
    return out


# --- Turning events into Things to-dos -----------------------------------

def day_of(v: datetime | date) -> date:
    return v.date() if isinstance(v, datetime) else v


def stamp(d: date, t: time | None = None) -> str:
    """Things 'when': a date, or date@time to also set a reminder."""
    return d.isoformat() + (f"@{t.strftime('%H:%M')}" if t else "")


def notes_for(ev: Event) -> str:
    parts = [p for p in (ev.location, ev.url, ev.description[:500]) if p]
    parts.append(f"From {ev.calendar} calendar (added by One Inbox)")
    return "\n".join(parts)


def todos_for(ev: Event, today: date) -> list[dict]:
    base = {"notes": notes_for(ev)}
    if ev.list:
        base["list"] = ev.list
    if ev.is_deadline:
        due = day_of(ev.start)
        due_txt = due.strftime("%a %-d %b")
        if isinstance(ev.start, datetime):
            due_txt += ev.start.strftime(" %H:%M")
        heads_up = max(today, due - timedelta(days=HEADS_UP_DAYS))
        todos = [dict(base, title=f"{ev.title} (due {due_txt})", when=stamp(heads_up, MORNING),
                      deadline=due.isoformat())]
        nudge = due - timedelta(days=NUDGE_DAYS)
        if nudge > heads_up:
            todos.append(dict(base, title=f"2 days left: {ev.title} (due {due_txt})", when=stamp(nudge, MORNING)))
        return todos
    day = day_of(ev.start)
    if ev.all_day:
        return [dict(base, title=ev.title, when=stamp(day))]
    remind = (ev.start - REMIND_BEFORE).time()
    return [dict(base, title=f"{ev.start.strftime('%H:%M')} {ev.title}", when=stamp(day, remind))]


def due_now(events: list[Event], seen: dict, now: datetime) -> list[Event]:
    """Events not sent yet that are close enough to add now."""
    today = now.date()
    out = []
    for ev in events:
        if ev.key() in seen:
            continue
        day = day_of(ev.start)
        if isinstance(ev.start, datetime) and ev.start < now:
            continue  # already started or already due
        if day < today:
            continue
        ahead = DEADLINE_AHEAD_DAYS if ev.is_deadline else SESSION_AHEAD_DAYS
        if (day - today).days <= ahead:
            out.append(ev)
    return sorted(out, key=lambda e: (day_of(e.start), e.title))


def load_seen(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_seen(path: Path, seen: dict, today: date) -> None:
    keep = (today - timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= keep}  # forget old ones so the file stays small
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, indent=0, sort_keys=True))


def collect(cfg: dict, now: datetime, fetcher=fetch) -> tuple[list[Event], list[str]]:
    """All events from the configured feeds and typed-in deadlines, plus any feed errors."""
    tz = now.tzinfo
    events: list[Event] = []
    errors: list[str] = []
    for c in cfg.get("calendars") or []:
        name = c.get("name", "calendar")
        try:
            events += parse_ics(fetcher(c["url"]), name, c.get("list", ""), tz)
        except Exception as e:  # one broken feed shouldn't stop mail sync
            errors.append(f"{name}: {e}")
    events += config_deadlines(cfg.get("deadlines"), tz)
    return events, errors


def things_json(events: list[Event], today: date) -> list[dict]:
    return [{"type": "to-do", "attributes": attrs} for ev in events for attrs in todos_for(ev, today)]
