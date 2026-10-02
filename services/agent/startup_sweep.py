"""The startup sweep for messages lost to a hard kill — ARCHITECTURE.md §7
("لا صمت"), owner decision F (2026-09-29).

A graceful restart waits for every in-flight reply job, but a hard kill
(SIGKILL, OOM, a crash) loses it: the customer's message is stored and
never answered. Once, in the background right after every start (never
periodically), this finds each conversation whose last stored message is
an unanswered inbound one from before the start and sends it through the
same funnel as any failed turn (webhook.escalate_and_notify_on_own_
connection): the standard fallback plus an escalation with reason
unanswered_at_startup and notes {"source": "startup_sweep"}. Nothing is
re-sent to the model.

Deliberately not covered (owner decisions, 2026-09-29):
- A turn that already opened an escalation but whose fallback WhatsApp
  refused (escalated_undelivered) looks unanswered too: hotel_agent can
  read only escalations.id, so the sweep retries the fallback and opens a
  second escalation. Rare, and accepted instead of a grant migration.
- Messages older than the lookback window (STARTUP_SWEEP_LOOKBACK_HOURS,
  default 20) and conversations past MAX_CONVERSATIONS_PER_SWEEP (the
  most recent go first; the rest are counted in the log).
- Only a conversation's last message is judged: an earlier lost message
  followed by a later answered one was seen by the model in that later
  turn.
- A conversation a staff member took over after its last message (owner
  decision D6, 2026-10-02): see find_unanswered_messages.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from services.agent import webhook
from services.agent.llm.pricing import riyadh_calendar_day, riyadh_day_bounds_utc

logger = logging.getLogger(__name__)

STARTUP_SWEEP_LOOKBACK_HOURS_ENV = "STARTUP_SWEEP_LOOKBACK_HOURS"

# 20 hours (owner, 2026-09-29): a customer missed 10 hours ago is still a
# booking lead for staff, and the reply still lands inside WhatsApp's
# 24-hour customer service window, outside which Meta refuses a
# free-form message -- hence the upper bound.
DEFAULT_LOOKBACK_HOURS = 20
MAX_LOOKBACK_HOURS = 24

# Bounds what one start can send, so a restart loop cannot flood customers
# or the escalation queue (owner, 2026-09-29).
MAX_CONVERSATIONS_PER_SWEEP = 20

# Only messages stored this long before the process started are swept, so
# a message the new process stored itself (its turn is live) is never
# mistaken for a lost one if the database clock runs behind this host's.
# Small enough to miss nothing a hard kill loses: systemd waits RestartSec
# (5s, ops/hotel-agent.service) before starting the new process.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)

REASON_UNANSWERED_AT_STARTUP = "unanswered_at_startup"
_SWEEP_NOTES = {"source": "startup_sweep"}


class StartupSweepConfigurationError(Exception):
    """Raised when STARTUP_SWEEP_LOOKBACK_HOURS is set to something other
    than a whole number of hours from 1 to MAX_LOOKBACK_HOURS."""


@dataclass(frozen=True)
class StartupSweepSettings:
    lookback: timedelta


@dataclass(frozen=True)
class UnansweredMessage:
    """The last message of a conversation, inbound and never answered."""

    message_id: int
    conversation_id: int
    customer_phone: str
    created_at: datetime


def load_startup_sweep_settings(
    env: Mapping[str, str] | None = None,
) -> StartupSweepSettings:
    """Reads the lookback window from STARTUP_SWEEP_LOOKBACK_HOURS, or
    DEFAULT_LOOKBACK_HOURS when it is unset or empty.

    Raises:
        StartupSweepConfigurationError: the value is not an integer from 1
            to MAX_LOOKBACK_HOURS.
    """
    active_env = os.environ if env is None else env
    raw = active_env.get(STARTUP_SWEEP_LOOKBACK_HOURS_ENV, "")
    if not raw:
        return StartupSweepSettings(lookback=timedelta(hours=DEFAULT_LOOKBACK_HOURS))
    try:
        hours = int(raw)
    except ValueError as exc:
        raise StartupSweepConfigurationError(
            f"{STARTUP_SWEEP_LOOKBACK_HOURS_ENV}={raw!r} is not an integer"
        ) from exc
    if not 1 <= hours <= MAX_LOOKBACK_HOURS:
        raise StartupSweepConfigurationError(
            f"{STARTUP_SWEEP_LOOKBACK_HOURS_ENV}={hours} must be from 1 to "
            f"{MAX_LOOKBACK_HOURS}"
        )
    return StartupSweepSettings(lookback=timedelta(hours=hours))


def find_unanswered_messages(
    conn: psycopg.Connection[Any], *, started_at: datetime, lookback: timedelta
) -> list[UnansweredMessage]:
    """Every conversation whose last stored message is inbound and was
    stored inside [started_at - lookback, started_at - CLOCK_SKEW_
    TOLERANCE), most recent first. A conversation with any later message --
    a reply, a fallback, or a newer message the live process is handling --
    is not returned. Nor is one a staff member held at any time after that
    message (a takeover still active, or one that ended after it): the
    message was theirs to answer, and the bot stays silent during a
    takeover (owner decision D6, 2026-10-02). Uses only the columns
    hotel_agent can read."""
    rows = conn.execute(
        "SELECT id, conversation_id, customer_phone, created_at FROM ("
        " SELECT DISTINCT ON (conversation_id)"
        " id, conversation_id, customer_phone, direction, created_at"
        " FROM messages WHERE created_at >= %s"
        " ORDER BY conversation_id, created_at DESC, id DESC"
        ") AS last_message "
        "WHERE direction = 'inbound' AND created_at < %s "
        "AND NOT EXISTS (SELECT FROM conversation_takeovers AS t"
        " WHERE t.conversation_id = last_message.conversation_id"
        " AND (t.ended_at IS NULL OR t.ended_at > last_message.created_at)) "
        "ORDER BY created_at DESC, id DESC",
        (started_at - lookback, started_at - CLOCK_SKEW_TOLERANCE),
    ).fetchall()
    return [
        UnansweredMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            created_at=created_at,
        )
        for message_id, conversation_id, customer_phone, created_at in rows
    ]


def is_silent_by_the_rate_cap(
    conn: psycopg.Connection[Any],
    message: UnansweredMessage,
    *,
    max_messages_per_number_per_day: int,
) -> bool:
    """Whether the message was the second or later past its number's daily
    cap on its Asia/Riyadh day -- deliberately unanswered (owner decision
    B), unlike the first one past the cap, which should have had the
    fallback. Counts the way caps.check_message_rate_cap did when the
    message was stored: its day's inbound messages up to and including it.
    """
    day_start, day_end = riyadh_day_bounds_utc(riyadh_calendar_day(message.created_at))
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE customer_phone = %s "
        "AND direction = 'inbound' AND created_at >= %s AND created_at < %s "
        "AND id <= %s",
        (message.customer_phone, day_start, day_end, message.message_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("SELECT COUNT(*) with no GROUP BY returned no row")
    position: int = row[0]
    return position > max_messages_per_number_per_day + 1


def _messages_to_answer(
    conn: psycopg.Connection[Any],
    *,
    started_at: datetime,
    lookback: timedelta,
    max_messages_per_number_per_day: int,
) -> tuple[list[UnansweredMessage], int]:
    """The unanswered messages to notify, and how many were skipped as
    silent by the rate cap."""
    unanswered = find_unanswered_messages(
        conn, started_at=started_at, lookback=lookback
    )
    to_answer = [
        message
        for message in unanswered
        if not is_silent_by_the_rate_cap(
            conn,
            message,
            max_messages_per_number_per_day=max_messages_per_number_per_day,
        )
    ]
    return to_answer, len(unanswered) - len(to_answer)


async def _notify(messages: list[UnansweredMessage]) -> Counter[str]:
    """Runs the funnel for each message in turn; returns the statuses."""
    statuses: Counter[str] = Counter()
    for message in messages:
        status = await webhook.escalate_and_notify_on_own_connection(
            conversation_id=message.conversation_id,
            customer_phone=message.customer_phone,
            reason=REASON_UNANSWERED_AT_STARTUP,
            exc=None,
            extra_notes=_SWEEP_NOTES,
        )
        logger.info(
            json.dumps(
                {
                    "event": "startup_sweep_notified",
                    "conversation_id": message.conversation_id,
                    "status": status,
                }
            )
        )
        statuses[status] += 1
    return statuses


async def run_startup_sweep(*, started_at: datetime) -> None:
    """The sweep itself, run once as a background task right after the
    service starts (services/agent/main.py). Logs startup_sweep_finished
    with the counts, or startup_sweep_failed at ERROR if the messages
    cannot be read (the database is down, a setting is broken): the
    service keeps running either way. Never raises."""
    try:
        sweep_settings = load_startup_sweep_settings()
        llm_settings = webhook.get_llm_settings()
        with webhook.get_db_connection() as conn:
            to_answer, skipped_rate_capped = _messages_to_answer(
                conn,
                started_at=started_at,
                lookback=sweep_settings.lookback,
                max_messages_per_number_per_day=(
                    llm_settings.max_messages_per_number_per_day
                ),
            )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "startup_sweep_failed",
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return
    statuses = await _notify(to_answer[:MAX_CONVERSATIONS_PER_SWEEP])
    logger.info(
        json.dumps(
            {
                "event": "startup_sweep_finished",
                "unanswered": len(to_answer),
                "skipped_rate_capped": skipped_rate_capped,
                "skipped_over_limit": max(
                    0, len(to_answer) - MAX_CONVERSATIONS_PER_SWEEP
                ),
                "statuses": dict(statuses),
            }
        )
    )
