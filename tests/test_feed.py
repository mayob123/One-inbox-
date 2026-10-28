import unittest
from datetime import datetime, timedelta, timezone

from onebox.feed import build, clean_pal, mood, next_event, pal_compact, post_summary, short, today

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
                             "now_id": None, "event_ts": None,
                             "pal_line": None, "pal_pose": None, "pal_pillars": None})

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
        self.assertEqual({k: v for k, v in f.items() if k not in ("items", "pillars", "week", "pool", "post")},
                         build(status, 5, NOON))
        self.assertEqual((f["week"], f["pool"], f["post"]), ([], [], []))
        self.assertEqual(f["items"][0], {"id": None, "title": "A-kasse hours", "kind": "admin", "due": None})
        self.assertEqual((len(f["items"]), len(f["items"][1]["title"]), f["items"][1]["kind"]), (2, 80, "other"))
        self.assertEqual(today({}, 0, NOON)["items"], [])

    def test_post_summary(self):
        now = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)
        def alert(i, src, days, **kw):
            return {"id": f"p{i}", "status": "decide", "post": src, "title": f"Ny besked {i}", "decision": None,
                    "received": (now - timedelta(days=days)).isoformat(), **kw}
        items = [alert(1, "Aula", 2), alert(2, "Aula", 0.5), alert(3, "e-Boks", 1),
                 alert(4, "Aula", 1, decision="done"), alert(5, "Aula", 20), alert(6, "Aula", 1, status="noise"),
                 {"id": "m", "status": "decide", "title": "Bill", "received": now.isoformat()}]
        out = post_summary(items, now)
        self.assertEqual([(s["source"], s["count"]) for s in out], [("Aula", 2), ("e-Boks", 1)])
        self.assertEqual((out[0]["since"], out[0]["latest"]), (items[0]["received"], "Ny besked 2"))
        self.assertEqual([i["id"] for i in out[0]["items"]], ["p1", "p2"])

    def test_small_jobs_and_week_in_today_feed(self):
        status = {"left": 1, "next": "A", "next_id": "A", "today": [{"id": "A", "title": "A", "kind": "admin"}],
                  "week": [{"id": "W", "title": "Trip form", "kind": "school", "due": "2026-10-30"}],
                  "pool": [{"id": "J", "title": "Bulb", "kind": "admin", "project": "Around the house",
                            "errand": False, "quick": True, "someday": False}]}
        f = today(status, 0, NOON)
        self.assertEqual(f["week"], [{"id": "W", "due": "2026-10-30", "title": "Trip form", "kind": "school"}])
        self.assertEqual(f["pool"][0]["project"], "Around the house")
        f = today(status, 0, NOON, actions={"done": ["J"], "skip": []})
        self.assertEqual((f["pool"], f["done"], f["left"]), ([], 1, 1))  # a side quest counts as done today
        self.assertEqual(f["pillars"]["admin"]["done_week"], 1)

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

    def test_pillars(self):
        status = {"left": 2, "today": [{"id": "A", "title": "Danish homework", "kind": "danish"},
                                       {"id": "B", "title": "Book Danish lesson", "kind": "danish"}],
                  "pillars": {"danish": {"done_week": 1, "open": 4, "last_done": "2026-10-20", "next_due": "2026-10-28"}}}
        p = today(status, 0, NOON, {"done": ["A"], "skip": []}, {"danish": 2, "job": 1})["pillars"]
        self.assertEqual(set(p), {"bill", "school", "admin", "msc", "job", "danish", "friend", "body", "travel", "other"})
        self.assertEqual(p["danish"], {"done_week": 2, "open": 3, "last_done": "2026-10-26", "next_due": "2026-10-28",
                                       "waiting_mail": 2})
        self.assertEqual(p["msc"], {"done_week": 0, "open": 0, "last_done": None, "next_due": None, "waiting_mail": 0})
        self.assertEqual(p["job"]["waiting_mail"], 1)

    def test_pal_state(self):
        now = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)
        pal = clean_pal({"at": 1793000000, "pose": "Happy", "focus": "danish", "line": "danish 1/3 this week, keep going",
                         "pillars": [{"key": "danish", "label": "DANISH", "done": 1, "target": 3},
                                     {"key": "msc", "label": "MSC PREP", "done": "x", "target": 2.0},
                                     {"label": "no key"}, "junk"] + [{"key": f"k{i}", "done": 1, "target": 1} for i in range(8)],
                         "extra": "dropped"}, now)
        self.assertNotIn("extra", pal)
        self.assertEqual(pal["pillars"][1], {"key": "msc", "label": "MSC PREP", "done": 0, "target": 2})
        c = pal_compact(pal, now)
        self.assertEqual(c["pal_pose"], "happy")
        self.assertEqual(len(c["pal_line"]), 24)
        self.assertEqual(c["pal_pillars"][:2], [["DANISH", 1, 3], ["MSC PR", 0, 2]])
        self.assertEqual(len(c["pal_pillars"]), 7)
        self.assertEqual(pal_compact(pal, now + timedelta(hours=37)), {"pal_line": None, "pal_pose": None, "pal_pillars": None})
        self.assertEqual(build({}, 0, now, pal=pal)["pal_pose"], "happy")
        self.assertEqual(today({}, 0, now, pal=pal)["pal_pillars"][0], ["DANISH", 1, 3])
        with self.assertRaises(ValueError):
            clean_pal(["not", "an", "object"], now)


if __name__ == "__main__":
    unittest.main()
