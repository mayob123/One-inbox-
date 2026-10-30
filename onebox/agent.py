"""The Mac side of One Inbox. Runs every 15 minutes from launchd.

Each run it:
  1. reads mail headers from Mail's local index (read-only),
  2. sorts them (triage.py), reads the body of the few that need Ben (links, attachments, a short excerpt)
     and sends the result to the Pi,
  3. reads Things 3 (read-only) for today's count, next item, bills and XP,
     Calendar (read-only) for the next few events, and the job tracker (read-only) for
     interviews and application deadlines,
  4. adds anything Ben marked "Add" on his phone to Things, via the Things URL scheme,
  5. adds study sessions and deadlines from his calendar feeds to Things (studycal.py).

Needs Full Disk Access for the Python that runs it (System Settings > Privacy & Security).
Config: ~/.config/onebox/agent.json (see docs/setup.md).
"""

from __future__ import annotations

import email
import email.policy
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import studycal
from .triage import JOB_ALERT_SENDERS, JOB_ALERTS, Message, summary, triage

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
JOB_TRACKER_DB = str(HOME / "Documents/job-tracker/data/job_tracker.db")
JOB_LEADS = ("Strong fit", "Possible fit")  # job tracker fit tiers worth a deadline nudge
LEADS_SHOWN = 5  # the best open leads the phone lists on the job hunt page
JOB_APPLY = ("Strong fit",)  # tracker leads that get an "Apply:" to-do before their deadline ("job_apply_tiers")

SKIP_MAILBOXES = ("junk", "spam", "trash", "deleted", "drafts", "blocked", "outbox")
JUNK_MAILBOXES = ("junk", "spam")
RESCUE_FROM_JUNK = JOB_ALERT_SENDERS  # Mail files job alerts as junk; read these senders from Junk anyway
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
ICLOUD = HOME / "Library/Mobile Documents/com~apple~CloudDocs"


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
            rescue = (r["address"] or "").lower() in RESCUE_FROM_JUNK and any(s in box.lower() for s in JUNK_MAILBOXES)
            if not rescue:
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


# --- Mail bodies: the details of the emails that need Ben ----------------

DETAIL_DAYS = 7      # only recent emails get their body read
DETAIL_MAX = 30      # and at most this many per run
EXCERPT_MAX = 1200
LINKS_MAX = 12
URL = re.compile(r"https?://[^\s<>\"')\]]+")
SKIP_LINK = re.compile(r"unsubscribe|afmeld|\.(png|jpe?g|gif)(\?|$)|/track/|list-manage|mailchimp", re.I)
# Where a reply's quoted history starts: "On Tue, ... wrote:", "Den ... skrev ...:", a ">" line, Outlook's "From:".
HTML_QUOTE = re.compile(r'<blockquote|class="gmail_quote|id="divRplyFwdMsg|id="appendonsend', re.I)
QUOTE = re.compile(r"^(On .+wrote:|Den .+skrev.*:|>|-----Original Message|From: .+|Fra: .+)", re.M)


def emlx_files(mail_dir: str, rowids: set[str]) -> tuple[dict, dict]:
    """Finds each message's .emlx and its downloaded attachments under ~/Library/Mail/V*, by rowid."""
    bodies, attachments = {}, {}
    for root, dirs, files in os.walk(mail_dir):
        base = os.path.basename(root)
        if base in rowids and os.path.basename(os.path.dirname(root)) == "Attachments":
            for sub, _, names in os.walk(root):
                attachments.setdefault(base, []).extend(os.path.join(sub, n) for n in names if not n.startswith("."))
        for f in files:
            if f.endswith(".emlx"):
                rid = f.split(".", 1)[0]
                if rid in rowids:
                    bodies[rid] = os.path.join(root, f)
    return bodies, attachments


def parse_emlx(path: str) -> email.message.EmailMessage:
    """An .emlx is a byte count line, the raw message, then an Apple plist."""
    with open(path, "rb") as fh:
        n = int(fh.readline().strip() or 0)
        raw = fh.read(n)
    return email.message_from_bytes(raw, policy=email.policy.default)


def message_details(msg: email.message.EmailMessage) -> dict:
    """The new part of the text (not the quoted history), its links, attachment names and Message-ID."""
    plain, rich = "", ""
    names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name:
            names.append(name)
            continue
        try:
            text = part.get_content()
        except (LookupError, ValueError):
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not rich:
            rich = text
    q = HTML_QUOTE.search(rich)
    rich = rich[:q.start()] if q else rich
    if not plain and rich:
        plain = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>|<br\s*/?>|</p>", "\n", rich)))
    m = QUOTE.search(plain)
    text = (plain[:m.start()] if m else plain).strip()
    links = URL.findall(text) + re.findall(r'href="(https?://[^"]+)"', rich)
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text))
    seen, keep = set(), []
    for link in links:
        link = html.unescape(link).rstrip(".,;")
        if link not in seen and not SKIP_LINK.search(link):
            seen.add(link)
            keep.append(link)
    mid = (msg.get("Message-ID") or "").strip().strip("<>")
    return {"excerpt": text[:EXCERPT_MAX], "links": keep[:LINKS_MAX], "attachments": names,
            "message_id": mid}


