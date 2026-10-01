"""Reply buttons on a booking offer, and the typed "bare yes" that stands
in for a tap (owner decisions 2026-10-01, ARCHITECTURE.md §7).

A reply that prices exactly one quote and ends with the booking offer
("Shall I pass this to a colleague to confirm your booking?") goes out
with two WhatsApp reply buttons: yes and "I have a question". A tap on
yes, or a typed reply that is nothing but a decisive yes, is answered in
code without a model call (services/agent/booking_yes.py); everything
else goes to the model as before.

Pure: no I/O. The button id names the quote it offers
(booking:yes:<quote_id>); it is never trusted on its own --
services/agent/booking_yes.py checks it against the database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from services.agent.fixed_texts import (
    BOOKING_QUESTION_BUTTON,
    BOOKING_YES_BUTTON,
    Language,
    detect_language,
)
from services.agent.llm.dispatch import ARABIC_NORMALIZE_FROM, ARABIC_NORMALIZE_TO
from services.agent.whatsapp_send import REPLY_BUTTONS_BODY_MAX_CHARS, ReplyButton

ButtonChoice = Literal["yes", "question"]

# An offer whose language cannot be told (no letters at all) gets the
# Arabic titles: most customers write Arabic.
DEFAULT_OFFER_LANGUAGE: Language = "ar"

_BUTTON_ID_PATTERN = re.compile(r"\Abooking:(yes|question):([0-9]{1,18})\Z")

# The booking offer that ends a quote reply (prompt.py's quote_reply
# examples): its last line asks a question and names both the colleague
# and the booking, in one of the three languages. "pemesanan" is listed
# beside "pesan" because the prompt's own Indonesian offer ("konfirmasi
# pemesanan") does not contain "pesan".
_OFFER_QUESTION_MARKS = ("?", "؟")
_OFFER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("زميل", ("حجز",)),
    ("colleague", ("book",)),
    ("rekan", ("pesan", "pemesanan")),
)

# The owner's list (2026-10-01), precision over recall: exactly one of
# these, after _normalized, with nothing else. Every other reply -- a
# second word, a courtesy, a question mark, a digit, another emoji -- goes
# to the model. Each entry is already in its normalized form.
BARE_YES_WORDS = frozenset(
    {
        "ايه",
        "ايوه",
        "نعم",
        "موافق",
        "احجز",
        "اكيد",
        "تم",
        "yes",
        "ok",
        "sure",
        "confirm",
        "iya",
        "ya",
        "boleh",
        "oke",
    }
)
# Thumbs up alone, in any skin tone, or with the emoji variation selector.
_THUMBS_UP = re.compile("\U0001f44d(?:[\U0001f3fb-\U0001f3ff]|️)?")
_TRAILING_PUNCTUATION = ".!"

_TATWEEL = chr(0x0640)  # ARABIC TATWEEL
_KEPT_LETTER_COUNT = len(ARABIC_NORMALIZE_TO)
_ARABIC_LETTER_MAPPING = str.maketrans(
    ARABIC_NORMALIZE_FROM[:_KEPT_LETTER_COUNT],
    ARABIC_NORMALIZE_TO,
    ARABIC_NORMALIZE_FROM[_KEPT_LETTER_COUNT:] + _TATWEEL,
)


@dataclass(frozen=True)
class ButtonTap:
    """A tapped reply button as WhatsApp reports it: the button's id and
    title, and context.id -- the WhatsApp id of the message that carried
    the button (None when the webhook did not include it)."""

    button_id: str
    title: str
    context_message_id: str | None


@dataclass(frozen=True)
class ParsedButtonId:
    choice: ButtonChoice
    quote_id: int


@dataclass(frozen=True)
class BookingOfferButtons:
    """The buttons that go out with a booking offer for quote_id."""

    quote_id: int
    buttons: tuple[ReplyButton, ...]


def button_id(choice: ButtonChoice, quote_id: int) -> str:
    """The id of the `choice` button on the offer for quote_id."""
    return f"booking:{choice}:{quote_id}"


def parse_button_id(raw_id: str) -> ParsedButtonId | None:
    """The choice and quote id in a button id this module made, or None
    for anything else."""
    match = _BUTTON_ID_PATTERN.fullmatch(raw_id)
    if match is None:
        return None
    choice: ButtonChoice = "yes" if match.group(1) == "yes" else "question"
    return ParsedButtonId(choice=choice, quote_id=int(match.group(2)))


def booking_offer_buttons(quote_id: int, language: Language) -> BookingOfferButtons:
    """The yes and question buttons for quote_id, titled in `language`."""
    return BookingOfferButtons(
        quote_id=quote_id,
        buttons=(
            ReplyButton(
                button_id=button_id("yes", quote_id),
                title=BOOKING_YES_BUTTON.render(language),
            ),
            ReplyButton(
                button_id=button_id("question", quote_id),
                title=BOOKING_QUESTION_BUTTON.render(language),
            ),
        ),
    )


def offer_language(text: str) -> Language:
    """The language of an offer, for its buttons and for the answer to
    them: the offer's own, never the customer's one-word reply ("ok" reads
    as English in any conversation)."""
    return detect_language(text) or DEFAULT_OFFER_LANGUAGE


def ends_with_booking_offer(text: str) -> bool:
    """Whether text's last line is the booking offer: a question naming
    both the colleague and the booking (_OFFER_MARKERS)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    last_line = _normalized(lines[-1])
    if not last_line.endswith(_OFFER_QUESTION_MARKS):
        return False
    return any(
        colleague in last_line and any(booking in last_line for booking in bookings)
        for colleague, bookings in _OFFER_MARKERS
    )


def buttons_for_reply(
    text: str, priced_quote_ids: tuple[int, ...]
) -> BookingOfferButtons | None:
    """The buttons a model reply goes out with, or None for plain text.
    Only when the turn priced exactly one quote (two options offered at
    once leave the choice to the model), the reply ends with the booking
    offer, and it fits a reply-buttons body."""
    if len(priced_quote_ids) != 1:
        return None
    if len(text) > REPLY_BUTTONS_BODY_MAX_CHARS or not ends_with_booking_offer(text):
        return None
    return booking_offer_buttons(priced_quote_ids[0], offer_language(text))


def text_with_button_titles(body: str, offer: BookingOfferButtons | None) -> str:
    """Everything the customer sees: the body, then each button title on a
    line of its own -- what the output guard checks (CLAUDE.md rule 8)."""
    if offer is None:
        return body
    titles = "\n".join(button.title for button in offer.buttons)
    return f"{body}\n\n{titles}"


def is_bare_decisive_yes(text: str) -> bool:
    """Whether a typed reply is nothing but a decisive yes: one of
    BARE_YES_WORDS, or a thumbs up, after _normalized."""
    normalized = _normalized(text).rstrip(_TRAILING_PUNCTUATION)
    return normalized in BARE_YES_WORDS or _THUMBS_UP.fullmatch(normalized) is not None


def _normalized(text: str) -> str:
    """NFKC, lower case, and Arabic spelling variants collapsed: tatweel
    and diacritics removed, alef forms unified, taa marbuta and alef
    maksura mapped as search_hotels maps them. Trimmed."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return folded.translate(_ARABIC_LETTER_MAPPING).strip()
