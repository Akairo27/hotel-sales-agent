"""Integration tests for services/agent/llm/caps.py against a real
Postgres instance — CLAUDE.md §9's spend/rate caps. Boundary correctness
(exact cap, exact Riyadh-day edge) is exercised for real here rather than
mocked, since the whole point of this module is a SUM/COUNT query no unit
test can stand in for.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from services.agent.llm.caps import (
    MessageRateCapExceededError,
    check_message_rate_cap,
    check_token_spend_caps,
    record_token_usage,
)
from services.agent.llm.config import LlmSettings
from services.agent.llm.conversation import UsageTotals
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    TokenSpendCapExceededError,
)
from tests.integration._seed import seed_conversation, seed_message

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"
_OTHER_PHONE = "+966500000002"
_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _settings(
    *,
    max_tokens_per_conversation: int = 1_000_000,
    max_spend_per_day_usd: Decimal = Decimal("1000"),
    max_messages_per_number_per_day: int = 1_000,
) -> LlmSettings:
    return LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=20,
        max_tokens_per_conversation=max_tokens_per_conversation,
        max_spend_per_day_usd=max_spend_per_day_usd,
        max_messages_per_number_per_day=max_messages_per_number_per_day,
    )


def test_check_token_spend_caps_allows_one_token_under_the_conversation_cap(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_tokens_per_conversation=100)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(90, 9, 99),
        now=_NOW,
    )

    check_token_spend_caps(
        db_conn, conversation_id=conversation_id, now=_NOW, settings=settings
    )


def test_check_token_spend_caps_raises_at_the_exact_conversation_cap(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_tokens_per_conversation=100)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(95, 5, 100),
        now=_NOW,
    )

    with pytest.raises(TokenSpendCapExceededError):
        check_token_spend_caps(
            db_conn, conversation_id=conversation_id, now=_NOW, settings=settings
        )


def test_check_token_spend_caps_sums_usage_across_several_calls(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_tokens_per_conversation=100)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    for _ in range(4):
        record_token_usage(
            db_conn,
            conversation_id=conversation_id,
            customer_phone=_PHONE,
            usage=UsageTotals(20, 5, 25),
            now=_NOW,
        )
    # 4 calls * 25 tokens = 100, exactly the cap.

    with pytest.raises(TokenSpendCapExceededError):
        check_token_spend_caps(
            db_conn, conversation_id=conversation_id, now=_NOW, settings=settings
        )


def test_check_token_spend_caps_conversation_cap_is_isolated_per_conversation(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_tokens_per_conversation=100)
    capped_conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    other_conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=capped_conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(100, 0, 100),
        now=_NOW,
    )

    with pytest.raises(TokenSpendCapExceededError):
        check_token_spend_caps(
            db_conn, conversation_id=capped_conversation_id, now=_NOW, settings=settings
        )
    # Same phone number, a different conversation — must not be blocked by
    # the other conversation's usage.
    check_token_spend_caps(
        db_conn, conversation_id=other_conversation_id, now=_NOW, settings=settings
    )


def test_check_token_spend_caps_daily_cap_is_global_across_phone_numbers(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The global daily cap is not per-number: one number's usage counts
    against everyone else's remaining budget, unlike the per-conversation
    cap above."""
    # 100 prompt tokens costs 100 * $0.75 / 1_000_000 = $0.000075.
    settings = _settings(max_spend_per_day_usd=Decimal("0.00015"))
    conversation_a = seed_conversation(db_conn, customer_phone=_PHONE)
    conversation_b = seed_conversation(db_conn, customer_phone=_OTHER_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=conversation_a,
        customer_phone=_PHONE,
        usage=UsageTotals(100, 0, 100),
        now=_NOW,
    )
    # $0.000075 is still under the $0.00015 cap.
    check_token_spend_caps(
        db_conn, conversation_id=conversation_b, now=_NOW, settings=settings
    )

    record_token_usage(
        db_conn,
        conversation_id=conversation_b,
        customer_phone=_OTHER_PHONE,
        usage=UsageTotals(100, 0, 100),
        now=_NOW,
    )
    # Combined spend across both phone numbers is now exactly $0.00015 —
    # conversation_a, which contributed none of the second call, is still
    # blocked because the cap is global.
    with pytest.raises(DailySpendCapExceededError):
        check_token_spend_caps(
            db_conn, conversation_id=conversation_a, now=_NOW, settings=settings
        )


