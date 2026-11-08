"""Mail read by the Pi itself over IMAP, so new mail reaches Pocket Pal while the Mac sleeps.

Ben wants Pocket Pal to run without the Mac (2026-11-08). The Mac agent read Mail.app's index; the Pi now logs in
to each account over IMAP with an app-specific password and runs the same triage. Settings live in a JSON file
of the Pi's own (ONEBOX_MAIL, default /etc/onebox-mail.json, readable only by root and the server):

    {"days": 21,
     "accounts": [{"label": "iCloud", "host": "imap.mail.me.com", "user": "ben@...", "password": "app-specific"}]}

Every few minutes it reads the headers of the last `days` of mail from every folder except junk, trash and drafts
(job alerts are read from Junk anyway, as on the Mac), triages them, reads the body of recent emails that need
Ben, and stores the items. Headers are kept in memory, so after the first run only new mail is downloaded.

When it takes over from the Mac, an email the Mac already sent is recognised by sender, subject and time and
keeps its old id, so it isn't listed twice and Ben's decisions on it stay.
"""

from __future__ import annotations

import email
import email.header
import email.policy
import email.utils
import imaplib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import agent
from .store import Store
from .triage import JOB_ALERT_SENDERS, Message, grade_post, normalize_subject, summary, triage, want_details

EVERY = 3 * 60
DETAIL_DAYS = 7
DETAIL_MAX = 30
LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
UID = re.compile(rb"UID (\d+)")
FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


def load(path: str) -> dict:
    """The settings file, or {} when there isn't one (then the Mac keeps reading mail)."""
    try:
        cfg = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{path} must hold a JSON object")
    accounts = [a for a in cfg.get("accounts") or [] if isinstance(a, dict) and a.get("user") and a.get("password")]
    return {"days": int(cfg.get("days") or 21), "accounts": accounts} if accounts else {}


def folder_name(raw: bytes) -> str:
    name = raw.strip()
    if name.startswith(b'"') and name.endswith(b'"'):
        name = name[1:-1].replace(b'\\"', b'"')
    return name.decode("utf-8", "replace")


def quoted(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def skip_folder(name: str, flags: bytes) -> tuple[bool, bool]:
    """(skip it, it's junk). Junk is still read for job alerts only."""
    low = name.lower()
    if b"\\Noselect" in flags or b"\\NonExistent" in flags:
        return True, False
    junk = any(s in low for s in agent.JUNK_MAILBOXES) or b"\\Junk" in flags
    if junk:
        return False, True
    return any(s in low for s in agent.SKIP_MAILBOXES) or b"\\Trash" in flags or b"\\Drafts" in flags, False


def decode(value: str | None) -> str:
    return str(email.header.make_header(email.header.decode_header(value))) if value else ""


def header_message(label: str, folder: str, uid: str, internal: bytes, flags: bytes, raw: bytes) -> Message:
    h = email.message_from_bytes(raw, policy=email.policy.compat32)
    name, addr = email.utils.parseaddr(decode(h.get("From")))
    received = email.utils.parsedate_to_datetime(internal.decode()).astimezone(timezone.utc)
    sent = "sent" in folder.lower()
    return Message(msg_id=f"imap:{label}:{folder}:{uid}", sender_name=name, sender_addr=addr,
                   subject=" ".join(decode(h.get("Subject")).split()), received=received,
                   mailbox="Sent" if sent else folder, account=label, unread=b"\\Seen" not in flags)


class Account:
    """One IMAP login and what's been read from it so far."""

    def __init__(self, cfg: dict, connect=None):
        self.cfg = cfg
        self.label = cfg.get("label") or "mail"
        self.connect = connect or (lambda: imaplib.IMAP4_SSL(cfg.get("host", "imap.mail.me.com"),
                                                             int(cfg.get("port", 993)), timeout=60))
        self.headers: dict = {}   # (folder, uidvalidity, uid) -> Message
        self.details: dict = {}   # msg_id -> details

    def read(self, days: int, now: datetime) -> tuple[list[Message], dict]:
        """Headers of the last `days` of mail, and a fetch(msg_id) to read one body on the same login."""
        imap = self.connect()
        imap.login(self.cfg["user"], self.cfg["password"])
        since = (now - timedelta(days=days)).strftime("%d-%b-%Y")
        out, where, live = [], {}, set()
        try:
            typ, lines = imap.list()
            for line in lines or []:
                m = LIST_LINE.match(line or b"")
                if not m:
                    continue
                folder = folder_name(m.group("name"))
                skip, junk = skip_folder(folder, m.group("flags"))
                if skip:
                    continue
                typ, _ = imap.select(quoted(folder), readonly=True)
                if typ != "OK":
                    continue
                validity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0]
                typ, found = imap.uid("SEARCH", None, "SINCE", since)
                uids = (found[0] or b"").split() if typ == "OK" else []
                if not uids:
                    continue
                flags = {}
                for part in imap.uid("FETCH", b",".join(uids).decode(), "(FLAGS)")[1] or []:
                    blob = part[0] if isinstance(part, tuple) else part
                    u, f = UID.search(blob or b""), FLAGS.search(blob or b"")
                    if u:
                        flags[u.group(1)] = f.group(1) if f else b""
                new = [u for u in uids if (folder, validity, u) not in self.headers]
                if new:
                    got = imap.uid("FETCH", b",".join(new).decode(),
                                   "(INTERNALDATE FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")[1]
                    for part in got or []:
                        if not isinstance(part, tuple):
                            continue
                        u, d = UID.search(part[0]), INTERNALDATE.search(part[0])
                        if not (u and d):
                            continue
                        try:
                            self.headers[(folder, validity, u.group(1))] = header_message(
                                self.label, folder, u.group(1).decode(), d.group(1), b"", part[1])
                        except (TypeError, ValueError):
                            continue
                for u in uids:
                    msg = self.headers.get((folder, validity, u))
                    if not msg:
                        continue
                    live.add((folder, u))
                    if junk and msg.sender_addr.lower() not in JOB_ALERT_SENDERS:
                        continue
                    msg.unread = b"\\Seen" not in flags.get(u, b"\\Seen")
                    out.append(msg)
                    where[msg.msg_id] = (folder, u.decode())
            # forget mail that's gone or older than `days`
            self.headers = {k: v for k, v in self.headers.items() if (k[0], k[2]) in live}
        except Exception:
            self._logout(imap)
            raise

        def body(msg_id: str) -> dict | None:
            if msg_id in self.details:
                return self.details[msg_id]
            folder, uid = where[msg_id]
            if imap.select(quoted(folder), readonly=True)[0] != "OK":
                return None
            got = imap.uid("FETCH", uid, "(BODY.PEEK[])")[1] or []
            raw = next((p[1] for p in got if isinstance(p, tuple)), None)
            if not raw:
                return None
            d = agent.message_details(email.message_from_bytes(raw, policy=email.policy.default))
            d["mail_link"] = agent.mail_link(d.pop("message_id"))
            self.details[msg_id] = d
            return d

        return out, {"body": body, "close": lambda: self._logout(imap)}

    @staticmethod
    def _logout(imap) -> None:
        try:
            imap.logout()
        except Exception:
            pass


