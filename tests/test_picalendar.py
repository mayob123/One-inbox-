import json
import os
import tempfile
import unittest
from unittest import mock

from onebox import agent, picalendar
from onebox.store import Store
from tests.test_studycal import ICS, NOW


class PiCalendarTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.dir.name, "db.sqlite3"))
        self.cfg = {"calendars": [{"name": "York MSc", "url": "https://canvas/feed.ics", "list": "York MSc",
                                   "tags": ["MSc"]}]}

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_no_file_means_nothing_to_do(self):
        self.assertEqual(picalendar.load(os.path.join(self.dir.name, "missing.json")), {})
        self.assertEqual(picalendar.sync(self.store, {}, NOW), {})

    def test_load_keeps_only_calendar_settings(self):
        path = os.path.join(self.dir.name, "cal.json")
        with open(path, "w") as f:
            json.dump({**self.cfg, "token": "never-used"}, f)
        self.assertEqual(set(picalendar.load(path)), {"calendars"})

    def test_adds_once_and_skips_what_the_mac_already_added(self):
        first = picalendar.sync(self.store, self.cfg, NOW, fetcher=lambda url: ICS)
        self.assertGreater(first["added"], 0)
        titles = [t["title"] for t in self.store.tasks()]
        self.assertIn("19:00 Live session: Week 1 welcome", titles)
        self.assertTrue(all(t["source"] == "calendar" and t["kind"] == "msc" for t in self.store.tasks()))
        self.assertEqual(picalendar.sync(self.store, self.cfg, NOW, fetcher=lambda url: ICS)["added"], 0)
        # a fresh seen list (Pi took over from the Mac) still doesn't add the same to-do twice
        self.store.set_meta(picalendar.SEEN_KEY, {})
        self.assertEqual(picalendar.sync(self.store, self.cfg, NOW, fetcher=lambda url: ICS)["added"], 0)
        self.assertEqual(len(self.store.tasks()), len(titles))

    def test_york_items_that_came_back_through_things_are_not_doubled(self):
        first = picalendar.sync(self.store, self.cfg, NOW, fetcher=lambda url: ICS)
        made = self.store.tasks()
        # what the old Mac put in Things, copied back and adopted (source app), with an older start day
        with self.store.lock:
            self.store.db.execute("UPDATE tasks SET source='app', created_at='2026-10-30T00:00:00+00:00'")
            self.store.db.commit()
        for t in made:
            self.store.add_task({"title": t["title"], "list": "upcoming", "when_day": t.get("when")}, "calendar")
        self.store.set_meta(picalendar.SEEN_KEY, {})
        self.assertEqual(picalendar.sync(self.store, self.cfg, NOW, fetcher=lambda url: ICS)["added"], 0)
        self.assertEqual(len(self.store.tasks()), first["added"])
        self.assertTrue(all(t["source"] == "app" for t in self.store.tasks()))

    def test_a_file_on_the_mac_is_read_beside_the_settings(self):
        path = os.path.join(self.dir.name, "onebox-calendars.json")
        with open(path, "w") as f:
            json.dump({"calendars": [{"name": "York blocks", "url": "~/.config/onebox/york-blocks.ics"}]}, f)
        self.assertEqual(picalendar.load(path)["calendars"][0]["url"],
                         os.path.join(self.dir.name, "onebox-york-blocks.ics"))

    def test_broken_feed_is_reported_not_raised(self):
        def boom(url):
            raise OSError("offline")
        got = picalendar.sync(self.store, self.cfg, NOW, fetcher=boom)
        self.assertEqual((got["added"], len(got["errors"])), (0, 1))
        self.assertEqual(self.store.get_meta("calendar_sync")["errors"], got["errors"])

    def test_mac_leaves_the_york_feed_to_the_pi(self):
        cfg = {**self.cfg, "calendars_on_pi": True, "job_tracker_db": "", "server": "http://pi", "token": "t"}
        with mock.patch.object(agent.studycal, "fetch") as fetch, mock.patch.object(agent, "call") as call:
            self.assertEqual(agent.sync_calendars(cfg, NOW), {})
        fetch.assert_not_called()
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
