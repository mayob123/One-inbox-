"""Job tracker tests: a small database shaped like the tracker's (made-up roles only)."""

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from onebox import agent, jobtrack

CPH = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=CPH)
TODAY = NOW.date()


def iso(days):
    return (TODAY + timedelta(days=days)).isoformat()


def make_tracker(path):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE applications (id INTEGER PRIMARY KEY, job_title TEXT, company TEXT, source_url TEXT,
        status TEXT, attachment_paths TEXT, deadline TEXT, deadline_rolling INTEGER DEFAULT 0,
        priority INTEGER DEFAULT 0, deadline_note TEXT, interview_at TEXT, category TEXT, notes TEXT);
      CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT, url TEXT, source TEXT,
        likely_closed INTEGER DEFAULT 0, danish_required INTEGER DEFAULT 0, fit_tier TEXT, dismissed INTEGER DEFAULT 0,
        deadline TEXT, deadline_rolling INTEGER DEFAULT 0, priority INTEGER DEFAULT 0, deadline_note TEXT);
    """)
    con.executemany(
        "INSERT INTO applications (id, job_title, company, source_url, status, attachment_paths, deadline,"
        " deadline_rolling, priority, interview_at) VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (1, "IT Student", "Acme", "https://x/1", "saved", "cv.pdf", iso(10), 0, 0, None),
            (2, "Bookmark only", "Bmk", "", "saved", None, None, 0, 0, None),
            (3, "Sent role", "Sentco", "", "applied", "cv.pdf", iso(3), 0, 0, None),
            (4, "Grants Officer", "Fundco", "", "interview", None, None, 0, 0, f"{iso(4)} 14:00"),
            (5, "Old interview", "Pastco", "", "interview", None, None, 0, 0, f"{iso(-2)} 10:00"),
            (6, "Missed it", "Lateco", "", "saved", "cv.pdf", iso(-1), 0, 0, None),
        ])
    con.executemany(
        "INSERT INTO jobs (id, title, company, url, source, fit_tier, deadline, deadline_rolling, priority,"
        " danish_required, likely_closed, dismissed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (10, "M365 Student", "Bigpharma", "https://x/10", "linkedin-manual", "Strong fit", None, 1, 0, 0, 0, 0),
            (11, "Data Student", "Faraway", "", "jobindex", "Possible fit", iso(30), 0, 0, 0, 0, 0),
            (12, "Partner Manager", "Ngo", "", "jobindex", "Possible fit", iso(6), 0, 0, 0, 0, 0),
            (13, "Dansk rolle", "Dk", "", "jobindex", "Strong fit", iso(5), 0, 0, 1, 0, 0),
            (14, "Closed role", "Gone", "", "jobindex", "Strong fit", iso(5), 0, 0, 0, 1, 0),
            (15, "Sent role", "Sentco", "", "jobindex", "Strong fit", iso(3), 0, 0, 0, 0, 0),
            (16, "Skipped", "Nope", "", "jobindex", "Strong fit", iso(5), 0, 0, 0, 0, 1),
        ])
    con.commit()
    con.close()


def make_things(path, todos):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE TMArea (uuid TEXT PRIMARY KEY, title TEXT);
      CREATE TABLE TMTask (uuid TEXT PRIMARY KEY, title TEXT, notes TEXT, type INTEGER, status INTEGER, trashed INTEGER,
        start INTEGER, startDate INTEGER, todayIndex INTEGER, stopDate REAL, deadline INTEGER, area TEXT, project TEXT);
    """)
    con.executemany("INSERT INTO TMTask (uuid, title, notes, type, status, trashed, start) VALUES (?,?,?,0,0,0,1)", todos)
    con.commit()
    con.close()


class JobTrackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "job_tracker.db")
        make_tracker(self.db)
        self.roles, self.done = jobtrack.read_roles(self.db, NOW)
        self.by = {r.marker: r for r in self.roles}

    def tearDown(self):
        self.dir.cleanup()

    def test_mirrors_apply_next(self):
        self.assertEqual(sorted(self.by), sorted([
            "job-tracker:draft:1", "job-tracker:draft:6", "job-tracker:interview:4",
            "job-tracker:lead:10", "job-tracker:lead:12"]))
        # 11 is a possible fit a month out (under More leads), 13 needs Danish, 14 closed,
        # 15 already applied for, 16 skipped, 2 a bookmark, 5 an interview that's over.

    def test_applied_roles_count_as_done(self):
        # 3 was sent, 4 and 5 got as far as an interview, 15 is the lead behind 3.
        self.assertEqual(self.done, {"job-tracker:draft:3", "job-tracker:draft:4", "job-tracker:draft:5",
                                     "job-tracker:lead:15"})

    def test_deadline_gets_heads_up_and_last_day(self):
        todos = jobtrack.todos_for(self.by["job-tracker:draft:1"], TODAY, "Job Search", "http://t")
        self.assertEqual(todos[0]["title"], "Apply: Acme, IT Student (due " + (TODAY + timedelta(days=10)).strftime("%a %-d %b") + ")")
        self.assertEqual(todos[0]["when"], iso(5))
        self.assertEqual(todos[0]["deadline"], iso(10))
        self.assertEqual(todos[0]["list"], "Job Search")
        self.assertEqual(todos[1]["title"], "Last day tomorrow: Acme, IT Student")
        self.assertEqual(todos[1]["when"], iso(9) + "@09:00")
        self.assertIn("job-tracker:draft:1 #", todos[0]["notes"])
        self.assertIn("https://x/1", todos[0]["notes"])

    def test_rolling_lands_today(self):
        todos = jobtrack.todos_for(self.by["job-tracker:lead:10"], TODAY, "", "http://t")
        self.assertEqual([t["when"] for t in todos], ["today"])
        self.assertTrue(todos[0]["title"].startswith("Apply early (rolling): Bigpharma"))
        self.assertIn("Strong fit", todos[0]["notes"])

    def test_interview_has_reminder_and_prep(self):
        todos = jobtrack.todos_for(self.by["job-tracker:interview:4"], TODAY, "", "http://t")
        self.assertEqual(todos[0]["title"], "Interview 14:00: Fundco, Grants Officer")
        self.assertEqual(todos[0]["when"], iso(4) + "@13:00")
        self.assertEqual(todos[1]["when"], iso(3) + "@09:00")

    def test_passed_deadline_is_never_added(self):
        new = jobtrack.new_roles(self.roles, {})
        self.assertNotIn("job-tracker:draft:6", [r.marker for r in new])
        self.assertEqual(new[0].kind, "interview")

    def test_tidy_completes_applied_and_cancels_the_rest(self):
        live_key = self.by["job-tracker:draft:1"].key()
        todos = [
            ("u1", "job-tracker:draft:3", "abc"),       # applied -> completed
            ("u2", "job-tracker:lead:16", "abc"),       # skipped -> canceled
            ("u3", "job-tracker:draft:1", live_key),    # still live -> left alone
            ("u4", "job-tracker:draft:1", "oldkey"),    # deadline moved -> canceled
            ("u5", "job-tracker:draft:6", "oldkey"),    # deadline passed, still open -> left alone
        ]
        self.assertEqual(jobtrack.tidy(self.roles, self.done, todos), {"completed": ["u1"], "canceled": ["u2", "u4"]})

    def test_open_todos_reads_markers(self):
        things = os.path.join(self.dir.name, "main.sqlite")
        make_things(things, [("u1", "Apply", "x\njob-tracker:lead:10 #a1b2 (added by One Inbox)"),
                             ("u2", "Other", "nothing here")])
        self.assertEqual(jobtrack.open_todos(things), [("u1", "job-tracker:lead:10", "a1b2")])

    def test_update_url(self):
        url = jobtrack.update_url("u1", "tok", "completed")
        self.assertEqual(url, "things:///update?id=u1&completed=true&auth-token=tok")


class AgentJobSyncTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "job_tracker.db")
        self.things = os.path.join(self.dir.name, "main.sqlite")
        make_tracker(self.db)
        make_things(self.things, [("u1", "Apply: Sentco", "job-tracker:lead:15 #k (added by One Inbox)")])
        self.cfg = {"job_tracker": {"db": self.db, "seen": os.path.join(self.dir.name, "seen.json")},
                    "things_auth_token": "tok"}

    def tearDown(self):
        self.dir.cleanup()

    def test_sync_adds_once_and_ticks_off_applied(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            out = agent.sync_jobs(self.cfg, NOW, self.things)
            urls = [c.args[0][2] for c in run.call_args_list]
            self.assertEqual(out["closed"], 1)
            self.assertIn("things:///update?id=u1&completed=true&auth-token=tok", urls)
            data = json.loads(urllib.parse.unquote(urls[-1].split("=", 1)[1]))
            self.assertEqual(out["added"], len(data))
            self.assertTrue(all(d["attributes"]["list"] == "Job Search" for d in data))
            run.reset_mock()
            again = agent.sync_jobs(self.cfg, NOW, self.things)
            self.assertEqual(again["added"], 0)

    def test_without_token_only_reports(self):
        self.cfg.pop("things_auth_token")
        with mock.patch.object(agent.subprocess, "run") as run:
            out = agent.sync_jobs(self.cfg, NOW, self.things)
        self.assertEqual(out["stale"], 1)
        self.assertEqual(len(run.call_args_list), 1)  # just the add

    def test_dry_run_changes_nothing(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            out = agent.sync_jobs(self.cfg, NOW, self.things, dry_run=True)
        run.assert_not_called()
        self.assertEqual(out["would_complete"], 1)
        self.assertFalse(Path(self.cfg["job_tracker"]["seen"]).exists())

    def test_missing_tracker_is_reported_not_raised(self):
        self.cfg["job_tracker"]["db"] = os.path.join(self.dir.name, "nope.db")
        out = agent.sync_jobs(self.cfg, NOW, self.things)
        self.assertIn("job tracker", out["error"])

    def test_tracker_is_opened_read_only(self):
        con = jobtrack.connect(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM jobs")
        con.close()


if __name__ == "__main__":
    unittest.main()
