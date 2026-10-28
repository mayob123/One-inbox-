"""Sort mail headers into: decide, done, scam, noise or info.

Works on headers only (sender, subject, dates, mailbox), so it runs fast on
the Mac and never needs message bodies. Rules are deliberately simple and
easy to extend: add a sender to a list rather than writing new logic.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

# --- word and sender lists -------------------------------------------------

NOISE_DOMAINS = {
    "klaviyomail.com", "sailthru.com", "substack.com", "mcdlv.net", "rsgsv.net",
    "euenews.uniqlo.com", "news.hm.com", "postcode.co.uk", "postcodelottery.co",
    "e.worldathletics.org", "sender.skyscanner.com", "mail.theresanaiforthat.com",
    "newsletter.thetimes.com", "comms.activision.com", "club21.com", "aftermiles.com",
    "communications.paypal.com", "watery.dk",
}
NOISE_LOCALPARTS = {"newsletter", "news", "marketing", "promo", "offers", "hi", "yo", "emily"}
NOISE_SUBJECT = re.compile(
    r"\b(sale|% off|discount|tilbud|nyhedsbrev|newsletter|webinar|early access|"
    r"price drop|wishlist|editor.s choice|briefing|advent calendar|is hiring|"
    r"search appearances|terms and conditions|looking for a new job|have you applied for this job|"
    r"is looking for an?)\b",
    re.I,
)
# Companies that only ever send promotions. Matched against the sender's name or domain.
NOISE_SENDERS = {
    "pinchme", "tracksmith", "parallels", "wonderbly", "nordisk film", "anbernic", "adidas",
    "snow peak", "analogue", "goodreads", "flare audio", "google health", "st lukes notices",
    "amazon.de rezensionen", "super duolingo",
}
# Marketing wording. Only applied to mail that isn't school, admin, job, bills, etc.
PROMO_SUBJECT = re.compile(
    r"(\bfree\b|gift card|\bsamples?\b|save (up to|on|\d)|\d+\s?%|credit to spend|introducing|"
    r"don.t miss|exclusive|pre-?order|available (now|today)|new arrivals|\bnew!|ever wonder|"
    r"could be earning|rate (your experience|us)|how would you rate|trustpilot|\bperk\b|"
    r"looking for a new job|have you applied|is looking for|glip af|forudbestil|unlock|"
    r"final days|loyalty|join us|cart\b|half stars|connect .* to )",
    re.I,
)
# Automatic notices: worth seeing in the folded list, never a task.
AUTO_NOTICE = re.compile(
    r"(run failed|run cancelled|build (failed|passed|succeeded)|workflow run|dependabot|"
    r"invited you to test|autosvar|auto-?reply|automatic reply|out of office|fraværende|"
    r"\bwelcome\b|account .*(has been )?created|rental was ended|\bstatement\b|"
    r"an update (to|on) our|gdpr|sunsetting|sorry to see you go|request received|"
    r"thank you for (applying|your application|your interest)|tak for din ansøgning|"
    r"new opportunity at|nouvelle opportunité|church notices|order is complete|"
    r"handling your personal data|unauthori[sz]ed .* notification)",
    re.I,
)
RECEIPT_SUBJECT = re.compile(
    r"\b(receipts?|kvittering|transaction statement|order confirmation|ordrebekræftelse|"
    r"thank you for your .*purchase|tak for din ordre)\b",
    re.I,
)
SECURITY_NOTICE = re.compile(
    r"\b(security alert|new sign-in|new login|passkey (was added|is now activated)|ssh .*key was added|"
    r"shared some google account data|one-time code|single-use code|security code|verification code|verify your email|new app(\(s\))? connected|"
    r"did you recently sign in|password (change|reset)|reset your .*password|"
    r"account information has been changed|secure link to)",
    re.I,
)
CONFIRM_SUBJECT = re.compile(
    r"\b(confirm(ed|ation)?|bekræft(else|et)?|receipt|kvittering|registered|registration|"
    r"you have sent|we have received|modtaget)\b",
    re.I,
)
BILL_SUBJECT = re.compile(
    r"\b(bill|invoice|faktura|regning|betalingsservice|payment due|opkrævning|"
    r"payment deadline|due date)\b",
    re.I,
)
DANISH_HINTS = re.compile(
    r"[æøå]|\b(og|til|din|dine|vi|har|brug|lektier|besked|brev|påmindelse|"
    r"spørgeskema|vigtigt|manglende|oplysninger)\b",
    re.I,
)

# Brands that phishing mail pretends to be, and the domains they really use.
BRANDS = {
    "easypark": ("easypark.net", "easypark.com", "easypark.dk"),
    "mobilepay": ("mobilepay.dk", "danskebank.dk"),
    "postnord": ("postnord.dk", "postnord.com"),
    "skat": ("skat.dk", "sktst.dk"),
    "e-boks": ("e-boks.dk", "e-boks.com"),
    "netflix": ("netflix.com",),
    "apple": ("apple.com", "icloud.com", "email.apple.com"),
    "paypal": ("paypal.com", "paypal.dk"),
    "nets": ("nets.eu",),
    "dhl": ("dhl.com", "dhl.dk"),
}
SCAM_SUBJECT = re.compile(
    r"(kort er udløbet|opdater dine? betalingsoplysninger|update (your )?payment|"
    r"verify your account|bekræft din konto|konto er spærret|account (is )?suspended|"
    r"pakke afventer|package (is )?waiting)",
    re.I,
)

# (pattern on "sender subject", area, kind). First match wins.
AREA_RULES = [
    (r"aula", "Kids & school", "school"),
    (r"e-boks|digital post|mit\.dk", "Admin & bills", "admin"),
    (r"york\.ac\.uk|university of york", "MSc York", "msc"),
    (r"a-kasse|aka\.dk|akademikernes", "Admin & bills", "admin"),
    (r"interview|application|job|hr-manager|recruit|stilling|ansøgning", "Job search", "job"),
    (r"lektier|dansk|danish|welcometodanmark|sprogskole", "Danish", "danish"),
    (r"flight|booking|easyjet|sas\.dk|norwegian|ryanair|hotel|rejse", "Travel", "travel"),
    (r"wedding|save the date|bryllup|birthday|fødselsdag", "Friends", "friend"),
    (r"tandl|doctor|læge|dentist", "Admin & bills", "admin"),
]

# Danish official post. An alert email only says "something new is waiting", so it is not a to-do
# of its own: unread alerts are counted per source on the phone, and the Mac keeps one rolling
# "Check Aula (3 new)" to-do in Things Today (agent.sync_post). Matched on the real sender domain
# only, so lookalikes don't count.
OFFICIAL_POST = {
    "e-Boks": ("e-boks.dk", "e-boks.com"),
    "Digital Post": ("mit.dk", "digitalpost.dk", "borger.dk"),
    "Aula": ("aula.dk",),
}
AUTO_ADD_DAYS = 7  # older alerts and homework stay on the page, so a first sync doesn't flood Things
# Homework from the Danish teacher goes straight to Things for today.
HOMEWORK = re.compile(r"lektie|homework|hjemmearbejde|opgave", re.I)

SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|fw|sv|vs|aw|vb|accepted|declined|opdateret invitation)\s*:\s*)+", re.I)


@dataclass
class Message:
    """One mail header, as read from Mail on the Mac."""

    msg_id: str
    sender_name: str
    sender_addr: str
    subject: str
    received: datetime
    mailbox: str = "INBOX"  # INBOX, Archive, Sent, ...
    account: str = ""
    unread: bool = False

    @property
    def is_sent(self) -> bool:
        return "sent" in self.mailbox.lower()

    @property
    def domain(self) -> str:
        return self.sender_addr.rsplit("@", 1)[-1].lower() if "@" in self.sender_addr else ""


@dataclass
class Item:
    """One thing for the triage page."""

    id: str
    status: str  # decide | done | scam | noise | info
    title: str
    task: str
    area: str
    kind: str
    source: str
    sender: str
    received: str
    why: str
    lang: str = "en"
    evidence: list = field(default_factory=list)
    auto: str = ""  # "things": the Pi queues it for Things as soon as it first sees it
    post: str = ""  # "Aula", "e-Boks" or "Digital Post": an official post alert, counted rather than added
    from_addr: str = ""  # sender's address, used to remember "no action needed"
    ref: str = ""  # the message in Mail (account:rowid), so the Mac can read its body for details

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_subject(subject: str) -> str:
    s = SUBJECT_PREFIX.sub("", subject or "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def item_id(msg: Message) -> str:
    return hashlib.sha1(f"{msg.account}|{msg.msg_id}".encode()).hexdigest()[:16]


def _domain_matches(domain: str, allowed: tuple) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def is_scam(msg: Message) -> str | None:
    """Return a reason when the mail claims a brand its sender domain doesn't own."""
    hay = f"{msg.sender_name} {msg.subject}".lower()
    for brand, domains in BRANDS.items():
        if brand in hay and not _domain_matches(msg.domain, domains):
            return f"Says it's from {brand.title()}, but was sent from {msg.domain or 'an unknown address'}."
    if SCAM_SUBJECT.search(msg.subject):
        return f"Asks you to update payment or account details, sent from {msg.domain or 'an unknown address'}."
    return None


