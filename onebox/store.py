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
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

DECISIONS = {"add", "done", "later", "skip"}


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
        """Insert new items and refresh known ones. Ben's decisions are never overwritten."""
        ts = now_iso()
        with self.lock:
            for it in items:
                self.db.execute(
                    """INSERT INTO items (id, data, status, received, seen_at) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET data=excluded.data, status=excluded.status,
                       seen_at=excluded.seen_at""",
                    (it["id"], json.dumps(it), it["status"], it["received"], ts),
                )
            self.db.commit()
        return len(items)

    def items(self, limit_days: int | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM items ORDER BY received DESC").fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
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
            self.db.commit()
            return cur.rowcount == 1

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
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM completed").fetchone()[0]

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
