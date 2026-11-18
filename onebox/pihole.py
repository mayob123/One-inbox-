"""Pi-hole's numbers for Pocket Pal's PC: read from Pi-hole v6's API on this Pi, so the phone never holds
a Pi-hole password. Config: PIHOLE_URL (default http://127.0.0.1) and PIHOLE_PASSWORD (an app password
from Pi-hole's web page, Settings > Web interface / API; leave it unset if the API has no password)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

CACHE_SECONDS = 60
TIMEOUT = 3


class PiHole:
    def __init__(self, url: str = "http://127.0.0.1", password: str = "", opener=urllib.request.urlopen):
        self.url = url.rstrip("/")
        self.password = password
        self._open = opener
        self._sid = ""
        self._cached: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self._sid:
            req.add_header("X-FTL-SID", self._sid)
        with self._open(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")

    def _login(self) -> None:
        """One session, kept and reused: Pi-hole only allows a few at once."""
        self._sid = ""
        if self.password:
            session = self._call("POST", "/api/auth", {"password": self.password}).get("session") or {}
            self._sid = session.get("sid") or ""

    def _get(self, path: str) -> dict:
        try:
            return self._call("GET", path)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self._login()
            return self._call("GET", path)

    def summary(self, now: float | None = None) -> dict:
        """{status, queries, blocked, percent, clients, domains_blocked, top_blocked}, cached for a minute.
        status is enabled, disabled, locked (needs PIHOLE_PASSWORD) or unreachable."""
        now = time.time() if now is None else now
        with self._lock:
            if self._cached and now - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
            try:
                stats = self._get("/api/stats/summary")
                blocking = self._get("/api/dns/blocking").get("blocking", "unknown")
                top = self._get("/api/stats/top_domains?blocked=true&count=5").get("domains") or []
                q = stats.get("queries") or {}
                out = {
                    "status": blocking if blocking in ("enabled", "disabled") else "unknown",
                    "queries": int(q.get("total") or 0),
                    "blocked": int(q.get("blocked") or 0),
                    "percent": round(float(q.get("percent_blocked") or 0), 1),
                    "clients": int((stats.get("clients") or {}).get("active") or 0),
                    "domains_blocked": int((stats.get("gravity") or {}).get("domains_being_blocked") or 0),
                    "top_blocked": [{"domain": str(d.get("domain", ""))[:60], "count": int(d.get("count") or 0)}
                                    for d in top[:5]],
                }
            except urllib.error.HTTPError as e:
                out = {"status": "locked" if e.code in (401, 403) else "unreachable"}
            except (OSError, ValueError):
                out = {"status": "unreachable"}
            out["at"] = int(now)
            self._cached = (now, out)
            return out