def mail_link(message_id: str) -> str:
    """Opens the email in Mail, on the Mac or the iPhone."""
    return "message:" + urllib.parse.quote(f"<{message_id}>", safe="@") if message_id else ""


def add_details(items: list[dict], mail_db: str, now: datetime, save_to: Path | None = ICLOUD) -> int:
    """Reads the body of recent emails that need Ben, and copies a homework email's attachments to
    iCloud Drive > One Inbox > <kind> so they open on the phone. Returns how many got details."""
    want = [it for it in items if it.get("status") == "decide" and it.get("ref", "").count(":") == 1
            and now - datetime.fromisoformat(it["received"]) <= timedelta(days=DETAIL_DAYS)][:DETAIL_MAX]
    if not want:
        return 0
    rowids = {it["ref"].rsplit(":", 1)[1] for it in want}
    mail_dir = os.path.dirname(os.path.dirname(mail_db))
    bodies, files = emlx_files(mail_dir, rowids)
    n = 0
    for it in want:
        rid = it["ref"].rsplit(":", 1)[1]
        if rid not in bodies:
            continue
        try:
            d = message_details(parse_emlx(bodies[rid]))
        except (OSError, ValueError):
            continue
        d["mail_link"] = mail_link(d.pop("message_id"))
        if save_to and it.get("auto") and files.get(rid) and save_to.is_dir():
            folder = save_to / "One Inbox" / (it.get("area") or "Mail")
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for src in files[rid]:
                dest = folder / os.path.basename(src)
                if not dest.exists():
                    shutil.copy2(src, dest)
                saved.append(f"iCloud Drive > One Inbox > {folder.name} > {dest.name}")
            d["saved"] = saved
        it.update(d)
        n += 1
    return n


# --- Things --------------------------------------------------------------

def things_date(d: date) -> int:
    """Things 3 stores dates as (year << 16) | (month << 12) | (day << 7)."""
    return (d.year << 16) | (d.month << 12) | (d.day << 7)


def from_things_date(v: int) -> date:
    return date(v >> 16, (v >> 12) & 0xF, (v >> 7) & 0x1F)


KIND_WORDS = [("bill", "bill"), ("pay", "bill"), ("betal", "bill"), ("school", "school"), ("kids", "school"),
              ("aula", "school"), ("family", "school"), ("doctor", "school"), ("lægen", "school"),
              ("york", "msc"), ("msc", "msc"), ("job", "job"), ("danish", "danish"),
              ("dansk", "danish"), ("friend", "friend"), ("travel", "travel"), ("trip", "travel"),
              ("admin", "admin")]


# One Things tag per Pocket Pal pillar. A tag decides the pillar; words in the title are only a guess.
PILLAR_TAGS = {"danish": "Danish", "msc": "MSc", "job": "Job hunt", "school": "Family", "admin": "Home admin",
               "friend": "Friends", "body": "Body"}
TAG_KINDS = {**{tag.lower(): kind for kind, tag in PILLAR_TAGS.items()},
             "kids": "school", "dansk": "danish", "york": "msc", "job": "job", "job search": "job", "school": "school",
             "admin": "admin",
             "payment": "admin", "budget": "admin",
             "bills": "bill", "bill": "bill", "friend": "friend", "health": "body", "fitness": "body"}


def tag_kind(tags: str | None) -> str | None:
    """The pillar a to-do's Things tags put it in ("Job hunt|Errand" -> job), or None."""
    for tag in (tags or "").split("|"):
        if tag.strip().lower() in TAG_KINDS:
            return TAG_KINDS[tag.strip().lower()]
    return None


def kind_of(*texts: str, tags: str | None = None) -> str:
    if kind := tag_kind(tags):
        return kind
    hay = " ".join(t for t in texts if t).lower()
    for word, kind in KIND_WORDS:
        if word in hay:
            return kind
    return "other"


def tags_col(con) -> str:
    """Each to-do's tags and its project's, joined with |. Empty on a database without tags."""
    has = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'TMTaskTag'").fetchone()
    if not has:
        return "'' AS tags"
    return """(SELECT group_concat(g.title, '|') FROM TMTaskTag tt JOIN TMTag g ON g.uuid = tt.tags
              WHERE tt.tasks = t.uuid OR tt.tasks = t.project) AS tags"""


