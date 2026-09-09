"""Reads the amounts a conversation's quotes legitimately let the model
state — the only place the output guard touches the database
(ARCHITECTURE.md §7, CLAUDE.md rule 8).

Scoped to every quote for a conversation_id, not one "active" quote:
conversations.active_quote_id exists as a column but is never written
anywhere in this codebase today, and writing it would mean touching
generate_reply's established "never writes to conversations" boundary
(services/agent/llm/conversation.py). quotes.conversation_id is already
reliably populated — dispatch_get_quote passes it straight through to
compute_quote's insert — so it gives the same information with zero new
writes, and it is actually more correct: a conversation can legitimately
produce more than one quote (different dates asked about in the same
thread — see migration 0024's own comment), and the model restating an
earlier one should not be treated as a violation.

Every cost-bearing field in quotes.nights (cost_per_night,
min_profit_halalas, target_margin_bps, and the rest of compute.py's audit
trail) is excluded in SQL, not in Python — CLAUDE.md §8 forbids logging
cost outright, so the safest place to keep it out of a Python value that
could later reach a log line is to never read it into one at all. This is
the same whitelist-by-hand discipline dispatch.quote_to_tool_result
already uses for what the model sees, pushed one layer further out.
"""

from __future__ import annotations

from typing import Any

import psycopg

from services.agent.output_guard.decision import AllowedAmounts

# migration 0009's quotes_all_nights_are_complete constraint guarantees
# nights is always a non-empty jsonb array with "ask"/"min_allowed" on
# every element, so this trusts that shape rather than re-checking it
# (CLAUDE.md rules 3-4: the constraint is the source of truth).
_LOAD_ALLOWED_AMOUNTS_SQL = """
    SELECT
        q.id,
        q.ask_price_total,
        q.min_allowed_total,
        (SELECT array_agg((night ->> 'ask')::bigint)
         FROM jsonb_array_elements(q.nights) AS night) AS night_asks,
        (SELECT array_agg((night ->> 'min_allowed')::bigint)
         FROM jsonb_array_elements(q.nights) AS night) AS night_floors
    FROM quotes AS q
    WHERE q.conversation_id = %s
    ORDER BY q.id
"""


def load_allowed_amounts(
    conn: psycopg.Connection[Any], conversation_id: int
) -> AllowedAmounts:
    """Reads every quote for conversation_id and builds the set of
    amounts a reply may legitimately state, plus the floor none may fall
    below.

    Returns an AllowedAmounts with an empty amounts_halalas and
    floor_halalas=None when the conversation has no quotes yet — every
    stated amount is then illegitimate by construction, which is correct:
    a reply that states any price before get_quote has ever run for this
    conversation has nothing legitimate to have copied it from.
    """
    rows = conn.execute(_LOAD_ALLOWED_AMOUNTS_SQL, (conversation_id,)).fetchall()

    quote_ids: list[int] = []
    amounts: set[int] = set()
    floors: list[int] = []
    for quote_id, ask_price_total, min_allowed_total, night_asks, night_floors in rows:
        quote_ids.append(int(quote_id))
        amounts.add(int(ask_price_total))
        amounts.update(int(ask) for ask in night_asks)
        floors.append(int(min_allowed_total))
        floors.extend(int(floor) for floor in night_floors)

    return AllowedAmounts(
        quote_ids=tuple(quote_ids),
        amounts_halalas=frozenset(amounts),
        floor_halalas=min(floors) if floors else None,
    )
