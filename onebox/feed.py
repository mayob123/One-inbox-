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
    has caught up. Works on a copy; an old agent without ids or a today list is left as is.
    Ticks in the small-jobs pool or this week's deadlines drop out of those lists too."""
    rows = status.get("today") or []
    lists = {k: status[k] for k in ("pool", "week") if status.get(k)}
    if not actions or not (rows or lists):
        return status
    done, skip = set(actions.get("done", [])), list(actions.get("skip", []))
    extra = {k: [r for r in v if r.get("id") not in done] for k, v in lists.items()}
    # an item can be in Today and in this week's list; count each tick once
    ticked = {r.get("id") for v in [rows, *lists.values()] for r in v if r.get("id") in done}
    out = {**status, **extra, "done_today": int(status.get("done_today", 0)) + len(ticked)}
    if not rows:
        return out
    kept = [r for r in rows if r.get("id") not in done]
    order = {u: i for i, u in enumerate(skip)}
    kept.sort(key=lambda r: (r.get("id") in order, order.get(r.get("id"), 0)))  # stable: skips last, oldest skip first
    first = kept[0] if kept else None
    return {**out, "today": kept,
            "left": max(0, int(status.get("left", 0)) - (len(rows) - len(kept))),
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


POST_DAYS = 14  # unread alerts older than this stop counting
POST_LIST_MAX = 10


def post_summary(items: list[dict], now: datetime) -> list[dict]:
    """Unread official post (Aula, e-Boks, Digital Post) per source, oldest alert first:
    [{"source": "Aula", "count": 3, "since": iso, "latest": "Ny besked fra …", "items": [{"id", "title", "received"}]}].
    `items` are the Pi's triage items; an alert is unread until Ben marks it checked."""
    groups: dict = {}
    for it in items:
        src = it.get("post")
        if not src or it.get("status") != "decide" or it.get("decision"):
            continue
        try:
            received = datetime.fromisoformat(it["received"])
        except (KeyError, ValueError):
            continue
        if received.tzinfo and now.tzinfo and now - received > timedelta(days=POST_DAYS):
            continue
        groups.setdefault(src, []).append(it)
    out = []
    for src, rows in sorted(groups.items()):
        rows.sort(key=lambda r: r["received"])
        out.append({"source": src, "count": len(rows), "since": rows[0]["received"],
                    "latest": short(rows[-1].get("title"), TITLE_MAX),
                    "items": [{"id": r["id"], "title": short(r.get("title"), TITLE_MAX), "received": r["received"]}
                              for r in rows[-POST_LIST_MAX:]]})
    return out


POOL_MAX = 30
WEEK_MAX = 10


def _rows(rows: list[dict] | None, keys: tuple, limit: int) -> list[dict]:
    out = []
    for r in rows or []:
        title = short(r.get("title"), TITLE_MAX)
        if not title:
            continue
        e = {k: r.get(k) for k in keys if r.get(k) is not None}
        e["title"] = title
        e["kind"] = r.get("kind") if r.get("kind") in KINDS else "other"
        out.append(e)
    return out[:limit]


def today(status: dict, xp: int, now: datetime, actions: dict | None = None,
          waiting_mail: dict | None = None, pal: dict | None = None, post: list[dict] | None = None) -> dict:
    """The watch feed plus every open item in Things Today, for the Pocket Pal phone app. Also the small-jobs
    pool (Around the house, Errands, 5 min), this week's deadlines outside Today and unread official post."""
    done = set((actions or {}).get("done", []))
    seen: set = set()
    ticked = []
    for r in [*(status.get("today") or []), *(status.get("pool") or []), *(status.get("week") or [])]:
        if r.get("id") in done and r.get("id") not in seen:
            seen.add(r.get("id"))
            ticked.append(r)
    life = pillars(status, waiting_mail or {}, ticked, now)
    status = apply_actions(status, actions)
    items = []
    for it in status.get("today") or []:
        title = short(it.get("title"), TITLE_MAX)
        if not title:
            continue
        kind = it.get("kind")
        entry = {"id": it.get("id"), "title": title, "kind": kind if kind in KINDS else "other", "due": it.get("due")}
        if it.get("notes"):
            entry["notes"] = it["notes"]
        items.append(entry)
    return {**build(status, xp, now, pal=pal), "items": items, "pillars": life,
            "week": _rows(status.get("week"), ("id", "due"), WEEK_MAX),
            "pool": _rows(status.get("pool"), ("id", "project", "errand", "quick", "someday"), POOL_MAX),
            "post": post or []}