TODAY_MAX = 40  # enough for any real Today list, and keeps the phone feed small

# Small, never-urgent jobs the phone offers as side quests: anything in these projects, or with these tags,
# that sits in Anytime or Someday without a date.
SMALL_JOB_PROJECTS = ("Around the house",)
ERRAND_TAGS = {"errands", "errand"}
QUICK_TAGS = {"5 min", "5min", "quick"}


NOTES_MAX = 2000  # a to-do's notes as the phone shows them


def notes_col(con) -> str:
    cols = {r[1] for r in con.execute("PRAGMA table_info(TMTask)")}
    return "COALESCE(t.notes, '')" if "notes" in cols else "''"


def things_text(notes: str) -> str:
    """Things keeps newer notes as a little XML document; the phone wants the plain text."""
    if notes.lstrip().startswith("<note"):
        notes = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"</p>|<br\s*/?>", "\n", notes)))
    return notes.strip()


def things_status(db_path: str, now: datetime) -> dict:
    today = now.date()
    t_today = things_date(today)
    midnight = datetime.combine(today, time.min, tzinfo=now.tzinfo).timestamp()
    con = ro_connect(db_path)
    try:
        open_today = con.execute(
            f"""SELECT t.uuid, t.title, t.deadline, {notes_col(con)} AS notes,
                      COALESCE(a.title, pa.title, '') AS area,
                      COALESCE(p.title, '') AS project, {tags_col(con)}
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
            f"""SELECT t.title, t.deadline, COALESCE(a.title, pa.title, '') AS area, {tags_col(con)}
               FROM TMTask t
               LEFT JOIN TMArea a ON a.uuid = t.area
               LEFT JOIN TMTask p ON p.uuid = t.project
               LEFT JOIN TMArea pa ON pa.uuid = p.area
               WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0
                 AND t.deadline IS NOT NULL AND t.deadline <= ?
               ORDER BY t.deadline""", (week,)).fetchall()
        not_today = "NOT (t.start = 1 AND t.startDate IS NOT NULL AND t.startDate <= ?)"
        week_rows = con.execute(
            f"""SELECT t.uuid, t.title, t.deadline, COALESCE(a.title, pa.title, '') AS area,
                       COALESCE(p.title, '') AS project, {tags_col(con)}
               FROM TMTask t
               LEFT JOIN TMArea a ON a.uuid = t.area
               LEFT JOIN TMTask p ON p.uuid = t.project
               LEFT JOIN TMArea pa ON pa.uuid = p.area
               WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0 AND {not_today}
                 AND t.deadline IS NOT NULL AND t.deadline <= ?
               ORDER BY t.deadline""", (t_today, week)).fetchall()
        pool_rows = con.execute(
            f"""SELECT t.uuid, t.title, t.start, COALESCE(a.title, pa.title, '') AS area,
                       COALESCE(p.title, '') AS project, {tags_col(con)}
               FROM TMTask t
               LEFT JOIN TMArea a ON a.uuid = t.area
               LEFT JOIN TMTask p ON p.uuid = t.project
               LEFT JOIN TMArea pa ON pa.uuid = p.area
               WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0 AND t.start IN (1, 2) AND t.startDate IS NULL
               ORDER BY t.start, t.title""").fetchall()
        checks = con.execute(
            """SELECT title, stopDate FROM TMTask WHERE type = 0 AND status = 3 AND trashed = 0
               AND stopDate >= ? AND title LIKE 'Check%'""", (midnight - 3 * 86400,)).fetchall()
    finally:
        con.close()
    post_done: dict = {}
    for c in checks:  # "Check Aula (3 new)" ticked in Things: everything that came before it is checked
        m = POST_TITLE.match(c["title"] or "")
        if m:
            at = datetime.fromtimestamp(c["stopDate"], now.tzinfo).isoformat(timespec="seconds")
            src = m.group(1).replace("+", " ")
            post_done[src] = max(post_done.get(src, at), at)
    pool = []
    for r in pool_rows:
        tags = {t.strip().rstrip(".").lower() for t in (r["tags"] or "").split("|")}  # "5 min." counts too
        errand = bool(tags & ERRAND_TAGS)
        quick = bool(tags & QUICK_TAGS)
        if not (errand or quick or r["project"].lower() in {p.lower() for p in SMALL_JOB_PROJECTS}):
            continue
        pool.append({"id": r["uuid"], "title": r["title"], "project": r["project"] or None,
                     "kind": kind_of(r["title"], r["area"], r["project"], tags=r["tags"]),
                     "errand": errand, "quick": quick, "someday": r["start"] == 2})
    bill = None
    for b in bills:
        if kind_of(b["title"], b["area"], tags=b["tags"]) == "bill":
            d = from_things_date(b["deadline"])
            bill = d.strftime("%a") if 0 <= (d - today).days < 7 else d.strftime("%-d %b")
            break
    first = open_today[0] if open_today else None
    today_list = [{"id": t["uuid"], "title": t["title"], "kind": kind_of(t["title"], t["area"], t["project"], tags=t["tags"]),
                   "due": from_things_date(t["deadline"]).isoformat() if t["deadline"] else None,
                   "notes": things_text(t["notes"])[:NOTES_MAX] or None}
                  for t in open_today[:TODAY_MAX]]
    return {
        "at": now.isoformat(timespec="seconds"),
        "left": len(open_today),
        "done_today": done_today,
        "completed_ids": completed_ids,
        "overdue": overdue,
        "next": first["title"] if first else None,
        "next_id": first["uuid"] if first else None,
        "next_kind": kind_of(first["title"], first["area"], first["project"], tags=first["tags"]) if first else None,
        "bill": bill,
        "today": today_list,
        "week": [{"id": r["uuid"], "title": r["title"], "due": from_things_date(r["deadline"]).isoformat(),
                  "kind": kind_of(r["title"], r["area"], r["project"], tags=r["tags"])} for r in week_rows[:10]],
        "pool": pool[:30],
        "post_done": post_done,
    }


# --- Pillars: how each area of life is going, for the phone ---------------

PILLAR_DAYS = 60  # how far back last_done looks


def things_pillars(db_path: str, now: datetime) -> dict:
    """Per kind: done since Monday, open (not Someday), last done date and next deadline."""
    today = now.date()
    monday = datetime.combine(today - timedelta(days=today.weekday()), time.min, tzinfo=now.tzinfo).timestamp()
    since = (now - timedelta(days=PILLAR_DAYS)).timestamp()
    join = """FROM TMTask t
              LEFT JOIN TMArea a ON a.uuid = t.area
              LEFT JOIN TMTask p ON p.uuid = t.project
              LEFT JOIN TMArea pa ON pa.uuid = p.area"""
    con = ro_connect(db_path)
    cols = f"t.title, COALESCE(a.title, pa.title, '') AS area, COALESCE(p.title, '') AS project, {tags_col(con)}"
    try:
        open_rows = con.execute(f"SELECT {cols}, t.deadline {join} "
                                "WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0 AND t.start != 2").fetchall()
        done_rows = con.execute(f"SELECT {cols}, t.stopDate {join} "
                                "WHERE t.type = 0 AND t.status = 3 AND t.trashed = 0 AND t.stopDate >= ?",
                                (since,)).fetchall()
    finally:
        con.close()
    out: dict = {}

    def entry(r) -> dict:
        k = kind_of(r["title"], r["area"], r["project"], tags=r["tags"])
        return out.setdefault(k, {"done_week": 0, "open": 0, "last_done": None, "next_due": None})

    for r in open_rows:
        e = entry(r)
        e["open"] += 1
        if r["deadline"]:
            due = from_things_date(r["deadline"]).isoformat()
            e["next_due"] = min(e["next_due"] or due, due)
    for r in done_rows:
        e = entry(r)
        e["done_week"] += r["stopDate"] >= monday
        day = datetime.fromtimestamp(r["stopDate"], now.tzinfo).date().isoformat()
        e["last_done"] = max(e["last_done"] or day, day)
    return out


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


# --- Job tracker -----------------------------------------------------------

def job_tracker(db_path: str, now: datetime, days: int = 14) -> tuple[list[dict], dict]:
    """Upcoming interviews as calendar-style events, and job hunt pillar extras: the next application
    deadline, applications sent this week and good open leads. Dates in the tracker are naive local time.
    Read-only: the tracker owns its database."""
    today = now.date()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    con = ro_connect(db_path)
    try:
        interviews = con.execute(
            """SELECT company, interview_at FROM applications
               WHERE interview_at >= ? AND interview_at < ? AND status NOT IN ('rejected', 'withdrawn')
               ORDER BY interview_at""",
            (now.strftime("%Y-%m-%d %H:%M"), (today + timedelta(days=days)).isoformat())).fetchall()
        app_due = con.execute("SELECT MIN(deadline) FROM applications WHERE status = 'saved' AND deadline >= ?",
                              (today.isoformat(),)).fetchone()[0]
        marks = ",".join("?" * len(JOB_LEADS))
        leads, lead_due = con.execute(
            f"""SELECT COUNT(*), MIN(CASE WHEN deadline >= ? THEN deadline END) FROM jobs
                WHERE COALESCE(dismissed, 0) = 0 AND COALESCE(likely_closed, 0) = 0 AND fit_tier IN ({marks})""",
            (today.isoformat(), *JOB_LEADS)).fetchone()
        applied = con.execute("SELECT COUNT(*) FROM applications WHERE date_applied >= ?", (monday,)).fetchone()[0]
        cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        link = next((c for c in ("url", "link", "job_url", "apply_url") if c in cols), None)
        lead_rows = con.execute(
            f"""SELECT title, company, deadline, {link or 'NULL'} AS url FROM jobs
                WHERE COALESCE(dismissed, 0) = 0 AND COALESCE(likely_closed, 0) = 0 AND fit_tier IN ({marks})
                  AND (deadline IS NULL OR deadline >= ?)
                ORDER BY deadline IS NULL, deadline, fit_tier LIMIT ?""",
            (*JOB_LEADS, today.isoformat(), LEADS_SHOWN)).fetchall()
    finally:
        con.close()
    events = []
    for r in interviews:
        try:
            start = datetime.strptime(r["interview_at"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
        except (TypeError, ValueError):
            continue
        events.append({"title": f"Interview: {r['company']}", "start": start.isoformat(timespec="minutes")})
    dues = [d for d in (app_due, lead_due) if d]
    extra = {"applied_week": applied, "leads": leads, "job_due": min(dues) if dues else None,
             "interview_at": events[0]["start"] if events else None,
             "lead_list": [{k: r[k] for k in ("title", "company", "deadline", "url") if r[k]} for r in lead_rows]}
    return events, extra


def job_deadlines(cfg: dict, now: datetime) -> list[studycal.Event]:
    """Strong-fit leads and saved applications with a deadline, as deadline events for studycal: an
    "Apply: …" to-do lands in Things Today a week before (with the Things deadline set), plus a nudge
    two days before. Empty if the tracker is missing or unreadable."""
    path = cfg.get("job_tracker_db", JOB_TRACKER_DB)
    if not path or not os.path.exists(path):
        return []
    tiers = tuple(cfg.get("job_apply_tiers") or JOB_APPLY)
    today = now.date().isoformat()
    try:
        con = ro_connect(path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
            link = next((c for c in ("url", "link", "job_url", "apply_url") if c in cols), None)
            marks = ",".join("?" * len(tiers))
            leads = con.execute(
                f"""SELECT 'job:' || rowid AS uid, title, company, deadline, {link or 'NULL'} AS url FROM jobs
                    WHERE COALESCE(dismissed, 0) = 0 AND COALESCE(likely_closed, 0) = 0
                      AND fit_tier IN ({marks}) AND deadline >= ?""", (*tiers, today)).fetchall()
            saved = con.execute(
                """SELECT 'app:' || rowid AS uid, job_title AS title, company, deadline, NULL AS url FROM applications
                   WHERE status = 'saved' AND deadline >= ?""", (today,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:  # the tracker's schema is its own; never let it stop a sync
        return []
    out = []
    for r in list(leads) + list(saved):
        try:
            due = date.fromisoformat((r["deadline"] or "")[:10])
        except ValueError:
            continue
        what = " at ".join(p for p in (r["title"], r["company"]) if p) or "job"
        out.append(studycal.Event(uid=r["uid"], title=f"Apply: {what}", start=due, calendar="job tracker",
                                  url=r["url"] or "", tags=["Job hunt"], extra={"deadline": True}))
    return out


def read_job_tracker(cfg: dict, status: dict, now: datetime) -> str:
    """Adds the tracker's interviews to status["events"] and its deadlines to the job pillar."""
    path = cfg.get("job_tracker_db", JOB_TRACKER_DB)
    if not path or not os.path.exists(path):
        return "Job tracker not found."
    try:
        events, extra = job_tracker(path, now)
    except sqlite3.Error as e:  # the tracker's schema is its own; never let it stop a sync
        return f"Could not read the job tracker: {e}"
    status["events"] = sorted(status.get("events", []) + events, key=lambda e: e["start"])
    job = status.setdefault("pillars", {}).setdefault(
        "job", {"done_week": 0, "open": 0, "last_done": None, "next_due": None})
    job.update(extra)
    if extra["job_due"]:
        job["next_due"] = min(job["next_due"] or extra["job_due"], extra["job_due"])
    return f"{len(events)} interviews, {extra['leads']} good leads, next deadline {extra['job_due'] or 'none'}"


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


def things_notes(it: dict) -> str:
    """What the to-do says in Things: the links and files first, so they're a tap away, then the email."""
    lines = [*it.get("links", []), *it.get("saved", [])]
    if it.get("mail_link"):
        lines.append(f"Open the email: {it['mail_link']}")
    if lines:
        lines.append("")
    lines += [it["title"], f"From {it.get('sender', '')}, {it['received'][:10]}"]
    if it.get("excerpt"):
        lines += ["", it["excerpt"]]
    lines += ["", f"Area: {it.get('area', '')}", "(added by One Inbox)"]
    return "\n".join(lines)[:4000]


def things_add_url(items: list[dict]) -> str:
    todos = []
    for it in items:
        attrs = {"title": it["task"], "notes": things_notes(it)}
        if it.get("when"):  # a capture said where: "errand: …" goes to Anytime with the Errands tag
            attrs["when"] = it["when"]
        elif it.get("auto"):  # homework and quick captures go to Today, so the watch shows them
            attrs["when"] = "today"
        for key in ("tags", "list", "deadline"):
            if it.get(key):
                attrs[key] = it[key]
        todos.append({"type": "to-do", "attributes": attrs})
    return "things:///json?data=" + urllib.parse.quote(json.dumps(todos))


def things_query(params: dict) -> str:
    """Things reads "+" literally, so spaces must go as %20: "Check Aula (2 new)", not "Check+Aula+(2+new)"."""
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def things_done_url(uuid: str, auth_token: str) -> str:
    """Things needs its own auth token (Settings > General > Enable Things URLs) to change a to-do."""
    return "things:///update?" + things_query({"id": uuid, "completed": "true", "auth-token": auth_token})


def complete_in_things(cfg: dict, dry_run: bool = False) -> int | str:
    """Tick off in Things what Ben marked done on the watch or phone."""
    try:
        ids = call(cfg["server"], cfg["token"], "GET", "/api/todo/outbox").get("ids", [])
    except urllib.error.HTTPError as e:  # a Pi that hasn't been updated yet
        return f"Pi has no /api/todo/outbox yet ({e.code}). Update it with install-pi.sh."
    if not ids:
        return 0
    token = cfg.get("things_auth_token", "")
    if not token:
        return f"{len(ids)} waiting: add things_auth_token to agent.json (see docs/setup.md)"
    for uuid in ids:
        subprocess.run(["open", "-g", things_done_url(uuid, token)], check=True)
    call(cfg["server"], cfg["token"], "POST", "/api/todo/ack", {"ids": ids})
    return len(ids)


TAGGED = Path.home() / ".config" / "onebox" / "tagged.json"


def untagged_guesses(db_path: str) -> list[tuple[str, str]]:
    """Open to-dos with no pillar tag whose title, area or project points at a pillar: (uuid, tag)."""
    con = ro_connect(db_path)
    try:
        rows = con.execute(
            f"""SELECT t.uuid, t.title, COALESCE(a.title, pa.title, '') AS area, COALESCE(p.title, '') AS project,
                       {tags_col(con)}
                FROM TMTask t
                LEFT JOIN TMArea a ON a.uuid = t.area
                LEFT JOIN TMTask p ON p.uuid = t.project
                LEFT JOIN TMArea pa ON pa.uuid = p.area
                WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0""").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        if tag_kind(r["tags"]):
            continue
        kind = kind_of(r["title"], r["area"], r["project"])
        tag = PILLAR_TAGS.get("admin" if kind == "bill" else kind)
        if tag:
            out.append((r["uuid"], tag))
    return out


def things_tag_url(uuid: str, tag: str, auth_token: str) -> str:
    return "things:///update?" + things_query({"id": uuid, "add-tags": tag, "auth-token": auth_token})


def tag_in_things(cfg: dict, db_path: str, limit: int = 20) -> int | str:
    """Writes the pillar the agent guessed back to Things as a tag, so Things and the app agree and a wrong
    guess is fixed by changing the tag. Each to-do is tagged once: one Ben untags stays untagged.
    Only with "auto_tag": true in agent.json."""
    if not cfg.get("auto_tag"):
        return 0
    token = cfg.get("things_auth_token", "")
    if not token:
        return "add things_auth_token to agent.json to tag to-dos"
    path = Path(cfg.get("tagged", TAGGED))
    done = json.loads(path.read_text()) if path.exists() else {}
    todo = [(u, t) for u, t in untagged_guesses(db_path) if u not in done][:limit]
    for uuid, tag in todo:
        subprocess.run(["open", "-g", things_tag_url(uuid, tag, token)], check=True)
        done[uuid] = tag
    if todo:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(done, indent=1))
    return len(todo)


