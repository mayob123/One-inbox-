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


def make_calendar(path):
    def apple(dt):
        return (dt - agent.APPLE_EPOCH).total_seconds()
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT);
      CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT, all_day INTEGER, calendar_id INTEGER);
      CREATE TABLE OccurrenceCache (day REAL, event_id INTEGER, calendar_id INTEGER, occurrence_date REAL,
        occurrence_start_date REAL, occurrence_end_date REAL);
      INSERT INTO Calendar VALUES (1, 'Home'), (2, 'Birthdays');
      INSERT INTO CalendarItem VALUES (1, 'Dentist', 0, 1), (2, 'Tom''s birthday', 0, 2), (3, 'Holiday', 1, 1),
        (4, 'Lecture', 0, 1), (5, 'Gone', 0, 1);
    """)
    con.executemany("INSERT INTO OccurrenceCache VALUES (0, ?, 1, ?, ?, NULL)", [
        (1, apple(NOW + timedelta(hours=3)), None),
        (2, apple(NOW + timedelta(hours=1)), None),
        (3, apple(NOW + timedelta(hours=1)), None),
        (4, apple(NOW + timedelta(hours=20)), apple(NOW + timedelta(hours=2))),
        (5, apple(NOW - timedelta(hours=1)), None),
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

    def test_next_events(self):
        cal = os.path.join(self.dir.name, "Calendar.sqlitedb")
        make_calendar(cal)
        events = agent.next_events(cal, NOW)
        self.assertEqual([e["title"] for e in events], ["Lecture", "Dentist"])
        self.assertEqual(datetime.fromisoformat(events[1]["start"]), (NOW + timedelta(hours=3)).replace(second=0, microsecond=0))
        self.assertEqual(agent.read_calendar({"calendar_db": self.mail}, NOW)[0], [])  # wrong schema: no crash

    def test_things_pillars(self):
        p = agent.things_pillars(self.things, NOW)
        # make_things: t1 Danish (open, Today), t2 bill (open, due +3), t3 Someday (left out),
        # t4 open with past deadline, t5 done today, t6 done 8 days ago.
        self.assertEqual(p["danish"]["open"], 1)
        self.assertEqual(p["bill"]["next_due"], (NOW.date() + timedelta(days=3)).isoformat())
        other = p["other"]
        self.assertEqual((other["open"], other["done_week"]), (1, 1))
        self.assertEqual(other["last_done"], NOW.date().isoformat())
        self.assertEqual(sum(e["open"] for e in p.values()), 3)

    def test_things_tags_decide_the_pillar(self):
        con = sqlite3.connect(self.things)
        con.executescript("""
          CREATE TABLE TMTag (uuid TEXT PRIMARY KEY, title TEXT);
          CREATE TABLE TMTaskTag (tasks TEXT, tags TEXT);
          INSERT INTO TMTag VALUES ('g1', 'Job hunt'), ('g2', 'Errand');
          INSERT INTO TMTaskTag VALUES ('t4', 'g1'), ('t4', 'g2'), ('t2', 'g2');
        """)
        con.commit()
        con.close()
        p = agent.things_pillars(self.things, NOW)
        self.assertEqual(p["job"]["open"], 1)  # "Late form" is tagged Job hunt
        self.assertEqual(p["bill"]["open"], 1)  # an unrelated tag leaves the guess alone
        s = agent.things_status(self.things, NOW)
        self.assertEqual(s["today"][0]["kind"], "danish")
        # Untagged to-dos that look like a pillar get its tag; t4 is tagged already.
        self.assertEqual(sorted(agent.untagged_guesses(self.things)), [("t1", "Danish"), ("t2", "Home admin")])

    def test_kind_of_prefers_tags(self):
        self.assertEqual(agent.kind_of("Pay rent", tags="Kids"), "school")
        self.assertEqual(agent.kind_of("Pay rent", tags="Dansk|x"), "danish")
        self.assertEqual(agent.kind_of("Pay rent", tags=None), "bill")
        self.assertEqual(agent.kind_of("Apply to Lundbeck", tags="Job search"), "job")

    def test_tagging_writes_each_guess_once(self):
        tagged = os.path.join(self.dir.name, "tagged.json")
        cfg = {"auto_tag": True, "things_auth_token": "tok", "tagged": tagged}
        opened = []
        real = agent.subprocess.run
        agent.subprocess.run = lambda args, check: opened.append(args[-1])
        try:
            self.assertEqual(agent.tag_in_things(cfg, self.things), 2)
            self.assertEqual(agent.tag_in_things(cfg, self.things), 0)
            self.assertEqual(agent.tag_in_things({"things_auth_token": "tok"}, self.things), 0)  # off by default
        finally:
            agent.subprocess.run = real
        self.assertTrue(all(u.startswith("things:///update?") and "add-tags=" in u for u in opened))
        with open(tagged) as f:
            self.assertEqual(json.load(f), {"t1": "Danish", "t2": "Home admin"})

    def test_things_done_url(self):
        url = agent.things_done_url("ABC123", "tok&en")
        q = urllib.parse.parse_qs(url.split("?", 1)[1])
        self.assertTrue(url.startswith("things:///update?"))
        self.assertEqual((q["id"], q["completed"], q["auth-token"]), (["ABC123"], ["true"], ["tok&en"]))

    def test_things_url_is_valid_json(self):
        url = agent.things_add_url([{"task": "Pay & check", "title": "Bill", "sender": "V", "received": "2026-10-22T00:00:00",
                                     "why": "", "area": "Admin"}])
        self.assertTrue(url.startswith("things:///json?data="))
        data = json.loads(urllib.parse.unquote(url.split("=", 1)[1]))
        self.assertEqual(data[0]["attributes"]["title"], "Pay & check")
        self.assertNotIn("when", data[0]["attributes"])

    def test_auto_items_go_to_today(self):
        url = agent.things_add_url([{"task": "Read in Aula: Ny besked", "title": "Ny besked", "sender": "Aula",
                                     "received": "2026-10-22T00:00:00", "auto": "things"}])
        data = json.loads(urllib.parse.unquote(url.split("=", 1)[1]))
        self.assertEqual(data[0]["attributes"]["when"], "today")


if __name__ == "__main__":
    unittest.main()
