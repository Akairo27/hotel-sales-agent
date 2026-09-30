"""request_booking_follow_up: passes a quoted stay the customer has said
yes to on to a colleague, as one booking_requested escalation per quote
(owner decisions 2026-09-30, ARCHITECTURE.md §7). It books nothing -- no
hold, no payment, no confirmation; a colleague contacts the customer.

Two checks come first, both read from the database, never taken from the
model's word:
- the quote belongs to this conversation's current session, the same
  scoping the output guard applies to amounts (services/agent/llm/session.py);
- the customer has written since the quote was made, so the tool can only
  answer a message that came after the price, never run in the same turn
  as the price it would pass on. The model is also told to call it only
  after an explicit yes; this check is the part it cannot talk around.

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
from typing import Any

import psycopg

from services.agent.llm.errors import InvalidToolArgumentsError
from services.agent.llm.session import load_session_start

logger = logging.getLogger(__name__)

REQUEST_BOOKING_FOLLOW_UP_TOOL = "request_booking_follow_up"
REASON_BOOKING_REQUESTED = "booking_requested"

# Hand-built, like dispatch.QUOTE_RESULT_KEYS: the model is told whether
# this call opened the request or found it already open, and nothing else.
BOOKING_FOLLOW_UP_RESULT_KEYS = frozenset(
    {"requested", "quote_id", "already_requested"}
)
BOOKING_FOLLOW_UP_LOG_SUMMARY_KEYS = frozenset({"quote_id", "already_requested"})

_CONFIRMABLE_QUOTE_SQL = """
    SELECT 1
    FROM quotes AS q
    WHERE q.id = %(quote_id)s
      AND q.conversation_id = %(conversation_id)s
      AND q.created_at >= COALESCE(%(session_start)s, '-infinity'::timestamptz)
      AND q.created_at < (
          SELECT max(m.created_at)
          FROM messages AS m
          WHERE m.conversation_id = %(conversation_id)s
            AND m.direction = 'inbound'
      )
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


def _quote_is_confirmable(
    conn: psycopg.Connection[Any], *, quote_id: int, conversation_id: int
) -> bool:
    row = conn.execute(
        _CONFIRMABLE_QUOTE_SQL,
        {
            "quote_id": quote_id,
            "conversation_id": conversation_id,
            "session_start": load_session_start(conn, conversation_id),
        },
    ).fetchone()
    return row is not None


def request_booking_follow_up(
    conn: psycopg.Connection[Any], *, quote_id: int, conversation_id: int
) -> dict[str, Any]:
    """Opens the booking_requested escalation for quote_id, or finds it
    already open, and returns the result the model sees
    (BOOKING_FOLLOW_UP_RESULT_KEYS).

    Raises:
        InvalidToolArgumentsError: code "quote_not_confirmable" -- quote_id
            is not a quote from this conversation's current session that
            the customer has written after.
    """
    if not _quote_is_confirmable(
        conn, quote_id=quote_id, conversation_id=conversation_id
    ):
        raise InvalidToolArgumentsError(
            f"quote {quote_id} is not a current-session quote of conversation "
            f"{conversation_id} that the customer has answered",
            code="quote_not_confirmable",
        )
    row = conn.execute(
        _INSERT_BOOKING_REQUEST_SQL,
        {"quote_id": quote_id, "conversation_id": conversation_id},
    ).fetchone()
    if row is not None:
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
    return {"requested": True, "quote_id": quote_id, "already_requested": row is None}
