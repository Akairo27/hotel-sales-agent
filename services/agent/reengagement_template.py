"""The re-engagement template (staff notification step 3, PR C; owner
decisions 2026-10-02 and 2026-10-03; ARCHITECTURE.md §7).

Outside WhatsApp's 24-hour customer service window only an approved
template message is accepted. A staff member holding a takeover sends this
one from the dashboard; the agent renders it here and sends it through
whatsapp_send.py, through the output guard's staff-reply mode and recorded
like any other outbound message (services/agent/staff_reply.py).

It is switched off until both template names are set in agent.env:
WHATSAPP_REENGAGEMENT_TEMPLATE (names the hotel, {{1}}) and
WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL (the variant without a hotel name,
used when the conversation names none -- owner decision 2026-10-02). Each
template exists in three languages in Meta's template manager, under the
language codes below, and the texts here are the ones approved there: the
recorded message shows what the customer was sent, so a template edited
in Meta without editing this file leaves the recorded text wrong.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import psycopg

from services.agent.fixed_texts import Language, customer_language
from services.agent.whatsapp_send import TEMPLATE_NAME_PATTERN

logger = logging.getLogger(__name__)

ENV_TEMPLATE_WITH_HOTEL = "WHATSAPP_REENGAGEMENT_TEMPLATE"
ENV_TEMPLATE_NO_HOTEL = "WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL"

# The language code each translation is created under in Meta's template
# manager. English is "en", not a regional code: a template created under
# another code must be recreated, or this table changed with it.
LANGUAGE_CODES: dict[Language, str] = {"ar": "ar", "en": "en", "id": "id"}

# What a customer whose language is unknown gets: Arabic alone (a template
# is one language; the bilingual rendering of the fixed texts does not fit).
DEFAULT_LANGUAGE: Language = "ar"

# Owner-approved 2026-10-01 (with the hotel's name as {{1}}); the variant
# without it drops «في {{1}}» and its equivalents.
_TEXT_WITH_HOTEL: dict[Language, str] = {
    "ar": (
        "حاولنا نتواصل معك بخصوص طلبك في {hotel}. ردّ على هذه الرسالة متى ما "
        "ناسبك ونكمل معك إن شاء الله."
    ),
    "en": (
        "We tried to reach you about your request at {hotel}. Reply to this "
        "message whenever it suits you and we will continue from there."
    ),
    "id": (
        "Kami mencoba menghubungi Anda tentang permintaan Anda di {hotel}. "
        "Balas pesan ini kapan saja, dan kami akan melanjutkannya."
    ),
}
_TEXT_NO_HOTEL: dict[Language, str] = {
    "ar": (
        "حاولنا نتواصل معك بخصوص طلبك. ردّ على هذه الرسالة متى ما ناسبك ونكمل "
        "معك إن شاء الله."
    ),
    "en": (
        "We tried to reach you about your request. Reply to this message "
        "whenever it suits you and we will continue from there."
    ),
    "id": (
        "Kami mencoba menghubungi Anda tentang permintaan Anda. Balas pesan "
        "ini kapan saja, dan kami akan melanjutkannya."
    ),
}

# A hotel name longer than this is cut: it is a template variable, and a
# template body has to stay readable.
HOTEL_NAME_MAX_CHARS = 100


class ReengagementConfigurationError(Exception):
    """Raised by load_reengagement_settings when exactly one of the two
    template names is set, or one is not a Meta template name."""


@dataclass(frozen=True)
class ReengagementSettings:
    template_with_hotel: str
    template_no_hotel: str


@dataclass(frozen=True)
class RenderedReengagement:
    """The template message to send and the text it shows the customer: the
    text is what the guard inspects and what is recorded."""

    template_name: str
    language_code: str
    body_parameters: tuple[str, ...]
    text: str


def load_reengagement_settings() -> ReengagementSettings | None:
    """The two template names, or None -- the feature is off -- when
    neither is set.

    Raises:
        ReengagementConfigurationError: only one is set, or a name is not a
            Meta template name (lowercase letters, digits, underscores).
    """
    with_hotel = os.environ.get(ENV_TEMPLATE_WITH_HOTEL, "").strip()
    no_hotel = os.environ.get(ENV_TEMPLATE_NO_HOTEL, "").strip()
    if not with_hotel and not no_hotel:
        return None
    if not with_hotel or not no_hotel:
        raise ReengagementConfigurationError(
            f"set both {ENV_TEMPLATE_WITH_HOTEL} and "
            f"{ENV_TEMPLATE_NO_HOTEL}, or neither"
        )
    for name in (with_hotel, no_hotel):
        if TEMPLATE_NAME_PATTERN.fullmatch(name) is None:
            raise ReengagementConfigurationError(
                "a template name is not a Meta template name"
            )
    return ReengagementSettings(
        template_with_hotel=with_hotel, template_no_hotel=no_hotel
    )


def reengagement_settings_or_none() -> ReengagementSettings | None:
    """load_reengagement_settings, with a misconfiguration logged at ERROR
    and treated as off: a half-set feature must never send."""
    try:
        return load_reengagement_settings()
    except ReengagementConfigurationError as exc:
        logger.error(
            json.dumps(
                {
                    "event": "reengagement_template_misconfigured",
                    "exception_message": str(exc),
                }
            )
        )
        return None


def clean_hotel_name(name: str | None) -> str | None:
    """The hotel name as one template parameter: whitespace (newlines and
    tabs included) collapsed to single spaces, cut at HOTEL_NAME_MAX_CHARS;
    None when nothing is left."""
    if name is None:
        return None
    cleaned = " ".join(name.split())[:HOTEL_NAME_MAX_CHARS].strip()
    return cleaned or None


def lookup_hotel_name(
    conn: psycopg.Connection[Any], hotel_id: int | None
) -> str | None:
    """The name of hotel_id as a template parameter, or None when there is no
    hotel (the variant without a name is used) or it is gone.

    Raises:
        psycopg.Error: the read failed.
    """
    if hotel_id is None:
        return None
    row = conn.execute(
        "SELECT hotel_name FROM hotels WHERE id = %s", (hotel_id,)
    ).fetchone()
    return clean_hotel_name(row[0]) if row is not None else None


def render_reengagement(
    settings: ReengagementSettings, *, language: Language | None, hotel_name: str | None
) -> RenderedReengagement:
    """The template to send for a customer in `language` (Arabic when
    unknown), naming hotel_name or, when there is none, the variant without
    a hotel. hotel_name must already be clean_hotel_name's."""
    chosen = language or DEFAULT_LANGUAGE
    if hotel_name is None:
        return RenderedReengagement(
            template_name=settings.template_no_hotel,
            language_code=LANGUAGE_CODES[chosen],
            body_parameters=(),
            text=_TEXT_NO_HOTEL[chosen],
        )
    return RenderedReengagement(
        template_name=settings.template_with_hotel,
        language_code=LANGUAGE_CODES[chosen],
        body_parameters=(hotel_name,),
        text=_TEXT_WITH_HOTEL[chosen].format(hotel=hotel_name),
    )


def render_for_conversation(
    conn: psycopg.Connection[Any],
    settings: ReengagementSettings,
    *,
    conversation_id: int,
    template_hotel_id: int | None,
) -> RenderedReengagement:
    """The template for a conversation: in the language of the customer's
    latest written message, naming the hotel the staff reply chose.

    Raises:
        psycopg.Error: a read failed.
    """
    return render_reengagement(
        settings,
        language=customer_language(conn, conversation_id),
        hotel_name=lookup_hotel_name(conn, template_hotel_id),
    )
