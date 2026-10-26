"""Job tracker into Things: what to apply for next, and interviews.

Ben's job tracker (a Streamlit app on the Mac) keeps its own SQLite database. The Mac
agent opens it read-only and mirrors the tracker's "Apply next" list into Things, so
deadlines show in Today and on the watch:

  - a role with a deadline: "Apply: Company, Role (due Fri 31 Oct)" that lands in Today
    5 days before, with the Things deadline set, plus a "last day tomorrow" nudge;
  - a rolling or starred role with no date: lands in Today straight away;
  - any other role the tracker puts in Apply next: goes to the list with no date;
  - an interview: a to-do that day with a reminder an hour before, and a prep to-do the day before.

Each to-do is added once. If a deadline changes, the new date is added again.
When a role leaves Apply next (applied, skipped, closed) or an interview is over, the agent
ticks its to-do off in Things. That needs the Things URL auth token in the config;
without it the agent only reports which to-dos are stale.

Nothing from the tracker goes to the Pi. It only travels from the database to Things,
both on the Mac.

Config (in ~/.config/onebox/agent.json):

  "job_tracker": {"db": "~/Documents/job-tracker/data/job_tracker.db", "list": "Job Search",
                  "tracker_url": "http://localhost:8501"},
  "things_auth_token": "…"   (optional, Things > Settings > General > Enable Things URLs > Manage)
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

DEFAULT_DB = "~/Documents/job-tracker/data/job_tracker.db"
DEFAULT_LIST = "Job Search"
DEFAULT_URL = "http://localhost:8501"

HEADS_UP_DAYS = 5
REMIND_BEFORE = timedelta(hours=1)
MORNING = time(9, 0)
MARKER = re.compile(r"(job-tracker:(?:draft|lead|interview):\d+)(?: #(\w+))?")
SENT = ("applied", "interview", "offer")

# The tracker's own urgency ranks (deadlines.py): today, critical (<= 3 days), soon (<= 7), rolling.
URGENT_DAYS = 7


@dataclass
class Role:
    kind: str            # "draft" (in applications, being written), "lead" (in jobs), "interview"
    id: int
    company: str
    title: str
    deadline: date | None = None
    rolling: bool = False
    priority: bool = False
    fit: str = ""
    url: str = ""
    note: str = ""
    interview_at: datetime | date | None = None
    passed: bool = False  # deadline gone; kept so its to-do stays open, never added anew

    @property
    def marker(self) -> str:
        return f"job-tracker:{self.kind}:{self.id}"

    def key(self) -> str:
        when = self.interview_at or self.deadline
        raw = f"{self.marker}|{when.isoformat() if when else ''}|{int(self.rolling)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @property
    def name(self) -> str:
        return f"{self.company.strip()}, {self.title.strip()}"


# --- Reading the tracker --------------------------------------------------

def parse_day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def parse_when(v, tz) -> datetime | date | None:
    """interview_at is 'YYYY-MM-DD HH:MM' or just a date."""
    if not v:
        return None
    v = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return parse_day(v)


def connect(path: str) -> sqlite3.Connection:
    full = str(Path(path).expanduser())
    con = sqlite3.connect(f"file:{urllib.parse.quote(full)}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def read_roles(db_path: str, now: datetime, show_danish: bool = False) -> tuple[list[Role], set[str]]:
    """The tracker's Apply next list (same rules as its Pipeline tab) plus upcoming interviews,
    and the markers of roles Ben has since applied for (their to-dos count as done)."""
    today = now.date()
    con = connect(db_path)
    try:
        apps = [dict(r) for r in con.execute("SELECT * FROM applications")]
        jobs = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE dismissed = 0")]
    finally:
        con.close()

    roles: list[Role] = []
    for a in apps:
        status = a.get("status")
        if status in ("interview", "offer"):
            when = parse_when(a.get("interview_at"), now.tzinfo)
            day = when.date() if isinstance(when, datetime) else when
            if day and day >= today:
                roles.append(Role("interview", a["id"], a["company"], a["job_title"],
                                  url=a.get("source_url") or "", interview_at=when))
            continue
        if status != "saved":
            continue
        # 'saved' also holds plain bookmarks; the tracker only shows real drafts.
        if not (a.get("attachment_paths") or a.get("deadline") or a.get("deadline_rolling") == 1
                or a.get("priority") == 1):
            continue
        roles.append(Role("draft", a["id"], a["company"], a["job_title"], deadline=parse_day(a.get("deadline")),
                          rolling=a.get("deadline_rolling") == 1, priority=a.get("priority") == 1,
                          url=a.get("source_url") or "", note=a.get("deadline_note") or ""))

    done = {f"job-tracker:draft:{a['id']}" for a in apps if a.get("status") in SENT}
    tracked = {((a.get("company") or "").strip().lower(), (a.get("job_title") or "").strip().lower()): a.get("status")
               for a in apps}
    for j in jobs:
        if j.get("likely_closed") or j.get("fit_tier") == "Not eligible":
            continue
        pair = ((j.get("company") or "").strip().lower(), (j.get("title") or "").strip().lower())
        if pair in tracked:
            if tracked[pair] in SENT:
                done.add(f"job-tracker:lead:{j['id']}")
            continue
        priority = j.get("priority") == 1
        rolling = j.get("deadline_rolling") == 1
        has_signal = j.get("deadline") or rolling or priority
        hand_vetted = str(j.get("source") or "").endswith("-manual")
        if not (hand_vetted or priority or (has_signal and j.get("fit_tier") in ("Strong fit", "Possible fit"))):
            continue
        if j.get("danish_required") and not priority and not show_danish:
            continue
        roles.append(Role("lead", j["id"], j.get("company") or "", j["title"], deadline=parse_day(j.get("deadline")),
                          rolling=rolling, priority=priority, fit=j.get("fit_tier") or "",
                          url=j.get("url") or "", note=j.get("deadline_note") or ""))

    out = []
    for r in roles:
        if r.kind == "interview":
            out.append(r)
            continue
        if r.deadline and r.deadline < today:
            r.passed = True  # the tracker files these under "Deadline passed", still open
            out.append(r)
            continue
        days = (r.deadline - today).days if r.deadline else None
        urgent = (days is not None and days <= URGENT_DAYS) or r.rolling
        # Apply next shows urgent roles, drafts and strong fits; the rest sit under "More leads".
        if urgent or r.kind == "draft" or r.fit == "Strong fit" or r.priority:
            out.append(r)
    return out, done


# --- Turning roles into Things to-dos -------------------------------------

def stamp(d: date, t: time | None = None) -> str:
    return d.isoformat() + (f"@{t.strftime('%H:%M')}" if t else "")


def notes_for(r: Role, tracker_url: str) -> str:
    parts = []
    if r.fit:
        parts.append(r.fit)
    if r.note:
        parts.append(r.note[:200])
    if r.url:
        parts.append(r.url)
    parts.append(f"Tracker: {tracker_url}")
    parts.append(f"{r.marker} #{r.key()} (added by One Inbox)")
    return "\n".join(parts)


def todos_for(r: Role, today: date, list_name: str, tracker_url: str) -> list[dict]:
    base = {"notes": notes_for(r, tracker_url)}
    if list_name:
        base["list"] = list_name
    if r.kind == "interview":
        when = r.interview_at
        day = when.date() if isinstance(when, datetime) else when
        if isinstance(when, datetime):
            todos = [dict(base, title=f"Interview {when.strftime('%H:%M')}: {r.name}",
                          when=stamp(day, (when - REMIND_BEFORE).time()), deadline=day.isoformat())]
        else:
            todos = [dict(base, title=f"Interview: {r.name}", when=stamp(day), deadline=day.isoformat())]
        prep = day - timedelta(days=1)
        if prep >= today:
            todos.append(dict(base, title=f"Prep for interview: {r.name}", when=stamp(prep, MORNING)))
        return todos
    if r.deadline:
        due_txt = r.deadline.strftime("%a %-d %b")
        heads_up = max(today, r.deadline - timedelta(days=HEADS_UP_DAYS))
        if r.rolling:
            heads_up = today  # rolling roles can close before the date, so start now
        label = "Apply early" if r.rolling else "Apply"
        todos = [dict(base, title=f"{label}: {r.name} (due {due_txt})", when=stamp(heads_up),
                      deadline=r.deadline.isoformat())]
        last = r.deadline - timedelta(days=1)
        if last > heads_up:
            todos.append(dict(base, title=f"Last day tomorrow: {r.name}", when=stamp(last, MORNING)))
        return todos
    if r.rolling:
        return [dict(base, title=f"Apply early (rolling): {r.name}", when="today")]
    if r.priority:
        return [dict(base, title=f"Apply: {r.name}", when="today")]
    return [dict(base, title=f"Apply: {r.name} (no deadline stated)")]


def new_roles(roles: list[Role], seen: dict) -> list[Role]:
    return sorted((r for r in roles if not r.passed and r.key() not in seen),
                  key=lambda r: (r.kind != "interview", r.deadline or date.max, r.name))


def things_json(roles: list[Role], today: date, list_name: str, tracker_url: str) -> list[dict]:
    return [{"type": "to-do", "attributes": a} for r in roles for a in todos_for(r, today, list_name, tracker_url)]


# --- Ticking off to-dos for roles that left Apply next ---------------------

def open_todos(things_db: str) -> list[tuple[str, str, str]]:
    """Open Things to-dos this module added, as (uuid, marker, key)."""
    con = connect(things_db)
    try:
        rows = con.execute(
            "SELECT uuid, notes FROM TMTask WHERE type = 0 AND status = 0 AND trashed = 0 "
            "AND notes LIKE '%job-tracker:%'").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        m = MARKER.search(r["notes"] or "")
        if m:
            out.append((r["uuid"], m.group(1), m.group(2) or ""))
    return out


def tidy(roles: list[Role], done: set[str], todos: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    """Which open to-dos to close: "completed" when Ben applied, "canceled" when the role left
    Apply next for another reason (skipped, closed) or its date changed and a new to-do replaced it."""
    live = {r.marker: r.key() for r in roles}
    out: dict[str, list[str]] = {"completed": [], "canceled": []}
    for uuid, marker, key in todos:
        if marker in done:
            out["completed"].append(uuid)
        elif marker not in live or (key and key != live[marker] and not _passed(roles, marker)):
            out["canceled"].append(uuid)
    return out


def _passed(roles: list[Role], marker: str) -> bool:
    return any(r.marker == marker and r.passed for r in roles)


def update_url(uuid: str, token: str, how: str) -> str:
    return "things:///update?" + urllib.parse.urlencode({"id": uuid, how: "true", "auth-token": token})