# --- Official post: one rolling "Check Aula (3 new)" to-do per source ------

POST_TITLE = re.compile(r"^Check[ +](Aula|e-Boks|Digital[ +]Post|Job[ +]alerts)[ +]\(\d+[ +]new\)")  # "+": older broken updates
POST_TAGS = {"Aula": ["Family"], "e-Boks": ["Home admin"], "Digital Post": ["Home admin"], JOB_ALERTS: ["Job hunt"]}
OLD_POST_TODO = re.compile(r"^Read in (Aula|e-Boks|Digital Post): ")


def post_notes(src: dict) -> str:
    how = ("Look through them in Mail (Mail may have put them in Junk) and save the good ones to the job tracker"
           if src["source"] == JOB_ALERTS else f"Open the {src['source']} app to read them")
    lines = [f"{how}, then tick this off.", ""]
    for it in reversed(src.get("items", [])):
        try:
            when = datetime.fromisoformat(it["received"]).astimezone().strftime("%a %-d %b %H:%M")
        except (KeyError, ValueError):
            when = ""
        lines.append(f"• {when}  {it.get('title') or ''}".rstrip())
    if src["count"] > len(src.get("items", [])):
        lines.append(f"…and {src['count'] - len(src['items'])} older")
    lines += ["", "(added by One Inbox)"]
    return "\n".join(lines)


