"""The Pi's own mail reader, against a small fake IMAP server."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from onebox import mailpoll
from onebox.store import Store

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def mail(frm, subject, when, body="Hello", mid="m1@x"):
    return {"from": frm, "subject": subject, "when": when, "body": body, "mid": mid}


class FakeIMAP:
    """Folders of messages by uid. Records what was fetched so tests can check only new mail is downloaded."""

    def __init__(self, folders):
        self.folders = folders  # name -> (flags, {uid: (message, seen)})
        self.current = None
        self.fetched = []
        self.logged_in = self.logged_out = False

    def login(self, user, password):
        if password != "app-pw":
            raise mailpoll.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logged_in = True

    def list(self):
        return "OK", [f'({flags}) "/" "{name}"'.encode() for name, (flags, _) in self.folders.items()]

    def select(self, name, readonly=False):
        self.current = name.strip('"')
        return ("OK", [b"1"]) if self.current in self.folders else ("NO", [])

    def response(self, code):
        return code, [b"42"]

    def uid(self, cmd, *args):
        msgs = self.folders[self.current][1]
        if cmd == "SEARCH":
            since = datetime.strptime(args[2], "%d-%b-%Y").replace(tzinfo=timezone.utc)
            return "OK", [" ".join(str(u) for u, (m, _) in sorted(msgs.items()) if m["when"] >= since).encode()]
        uids, what = [int(u) for u in args[0].split(",")], args[1]
        out = []
        for u in uids:
            m, seen = msgs[u]
            flags = "\\Seen" if seen else ""
            if what == "(FLAGS)":
                out.append(f"{u} (UID {u} FLAGS ({flags}))".encode())
                continue
            self.fetched.append((self.current, u, what))
            head = f"From: {m['from']}\r\nSubject: {m['subject']}\r\nMessage-ID: <{m['mid']}>\r\n"
            raw = (head + "\r\n" + m["body"]) if "BODY.PEEK[]" in what else head
            date = m["when"].strftime("%d-%b-%Y %H:%M:%S +0000")
            out.append((f'{u} (UID {u} INTERNALDATE "{date}" FLAGS ({flags}) BODY[] {{{len(raw)}}}'.encode(),
                        raw.encode()))
            out.append(b")")
        return "OK", out

    def logout(self):
        self.logged_out = True


class MailPollTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.dir.name, "db.sqlite3"))
        day = timedelta(days=1)
        self.imap = FakeIMAP({
            "INBOX": ("\\HasNoChildren", {
                1: (mail("Maria <hr@example.dk>", "2nd interview: Case", NOW - 2 * day,
                         "Can you come Tuesday? https://example.dk/book", "a@x"), False),
                2: (mail("Old <old@example.dk>", "Ancient", NOW - 60 * day, mid="b@x"), True),
                3: (mail("Aula <noreply@aula.dk>", "Ny besked fra skolen", NOW - day, mid="c@x"), False),
            }),
            "Sent Messages": ("\\Sent", {}),
            "Junk": ("\\Junk", {
                4: (mail("Spam <x@spam.biz>", "Win a prize", NOW - day, mid="d@x"), False),
                5: (mail("LinkedIn <jobalerts-noreply@linkedin.com>", "DTU is hiring", NOW - day, mid="e@x"), False),
            }),
            "Deleted Messages": ("\\Trash", {6: (mail("a <a@b.dk>", "Gone", NOW - day, mid="f@x"), False)}),
        })
        self.cfg = {"days": 21, "accounts": [{"label": "iCloud", "user": "ben", "password": "app-pw"}]}
        self.accounts = {"ben": mailpoll.Account(self.cfg["accounts"][0], connect=lambda: self.imap)}

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_load(self):
        path = os.path.join(self.dir.name, "mail.json")
        self.assertEqual(mailpoll.load(path), {})
        with open(path, "w") as f:
            json.dump({"accounts": [{"user": "ben"}]}, f)
        self.assertEqual(mailpoll.load(path), {})  # no password: nothing to log in with
        with open(path, "w") as f:
            json.dump(self.cfg, f)
        self.assertEqual(mailpoll.load(path)["accounts"][0]["label"], "iCloud")

    def test_reads_triages_and_details(self):
        report = mailpoll.sync(self.store, self.cfg, self.accounts, NOW)
        self.assertEqual(report["errors"], [])
        items = {i["title"]: i for i in self.store.items()}
        self.assertEqual(set(items), {"2nd interview: Case", "Ny besked fra skolen", "DTU is hiring"})
        interview = items["2nd interview: Case"]
        self.assertEqual((interview["status"], interview["source"]), ("decide", "iCloud"))
        self.assertIn("https://example.dk/book", interview["links"])
        self.assertEqual(interview["mail_link"], "message:%3Ca@x%3E")
        self.assertEqual(items["Ny besked fra skolen"]["post"], "Aula")
        self.assertTrue(self.imap.logged_out)
        self.assertEqual(self.store.get_meta("mail_sync")["details"], report["details"])

    def test_second_run_downloads_only_new_mail(self):
        mailpoll.sync(self.store, self.cfg, self.accounts, NOW)
        heads = [f for f in self.imap.fetched if "HEADER" in f[2]]
        self.imap.fetched.clear()
        self.imap.folders["INBOX"][1][7] = (mail("Tom <tom@example.dk>", "Dinner Friday?", NOW, mid="g@x"), False)
        mailpoll.sync(self.store, self.cfg, self.accounts, NOW + timedelta(minutes=3))
        again = [f for f in self.imap.fetched if "HEADER" in f[2]]
        self.assertGreater(len(heads), 1)
        self.assertEqual([(f[0], f[1]) for f in again], [("INBOX", 7)])
        self.assertIn("Dinner Friday?", [i["title"] for i in self.store.items()])

    def test_takes_over_the_macs_items_without_doubles(self):
        mac = {"id": "macid000000000001", "status": "decide", "title": "2nd interview: Case", "from_addr": "hr@example.dk",
               "received": (NOW - timedelta(days=2, seconds=-20)).isoformat(), "kind": "job"}
        self.store.upsert_items([mac])
        self.store.decide(mac["id"], "none")
        report = mailpoll.sync(self.store, self.cfg, self.accounts, NOW)
        self.assertEqual(report["claimed"], 1)
        rows = [i for i in self.store.items() if i["title"] == "2nd interview: Case"]
        self.assertEqual([(r["id"], r["decision"]) for r in rows], [("macid000000000001", "none")])

    def test_wrong_password_is_reported(self):
        self.cfg["accounts"][0]["password"] = "nope"
        report = mailpoll.sync(self.store, self.cfg, self.accounts, NOW)
        self.assertIn("AUTHENTICATIONFAILED", report["errors"][0])
        self.assertEqual(self.store.items(), [])


if __name__ == "__main__":
    unittest.main()
