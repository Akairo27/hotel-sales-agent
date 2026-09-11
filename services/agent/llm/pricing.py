"""Cost estimation for services/agent/llm — CLAUDE.md §9's spend caps.

Two narrow, pure functions, no I/O — mirrors lib/money.py's shape: a single
helper reused everywhere instead of a second implementation growing up
next to it.

CLAUDE.md rule 2: cost never enters the LLM context. Nothing in this module
is reachable from a prompt, a tool result, or anything the model can read
— it is read only by services.agent.llm.caps, on the application side of
the model call.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

# Verified against ai.google.dev/gemini-api/docs/pricing on 2026-09-11 and
# cross-checked against independent aggregation. Standard tier. Both
# figures double on 2027-01-01 ($1.50 / $7.50) — revisit before then.
#
# Two billing buckets, not three: the installed google-genai SDK's
# UsageMetadata carries thoughts_token_count (extended-thinking tokens)
# separately from prompt_token_count/candidates_token_count — confirmed
# against the installed package, not assumed. Gemini bills thinking
# tokens at the output rate, so services.agent.llm.conversation._usage_from
# folds thoughts_token_count into candidates_tokens before this formula
# ever sees it, rather than adding a third rate/bucket here.
GEMINI_FLASH_INPUT_USD_PER_MILLION_TOKENS = Decimal("0.75")
GEMINI_FLASH_OUTPUT_USD_PER_MILLION_TOKENS = Decimal("3.75")

_MILLION = Decimal(1_000_000)

_RIYADH = ZoneInfo("Asia/Riyadh")


def estimate_cost_usd(*, prompt_tokens: int, candidates_tokens: int) -> Decimal:
    """Estimates the USD cost of one model call from its token counts.

    Uses GEMINI_FLASH_INPUT_USD_PER_MILLION_TOKENS /
    GEMINI_FLASH_OUTPUT_USD_PER_MILLION_TOKENS — the only pricing this
    module knows. Never rounded: callers that need a rounded display value
    round at the point of display, not here, so summing many calls' costs
    does not compound rounding error.
    """
    input_cost = (
        Decimal(prompt_tokens) * GEMINI_FLASH_INPUT_USD_PER_MILLION_TOKENS / _MILLION
    )
    output_cost = (
        Decimal(candidates_tokens)
        * GEMINI_FLASH_OUTPUT_USD_PER_MILLION_TOKENS
        / _MILLION
    )
    return input_cost + output_cost


def riyadh_calendar_day(now: datetime) -> date:
    """Converts a timestamp to the Asia/Riyadh calendar date it falls on.

    The daily spend/message caps reset on the business's local day, not
    UTC midnight — this is the one conversion point every "today" query
    goes through (services.agent.llm.caps), never hand-rolled a second
    place, mirroring lib/hijri.py's single-source-of-truth pattern.

    `now` must be timezone-aware — a naive datetime has no defined offset
    from Asia/Riyadh and would silently produce a wrong day.
    """
    if now.tzinfo is None:
        raise ValueError("riyadh_calendar_day requires a timezone-aware datetime")
    return now.astimezone(_RIYADH).date()


def riyadh_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """The [start, end) UTC instants spanning one Asia/Riyadh calendar day.

    The one place that turns a riyadh_calendar_day() result back into a
    concrete range — every "today so far" query (services.agent.llm.caps)
    filters `created_at` against this pair rather than re-deriving the
    UTC offset itself, so the boundary is computed exactly once no matter
    how many callers need it.
    """
    start_local = datetime(day.year, day.month, day.day, tzinfo=_RIYADH)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
