import json
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
