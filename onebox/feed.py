"""The small JSON feed the Garmin buddy watch face reads.

Contract: docs/watch-feed.md. Keep it additive: never rename or remove a field.
"""

from __future__ import annotations

from datetime import datetime, timedelta

FEED_VERSION = 1
NEXT_MAX = 20
KINDS = {"bill", "school", "admin", "msc", "job", "danish", "friend", "travel", "other"}


def short(text: str | None, limit: int = NEXT_MAX) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def mood(left: int, overdue: int, friends_overdue: int, now: datetime) -> str:
    """First match wins: hungry, lonely, sleepy, happy, normal."""
    if overdue > 0:
        return "hungry"
    if friends_overdue > 0:
        return "lonely"
    if now.hour >= 22 and left > 0:
        return "sleepy"
    if left == 0:
        return "happy"
    return "normal"


def next_event(events: list[dict], now: datetime) -> tuple[str | None, str | None]:
    """The first event that hasn't started yet, within 24 h: ("Lecture", "14:00") or ("Dentist", "Tue 09:00")."""
    now = now if now.tzinfo else now.astimezone()
    for e in events or []:
        try:
            start = datetime.fromisoformat(e["start"]).astimezone(now.tzinfo)
        except (KeyError, TypeError, ValueError):
            continue
        if now <= start < now + timedelta(hours=24):
            at = start.strftime("%H:%M") if start.date() == now.date() else start.strftime("%a %H:%M")
            return short(e.get("title")), at
    return None, None


def build(status: dict, xp: int, now: datetime) -> dict:
    """status comes from the Mac agent's last report (see agent.things_status)."""
    left = int(status.get("left", 0))
    kind = status.get("next_kind")
    event, event_at = next_event(status.get("events", []), now)
    return {
        "v": FEED_VERSION,
        "at": status.get("at") or now.isoformat(timespec="seconds"),
        "left": left,
        "done": int(status.get("done_today", 0)),
        "xp": int(xp),
        "next": short(status.get("next")),
        "kind": kind if kind in KINDS else (None if not status.get("next") else "other"),
        "bill": status.get("bill"),
        "mood": mood(left, int(status.get("overdue", 0)), int(status.get("friends_overdue", 0)), now),
        "event": event,
        "event_at": event_at,
    }
