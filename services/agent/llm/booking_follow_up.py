"""request_booking_follow_up: passes the quoted stay the customer has just
said yes to on to a colleague, as one booking_requested escalation per
quote (owner decisions 2026-09-30, ARCHITECTURE.md §7). It books nothing
-- no hold, no payment, no confirmation; a colleague contacts the
customer.

The tool takes no arguments: the model never names a quote. A live test
showed why -- tool results are not kept in the conversation history, so in
the turn where the customer says yes the model has no quote id at all, and
an id it guessed could name the wrong stay. The database picks the quote
instead: the latest quote of this conversation's current session
(services/agent/llm/session.py) that is still valid (made less than
quote_validity ago, the window the output guard applies) and that the
customer has written after -- the offer their yes answers. With none,
nothing is written and the model is told to give a fresh price first.

The result summarises the quote that was passed on (hotel, room type,
dates, rooms, total) so the reply restates exactly what staff received.

At most one escalation per quote: migration 0032's partial unique index
refuses a second booking_requested row for the same quote, and the insert
uses ON CONFLICT DO NOTHING, so a customer who says yes twice -- even in
two messages handled at the same moment -- gets a single follow-up. The
insert is its own statement rather than
services.agent.output_guard.enforcement.open_escalation: that one has no
quote_id and no conflict clause, and it lives in the output guard, which
this tool does not change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg

from lib.money import format_halalas_as_arabic_riyal, format_halalas_as_sar
from services.agent.llm.errors import InvalidToolArgumentsError
from services.agent.llm.session import load_session_start

logger = logging.getLogger(__name__)

REQUEST_BOOKING_FOLLOW_UP_TOOL = "request_booking_follow_up"
REASON_BOOKING_REQUESTED = "booking_requested"

# Hand-built, like dispatch.QUOTE_RESULT_KEYS: whether this call opened the
# request or found it already open, and the stay that was passed on.
BOOKING_FOLLOW_UP_RESULT_KEYS = frozenset(
    {
        "requested",
        "already_requested",
        "quote_id",
        "hotel_name",
        "room_type_name",
        "check_in",
        "check_out",
        "rooms",
        "total_price_display",
        "total_price_display_ar",
    }
)
BOOKING_FOLLOW_UP_LOG_SUMMARY_KEYS = frozenset({"quote_id", "already_requested"})

_ANSWERED_QUOTE_SQL = """
    SELECT q.id, h.hotel_name, rt.room_type_name, q.check_in, q.check_out,
           q.rooms, q.ask_price_total
    FROM quotes AS q
    JOIN hotels AS h ON h.id = q.hotel_id
    JOIN room_types AS rt ON rt.id = q.room_type_id
    WHERE q.conversation_id = %(conversation_id)s
      AND q.created_at >= COALESCE(%(session_start)s, '-infinity'::timestamptz)
      AND q.created_at > now() - %(validity)s::interval
      AND q.created_at < (
          SELECT max(m.created_at)
          FROM messages AS m
          WHERE m.conversation_id = %(conversation_id)s
            AND m.direction = 'inbound'
      )
    ORDER BY q.created_at DESC, q.id DESC
    LIMIT 1
"""

# The conflict target's predicate must repeat the index's own predicate
# literally for Postgres to use that index as the arbiter (migration 0032).
_INSERT_BOOKING_REQUEST_SQL = """
    INSERT INTO escalations (conversation_id, customer_phone, reason, notes, quote_id)
    SELECT c.id, c.customer_phone, 'booking_requested', '{}', %(quote_id)s
    FROM conversations AS c
    WHERE c.id = %(conversation_id)s
    ON CONFLICT (quote_id) WHERE reason = 'booking_requested' DO NOTHING
    RETURNING id
"""


@dataclass(frozen=True)
class _AnsweredQuote:
    quote_id: int
    hotel_name: str
    room_type_name: str
    check_in: date
    check_out: date
    rooms: int
    total_halalas: int


def _load_answered_quote(
    conn: psycopg.Connection[Any], *, conversation_id: int, quote_validity: timedelta
) -> _AnsweredQuote | None:
    row = conn.execute(
        _ANSWERED_QUOTE_SQL,
        {
            "conversation_id": conversation_id,
            "session_start": load_session_start(conn, conversation_id),
            "validity": quote_validity,
        },
    ).fetchone()
    if row is None:
        return None
    return _AnsweredQuote(
        quote_id=int(row[0]),
        hotel_name=row[1],
        room_type_name=row[2],
        check_in=row[3],
        check_out=row[4],
        rooms=int(row[5]),
        total_halalas=int(row[6]),
    )


def _open_request(
    conn: psycopg.Connection[Any], *, quote_id: int, conversation_id: int
) -> bool:
    """Opens the escalation; False when this quote already has one."""
    row = conn.execute(
        _INSERT_BOOKING_REQUEST_SQL,
        {"quote_id": quote_id, "conversation_id": conversation_id},
    ).fetchone()
    if row is None:
        return False
    logger.info(
        json.dumps(
            {
                "event": "booking_follow_up_requested",
                "conversation_id": conversation_id,
                "quote_id": quote_id,
                "escalation_id": int(row[0]),
            }
        )
    )
    return True


def request_booking_follow_up(
    conn: psycopg.Connection[Any], *, conversation_id: int, quote_validity: timedelta
) -> dict[str, Any]:
    """Opens the booking_requested escalation for the quote the customer is
    answering, or finds it already open, and returns the result the model
    sees (BOOKING_FOLLOW_UP_RESULT_KEYS).

    Raises:
        InvalidToolArgumentsError: code "quote_not_confirmable" -- this
            conversation's current session has no still-valid quote that the
            customer has written after.
    """
    quote = _load_answered_quote(
        conn, conversation_id=conversation_id, quote_validity=quote_validity
    )
    if quote is None:
        raise InvalidToolArgumentsError(
            f"conversation {conversation_id} has no valid quote the customer "
            "has answered",
            code="quote_not_confirmable",
        )
    opened = _open_request(
        conn, quote_id=quote.quote_id, conversation_id=conversation_id
    )
    return {
        "requested": True,
        "already_requested": not opened,
        "quote_id": quote.quote_id,
        "hotel_name": quote.hotel_name,
        "room_type_name": quote.room_type_name,
        "check_in": quote.check_in.isoformat(),
        "check_out": quote.check_out.isoformat(),
        "rooms": quote.rooms,
        "total_price_display": format_halalas_as_sar(quote.total_halalas),
        "total_price_display_ar": format_halalas_as_arabic_riyal(quote.total_halalas),
    }
