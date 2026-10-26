import unittest
from datetime import datetime

from onebox.feed import build, mood, short, today

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
                             "next": "A-kasse hours", "kind": "admin", "bill": "Thu", "mood": "hungry"})

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
        self.assertEqual(f["items"][0], {"title": "A-kasse hours", "kind": "admin", "due": None})
        self.assertEqual((len(f["items"]), len(f["items"][1]["title"]), f["items"][1]["kind"]), (2, 80, "other"))
        self.assertEqual(today({}, 0, NOON)["items"], [])


if __name__ == "__main__":
    unittest.main()
