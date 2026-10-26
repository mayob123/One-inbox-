"""Study calendar tests: a small .ics like the ones Moodle, Canvas and MyTimetable export."""

import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from onebox import agent, studycal

CPH = timezone(timedelta(hours=1))  # Copenhagen in November
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=CPH)

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:live-1@vle\r
SUMMARY:Live session: Week 1 welcome\r
DTSTART:20261103T180000Z\r
DTEND:20261103T190000Z\r
LOCATION:Zoom\r
DESCRIPTION:Join from the module page\\, link inside.\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:assign-1@vle\r
SUMMARY:Formative quiz 1 is due\r
DTSTART;TZID=Europe/London:20261116T120000\r
DTEND;TZID=Europe/London:20261116T120000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:assign-2@vle\r
SUMMARY:Module 1 essay\r
DTSTART:20261221T120000Z\r
DTEND:20261221T120000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:far-1@vle\r
SUMMARY:Live session: Week 6\r
DTSTART:20261208T180000Z\r
DTEND:20261208T190000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old-1@vle\r
SUMMARY:Induction\r
DTSTART:20261030T100000Z\r
DTEND:20261030T110000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancel-1@vle\r
SUMMARY:Cancelled seminar\r
STATUS:CANCELLED\r
DTSTART:20261104T100000Z\r
DTEND:20261104T110000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:allday-1@vle\r
SUMMARY:Reading week\r
DTSTART;VALUE=DATE:20261109\r
DTEND;VALUE=DATE:20261110\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:long-1@vle\r
SUMMARY:A very long title that is folded\r
  across two lines\r
DTSTART:20261105T090000Z\r
DTEND:20261105T100000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.events = {e.uid: e for e in studycal.parse_ics(ICS, "York MSc", "York MSc", CPH)}

    def test_parses_and_converts_to_local_time(self):
        live = self.events["live-1@vle"]
        self.assertEqual(live.start, datetime(2026, 11, 3, 19, 0, tzinfo=CPH))
        self.assertEqual(live.location, "Zoom")
        self.assertEqual(live.description, "Join from the module page, link inside.")
        self.assertFalse(live.is_deadline)

    def test_tzid_and_folding_and_cancelled(self):
        quiz = self.events["assign-1@vle"]
        self.assertEqual(quiz.start.hour, 13)  # 12:00 London = 13:00 Copenhagen
        self.assertTrue(quiz.is_deadline)
        self.assertEqual(self.events["long-1@vle"].title, "A very long title that is folded across two lines")
        self.assertNotIn("cancel-1@vle", self.events)
        self.assertTrue(self.events["allday-1@vle"].all_day)

    def test_zero_length_event_is_a_deadline(self):
        self.assertTrue(self.events["assign-2@vle"].is_deadline)


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.events = studycal.parse_ics(ICS, "York MSc", "York MSc", CPH)

    def test_due_now_window(self):
        picked = {e.uid for e in studycal.due_now(self.events, {}, NOW)}
        self.assertIn("live-1@vle", picked)          # tomorrow
        self.assertIn("assign-2@vle", picked)        # deadline 7 weeks out is fine
        self.assertNotIn("far-1@vle", picked)        # session more than 14 days out waits
        self.assertNotIn("old-1@vle", picked)        # in the past

    def test_seen_events_are_not_added_twice(self):
        first = studycal.due_now(self.events, {}, NOW)
        seen = {e.key(): "2026-11-02" for e in first}
        self.assertEqual(studycal.due_now(self.events, seen, NOW), [])

    def test_moved_session_is_added_again(self):
        live = next(e for e in self.events if e.uid == "live-1@vle")
        seen = {live.key(): "2026-11-02"}
        live.start += timedelta(hours=1)
        self.assertIn(live, studycal.due_now([live], seen, NOW))

    def test_session_todo_has_time_and_reminder(self):
        live = next(e for e in self.events if e.uid == "live-1@vle")
        [todo] = studycal.todos_for(live, NOW.date())
        self.assertEqual(todo["title"], "19:00 Live session: Week 1 welcome")
        self.assertEqual(todo["when"], "2026-11-03@18:45")
        self.assertEqual(todo["list"], "York MSc")
        self.assertIn("Zoom", todo["notes"])

    def test_deadline_gets_heads_up_and_nudge(self):
        essay = next(e for e in self.events if e.uid == "assign-2@vle")
        heads_up, nudge = studycal.todos_for(essay, NOW.date())
        self.assertEqual(heads_up["when"], "2026-12-14@09:00")
        self.assertEqual(heads_up["deadline"], "2026-12-21")
        self.assertIn("due Mon 21 Dec 13:00", heads_up["title"])
        self.assertEqual(nudge["when"], "2026-12-19@09:00")
        self.assertTrue(nudge["title"].startswith("2 days left: Module 1 essay"))

    def test_close_deadline_starts_today_without_nudge(self):
        soon = studycal.Event(uid="x", title="Quiz due", start=date(2026, 11, 3), extra={"deadline": True})
        [todo] = studycal.todos_for(soon, NOW.date())
        self.assertEqual(todo["when"], "2026-11-02@09:00")
        self.assertEqual(todo["deadline"], "2026-11-03")

    def test_typed_in_deadlines(self):
        cfg = {"deadlines": [{"title": "Module 1 assignment", "due": "2026-12-14", "list": "York MSc"}]}
        events, errors = studycal.collect(cfg, NOW)
        self.assertEqual(errors, [])
        [ev] = events
        self.assertTrue(ev.is_deadline)
        self.assertEqual(studycal.todos_for(ev, NOW.date())[0]["deadline"], "2026-12-14")

    def test_broken_feed_is_reported_not_raised(self):
        def boom(url):
            raise OSError("offline")
        cfg = {"calendars": [{"name": "York MSc", "url": "https://example.invalid/x.ics"}]}
        events, errors = studycal.collect(cfg, NOW, fetcher=boom)
        self.assertEqual(events, [])
        self.assertEqual(errors, ["York MSc: offline"])


class AgentSyncTests(unittest.TestCase):
    def test_sync_opens_things_once_and_remembers(self):
        with tempfile.TemporaryDirectory() as d:
            ics = Path(d, "feed.ics")
            ics.write_text(ICS)
            cfg = {"calendars": [{"name": "York MSc", "url": str(ics), "list": "York MSc"}],
                   "calendar_seen": os.path.join(d, "seen.json")}
            with mock.patch.object(agent.subprocess, "run") as run:
                first = agent.sync_calendars(cfg, NOW)
                second = agent.sync_calendars(cfg, NOW)
            self.assertGreater(first["added"], 0)
            self.assertEqual(second["added"], 0)
            self.assertEqual(run.call_count, 1)
            url = run.call_args[0][0][2]
            self.assertTrue(url.startswith("things:///json?data="))
            todos = json.loads(urllib.parse.unquote(url.split("data=", 1)[1]))
            titles = [t["attributes"]["title"] for t in todos]
            self.assertIn("19:00 Live session: Week 1 welcome", titles)

    def test_no_calendars_configured_does_nothing(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            self.assertEqual(agent.sync_calendars({}, NOW), {})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