def official_post(msg: Message) -> str | None:
    """e-Boks, Digital Post or Aula, judged by the sender's real domain."""
    for name, domains in OFFICIAL_POST.items():
        if _domain_matches(msg.domain, domains):
            return name
    return None


def is_noise(msg: Message) -> bool:
    local = msg.sender_addr.split("@", 1)[0].lower()
    if _domain_matches(msg.domain, tuple(NOISE_DOMAINS)):
        return True
    if local in NOISE_LOCALPARTS:
        return True
    who = f"{msg.sender_name} {msg.domain}".lower()
    if any(name in who for name in NOISE_SENDERS):
        return True
    return bool(NOISE_SUBJECT.search(msg.subject))


EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def is_promo(msg: Message, kind: str) -> bool:
    """Marketing from a company: emoji or sales wording in a mail that isn't about something we track."""
    return kind == "other" and bool(PROMO_SUBJECT.search(msg.subject) or EMOJI.search(msg.subject))


def classify_area(msg: Message) -> tuple[str, str]:
    hay = f"{msg.sender_name} {msg.sender_addr} {msg.subject}".lower()
    if BILL_SUBJECT.search(msg.subject):
        return "Admin & bills", "bill"
    for pattern, area, kind in AREA_RULES:
        if re.search(pattern, hay):
            return area, kind
    return "Inbox", "other"


