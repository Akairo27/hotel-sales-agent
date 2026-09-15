"""The cap-checking library behind CLAUDE.md §9's spend/rate/turn caps.

Pure-ish functions (conn + explicit `now` in, following
services/worker/hold_expiry.py's pattern of never reading the clock
itself). Three checks, two writes:

- check_token_spend_caps: called from generate_reply, before every
  transport.generate() call inside its tool-calling loop (not just once
  before the loop) — see its own docstring for why usage_so_far is a
  required argument, not a default.
- record_token_usage: NOT called by generate_reply — see
  conversation.py's module docstring, generate_reply never writes to the
  database. The webhook calls this immediately after generate_reply
  returns or raises with usage attached — see its own docstring for why
  this is never gated on the output guard or the send.
- increment_turn_count: called by the webhook at the same two points as
  record_token_usage above, for the same reason — see its own docstring.
- check_message_rate_cap: called by the webhook, before conversation
  state is even loaded — gates an inbound message before a conversation
  turn even starts, so it lives here rather than as a
  generate_reply-raised LlmError.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg

from services.agent.llm.config import LlmSettings
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    TokenSpendCapExceededError,
)
from services.agent.llm.pricing import (
    estimate_cost_usd,
    riyadh_calendar_day,
    riyadh_day_bounds_utc,
)

if TYPE_CHECKING:
    from services.agent.llm.conversation import UsageTotals

logger = logging.getLogger(__name__)

_INBOUND = "inbound"


class MessageRateCapExceededError(Exception):
    """Raised when a phone number has already sent
    settings.max_messages_per_number_per_day inbound messages on the
    current Asia/Riyadh calendar day.

    Not an LlmError: this gates an inbound WhatsApp message before a
    conversation turn even starts (the webhook layer), not something
    generate_reply itself can raise.
    """


def check_token_spend_caps(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    now: datetime,
    settings: LlmSettings,
    usage_so_far: UsageTotals,
) -> None:
    """Raises if this conversation's or today's total spend — committed
    usage from token_usage plus usage_so_far from the current turn's own
    model calls so far — is at or above its cap.

    Called from generate_reply before every transport.generate() call in
    its tool-calling loop, not just once before the loop starts.
    usage_so_far is required, not defaulted to zero: generate_reply never
    writes to token_usage itself (the webhook does, once the whole turn
    ends — see conversation.py's module docstring), so without
    usage_so_far this function would be blind to spend from earlier
    iterations of the very turn it is checking, and re-checking would
    change nothing. Every caller must state explicitly what this turn has
    already spent; pass UsageTotals.zero() for a turn-start check.

    Neither exception below carries usage_so_far itself — that happens one
    level up, in generate_reply's own wrapping try/except
    (conversation.py), via errors.attach_usage_so_far. This function's
    only job with usage_so_far is the arithmetic above; carrying it on
    whatever gets raised is a concern shared by every exception the
    tool-calling loop can produce, not just these two, so it lives in one
    place there rather than being duplicated here.

    Raises:
        TokenSpendCapExceededError: conversation_id's token_usage total,
            plus usage_so_far, has already reached
            settings.max_tokens_per_conversation.
        DailySpendCapExceededError: today's (Asia/Riyadh calendar day)
            estimated spend across every conversation, plus usage_so_far,
            has already reached settings.max_spend_per_day_usd. A soft cap
            — the check below is a SUM query, not a lock, so a small
            overshoot under concurrent load right at the boundary is
            possible and accepted. Logs one structured ERROR event every
            time this is raised, with no deduplication: each blocked
            customer is a real customer who got no help.
    """
    conversation_row = conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) FROM token_usage "
        "WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    if conversation_row is None:
        raise RuntimeError("SELECT SUM(...) with no GROUP BY returned no row")
    conversation_total_tokens: int = conversation_row[0] + usage_so_far.total_tokens
    if conversation_total_tokens >= settings.max_tokens_per_conversation:
        raise TokenSpendCapExceededError(
            f"conversation {conversation_id} has used "
            f"{conversation_total_tokens} tokens, at or above its cap "
            f"({settings.max_tokens_per_conversation})"
        )

    day = riyadh_calendar_day(now)
    day_start_utc, day_end_utc = riyadh_day_bounds_utc(day)
    daily_row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens), 0), "
        "COALESCE(SUM(candidates_tokens), 0) FROM token_usage "
        "WHERE created_at >= %s AND created_at < %s",
        (day_start_utc, day_end_utc),
    ).fetchone()
    if daily_row is None:
        raise RuntimeError("SELECT SUM(...) with no GROUP BY returned no row")
    daily_prompt_tokens = daily_row[0] + usage_so_far.prompt_tokens
    daily_candidates_tokens = daily_row[1] + usage_so_far.candidates_tokens
    daily_spend_usd = estimate_cost_usd(
        prompt_tokens=daily_prompt_tokens, candidates_tokens=daily_candidates_tokens
    )
    if daily_spend_usd >= settings.max_spend_per_day_usd:
        logger.error(
            json.dumps(
                {
                    "event": "daily_spend_cap_exceeded",
                    "conversation_id": conversation_id,
                    "riyadh_day": day.isoformat(),
                    "daily_spend_usd": str(daily_spend_usd),
                    "max_spend_per_day_usd": str(settings.max_spend_per_day_usd),
                }
            )
        )
        raise DailySpendCapExceededError(
            f"today's ({day.isoformat()}) estimated spend "
            f"({daily_spend_usd} USD) is at or above the daily cap "
            f"({settings.max_spend_per_day_usd} USD)"
        )


def record_token_usage(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    usage: UsageTotals,
    now: datetime,
) -> None:
    """Inserts one token_usage row for one completed model call.

    NOT called by generate_reply — see conversation.py's module
    docstring: generate_reply never writes to the database. The webhook
    calls this immediately after generate_reply returns a reply, or
    raises an exception carrying usage_so_far > 0 — never gated on the
    output guard or the WhatsApp send. Real cost is incurred the moment
    the model is actually called, not when (or whether) a reply is ever
    delivered to the customer, so this write cannot wait for either:
    gating it on delivery would leave every call this webhook makes
    unrecorded against CLAUDE.md §9's spend caps for as long as a later
    step in the same turn keeps failing.

    Each call is its own independent, immediately committed write (the
    webhook's connection is autocommit — see webhook.py's
    get_db_connection docstring) rather than one transaction bundled with
    increment_turn_count below: an earlier write that already happened
    (this one) must survive even if a later one in the same request
    fails, the same reasoning that docstring already gives for every
    other write in that module.
    """
    conn.execute(
        "INSERT INTO token_usage "
        "(conversation_id, customer_phone, prompt_tokens, candidates_tokens, "
        "total_tokens, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            conversation_id,
            customer_phone,
            usage.prompt_tokens,
            usage.candidates_tokens,
            usage.total_tokens,
            now,
        ),
    )


def increment_turn_count(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> None:
    """Increments conversations.turn_count by one — without this,
    generate_reply's own turn-cap check (conversation.py:
    `state.turn_count >= settings.max_conversation_turns`) compares
    against a column that never moves, and CLAUDE.md §9's turn cap has
    no effect.

    Called by the webhook at the same two points as record_token_usage
    above, under the same condition: a real model call already happened
    this turn (usage_so_far.total_tokens > 0, or generate_reply returned
    normally) is what counts as "one turn used," not whether the guard
    allowed the reply or the send succeeded — the identical reasoning
    record_token_usage's own docstring gives, applied to the turn cap
    instead of the spend caps. TurnCapExceededError itself never reaches
    here: it is raised before any model call in the turn
    (errors.read_usage_so_far's own docstring), so there is nothing new
    to count.
    """
    conn.execute(
        "UPDATE conversations SET turn_count = turn_count + 1 WHERE id = %s",
        (conversation_id,),
    )


def check_message_rate_cap(
    conn: psycopg.Connection[Any],
    *,
    customer_phone: str,
    now: datetime,
    settings: LlmSettings,
) -> None:
    """Raises MessageRateCapExceededError if customer_phone has already
    sent settings.max_messages_per_number_per_day inbound messages on the
    current Asia/Riyadh calendar day. Called by the webhook, before
    conversation state is even loaded.
    """
    day = riyadh_calendar_day(now)
    day_start_utc, day_end_utc = riyadh_day_bounds_utc(day)
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE customer_phone = %s "
        "AND direction = %s AND created_at >= %s AND created_at < %s",
        (customer_phone, _INBOUND, day_start_utc, day_end_utc),
    ).fetchone()
    if row is None:
        raise RuntimeError("SELECT COUNT(*) with no GROUP BY returned no row")
    message_count: int = row[0]
    if message_count >= settings.max_messages_per_number_per_day:
        raise MessageRateCapExceededError(
            f"{customer_phone} has sent {message_count} messages on "
            f"{day.isoformat()}, at or above the cap "
            f"({settings.max_messages_per_number_per_day})"
        )
