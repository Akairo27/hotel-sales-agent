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
    UserTurn,
)
from tests.eval_model_candidates import (
    EvalConfigurationError,
    require_only_seeded_hotels,
    run_scenario,
    seed_eval_database,
)
from tests.eval_scenarios import (
    LATIN_NAME_OF_THE_ARABIC_HOTEL,
    SCENARIOS,
    SEEDED_ARABIC_HOTEL_NAME,
    SEEDED_HOTEL_NAME,
    Scenario,
    ScenarioResult,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_MODEL = "vendor/model-1"
_USAGE = ModelUsage(prompt_tokens=10, candidates_tokens=5, total_tokens=15)

Step = Callable[[list[Turn]], ModelTurn]


class _ScriptedTransport:
    """Plays back one scripted step per model call."""

    def __init__(self, steps: list[Step]) -> None:
        self._steps = list(steps)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del system_instruction, deadline  # unused: the script decides what to say
        return ModelResponse(turn=self._steps.pop(0)(turns), usage=_USAGE)


class _FailingTransport:
    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del turns, system_instruction, deadline  # unused: this fake only ever fails
        raise ModelUnavailableError(
            "model call failed: OpenRouterCallError (status=404)"
        )


def _scenario(key: str) -> Scenario:
    return next(s for s in SCENARIOS if s.key == key)


def _call_tool(name: str, check_in: str, check_out: str, *, hotel_id: int = 1) -> Step:
    """hotel_id 1 is SEEDED_HOTEL_NAME, 2 SEEDED_ARABIC_HOTEL_NAME; each
    hotel's one room type has the same id as the hotel (seed order)."""
    args: dict[str, Any] = {
        "hotel_id": hotel_id,
        "room_type_id": hotel_id,
        "check_in": check_in,
        "check_out": check_out,
        "rooms": 1,
    }
    return lambda _turns: ModelTurn(
        text=None, tool_calls=(ToolCall(id="call_0", name=name, args=args),)
    )


def _search_for(name: str) -> Step:
    return lambda _turns: ModelTurn(
        text=None,
        tool_calls=(
            ToolCall(id="call_0", name="search_hotels", args={"hotel_name": name}),
        ),
    )


def _search_hotels_step() -> Step:
    """Every scripted scenario below now has to resolve the seeded hotel
    (SEEDED_HOTEL_NAME, ids 1/1 -- seed_eval_database's only hotel) via
    search_hotels before its first check_availability/get_quote call, to
    satisfy dispatch_tool's resolved-stays guard (services/agent/llm/
    dispatch.py)."""
    return lambda _turns: ModelTurn(
        text=None,
        tool_calls=(
            ToolCall(
                id="call_0",
                name="search_hotels",
                args={"hotel_name": SEEDED_HOTEL_NAME},
            ),
        ),
    )


def _say(text: str) -> Step:
    return lambda _turns: ModelTurn(text=text, tool_calls=())


def _state_the_quoted_total(turns: list[Turn]) -> ModelTurn:
    """Reads the price from the real get_quote result, so the output guard
    sees a matching amount -- but only the total: an incomplete quote
    reply by prompt.py's quote_reply."""
    last = turns[-1]
    assert isinstance(last, ToolResultTurn)
    total = last.results[0].result["total_price_display"]
    return ModelTurn(text=f"The total is {total}.", tool_calls=())


def _last_quote(turns: list[Turn]) -> dict[str, Any]:
    last = turns[-1]
    assert isinstance(last, ToolResultTurn)
    return last.results[0].result


def _write_the_english_quote_reply(turns: list[Turn]) -> ModelTurn:
    """The approved English quote reply, filled from the real get_quote
    result as a well-behaved model would."""
    quote = _last_quote(turns)
    return ModelTurn(
        text=(
            f"{quote['hotel_name']}, {quote['room_type_name']} room, "
            f"{quote['night_count']} nights, 5 to 7 October:\n"
            f"Total *{quote['total_price_display']}* "
            f"({quote['price_per_night_display']} per night).\n"
            f"Only {quote['distance_to_haram_display']} from the Haram.\n"
            "Shall I pass this to a colleague to confirm your booking?"
        ),
        tool_calls=(),
    )


def _write_the_arabic_quote_reply(turns: list[Turn]) -> ModelTurn:
    quote = _last_quote(turns)
    return ModelTurn(
        text=(
            f"{quote['hotel_name']}، غرفة {quote['room_type_name']}، ليلتين "
            "من 5 إلى 7 أكتوبر:\n"
            f"الإجمالي *{quote['total_price_display_ar']}* "
            f"({quote['price_per_night_display_ar']} لليلة).\n"
            f"يبعد {quote['distance_to_haram_display_ar']} عن الحرم.\n"
            "تحب أبلّغ زميلي يؤكّد لك الحجز؟"
        ),
        tool_calls=(),
    )


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
            _search_hotels_step(),
            _call_tool("get_quote", "2026-10-05", "2026-10-07"),
            _write_the_english_quote_reply,
        ]
    )

    result = _run(db_conn, _scenario("price_direct"), transport)

    assert result.passed
    assert result.quote_reply_ok is True
    assert result.buttons_ok is True
    assert result.tool_names == ("search_hotels", "get_quote")
    assert result.reply_text is not None
    assert result.reply_text.startswith("Test Hotel, Standard room, 2 nights")
    assert result.stay_tool_ok is True
    assert result.quote_ok is True
    assert result.guard_allowed is True
    assert result.leaked is False
    assert result.model_calls == 3
    assert result.retries == 0
    assert result.total_tokens == 45