def language(msg: Message) -> str:
    return "da" if DANISH_HINTS.search(msg.subject) else "en"


def suggest_task(msg: Message, kind: str) -> str:
    who = (msg.sender_name or msg.sender_addr).split("<")[0].strip().strip('"') or "sender"
    subject = SUBJECT_PREFIX.sub("", msg.subject).strip() or "(no subject)"
    if kind == "bill":
        text = f"Check and pay: {who}"
    elif kind == "friend":
        text = f"Reply to {who}: {subject}"
    elif kind == "travel":
        text = f"Put in calendar: {subject}"
    else:
        text = f"Deal with: {subject}"
    return _cut(text)


def _cut(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def find_done_evidence(msg: Message, later: list[Message]) -> list[str]:
    """Look for signs Ben already dealt with msg: a reply, an acceptance or a confirmation."""
    topic = normalize_subject(msg.subject)
    if not topic:
        return []
    evidence = []
    for other in later:
        if other.received <= msg.received:
            continue
        other_topic = normalize_subject(other.subject)
        same_topic = other_topic == topic or (len(topic) > 12 and topic in other_topic)
        when = other.received.strftime("%-d %b")
        if other.is_sent and same_topic:
            verb = "accepted" if other.subject.lower().startswith("accepted") else "replied"
            evidence.append(f"You {verb} on {when}.")
        elif not other.is_sent and other.domain and _same_org(other.domain, msg.domain) \
                and CONFIRM_SUBJECT.search(other.subject) and _shared_keywords(msg, other):
            evidence.append(f"Confirmed by a later email on {when}: \"{other.subject[:60]}\".")
    return evidence


STOPWORDS = {"about", "after", "before", "their", "there", "these", "which", "would", "your", "yours",
             "please", "online", "update", "required", "action", "urgent", "reminder", "confirmation",
             "receipt", "registration", "university", "universitet", "school", "skole",
             "kommune", "student"}


def _keywords(msg: Message) -> set[str]:
    org = set(re.findall(r"[a-zæøå]{5,}", f"{msg.sender_name} {msg.domain}".lower()))
    words = set(re.findall(r"[a-zæøå]{5,}", msg.subject.lower()))
    return words - STOPWORDS - org


def _shared_keywords(a: Message, b: Message) -> bool:
    """A later confirmation only counts if it is about the same thing (shares a real word)."""
    return bool(_keywords(a) & _keywords(b))


def _same_org(a: str, b: str) -> bool:
    def root(d: str) -> str:
        parts = d.split(".")
        # york.ac.uk style
        if len(parts) >= 3 and parts[-2] in {"ac", "co", "gov", "org"}:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])
    return root(a) == root(b)


