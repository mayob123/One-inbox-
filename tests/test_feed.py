import unittest
from datetime import datetime, timedelta, timezone

from onebox.feed import build, mood, next_event, short, today

NOON = datetime(2026, 10, 26, 12, 0)
LATE = datetime(2026, 10, 26, 22, 30)


class FeedTests(unittest.TestCase):
    def test_mood_order(self):
        self.assertEqual(mood(3, 1, 1, LATE), "hungry")
        self.assertEqual(mood(3, 0, 1, LATE), "lonely")
        self.assertEqual(mood(3, 0, 0, LATE), "sleepy")
        self.assertEqual(mood(0, 0, 0, LATE), "happy")
        self.assertEqual(mood(2, 0, 0, NOON), "normal")

    def test_next_is_cut_to_20_chars(self):
        s = short("Send A-kasse your working hours")
        self.assertEqual(len(s), 20)
        self.assertTrue(s.endswith("…"))
        self.assertEqual(short("A-kasse hours"), "A-kasse hours")

    def test_build_matches_contract(self):
        f = build({"left": 3, "done_today": 2, "next": "A-kasse hours", "next_kind": "admin",
                   "bill": "Thu", "overdue": 1, "at": "2026-10-26T07:30:00+01:00"}, 148, NOON)
        self.assertEqual(f, {"v": 1, "at": "2026-10-26T07:30:00+01:00", "left": 3, "done": 2, "xp": 148,
                             "next": "A-kasse hours", "kind": "admin", "bill": "Thu", "mood": "hungry",
                             "event": None, "event_at": None,
                             "now_id": None, "event_ts": None})

    def test_empty_status_is_valid(self):
        f = build({}, 0, NOON)
        self.assertEqual((f["left"], f["next"], f["kind"], f["mood"]), (0, None, None, "happy"))

    def test_unknown_kind_becomes_other(self):
        self.assertEqual(build({"left": 1, "next": "x", "next_kind": "weird"}, 0, NOON)["kind"], "other")

    def test_today_adds_the_whole_list(self):
        status = {"left": 2, "next": "A-kasse hours", "next_kind": "admin",
                  "today": [{"title": "A-kasse hours", "kind": "admin", "due": None},
                            {"title": "x" * 200, "kind": "weird", "due": "2026-10-28"},
                            {"title": "  ", "kind": "other"}]}
        f = today(status, 5, NOON)
        self.assertEqual({k: v for k, v in f.items() if k != "items"}, build(status, 5, NOON))
        self.assertEqual(f["items"][0], {"id": None, "title": "A-kasse hours", "kind": "admin", "due": None})
        self.assertEqual((len(f["items"]), len(f["items"][1]["title"]), f["items"][1]["kind"]), (2, 80, "other"))
        self.assertEqual(today({}, 0, NOON)["items"], [])

    def test_next_event(self):
        cph = timezone(timedelta(hours=1))
        now = datetime(2026, 10, 26, 12, 0, tzinfo=cph)  # a Monday
        events = [{"title": "Earlier", "start": "2026-10-26T11:00+01:00"},
                  {"title": "MSc lecture: Research methods", "start": "2026-10-26T13:00:00+00:00"},
                  {"title": "Dentist", "start": "2026-10-27T09:00+01:00"}]
        lecture_ts = int(datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(next_event(events, now), ("MSc lecture: Resear…", "14:00", lecture_ts))
        self.assertEqual(next_event(events[2:], now)[:2], ("Dentist", "Tue 09:00"))
        self.assertEqual(next_event(events[2:], now - timedelta(hours=4)), (None, None, None))  # over 24 h away
        self.assertEqual(next_event([{"title": "bad"}, {"start": "nope"}], now), (None, None, None))
        f = build({"events": events}, 0, now)
        self.assertEqual((f["event"], f["event_at"], f["event_ts"]), ("MSc lecture: Resear…", "14:00", lecture_ts))

    def test_done_and_skip_before_things_catches_up(self):
        rows = [{"id": "A", "title": "Danish homework", "kind": "danish"},
                {"id": "B", "title": "Pay phone bill", "kind": "bill"},
                {"id": "C", "title": "Ring dentist", "kind": "admin"}]
        status = {"left": 3, "done_today": 1, "next": "Danish homework", "next_kind": "danish", "next_id": "A",
                  "today": rows}
        f = build(status, 10, NOON)
        self.assertEqual((f["next"], f["now_id"], f["left"]), ("Danish homework", "A", 3))
        f = build(status, 10, NOON, {"done": ["A"], "skip": ["B"]})
        self.assertEqual((f["next"], f["now_id"], f["kind"], f["left"], f["done"]), ("Ring dentist", "C", "admin", 2, 2))
        t = today(status, 10, NOON, {"done": [], "skip": ["A", "B"]})
        self.assertEqual([i["id"] for i in t["items"]], ["C", "A", "B"])
        self.assertEqual(t["now_id"], "C")
        f = build(status, 10, NOON, {"done": ["A", "B", "C"], "skip": []})
        self.assertEqual((f["next"], f["now_id"], f["left"], f["mood"]), (None, None, 0, "happy"))
        self.assertEqual(build({"left": 1, "next": "Old agent"}, 0, NOON, {"done": ["A"], "skip": []})["now_id"], None)


if __name__ == "__main__":
    unittest.main()
