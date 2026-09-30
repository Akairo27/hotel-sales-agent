"""The fixed, non-LLM texts sent to a customer — the fallback and the
"please type" reply — and the choice of language they go out in (owner
decisions, 2026-09-30; ARCHITECTURE.md §7).

Each text exists in Arabic (simple "white" Arabic understood across the
Arab world, with a light Gulf touch -- owner decision 2026-09-30, the same
register as prompt.py's arabic_register), English and Indonesian. It is
sent in the customer's language alone when that is known from their
latest written message, and as Arabic then English when it is not (no
written message yet, or the lookup failed). Every rendering is still
checked by the output guard like any outbound text (CLAUDE.md rule 8), and
none contains a digit of any kind, so none can ever be a candidate amount
(services/agent/output_guard/extraction.py finds nothing to extract from
text without digits): every rendering is provably, not just presumably,
always allowed. tests/unit/test_fixed_texts.py checks the digits
character by character; tests/integration/test_output_guard.py checks
every rendering against the guard itself.

The fallback was bilingual and never language-detected until 2026-09-30,
on the reasoning that detection is one more thing that can go wrong at
the moment something already did. The owner chose one language; the
lookup failing still falls back to the bilingual text, never to silence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

Language = Literal["ar", "en", "id"]


@dataclass(frozen=True)
class FixedText:
    arabic: str
    english: str
    indonesian: str

    def render(self, language: Language | None) -> str:
        """The text in `language` alone; Arabic then English when the
        language is unknown."""
        if language == "ar":
            return self.arabic
        if language == "en":
            return self.english
        if language == "id":
            return self.indonesian
        return f"{self.arabic}\n{self.english}"


# What the customer gets instead of a reply that could not be sent -- a
# blocked, blank or unsendable reply, or any failed turn (webhook.py's
# _escalate_and_notify). Fixed by design: the model is never asked to
# rephrase a blocked reply (an unmanipulated second attempt is not
# guaranteed, and it would spend more tokens on a turn that already
# failed), and "a colleague will follow up" matches prompt.py's
# no_booking_actions rule.
FALLBACK = FixedText(
    arabic=(
        "لحظة لو سمحت، خلّيني أتأكد من طلبك مع زميلي، ويتواصل معك قريباً إن شاء الله."
    ),
    english=(
        "One moment — I need to double-check this with a colleague, and "
        "they'll follow up with you shortly."
    ),
    indonesian=(
        "Mohon tunggu sebentar — saya perlu memastikan hal ini dengan rekan "
        "saya, dan dia akan segera menghubungi Anda."
    ),
)

# The reply to a voice note or an image: the agent reads text only.
PLEASE_TYPE = FixedText(
    arabic=(
        "المعذرة، ما أقدر أسمع الرسائل الصوتية ولا أشوف الصور حالياً. اكتب لي "
        "طلبك وأخدمك مباشرة."
    ),
    english=(
        "Sorry, I can't read voice notes or images yet. Please type your "
        "request and I'll help you right away."
    ),
    indonesian=(
        "Maaf, saya belum bisa membaca pesan suara atau gambar. Silakan "
        "ketik permintaan Anda, dan saya akan langsung membantu."
    ),
)

# Owner-approved (2026-09-30): Latin-script text with any of these whole
# words is Indonesian, otherwise English. Deliberately no word English
# shares (such as "hotel").
INDONESIAN_WORDS = frozenset(
    {
        "saya",
        "berapa",
        "kamar",
        "harga",
        "tanggal",
        "bisa",
        "apakah",
        "halo",
        "untuk",
        "dari",
        "malam",
        "tidak",
        "ada",
        "mau",
        "dengan",
    }
)
_INDONESIAN_PHRASE = re.compile(r"\bterima\s+kasih\b", re.IGNORECASE)
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_ARABIC_LETTER = re.compile("[ؠ-يٮ-ۓۺ-ۿ]")
_LATIN_LETTER = re.compile("[A-Za-z]")

# The body webhook.py stores in place of media: "[audio message]", plus a
# caption when there is one. Defined here, the one place that writes it
# (media_placeholder) and the one place that reads it (customer_language).
_MEDIA_PLACEHOLDER_PATTERN = r"^\[[a-z_]+ message\]"


def media_placeholder(message_type: str) -> str:
    """The stored body standing in for a media message of message_type."""
    return f"[{message_type} message]"


def detect_language(text: str) -> Language | None:
    """The language of one written message, by script: mostly Arabic
    letters is Arabic (a tie counts as Arabic); otherwise Latin letters
    are Indonesian when any INDONESIAN_WORDS word (or "terima kasih")
    appears, else English. None when the text has no letters at all."""
    arabic = len(_ARABIC_LETTER.findall(text))
    latin = len(_LATIN_LETTER.findall(text))
    if arabic == 0 and latin == 0:
        return None
    if arabic >= latin:
        return "ar"
    words = {word.lower() for word in _LATIN_WORD.findall(text)}
    if words & INDONESIAN_WORDS or _INDONESIAN_PHRASE.search(text):
        return "id"
    return "en"


def customer_language(
    conn: psycopg.Connection[Any], conversation_id: int
) -> Language | None:
    """The language of the customer's latest written message in the whole
    conversation (media placeholders skipped), or None when there is none.

    Raises:
        psycopg.Error: the read failed -- the caller sends the bilingual
            text instead (webhook._send_fallback_or_log_failure).
    """
    row = conn.execute(
        "SELECT body FROM messages WHERE conversation_id = %s "
        "AND direction = 'inbound' AND body !~ %s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (conversation_id, _MEDIA_PLACEHOLDER_PATTERN),
    ).fetchone()
    if row is None:
        return None
    body: str = row[0]
    return detect_language(body)
