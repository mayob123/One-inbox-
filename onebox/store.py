"""SQLite storage on the Pi: triage items, Ben's decisions, the Things outbox and XP."""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
import threading
from datetime import date, datetime, timezone

from .todos import task_from_todo, todo_from_item

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  status TEXT NOT NULL,
  received TEXT NOT NULL,
  decision TEXT,
  decided_at TEXT,
  sent_to_things_at TEXT,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS completed (
  uuid TEXT PRIMARY KEY,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiet (
  sender TEXT PRIMARY KEY,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todo_actions (
  uuid TEXT NOT NULL,
  action TEXT NOT NULL,
  day TEXT NOT NULL,
  at TEXT NOT NULL,
  synced_at TEXT,
  PRIMARY KEY (uuid, action)
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  notes TEXT,
  kind TEXT,
  list TEXT,
  when_day TEXT,
  due TEXT,
  tags TEXT,
  project TEXT,
  area TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  done_at TEXT
);
CREATE TABLE IF NOT EXISTS task_edits (
  uuid TEXT PRIMARY KEY,
  fields TEXT NOT NULL,
  at TEXT NOT NULL,
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

DECISIONS = {"add", "done", "none", "later", "skip"}  # later/skip: older pages


def sender_key(item: dict) -> str:
    return (item.get("from_addr") or item.get("sender") or "").strip().lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # --- items -------------------------------------------------------------

    def upsert_items(self, items: list[dict]) -> int:
        """Insert new items and refresh known ones. Ben's decisions are never overwritten.

        Items marked auto="things" (e-Boks, Aula, quick capture) are queued for Things the
        first time they are seen, unless Ben said the sender needs no action. He can still
        untick them on the page before the Mac syncs.
        """
        ts = now_iso()
        with self.lock:
            quiet = {q["sender"] for q in self.db.execute("SELECT sender FROM quiet")}
            for it in items:
                auto = it.get("auto") == "things" and sender_key(it) not in quiet
                self.db.execute(
                    """INSERT INTO items (id, data, status, received, seen_at, decision, decided_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET data=excluded.data, status=excluded.status,
                       seen_at=excluded.seen_at""",
                    (it["id"], json.dumps(it), it["status"], it["received"], ts,
                     "add" if auto else None, ts if auto else None),
                )
            self.db.commit()
        return len(items)

    def items(self, limit_days: int | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM items ORDER BY received DESC").fetchall()
            quiet = {q["sender"] for q in self.db.execute("SELECT sender FROM quiet")}
        out = []
        for r in rows:
            d = json.loads(r["data"])
            muted = r["decision"] == "none" or (not r["decision"] and sender_key(d) in quiet)
            if d.get("status") == "decide" and muted:
                d["status"] = "noise"
                d["why"] = "You said emails from this sender need no action."
                d["quiet"] = sender_key(d)
            d["decision"] = r["decision"]
            d["queued_for_things"] = r["decision"] == "add" and not r["sent_to_things_at"]
            d["in_things"] = bool(r["sent_to_things_at"])
            out.append(d)
        return out

    def decide(self, item_id: str, decision: str | None) -> bool:
        if decision is not None and decision not in DECISIONS:
            raise ValueError("unknown decision")
        with self.lock:
            cur = self.db.execute(
                "UPDATE items SET decision=?, decided_at=? WHERE id=? AND sent_to_things_at IS NULL",
                (decision, now_iso() if decision else None, item_id),
            )
            row = self.db.execute("SELECT data FROM items WHERE id=?", (item_id,)).fetchone()
            key = sender_key(json.loads(row["data"])) if row else ""
            if cur.rowcount == 1 and key:
                if decision == "none":  # learn: future mail from this sender is folded away
                    self.db.execute("INSERT OR REPLACE INTO quiet (sender, at) VALUES (?, ?)", (key, now_iso()))
                else:
                    self.db.execute("DELETE FROM quiet WHERE sender=?", (key,))
            self.db.commit()
            return cur.rowcount == 1

    def unquiet(self, sender: str) -> bool:
        """Stop folding a sender's mail, and bring back the emails already marked no action needed."""
        key = sender.strip().lower()
        with self.lock:
            n = self.db.execute("DELETE FROM quiet WHERE sender=?", (key,)).rowcount
            for r in self.db.execute("SELECT id, data FROM items WHERE decision='none'").fetchall():
                if sender_key(json.loads(r["data"])) == key:
                    self.db.execute("UPDATE items SET decision=NULL, decided_at=NULL WHERE id=?", (r["id"],))
                    n += 1
            self.db.commit()
        return n > 0

    def quiet_senders(self) -> list[str]:
        with self.lock:
            return [r["sender"] for r in self.db.execute("SELECT sender FROM quiet ORDER BY at DESC")]

    # --- official post: alerts are counted, then checked in one go ---------

    def check_post(self, source: str, before: str | None = None) -> int:
        """Mark unread alerts from `source` (Aula, e-Boks, Digital Post) as checked, all of them or those
        received up to `before` (an ISO time: when Ben ticked "Check Aula" in Things). Returns how many."""
        cut = None
        if before:
            try:
                cut = datetime.fromisoformat(before)
            except ValueError:
                return 0
        ts = now_iso()
        with self.lock:
            n = 0
            for r in self.db.execute("SELECT id, data FROM items WHERE decision IS NULL AND status='decide'").fetchall():
                d = json.loads(r["data"])
                if d.get("post") != source:
                    continue
                if cut is not None:
                    try:
                        received = datetime.fromisoformat(d["received"])
                        if received.tzinfo is None or cut.tzinfo is None:  # compare like with like
                            received, cut = received.replace(tzinfo=None), cut.replace(tzinfo=None)
                        if received > cut:
                            continue
                    except (KeyError, ValueError):
                        continue
                n += self.db.execute("UPDATE items SET decision='done', decided_at=? WHERE id=?", (ts, r["id"])).rowcount
            self.db.commit()
        return n

    # --- outbox to Things (the Mac agent drains it) -----------------------

    def outbox(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM items WHERE decision='add' AND sent_to_things_at IS NULL"
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def ack_outbox(self, ids: list[str]) -> int:
        ts = now_iso()
        with self.lock:
            n = 0
            for i in ids:
                n += self.db.execute(
                    "UPDATE items SET sent_to_things_at=? WHERE id=? AND decision='add'", (ts, i)
                ).rowcount
            self.db.commit()
        return n

    def outbox_to_tasks(self, today: date) -> list[dict]:
        """Everything that used to wait here for the Mac to add to Things (emails Ben added, captures, the
        automatic Aula/e-Boks/homework items) goes on the Pi's own to-do list instead. Returns the new to-dos."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, data FROM items WHERE decision='add' AND sent_to_things_at IS NULL").fetchall()
        made = []
        for r in rows:
            item = json.loads(r["data"])
            fields = task_from_todo(todo_from_item(item), today)
            if fields:
                made.append(self.add_task(fields, "capture" if item.get("source") == "capture" else "inbox"))
            self.ack_outbox([r["id"]])
        return made

    def add_todos(self, todos: list[dict], source: str, today: date) -> list[dict]:
        """Things-shaped to-dos from the Mac (calendar and job deadlines) onto the Pi's list."""
        made = []
        for attrs in todos[:100]:
            fields = task_from_todo(attrs, today) if isinstance(attrs, dict) else None
            if fields:
                made.append(self.add_task(fields, source))
        return made

    def adopt_things(self) -> int:
        """Pocket Pal goes separate: the open to-dos copied from Things become the Pi's own, so the app can change
        them freely and nothing waits to be carried back to Things."""
        with self.lock:
            n = self.db.execute("UPDATE tasks SET source='app', updated_at=? WHERE source='things' AND status='open'",
                                (now_iso(),)).rowcount
            self.db.execute("UPDATE todo_actions SET synced_at=? WHERE synced_at IS NULL", (now_iso(),))
            self.db.execute("UPDATE task_edits SET synced_at=? WHERE synced_at IS NULL", (now_iso(),))
            self.db.commit()
        return n

    # --- XP: one point per Things to-do Ben completes, counted once -------

    def record_completed(self, uuids: list[str]) -> int:
        ts = now_iso()
        with self.lock:
            added = 0
            for u in uuids:
                added += self.db.execute(
                    "INSERT OR IGNORE INTO completed (uuid, at) VALUES (?, ?)", (u, ts)
                ).rowcount
            self.db.commit()
        return added

    def xp(self) -> int:
        """Things completions, items ticked on the watch or phone that Things hasn't caught up with, and to-dos
        done on Pocket Pal's own list, each counted once."""
        with self.lock:
            return self.db.execute(
                """SELECT (SELECT COUNT(*) FROM completed) +
                          (SELECT COUNT(*) FROM todo_actions WHERE action='done'
                           AND uuid NOT IN (SELECT uuid FROM completed)) +
                          (SELECT COUNT(*) FROM tasks WHERE status='done'
                           AND id NOT IN (SELECT uuid FROM completed)
                           AND id NOT IN (SELECT uuid FROM todo_actions WHERE action='done'))""").fetchone()[0]

    # --- to-dos ticked or skipped on the watch or phone ------------------

    def todo_action(self, uuid: str, action: str, day: str) -> None:
        """done: hide it now, and the Mac completes it in Things. skip: move it to the end until `day` ends.
        later: `day` is the day it should come back; it leaves Today now and the Mac moves it in Things."""
        with self.lock:
            self.db.execute(
                """INSERT INTO todo_actions (uuid, action, day, at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(uuid, action) DO UPDATE SET day=excluded.day, at=excluded.at, synced_at=NULL""",
                (uuid, action, day, now_iso()))
            self.db.commit()

    def todo_state(self, day: str) -> dict:
        """{"done": ids ticked but not yet completed in Things, "skip": ids skipped today, oldest skip first}."""
        with self.lock:
            done = [r[0] for r in self.db.execute(
                "SELECT uuid FROM todo_actions WHERE action='done' AND uuid NOT IN (SELECT uuid FROM completed)")]
            skip = [r[0] for r in self.db.execute(
                "SELECT uuid FROM todo_actions WHERE action='skip' AND day=? ORDER BY at", (day,))]
            later = {r[0]: r[1] for r in self.db.execute(
                """SELECT uuid, day FROM todo_actions WHERE action='later' AND day>?
                   AND uuid NOT IN (SELECT uuid FROM todo_actions WHERE action='done')""", (day,))}
        return {"done": done, "skip": skip, "later": later}

    def todo_outbox(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT uuid FROM todo_actions WHERE action='done' AND synced_at IS NULL ORDER BY at")]

    def later_outbox(self) -> list[dict]:
        """To-dos pushed to another day that the Mac hasn't moved in Things yet: [{"id", "when"}]."""
        with self.lock:
            return [{"id": r[0], "when": r[1]} for r in self.db.execute(
                "SELECT uuid, day FROM todo_actions WHERE action='later' AND synced_at IS NULL ORDER BY at")]

    def edit_outbox(self, today: str) -> list[dict]:
        """Edits to Things to-dos the Mac hasn't made in Things yet: [{"id", "at", "title"?, "notes"?, "when"?}].
        `when` is what Things takes: "today", "anytime", "someday" or a day."""
        with self.lock:
            rows = self.db.execute(
                """SELECT e.uuid, e.fields, e.at, t.when_day, t.list FROM task_edits e JOIN tasks t ON t.id = e.uuid
                   WHERE e.synced_at IS NULL AND t.status = 'open' ORDER BY e.at""").fetchall()
        out = []
        for r in rows:
            f = json.loads(r["fields"])
            item = {"id": r["uuid"], "at": r["at"]}
            if "title" in f:
                item["title"] = f["title"]
            if "notes" in f:
                item["notes"] = f["notes"] or ""
            if "when_day" in f or "list" in f:
                day, lst = r["when_day"], r["list"]
                if lst == "someday":
                    item["when"] = "someday"
                elif day and day > today:
                    item["when"] = day
                elif day or lst == "today":
                    item["when"] = "today"
                else:
                    item["when"] = "anytime"
            out.append(item)
        return out

    def ack_edits(self, done: list[dict]) -> int:
        """The Mac made these edits in Things. Each carries the `at` it was sent with, so a newer edit stays queued."""
        ts = now_iso()
        with self.lock:
            n = sum(self.db.execute("UPDATE task_edits SET synced_at=? WHERE uuid=? AND at=? AND synced_at IS NULL",
                                    (ts, str(d.get("id")), str(d.get("at")))).rowcount for d in done if d.get("id"))
            self.db.commit()
        return n

    def ack_todos(self, uuids: list[str]) -> int:
        ts = now_iso()
        with self.lock:
            n = sum(self.db.execute("""UPDATE todo_actions SET synced_at=? WHERE uuid=? AND action IN ('done', 'later')
                                       AND synced_at IS NULL""", (ts, u)).rowcount for u in uuids)
            self.db.commit()
        return n

    # --- tasks: every open to-do, kept on the Pi -----------------------------
    # Phase 1 of moving the list into Pocket Pal: the Pi mirrors every open Things to-do (source "things")
    # and can hold to-dos of its own (source "app"). Things is still where Things to-dos change: done and
    # Not today on one go through todo_actions, so the Mac does them in Things as before.

    TASK_FIELDS = ("title", "notes", "kind", "list", "when_day", "due", "project", "area")

    def sync_things(self, rows: list[dict], completed: list[str] | None = None) -> dict:
        """Mirror the Mac's full list of open Things to-dos (Someday included). One that has left the list is
        done (completed in Things, or ticked here) or gone (deleted or cancelled in Things). A tick or a Not today
        made here that the Mac hasn't carried out in Things yet wins over the older Things state."""
        ts = now_iso()
        seen = set()
        with self.lock:
            for r in rows:
                uid = str(r.get("id") or "")
                if not uid or not str(r.get("title") or "").strip():
                    continue
                seen.add(uid)
                vals = (uid, str(r["title"])[:500], (r.get("notes") or None), r.get("kind"), r.get("list"),
                        r.get("when"), r.get("due"), json.dumps(r.get("tags") or []), r.get("project"), r.get("area"))
                self.db.execute(
                    """INSERT INTO tasks (id, source, title, notes, kind, list, when_day, due, tags, project, area,
                                          status, created_at, updated_at)
                       VALUES (?, 'things', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         title=CASE WHEN EXISTS (SELECT 1 FROM task_edits e WHERE e.uuid=tasks.id AND e.synced_at IS NULL) THEN tasks.title ELSE excluded.title END,
                         notes=CASE WHEN EXISTS (SELECT 1 FROM task_edits e WHERE e.uuid=tasks.id AND e.synced_at IS NULL) THEN tasks.notes ELSE COALESCE(excluded.notes, tasks.notes) END,
                         kind=excluded.kind, due=excluded.due,
                         tags=excluded.tags, project=excluded.project, area=excluded.area,
                         list=CASE WHEN EXISTS (SELECT 1 FROM todo_actions a WHERE a.uuid=tasks.id
                                  AND a.action='later' AND a.synced_at IS NULL) OR EXISTS (SELECT 1 FROM task_edits e WHERE e.uuid=tasks.id AND e.synced_at IS NULL)
                                  THEN tasks.list ELSE excluded.list END,
                         when_day=CASE WHEN EXISTS (SELECT 1 FROM todo_actions a WHERE a.uuid=tasks.id
                                  AND a.action='later' AND a.synced_at IS NULL) OR EXISTS (SELECT 1 FROM task_edits e WHERE e.uuid=tasks.id AND e.synced_at IS NULL)
                                  THEN tasks.when_day ELSE excluded.when_day END,
                         status=CASE WHEN EXISTS (SELECT 1 FROM todo_actions a WHERE a.uuid=tasks.id AND a.action='done'
                                  AND a.uuid NOT IN (SELECT uuid FROM completed)) THEN tasks.status ELSE 'open' END,
                         done_at=CASE WHEN EXISTS (SELECT 1 FROM todo_actions a WHERE a.uuid=tasks.id AND a.action='done'
                                  AND a.uuid NOT IN (SELECT uuid FROM completed)) THEN tasks.done_at ELSE NULL END,
                         updated_at=excluded.updated_at
                       WHERE tasks.source = 'things'""", (*vals, ts, ts))
            done_ids = set(completed or []) | {r[0] for r in self.db.execute("SELECT uuid FROM completed")} \
                | {r[0] for r in self.db.execute("SELECT uuid FROM todo_actions WHERE action='done'")}
            left = [r[0] for r in self.db.execute("SELECT id FROM tasks WHERE source='things' AND status='open'")
                    if r[0] not in seen]
            for uid in left:
                status = "done" if uid in done_ids else "gone"
                self.db.execute("UPDATE tasks SET status=?, done_at=?, updated_at=? WHERE id=?",
                                (status, ts if status == "done" else None, ts, uid))
            self.db.commit()
        return {"open": len(seen), "closed": len(left)}

    @staticmethod
    def _task(r: sqlite3.Row) -> dict:
        d = {k: r[k] for k in ("id", "source", "title", "notes", "kind", "list", "due", "project", "area", "status",
                               "created_at", "updated_at", "done_at")}
        d["when"] = r["when_day"]
        d["tags"] = json.loads(r["tags"] or "[]")
        return {k: v for k, v in d.items() if v not in (None, [], "")}

    def backup(self, folder: str, day: str, keep: int = 14) -> str | None:
        """Copy the whole database to folder/onebox-<day>.sqlite3 (SQLite's online backup, safe while it runs),
        once a day, keeping the newest `keep` copies. Returns the new file, or None when today's is already there."""
        out = Path(folder)
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"onebox-{day}.sqlite3"
        if target.exists():
            return None
        tmp = target.with_suffix(".part")
        dest = sqlite3.connect(tmp)
        try:
            with self.lock:
                self.db.backup(dest)
        finally:
            dest.close()
        tmp.replace(target)
        for old in sorted(out.glob("onebox-*.sqlite3"))[:-keep]:
            old.unlink()
        return str(target)

    def tasks(self, status: str = "open") -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                """SELECT * FROM tasks WHERE status=?
                   ORDER BY when_day IS NULL, when_day, due IS NULL, due, created_at""", (status,)).fetchall()
        return [self._task(r) for r in rows]

    def task(self, task_id: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(r) if r else None

    def add_task(self, fields: dict, source: str = "app") -> dict:
        """A to-do on the Pi's own list (Things never gets it): made in Pocket Pal ("app"), or by One Inbox
        from an email, a capture or a calendar or job deadline ("inbox", "capture", "calendar")."""
        ts = now_iso()
        tid = "pp-" + secrets.token_hex(6)
        with self.lock:
            self.db.execute(
                """INSERT INTO tasks (id, source, title, notes, kind, list, when_day, due, tags, project, status,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (tid, source, fields["title"], fields.get("notes"), fields.get("kind"), fields.get("list"),
                 fields.get("when_day"), fields.get("due"), json.dumps(fields.get("tags") or []),
                 fields.get("project"), ts, ts))
            self.db.commit()
        return self.task(tid)

    # What can change on a Things to-do from here: the Mac carries it over to Things (title, notes and day).
    THINGS_EDITABLE = ("title", "notes", "when_day", "list")

    def edit_task(self, task_id: str, fields: dict) -> str:
        """Change a to-do. One from Things changes here at once and waits in the Mac's outbox, which makes the same
        change in Things; until then the next sync keeps this version. Returns "ok" or "missing"."""
        cur = self.task(task_id)
        if not cur:
            return "missing"
        things = cur["source"] == "things"
        allowed = self.THINGS_EDITABLE if things else self.TASK_FIELDS
        sets = {k: v for k, v in fields.items() if k in allowed}
        if things and sets:
            with self.lock:
                row = self.db.execute("SELECT fields FROM task_edits WHERE uuid=? AND synced_at IS NULL",
                                      (task_id,)).fetchone()
                merged = {**(json.loads(row["fields"]) if row else {}), **sets}
                self.db.execute(
                    """INSERT INTO task_edits (uuid, fields, at) VALUES (?, ?, ?)
                       ON CONFLICT(uuid) DO UPDATE SET fields=excluded.fields, at=excluded.at, synced_at=NULL""",
                    (task_id, json.dumps(merged), datetime.now(timezone.utc).isoformat()))
                self.db.commit()
        elif things:
            return "ok"
        if "tags" in fields and not things:
            sets["tags"] = json.dumps(fields["tags"] or [])
        if not sets:
            return "ok"
        with self.lock:
            self.db.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?",
                            (*sets.values(), now_iso(), task_id))
            self.db.commit()
        return "ok"

    def finish_task(self, task_id: str, day: str) -> bool:
        """Done. A Things to-do also goes to the Mac's outbox, which completes it in Things."""
        cur = self.task(task_id)
        if not cur or cur["status"] != "open":
            return False
        if cur["source"] == "things":
            self.todo_action(task_id, "done", day)
        ts = now_iso()
        with self.lock:
            self.db.execute("UPDATE tasks SET status='done', done_at=?, updated_at=? WHERE id=?", (ts, ts, task_id))
            self.db.commit()
        return True

    def close_task(self, task_id: str) -> bool:
        """Take an open to-do off the list without counting it as done (a double)."""
        with self.lock:
            n = self.db.execute("UPDATE tasks SET status='gone', updated_at=? WHERE id=? AND status='open'",
                                (now_iso(), task_id)).rowcount
            self.db.commit()
        return bool(n)

    def push_task(self, task_id: str, when: str) -> bool:
        """Not today: set for `when`. A Things to-do is moved in Things by the Mac too."""
        cur = self.task(task_id)
        if not cur or cur["status"] != "open":
            return False
        if cur["source"] == "things":
            self.todo_action(task_id, "later", when)
        with self.lock:
            self.db.execute("UPDATE tasks SET when_day=?, list='upcoming', updated_at=? WHERE id=?",
                            (when, now_iso(), task_id))
            self.db.commit()
        return True

    # --- meta: last agent report ------------------------------------------

    def set_meta(self, key: str, value) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.db.commit()

    def get_meta(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
