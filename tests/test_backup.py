import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from onebox.server import Config, backup_loop
from onebox.store import Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(str(self.dir / "onebox.sqlite3"))
        self.store.add_task({"title": "Hang the pictures", "kind": "admin", "list": "anytime"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_once_a_day_and_keeps_fourteen(self):
        out = self.dir / "backups"
        made = self.store.backup(str(out), "2026-11-02")
        self.assertTrue(made and made.endswith("onebox-2026-11-02.sqlite3"))
        self.assertIsNone(self.store.backup(str(out), "2026-11-02"))  # already done today
        rows = sqlite3.connect(made).execute("SELECT title FROM tasks").fetchall()
        self.assertEqual(rows, [("Hang the pictures",)])
        for d in range(3, 20):
            self.store.backup(str(out), f"2026-11-{d:02d}")
        kept = sorted(p.name for p in out.glob("onebox-*.sqlite3"))
        self.assertEqual(len(kept), 14)
        self.assertEqual(kept[0], "onebox-2026-11-06.sqlite3")
        self.assertEqual(list(out.glob("*.part")), [])

    def test_loop_backs_up_and_defaults_next_to_the_db(self):
        key = "k" * 20
        cfg = Config({"ONEBOX_DB": str(self.dir / "onebox.sqlite3"), "ONEBOX_UI_KEY": key,
                      "ONEBOX_AGENT_TOKEN": key, "ONEBOX_WATCH_TOKEN": key})
        self.assertEqual(Path(cfg.backups), self.dir.resolve() / "backups")
        stop = threading.Event()
        stop.set()  # one pass, then return
        backup_loop(cfg, self.store, stop)
        self.assertEqual(len(list(Path(cfg.backups).glob("onebox-*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
