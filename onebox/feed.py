"""The small JSON feed the Garmin buddy watch face reads.

Contract: docs/watch-feed.md. Keep it additive: never rename or remove a field.
The phone also gets the whole Today list (today()); the watch feed stays small for the Garmin's memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta

FEED_VERSION = 1
NEXT_MAX = 20
KINDS = {"bill", "school", "admin", "msc", "job", "danish", "friend", "body", "travel", "other"}


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


def next_event(events: list[dict], now: datetime) -> tuple[str | None, str | None, int | None]:
    """The first event that hasn't started yet, within 24 h, with its start as epoch seconds:
    ("Lecture", "14:00", 1793019600) or ("Dentist", "Tue 09:00", ...)."""
    now = now if now.tzinfo else now.astimezone()
    for e in events or []:
        try:
            start = datetime.fromisoformat(e["start"]).astimezone(now.tzinfo)
        except (KeyError, TypeError, ValueError):
            continue
        if now <= start < now + timedelta(hours=24):
            at = start.strftime("%H:%M") if start.date() == now.date() else start.strftime("%a %H:%M")
            return short(e.get("title")), at, int(start.timestamp())
    return None, None, None


def apply_actions(status: dict, actions: dict | None) -> dict:
    """Drop items ticked on the watch or phone and move skipped ones to the end, before the Mac
    has caught up. Works on a copy; an old agent without ids or a today list is left as is."""
    rows = status.get("today")
    if not actions or not rows:
        return status
    done, skip = set(actions.get("done", [])), list(actions.get("skip", []))
    kept = [r for r in rows if r.get("id") not in done]
    order = {u: i for i, u in enumerate(skip)}
    kept.sort(key=lambda r: (r.get("id") in order, order.get(r.get("id"), 0)))  # stable: skips last, oldest skip first
    gone = len(rows) - len(kept)
    first = kept[0] if kept else None
    return {**status, "today": kept,
            "left": max(0, int(status.get("left", 0)) - gone),
            "done_today": int(status.get("done_today", 0)) + gone,
            "next": first["title"] if first else None,
            "next_kind": first.get("kind") if first else None,
            "next_id": first.get("id") if first else None}


PAL_MAX_AGE = 36 * 3600
PAL_LINE_MAX = 24
PAL_LABEL_MAX = 6
PAL_PILLARS_MAX = 7


def pal_compact(pal: dict | None, now: datetime) -> dict:
    """The phone's Lyra state, cut down for the watch. All null when there is none or it is over 36 h old."""
    if not pal or now.timestamp() - float(pal.get("received_at", 0)) > PAL_MAX_AGE:
        return {"pal_line": None, "pal_pose": None, "pal_pillars": None}
    rows = [[(p.get("label") or p.get("key") or "")[:PAL_LABEL_MAX], p.get("done", 0), p.get("target", 0)]
            for p in (pal.get("pillars") or [])[:PAL_PILLARS_MAX]]
    return {"pal_line": short(pal.get("line"), PAL_LINE_MAX), "pal_pose": pal.get("pose"), "pal_pillars": rows}


def clean_pal(body: dict, now: datetime) -> dict:
    """Keep only the fields the phone is meant to send, with sane types and lengths."""
    if not isinstance(body, dict):
        raise ValueError("send a JSON object")
    def text(v, n):
        return str(v)[:n] if isinstance(v, (str, int, float)) and str(v).strip() else None

    def count(v):
        return max(0, min(999, int(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    pillars = []
    for p in body.get("pillars") or []:
        if isinstance(p, dict) and text(p.get("key"), 20):
            pillars.append({"key": text(p.get("key"), 20), "label": text(p.get("label"), 20) or text(p.get("key"), 20),
                            "done": count(p.get("done")), "target": count(p.get("target"))})
    pose = text(body.get("pose"), 16)
    return {
        "at": body.get("at") if isinstance(body.get("at"), (int, float)) else None,
        "received_at": int(now.timestamp()),
        "pillars": pillars[:12],
        "focus": text(body.get("focus"), 20),
        "pose": pose.lower() if pose else None,
        "line": text(body.get("line"), 80),
    }


def build(status: dict, xp: int, now: datetime, actions: dict | None = None, pal: dict | None = None) -> dict:
    """status comes from the Mac agent's last report (see agent.things_status); pal from the phone (POST /api/pal)."""
    status = apply_actions(status, actions)
    left = int(status.get("left", 0))
    kind = status.get("next_kind")
    event, event_at, event_ts = next_event(status.get("events", []), now)
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
        "now_id": status.get("next_id") if status.get("next") else None,
        "event_ts": event_ts,
        **pal_compact(pal, now),
    }


TITLE_MAX = 80


def pillars(status: dict, waiting_mail: dict, ticked: list[dict], now: datetime) -> dict:
    """One entry per kind: Things counts from the Mac, mail still waiting on a decision from the Pi,
    plus anything ticked on the watch or phone that Things hasn't caught up with."""
    sent = status.get("pillars") or {}
    out = {}
    for k in sorted(KINDS):
        e = dict({"done_week": 0, "open": 0, "last_done": None, "next_due": None}, **sent.get(k, {}))
        e["waiting_mail"] = int(waiting_mail.get(k, 0))
        out[k] = e
    for r in ticked:
        e = out[r.get("kind") if r.get("kind") in KINDS else "other"]
        e["done_week"] += 1
        e["open"] = max(0, e["open"] - 1)
        e["last_done"] = now.date().isoformat()
    return out


def today(status: dict, xp: int, now: datetime, actions: dict | None = None,
          waiting_mail: dict | None = None, pal: dict | None = None) -> dict:
    """The watch feed plus every open item in Things Today, for the Pocket Pal phone app."""
    done = set((actions or {}).get("done", []))
    ticked = [r for r in status.get("today") or [] if r.get("id") in done]
    life = pillars(status, waiting_mail or {}, ticked, now)
    status = apply_actions(status, actions)
    items = []
    for it in status.get("today") or []:
        title = short(it.get("title"), TITLE_MAX)
        if not title:
            continue
        kind = it.get("kind")
        items.append({"id": it.get("id"), "title": title, "kind": kind if kind in KINDS else "other",
                      "due": it.get("due")})
    return {**build(status, xp, now, pal=pal), "items": items, "pillars": life}
