import json
from datetime import datetime, timedelta
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from onebox.server import Config, make_handler
from onebox.store import Store

ENV = {"ONEBOX_UI_KEY": "u" * 20, "ONEBOX_AGENT_TOKEN": "a" * 20, "ONEBOX_WATCH_TOKEN": "w" * 20,
       "ONEBOX_CAPTURE_TOKEN": "c" * 20, "ONEBOX_DB": ":memory:"}
ITEM = {"id": "0123456789abcdef", "status": "decide", "title": "Your latest bill is here", "task": "Check and pay: Vodafone",
        "area": "Admin & bills", "kind": "bill", "source": "mac", "sender": "Vodafone",
        "received": "2026-10-22T13:18:12+00:00", "why": "No reply found.", "lang": "en", "evidence": []}


class Opener:
    """Keeps cookies and never follows redirects, like a careful browser."""

    def __init__(self):
        self.cookie = ""

    def req(self, base, method, path, body=None, headers=None, form=None):
        data = None
        h = dict(headers or {})
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        if form is not None:
            data, h["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        if self.cookie:
            h["Cookie"] = self.cookie
        r = urllib.request.Request(base + path, data=data, method=method, headers=h)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        try:
            resp = urllib.request.build_opener(NoRedirect).open(r)
            code, raw, hdrs = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            code, raw, hdrs = e.code, e.read(), e.headers
        if hdrs.get("Set-Cookie"):
            self.cookie = hdrs["Set-Cookie"].split(";")[0]
        return code, raw


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Config(ENV), self.store))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.web = Opener()
        self.agent = {"Authorization": "Bearer " + "a" * 20}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()

    def ingest(self, **extra):
        return self.web.req(self.base, "POST", "/api/ingest", {"items": [ITEM], **extra}, self.agent)

    def test_config_rejects_short_tokens(self):
        with self.assertRaises(SystemExit):
            Config({"ONEBOX_UI_KEY": "short"})

    def test_ui_needs_key(self):
        self.assertEqual(self.web.req(self.base, "GET", "/api/items")[0], 401)
        code, body = self.web.req(self.base, "GET", "/")
        self.assertIn(b"Your key", body)
        self.assertEqual(self.web.req(self.base, "POST", "/login", form={"key": "wrong"})[0], 401)
        self.assertEqual(self.web.req(self.base, "POST", "/login", form={"key": "u" * 20})[0], 303)
        self.assertEqual(self.web.req(self.base, "POST", "/login", form={"key": " ONEBOX_UI_KEY=" + "u" * 20 + " "})[0], 303)
        self.assertEqual(self.web.req(self.base, "GET", "/api/items")[0], 200)

    def test_agent_token_required(self):
        code, _ = self.web.req(self.base, "POST", "/api/ingest", {"items": [ITEM]}, {"Authorization": "Bearer nope"})
        self.assertEqual(code, 401)

    def test_decide_add_flows_to_outbox_once(self):
        self.ingest()
        self.web.req(self.base, "POST", "/login", form={"key": "u" * 20})
        code, _ = self.web.req(self.base, "POST", f"/api/items/{ITEM['id']}/decision", {"decision": "add"})
        self.assertEqual(code, 200)
        out = json.loads(self.web.req(self.base, "GET", "/api/outbox", headers=self.agent)[1])["items"]
        self.assertEqual([i["id"] for i in out], [ITEM["id"]])
        self.web.req(self.base, "POST", "/api/outbox/ack", {"ids": [ITEM["id"]]}, self.agent)
        out = json.loads(self.web.req(self.base, "GET", "/api/outbox", headers=self.agent)[1])["items"]
        self.assertEqual(out, [])
        # Once in Things it can't be changed from the page.
        code, _ = self.web.req(self.base, "POST", f"/api/items/{ITEM['id']}/decision", {"decision": "skip"})
        self.assertEqual(code, 409)

    def test_reingest_keeps_decision(self):
        self.ingest()
        self.store.decide(ITEM["id"], "later")
        self.ingest()
        self.assertEqual(self.store.items()[0]["decision"], "later")

    def test_no_action_needed_learns_the_sender(self):
        newer = {**ITEM, "id": "fedcba9876543210", "from_addr": "billing@vodafone.dk", "title": "Next bill"}
        first = {**ITEM, "from_addr": "billing@vodafone.dk"}
        self.web.req(self.base, "POST", "/api/ingest", {"items": [first]}, self.agent)
        self.web.req(self.base, "POST", "/login", form={"key": "u" * 20})
        self.web.req(self.base, "POST", f"/api/items/{ITEM['id']}/decision", {"decision": "none"})
        self.web.req(self.base, "POST", "/api/ingest", {"items": [first, newer]}, self.agent)
        self.assertEqual({i["status"] for i in self.store.items()}, {"noise"})
        code, _ = self.web.req(self.base, "POST", "/api/quiet/remove", {"sender": "billing@vodafone.dk"})
        self.assertEqual(code, 200)
        self.assertEqual({i["status"] for i in self.store.items()}, {"decide"})

    def test_bad_decision_is_400(self):
        self.ingest()
        self.web.req(self.base, "POST", "/login", form={"key": "u" * 20})
        code, _ = self.web.req(self.base, "POST", f"/api/items/{ITEM['id']}/decision", {"decision": "delete"})
        self.assertEqual(code, 400)

    def test_watch_feed_and_xp(self):
        self.assertEqual(self.web.req(self.base, "GET", "/feed/watch.json")[0], 401)
        status = {"left": 2, "done_today": 1, "next": "Danish homework, chapter 4", "next_kind": "danish",
                  "completed_ids": ["t1", "t2"], "at": "2026-10-26T07:30:00+01:00"}
        self.ingest(things=status)
        self.ingest(things=dict(status, completed_ids=["t2", "t3"]))
        code, raw = self.web.req(self.base, "GET", "/feed/watch.json", headers={"X-OneInbox-Token": "w" * 20})
        f = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual((f["v"], f["left"], f["done"], f["xp"], f["kind"]), (1, 2, 1, 3, "danish"))
        self.assertLessEqual(len(f["next"]), 20)
        self.assertLess(len(raw), 1024)

    def test_pihole_needs_a_token(self):
        self.assertEqual(self.web.req(self.base, "GET", "/api/pihole")[0], 401)
        code, raw = self.web.req(self.base, "GET", "/api/pihole", headers={"X-OneInbox-Token": "w" * 20})
        self.assertEqual(code, 200)
        self.assertIn(json.loads(raw)["status"], ("enabled", "disabled", "locked", "unreachable", "unknown"))

    def test_today_feed_lists_every_item(self):
        self.assertEqual(self.web.req(self.base, "GET", "/feed/today.json")[0], 401)
        items = [{"title": f"Item {i}", "kind": "admin", "due": None} for i in range(12)]
        self.ingest(things={"left": 12, "next": "Item 0", "next_kind": "admin", "today": items,
                            "at": "2026-10-26T07:30:00+01:00"})
        code, raw = self.web.req(self.base, "GET", "/feed/today.json", headers={"X-OneInbox-Token": "w" * 20})
        self.assertEqual(code, 200)
        self.assertEqual([i["title"] for i in json.loads(raw)["items"]], [f"Item {i}" for i in range(12)])
        _, watch = self.web.req(self.base, "GET", "/feed/watch.json", headers={"X-OneInbox-Token": "w" * 20})
        self.assertNotIn("items", json.loads(watch))

    def outbox(self):
        return json.loads(self.web.req(self.base, "GET", "/api/outbox", headers=self.agent)[1])["items"]

    def test_auto_items_are_queued_once_and_can_be_unticked(self):
        auto = dict(ITEM, id="fedcba9876543210", title="Ny besked", task="Read in Aula: Ny besked", auto="things")
        self.web.req(self.base, "POST", "/api/ingest", {"items": [auto]}, self.agent)
        self.assertEqual([i["id"] for i in self.outbox()], [auto["id"]])
        self.store.decide(auto["id"], None)  # Ben unticks it on the page
        self.web.req(self.base, "POST", "/api/ingest", {"items": [auto]}, self.agent)
        self.assertEqual(self.outbox(), [])

    def test_quiet_sender_is_not_auto_added(self):
        first = dict(ITEM, id="1111111111111111", from_addr="noreply@aula.dk", auto="things")
        self.web.req(self.base, "POST", "/api/ingest", {"items": [first]}, self.agent)
        self.store.decide(first["id"], "none")  # "No action needed" remembers the sender
        later = dict(first, id="2222222222222222")
        self.web.req(self.base, "POST", "/api/ingest", {"items": [later]}, self.agent)
        self.assertEqual(self.outbox(), [])

    def test_capture_needs_ui_cookie_or_capture_token(self):
        self.assertEqual(self.web.req(self.base, "POST", "/api/capture", {"text": "Buy milk"})[0], 401)
        self.assertEqual(self.web.req(self.base, "POST", "/api/capture", {"text": "Buy milk"},
                                      {"Authorization": "Bearer " + "w" * 20})[0], 401)
        code, raw = self.web.req(self.base, "POST", "/api/capture", {"text": "Ring the tandlæge"},
                                 {"Authorization": "Bearer " + "c" * 20})
        self.assertEqual(code, 200)
        self.web.req(self.base, "POST", "/login", form={"key": "u" * 20})
        self.assertEqual(self.web.req(self.base, "POST", "/api/capture", {"text": "Buy milk"})[0], 200)
        self.assertEqual(self.web.req(self.base, "POST", "/api/capture", {"text": "  "})[0], 400)
        self.assertEqual(self.web.req(self.base, "POST", "/api/capture", {"text": "x" * 501})[0], 400)
        out = {i["task"]: i for i in self.outbox()}
        self.assertEqual(set(out), {"Ring the tandlæge", "Buy milk"})
        self.assertEqual(out["Ring the tandlæge"]["area"], "Admin & bills")

    def test_capture_token_is_optional(self):
        Config({k: v for k, v in ENV.items() if k != "ONEBOX_CAPTURE_TOKEN"})
        with self.assertRaises(SystemExit):
            Config(dict(ENV, ONEBOX_CAPTURE_TOKEN="short"))

    def test_todo_done_and_skip(self):
        W = {"X-OneInbox-Token": "w" * 20}
        rows = [{"id": "uuidA", "title": "Danish homework", "kind": "danish", "due": None},
                {"id": "uuidB", "title": "Pay phone bill", "kind": "bill", "due": None},
                {"id": "uuidC", "title": "Ring dentist", "kind": "admin", "due": None}]
        self.ingest(things={"left": 3, "done_today": 0, "next": "Danish homework", "next_kind": "danish",
                            "next_id": "uuidA", "today": rows, "completed_ids": []})
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/done", {"id": "uuidA"})[0], 401)
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/done", {"id": "nope"}, W)[0], 404)
        code, raw = self.web.req(self.base, "POST", "/api/todo/done", {"id": "uuidA"}, W)
        self.assertEqual(code, 200)
        f = json.loads(raw)
        self.assertEqual((f["now_id"], f["left"], f["done"], f["xp"]), ("uuidB", 2, 1, 1))
        # The phone skips B with its capture token.
        self.web.req(self.base, "POST", "/api/todo/skip", {"id": "uuidB"}, {"Authorization": "Bearer " + "c" * 20})
        f = json.loads(self.web.req(self.base, "GET", "/feed/watch.json", headers=W)[1])
        self.assertEqual((f["next"], f["now_id"]), ("Ring dentist", "uuidC"))
        items = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])["items"]
        self.assertEqual([i["id"] for i in items], ["uuidC", "uuidB"])
        # The Mac completes A in Things once, then Things reports it done: XP stays 1.
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["ids"], ["uuidA"])
        self.web.req(self.base, "POST", "/api/todo/ack", {"ids": ["uuidA"]}, self.agent)
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["ids"], [])
        self.ingest(things={"left": 2, "done_today": 1, "next": "Pay phone bill", "next_kind": "bill",
                            "next_id": "uuidB", "today": rows[1:], "completed_ids": ["uuidA"]})
        f = json.loads(self.web.req(self.base, "GET", "/feed/watch.json", headers=W)[1])
        self.assertEqual((f["now_id"], f["left"], f["done"], f["xp"]), ("uuidC", 2, 1, 1))

    def test_todo_later_leaves_today_and_comes_back(self):
        W = {"X-OneInbox-Token": "w" * 20}
        C = {"Authorization": "Bearer " + "c" * 20}
        rows = [{"id": "uuidA", "title": "Danish homework", "kind": "danish", "due": None},
                {"id": "uuidB", "title": "Pay phone bill", "kind": "bill", "due": None}]
        later = [{"id": "uuidL", "title": "Quizlet deck", "kind": "danish", "when": None, "due": None}]
        self.ingest(things={"left": 2, "done_today": 0, "next": "Danish homework", "next_kind": "danish",
                            "next_id": "uuidA", "today": rows, "later": later, "completed_ids": []})
        day = datetime.now(Config(ENV).tz).date()
        tomorrow = (day + timedelta(days=1)).isoformat()
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/later", {"id": "uuidA", "when": tomorrow})[0], 401)
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/later", {"id": "uuidA"}, C)[0], 400)
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/later",
                                      {"id": "uuidA", "when": day.isoformat()}, C)[0], 400)
        code, raw = self.web.req(self.base, "POST", "/api/todo/later", {"id": "uuidA", "when": tomorrow}, C)
        self.assertEqual(code, 200)
        self.assertEqual((json.loads(raw)["now_id"], json.loads(raw)["left"], json.loads(raw)["done"]), ("uuidB", 1, 0))
        f = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])
        self.assertEqual([i["id"] for i in f["items"]], ["uuidB"])
        self.assertEqual([(r["id"], r.get("when")) for r in f["later"]], [("uuidA", tomorrow), ("uuidL", None)])
        # An item already outside Today can be pushed to a day too.
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/later", {"id": "uuidL", "when": tomorrow}, C)[0], 200)
        box = json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])
        self.assertEqual((box["ids"], box["later"]), ([], [{"id": "uuidA", "when": tomorrow}, {"id": "uuidL", "when": tomorrow}]))
        self.web.req(self.base, "POST", "/api/todo/ack", {"ids": ["uuidA", "uuidL"]}, self.agent)
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["later"], [])

    def test_tasks_mirror_things_and_hold_app_todos(self):
        C = {"Authorization": "Bearer " + "c" * 20}
        day = datetime.now(Config(ENV).tz).date()
        tomorrow = (day + timedelta(days=1)).isoformat()
        everything = [
            {"id": "A", "title": "Danish homework", "kind": "danish", "list": "today", "when": day.isoformat()},
            {"id": "B", "title": "Quizlet deck", "kind": "danish", "list": "anytime", "tags": ["Danish"]},
            {"id": "C", "title": "Learn Python", "kind": "msc", "list": "someday"}]
        self.ingest(things={"left": 1, "today": everything[:1], "all": everything, "completed_ids": []})
        self.assertNotIn("all", self.store.get_meta("things_status"))  # kept in its own table, not the meta blob
        self.assertEqual(self.web.req(self.base, "GET", "/api/tasks")[0], 401)
        tasks = json.loads(self.web.req(self.base, "GET", "/api/tasks", headers=C)[1])["tasks"]
        self.assertEqual([(t["id"], t["source"], t.get("list")) for t in tasks],
                         [("A", "things", "today"), ("B", "things", "anytime"), ("C", "things", "someday")])
        self.assertEqual(tasks[1]["tags"], ["Danish"])
        # A to-do made in the app lives on the Pi only.
        code, raw = self.web.req(self.base, "POST", "/api/tasks", {"title": "Book PD3 exam", "kind": "danish",
                                                                    "when": "tomorrow"}, C)
        self.assertEqual(code, 201)
        new = json.loads(raw)["task"]
        self.assertEqual((new["source"], new["when"], new["list"]), ("app", tomorrow, "upcoming"))
        errand = json.loads(self.web.req(self.base, "POST", "/api/tasks", {
            "title": "Buy bulbs", "kind": "admin", "list": "anytime", "tags": ["Home admin", "Errands"],
            "project": "Around the house"}, C)[1])["task"]
        self.assertEqual((errand["tags"], errand["project"]), (["Home admin", "Errands"], "Around the house"))
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks", {"title": "x", "tags": "Errands"}, C)[0], 400)
        self.web.req(self.base, "POST", f"/api/tasks/{errand['id']}/done", {}, C)
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks", {"title": " "}, C)[0], 400)
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks", {"title": "x", "kind": "nope"}, C)[0], 400)
        self.assertEqual(self.web.req(self.base, "POST", f"/api/tasks/{new['id']}", {"title": "Book the PD3 exam"}, C)[0], 200)
        self.assertEqual(self.store.task(new["id"])["title"], "Book the PD3 exam")
        # Done, Not today and edits on Things to-dos go through the Mac.
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks/B/later", {"when": tomorrow}, C)[0], 200)
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks/B/later", {"when": day.isoformat()}, C)[0], 400)
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks/A/done", {}, C)[0], 200)
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks/A/done", {}, C)[0], 404)
        box = json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])
        self.assertEqual((box["ids"], box["later"], box["edits"]), (["A"], [{"id": "B", "when": tomorrow}], []))
        # The next sync still shows the old Things state: the tick and the new day here win until the Mac acts.
        self.ingest(things={"left": 1, "all": everything, "completed_ids": []})
        self.assertEqual(self.store.task("A")["status"], "done")
        self.assertEqual(self.store.task("B")["when"], tomorrow)
        # The Mac did both; Things reports A completed, and C was deleted in Things.
        self.web.req(self.base, "POST", "/api/todo/ack", {"ids": ["A", "B"]}, self.agent)
        self.ingest(things={"left": 0, "all": [dict(everything[1], list="upcoming", when=tomorrow)],
                            "completed_ids": ["A"]})
        self.assertEqual([(t["id"], t["status"]) for t in json.loads(self.web.req(
            self.base, "GET", "/api/tasks?status=done", headers=C)[1])["tasks"]], [("A", "done"), (errand["id"], "done")])
        self.assertEqual(self.store.task("C")["status"], "gone")
        open_ids = [t["id"] for t in json.loads(self.web.req(self.base, "GET", "/api/tasks", headers=C)[1])["tasks"]]
        self.assertEqual(sorted(open_ids), sorted(["B", new["id"]]))
        # An older agent that sends no full list leaves the mirror alone.
        self.ingest(things={"left": 0, "completed_ids": []})
        self.assertEqual(self.store.task("B")["status"], "open")
        self.web.req(self.base, "POST", f"/api/tasks/{new['id']}/done", {}, C)
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["ids"], [])

    def test_editing_a_things_todo_goes_through_the_mac(self):
        C = {"Authorization": "Bearer " + "c" * 20}
        day = datetime.now(Config(ENV).tz).date()
        later = (day + timedelta(days=3)).isoformat()
        row = {"id": "B", "title": "Quizlet deck", "kind": "danish", "list": "anytime", "notes": "old"}
        self.ingest(things={"left": 1, "all": [row], "completed_ids": []})
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks/B",
                                      {"title": "Quizlet deck 3", "notes": "chapter 3", "kind": "msc"}, C)[0], 200)
        self.assertEqual(self.web.req(self.base, "POST", "/api/tasks/B", {"when": later, "list": "upcoming"}, C)[0], 200)
        task = self.store.task("B")
        self.assertEqual((task["title"], task["notes"], task["when"], task["kind"]),
                         ("Quizlet deck 3", "chapter 3", later, "danish"))  # the pillar stays Things' tag
        box = json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])
        [edit] = box["edits"]
        self.assertEqual({k: v for k, v in edit.items() if k != "at"},
                         {"id": "B", "title": "Quizlet deck 3", "notes": "chapter 3", "when": later})
        # Things still has the old version until the Mac acts: the edit here wins.
        self.ingest(things={"left": 1, "all": [row], "completed_ids": []})
        self.assertEqual((self.store.task("B")["title"], self.store.task("B")["when"]), ("Quizlet deck 3", later))
        # An edit made after the Mac read the outbox stays queued.
        self.web.req(self.base, "POST", "/api/tasks/B", {"title": "Quizlet deck 4"}, C)
        self.web.req(self.base, "POST", "/api/todo/ack", {"ids": [], "edits": [edit]}, self.agent)
        [again] = json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["edits"]
        self.assertEqual(again["title"], "Quizlet deck 4")
        self.web.req(self.base, "POST", "/api/todo/ack", {"ids": [], "edits": [again]}, self.agent)
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["edits"], [])
        # Once Things has it, its version is the truth again.
        self.ingest(things={"left": 1, "all": [dict(row, title="Quizlet deck 4", notes="chapter 3", list="upcoming",
                                                    when=later)], "completed_ids": []})
        self.assertEqual(self.store.task("B")["title"], "Quizlet deck 4")
        self.ingest(things={"left": 1, "all": [dict(row, title="Renamed in Things")], "completed_ids": []})
        self.assertEqual(self.store.task("B")["title"], "Renamed in Things")

    def test_today_feed_has_pillars_with_waiting_mail(self):
        self.ingest(things={"left": 0, "pillars": {"bill": {"done_week": 1, "open": 2}}})
        code, raw = self.web.req(self.base, "GET", "/feed/today.json", headers={"X-OneInbox-Token": "w" * 20})
        p = json.loads(raw)["pillars"]
        self.assertEqual((p["bill"]["waiting_mail"], p["bill"]["open"], p["danish"]["waiting_mail"]), (1, 2, 0))
        self.store.decide(ITEM["id"], "later")
        p = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers={"X-OneInbox-Token": "w" * 20})[1])["pillars"]
        self.assertEqual(p["bill"]["waiting_mail"], 0)

    def test_phone_lists_and_decides_waiting_mail(self):
        self.ingest()
        self.assertEqual(self.web.req(self.base, "GET", "/api/waiting")[0], 401)
        code, raw = self.web.req(self.base, "GET", "/api/waiting", headers={"Authorization": "Bearer " + "c" * 20})
        self.assertEqual(code, 200)
        self.assertEqual([i["id"] for i in json.loads(raw)["items"]], [ITEM["id"]])
        path = f"/api/items/{ITEM['id']}/decision"
        self.assertEqual(self.web.req(self.base, "POST", path, {"decision": "add"},
                                      {"Authorization": "Bearer " + "w" * 20})[0], 401)  # the watch can't decide
        self.assertEqual(self.web.req(self.base, "POST", path, {"decision": "add"},
                                      {"Authorization": "Bearer " + "c" * 20})[0], 200)
        raw = self.web.req(self.base, "GET", "/api/waiting", headers={"Authorization": "Bearer " + "c" * 20})[1]
        self.assertEqual(json.loads(raw)["items"], [])
        self.assertEqual([i["id"] for i in self.outbox()], [ITEM["id"]])

    def test_official_post_is_a_mailbox(self):
        W, C = {"X-OneInbox-Token": "w" * 20}, {"Authorization": "Bearer " + "c" * 20}
        a1 = dict(ITEM, id="a000000000000001", title="Ny besked", task="Read in Aula: Ny besked", kind="school",
                  post="Aula", from_addr="noreply@aula.dk", received="2026-10-27T08:00:00+01:00")
        a2 = dict(a1, id="a000000000000002", title="Ny besked fra Lærer", received="2026-10-27T15:00:00+01:00")
        e1 = dict(a1, id="e000000000000001", title="Du har ny post", post="e-Boks", kind="admin")
        rows = [{"id": "CHK", "title": "Check Aula (2 new)", "kind": "school"}]
        self.web.req(self.base, "POST", "/api/ingest", {"items": [a1, a2, e1], "things": {"left": 1, "today": rows}},
                     self.agent)
        self.assertEqual(self.outbox(), [])  # not one to-do per alert
        t = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])
        self.assertEqual([(s["source"], s["count"]) for s in t["post"]], [("Aula", 2), ("e-Boks", 1)])
        self.assertEqual(t["pillars"]["school"]["waiting_mail"], 0)  # counted as post, not as waiting mail
        waiting = json.loads(self.web.req(self.base, "GET", "/api/waiting", headers=C)[1])["items"]
        self.assertEqual(waiting, [])
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/post", headers=self.agent)[1])["sources"][0]["count"], 2)
        # "Checked them" on the phone clears Aula and ticks the rolling to-do
        self.assertEqual(self.web.req(self.base, "POST", "/api/post/checked", {"source": "Aula"})[0], 401)
        self.assertEqual(self.web.req(self.base, "POST", "/api/post/checked", {"source": "Nope"}, C)[0], 400)
        code, raw = self.web.req(self.base, "POST", "/api/post/checked", {"source": "Aula"}, C)
        self.assertEqual((code, json.loads(raw)["checked"]), (200, 2))
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["ids"], ["CHK"])
        t = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])
        self.assertEqual([s["source"] for s in t["post"]], ["e-Boks"])
        # Ticking "Check e-Boks" in Things clears what came before it, not what came after
        e2 = dict(e1, id="e000000000000002", received="2026-10-28T09:00:00+01:00")
        self.web.req(self.base, "POST", "/api/ingest",
                     {"items": [e2], "things": {"left": 0, "post_done": {"e-Boks": "2026-10-27T20:00:00+01:00"}}}, self.agent)
        t = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])
        self.assertEqual([(s["source"], s["count"], s["items"][0]["id"]) for s in t["post"]], [("e-Boks", 1, e2["id"])])
        self.assertNotIn("post_done", self.store.get_meta("things_status"))

    def test_side_quests_can_be_ticked(self):
        W = {"X-OneInbox-Token": "w" * 20}
        self.ingest(things={"left": 0, "today": [], "pool": [{"id": "J1", "title": "Bulb", "kind": "admin"}],
                            "week": [{"id": "W1", "title": "Trip form", "kind": "school", "due": "2026-10-30"}]})
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/done", {"id": "J1"}, W)[0], 200)
        self.assertEqual(self.web.req(self.base, "POST", "/api/todo/done", {"id": "W1"}, W)[0], 200)
        t = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])
        self.assertEqual((t["pool"], t["week"], t["done"]), ([], [], 2))
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/todo/outbox", headers=self.agent)[1])["ids"],
                         ["J1", "W1"])

    def test_capture_to_anytime(self):
        code, raw = self.web.req(self.base, "POST", "/api/capture", {"text": "errand: pick up frames"},
                                 {"Authorization": "Bearer " + "c" * 20})
        self.assertEqual((code, json.loads(raw)["when"]), (200, "Anytime"))
        out = self.outbox()[0]
        self.assertEqual((out["task"], out["when"], out["tags"]), ("Pick up frames", "anytime", ["Errands"]))

    def test_capture_on_a_named_day(self):
        code, raw = self.web.req(self.base, "POST", "/api/capture", {"text": "Dentist on 2026-11-02 at 9"},
                                 {"Authorization": "Bearer " + "c" * 20})
        body = json.loads(raw)
        self.assertEqual((code, body["when"]), (200, "Mon 2 Nov at 09:00"))
        self.assertIn("for Mon 2 Nov at 09:00", body["message"])
        self.assertEqual(self.outbox()[0]["when"], "2026-11-02@09:00")

    def test_pal_round_trip(self):
        W = {"X-OneInbox-Token": "w" * 20}
        body = {"at": 1793000000, "pose": "proud", "focus": None, "line": "ALL PILLARS ON TRACK",
                "pillars": [{"key": "danish", "label": "DANISH", "done": 3, "target": 3}]}
        self.assertEqual(self.web.req(self.base, "POST", "/api/pal", body, W)[0], 401)  # the watch only reads
        code, raw = self.web.req(self.base, "POST", "/api/pal", body, {"Authorization": "Bearer " + "c" * 20})
        self.assertEqual((code, json.loads(raw)["pal_pose"]), (200, "proud"))
        self.assertEqual(json.loads(self.web.req(self.base, "GET", "/api/pal", headers=W)[1])["line"], "ALL PILLARS ON TRACK")
        code, raw = self.web.req(self.base, "GET", "/feed/watch.json", headers=W)
        f = json.loads(raw)
        self.assertEqual((f["pal_line"], f["pal_pose"], f["pal_pillars"]), ("ALL PILLARS ON TRACK", "proud", [["DANISH", 3, 3]]))
        self.assertLess(len(raw), 1024)
        t = json.loads(self.web.req(self.base, "GET", "/feed/today.json", headers=W)[1])
        self.assertIsInstance(t["pillars"], dict)  # the Pi's own per-kind pillars stay as they were
        self.assertEqual(self.web.req(self.base, "POST", "/api/pal", [1, 2], {"Authorization": "Bearer " + "c" * 20})[0], 400)


if __name__ == "__main__":
    unittest.main()
