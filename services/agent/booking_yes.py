"""Decides whether a customer's message is a booking yes that code answers
without the model (owner decisions 2026-10-01, ARCHITECTURE.md §7): a tap
on a booking offer's yes button, or a typed bare yes
(booking_buttons.is_bare_decisive_yes) that directly answers the offer.

Every check is on the database; the button id is never trusted alone. A
tap on yes passes when:
  1. its id is one this system made (booking_buttons.parse_button_id);
  2. context.id is one of our own outbound messages in this conversation;
  3. the quote belongs to this conversation;
  4. the quote is from the current session and still valid (made less
     than quote_validity ago, the output guard's own window);
  5. no newer quote exists in the session.
Checks 1-3 failing is not a real customer action (ButtonMismatch: the
fallback and an escalation). 4 failing goes to the model, which gives a
fresh price. 5 failing offers the newer price (OfferNewerPrice) when it is
still valid and the only quote of its turn, and otherwise goes to the
model.

A typed bare yes passes when the session's latest quote is still valid
and the only quote of its turn, the first message after it is our reply
ending with the booking offer, and the only message after that reply is
this one. Anything else goes to the model.

None means "the model answers": a question tap, any other text, or a check
that sends the turn there.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import psycopg

from services.agent.booking_buttons import (
    ButtonTap,
    ends_with_booking_offer,
    is_bare_decisive_yes,
    offer_language,
    parse_button_id,
)
from services.agent.fixed_texts import Language
from services.agent.llm.booking_follow_up import QuoteSummary, load_quote_summary
from services.agent.llm.session import load_session_start

logger = logging.getLogger(__name__)

MismatchProblem = Literal["unknown_button_id", "unknown_offer_message", "foreign_quote"]

_OFFER_MESSAGE_SQL = """
    SELECT body
    FROM messages
    WHERE conversation_id = %(conversation_id)s
      AND direction = 'outbound'
      AND whatsapp_message_id = %(whatsapp_message_id)s
"""

_TAPPED_QUOTE_SQL = """
    SELECT q.created_at >= COALESCE(%(session_start)s, '-infinity'::timestamptz)
           AND q.created_at > now() - %(validity)s::interval
    FROM quotes AS q
    WHERE q.id = %(quote_id)s AND q.conversation_id = %(conversation_id)s
"""

# The session's latest quote; whether it is still valid; whether it is
# the only quote of its turn (no other quote since the last message
# before it); and when it was made.
_LATEST_QUOTE_SQL = """
    SELECT q.id,
           q.created_at > now() - %(validity)s::interval,
           NOT EXISTS (
               SELECT 1
               FROM quotes AS other
               WHERE other.conversation_id = q.conversation_id
                 AND other.id <> q.id
                 AND other.created_at > COALESCE(
                     (SELECT max(m.created_at)
                      FROM messages AS m
                      WHERE m.conversation_id = q.conversation_id
                        AND m.created_at < q.created_at),
                     '-infinity'::timestamptz)
           ),
           q.created_at
    FROM quotes AS q
    WHERE q.conversation_id = %(conversation_id)s
      AND q.created_at >= COALESCE(%(session_start)s, '-infinity'::timestamptz)
    ORDER BY q.created_at DESC, q.id DESC
    LIMIT 1
"""

# The first three messages after a quote: a direct answer to its offer is
# exactly two -- our reply, then the customer's message.
_MESSAGES_AFTER_QUOTE_SQL = """
    SELECT direction, body, whatsapp_message_id
    FROM messages
    WHERE conversation_id = %(conversation_id)s
      AND created_at > %(quote_created_at)s
    ORDER BY created_at, id
    LIMIT 3
