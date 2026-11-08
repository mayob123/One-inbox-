import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

from onebox import agent, friends, picalendar
from onebox.server import Config, make_handler
from onebox.store import Store

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 11, 8, 18, 0, tzinfo=TZ)  # a Sunday
ENV = {"ONEBOX_UI_KEY": "u" * 20, "ONEBOX_AGENT_TOKEN": "a" * 20, "ONEBOX_WATCH_TOKEN": "w" * 20,
       "ONEBOX_CAPTURE_TOKEN": "c" * 20, "ONEBOX_DB": ":memory:"}


class FriendsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        friends.ensure(self.store)

    def add(self, name, every=14, **kw):
        return friends.add(self.store, friends.clean({"name": name, "every_days": every, **kw}, new=True))

    def test_clean_checks_what_the_phone_sends(self):
        self.assertEqual(friends.clean({"name": " Sara ", "handles": "+45 1234, sara@x.dk"}, new=True),
                         {"name": "Sara", "every_days": 21, "handles": ["+45 1234", "sara@x.dk"], "also": []})
        for bad in ({"name": ""}, {"name": "x" * 61}, {"name": "Sara", "every_days": 1},
                    {"name": "Sara", "every_days": "often"}, {"name": "Sara", "handles": list("abcdefg")}):
            with self.assertRaises(ValueError):
                friends.clean(bad, new=True)
        self.assertEqual(friends.clean({"every_days": 30}, new=False), {"every_days": 30})

    def test_never_logged_friends_come_first_and_are_ready(self):
        a = self.add("Anna")
        b = self.add("Ben B")
        friends.log(self.store, a["id"], "saw", NOW - timedelta(days=2))
        got = friends.listing(self.store, NOW)
        self.assertEqual([f["name"] for f in got["friends"]], ["Ben B", "Anna"])
        self.assertTrue(got["friends"][0]["ready"])
        self.assertIsNone(got["friends"][0]["days"])
        anna = got["friends"][1]
        self.assertEqual((anna["days"], anna["ago"], anna["ready"], anna["last_how"]), (2, "2 days ago", False, "saw"))
        self.assertEqual(got["ready"], 1)
        self.assertEqual(got["caught_up_week"], 1)

    def test_longest_past_their_rhythm_first(self):
        tom = self.add("Tom", every=7)    # 14 days: twice his rhythm
        eva = self.add("Eva", every=28)   # 35 days: 1.25 times
        friends.log(self.store, tom["id"], "spoke", NOW - timedelta(days=14))
        friends.log(self.store, eva["id"], "saw", NOW - timedelta(days=35))
        got = friends.listing(self.store, NOW)["friends"]
        self.assertEqual([f["name"] for f in got], ["Tom", "Eva"])
        self.assertEqual(got[0]["ago"], "2 weeks ago")
        self.assertEqual(got[0]["last_spoke"][:10], (NOW - timedelta(days=14)).astimezone(friends.timezone.utc).isoformat()[:10])

    def test_undo_takes_back_the_last_tap_only(self):
        f = self.add("Sara")
        friends.log(self.store, f["id"], "saw", NOW - timedelta(days=20))
        friends.log(self.store, f["id"], "spoke", NOW - timedelta(days=1))
        self.assertTrue(friends.undo(self.store, f["id"]))
        self.assertEqual(friends.listing(self.store, NOW)["friends"][0]["days"], 20)
        self.assertTrue(friends.undo(self.store, f["id"]))
        self.assertFalse(friends.undo(self.store, f["id"]))

    def test_calendar_past_counts_future_is_a_plan_and_cancelled_plans_go(self):
        sara = self.add("Sara", also=["Sars"])
        self.add("Al")  # "Al" mustn't match "Almost"
        events = [{"title": "Dinner with Sara", "start": (NOW - timedelta(days=3)).isoformat()},
                  {"title": "Coffee w/ sars", "start": (NOW + timedelta(days=4)).isoformat()},
                  {"title": "Almost forgot: dentist", "start": (NOW - timedelta(days=1)).isoformat()}]
        win = (NOW - timedelta(days=30), NOW + timedelta(days=30))
        self.assertEqual(friends.match_events(self.store, events, NOW, "mac", win), 2)
        got = {f["name"]: f for f in friends.listing(self.store, NOW)["friends"]}
        self.assertEqual(got["Sara"]["days"], 3)
        self.assertEqual(got["Sara"]["last_how"], "calendar")
        self.assertEqual(got["Sara"]["plan"]["title"], "Coffee w/ sars")
        self.assertFalse(got["Sara"]["ready"])
        self.assertIsNone(got["Al"]["days"])
        # the coffee's cancelled: the next sync no longer sends it
        friends.match_events(self.store, events[:1], NOW, "mac", win)
        got = {f["name"]: f for f in friends.listing(self.store, NOW)["friends"]}
        self.assertIsNone(got["Sara"]["plan"])
        self.assertEqual(got["Sara"]["days"], 3)  # the dinner that happened stays
        # and a plan whose day has come counts as seeing them
        friends.match_events(self.store, events, NOW, "mac", win)
        later = NOW + timedelta(days=5)
        got = {f["name"]: f for f in friends.listing(self.store, later)["friends"]}
        self.assertEqual(got["Sara"]["days"], 1)
        self.assertEqual(sara["name"], "Sara")

    def test_messages_keep_the_newest(self):
        f = self.add("Tom")
        friends.record_messages(self.store, [{"id": f["id"], "at": (NOW - timedelta(days=9)).isoformat()}])
        friends.record_messages(self.store, [{"id": f["id"], "at": (NOW - timedelta(days=2)).isoformat()},
                                             {"id": "nobody", "at": NOW.isoformat()}])
        got = friends.listing(self.store, NOW)["friends"][0]
        self.assertEqual((got["days"], got["last_how"]), (2, "message"))
        with self.store.lock:
            n = self.store.db.execute("SELECT COUNT(*) FROM friend_contacts WHERE how='message'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_seed_only_when_empty(self):
        self.assertEqual(friends.seed(self.store, [{"name": "Tom", "every_days": 21, "handles": ["+45"]},
                                                   {"name": ""}]), 1)
        self.assertEqual(friends.seed(self.store, [{"name": "Eva"}]), 0)

    def test_with_friends_counts_catch_ups_toward_the_pillar(self):
        status = {"friends_overdue": 3, "pillars": {"friend": {"done_week": 1, "open": 2}}}
        self.assertIs(friends.with_friends(status, self.store, NOW), status)  # no list yet: the Mac's count stands
        a, b = self.add("Anna"), self.add("Bo")
        friends.log(self.store, a["id"], "saw", NOW - timedelta(days=1))
        friends.log(self.store, a["id"], "spoke", NOW - timedelta(hours=1))
        friends.log(self.store, b["id"], "saw", NOW - timedelta(days=10))  # last week
        got = friends.with_friends(status, self.store, NOW)
        self.assertEqual(got["friends_overdue"], 0)
        self.assertEqual(got["pillars"]["friend"]["done_week"], 1 + 2)  # Anna on two days this week
        self.assertEqual(got["pillars"]["friend"]["open"], 2)

    def test_pi_calendar_feed(self):
        self.add("Sara")
        start = (NOW - timedelta(days=2)).astimezone(friends.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ics = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Run with Sara\nDTSTART:{start}\nEND:VEVENT\nEND:VCALENDAR\n"
        got = picalendar.sync(self.store, {"friend_calendars": [{"name": "iCloud", "url": "x"}]}, NOW,
                              fetcher=lambda url: ics)
        self.assertEqual(got, {})
        self.assertEqual(friends.listing(self.store, NOW)["friends"][0]["days"], 2)
        self.assertEqual(self.store.get_meta("friends_calendar_sync")["matched"], 1)

        def boom(url):
            raise OSError("down")
        self.assertEqual(friends.sync_calendars(self.store, [{"url": "x"}], NOW, boom)["errors"], ["calendar: down"])
        self.assertEqual(friends.listing(self.store, NOW)["friends"][0]["days"], 2)


class FriendsServerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Config(ENV), self.store))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, token="c"):
        h = {"Content-Type": "application/json"}
        if token == "w":
            h["X-OneInbox-Token"] = "w" * 20
        elif token:
            h["Authorization"] = "Bearer " + token * 20
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_phone_adds_taps_edits_and_removes(self):
        self.assertEqual(self.req("GET", "/api/friends", token=None)[0], 401)
        self.assertEqual(self.req("POST", "/api/friends", {"name": "Sara"}, token="w")[0], 401)
        code, got = self.req("POST", "/api/friends", {"name": "Sara", "every_days": 14})
        self.assertEqual(code, 201)
        fid = got["friend"]["id"]
        self.assertEqual(self.req("GET", "/api/friends", token="w")[1]["ready"], 1)
        code, got = self.req("POST", f"/api/friends/{fid}/saw")
        self.assertEqual((code, got["friends"][0]["ago"], got["ready"]), (200, "today", 0))
        code, got = self.req("POST", f"/api/friends/{fid}/undo")
        self.assertEqual(got["friends"][0]["days"], None)
        day = (datetime.now(TZ).date() - timedelta(days=3)).isoformat()
        code, got = self.req("POST", f"/api/friends/{fid}/spoke", {"on": day})
        self.assertEqual(got["friends"][0]["days"], 3)
        self.assertEqual(self.req("POST", f"/api/friends/{fid}/spoke", {"on": "2020-01-01"})[0], 400)
        code, got = self.req("POST", f"/api/friends/{fid}", {"every_days": 60, "also": ["Sars"]})
        self.assertEqual((got["friends"][0]["every_days"], got["friends"][0]["also"]), (60, ["Sars"]))
        self.assertEqual(self.req("POST", f"/api/friends/{fid}", {"every_days": 0})[0], 400)
        self.assertEqual(self.req("POST", f"/api/friends/{fid}/remove")[1]["friends"], [])
        self.assertEqual(self.req("POST", f"/api/friends/{fid}/saw")[0], 404)

    def test_agent_seeds_and_sends_messages_and_events(self):
        code, got = self.req("POST", "/api/agent/friends", {"seed": [{"name": "Tom", "handles": ["+4511"]}]}, token="a")
        self.assertEqual((code, got["seeded"], got["friends"][0]["handles"]), (200, 1, ["+4511"]))
        fid = got["friends"][0]["id"]
        now = datetime.now(TZ)
        code, got = self.req("POST", "/api/agent/friends", {
            "messages": [{"id": fid, "at": (now - timedelta(days=5)).isoformat()}],
            "events": [{"title": "Beers with Tom", "start": (now + timedelta(days=2)).isoformat()}],
            "window": {"from": (now - timedelta(days=30)).isoformat(), "to": (now + timedelta(days=30)).isoformat()},
        }, token="a")
        self.assertEqual((got["messages"], got["events"]), (1, 1))
        f = self.req("GET", "/api/friends", token="w")[1]["friends"][0]
        self.assertEqual((f["days"], f["plan"]["title"], f["ready"]), (5, "Beers with Tom", False))
        self.assertEqual(self.req("POST", "/api/agent/friends", {}, token="c")[0], 401)


class AgentFriendsTests(unittest.TestCase):
    def test_friend_messages_reads_the_newest_either_way(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "chat.db")
            con = sqlite3.connect(db)
            con.executescript("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);"
                              "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER, date INTEGER);")
            con.execute("INSERT INTO handle VALUES (1, '+4511'), (2, 'tom@x.dk')")
            when = (NOW - timedelta(days=4) - agent.APPLE_EPOCH).total_seconds()
            con.execute("INSERT INTO message VALUES (1, 1, ?), (2, 2, ?)", (int(when * 1e9) - 10**12, int(when * 1e9)))
            con.commit()
            con.close()
            got = agent.friend_messages(db, [{"id": "a", "handles": ["+4511", "tom@x.dk"]},
                                             {"id": "b", "handles": []}])
            self.assertEqual([g["id"] for g in got], ["a"])
            self.assertEqual(datetime.fromisoformat(got[0]["at"]).date(), (NOW - timedelta(days=4)).astimezone(agent.APPLE_EPOCH.tzinfo).date())


if __name__ == "__main__":
    unittest.main()