def triage(messages: list[Message], now: datetime | None = None) -> list[Item]:
    """Turn mail headers into triage items. Sent mail is only used as evidence."""
    now = now or utcnow()
    inbound = sorted((m for m in messages if not m.is_sent), key=lambda m: m.received)
    sent = [m for m in messages if m.is_sent]
    latest = {(normalize_subject(m.subject), m.sender_addr.lower()): m for m in inbound}  # newest wins
    items = []
    for msg in inbound:
        area, kind = classify_area(msg)
        base = dict(
            id=item_id(msg), title=msg.subject or "(no subject)", task=suggest_task(msg, kind),
            area=area, kind=kind, source=msg.account or "mail",
            sender=msg.sender_name or msg.sender_addr, received=msg.received.isoformat(),
            lang=language(msg), from_addr=msg.sender_addr.lower(), ref=msg.msg_id,
        )
        scam = is_scam(msg)
        if scam:
            items.append(Item(status="scam", why=scam + " Don't click anything in it.", **base))
            continue
        post = official_post(msg)
        if post:  # before the noise and confirmation rules: "Du har modtaget ny post" is not a receipt
            evidence = find_done_evidence(msg, sent)
            if evidence:
                items.append(Item(status="done", why=" ".join(evidence), evidence=evidence, **base))
                continue
            fresh = now - msg.received <= timedelta(days=AUTO_ADD_DAYS)
            base["task"] = _cut(f"Read in {post}: {base['title']}")
            why = f"New in {post}. " + (f"Counted in today's Check {post} to-do." if fresh else "Older than a week, so not counted.")
            items.append(Item(status="decide", why=why, post=post if fresh else "", **base))
            continue
        if kind == "danish" and HOMEWORK.search(msg.subject):
            fresh = now - msg.received <= timedelta(days=AUTO_ADD_DAYS)
            base["task"] = _cut(f"Danish homework: {normalize_subject(msg.subject).capitalize()}")
            why = f"Homework from {base['sender']}. " + ("Added to Things for today, with its links."
                                                        if fresh else "Older than a week, so not added.")
            items.append(Item(status="decide", why=why, auto="things" if fresh else "", **base))
            continue
        if SECURITY_NOTICE.search(msg.subject):
            items.append(Item(status="info", why="Account security notice. Ignore it if this was you.", **base))
            continue
        if is_noise(msg):
            items.append(Item(status="noise", why="Newsletter or promotion.", **base))
            continue
        if is_promo(msg, kind):
            items.append(Item(status="noise", why="Looks like marketing.", **base))
            continue
        if AUTO_NOTICE.search(msg.subject) and kind not in {"school", "admin", "bill", "friend"}:
            items.append(Item(status="info", why="Automatic notice. Nothing to do.", **base))
            continue
        if RECEIPT_SUBJECT.search(f"{msg.sender_name} {msg.subject}"):
            items.append(Item(status="info", why="Receipt. Nothing to do.", **base))
            continue
        if CONFIRM_SUBJECT.search(msg.subject) and not BILL_SUBJECT.search(msg.subject):
            items.append(Item(status="info", why="A confirmation. Nothing to do.", **base))
            continue
        evidence = find_done_evidence(msg, sent + inbound)
        if evidence:
            items.append(Item(status="done", why=" ".join(evidence), evidence=evidence, **base))
            continue
        key = (normalize_subject(msg.subject), msg.sender_addr.lower())
        if key in latest and latest[key] is not msg:
            items.append(Item(status="info", why="An earlier copy of a newer email.", **base))
            continue
        why = "No reply or confirmation found yet."
        if base["lang"] == "da":
            why = "In Danish. " + why
        items.append(Item(status="decide", why=why, **base))
    return items


