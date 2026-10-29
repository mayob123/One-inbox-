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


def make_job_tracker(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE applications (id INTEGER PRIMARY KEY, job_title TEXT, company TEXT, date_applied TEXT,
            status TEXT, deadline TEXT, interview_at TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT, deadline TEXT, dismissed INTEGER,
            likely_closed INTEGER, fit_tier TEXT);
    """)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M")
    day = lambda n: (NOW.date() + timedelta(days=n)).isoformat()
    con.executemany("INSERT INTO applications (company, date_applied, status, deadline, interview_at) VALUES (?,?,?,?,?)", [
        ("DTU", NOW.date().isoformat(), "interview", None, fmt(NOW + timedelta(hours=5))),
        ("Old Co", day(-30), "interview", None, fmt(NOW - timedelta(days=7))),      # past
        ("Gone Co", day(-30), "withdrawn", None, fmt(NOW + timedelta(hours=2))),    # withdrawn
        ("Saved Co", None, "saved", day(6), None),
        ("Late Co", None, "saved", day(-1), None),                                  # deadline passed
    ])
    con.executemany("INSERT INTO jobs (title, deadline, dismissed, likely_closed, fit_tier) VALUES (?,?,?,?,?)", [
        ("Strong", day(4), 0, 0, "Strong fit"),
        ("Possible", None, 0, 0, "Possible fit"),
        ("Long", day(1), 0, 0, "Long shot"),
        ("Dismissed", day(1), 1, 0, "Strong fit"),
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
        self.assertEqual(agent.kind_of("Pay rent", tags="Family"), "school")
        self.assertEqual(agent.kind_of("Book the doctor for Oscar"), "school")
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

    def test_job_tracker(self):
        db = os.path.join(self.dir.name, "job_tracker.db")
        make_job_tracker(db)
        status = {"events": [{"title": "Lecture", "start": (NOW + timedelta(hours=1)).isoformat()}],
                  "pillars": {"job": {"done_week": 1, "open": 2, "last_done": None, "next_due": None}}}
        agent.read_job_tracker({"job_tracker_db": db}, status, NOW)
        self.assertEqual([e["title"] for e in status["events"]], ["Lecture", "Interview: DTU"])
        job = status["pillars"]["job"]
        due = (NOW.date() + timedelta(days=4)).isoformat()
        self.assertEqual((job["next_due"], job["job_due"], job["leads"], job["applied_week"], job["open"]),
                         (due, due, 2, 1, 2))
        self.assertEqual([lead["title"] for lead in job["lead_list"]], ["Strong", "Possible"])  # soonest first
        self.assertEqual(datetime.fromisoformat(job["interview_at"]),
                         (NOW + timedelta(hours=5)).replace(second=0, microsecond=0))
        self.assertEqual(agent.read_job_tracker({"job_tracker_db": self.mail}, {}, NOW)[:9], "Could not")
        self.assertEqual(agent.read_job_tracker({"job_tracker_db": "/nonexistent"}, {}, NOW), "Job tracker not found.")

    def test_wake_mail_starts_mail_only_when_closed(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"returncode": 1 if cmd[0] == "pgrep" else 0})()

        real_run, real_platform = agent.subprocess.run, agent.sys.platform
        agent.subprocess.run, agent.sys.platform = fake_run, "darwin"
        try:
            self.assertTrue(agent.wake_mail({}))
            self.assertEqual(calls[-1], ["open", "-g", "-j", "-a", "Mail"])
            calls.clear()
            self.assertFalse(agent.wake_mail({"keep_mail_open": False}))
            self.assertEqual(calls, [])
        finally:
            agent.subprocess.run, agent.sys.platform = real_run, real_platform

    def test_things_done_url(self):
        url = agent.things_done_url("ABC123", "tok&en")
        q = urllib.parse.parse_qs(url.split("?", 1)[1])
        self.assertTrue(url.startswith("things:///update?"))
        self.assertEqual((q["id"], q["completed"], q["auth-token"]), (["ABC123"], ["true"], ["tok&en"]))

    def test_things_update_url_spaces_are_not_plus(self):
        url = agent.things_update_url("ABC123", "tok", title="Check Aula (3 new)")
        self.assertIn("title=Check%20Aula%20%283%20new%29", url)
        self.assertNotIn("+", url)

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

    def test_captures_can_say_where_they_go(self):
        url = agent.things_add_url([{"task": "Pick up frames", "title": "Pick up frames", "sender": "You",
                                     "received": "2026-10-22T00:00:00", "auto": "things", "when": "anytime",
                                     "tags": ["Errands"], "list": "Around the house", "deadline": "2026-10-30"}])
        a = json.loads(urllib.parse.unquote(url.split("=", 1)[1]))[0]["attributes"]
        self.assertEqual((a["when"], a["tags"], a["list"], a["deadline"]),
                         ("anytime", ["Errands"], "Around the house", "2026-10-30"))

    def test_post_plan_keeps_one_check_todo_per_source(self):
        aula = {"source": "Aula", "count": 2, "since": "2026-10-26T08:00:00+01:00",
                "items": [{"id": "a", "title": "Ny besked", "received": "2026-10-26T08:00:00+01:00"},
                          {"id": "b", "title": "Ny besked fra Lærer", "received": "2026-10-27T14:02:00+01:00"}]}
        eboks = {"source": "e-Boks", "count": 0, "items": []}
        plan = agent.post_plan([aula, eboks], {})
        self.assertEqual(len(plan), 1)
        op, a = plan[0]
        self.assertEqual((op, a["title"], a["when"], a["tags"]), ("add", "Check Aula (2 new)", "today", ["Family"]))
        self.assertIn("Ny besked fra Lærer", a["notes"])
        bullets = [line for line in a["notes"].splitlines() if line.startswith("• ")]
        self.assertEqual(len(bullets), 2)
        self.assertTrue(bullets[0].endswith("Ny besked fra Lærer"))  # newest first
        # already there with the same count: nothing to do; a new alert: update the one to-do
        self.assertEqual(agent.post_plan([aula], {"Aula": ("U1", "Check Aula (2 new)")}), [])
        plan = agent.post_plan([dict(aula, count=3)], {"Aula": ("U1", "Check Aula (2 new)")})
        self.assertEqual([(op, a["id"], a["title"]) for op, a in plan], [("update", "U1", "Check Aula (3 new)")])

    def test_post_todos_in_things(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "things.sqlite")
            make_things(path)
            con = sqlite3.connect(path)
            stop = NOW.timestamp() - 600
            con.executemany("INSERT INTO TMTask VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("p1", "Check Aula (3 new)", 0, 0, 0, 1, agent.things_date(NOW.date()), 3, None, None, None, None),
                ("p2", "Check e-Boks (1 new)", 0, 3, 0, 1, agent.things_date(NOW.date()), 0, stop, None, None, None),
                ("p3", "Check the tyres", 0, 0, 0, 1, None, 0, None, None, None, None),
            ])
            con.commit()
            con.close()
            self.assertEqual(agent.open_post_todos(path), {"Aula": ("p1", "Check Aula (3 new)")})
            s = agent.things_status(path, NOW)
            self.assertEqual(list(s["post_done"]), ["e-Boks"])
            self.assertEqual(datetime.fromisoformat(s["post_done"]["e-Boks"]).timestamp(), int(stop))

    def test_small_jobs_pool_and_this_week(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "things.sqlite")
            make_things(path)
            con = sqlite3.connect(path)
            td = agent.things_date
            con.executescript("CREATE TABLE TMTag (uuid TEXT, title TEXT); CREATE TABLE TMTaskTag (tasks TEXT, tags TEXT);"
                              "INSERT INTO TMTag VALUES ('g1', 'Errands'), ('g2', '5 min'), ('g3', 'Home admin');")
            con.executemany("INSERT INTO TMTask VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("H", "Around the house", 1, 0, 0, 1, None, 0, None, None, None, None),  # the project itself
                ("j1", "Bulb in the master bedroom", 0, 0, 0, 1, None, 0, None, None, None, "H"),
                ("j2", "Pick up frames", 0, 0, 0, 1, None, 0, None, None, None, None),
                ("j3", "Hang pictures", 0, 0, 0, 2, None, 0, None, None, None, "H"),
                ("j4", "Not a small job", 0, 0, 0, 1, None, 0, None, None, None, None),
                ("j5", "Scheduled errand", 0, 0, 0, 1, td(NOW.date() + timedelta(days=2)), 0, None, None, None, None),
                ("w1", "Trip form", 0, 0, 0, 1, None, 0, None, td(NOW.date() + timedelta(days=3)), None, None),
                ("w2", "Later form", 0, 0, 0, 1, None, 0, None, td(NOW.date() + timedelta(days=12)), None, None),
            ])
            con.executemany("INSERT INTO TMTaskTag VALUES (?, ?)", [("j2", "g1"), ("j5", "g1"), ("j1", "g2")])
            con.commit()
            con.close()
            s = agent.things_status(path, NOW)
        pool = {r["id"]: r for r in s["pool"]}
        self.assertEqual(set(pool), {"j1", "j2", "j3"})
        self.assertEqual((pool["j1"]["quick"], pool["j1"]["project"]), (True, "Around the house"))
        self.assertEqual((pool["j2"]["errand"], pool["j3"]["someday"]), (True, True))
        week = [r["id"] for r in s["week"]]
        self.assertIn("w1", week)
        self.assertIn("t4", week)  # overdue, scheduled for later: still this week's business
        self.assertNotIn("w2", week)
        self.assertNotIn("t2", week)  # already in Today

    def test_mail_details_from_emlx(self):
        raw = (b"Message-ID: <abc123@mail.dk>\r\nFrom: Adil <adil@welcometodanmark.dk>\r\nSubject: Re: manglende lektier\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
               b"Hej Ben,\r\nSe afsnit 1: https://www.dr.dk/drtv/gift-1 og quizlet https://quizlet.com/join/xyz.\r\n"
               b"Unsubscribe: https://example.com/unsubscribe\r\n\r\nDen 14. okt. skrev Ben:\r\n> old https://old.example\r\n")
        with tempfile.TemporaryDirectory() as d:
            mail = os.path.join(d, "V10")
            msgs = os.path.join(mail, "ACC1", "INBOX.mbox", "Data", "Messages")
            files = os.path.join(mail, "ACC1", "INBOX.mbox", "Data", "Attachments", "7", "2")
            os.makedirs(msgs)
            os.makedirs(files)
            with open(os.path.join(msgs, "7.emlx"), "wb") as fh:
                fh.write(str(len(raw)).encode() + b"\n" + raw + b"<?xml version=\"1.0\"?><plist/>")
            with open(os.path.join(files, "slides.pdf"), "wb") as fh:
                fh.write(b"%PDF")
            icloud = os.path.join(d, "iCloud")
            os.makedirs(icloud)
            item = {"status": "decide", "ref": "ACC1:7", "received": NOW.isoformat(), "auto": "things",
                    "area": "Danish", "title": "Re: manglende lektier", "task": "Danish homework", "sender": "Adil"}
            n = agent.add_details([item], os.path.join(mail, "MailData", "Envelope Index"), NOW,
                                  save_to=agent.Path(icloud))
            self.assertEqual(n, 1)
            self.assertEqual(item["links"], ["https://www.dr.dk/drtv/gift-1", "https://quizlet.com/join/xyz"])
            self.assertNotIn("old", item["excerpt"])
            self.assertEqual(item["mail_link"], "message:%3Cabc123@mail.dk%3E")
            self.assertTrue(os.path.exists(os.path.join(icloud, "One Inbox", "Danish", "slides.pdf")))
            notes = agent.things_notes(item)
            self.assertTrue(notes.startswith("https://www.dr.dk/drtv/gift-1\nhttps://quizlet.com/join/xyz\n"))
            self.assertIn("Open the email: message:%3Cabc123@mail.dk%3E", notes)

    def test_things_notes_reach_the_phone(self):
        self.assertEqual(agent.things_text("<note xml:space=\"preserve\"><p>Quizlet: https://q.com/x</p></note>"),
                         "Quizlet: https://q.com/x")


if __name__ == "__main__":
    unittest.main()
