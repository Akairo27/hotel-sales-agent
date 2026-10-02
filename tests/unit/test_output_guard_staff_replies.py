"""The output guard's staff-reply mode (services/agent/output_guard/
staff_replies.py): it never blocks, and it finds every amount a staff
reply states for the audit -- but never a phone or ID number."""

from __future__ import annotations

import json
import logging

import pytest

from services.agent.output_guard.config import MAX_AUDITED_BARE_AMOUNT_DIGITS
from services.agent.output_guard.staff_replies import audit_record, inspect_staff_reply

_CONVERSATION_ID = 7


def _amounts(text: str) -> list[tuple[str, int | None]]:
    inspection = inspect_staff_reply(text, conversation_id=_CONVERSATION_ID)
    return [(amount.raw, amount.halalas) for amount in inspection.stated_amounts]


def test_the_inspection_carries_the_text_unchanged() -> None:
    text = "  أبشر، الغرفة جاهزة\nYour room is ready  "

    inspection = inspect_staff_reply(text, conversation_id=_CONVERSATION_ID)

    assert inspection.text == text
    assert inspection.stated_amounts == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("السعر 900 ريال", [("900", 90_000)], id="arabic-riyal"),
        pytest.param("It is 1,250 SAR", [("1,250", 125_000)], id="sar"),
        pytest.param("total 2,400", [("2,400", 240_000)], id="grouped-unmarked"),
        pytest.param("I can do 750 for you", [("750", 75_000)], id="bare-price"),
        pytest.param("400 USD", [("400", 40_000)], id="foreign-currency"),
        pytest.param("20% off", [("20", None)], id="percentage"),
        pytest.param(
            "900 ريال then 850 ريال",
            [("900", 90_000), ("850", 85_000)],
            id="two-amounts",
        ),
    ],
)
def test_every_stated_amount_is_found_even_where_the_model_would_be_blocked(
    text: str, expected: list[tuple[str, int | None]]
) -> None:
    assert _amounts(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Call me on 0501234567", id="phone"),
        pytest.param("Booking 12345678 confirmed", id="eight-digit-reference"),
        pytest.param("2 rooms for 3 nights", id="small-counts"),
        pytest.param("check in 2026-10-20", id="date"),
    ],
)
def test_phone_and_id_numbers_and_small_counts_are_not_amounts(text: str) -> None:
    assert _amounts(text) == []


def test_a_bare_run_at_the_digit_limit_is_still_an_amount() -> None:
    bare = "9" * MAX_AUDITED_BARE_AMOUNT_DIGITS

    assert _amounts(f"for {bare} total") == [(bare, int(bare) * 100)]


def test_the_audit_record_says_what_marked_each_amount() -> None:
    inspection = inspect_staff_reply("400 USD or 20%", conversation_id=_CONVERSATION_ID)

    assert [audit_record(amount) for amount in inspection.stated_amounts] == [
        {
            "raw": "400",
            "halalas": 40_000,
            "sar_marker": False,
            "foreign_currency_marker": "USD",
            "percentage": False,
        },
        {
            "raw": "20",
            "halalas": None,
            "sar_marker": False,
            "foreign_currency_marker": None,
            "percentage": True,
        },
    ]


def test_the_log_carries_the_amount_count_never_the_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.output_guard.staff_replies")
    text = "Your room is 1,500 ريال, call 0501234567"

    inspect_staff_reply(text, conversation_id=_CONVERSATION_ID)

    (record,) = caplog.records
    assert json.loads(record.getMessage()) == {
        "event": "output_guard_staff_reply",
        "conversation_id": _CONVERSATION_ID,
        "stated_amount_count": 1,
    }