def claim_ids(items: list[dict], known: list[dict]) -> int:
    """Give an email the Mac already sent its old id: same sender and subject, received within a minute."""
    index: dict = {}
    for k in known:
        try:
            at = datetime.fromisoformat(k["received"]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        index.setdefault(((k.get("from_addr") or "").lower(), normalize_subject(k.get("title") or "")), []).append(
            (at, k["id"]))
    have = {k["id"] for k in known}
    taken = {i["id"] for i in items}
    n = 0
    for it in items:
        if it["id"] in have:
            continue
        at = datetime.fromisoformat(it["received"])
        for when, old in index.get((it["from_addr"], normalize_subject(it["title"])), []):
            if abs((when - at).total_seconds()) <= 90 and old not in taken:
                it["id"] = old
                taken.add(old)
                n += 1
                break
    return n


def sync(store: Store, cfg: dict, accounts: dict, now: datetime) -> dict:
    """Read every account, triage, add details to recent emails that need Ben, and store the items."""
    if not cfg:
        return {}
    msgs, readers, errors = [], [], []
    for a in cfg["accounts"]:
        acc = accounts.setdefault(a["user"], Account(a))
        acc.cfg = a
        try:
            got, reader = acc.read(cfg["days"], now)
        except Exception as e:  # one account failing (wrong password, offline) shouldn't stop the others
            errors.append(f"{acc.label}: {e}")
            continue
        msgs += got
        readers.append(reader)
    try:
        triaged = triage(msgs, now)
        items = [i.to_dict() for i in triaged]
        want = want_details(items, now, DETAIL_DAYS, DETAIL_MAX)
        detailed = 0
        for it in want:
            for r in readers:
                try:
                    d = r["body"](it["ref"])
                except KeyError:
                    continue
                except Exception as e:  # a body we can't read never stops the sync
                    errors.append(f"body: {e}")
                    break
                if d:
                    it.update(d)
                    grade_post(it)  # the body says what a post alert is
                    detailed += 1
                break
    finally:
        for r in readers:
            r["close"]()
    if not readers:
        store.set_meta("mail_sync", {"at": now.isoformat(timespec="seconds"), "errors": errors})
        return {"errors": errors}
    claimed = claim_ids(items, store.items())
    store.upsert_items(items)
    store.outbox_to_tasks(now.date())
    report = {"at": now.isoformat(timespec="seconds"), "mail": summary(triaged), "details": detailed,
              "claimed": claimed, "errors": errors}
    store.set_meta("mail_sync", report)
    return report


def loop(path: str, store: Store, tz, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    accounts: dict = {}
    first = True
    while True:
        try:
            got = sync(store, load(path), accounts, datetime.now(tz))
            if got.get("errors") or (first and got):
                # counts only (how many emails, details, claimed), never a subject or address
                print(f"One Inbox mail: {got.get('mail')}, details {got.get('details')}, kept the Mac's ids "
                      f"for {got.get('claimed')}, errors {got.get('errors')}", flush=True)
                first = False
        except Exception as e:  # never take the server down
            print(f"One Inbox mail failed: {e}", flush=True)
        if stop.wait(EVERY):
            return
