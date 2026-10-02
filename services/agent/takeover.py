"""Whether a staff member has taken a conversation over from the dashboard
(staff notification step 2a, owner decisions 2026-10-02; ARCHITECTURE.md
§7 "إبلاغ الموظفين بالتصعيدات"; migration 0034).

While a takeover is active the bot stays silent: every customer message is
stored and nothing is sent, apart from the one acknowledgement
(services/agent/takeover_ack.py). The webhook checks this when it stores a
message, and again just before anything goes out to the customer, so a turn
already running when the takeover lands sends nothing either. Owner-approved
silences, listed in ARCHITECTURE.md §7 ("لا صمت").

The agent only reads this state; creating and ending takeovers is the
dashboard's (migration 0034's staff_take_over_conversation and
staff_close_conversation).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def active_takeover_exists(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> bool:
    """Whether the conversation has an active (not yet ended) takeover.

    Raises:
        psycopg.Error: the read failed.
    """
    row = conn.execute(
        "SELECT EXISTS (SELECT FROM conversation_takeovers "
        "WHERE conversation_id = %s AND ended_at IS NULL)",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("SELECT EXISTS returned no row")
    taken_over: bool = row[0]
    return taken_over


def is_taken_over(conn: psycopg.Connection[Any], *, conversation_id: int) -> bool:
    """active_takeover_exists, with a failed check logged at ERROR and read
    as "not taken over": the bot then answers as usual (owner decision D6,
    2026-10-02) -- the same "smaller harm than silence" rule as the rate-cap
    check. Never raises."""
    try:
        return active_takeover_exists(conn, conversation_id=conversation_id)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "takeover_check_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return False
