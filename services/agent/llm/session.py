"""Conversation sessions — ARCHITECTURE.md §7.

conversations has exactly one row per phone number, forever (migration
0026's UNIQUE (customer_phone)), so "the conversation" is not a bounded
thing. Without a boundary a bare "hello" the next day would show the model
yesterday's dates, and the turn and token caps (CLAUDE.md §9) would
accumulate for the life of the phone number until every returning customer
was escalated.

A session is a run of messages with no idle gap longer than
SESSION_IDLE_GAP (services.agent.llm.config). Everything meant to be
per-session reads the same boundary from load_session_start: the model's
message window (context.load_recent_messages), the per-conversation token
cap (caps.check_token_spend_caps), and the amounts the output guard treats
as legitimate (output_guard.quotes.load_allowed_amounts). The counters
kept on the conversations row are reset by start_new_session_if_idle,
called once as an inbound message arrives.

No message is ever deleted: a person reviewing the conversation still sees
the whole history; only what the model, the caps and the guard consider
"this conversation" changes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import psycopg

from services.agent.llm.config import SESSION_IDLE_GAP

logger = logging.getLogger(__name__)

# The latest message whose gap from the previous one exceeds the idle gap
# starts the current session; the very first message has no predecessor
# (a NULL gap) and starts the first one. Ties on created_at are ordered by
# id so the window function is deterministic.
_SESSION_START_SQL = """
    SELECT MAX(created_at)
    FROM (
        SELECT created_at,
               created_at - LAG(created_at) OVER (ORDER BY created_at, id) AS gap
        FROM messages
        WHERE conversation_id = %s
    ) AS spaced
    WHERE gap IS NULL OR gap > %s
"""


def load_session_start(
    conn: psycopg.Connection[Any], conversation_id: int
) -> datetime | None:
    """When the conversation's current session began: the created_at of
    its first message. None when the conversation has no messages at all.

    Read-only, and the same answer for every caller until a new message
    arrives, so the window, the caps and the guard can never disagree on
    where the session starts.
    """
    row = conn.execute(
        _SESSION_START_SQL, (conversation_id, SESSION_IDLE_GAP)
    ).fetchone()
    if row is None:
        raise RuntimeError("SELECT MAX(...) with no GROUP BY returned no row")
    started_at: datetime | None = row[0]
    return started_at


def start_new_session_if_idle(
    conn: psycopg.Connection[Any], *, conversation_id: int, now: datetime
) -> bool:
    """Resets the per-session counters on the conversations row (turn_count,
    active_quote_id, concession_count) when the conversation's last message
    is more than SESSION_IDLE_GAP old, so the message about to be stored
    opens a fresh session. Returns True when it reset anything.

    Must run BEFORE the inbound message is inserted: once it is stored the
    conversation's newest message is, by definition, not idle. A single
    atomic UPDATE, so two deliveries racing for the same number cannot
    reset twice in a way that loses a turn; a duplicate delivery of a
    message already stored never resets (the stored message is recent).
    A row whose counters are already zero, or a conversation with no
    messages yet (nothing to be idle since), is left untouched.

    `now` must be timezone-aware.
    """
    cursor = conn.execute(
        "UPDATE conversations "
        "SET turn_count = 0, active_quote_id = NULL, concession_count = 0 "
        "WHERE id = %s "
        "AND (turn_count <> 0 OR active_quote_id IS NOT NULL "
        "OR concession_count <> 0) "
        "AND EXISTS (SELECT 1 FROM messages WHERE conversation_id = %s) "
        "AND NOT EXISTS (SELECT 1 FROM messages "
        "WHERE conversation_id = %s AND created_at >= %s)",
        (conversation_id, conversation_id, conversation_id, now - SESSION_IDLE_GAP),
    )
    was_reset = cursor.rowcount == 1
    if was_reset:
        logger.info(
            json.dumps(
                {
                    "event": "conversation_session_reset",
                    "conversation_id": conversation_id,
                }
            )
        )
    return was_reset


def touch_last_message_at(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> None:
    """Stamps conversations.last_message_at, which nothing wrote before:
    ARCHITECTURE.md §10's retention policy counts a year from it, so it has
    to move with every message in either direction."""
    conn.execute(
        "UPDATE conversations SET last_message_at = now() WHERE id = %s",
        (conversation_id,),
    )
