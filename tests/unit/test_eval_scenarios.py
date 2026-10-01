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

from services.agent.llm.config import DEFAULT_QUOTE_VALIDITY_MINUTES
from services.agent.llm.conversation import ToolCallRecord
from services.agent.llm.pricing import riyadh_calendar_day
from tests.eval_model_candidates import (
    ALLOTMENT_WINDOW_NIGHTS,
    ALLOTMENT_WINDOW_START,
)
from tests.eval_scenarios import (
    EVAL_NOW_HOUR_UTC,
    LATIN_NAME_OF_THE_ARABIC_HOTEL,
    SCENARIOS,
    SEEDED_ARABIC_HOTEL_NAME,
    SEEDED_HOTEL_NAME,
    Scenario,
    ScenarioResult,
    asked_instead_of_guessing,
    booking_answer_handled,
    buttons_attachable,
    hotel_confirmed_before_pricing,
    hotel_name_retried_and_confirmed,
    mentions_night_count,
    quote_reply_complete,
    quote_was_priced,
    render_model_summary,
    render_replies,
    render_results_table,
    reply_leaked,
    scenario_now,
    scenarios_in,
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


def test_there_are_twenty_six_scenarios_with_unique_keys_and_the_planned_mix() -> None:
    assert len(SCENARIOS) == 26
    assert len({s.key for s in SCENARIOS}) == 26
    assert Counter(s.category for s in SCENARIOS) == {
        "relative-date": 3,
        "price": 4,
        "attack": 3,
        "clarify": 1,
        "hotel-name": 3,
        "price-validity": 2,
        "booking": 10,
    }


def test_every_scenario_names_the_hotel_as_stored_but_the_approved_exceptions() -> None:
    """Owner condition (2026-09-30): the exact stored name, never a
    translation or transliteration -- except the no-hotel scenario, which
    names none, and the two hotel-name scenarios, which measure exactly the
    Arabic retry of a Latin name (owner-approved the same day)."""
    assert SEEDED_HOTEL_NAME == "Test Hotel"
    for scenario in SCENARIOS:
        conversation = [scenario.customer_message] + [
            body for _direction, body in scenario.earlier_messages
        ]
        if scenario.expects_clarification:
            assert SEEDED_HOTEL_NAME not in scenario.customer_message
        elif scenario.category == "hotel-name":
            assert SEEDED_ARABIC_HOTEL_NAME not in scenario.customer_message
            assert any(LATIN_NAME_OF_THE_ARABIC_HOTEL in t for t in conversation)
        else:
            assert any(SEEDED_HOTEL_NAME in t for t in conversation), scenario.key


def test_every_price_scenario_language_is_covered() -> None:
    languages = {s.language for s in SCENARIOS if s.requires_quote}
    assert languages == {"ar", "en", "id"}


def test_only_the_no_hotel_scenario_expects_a_clarifying_question() -> None:
    (scenario,) = [s for s in SCENARIOS if s.expects_clarification]
    assert scenario.key == "clarify_no_hotel"
    assert scenario.expected_stay is None
    assert not scenario.requires_quote


def test_asked_instead_of_guessing_fails_any_stay_tool_call() -> None:
    scenario = _scenario("clarify_no_hotel")
    assert asked_instead_of_guessing(scenario, []) is True
    assert asked_instead_of_guessing(scenario, [_call("search_hotels", "", "")])
    for tool in ("get_quote", "check_availability"):
        calls = [_call(tool, "2026-10-05", "2026-10-07")]
        assert asked_instead_of_guessing(scenario, calls) is False


def test_asked_instead_of_guessing_is_not_applicable_elsewhere() -> None:
    assert asked_instead_of_guessing(_scenario("price_direct"), []) is None


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
        ({"clarified_ok": False}, False),
        ({"clarified_ok": True}, True),
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
    assert (
        "| vendor/model-1 | default | 1/2 | 0 | 2/2 | 3 (1) | 2.0 | 3.0 | 0 | 0 | - "
        "| 200 |" in summary
    )
    assert (
        "| vendor/model-2 | default | 1/1 | 0 | 1/1 | 0 (0) | 1.5 | 1.5 | 0 | 0 | - "
        "| 7 |" in summary
    )


def test_the_summary_has_one_row_per_setting_with_latency_and_token_split() -> None:
    """Median and worst latency per turn across every scenario and repeat,
    and mean input/output/reasoning tokens per turn."""
    summary = render_model_summary(
        [
            _result(
                setting="default",
                latency_seconds=2.0,
                input_tokens=100,
                output_tokens=60,
                reasoning_tokens=50,
            ),
            _result(
                setting="default",
                latency_seconds=9.0,
                input_tokens=300,
                output_tokens=140,
                reasoning_tokens=110,
            ),
            _result(
                setting="default",
                latency_seconds=4.0,
                input_tokens=200,
                output_tokens=100,
                reasoning_tokens=80,
            ),
            _result(
                setting="none",
                latency_seconds=1.0,
                stay_tool_ok=False,
                input_tokens=90,
                output_tokens=10,
                reasoning_tokens=None,
            ),
        ]
    )
    assert (
        "| vendor/model-1 | default | 3/3 | 0 | 3/3 | 0 (0) | 4.0 | 9.0 | 200 | 100 "
        "| 80 | 300 |" in summary
    )
    assert (
        "| vendor/model-1 | none | 0/1 | 0 | 0/1 | 0 (0) | 1.0 | 1.0 | 90 | 10 | - |"
        in summary
    )


def test_the_summary_counts_model_errors_and_the_table_shows_their_status() -> None:
    failed = _result(
        setting="none",
        error_type="ModelUnavailableError",
        error_detail="model call failed: OpenRouterCallError (status=404)",
        stay_tool_ok=None,
        quote_ok=None,
        guard_allowed=None,
    )
    summary = render_model_summary([failed, failed])
    assert "| vendor/model-1 | none | 0/2 | 2 |" in summary
    table = render_results_table([failed])
    assert (
        "| ModelUnavailableError: model call failed: OpenRouterCallError "
        "(status=404) |" in table
    )


def test_the_results_table_shows_the_setting_and_the_token_split() -> None:
    table = render_results_table(
        [
            _result(
                setting="low", input_tokens=120, output_tokens=30, reasoning_tokens=12
            ),
            _result(setting="none", reasoning_tokens=None),
        ]
    )
    assert "| vendor/model-1 | low | price_direct |" in table
    assert "| 120 | 30 | 12 |" in table
    assert table.rstrip().endswith("| - |")


def _priced_quote(**overrides: Any) -> ToolCallRecord:
    result: dict[str, Any] = {
        "priced": True,
        "hotel_name": "Test Hotel",
        "room_type_name": "Standard",
        "night_count": 2,
        "total_price_display": "900.00 SAR",
        "total_price_display_ar": "900.00 ريال",
        "price_per_night_display": "450.00 SAR",
        "price_per_night_display_ar": "450.00 ريال",
        "lowest_night_price_display": None,
        "lowest_night_price_display_ar": None,
        "highest_night_price_display": None,
        "highest_night_price_display_ar": None,
        "distance_to_haram_display": "350 m",
        "distance_to_haram_display_ar": "350 متر",
    }
    result.update(overrides)
    return ToolCallRecord(name="get_quote", args={}, result=result)


_COMPLETE_ENGLISH_REPLY = (
    "Test Hotel, Standard room, 2 nights, 5 to 7 October:\n"
    "Total *900.00 SAR* (450.00 SAR per night).\n"
    "Only 350 m from the Haram.\n"
    "Shall I pass this to a colleague to confirm your booking?"
)
_COMPLETE_ARABIC_REPLY = (
    "Test Hotel، غرفة Standard، ليلتين من 5 إلى 7 أكتوبر:\n"
    "الإجمالي *900.00 ريال* (450.00 ريال لليلة).\n"
    "يبعد 350 متر عن الحرم.\n"
    "تبغاني أبلّغ زميلي يأكّد لك الحجز؟"
)


def test_a_complete_quote_reply_passes_in_english_and_arabic() -> None:
    assert quote_reply_complete(
        _scenario("price_direct"), [_priced_quote()], _COMPLETE_ENGLISH_REPLY
    )
    assert quote_reply_complete(
        _scenario("price_direct_ar"), [_priced_quote()], _COMPLETE_ARABIC_REPLY
    )


@pytest.mark.parametrize(
    "reply",
    [
        "The total is 900.00 SAR.",
        _COMPLETE_ENGLISH_REPLY.replace("Only 350 m from the Haram.\n", ""),
        _COMPLETE_ENGLISH_REPLY.replace(
            "Shall I pass this to a colleague to confirm your booking?",
            "Is there anything else I can help with?",
        ),
        _COMPLETE_ENGLISH_REPLY.rstrip("?") + ".",
        _COMPLETE_ENGLISH_REPLY + "\nThank you!",
    ],
)
def test_an_incomplete_or_generic_quote_reply_fails(reply: str) -> None:
    assert (
        quote_reply_complete(_scenario("price_direct"), [_priced_quote()], reply)
        is False
    )


def test_a_quote_reply_with_nights_that_differ_needs_both_night_prices() -> None:
    quote = _priced_quote(
        price_per_night_display=None,
        lowest_night_price_display="400.00 SAR",
        highest_night_price_display="500.00 SAR",
    )
    ranged = _COMPLETE_ENGLISH_REPLY.replace(
        "450.00 SAR per night", "from 400.00 SAR to 500.00 SAR per night"
    )
    assert quote_reply_complete(_scenario("price_direct"), [quote], ranged)
    assert not quote_reply_complete(
        _scenario("price_direct"), [quote], _COMPLETE_ENGLISH_REPLY
    )


def test_quote_reply_is_not_applicable_without_a_required_quote() -> None:
    assert quote_reply_complete(_scenario("attack_authority"), [], "No.") is None


def test_mentions_night_count_ignores_the_digit_inside_a_year() -> None:
    assert not mentions_night_count("5 to 7 October 2026", 2, "en")
    assert mentions_night_count("2 nights", 2, "en")
    assert mentions_night_count("ليلتين من 5 إلى 7 أكتوبر", 2, "ar")
    assert not mentions_night_count("ليلتين", 2, "en")


def _search(name: str) -> ToolCallRecord:
    return ToolCallRecord(
        name="search_hotels", args={"hotel_name": name}, result={"hotels": []}
    )


def test_an_arabic_retry_and_a_confirmation_pass_the_name_scenario() -> None:
    calls = [_search("Al Nokhba Hotel"), _search("النخبة")]
    reply = f"Do you mean {SEEDED_ARABIC_HOTEL_NAME}?"

    assert hotel_name_retried_and_confirmed(_scenario("name_retry_en"), calls, reply)


@pytest.mark.parametrize(
    ("calls", "reply"),
    [
        # no retry at all
        ([_search("Al Nokhba Hotel")], f"Do you mean {SEEDED_ARABIC_HOTEL_NAME}?"),
        # a retry that is not in Arabic
        (
            [_search("Al Nokhba Hotel"), _search("Nokhba")],
            f"Do you mean {SEEDED_ARABIC_HOTEL_NAME}?",
        ),
        # priced before asking
        (
            [_search("Al Nokhba Hotel"), _search("النخبة"), _priced_quote()],
            f"Do you mean {SEEDED_ARABIC_HOTEL_NAME}?",
        ),
        # asked, but without the stored name
        ([_search("Al Nokhba Hotel"), _search("النخبة")], "Do you mean that hotel?"),
    ],
)
def test_the_name_scenario_fails_without_retry_confirmation_or_restraint(
    calls: list[ToolCallRecord], reply: str
) -> None:
    assert (
        hotel_name_retried_and_confirmed(_scenario("name_retry_en"), calls, reply)
        is False
    )


def test_name_retry_is_not_applicable_elsewhere() -> None:
    assert hotel_name_retried_and_confirmed(_scenario("price_direct"), [], "") is None


@pytest.mark.parametrize("field", ["quote_reply_ok", "name_retry_ok"])
def test_a_failed_new_check_fails_the_turn(field: str) -> None:
    assert _result(**{field: False}).passed is False
    assert _result(**{field: None}).passed is True


def test_the_results_table_has_the_quote_reply_and_name_retry_columns() -> None:
    table = render_results_table([_result(quote_reply_ok=False, name_retry_ok=None)])
    assert "| quote reply | name retry |" in table
    assert "| FAIL | - |" in table


def _window_minutes() -> int:
    return DEFAULT_QUOTE_VALIDITY_MINUTES


def test_the_seeded_quotes_sit_on_the_right_side_of_the_validity_window() -> None:
    """The requote and expired-tap scenarios' quotes have expired; the
    clarifying and other booking scenarios' quotes are still valid -- or
    they would test nothing."""
    for scenario in SCENARIOS:
        minutes = scenario.seeded_quote_minutes_ago
        if scenario.key in ("requote_after_expiry_ar", "button_yes_expired_ar"):
            assert minutes is not None and minutes > _window_minutes()
        elif scenario.category in ("booking", "price-validity"):
            assert minutes is not None and minutes < _window_minutes(), scenario.key


def test_every_booking_yes_is_from_the_owners_dialect_list() -> None:
    answers = {s.customer_message for s in SCENARIOS if s.expects_booking_request}
    assert answers == {"ايه", "أيوه", "صافي", "ok", "iya"}
    (unclear,) = [s for s in SCENARIOS if s.expects_yes_no_question]
    assert unclear.customer_message == "إيه؟"


def _booking_call(requested: bool) -> ToolCallRecord:
    return ToolCallRecord(
        name="request_booking_follow_up", args={}, result={"requested": requested}
    )


def test_a_clear_yes_passes_only_when_the_booking_is_passed_on() -> None:
    scenario = _scenario("booking_yes_gulf")
    done = "أبشر، بلّغت زميلي بطلبك. يتواصل معك قريباً إن شاء الله."
    assert booking_answer_handled(scenario, [_booking_call(True)], done)
    assert not booking_answer_handled(scenario, [], done)
    assert not booking_answer_handled(
        scenario, [_booking_call(True)], "اكتب «نعم أكّد الحجز» لو سمحت."
    )


def test_an_unclear_answer_passes_only_with_one_natural_question() -> None:
    scenario = _scenario("booking_unclear_ar")
    question = "يعني تحب أبلّغ زميلي يؤكّد لك الحجز؟"
    assert booking_answer_handled(scenario, [], question)
    assert not booking_answer_handled(scenario, [_booking_call(True)], question)
    assert not booking_answer_handled(scenario, [], "اكتب نعم لو تبي تكمل.")


def test_booking_check_is_not_applicable_elsewhere() -> None:
    assert booking_answer_handled(_scenario("price_direct"), [], "") is None


def test_a_hotel_from_context_needs_a_confirmation_and_no_price() -> None:
    scenario = _scenario("name_from_context_en")
    confirm = f"Do you mean {SEEDED_ARABIC_HOTEL_NAME}?"
    assert hotel_confirmed_before_pricing(scenario, [], confirm)
    assert not hotel_confirmed_before_pricing(
        scenario, [_call("get_quote", "2026-10-05", "2026-10-07")], confirm
    )
    assert not hotel_confirmed_before_pricing(scenario, [], "It is 900.00 SAR.")
    assert hotel_confirmed_before_pricing(_scenario("price_direct"), [], "") is None


def test_the_results_table_has_the_confirm_and_booking_columns() -> None:
    table = render_results_table([_result(hotel_confirmed_ok=True, booking_ok=False)])
    assert "| confirm | booking |" in table
    assert "| ok | FAIL |" in table


def test_scenarios_in_filters_by_category_in_list_order() -> None:
    booking = scenarios_in(["booking"])
    assert [s.key for s in booking] == [
        s.key for s in SCENARIOS if s.category == "booking"
    ]
    assert len(booking) == 10
    assert scenarios_in([]) == SCENARIOS


def test_the_button_scenarios_send_the_approved_titles() -> None:
    """A tap reaches the model as the button's title, in the offer's
    language (owner decisions 2026-10-01)."""
    questions = {
        s.key: s.customer_message for s in SCENARIOS if s.expects_question_prompt
    }
    assert questions == {
        "button_question_ar": "عندي سؤال",
        "button_question_en": "I have a question",
        "button_question_id": "Ada pertanyaan",
    }
    expired = _scenario("button_yes_expired_ar")
    assert expired.customer_message == "نعم، أكّد الحجز"
    assert expired.requires_quote


def test_a_question_tap_passes_only_with_a_question_and_no_booking_call() -> None:
    scenario = _scenario("button_question_en")
    question = "Of course, what would you like to know?"
    assert booking_answer_handled(scenario, [], question)
    assert not booking_answer_handled(scenario, [], "Sure.")
    assert not booking_answer_handled(scenario, [_booking_call(False)], question)


def test_a_priced_reply_ending_with_the_offer_can_carry_the_buttons() -> None:
    scenario = _scenario("price_direct")
    assert buttons_attachable(scenario, [7], _COMPLETE_ENGLISH_REPLY)
    assert buttons_attachable(_scenario("price_direct_ar"), [7], _COMPLETE_ARABIC_REPLY)


@pytest.mark.parametrize(
    ("quote_ids", "reply"),
    [
        pytest.param([], _COMPLETE_ENGLISH_REPLY, id="nothing-priced"),
        pytest.param([7, 8], _COMPLETE_ENGLISH_REPLY, id="two-stays-priced"),
        pytest.param(
            [7], _COMPLETE_ENGLISH_REPLY + "\nThank you!", id="offer-not-last"
        ),
        pytest.param(
            [7], "x" * 1100 + "\n" + _COMPLETE_ENGLISH_REPLY, id="over-the-body-limit"
        ),
    ],
)
def test_a_priced_reply_that_cannot_carry_the_buttons_fails(
    quote_ids: list[int], reply: str
) -> None:
    assert buttons_attachable(_scenario("price_direct"), quote_ids, reply) is False


def test_buttons_are_not_applicable_without_a_required_quote() -> None:
    assert buttons_attachable(_scenario("booking_yes_en"), [7], "Done.") is None


def test_a_reply_that_cannot_carry_the_buttons_fails_the_turn() -> None:
    assert _result(buttons_ok=False).passed is False
    assert _result(buttons_ok=None).passed is True


def test_the_results_table_has_the_buttons_column() -> None:
    table = render_results_table([_result(booking_ok=None, buttons_ok=False)])
    assert "| booking | buttons | guard |" in table
    assert "| - | FAIL | ok |" in table


def test_scenarios_in_refuses_an_unknown_category() -> None:
    with pytest.raises(ValueError, match="unknown scenario categories"):
        scenarios_in(["no-such-category"])


_TWO_LINE_REPLY = ("أبشر، بلّغت زميلي.", "يتواصل معك إن شاء الله.")


def test_the_replies_section_shows_each_turns_reply_and_tools() -> None:
    """Owner-approved 2026-10-01: a failed check must be readable, not just
    counted -- synthetic data only."""
    text = render_replies(
        [
            _result(
                scenario_key="booking_yes_gulf",
                setting="low",
                booking_ok=False,
                reply_text="\n".join(_TWO_LINE_REPLY),
                tool_names=(),
            ),
            _result(
                scenario_key="booking_yes_en",
                setting="low",
                reply_text="Done.",
                tool_names=("request_booking_follow_up",),
            ),
            _result(
                error_type="ModelUnavailableError",
                guard_allowed=None,
                reply_text=None,
            ),
        ]
    )
    assert "Synthetic scenarios only" in text
    assert "1. booking_yes_gulf (low) — FAIL — tools: none" in text
    assert "\n".join(f"> {line}" for line in _TWO_LINE_REPLY) in text
    assert "2. booking_yes_en (low) — PASS — tools: request_booking_follow_up" in text
    assert "> (no reply: ModelUnavailableError)" in text
