"""To-dos that used to go to Things 3 now go to Pocket Pal's own list on the Pi.

Ben moved his to-do list into Pocket Pal (2026-11-06) and wants nothing to write to Things any more. Everything
that made a Things to-do (an email added from triage, a Siri capture, a calendar or job deadline) makes a to-do
in the Pi's `tasks` table instead. These helpers turn the old Things JSON shape into the table's fields.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

KINDS = {"bill", "school", "admin", "msc", "job", "danish", "friend", "body", "travel", "other"}
# A Things-style tag names its pillar ("Job hunt" -> job), as the Mac agent reads Things' tags.
TAG_KINDS = {"danish": "danish", "dansk": "danish", "msc": "msc", "york": "msc", "job hunt": "job", "job": "job",
             "job search": "job", "family": "school", "kids": "school", "school": "school", "home admin": "admin",
             "admin": "admin", "payment": "admin", "budget": "admin", "bills": "bill", "bill": "bill",
             "friends": "friend", "friend": "friend", "body": "body", "health": "body", "fitness": "body"}
DAY = re.compile(r"(\d{4}-\d{2}-\d{2})(?:@\d{1,2}:\d{2})?")
TITLE_MAX = 500


def kind_from(kind: str | None, tags: list[str] | None) -> str:
    if kind in KINDS:
        return kind
    for t in tags or []:
        k = TAG_KINDS.get(str(t).strip().lower())
        if k:
            return k
    return "other"


def task_from_todo(attrs: dict, today: date) -> dict | None:
    """Things JSON attributes ({"title", "notes", "when", "tags", "deadline", "list"}, plus an optional "kind")
    as fields for Store.add_task, or None when there's no title. "when" is "today", "evening", "tomorrow",
    "anytime", "someday" or a day ("2026-11-09", or "2026-11-09@08:30" with a reminder time, which is dropped)."""
    title = " ".join(str(attrs.get("title") or "").split())[:TITLE_MAX]
    if not title:
        return None
    tags = [str(t).strip()[:40] for t in (attrs.get("tags") or []) if str(t).strip()][:10]
    when = str(attrs.get("when") or "").strip().lower()
    day, lst = None, "anytime"
    if when in ("today", "evening"):
        day, lst = today.isoformat(), "today"
    elif when == "tomorrow":
        day, lst = (today + timedelta(days=1)).isoformat(), "upcoming"
    elif when == "someday":
        lst = "someday"
    elif m := DAY.fullmatch(when):
        day = m.group(1)
        lst = "today" if day <= today.isoformat() else "upcoming"
        day = max(day, today.isoformat())
    deadline = str(attrs.get("deadline") or "")
    notes = str(attrs.get("notes") or "").strip()[:4000] or None
    project = str(attrs.get("list") or "").strip()[:100] or None
    return {"title": title, "notes": notes, "kind": kind_from(attrs.get("kind"), tags), "list": lst,
            "when_day": day, "due": deadline if DAY.fullmatch(deadline) else None, "tags": tags, "project": project}


def item_notes(it: dict) -> str:
    """What a to-do made from an email says: the links first, so they're a tap away, then the email."""
    lines = [*it.get("links", []), *it.get("saved", [])]
    if it.get("mail_link"):
        lines.append(f"Open the email: {it['mail_link']}")
    if lines:
        lines.append("")
    if it.get("source") != "capture":
        lines += [it.get("title") or "", f"From {it.get('sender', '')}, {str(it.get('received') or '')[:10]}"]
        if it.get("excerpt"):
            lines += ["", it["excerpt"]]
    return "\n".join(lines).strip()[:4000]


def todo_from_item(it: dict) -> dict:
    """A triage item (an email Ben added, or a capture) in the Things JSON shape task_from_todo reads."""
    attrs = {"title": it.get("task") or it.get("title"), "notes": item_notes(it), "kind": it.get("kind"),
             "when": it.get("when") or ("today" if it.get("auto") else "anytime")}
    for key in ("tags", "list", "deadline"):
        if it.get(key):
            attrs[key] = it[key]
    return attrs
