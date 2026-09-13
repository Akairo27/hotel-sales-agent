"""Unit tests for services/agent/llm/pricing.py — pure, no I/O, no
database. estimate_cost_usd's arithmetic and riyadh_calendar_day/
riyadh_day_bounds_utc's UTC<->Asia/Riyadh boundary conversion (Asia/Riyadh
is a fixed UTC+3 offset, no daylight saving, per the IANA tzdata).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from services.agent.llm.pricing import (
    GEMINI_FLASH_INPUT_USD_PER_MILLION_TOKENS,
    GEMINI_FLASH_OUTPUT_USD_PER_MILLION_TOKENS,
    estimate_cost_usd,
    riyadh_calendar_day,
    riyadh_day_bounds_utc,
)


def test_estimate_cost_usd_charges_only_input_rate_for_prompt_tokens() -> None:
    cost = estimate_cost_usd(prompt_tokens=1_000_000, candidates_tokens=0)
    assert cost == GEMINI_FLASH_INPUT_USD_PER_MILLION_TOKENS


def test_estimate_cost_usd_charges_only_output_rate_for_candidate_tokens() -> None:
    cost = estimate_cost_usd(prompt_tokens=0, candidates_tokens=1_000_000)
    assert cost == GEMINI_FLASH_OUTPUT_USD_PER_MILLION_TOKENS


def test_estimate_cost_usd_sums_both_rates() -> None:
    cost = estimate_cost_usd(prompt_tokens=100, candidates_tokens=50)
    # 100 * 0.75 / 1_000_000 + 50 * 3.75 / 1_000_000
    assert cost == Decimal("0.000075") + Decimal("0.0001875")


def test_estimate_cost_usd_returns_zero_for_zero_tokens() -> None:
    assert estimate_cost_usd(prompt_tokens=0, candidates_tokens=0) == Decimal(0)


def test_estimate_cost_usd_never_rounds_a_sub_cent_amount_away() -> None:
    # A single call this small rounds to $0.00 at cent precision — the
    # function must return the exact value so a caller summing many calls
    # into a daily total does not silently lose it.
    cost = estimate_cost_usd(prompt_tokens=1, candidates_tokens=0)
    assert cost == Decimal("0.00000075")
    assert cost != Decimal("0.00")


@pytest.mark.parametrize(
    ("utc_time", "expected_riyadh_day"),
    [
        # Riyadh midnight is UTC 21:00 the previous day (fixed UTC+3,
        # no DST) — one second before it is still the previous
        # Riyadh day.
        (datetime(2026, 9, 1, 20, 59, 59, tzinfo=UTC), date(2026, 9, 1)),
        (datetime(2026, 9, 1, 21, 0, 0, tzinfo=UTC), date(2026, 9, 2)),
        (datetime(2026, 9, 2, 20, 59, 59, tzinfo=UTC), date(2026, 9, 2)),
        (datetime(2026, 9, 2, 21, 0, 0, tzinfo=UTC), date(2026, 9, 3)),
    ],
)
def test_riyadh_calendar_day_boundary(
    utc_time: datetime, expected_riyadh_day: date
) -> None:
    assert riyadh_calendar_day(utc_time) == expected_riyadh_day


def test_riyadh_calendar_day_rejects_a_naive_datetime() -> None:
    naive = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        riyadh_calendar_day(naive)


def test_riyadh_day_bounds_utc_spans_exactly_one_riyadh_day() -> None:
    start, end = riyadh_day_bounds_utc(date(2026, 9, 2))
    assert start == datetime(2026, 9, 1, 21, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 21, 0, 0, tzinfo=UTC)
    assert riyadh_calendar_day(start) == date(2026, 9, 2)
    assert riyadh_calendar_day(end) == date(2026, 9, 3)