def test_check_token_spend_caps_daily_cap_resets_on_the_riyadh_calendar_day(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Riyadh midnight is UTC 21:00 (fixed UTC+3, no DST) — not the UTC
    calendar day boundary."""
    # 1000 prompt tokens costs $0.00075, above this $0.0005 cap.
    settings = _settings(max_spend_per_day_usd=Decimal("0.0005"))
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    same_riyadh_day_timestamp = datetime(2026, 9, 1, 20, 59, 59, tzinfo=UTC)
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(1000, 0, 1000),
        now=same_riyadh_day_timestamp,
    )

    # Still within the same Riyadh calendar day (2026-09-01) — must count.
    with pytest.raises(DailySpendCapExceededError):
        check_token_spend_caps(
            db_conn,
            conversation_id=conversation_id,
            now=datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC),
            settings=settings,
        )

    # One second later in UTC, Riyadh's calendar day has already turned
    # over to 2026-09-02 — the earlier row belongs to yesterday and must
    # not count toward today's total.
    check_token_spend_caps(
        db_conn,
        conversation_id=conversation_id,
        now=datetime(2026, 9, 1, 21, 0, 0, tzinfo=UTC),
        settings=settings,
    )


def test_check_token_spend_caps_logs_the_daily_cap_block_every_time(
    db_conn: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(max_spend_per_day_usd=Decimal("0.00005"))
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(1000, 0, 1000),
        now=_NOW,
    )

    caplog.set_level(logging.ERROR, logger="services.agent.llm.caps")
    for _ in range(3):
        with pytest.raises(DailySpendCapExceededError):
            check_token_spend_caps(
                db_conn, conversation_id=conversation_id, now=_NOW, settings=settings
            )

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 3
    for record in error_records:
        payload = json.loads(record.getMessage())
        assert payload["event"] == "daily_spend_cap_exceeded"
        assert payload["conversation_id"] == conversation_id


def test_record_token_usage_and_check_token_spend_caps_round_trip(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(prompt_tokens=7, candidates_tokens=3, total_tokens=10),
        now=_NOW,
    )

    row = db_conn.execute(
        "SELECT conversation_id, customer_phone, prompt_tokens, candidates_tokens, "
        "total_tokens FROM token_usage WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert row == (conversation_id, _PHONE, 7, 3, 10)


def test_check_message_rate_cap_allows_one_message_under_the_cap(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_messages_per_number_per_day=3)
    now = datetime.now(UTC)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    for _ in range(2):
        seed_message(
            db_conn,
            conversation_id,
            direction="inbound",
            body="hi",
            customer_phone=_PHONE,
        )

    check_message_rate_cap(db_conn, customer_phone=_PHONE, now=now, settings=settings)


def test_check_message_rate_cap_raises_at_the_exact_cap(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_messages_per_number_per_day=3)
    now = datetime.now(UTC)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    for _ in range(3):
        seed_message(
            db_conn,
            conversation_id,
            direction="inbound",
            body="hi",
            customer_phone=_PHONE,
        )

    with pytest.raises(MessageRateCapExceededError):
        check_message_rate_cap(
            db_conn, customer_phone=_PHONE, now=now, settings=settings
        )


def test_check_message_rate_cap_only_counts_inbound_messages(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_messages_per_number_per_day=1)
    now = datetime.now(UTC)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_message(
        db_conn, conversation_id, direction="outbound", body="hi", customer_phone=_PHONE
    )

    check_message_rate_cap(db_conn, customer_phone=_PHONE, now=now, settings=settings)


def test_check_message_rate_cap_is_isolated_per_phone_number(
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_messages_per_number_per_day=1)
    now = datetime.now(UTC)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_message(
        db_conn, conversation_id, direction="inbound", body="hi", customer_phone=_PHONE
    )

    with pytest.raises(MessageRateCapExceededError):
        check_message_rate_cap(
            db_conn, customer_phone=_PHONE, now=now, settings=settings
        )
    # A different phone number, untouched by the other number's messages.
    check_message_rate_cap(
        db_conn, customer_phone=_OTHER_PHONE, now=now, settings=settings
    )