# Spoken or typed prefixes that send a capture somewhere other than Today: "errand: pick up frames".
CAPTURE_PLACES = {
    "errand": {"when": "anytime", "tags": ["Errands"]},
    "house": {"when": "anytime", "list": "Around the house"},
    "someday": {"when": "someday"},
}
CAPTURE_PREFIX = re.compile(r"^\s*(" + "|".join(CAPTURE_PLACES) + r")s?\s*[:,-]\s*", re.I)
WHENS = {"today", "anytime", "someday"}


def capture_item(text: str, now: datetime) -> Item:
    """A to-do Ben typed or dictated himself. It goes to Things for today."""
    text = re.sub(r"\s+", " ", text).strip()
    msg = Message(msg_id=f"capture|{now.isoformat()}|{text}", sender_name="", sender_addr="",
                  subject=text, received=now, account="capture")
    area, kind = classify_area(msg)
    return Item(id=item_id(msg), status="decide", title=text, task=_cut(text, 200), area=area, kind=kind,
                source="capture", sender="You", received=now.isoformat(timespec="seconds"),
                why="You added this.", lang=language(msg), auto="things")


def capture(text: str, now: datetime, place: dict | None = None) -> dict:
    """capture_item, sent somewhere other than Today when the text starts with a place ("errand: …",
    "house: …", "someday: …") or the phone says where (`place`: when, tags, list, deadline)."""
    text = re.sub(r"\s+", " ", text).strip()
    where = {k: v for k, v in (place or {}).items() if v not in (None, "", [])}
    m = CAPTURE_PREFIX.match(text)
    if m and text[m.end():].strip():
        where = {**CAPTURE_PLACES[m.group(1).lower()], **where}
        text = text[m.end():].strip()
        text = text[0].upper() + text[1:]
    d = capture_item(text, now).to_dict()
    if where.get("when") in WHENS:
        d["when"] = where["when"]
    if isinstance(where.get("tags"), list):
        d["tags"] = [str(t).strip()[:40] for t in where["tags"] if str(t).strip()][:5]
    if isinstance(where.get("list"), str) and where["list"].strip():
        d["list"] = where["list"].strip()[:80]
    if isinstance(where.get("deadline"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", where["deadline"]):
        d["deadline"] = where["deadline"]
    return d


def summary(items: list[Item]) -> dict:
    counts = {"decide": 0, "done": 0, "scam": 0, "noise": 0, "info": 0}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    return counts


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
