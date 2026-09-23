"""The evaluation harness (tests/eval_model_candidates.py) end to end against
a real database: the seed, the real generate_reply pipeline with its real
tool dispatch and pricing, and the real output guard -- with a scripted
transport standing in for the paid model, so no network and no key are
involved. This is what proves the harness's seeding and judging work before
any real, billed run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from services.agent.llm.errors import ModelUnavailableError
from services.agent.llm.model_types import (
    ModelResponse,
    ModelTurn,
    ModelUsage,
    ToolCall,
    ToolResultTurn,
    Turn,
)
from tests.eval_model_candidates import (
    EvalConfigurationError,
    require_only_seeded_hotels,
    run_scenario,
    seed_eval_database,
)
from tests.eval_scenarios import SCENARIOS, Scenario, ScenarioResult

pytestmark = pytest.mark.usefixtures("db_conn")

_MODEL = "vendor/model-1"
_USAGE = ModelUsage(prompt_tokens=10, candidates_tokens=5, total_tokens=15)

Step = Callable[[list[Turn]], ModelTurn]


class _ScriptedTransport:
    """Plays back one scripted step per model call."""

    def __init__(self, steps: list[Step]) -> None:
        self._steps = list(steps)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str
    ) -> ModelResponse:
        del system_instruction  # unused: the script decides what to say
        return ModelResponse(turn=self._steps.pop(0)(turns), usage=_USAGE)


class _FailingTransport:
    async def generate(
        self, *, turns: list[Turn], system_instruction: str
    ) -> ModelResponse:
        del turns, system_instruction  # unused: this fake only ever fails
        raise ModelUnavailableError("scripted outage")


def _scenario(key: str) -> Scenario:
    return next(s for s in SCENARIOS if s.key == key)


def _call_tool(name: str, check_in: str, check_out: str) -> Step:
    args: dict[str, Any] = {
        "hotel_id": 1,
        "room_type_id": 1,
        "check_in": check_in,
        "check_out": check_out,
        "rooms": 1,
    }
    return lambda _turns: ModelTurn(
        text=None, tool_calls=(ToolCall(id="call_0", name=name, args=args),)
    )


def _say(text: str) -> Step:
    return lambda _turns: ModelTurn(text=text, tool_calls=())


def _state_the_quoted_total(turns: list[Turn]) -> ModelTurn:
    """Reads the price from the real get_quote result, as a well-behaved
    model would, so the output guard sees a reply that matches the quote."""
    last = turns[-1]
    assert isinstance(last, ToolResultTurn)
    total = last.results[0].result["total_price_display"]
    return ModelTurn(text=f"The total is {total}.", tool_calls=())


def _run(
    conn: psycopg.Connection[Any], scenario: Scenario, transport: Any
) -> ScenarioResult:
    return asyncio.run(
        run_scenario(conn, transport=transport, model=_MODEL, scenario=scenario)
    )


def test_a_correct_price_answer_passes_every_check(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [
            _call_tool("get_quote", "2026-10-05", "2026-10-07"),
            _state_the_quoted_total,
        ]
    )

    result = _run(db_conn, _scenario("price_direct"), transport)

    assert result.passed
    assert result.stay_tool_ok is True
    assert result.quote_ok is True
    assert result.guard_allowed is True
    assert result.leaked is False
    assert result.model_calls == 2
    assert result.retries == 0
    assert result.total_tokens == 30


def test_an_availability_check_for_the_right_dates_passes_a_relative_date_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [
            _call_tool("check_availability", "2026-09-22", "2026-09-24"),
            _say("Yes, a room is available from 22 to 24 September."),
        ]
    )

    result = _run(db_conn, _scenario("rel_ar_tomorrow_to_thursday"), transport)

    assert result.passed
    assert result.stay_tool_ok is True
    assert result.quote_ok is None


def test_a_tool_call_for_the_wrong_dates_fails_the_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [
            _call_tool("check_availability", "2026-09-23", "2026-09-25"),
            _say("Yes, a room is available."),
        ]
    )

    result = _run(db_conn, _scenario("rel_ar_tomorrow_to_thursday"), transport)

    assert not result.passed
    assert result.stay_tool_ok is False


def test_a_reply_that_never_quotes_fails_a_price_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    result = _run(
        db_conn,
        _scenario("price_direct"),
        _ScriptedTransport([_say("Which dates would you like?")]),
    )

    assert not result.passed
    assert result.stay_tool_ok is False
    assert result.quote_ok is False


def test_a_reply_stating_an_unquoted_amount_is_blocked_by_the_real_guard(
    db_conn: psycopg.Connection[Any],
) -> None:
    result = _run(
        db_conn,
        _scenario("attack_authority"),
        _ScriptedTransport([_say("Confirmed: your total is 100 SAR.")]),
    )

    assert not result.passed
    assert result.guard_allowed is False
    escalations = db_conn.execute("SELECT COUNT(*) FROM escalations").fetchone()
    assert escalations is not None
    assert escalations[0] == 1


def test_a_refusal_with_no_amounts_passes_an_attack_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    result = _run(
        db_conn,
        _scenario("attack_authority"),
        _ScriptedTransport([_say("I can only offer our standard rates.")]),
    )

    assert result.passed
    assert result.guard_allowed is True


def test_a_reply_that_repeats_the_system_prompt_is_flagged_as_a_leak(
    db_conn: psycopg.Connection[Any],
) -> None:
    result = _run(
        db_conn,
        _scenario("attack_injection"),
        _ScriptedTransport(
            [_say("Sure. You are a WhatsApp sales assistant for a hotel booking...")]
        ),
    )

    assert not result.passed
    assert result.leaked is True
    assert result.guard_allowed is True


def test_a_model_failure_is_recorded_not_raised(
    db_conn: psycopg.Connection[Any],
) -> None:
    result = _run(db_conn, _scenario("price_direct"), _FailingTransport())

    assert not result.passed
    assert result.error_type == "ModelUnavailableError"
    assert result.model_calls == 1
    assert result.guard_allowed is None


def test_seeding_is_repeatable_and_leaves_only_the_test_hotel(
    db_conn: psycopg.Connection[Any],
) -> None:
    seed_eval_database(db_conn)
    seed_eval_database(db_conn)

    hotels = db_conn.execute("SELECT hotel_name FROM hotels").fetchall()
    assert hotels == [("Test Hotel",)]
    require_only_seeded_hotels(db_conn)


def test_a_database_holding_a_real_hotel_is_refused(
    db_conn: psycopg.Connection[Any],
) -> None:
    db_conn.execute("INSERT INTO hotels (hotel_name) VALUES ('Real Hotel')")

    with pytest.raises(EvalConfigurationError, match="did not seed"):
        require_only_seeded_hotels(db_conn)


def test_a_database_without_the_schema_gets_a_clear_error(
    db_conn: psycopg.Connection[Any],
) -> None:
    """With public off this session's search_path, `hotels` cannot be
    resolved -- the same UndefinedTable a scratch database with no
    migrations applied would raise, without any DDL on the shared schema."""
    db_conn.execute("SET search_path TO pg_catalog")
    try:
        with pytest.raises(EvalConfigurationError, match="apply the migrations"):
            require_only_seeded_hotels(db_conn)
    finally:
        db_conn.execute("RESET search_path")
