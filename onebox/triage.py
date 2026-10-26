"""Sort mail headers into: decide, done, scam, noise or info.

Works on headers only (sender, subject, dates, mailbox), so it runs fast on
the Mac and never needs message bodies. Rules are deliberately simple and
easy to extend: add a sender to a list rather than writing new logic.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

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
    from_addr: str = ""  # sender's address, used to remember "no action needed"

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
    return text if len(text) <= 70 else text[:69] + "…"


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


def triage(messages: list[Message]) -> list[Item]:
    """Turn mail headers into triage items. Sent mail is only used as evidence."""
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
            lang=language(msg), from_addr=msg.sender_addr.lower(),
        )
        scam = is_scam(msg)
        if scam:
            items.append(Item(status="scam", why=scam + " Don't click anything in it.", **base))
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


def summary(items: list[Item]) -> dict:
    counts = {"decide": 0, "done": 0, "scam": 0, "noise": 0, "info": 0}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    return counts


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