def open_post_todos(db_path: str) -> dict:
    """{source: [(uuid, title), ...]} for the open "Check …" to-dos One Inbox keeps in Things. There should be
    one per source; more are left over from a bug and get cancelled."""
    con = ro_connect(db_path)
    try:
        rows = con.execute("""SELECT uuid, title FROM TMTask WHERE type = 0 AND status = 0 AND trashed = 0
                              AND title LIKE 'Check%'""").fetchall()
    finally:
        con.close()
    out: dict = {}
    for r in rows:
        m = POST_TITLE.match(r["title"] or "")
        if m:
            out.setdefault(m.group(1).replace("+", " "), []).append((r["uuid"], r["title"]))
    return out


def things_update_url(uuid: str, auth_token: str, **attrs) -> str:
    return "things:///update?" + things_query({"id": uuid, **attrs, "auth-token": auth_token})


def post_plan(sources: list[dict], open_todos: dict) -> list[tuple[str, dict]]:
    """What to do in Things for unread official post: ("add", attrs) for a new Check to-do in Today, or
    ("update", {"id", "title", "notes"}) when the count changed, and ("cancel", {"id"}) for any extra Check
    to-do of the same source. One to-do per source, never more."""
    plan = []
    for todos in open_todos.values():
        plan += [("cancel", {"id": uuid}) for uuid, _ in todos[1:]]
    for src in sources:
        if not src.get("count"):
            continue
        title = f"Check {src['source']} ({src['count']} new)"
        found = open_todos.get(src["source"]) or []
        current = found[0] if found else None
        if current is None:
            plan.append(("add", {"title": title, "notes": post_notes(src), "when": "today",
                                 "tags": POST_TAGS.get(src["source"], [])}))
        elif current[1] != title:
            plan.append(("update", {"id": current[0], "title": title, "notes": post_notes(src)}))
    return plan


