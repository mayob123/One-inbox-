import io
import json
import unittest
import urllib.error

from onebox.pihole import PiHole

SUMMARY = {"queries": {"total": 12000, "blocked": 1500, "percent_blocked": 12.512},
           "clients": {"active": 6}, "gravity": {"domains_being_blocked": 180000}}


class FakePiHole:
    """Answers like Pi-hole v6; with a password set it wants a session first."""

    def __init__(self, password=""):
        self.password, self.logins, self.calls = password, 0, []

    def __call__(self, req, timeout):
        path = req.full_url.split("127.0.0.1", 1)[1]
        self.calls.append(path)
        if path == "/api/auth":
            self.logins += 1
            ok = json.loads(req.data)["password"] == self.password
            return io.BytesIO(json.dumps({"session": {"valid": ok, "sid": "s1" if ok else None}}).encode())
        if self.password and req.get_header("X-ftl-sid") != "s1":
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        body = {"/api/stats/summary": SUMMARY, "/api/dns/blocking": {"blocking": "enabled"},
                "/api/stats/top_domains?blocked=true&count=5": {"domains": [{"domain": "ads.example", "count": 90}]}}[path]
        return io.BytesIO(json.dumps(body).encode())


class PiHoleTests(unittest.TestCase):
    def test_summary(self):
        p = PiHole(opener=FakePiHole())
        s = p.summary(now=100)
        self.assertEqual((s["status"], s["queries"], s["blocked"], s["percent"], s["clients"]),
                         ("enabled", 12000, 1500, 12.5, 6))
        self.assertEqual(s["top_blocked"], [{"domain": "ads.example", "count": 90}])

    def test_logs_in_once_and_caches(self):
        fake = FakePiHole(password="app-pass")
        p = PiHole(password="app-pass", opener=fake)
        p.summary(now=100)
        n = len(fake.calls)
        self.assertEqual(p.summary(now=130)["queries"], 12000)
        self.assertEqual(len(fake.calls), n)  # cached for a minute
        p.summary(now=200)
        self.assertEqual(fake.logins, 1)  # the session is reused

    def test_wrong_password_and_no_pihole(self):
        locked = PiHole(password="nope", opener=FakePiHole("app-pass")).summary(now=1)
        self.assertEqual((locked["status"], locked["reason"]), ("locked", "HTTP 401"))

        def down(req, timeout):
            raise urllib.error.URLError("refused")
        self.assertEqual(PiHole(opener=down).summary(now=1), {"status": "unreachable", "reason": "URLError", "at": 1})

    def test_reason_carries_pihole_error_key(self):
        def busy(req, timeout):
            body = io.BytesIO(json.dumps({"error": {"key": "api_seats_exceeded"}}).encode())
            raise urllib.error.HTTPError(req.full_url, 429, "too many", {}, body)
        self.assertEqual(PiHole(opener=busy).summary(now=1)["reason"], "HTTP 429 api_seats_exceeded")


if __name__ == "__main__":
    unittest.main()
