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
       "ONEBOX_DB": ":memory:"}
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


if __name__ == "__main__":
    unittest.main()
