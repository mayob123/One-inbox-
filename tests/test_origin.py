"""Where a to-do came from, for the word Pocket Pal shows on it."""

import unittest
from datetime import date, datetime, timezone

from onebox import feed, studycal
from onebox.store import Store
from onebox.todos import origin


class OriginTests(unittest.TestCase):
    def test_each_source_has_a_word(self):
        study = studycal.config_study([{"title": "A&DS Week 4", "on": "2026-11-23", "tags": ["MSc"]}])[0]
        [plan] = studycal.todos_for(study, date(2026, 11, 17))
        canvas = studycal.Event(uid="u", title="Formative", start=date(2026, 11, 30), calendar="York MSc",
                                tags=["MSc"], extra={"deadline": True})
        [york] = studycal.todos_for(canvas, date(2026, 11, 17))
        self.assertEqual(origin({"source": "calendar", "kind": "msc", **plan}), "York plan")
        self.assertEqual(origin({"source": "calendar", **york}), "York calendar")
        self.assertEqual(origin({"source": "calendar", "title": "Apply: X at Y (due Fri 4 Dec)"}), "Jobs")
        self.assertEqual(origin({"source": "inbox", "title": "Danish homework"}), "Email")
        self.assertEqual(origin({"source": "capture", "title": "Milk"}), "Quick add")
        self.assertEqual(origin({"source": "app", "title": "Mine"}), "You")
        # a York item that went round through Things and was adopted still says York
        self.assertEqual(origin({"source": "app", **york}), "York calendar")
        # and one that lost its note on the way, but kept the deadline title
        self.assertEqual(origin({"source": "app", "kind": "msc", "title": "Formative (due Mon 30 Nov 13:00)"}),
                         "York calendar")
        self.assertEqual(origin({"source": "app", "title": "Ring about the due date"}), "You")

    def test_tasks_and_feed_carry_it(self):
        store = Store(":memory:")
        store.add_task({"title": "Mine", "list": "today", "when_day": "2026-11-17"}, "app")
        store.add_task({"title": "Homework", "list": "upcoming", "when_day": "2026-11-20"}, "inbox")
        tasks = store.tasks()
        self.assertEqual({t["title"]: t["from"] for t in tasks}, {"Mine": "You", "Homework": "Email"})
        now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        status = feed.from_tasks(tasks, [], now)
        got = feed.today(status, 0, now)
        self.assertEqual(got["items"][0]["from"], "You")
        self.assertEqual(got["later"][0]["from"], "Email")
        self.assertNotIn("from", feed.build(status, 0, now))  # the watch feed stays small
        store.close()


if __name__ == "__main__":
    unittest.main()
