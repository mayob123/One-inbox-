"""SQLite storage on the Pi: triage items, Ben's decisions, the Things outbox and XP."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

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
        """Things completions, plus items ticked on the watch or phone that Things hasn't caught up with."""
        with self.lock:
            return self.db.execute(
                """SELECT (SELECT COUNT(*) FROM completed) +
                          (SELECT COUNT(*) FROM todo_actions WHERE action='done'
                           AND uuid NOT IN (SELECT uuid FROM completed))""").fetchone()[0]

    # --- to-dos ticked or skipped on the watch or phone ------------------

    def todo_action(self, uuid: str, action: str, day: str) -> None:
        """done: hide it now, and the Mac completes it in Things. skip: move it to the end until `day` ends."""
        with self.lock:
            self.db.execute(
                """INSERT INTO todo_actions (uuid, action, day, at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(uuid, action) DO UPDATE SET day=excluded.day, at=excluded.at""",
                (uuid, action, day, now_iso()))
            self.db.commit()

    def todo_state(self, day: str) -> dict:
        """{"done": ids ticked but not yet completed in Things, "skip": ids skipped today, oldest skip first}."""
        with self.lock:
            done = [r[0] for r in self.db.execute(
                "SELECT uuid FROM todo_actions WHERE action='done' AND uuid NOT IN (SELECT uuid FROM completed)")]
            skip = [r[0] for r in self.db.execute(
                "SELECT uuid FROM todo_actions WHERE action='skip' AND day=? ORDER BY at", (day,))]
        return {"done": done, "skip": skip}

    def todo_outbox(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT uuid FROM todo_actions WHERE action='done' AND synced_at IS NULL ORDER BY at")]

    def ack_todos(self, uuids: list[str]) -> int:
        ts = now_iso()
        with self.lock:
            n = sum(self.db.execute("UPDATE todo_actions SET synced_at=? WHERE uuid=? AND action='done'",
                                    (ts, u)).rowcount for u in uuids)
            self.db.commit()
        return n

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
