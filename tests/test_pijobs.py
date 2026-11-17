"""The Pi reading the job tracker itself, so Pocket Pal's job leads don't wait for the Mac."""

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from onebox import pijobs, studycal
from onebox.server import Config, make_handler
from onebox.store import Store

from tests.test_agent import NOW, make_job_tracker
from tests.test_server import ENV, Opener


class PiJobsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "job_tracker.db")
        make_job_tracker(self.db)
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_no_copy_means_nothing_changes(self):
        self.assertEqual(pijobs.sync(self.store, os.path.join(self.dir.name, "missing.db"), NOW), {})
        self.assertIsNone(pijobs.fresh(self.store, NOW))
        status = {"at": "2026-10-25T09:00:00+00:00", "pillars": {"job": {"leads": 9}}}
        self.assertEqual(pijobs.apply(status, None), status)

    def test_reads_leads_and_interviews_but_adds_no_apply_todos(self):
        got = pijobs.sync(self.store, self.db, NOW)
        self.assertEqual(got["extra"]["leads"], 2)
        self.assertEqual([e["title"] for e in got["events"]], ["Interview: DTU"])
        self.assertEqual(got["added"], 0)
        self.assertEqual(self.store.tasks(), [])

    def test_open_apply_todos_are_put_away_and_bens_own_stay(self):
        events = pijobs.agent.job_deadlines({"job_tracker_db": self.db}, NOW)
        old = [a for ev in events for a in studycal.todos_for(ev, NOW.date())]
        self.store.add_todos(old, "calendar", NOW.date())
        mine = self.store.add_task({"title": "Apply: the one I typed myself"}, "app")
        self.assertEqual(pijobs.sync(self.store, self.db, NOW)["put_away"], 2)
        self.assertEqual([t["id"] for t in self.store.tasks()], [mine["id"]])
        self.assertEqual(len(self.store.tasks("gone")), 2)
        self.assertEqual(pijobs.sync(self.store, self.db, NOW + timedelta(minutes=10))["put_away"], 0)

    def test_apply_replaces_the_macs_job_part_and_time(self):
        got = pijobs.sync(self.store, self.db, NOW)
        mac = {"at": (NOW - timedelta(days=3)).isoformat(), "todos": "pi",
               "events": [{"title": "Dentist", "start": (NOW + timedelta(hours=1)).isoformat()},
                          {"title": "Interview: Old", "start": (NOW + timedelta(hours=2)).isoformat()}],
               "pillars": {"job": {"leads": 40, "lead_list": [{"title": "stale"}], "job_due": "2026-10-01"}}}
        out = pijobs.apply(mac, pijobs.fresh(self.store, NOW + timedelta(minutes=5)))
        self.assertEqual(out["at"], got["at"])
        self.assertEqual([e["title"] for e in out["events"]], ["Dentist", "Interview: DTU"])
        self.assertEqual(out["pillars"]["job"]["leads"], 2)
        self.assertNotIn({"title": "stale"}, out["pillars"]["job"]["lead_list"])
        self.assertIsNone(pijobs.fresh(self.store, NOW + timedelta(hours=3)))  # copy stopped: back to the Mac's


class FeedTests(unittest.TestCase):
    def test_today_feed_uses_the_pis_job_read(self):
        store = Store(":memory:")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Config(ENV), store))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base, web = f"http://127.0.0.1:{httpd.server_address[1]}", Opener()
            now = datetime.now().astimezone()
            old = (now - timedelta(days=3)).isoformat(timespec="seconds")
            web.req(base, "POST", "/api/ingest", {"items": [], "things": {"at": old, "todos": "pi"}},
                    {"Authorization": "Bearer " + "a" * 20})
            store.set_meta("job_sync", {"at": now.isoformat(timespec="seconds"), "events": [],
                                        "extra": {"leads": 3, "lead_list": [], "job_due": None}})
            code, raw = web.req(base, "GET", "/feed/today.json", headers={"X-OneInbox-Token": "w" * 20})
            self.assertEqual(code, 200)
            self.assertNotEqual(json.loads(raw)["at"], old)
        finally:
            httpd.shutdown()
            httpd.server_close()
            store.close()


if __name__ == "__main__":
    unittest.main()
