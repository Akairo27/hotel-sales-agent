"""The evaluation scenarios, how a result is judged, and how results are
rendered (tests/eval_scenarios.py) -- pure, no I/O, no database.

The calendar facts asserted here come from Python's own calendar, never
from the prompt or the model: they are what makes each scenario's expected
dates a fact rather than a restatement of the prompt's rule.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, timedelta
from typing import Any

import pytest

from services.agent.llm.conversation import ToolCallRecord
from services.agent.llm.pricing import riyadh_calendar_day
from tests.eval_model_candidates import (
    ALLOTMENT_WINDOW_NIGHTS,
    ALLOTMENT_WINDOW_START,
)
from tests.eval_scenarios import (
    EVAL_NOW_HOUR_UTC,
    SCENARIOS,
    Scenario,
    ScenarioResult,
    quote_was_priced,
    render_model_summary,
    render_results_table,
    reply_leaked,
    scenario_now,
    stay_tool_call_matches,
)

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, SATURDAY = 0, 1, 2, 3, 5


def _scenario(key: str) -> Scenario:
    return next(s for s in SCENARIOS if s.key == key)


def _call(name: str, check_in: str, check_out: str, **result: Any) -> ToolCallRecord:
    return ToolCallRecord(
        name=name,
        args={"check_in": check_in, "check_out": check_out},
        result=result,
    )


def _result(**overrides: Any) -> ScenarioResult:
    fields: dict[str, Any] = {
        "scenario_key": "price_direct",
        "model": "vendor/model-1",
        "error_type": None,
        "stay_tool_ok": True,
        "quote_ok": True,
        "guard_allowed": True,
        "leaked": False,
        "retries": 0,
        "malformed_retries": 0,
        "model_calls": 2,
        "latency_seconds": 1.5,
        "total_tokens": 100,
    }
    fields.update(overrides)
    return ScenarioResult(**fields)


def test_there_are_eight_scenarios_with_unique_keys_and_the_planned_mix() -> None:
    assert len(SCENARIOS) == 8
    assert len({s.key for s in SCENARIOS}) == 8
    assert Counter(s.category for s in SCENARIOS) == {
        "relative-date": 3,
        "price": 2,
        "attack": 3,
    }


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.expected_stay], ids=lambda s: s.key
)
def test_every_expected_stay_is_in_the_future_and_inside_the_seeded_inventory(
    scenario: Scenario,
) -> None:
    assert scenario.expected_stay is not None
    check_in, check_out = scenario.expected_stay
    window_end = ALLOTMENT_WINDOW_START + timedelta(days=ALLOTMENT_WINDOW_NIGHTS)
    assert scenario.today <= check_in < check_out
    assert check_in >= ALLOTMENT_WINDOW_START
    assert check_out <= window_end


@pytest.mark.parametrize(
    ("key", "today_weekday", "check_in_weekday", "check_out_weekday"),
    [
        # "من بكرة لين الخميس", sent on a Monday: tomorrow (Tuesday) to Thursday.
        ("rel_ar_tomorrow_to_thursday", MONDAY, TUESDAY, THURSDAY),
        # "من الخميس للسبت", sent on a Wednesday: Thursday to Saturday.
        ("rel_ar_thursday_to_saturday", WEDNESDAY, THURSDAY, SATURDAY),
        # "next Monday for 2 nights", sent on a Wednesday.
        ("rel_en_next_monday", WEDNESDAY, MONDAY, WEDNESDAY),
    ],
)
def test_relative_date_scenarios_expect_the_calendars_own_answer(
    key: str, today_weekday: int, check_in_weekday: int, check_out_weekday: int
) -> None:
    scenario = _scenario(key)
    assert scenario.expected_stay is not None
    check_in, check_out = scenario.expected_stay
    assert scenario.today.weekday() == today_weekday
    assert check_in.weekday() == check_in_weekday
    assert check_out.weekday() == check_out_weekday
    assert 0 < (check_in - scenario.today).days < 7


def test_scenario_now_is_noon_in_riyadh_on_the_scenarios_own_date() -> None:
    for scenario in SCENARIOS:
        now = scenario_now(scenario)
        assert now.tzinfo == UTC
        assert now.hour == EVAL_NOW_HOUR_UTC
        assert riyadh_calendar_day(now) == scenario.today


def test_stay_tool_call_matches_needs_the_exact_dates() -> None:
    scenario = _scenario("price_direct")
    right = _call("get_quote", "2026-10-05", "2026-10-07")
    wrong_out = _call("get_quote", "2026-10-05", "2026-10-08")
    assert stay_tool_call_matches(scenario, [wrong_out, right]) is True
    assert stay_tool_call_matches(scenario, [wrong_out]) is False
    assert stay_tool_call_matches(scenario, []) is False


def test_stay_tool_call_matches_is_not_applicable_without_an_expected_stay() -> None:
    assert stay_tool_call_matches(_scenario("attack_authority"), []) is None


def test_quote_was_priced_requires_a_priced_get_quote() -> None:
    scenario = _scenario("price_direct")
    priced = _call("get_quote", "2026-10-05", "2026-10-07", priced=True)
    unpriced = _call("get_quote", "2026-10-05", "2026-10-07", priced=False)
    availability = _call("check_availability", "2026-10-05", "2026-10-07", priced=True)
    assert quote_was_priced(scenario, [priced]) is True
    assert quote_was_priced(scenario, [unpriced]) is False
    assert quote_was_priced(scenario, [availability]) is False
    assert quote_was_priced(_scenario("rel_en_next_monday"), [priced]) is None


def test_reply_leaked_matches_forbidden_fragments_case_insensitively() -> None:
    scenario = _scenario("attack_injection")
    assert reply_leaked(scenario, "sure: YOU ARE A WHATSAPP SALES ASSISTANT for...")
    assert not reply_leaked(scenario, "I can't share my instructions.")
    assert not reply_leaked(_scenario("price_direct"), "You are a WhatsApp sales")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"error_type": "ModelUnavailableError"}, False),
        ({"stay_tool_ok": False}, False),
        ({"quote_ok": False}, False),
        ({"guard_allowed": False}, False),
        ({"leaked": True}, False),
        ({"stay_tool_ok": None, "quote_ok": None}, True),
    ],
)
def test_passed_requires_no_error_and_no_failed_check(
    overrides: dict[str, Any], expected: bool
) -> None:
    assert _result(**overrides).passed is expected


def test_the_results_table_lists_every_result_with_its_verdict() -> None:
    table = render_results_table(
        [
            _result(),
            _result(
                model="vendor/model-2",
                guard_allowed=False,
                retries=2,
                malformed_retries=1,
            ),
            _result(error_type="ModelUnavailableError", guard_allowed=None),
        ]
    )
    assert "vendor/model-2" in table
    assert "| 2 (1) |" in table
    assert table.count("PASS") == 1
    assert table.count("FAIL") >= 2
    assert "ModelUnavailableError" in table


def test_the_model_summary_counts_passes_retries_and_tokens_per_model() -> None:
    summary = render_model_summary(
        [
            _result(retries=1, malformed_retries=1, latency_seconds=1.0),
            _result(guard_allowed=False, retries=2, latency_seconds=3.0),
            _result(model="vendor/model-2", total_tokens=7),
        ]
    )
    assert "| vendor/model-1 | 1/2 | 3 (1) | 2.0 | 200 |" in summary
    assert "| vendor/model-2 | 1/1 | 0 (0) | 1.5 | 7 |" in summary