def test_an_availability_check_for_the_right_dates_passes_a_relative_date_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [
            _search_hotels_step(),
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
            _search_hotels_step(),
            _call_tool("check_availability", "2026-09-23", "2026-09-25"),
            _say("Yes, a room is available."),
        ]
    )

    result = _run(db_conn, _scenario("rel_ar_tomorrow_to_thursday"), transport)

    assert not result.passed
    assert result.stay_tool_ok is False


def test_a_reply_stating_only_the_total_fails_the_quote_reply_check(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Priced correctly and allowed by the guard, but not a complete quote
    reply (owner decision 2026-09-30)."""
    transport = _ScriptedTransport(
        [
            _search_hotels_step(),
            _call_tool("get_quote", "2026-10-05", "2026-10-07"),
            _state_the_quoted_total,
        ]
    )

    result = _run(db_conn, _scenario("price_direct"), transport)

    assert result.quote_ok is True
    assert result.guard_allowed is True
    assert result.quote_reply_ok is False
    assert not result.passed


def test_an_arabic_retry_and_a_confirmation_pass_the_name_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The first search for the Latin name really finds nothing in the
    seeded database, and the Arabic retry really finds the hotel."""
    found: list[list[str]] = []

    def _record_and_confirm(turns: list[Turn]) -> ModelTurn:
        found.append([hotel["hotel_name"] for hotel in _last_quote(turns)["hotels"]])
        return ModelTurn(text=f"Do you mean {SEEDED_ARABIC_HOTEL_NAME}?", tool_calls=())

    def _record_and_retry(turns: list[Turn]) -> ModelTurn:
        found.append([hotel["hotel_name"] for hotel in _last_quote(turns)["hotels"]])
        return _search_for("النخبة")(turns)

    transport = _ScriptedTransport(
        [
            _search_for(LATIN_NAME_OF_THE_ARABIC_HOTEL),
            _record_and_retry,
            _record_and_confirm,
        ]
    )

    result = _run(db_conn, _scenario("name_retry_en"), transport)

    assert found == [[], [SEEDED_ARABIC_HOTEL_NAME]]
    assert result.name_retry_ok is True
    assert result.passed


def test_pricing_before_the_customer_confirms_fails_the_name_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [
            _search_for(LATIN_NAME_OF_THE_ARABIC_HOTEL),
            _search_for("النخبة"),
            _call_tool("get_quote", "2026-10-05", "2026-10-07", hotel_id=2),
            _write_the_english_quote_reply,
        ]
    )

    result = _run(db_conn, _scenario("name_retry_en"), transport)

    assert result.name_retry_ok is False
    assert not result.passed


def test_the_confirmed_hotel_is_quoted_with_the_earlier_messages_in_view(
    db_conn: psycopg.Connection[Any],
) -> None:
    """name_confirmed_ar's earlier messages are stored before the customer's
    yes, so the model sees the question it asked."""
    seen_history: list[list[str]] = []

    def _search_after_reading_history(turns: list[Turn]) -> ModelTurn:
        seen_history.append(
            [
                turn.text or ""
                for turn in turns
                if isinstance(turn, (UserTurn, ModelTurn))
            ]
        )
        return _search_for("النخبة")(turns)

    transport = _ScriptedTransport(
        [
            _search_after_reading_history,
            _call_tool("get_quote", "2026-10-05", "2026-10-07", hotel_id=2),
            _write_the_arabic_quote_reply,
        ]
    )

    result = _run(db_conn, _scenario("name_confirmed_ar"), transport)

    (history,) = seen_history
    assert history[-2:] == [f"تقصد {SEEDED_ARABIC_HOTEL_NAME}؟", "إيه نعم"]
    assert result.quote_ok is True
    assert result.quote_reply_ok is True
    assert result.guard_allowed is True
    assert result.passed


def test_an_expired_yes_tap_passes_with_a_fresh_button_ready_quote(
    db_conn: psycopg.Connection[Any],
) -> None:
    """button_yes_expired_ar: the stored tap title reaches the model, which
    prices the stay again and writes a complete reply ending with the
    offer -- one the buttons can go out with."""
    transport = _ScriptedTransport(
        [
            _search_hotels_step(),
            _call_tool("get_quote", "2026-10-05", "2026-10-07"),
            _write_the_arabic_quote_reply,
        ]
    )

    result = _run(db_conn, _scenario("button_yes_expired_ar"), transport)

    assert result.quote_ok is True
    assert result.quote_reply_ok is True
    assert result.buttons_ok is True
    assert result.guard_allowed is True
    assert result.passed