def sync_post(cfg: dict, db_path: str) -> int | str:
    """Keeps one "Check Aula (3 new)" to-do in Things per source with unread alerts."""
    try:
        sources = call(cfg["server"], cfg["token"], "GET", "/api/post").get("sources", [])
    except urllib.error.HTTPError as e:  # a Pi that hasn't been updated yet
        return f"Pi has no /api/post yet ({e.code}). Update it with install-pi.sh."
    plan = post_plan(sources, open_post_todos(db_path))
    token = cfg.get("things_auth_token", "")
    adds = [{"type": "to-do", "attributes": a} for op, a in plan if op == "add"]
    if adds:
        subprocess.run(["open", "-g", "things:///json?data=" + urllib.parse.quote(json.dumps(adds))], check=True)
    for op, a in plan:
        if op == "update" and token:
            subprocess.run(["open", "-g", things_update_url(a["id"], token, title=a["title"], notes=a["notes"])],
                           check=True)
        elif op == "cancel" and token:
            subprocess.run(["open", "-g", things_update_url(a["id"], token, canceled="true")], check=True)
    if any(op != "add" for op, _ in plan) and not token:
        return f"{len(adds)} added; add things_auth_token to agent.json to update the counts"
    return len(plan)


def tidy_post(cfg: dict, db_path: str) -> int | str:
    """Cancels the old one-per-email "Read in Aula: …" to-dos that One Inbox added before the rolling
    Check to-do existed. Only with "post_tidy": true in agent.json; they stay in the Things Logbook."""
    if not cfg.get("post_tidy"):
        return 0
    token = cfg.get("things_auth_token", "")
    if not token:
        return "add things_auth_token to agent.json to tidy old post to-dos"
    con = ro_connect(db_path)
    try:
        rows = con.execute(f"""SELECT t.uuid, t.title, {notes_col(con)} AS notes FROM TMTask t
                               WHERE t.type = 0 AND t.status = 0 AND t.trashed = 0 AND t.title LIKE 'Read in %'""").fetchall()
    finally:
        con.close()
    old = [r["uuid"] for r in rows if OLD_POST_TODO.match(r["title"] or "") and "added by One Inbox" in r["notes"]]
    for uuid in old:
        subprocess.run(["open", "-g", things_update_url(uuid, token, canceled="true")], check=True)
    return len(old)


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg.setdefault("server", os.environ.get("ONEBOX_SERVER", "http://inbox.home:8787"))
    cfg.setdefault("token", os.environ.get("ONEBOX_AGENT_TOKEN", ""))
    cfg.setdefault("days", 21)
    return cfg


