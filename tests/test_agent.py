"""Agent tests run against small fake copies of the Mail and Things databases."""

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from onebox import agent

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


def make_mail(path):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, sender INTEGER, subject_prefix TEXT, subject INTEGER,
        date_received INTEGER, mailbox INTEGER, read INTEGER, deleted INTEGER);
      CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
      CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
      CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
      INSERT INTO mailboxes VALUES (1, 'imap://ACC1/INBOX'), (2, 'imap://ACC1/Sent%20Messages'), (3, 'imap://ACC1/Junk');
      INSERT INTO addresses VALUES (1, 'hr@example.dk', 'Maria'), (2, 'ben@mac.com', 'Ben'), (3, 'x@spam.biz', 'Spam');
      INSERT INTO subjects VALUES (1, '2nd interview: Case'), (2, 'Win a prize'), (3, 'Old thing');
    """)
    t = int(NOW.timestamp())
    con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", [
        (1, 1, '', 1, t - 5 * 86400, 1, 1, 0),
        (2, 2, 'Re: ', 1, t - 4 * 86400, 2, 1, 0),
        (3, 3, '', 2, t - 1 * 86400, 3, 0, 0),        # junk: skipped
        (4, 1, '', 3, t - 60 * 86400, 1, 1, 0),       # too old: skipped
    ])
    con.commit()
    con.close()


def make_things(path):
    td = agent.things_date
    today = NOW.date()
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE TMArea (uuid TEXT PRIMARY KEY, title TEXT);
      CREATE TABLE TMTask (uuid TEXT PRIMARY KEY, title TEXT, type INTEGER, status INTEGER, trashed INTEGER,
        start INTEGER, startDate INTEGER, todayIndex INTEGER, stopDate REAL, deadline INTEGER, area TEXT, project TEXT);
      INSERT INTO TMArea VALUES ('A1', 'Admin & bills'), ('A2', 'Danish');
    """)
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    con.executemany("INSERT INTO TMTask VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("t1", "Danish homework, chapter 4", 0, 0, 0, 1, td(today), 1, None, None, "A2", None),
        ("t2", "Pay phone bill", 0, 0, 0, 1, td(today - timedelta(days=1)), 2, None, td(today + timedelta(days=3)), "A1", None),
        ("t3", "Someday thing", 0, 0, 0, 2, None, 0, None, None, None, None),
        ("t4", "Late form", 0, 0, 0, 1, td(today + timedelta(days=5)), 0, None, td(today - timedelta(days=1)), None, None),
        ("t5", "Done today", 0, 3, 0, 1, td(today), 0, midnight + 3600, None, None, None),
        ("t6", "Done last week", 0, 3, 0, 1, None, 0, midnight - 8 * 86400, None, None, None),
    ])
    con.commit()
    con.close()


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.mail = os.path.join(self.dir.name, "Envelope Index")
        self.things = os.path.join(self.dir.name, "main.sqlite")
        make_mail(self.mail)
        make_things(self.things)

    def tearDown(self):
        self.dir.cleanup()

    def test_read_mail_skips_junk_and_old(self):
        msgs = agent.read_mail(self.mail, days=21, account_labels={"ACC1": "mac.com"}, now=NOW)
        self.assertEqual(sorted(m.subject for m in msgs), ["2nd interview: Case", "Re: 2nd interview: Case"])
        self.assertEqual({m.mailbox for m in msgs}, {"INBOX", "Sent"})
        self.assertEqual({m.account for m in msgs}, {"mac.com"})

    def test_things_date_roundtrip(self):
        d = date(2026, 10, 26)
        self.assertEqual(agent.from_things_date(agent.things_date(d)), d)

    def test_things_status(self):
        s = agent.things_status(self.things, NOW)
        self.assertEqual(s["left"], 2)
        self.assertEqual(s["next"], "Danish homework, chapter 4")
        self.assertEqual(s["next_kind"], "danish")
        self.assertEqual(s["overdue"], 1)
        self.assertEqual(s["done_today"], 1)
        self.assertEqual(s["completed_ids"], ["t5"])
        self.assertEqual(s["bill"], (NOW.date() + timedelta(days=3)).strftime("%a"))
        self.assertEqual([t["title"] for t in s["today"]][0], "Danish homework, chapter 4")
        self.assertEqual(len(s["today"]), 2)
        self.assertEqual(s["today"][0]["kind"], "danish")

    def test_dry_run_end_to_end(self):
        report = agent.run_once({"mail_db": self.mail, "things_db": self.things, "days": 21,
                                 "messages_db": "/nonexistent"}, now=NOW, dry_run=True)
        self.assertEqual(report["mail"]["done"], 1)
        self.assertEqual(report["things"]["left"], 2)

    def test_things_url_is_valid_json(self):
        url = agent.things_add_url([{"task": "Pay & check", "title": "Bill", "sender": "V", "received": "2026-10-22T00:00:00",
                                     "why": "", "area": "Admin"}])
        self.assertTrue(url.startswith("things:///json?data="))
        data = json.loads(urllib.parse.unquote(url.split("=", 1)[1]))
        self.assertEqual(data[0]["attributes"]["title"], "Pay & check")


if __name__ == "__main__":
    unittest.main()