def test_repeating_the_expired_price_fails_the_expired_tap_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport([_say("تمام، الإجمالي *900.00 ريال*.")])

    result = _run(db_conn, _scenario("button_yes_expired_ar"), transport)

    assert result.quote_ok is False
    assert result.buttons_ok is False
    assert result.guard_allowed is False
    assert not result.passed


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
    # The failed call's type and HTTP status, as client.py words it -- so a
    # rejected reasoning setting shows why (no response body exists in it).
    assert result.error_detail == "model call failed: OpenRouterCallError (status=404)"
    assert result.model_calls == 1
    assert result.guard_allowed is None


def test_asking_which_hotel_passes_the_no_hotel_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport([_say("Which hotel would you like me to check?")])

    result = _run(db_conn, _scenario("clarify_no_hotel"), transport)

    assert result.clarified_ok is True
    assert result.passed


def test_pricing_a_guessed_hotel_fails_the_no_hotel_scenario(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [
            _call_tool("get_quote", "2026-10-05", "2026-10-07"),
            _say("It is available."),
        ]
    )

    result = _run(db_conn, _scenario("clarify_no_hotel"), transport)

    assert result.clarified_ok is False
    assert not result.passed


def test_seeding_is_repeatable_and_leaves_only_the_test_hotels(
    db_conn: psycopg.Connection[Any],
) -> None:
    seed_eval_database(db_conn)
    seed_eval_database(db_conn)

    hotels = db_conn.execute("SELECT hotel_name FROM hotels ORDER BY id").fetchall()
    assert hotels == [(SEEDED_HOTEL_NAME,), (SEEDED_ARABIC_HOTEL_NAME,)]
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


def _pass_the_booking_on(_turns: list[Turn]) -> ModelTurn:
    return ModelTurn(
        text=None,
        tool_calls=(ToolCall(id="call_0", name="request_booking_follow_up", args={}),),
    )


def _restate_what_was_passed_on(turns: list[Turn]) -> ModelTurn:
    """The approved Arabic reply after a booking is passed on, filled from
    the tool's own summary."""
    passed_on = _last_quote(turns)
    return ModelTurn(
        text=(
            f"أبشر، بلّغت زميلي بطلبك: {passed_on['hotel_name']}، غرفة "
            f"{passed_on['room_type_name']}، من 5 إلى 7 أكتوبر، الإجمالي "
            f"{passed_on['total_price_display_ar']}. يتواصل معك قريباً إن شاء "
            "الله لتأكيد الحجز."
        ),
        tool_calls=(),
    )


def test_a_clear_yes_passes_the_seeded_quote_on_and_restates_it(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Fix for the live test of 2026-09-30: the argument-free tool finds the
    quote the customer answered (seeded 5 minutes earlier), opens the real
    escalation, and the restated total passes the guard inside the window."""
    transport = _ScriptedTransport([_pass_the_booking_on, _restate_what_was_passed_on])

    result = _run(db_conn, _scenario("booking_yes_gulf"), transport)

    assert result.booking_ok is True
    assert result.guard_allowed is True
    assert result.passed
    requests = db_conn.execute(
        "SELECT count(*) FROM escalations WHERE reason = 'booking_requested' "
        "AND quote_id IS NOT NULL"
    ).fetchone()
    assert requests == (1,)


def test_the_seeded_quote_is_as_old_as_the_scenario_says(
    db_conn: psycopg.Connection[Any],
) -> None:
    _run(db_conn, _scenario("requote_after_expiry_ar"), _ScriptedTransport([_say("x")]))

    row = db_conn.execute(
        "SELECT ask_price_total, extract(epoch FROM now() - created_at) / 60 "
        "FROM quotes ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    total, age_minutes = row
    assert total == 80_000
    assert 45 <= age_minutes < 46


def test_copying_an_expired_price_is_blocked_by_the_guard(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The live-test failure, replayed: the price from the earlier reply is
    past its validity, so restating it fails the guard and the scenario."""
    transport = _ScriptedTransport([_say("Test Hotel، الإجمالي *800.00 ريال*.")])

    result = _run(db_conn, _scenario("requote_after_expiry_ar"), transport)

    assert result.guard_allowed is False
    assert result.quote_ok is False
    assert not result.passed


def test_a_fresh_quote_after_expiry_passes(db_conn: psycopg.Connection[Any]) -> None:
    transport = _ScriptedTransport(
        [
            _search_hotels_step(),
            _call_tool("get_quote", "2026-10-05", "2026-10-07"),
            _write_the_arabic_quote_reply,
        ]
    )

    result = _run(db_conn, _scenario("requote_after_expiry_ar"), transport)

    assert result.quote_ok is True
    assert result.quote_reply_ok is True
    assert result.guard_allowed is True
    assert result.passed


def test_restating_the_price_within_the_window_passes_the_guard(
    db_conn: psycopg.Connection[Any],
) -> None:
    transport = _ScriptedTransport(
        [_say("The total of 900.00 SAR covers the room only, without breakfast.")]
    )

    result = _run(db_conn, _scenario("clarify_within_window_en"), transport)

    assert result.guard_allowed is True
    assert result.passed
