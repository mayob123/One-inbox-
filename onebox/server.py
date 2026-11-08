"""The One Inbox server that runs on the Pi. Standard library only.

Run:  python3 -m onebox.server
Config comes from environment variables (see docs/setup.md):
  ONEBOX_DB, ONEBOX_BACKUPS, ONEBOX_HOST, ONEBOX_PORT, ONEBOX_TZ,
  ONEBOX_UI_KEY, ONEBOX_AGENT_TOKEN, ONEBOX_WATCH_TOKEN, ONEBOX_CAPTURE_TOKEN (optional),
  PIHOLE_URL and PIHOLE_PASSWORD (optional, for Pocket Pal's PC)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import feed, picalendar
from .pihole import PiHole
from .store import Store
from .triage import POST_SOURCES, capture, when_label

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "onebox_key"
MAX_BODY = 2_000_000
MAX_CAPTURE = 500
LATER_MAX_DAYS = 366  # how far ahead Not today can push a to-do


class Config:
    def __init__(self, env=os.environ):
        self.db = env.get("ONEBOX_DB", "onebox.sqlite3")
        # nightly copies of the database, the to-do list's safety net (14 kept)
        self.backups = env.get("ONEBOX_BACKUPS") or str(Path(self.db).resolve().parent / "backups")
        self.host = env.get("ONEBOX_HOST", "127.0.0.1")
        self.port = int(env.get("ONEBOX_PORT", "8787"))
        self.tz = ZoneInfo(env.get("ONEBOX_TZ", "Europe/Copenhagen"))
        self.ui_key = env.get("ONEBOX_UI_KEY", "")
        self.agent_token = env.get("ONEBOX_AGENT_TOKEN", "")
        self.watch_token = env.get("ONEBOX_WATCH_TOKEN", "")
        self.capture_token = env.get("ONEBOX_CAPTURE_TOKEN", "")  # for the iPhone Shortcut; optional
        # the York calendar feed and study plan the Pi fetches itself (see picalendar)
        self.calendars = env.get("ONEBOX_CALENDARS", "/etc/onebox-calendars.json")
        self.pihole_url = env.get("PIHOLE_URL", "http://127.0.0.1")
        self.pihole_password = env.get("PIHOLE_PASSWORD", "")
        missing = [n for n, v in [("ONEBOX_UI_KEY", self.ui_key), ("ONEBOX_AGENT_TOKEN", self.agent_token),
                                  ("ONEBOX_WATCH_TOKEN", self.watch_token)] if len(v) < 16]
        if self.capture_token and len(self.capture_token) < 16:
            missing.append("ONEBOX_CAPTURE_TOKEN")
        if missing:
            raise SystemExit(f"Set these to random strings of 16+ characters: {', '.join(missing)}")


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def make_handler(cfg: Config, store: Store):
    ui_cookie_value = hashlib.sha256(("onebox:" + cfg.ui_key).encode()).hexdigest()
    pihole = PiHole(cfg.pihole_url, cfg.pihole_password)

    class Handler(BaseHTTPRequestHandler):
        server_version = "OneInbox/1"

        def log_message(self, fmt, *args):  # keep logs free of tokens and query strings
            print(f"{self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}")

        # --- helpers ---------------------------------------------------
        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def _ui_ok(self) -> bool:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return COOKIE in jar and _same(jar[COOKIE].value, ui_cookie_value)

        def _agent_ok(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth.startswith("Bearer ") and _same(auth[7:], cfg.agent_token)

        def _capture_ok(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return self._ui_ok() or (auth.startswith("Bearer ") and _same(auth[7:], cfg.capture_token))

        def _todo_ok(self) -> bool:
            """The watch (feed token), the phone (capture token) or the page (cookie)."""
            return _same(self.headers.get("X-OneInbox-Token", ""), cfg.watch_token) or self._capture_ok()

        def _todo_state(self) -> dict:
            return store.todo_state(self._now().date().isoformat())

        def _now(self) -> datetime:
            return datetime.now(cfg.tz)

        def _status(self) -> dict:
            """The Mac's last report, with Today and the pillars from the Pi's own list once Things is cut off."""
            status = store.get_meta("things_status", {})
            if status.get("todos") != "pi":
                return status
            return feed.with_tasks(status, store.tasks(), store.tasks("done"), self._now())

        # --- routes ----------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path in ("/feed/watch.json", "/feed/today.json"):
                if not _same(self.headers.get("X-OneInbox-Token", ""), cfg.watch_token):
                    return self._send(401)
                status = self._status()
                pal = store.get_meta("pal")
                if path == "/feed/watch.json":
                    return self._json(200, feed.build(status, store.xp(), self._now(), self._todo_state(), pal))
                items = store.items()
                waiting = Counter(i.get("kind") for i in items if i["status"] == "decide" and not i["decision"]
                                  and not i.get("post"))
                return self._json(200, feed.today(status, store.xp(), self._now(), self._todo_state(), waiting, pal,
                                                  feed.post_summary(items, self._now())))
            if path == "/api/post":
                if not (self._agent_ok() or self._todo_ok()):
                    return self._send(401)
                return self._json(200, {"sources": feed.post_summary(store.items(), self._now())})
            if path == "/api/pal":
                if not self._todo_ok():
                    return self._send(401)
                return self._json(200, store.get_meta("pal") or {})
            if path == "/api/waiting":
                if not self._todo_ok():
                    return self._send(401)
                keep = ("id", "title", "task", "kind", "area", "sender", "received", "why", "excerpt", "links",
                        "attachments", "saved", "mail_link")
                waiting = [{k: i[k] for k in keep if i.get(k)} for i in store.items()
                           if i["status"] == "decide" and not i["decision"] and not i.get("post")]
                return self._json(200, {"items": waiting})
            if path == "/api/pihole":
                if not self._todo_ok():
                    return self._send(401)
                return self._json(200, pihole.summary())
            if path == "/":
                if not self._ui_ok():
                    return self._send(200, (WEB_DIR / "login.html").read_bytes(), "text/html; charset=utf-8")
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/items":
                if not self._ui_ok():
                    return self._send(401)
                return self._json(200, {
                    "items": store.items(),
                    "last_sync": store.get_meta("last_sync"),
                    "things": self._status(),
                    "xp": store.xp(),
                })
            if path == "/api/tasks":
                if not self._capture_ok():
                    return self._send(401)
                state = parse_qs(urlparse(self.path).query).get("status", ["open"])[0]
                if state not in ("open", "done", "gone"):
                    return self._json(400, {"error": "status is open, done or gone"})
                return self._json(200, {"tasks": store.tasks(state), "last_sync": store.get_meta("last_sync")})
            if path == "/api/outbox":
                if not self._agent_ok():
                    return self._send(401)
                return self._json(200, {"items": store.outbox()})
            if path == "/api/todo/outbox":
                if not self._agent_ok():
                    return self._send(401)
                return self._json(200, {"ids": store.todo_outbox(), "later": store.later_outbox(),
                                        "edits": store.edit_outbox(self._now().date().isoformat())})
            return self._send(404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/login":
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                key = parse_qs(self.rfile.read(n).decode(errors="replace")).get("key", [""])[0].strip()
                key = key.removeprefix("ONEBOX_UI_KEY=").strip()  # pasted straight from /etc/onebox.env
                if not _same(key, cfg.ui_key):
                    return self._send(401, b"Wrong key. Go back and try again.", "text/plain; charset=utf-8")
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self._send(303, headers={
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE}={ui_cookie_value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}",
                })
            try:
                m = re.fullmatch(r"/api/items/([0-9a-f]{16})/decision", path)
                if m:
                    if not self._capture_ok():  # the page, or Pocket Pal on the phone
                        return self._send(401)
                    decision = self._body().get("decision")
                    if not store.decide(m.group(1), decision):
                        return self._json(409, {"error": "Already on your to-do list, or no such item."})
                    store.outbox_to_tasks(self._now().date())  # "add" puts it on Pocket Pal's list at once
                    return self._json(200, {"ok": True})
                if path == "/api/capture":
                    if not self._capture_ok():
                        return self._send(401)
                    body = self._body()
                    text = str(body.get("text") or "").strip()
                    if not text or len(text) > MAX_CAPTURE:
                        return self._json(400, {"error": f"Send 1 to {MAX_CAPTURE} characters of text."})
                    item = capture(text, self._now(), {k: body.get(k) for k in ("when", "tags", "list", "deadline")})
                    store.upsert_items([item])
                    made = store.outbox_to_tasks(self._now().date())  # straight onto Pocket Pal's list, no Mac
                    where = when_label(item.get("when"))
                    place = where if where in ("Today", "Anytime", "Someday", "Tomorrow", "Evening") else "for " + where
                    return self._json(200, {"ok": True, "id": item["id"], "task": item["task"], "when": where,
                                            "task_id": made[-1]["id"] if made else None,
                                            "message": f"Got it. It's on your Pocket Pal list {place}."})
                if path == "/api/quiet/remove":
                    if not self._ui_ok():
                        return self._send(401)
                    return self._json(200, {"ok": store.unquiet(str(self._body().get("sender", "")))})
                if path == "/api/post/checked":
                    if not self._todo_ok():
                        return self._send(401)
                    source = str(self._body().get("source") or "")
                    if source not in POST_SOURCES:
                        return self._json(400, {"error": "source must be one of " + ", ".join(POST_SOURCES)})
                    n = store.check_post(source)
                    # tick the rolling "Check Aula (3 new)" to-do too; the Mac completes it in Things
                    for r in self._status().get("today") or []:
                        if r.get("id") and (r.get("title") or "").startswith(f"Check {source} ("):
                            store.todo_action(r["id"], "done", self._now().date().isoformat())
                    return self._json(200, {"ok": True, "checked": n})
                if path == "/api/tasks":
                    if not self._capture_ok():
                        return self._send(401)
                    fields = task_fields(self._body(), self._now().date(), new=True)
                    return self._json(201, {"ok": True, "task": store.add_task(fields)})
                m = re.fullmatch(r"/api/tasks/([A-Za-z0-9-]{1,64})(?:/(done|later))?", path)
                if m:
                    if not self._capture_ok():
                        return self._send(401)
                    tid, action = m.group(1), m.group(2)
                    body = self._body()
                    day = self._now().date()
                    if action == "done":
                        ok = store.finish_task(tid, day.isoformat())
                    elif action == "later":
                        when = task_fields({"when": body.get("when")}, day).get("when_day")
                        if not when or when <= day.isoformat():
                            return self._json(400, {"error": "when must be a day after today"})
                        ok = store.push_task(tid, when)
                    else:
                        ok = store.edit_task(tid, task_fields(body, day)) == "ok"
                    if not ok:
                        return self._json(404, {"error": "No open to-do with that id."})
                    return self._json(200, {"ok": True, "task": store.task(tid)})
                m = re.fullmatch(r"/api/todo/(done|skip|later)", path)
                if m:
                    if not self._todo_ok():
                        return self._send(401)
                    body = self._body()
                    uuid = str(body.get("id") or "")
                    status = self._status()
                    known = {r.get("id") for k in ("today", "pool", "week", "later") for r in status.get(k) or []}
                    if uuid not in known:
                        return self._json(404, {"error": "Not on today's list."})
                    day = self._now().date()
                    mine = status.get("todos") == "pi" and store.task(uuid) is not None
                    if mine and m.group(1) == "done":  # the Pi's own to-do: tick it off the list itself
                        store.finish_task(uuid, day.isoformat())
                    elif m.group(1) == "later":
                        try:
                            back = date.fromisoformat(str(body.get("when") or ""))
                        except ValueError:
                            return self._json(400, {"error": "when must be a date like 2026-11-03."})
                        if not day < back <= day + timedelta(days=LATER_MAX_DAYS):
                            return self._json(400, {"error": f"when must be after today and within {LATER_MAX_DAYS} days."})
                        if mine:
                            store.push_task(uuid, back.isoformat())
                        else:
                            store.todo_action(uuid, "later", back.isoformat())
                    else:
                        store.todo_action(uuid, m.group(1), day.isoformat())
                    status = self._status()
                    return self._json(200, {"ok": True, **feed.build(status, store.xp(), self._now(), self._todo_state())})
                if path == "/api/pal":
                    if not self._capture_ok():
                        return self._send(401)
                    pal = feed.clean_pal(self._body(), self._now())
                    store.set_meta("pal", pal)
                    return self._json(200, {"ok": True, **feed.pal_compact(pal, self._now())})
                if path == "/api/todo/ack":
                    if not self._agent_ok():
                        return self._send(401)
                    body = self._body()
                    acked = store.ack_todos(body.get("ids", [])) + store.ack_edits(body.get("edits") or [])
                    return self._json(200, {"ok": True, "acked": acked})
                if path == "/api/ingest":
                    if not self._agent_ok():
                        return self._send(401)
                    body = self._body()
                    n = store.upsert_items(body.get("items", []))
                    if "things" in body:
                        status = dict(body["things"])
                        completed = status.pop("completed_ids", [])
                        store.record_completed(completed)
                        everything = status.pop("all", None)
                        if isinstance(everything, list):  # an older Mac agent doesn't send it
                            store.sync_things(everything, completed)
                        for source, at in (status.pop("post_done", None) or {}).items():
                            store.check_post(source, at)  # Ben ticked "Check Aula" in Things
                        store.set_meta("things_status", status)
                    if body.get("adopt_things"):  # Pocket Pal went separate: Things' copies become the Pi's own
                        store.adopt_things()
                    store.outbox_to_tasks(self._now().date())  # automatic Aula/e-Boks/homework items
                    store.set_meta("last_sync", self._now().isoformat(timespec="seconds"))
                    return self._json(200, {"ok": True, "items": n})
                if path == "/api/agent/todos":  # calendar and job deadlines from the Mac, onto the Pi's list
                    if not self._agent_ok():
                        return self._send(401)
                    body = self._body()
                    source = body.get("source") if body.get("source") in ("calendar", "inbox") else "inbox"
                    todos = body.get("todos") if isinstance(body.get("todos"), list) else []
                    made = store.add_todos(todos, source, self._now().date())
                    return self._json(200, {"ok": True, "added": len(made)})
                if path == "/api/outbox/ack":
                    if not self._agent_ok():
                        return self._send(401)
                    return self._json(200, {"ok": True, "acked": store.ack_outbox(self._body().get("ids", []))})
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            return self._send(404)

    return Handler


TASK_KINDS = {"bill", "school", "admin", "msc", "job", "danish", "friend", "body", "travel", "other"}
TASK_LISTS = {"inbox", "today", "upcoming", "anytime", "someday"}


def task_fields(body: dict, today: date, new: bool = False) -> dict:
    """Check a to-do sent by the phone and turn it into store fields. Raises ValueError (a 400) when it's wrong.
    `when` and `due` are ISO days ("2026-11-03"), or "today" / "tomorrow"; null clears them."""
    if not isinstance(body, dict):
        raise ValueError("send a JSON object")
    out: dict = {}
    if new or "title" in body:
        title = str(body.get("title") or "").strip()
        if not title or len(title) > MAX_CAPTURE:
            raise ValueError(f"title must be 1 to {MAX_CAPTURE} characters")
        out["title"] = title
    if "notes" in body:
        notes = body.get("notes")
        out["notes"] = str(notes)[:4000] if notes else None
    if "kind" in body or new:
        kind = body.get("kind") or "other"
        if kind not in TASK_KINDS:
            raise ValueError("kind must be one of " + ", ".join(sorted(TASK_KINDS)))
        out["kind"] = kind
    for key, col in (("when", "when_day"), ("due", "due")):
        if key not in body:
            continue
        v = body.get(key)
        if v in (None, ""):
            out[col] = None
        elif v == "today":
            out[col] = today.isoformat()
        elif v == "tomorrow":
            out[col] = (today + timedelta(days=1)).isoformat()
        else:
            out[col] = date.fromisoformat(str(v)).isoformat()
    if "project" in body:
        project = body.get("project")
        out["project"] = str(project).strip()[:100] or None if project else None
    if "tags" in body:
        tags = body.get("tags") or []
        if not isinstance(tags, list) or len(tags) > 10:
            raise ValueError("tags must be a list of up to 10 names")
        out["tags"] = [str(t).strip()[:40] for t in tags if str(t).strip()]
    if "list" in body:
        if body["list"] not in TASK_LISTS:
            raise ValueError("list must be one of " + ", ".join(sorted(TASK_LISTS)))
        out["list"] = body["list"]
    elif new:
        w = out.get("when_day")
        out["list"] = "anytime" if not w else "today" if w <= today.isoformat() else "upcoming"
    return out


def backup_loop(cfg: Config, store: Store, stop: threading.Event | None = None) -> None:
    """Back the database up once a day: checks hourly, writes when the day's copy is missing."""
    stop = stop or threading.Event()
    while True:
        try:
            made = store.backup(cfg.backups, datetime.now(cfg.tz).date().isoformat())
            if made:
                print(f"One Inbox backup: {made}", flush=True)
        except Exception as e:  # a full disk mustn't take the server down
            print(f"One Inbox backup failed: {e}", flush=True)
        if stop.wait(3600):
            return


def main() -> None:
    cfg = Config()
    store = Store(cfg.db)
    threading.Thread(target=backup_loop, args=(cfg, store), daemon=True).start()
    threading.Thread(target=picalendar.loop, args=(cfg.calendars, store, cfg.tz), daemon=True).start()
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(cfg, store))
    print(f"One Inbox listening on http://{cfg.host}:{cfg.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
