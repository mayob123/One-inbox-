import unittest
from datetime import datetime, timedelta, timezone

from onebox.triage import Message, capture_item, is_scam, normalize_subject, triage

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def msg(subject, addr, name="", days=0, box="INBOX", mid=None):
    return Message(msg_id=mid or f"{subject}-{days}", sender_name=name, sender_addr=addr, subject=subject,
                   received=T0 + timedelta(days=days), mailbox=box, account="mac")


def by_title(items):
    return {i.title: i for i in items}


class TriageTests(unittest.TestCase):
    def test_reply_in_sent_marks_done(self):
        items = by_title(triage([
            msg("2nd interview: Case", "hr@example.dk", "Maria"),
            msg("Re: 2nd interview: Case", "ben@mac.com", box="Sent", days=2),
        ]))
        self.assertEqual(items["2nd interview: Case"].status, "done")
        self.assertIn("You replied", items["2nd interview: Case"].why)

    def test_accepted_invite_marks_done(self):
        items = by_title(triage([
            msg("Invitation: WtD - Ben, Thu Oct 15", "adil@welcometodanmark.dk"),
            msg("Accepted: Invitation: WtD - Ben, Thu Oct 15", "ben@mac.com", box="Sent", days=1),
        ]))
        self.assertEqual(items["Invitation: WtD - Ben, Thu Oct 15"].status, "done")

    def test_later_confirmation_on_same_topic_marks_done(self):
        items = by_title(triage([
            msg("URGENT: University of York - Payment Deadline", "enrolments@online.york.ac.uk",
                "University of York Online Enrolments"),
            msg("Receipt for online payment from eVision", "do-not-reply@notifications.york.ac.uk", days=9),
        ]))
        self.assertEqual(items["URGENT: University of York - Payment Deadline"].status, "done")

    def test_confirmation_on_other_topic_does_not_mark_done(self):
        items = by_title(triage([
            msg("University of York - York Online Joining Instructions", "student-records@york.ac.uk"),
            msg("University of York - Confirmation of Module Registration", "york-online-admin@york.ac.uk", days=8),
        ]))
        self.assertEqual(items["University of York - York Online Joining Instructions"].status, "decide")

    def test_lookalike_brand_is_scam(self):
        m = msg("EasyPark AB-Dit kort er udløbet", "reviews@reviewmgr.com", "Kundeservice")
        self.assertIsNotNone(is_scam(m))
        self.assertEqual(triage([m])[0].status, "scam")

    def test_real_brand_domain_is_not_scam(self):
        self.assertIsNone(is_scam(msg("Your EasyPark receipt", "noreply@easypark.net", "EasyPark")))

    def test_payment_update_request_is_scam(self):
        self.assertEqual(triage([msg("Vigtigt: Opdater dine betalingsoplysninger", "info@reviewmgr.com")])[0].status, "scam")

    def test_newsletters_and_receipts_stay_out_of_the_way(self):
        items = by_title(triage([
            msg("Summer Sale now on!", "emails@euenews.uniqlo.com", "UNIQLO"),
            msg("Rejsekort receipt – Oct 23, 2026", "noreply@rejsekort.dk"),
        ]))
        self.assertEqual(items["Summer Sale now on!"].status, "noise")
        self.assertEqual(items["Rejsekort receipt – Oct 23, 2026"].status, "info")

    def test_bill_needs_a_decision(self):
        it = triage([msg("Your latest bill is here Ben", "noreply@notify.vodafone.co.uk", "Vodafone")])[0]
        self.assertEqual((it.status, it.kind), ("decide", "bill"))
        self.assertTrue(it.task.startswith("Check and pay"))

    def test_danish_mail_is_flagged(self):
        it = triage([msg("Påmindelse fra tandlægen", "info@sehestedtand.dk", "Sehested Tandlægerne")])[0]
        self.assertEqual((it.status, it.lang, it.area), ("decide", "da", "Admin & bills"))

    def test_sent_mail_never_becomes_an_item(self):
        self.assertEqual(triage([msg("Handing in my notice", "ben@mac.com", box="Sent")]), [])

    def test_normalize_subject(self):
        self.assertEqual(normalize_subject("Re: Sv: Fwd: Hello  World"), "hello world")

    def test_ids_are_stable(self):
        a = triage([msg("Hello", "a@b.dk", mid="x1")])[0].id
        b = triage([msg("Hello", "a@b.dk", mid="x1")])[0].id
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{16}$")

    def test_official_post_goes_straight_to_things(self):
        items = by_title(triage([
            msg("Du har modtaget ny post", "noreply@e-boks.dk", "e-Boks"),
            msg("Ny besked fra Lærer", "noreply@aula.dk", "Aula", days=1),
            msg("Du har modtaget Digital Post", "noreply@mit.dk", "Digital Post", days=1),
        ], now=T0 + timedelta(days=2)))
        for title, where in [("Du har modtaget ny post", "e-Boks"), ("Ny besked fra Lærer", "Aula"),
                             ("Du har modtaget Digital Post", "Digital Post")]:
            self.assertEqual((items[title].status, items[title].auto), ("decide", "things"), title)
            self.assertTrue(items[title].task.startswith(f"Read in {where}:"))

    def test_old_or_lookalike_official_post_is_not_auto_added(self):
        items = by_title(triage([
            msg("Ny besked", "noreply@aula.dk", "Aula"),
            msg("Du har ny post i e-Boks", "info@eboks-dk.com", "e-Boks"),
        ], now=T0 + timedelta(days=10)))
        self.assertEqual((items["Ny besked"].status, items["Ny besked"].auto), ("decide", ""))
        self.assertEqual(items["Du har ny post i e-Boks"].status, "scam")

    def test_capture_item(self):
        it = capture_item("  Sign the  Aula form for Friday ", T0)
        self.assertEqual((it.title, it.kind, it.auto, it.source), ("Sign the Aula form for Friday", "school", "things", "capture"))
        self.assertNotEqual(it.id, capture_item("Sign the Aula form for Friday", T0 + timedelta(seconds=1)).id)


if __name__ == "__main__":
    unittest.main()