def wake_mail(cfg: dict) -> bool:
    """Mail only fetches new messages (and so updates the index we read) while it is open.
    Start it hidden in the background if it isn't running. Returns True if it had to be started."""
    if not cfg.get("keep_mail_open", True) or sys.platform != "darwin":
        return False
    if subprocess.run(["pgrep", "-xq", "Mail"]).returncode == 0:
        return False
    subprocess.run(["open", "-g", "-j", "-a", "Mail"])
    return True


def run_once(cfg: dict, now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or datetime.now().astimezone()
    report: dict = {}
    if not dry_run and wake_mail(cfg):
        report["mail_started"] = "Mail wasn't open, so new mail wasn't arriving. Started it in the background."
    mail_db = cfg.get("mail_db") or newest(MAIL_GLOB)
    items = []
    if mail_db:
        msgs = read_mail(mail_db, cfg["days"], cfg.get("account_labels"), now)
        triaged = triage(msgs, now)
        items = [i.to_dict() for i in triaged]
        report["mail"] = summary(triaged)
        try:
            report["mail_details"] = add_details(items, mail_db, now, None if dry_run else ICLOUD)
        except OSError as e:  # never let a body we can't read stop the sync
            report["mail_details"] = f"Couldn't read mail bodies: {e}"
    else:
        report["mail"] = "Mail index not found. Is Full Disk Access on?"
    payload: dict = {"items": items}
    things_db = cfg.get("things_db") or newest(THINGS_GLOB)
    if things_db:
        status = things_status(things_db, now)
        status["pillars"] = things_pillars(things_db, now)
        status["friends_overdue"] = friends_overdue(cfg.get("messages_db", MESSAGES_DB), cfg.get("friends", []), now)
        status["events"], report["calendar"] = read_calendar(cfg, now)
        report["job_tracker"] = read_job_tracker(cfg, status, now)
        payload["things"] = status
        report["things"] = {k: v for k, v in status.items() if k not in ("completed_ids", "pool", "week")}
        report["small_jobs"], report["this_week"] = len(status["pool"]), len(status["week"])
    if dry_run:
        report["would_send"] = len(items)
        if cfg.get("calendars") or cfg.get("deadlines") or job_deadlines(cfg, now):
            report["calendar"] = calendar_preview(cfg, now)
        return report
    call(cfg["server"], cfg["token"], "POST", "/api/ingest", payload)
    outbox = call(cfg["server"], cfg["token"], "GET", "/api/outbox").get("items", [])
    if outbox:
        subprocess.run(["open", "-g", things_add_url(outbox)], check=True)
        call(cfg["server"], cfg["token"], "POST", "/api/outbox/ack", {"ids": [i["id"] for i in outbox]})
    report["added_to_things"] = len(outbox)
    report["completed_in_things"] = complete_in_things(cfg)
    report["calendar"] = sync_calendars(cfg, now)
    if things_db:
        report["post_in_things"] = sync_post(cfg, things_db)
        report["post_tidied"] = tidy_post(cfg, things_db)
        report["tagged_in_things"] = tag_in_things(cfg, things_db)
    return report


def calendar_preview(cfg: dict, now: datetime) -> dict:
    events, errors = studycal.collect(cfg, now)
    events += job_deadlines(cfg, now)
    seen = studycal.load_seen(Path(cfg.get("calendar_seen", CALENDAR_SEEN)))
    new = studycal.due_now(events, seen, now)
    return {"events": len(events), "would_add": [t["attributes"]["title"] for t in studycal.things_json(new, now.date())],
            "errors": errors}


def sync_calendars(cfg: dict, now: datetime) -> dict:
    jobs = job_deadlines(cfg, now)
    if not cfg.get("calendars") and not cfg.get("deadlines") and not jobs:
        return {}
    seen_path = Path(cfg.get("calendar_seen", CALENDAR_SEEN))
    events, errors = studycal.collect(cfg, now)
    events += jobs
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