"""


@dataclass(frozen=True)
class CustomerMessage:
    """The message a turn answers: its WhatsApp id, the text stored for it
    (a tapped button's title), and the tap itself when it is one."""

    whatsapp_message_id: str
    text: str
    button: ButtonTap | None


@dataclass(frozen=True)
class PassOn:
    """Pass `quote` on to a colleague and confirm it in `language`."""

    quote: QuoteSummary
    language: Language
    source: Literal["button", "text"]


@dataclass(frozen=True)
class OfferNewerPrice:
    """The tapped offer has been replaced: offer quote_id instead."""

    tapped_quote_id: int
    quote_id: int
    language: Language


@dataclass(frozen=True)
class ButtonMismatch:
    """A yes tap that cannot be a real customer action."""

    problem: MismatchProblem


BookingDecision = PassOn | OfferNewerPrice | ButtonMismatch


@dataclass(frozen=True)
class _LatestQuote:
    quote_id: int
    valid: bool
    only_one_of_its_turn: bool
    created_at: datetime


def decide_booking_yes(
    conn: psycopg.Connection[Any],
    message: CustomerMessage,
    *,
    conversation_id: int,
    quote_validity: timedelta,
) -> BookingDecision | None:
    """What code does with `message`, or None when the model answers it.
    Reads only; the caller acts on the decision.

    Raises:
        psycopg.Error: a read failed.
    """
    if message.button is not None:
        return _decide_tap(
            conn,
            message.button,
            conversation_id=conversation_id,
            quote_validity=quote_validity,
        )
    if not is_bare_decisive_yes(message.text):
        return None
    return _decide_typed_yes(
        conn,
        whatsapp_message_id=message.whatsapp_message_id,
        conversation_id=conversation_id,
        quote_validity=quote_validity,
    )


def _decide_tap(
    conn: psycopg.Connection[Any],
    tap: ButtonTap,
    *,
    conversation_id: int,
    quote_validity: timedelta,
) -> BookingDecision | None:
    parsed = parse_button_id(tap.button_id)
    if parsed is None:
        return ButtonMismatch(problem="unknown_button_id")
    if parsed.choice == "question":
        return None
    offer_body = _our_offer_message(
        conn,
        conversation_id=conversation_id,
        whatsapp_message_id=tap.context_message_id,
    )
    if offer_body is None:
        return ButtonMismatch(problem="unknown_offer_message")
    session_start = load_session_start(conn, conversation_id)
    tapped = conn.execute(
        _TAPPED_QUOTE_SQL,
        {
            "quote_id": parsed.quote_id,
            "conversation_id": conversation_id,
            "session_start": session_start,
            "validity": quote_validity,
        },
    ).fetchone()
    if tapped is None:
        return ButtonMismatch(problem="foreign_quote")
    if not tapped[0]:
        _log_left_to_model(conversation_id, parsed.quote_id, "quote_expired")
        return None
    language = offer_language(offer_body)
    latest = _latest_quote(
        conn,
        conversation_id=conversation_id,
        session_start=session_start,
        quote_validity=quote_validity,
    )
    if latest is None or latest.quote_id == parsed.quote_id:
        return _pass_on(conn, parsed.quote_id, conversation_id, language, "button")
    if not (latest.valid and latest.only_one_of_its_turn):
        _log_left_to_model(
            conversation_id, parsed.quote_id, "newer_quote_not_offerable"
        )
        return None
    return OfferNewerPrice(
        tapped_quote_id=parsed.quote_id, quote_id=latest.quote_id, language=language
    )


def _decide_typed_yes(
    conn: psycopg.Connection[Any],
    *,
    whatsapp_message_id: str,
    conversation_id: int,
    quote_validity: timedelta,
) -> PassOn | None:
    latest = _latest_quote(
        conn,
        conversation_id=conversation_id,
        session_start=load_session_start(conn, conversation_id),
        quote_validity=quote_validity,
    )
    if latest is None or not (latest.valid and latest.only_one_of_its_turn):
        return None
    after = conn.execute(
        _MESSAGES_AFTER_QUOTE_SQL,
        {"conversation_id": conversation_id, "quote_created_at": latest.created_at},
    ).fetchall()
    if len(after) != 2:
        return None
    (reply_direction, reply_body, _), (answer_direction, _, answer_id) = after
    if reply_direction != "outbound" or not ends_with_booking_offer(reply_body):
        return None
    if answer_direction != "inbound" or answer_id != whatsapp_message_id:
        return None
    return _pass_on(
        conn, latest.quote_id, conversation_id, offer_language(reply_body), "text"
    )


def _our_offer_message(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    whatsapp_message_id: str | None,
) -> str | None:
    if whatsapp_message_id is None:
        return None
    row = conn.execute(
        _OFFER_MESSAGE_SQL,
        {
            "conversation_id": conversation_id,
            "whatsapp_message_id": whatsapp_message_id,
        },
    ).fetchone()
    return None if row is None else str(row[0])


def _latest_quote(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    session_start: datetime | None,
    quote_validity: timedelta,
) -> _LatestQuote | None:
    row = conn.execute(
        _LATEST_QUOTE_SQL,
        {
            "conversation_id": conversation_id,
            "session_start": session_start,
            "validity": quote_validity,
        },
    ).fetchone()
    if row is None:
        return None
    return _LatestQuote(
        quote_id=int(row[0]),
        valid=bool(row[1]),
        only_one_of_its_turn=bool(row[2]),
        created_at=row[3],
    )


def _pass_on(
    conn: psycopg.Connection[Any],
    quote_id: int,
    conversation_id: int,
    language: Language,
    source: Literal["button", "text"],
) -> PassOn | None:
    summary = load_quote_summary(
        conn, conversation_id=conversation_id, quote_id=quote_id
    )
    if summary is None:
        return None
    return PassOn(quote=summary, language=language, source=source)


def _log_left_to_model(conversation_id: int, quote_id: int, reason: str) -> None:
    logger.info(
        json.dumps(
            {
                "event": "booking_button_left_to_model",
                "conversation_id": conversation_id,
                "quote_id": quote_id,
                "reason": reason,
            }
        )
    )
