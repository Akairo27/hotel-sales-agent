"""Paid, opt-in comparison of candidate models on the real agent pipeline.

Not a pytest module and not part of CI: it makes real, billed API calls,
the same posture as tests/generate_season_conformance_fixtures.py. Run it
by hand:

    EVAL_DATABASE_URL=... OPENROUTER_API_KEY=... \\
    python -m tests.eval_model_candidates --confirm-scratch-db \\
        --model deepseek/deepseek-v4-pro-0813 --provider <approved-slug> ...

Reasoning settings (added 2026-09-30): --reasoning-effort is repeatable;
"default" sends no reasoning field (production until 2026-09-30, when
"low" was chosen -- ARCHITECTURE.md §10), any other value is
sent as OpenRouter's reasoning.effort. --repeat N runs every (setting,
scenario) pair N times, interleaving settings within each repeat, and
--output PATH also writes the report to a file (the manual GitHub workflow
.github/workflows/model-eval.yml uploads it). Model calls use production's
per-attempt timeout and turn budget, so a slow setting is measured, not
cut short.

Each (model, scenario) pair runs the real generate_reply -- real prompt,
real tool declarations, real dispatch against a seeded database, real
pricing -- then the real enforce_outbound_text output guard on the reply,
and records what happened: did the model call the right tool for the right
dates, did the guard block its reply, did it leak the system prompt, how
many retries and how long. Nothing here reimplements product logic.

The database is TRUNCATED before every scenario. It must be a scratch
database that already has the migrations applied (the one TEST_DATABASE_URL
points at, after a normal test run, is exactly that). This module refuses to
run against DATABASE_URL, without --confirm-scratch-db, or if `hotels`
holds anything it did not seed itself.

The customer messages below are new text, written per CLAUDE.md §6's
adversarial categories and from the real reported relative-date phrases
(tests/unit/test_llm_prompt.py). tests/adversarial/test_output_guard.py's
_CASES cannot be reused as inputs: those are candidate model OUTPUTS the
guard is judged on, not customer messages.

Dollar cost is not computed: providers price differently and the routed
provider is OpenRouter's choice within the allowlist. Tokens are reported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from services.agent.llm.client import (
    GeminiTransport,
    ModelTransport,
    OpenRouterTransport,
)
from services.agent.llm.config import (
    GEMINI_MODELS,
    MODEL_ATTEMPT_TIMEOUT_MS,
    REASONING_EFFORTS,
    LlmSettings,
    ReasoningEffort,
)
from services.agent.llm.conversation import generate_reply
from services.agent.llm.errors import (
    LlmConfigurationError,
    LlmError,
    ModelUnavailableError,
)
from services.agent.llm.model_types import ModelResponse, Turn
from services.agent.output_guard.enforcement import enforce_outbound_text
from tests.conftest import _TABLES_TO_TRUNCATE
from tests.eval_scenarios import (
    SCENARIO_CATEGORIES,
    SEEDED_ARABIC_HOTEL_NAME,
    SEEDED_HOTEL_NAME,
    Scenario,
    ScenarioResult,
    asked_instead_of_guessing,
    booking_answer_handled,
    booking_passed_on,
    buttons_attachable,
    hotel_confirmed_before_pricing,
    hotel_name_retried_and_confirmed,
    quote_reply_complete,
    quote_was_priced,
    render_model_summary,
    render_replies,
    render_results_table,
    reply_leaked,
    reply_register_ok,
    scenario_now,
    scenarios_in,
    stay_tool_call_matches,
)
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel,
    seed_message,
    seed_price_rule,
    seed_quote,
    seed_room_type,
    seed_season,
)

# Production's own per-attempt ceiling (the turn budget comes from
# generate_reply itself): with a shorter one a slower reasoning setting
# would time out here and look worse than it is in production.
EVAL_TIMEOUT_MS = MODEL_ATTEMPT_TIMEOUT_MS
# The --reasoning-effort value that sends no reasoning field at all.
DEFAULT_SETTING = "default"
EVAL_TOTAL_ROOMS = 5
EVAL_COST_PER_NIGHT_HALALAS = 10_000
EVAL_TARGET_MARGIN_BPS = 2_000
EVAL_MIN_PROFIT_HALALAS = 1_000

# A fixed, generous cap set: this harness measures model behavior, not the
# caps (which have their own tests), so none of them may trip mid-run.
_EVAL_MAX_CONVERSATION_TURNS = 20
_EVAL_MAX_TOKENS_PER_CONVERSATION = 1_000_000
_EVAL_MAX_SPEND_PER_DAY_USD = Decimal("1000")
_EVAL_MAX_MESSAGES_PER_NUMBER_PER_DAY = 1_000

# The seeded inventory: one window wide enough to cover every scenario.
ALLOTMENT_WINDOW_START = date(2026, 9, 21)
ALLOTMENT_WINDOW_NIGHTS = 30

_CLIENT_LOGGER_NAME = "services.agent.llm.client"
_RETRY_EVENT = "model_call_retry"
_OPENROUTER_CALL_ERROR = "OpenRouterCallError"


class EvalConfigurationError(Exception):
    """The harness was asked to run in a way that is unsafe or incomplete:
    a missing environment variable, a database that is not a scratch
    database, or a missing confirmation flag."""


class _CountingTransport:
    """Counts every model call, including ones that raise, and adds up the
    token usage of the ones that return -- input, output, and the
    reasoning part of the output (None until some call reports it)."""

    def __init__(self, inner: ModelTransport) -> None:
        self._inner = inner
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens: int | None = None

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        self.calls += 1
        response = await self._inner.generate(
            turns=turns, system_instruction=system_instruction, deadline=deadline
        )
        self.input_tokens += response.usage.prompt_tokens
        self.output_tokens += response.usage.candidates_tokens
        if response.usage.reasoning_tokens is not None:
            self.reasoning_tokens = (
                self.reasoning_tokens or 0
            ) + response.usage.reasoning_tokens
        return response


@dataclass(frozen=True)
class EvalTarget:
    """One model under one reasoning setting."""

    model: str
    setting: str
    transport: ModelTransport


class _RetryCounter(logging.Handler):
    """Counts the retry events services.agent.llm.client logs. A retry of
    an OpenRouterCallError with no HTTP status is a malformed or partial
    reply (broken tool-call JSON, an empty choice) -- counted separately,
    since tool-calling reliability is the known risk for some candidates."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.retries = 0
        self.malformed_retries = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = json.loads(record.getMessage())
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("event") != _RETRY_EVENT:
            return
        self.retries += 1
        if (
            event.get("exception_type") == _OPENROUTER_CALL_ERROR
            and "status_code" not in event
        ):
            self.malformed_retries += 1


def require_scratch_database(url: str | None, *, production_url: str | None) -> str:
    """Raises EvalConfigurationError unless url is set and is not the
    production database URL."""
    if not url:
        raise EvalConfigurationError("EVAL_DATABASE_URL is not set")
    if production_url and url == production_url:
        raise EvalConfigurationError(
            "EVAL_DATABASE_URL equals DATABASE_URL: refusing to truncate the "
            "production database"
        )
    return url


def require_only_seeded_hotels(conn: psycopg.Connection[Any]) -> None:
    """Refuses a database holding any hotel this harness did not seed: the
    harness truncates everything, so real data must never be there."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM hotels WHERE hotel_name <> ALL(%s)",
            ([SEEDED_HOTEL_NAME, SEEDED_ARABIC_HOTEL_NAME],),
        ).fetchone()
    except psycopg.errors.UndefinedTable as exc:
        raise EvalConfigurationError(
            "the `hotels` table does not exist: apply the migrations to the "
            "scratch database first (a normal pytest run against "
            "TEST_DATABASE_URL does this)"
        ) from exc
    if row is None or row[0] != 0:
        raise EvalConfigurationError(
            "refusing to run: `hotels` contains rows this harness did not seed"
        )


def _seed_bookable_hotel(
    conn: psycopg.Connection[Any], *, hotel_name: str, distance_to_haram_meters: int
) -> None:
    """One active, complete-profile Makkah hotel with a Standard room type
    and the whole inventory window."""
    hotel_id = seed_hotel(
        conn,
        hotel_name=hotel_name,
        city="makkah",
        zone="makkah_central",
        star_rating=4,
        distance_to_haram_meters=distance_to_haram_meters,
        address_text="Test address",
    )
    room_type_id = seed_room_type(conn, hotel_id, room_type_name="Standard")
    seed_allotment_nights(
        conn,
        hotel_id,
        room_type_id,
        ALLOTMENT_WINDOW_START,
        nights=ALLOTMENT_WINDOW_NIGHTS,
        total_rooms=EVAL_TOTAL_ROOMS,
        cost_per_night=EVAL_COST_PER_NIGHT_HALALAS,
    )


def seed_eval_database(conn: psycopg.Connection[Any]) -> None:
    """Truncates every table, then seeds two hotels, each with one room
    type and one wide window of inventory -- SEEDED_HOTEL_NAME (ids 1/1)
    and SEEDED_ARABIC_HOTEL_NAME (ids 2/2), whose Arabic stored name only
    the hotel-name scenarios need -- plus a default season and one global
    price rule: the same shape tests/integration/test_llm_dispatch_integration.py
    prices against. The connection must be autocommit (seed_price_rule's
    session-scoped actor setting depends on it).

    The hotel is seeded with a full, complete profile (city/zone/star/
    distance/address), not the bare seed_hotel_and_room_type shape used
    elsewhere in this test suite: search_hotels (services/agent/llm/
    dispatch.py) only ever resolves an active hotel with a complete
    profile, and a real scenario now has to call it before
    check_availability/get_quote."""
    conn.execute(
        sql.SQL("TRUNCATE {tables} RESTART IDENTITY CASCADE").format(
            tables=sql.SQL(", ").join(sql.Identifier(t) for t in _TABLES_TO_TRUNCATE)
        )
    )
    _seed_bookable_hotel(
        conn, hotel_name=SEEDED_HOTEL_NAME, distance_to_haram_meters=350
    )
    _seed_bookable_hotel(
        conn, hotel_name=SEEDED_ARABIC_HOTEL_NAME, distance_to_haram_meters=800
    )
    seed_season(
        conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )
    seed_price_rule(
        conn,
        scope="global",
        target_margin_bps=EVAL_TARGET_MARGIN_BPS,
        min_profit_by_lead_time=flat_min_profit(EVAL_MIN_PROFIT_HALALAS),
        demand_curve=flat_demand_curve(),
    )


# SEEDED_HOTEL_NAME and its one room type: seeded first, after the
# identity restart (seed_eval_database).
_SEEDED_HOTEL_ID = 1
_SEEDED_ROOM_TYPE_ID = 1
_SEEDED_SEASON_ID = 1
# A seeded quote's floor per night, below every scenario's nightly price.
_SEEDED_NIGHT_FLOOR_HALALAS = 30_000
# Around a seeded quote: the conversation's first message comes this long
# before it (so the quote falls inside the session), and the replies after
# it this far apart.
_BEFORE_THE_QUOTE = timedelta(seconds=30)
_BETWEEN_EARLIER_MESSAGES = timedelta(seconds=5)


def _seeded_quote_nights(check_in: date, night_asks: tuple[int, ...]) -> str:
    return json.dumps(
        [
            {
                "date": (check_in + timedelta(days=index)).isoformat(),
                "season_id": _SEEDED_SEASON_ID,
                "ask": ask,
                "min_allowed": _SEEDED_NIGHT_FLOOR_HALALAS,
                "override_applied": True,
            }
            for index, ask in enumerate(night_asks)
        ]
    )


def _seed_quote_and_earlier_messages(
    conn: psycopg.Connection[Any], conversation_id: int, scenario: Scenario
) -> None:
    """The scenario's quote, made seeded_quote_minutes_ago minutes before
    now on the database's own clock (the one quote validity is measured
    by), inside the session its first earlier message opens, with the
    rest of the earlier messages just after it."""
    assert scenario.seeded_quote_minutes_ago is not None
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    quoted_at: datetime = row[0] - timedelta(minutes=scenario.seeded_quote_minutes_ago)
    (first_direction, first_body), *later = scenario.earlier_messages
    seed_message(
        conn,
        conversation_id,
        direction=first_direction,
        body=first_body,
        created_at=quoted_at - _BEFORE_THE_QUOTE,
    )
    check_in, check_out = date(2026, 10, 5), date(2026, 10, 7)
    asks = scenario.seeded_quote_night_asks
    seed_quote(
        conn,
        _SEEDED_HOTEL_ID,
        _SEEDED_ROOM_TYPE_ID,
        conversation_id=conversation_id,
        ask_price_total=sum(asks),
        min_allowed_total=_SEEDED_NIGHT_FLOOR_HALALAS * len(asks),
        nights=_seeded_quote_nights(check_in, asks),
        created_at=quoted_at,
        check_in=check_in,
        check_out=check_out,
    )
    for index, (direction, body) in enumerate(later, start=1):
        seed_message(
            conn,
            conversation_id,
            direction=direction,
            body=body,
            created_at=quoted_at + index * _BETWEEN_EARLIER_MESSAGES,
        )


def _seed_scenario_conversation(
    conn: psycopg.Connection[Any], scenario: Scenario
) -> int:
    """The scenario's conversation: its earlier messages (and quote, if it
    has one), then the customer's message. Returns the conversation id."""
    conversation_id = seed_conversation(conn)
    if scenario.seeded_quote_minutes_ago is None:
        for direction, body in scenario.earlier_messages:
            seed_message(conn, conversation_id, direction=direction, body=body)
    else:
        _seed_quote_and_earlier_messages(conn, conversation_id, scenario)
    seed_message(
        conn, conversation_id, direction="inbound", body=scenario.customer_message
    )
    return conversation_id


def eval_settings(
    model: str, *, api_key: str = "unused-by-generate-reply"
) -> LlmSettings:
    return LlmSettings(
        model=model,
        api_key=api_key,
        timeout_ms=EVAL_TIMEOUT_MS,
        max_conversation_turns=_EVAL_MAX_CONVERSATION_TURNS,
        max_tokens_per_conversation=_EVAL_MAX_TOKENS_PER_CONVERSATION,
        max_spend_per_day_usd=_EVAL_MAX_SPEND_PER_DAY_USD,
        max_messages_per_number_per_day=_EVAL_MAX_MESSAGES_PER_NUMBER_PER_DAY,
        max_tokens_per_number_per_day=_EVAL_MAX_TOKENS_PER_CONVERSATION,
    )


async def run_scenario(
    conn: psycopg.Connection[Any],
    *,
    transport: ModelTransport,
    model: str,
    scenario: Scenario,
    setting: str = DEFAULT_SETTING,
) -> ScenarioResult:
    """Seeds a fresh database, stores the scenario's earlier messages (if
    any) and its customer message, runs the real generate_reply, judges
    the reply with the real output
    guard, and records the outcome. Only LlmError (the model-side failure
    family: unavailable, malformed usage, tool-loop limit, an unknown
    tool, ...) is recorded as an error; anything else is a bug in the
    harness or its seed and is left to crash the run. A bad tool argument
    is not an error here: generate_reply hands the model a fixed tool
    error and the turn goes on, as in production."""
    seed_eval_database(conn)
    conversation_id = _seed_scenario_conversation(conn, scenario)
    settings = eval_settings(model)
    counting = _CountingTransport(transport)
    retry_counter = _RetryCounter()
    client_logger = logging.getLogger(_CLIENT_LOGGER_NAME)
    client_logger.addHandler(retry_counter)
    started = time.perf_counter()
    try:
        reply = await generate_reply(
            conn,
            conversation_id=conversation_id,
            customer_name=None,
            transport=counting,
            settings=settings,
            now=scenario_now(scenario),
        )
    except LlmError as exc:
        return ScenarioResult(
            scenario_key=scenario.key,
            model=model,
            error_type=type(exc).__name__,
            stay_tool_ok=None,
            quote_ok=None,
            guard_allowed=None,
            leaked=False,
            retries=retry_counter.retries,
            malformed_retries=retry_counter.malformed_retries,
            model_calls=counting.calls,
            latency_seconds=time.perf_counter() - started,
            total_tokens=counting.input_tokens + counting.output_tokens,
            setting=setting,
            input_tokens=counting.input_tokens,
            output_tokens=counting.output_tokens,
            reasoning_tokens=counting.reasoning_tokens,
            error_detail=(str(exc) if isinstance(exc, ModelUnavailableError) else None),
        )
    finally:
        client_logger.removeHandler(retry_counter)
    latency = time.perf_counter() - started
    verdict = enforce_outbound_text(
        conn,
        conversation_id=conversation_id,
        text=reply.text,
        quote_validity=settings.quote_validity,
        booking_passed_on=booking_passed_on(reply.tool_calls),
    )
    return ScenarioResult(
        scenario_key=scenario.key,
        model=model,
        error_type=None,
        stay_tool_ok=stay_tool_call_matches(scenario, reply.tool_calls),
        quote_ok=quote_was_priced(scenario, reply.tool_calls),
        clarified_ok=asked_instead_of_guessing(scenario, reply.tool_calls),
        quote_reply_ok=quote_reply_complete(scenario, reply.tool_calls, reply.text),
        name_retry_ok=hotel_name_retried_and_confirmed(
            scenario, reply.tool_calls, reply.text
        ),
        hotel_confirmed_ok=hotel_confirmed_before_pricing(
            scenario, reply.tool_calls, reply.text
        ),
        booking_ok=booking_answer_handled(scenario, reply.tool_calls, reply.text),
        buttons_ok=buttons_attachable(scenario, reply.quote_ids, reply.text),
        guard_allowed=verdict.allowed,
        register_ok=reply_register_ok(reply.text),
        leaked=reply_leaked(scenario, reply.text),
        reply_text=reply.text,
        tool_names=tuple(call.name for call in reply.tool_calls),
        retries=retry_counter.retries,
        malformed_retries=retry_counter.malformed_retries,
        model_calls=counting.calls,
        latency_seconds=latency,
        total_tokens=reply.usage.total_tokens,
        setting=setting,
        input_tokens=counting.input_tokens,
        output_tokens=counting.output_tokens,
        reasoning_tokens=counting.reasoning_tokens,
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise EvalConfigurationError(f"{name} is not set")
    return value


def build_transports(
    models: Sequence[str],
    providers: Sequence[str],
    *,
    include_gemini_baseline: bool,
    reasoning_efforts: Sequence[str] = (DEFAULT_SETTING,),
) -> list[EvalTarget]:
    """The OpenRouter candidates get the providers passed on the command
    line -- never OPENROUTER_ROUTES, which stays empty until a provider is
    approved -- one target per reasoning setting. The Gemini baseline runs
    once, under the default setting. Synthetic scenario data is all that is
    ever sent."""
    transports: list[EvalTarget] = []
    if include_gemini_baseline:
        gemini_model = _require_env("LLM_MODEL")
        if gemini_model not in GEMINI_MODELS:
            raise EvalConfigurationError(
                f"LLM_MODEL={gemini_model!r} is not a Gemini model; "
                "the baseline needs the deployed Gemini one"
            )
        settings = eval_settings(gemini_model, api_key=_require_env("LLM_API_KEY"))
        transports.append(
            EvalTarget(gemini_model, DEFAULT_SETTING, GeminiTransport(settings))
        )
    if models:
        api_key = _require_env("OPENROUTER_API_KEY")
        for model in models:
            for setting in reasoning_efforts:
                transports.append(
                    EvalTarget(
                        model,
                        setting,
                        OpenRouterTransport(
                            model=model,
                            api_key=api_key,
                            providers=tuple(providers),
                            timeout_ms=EVAL_TIMEOUT_MS,
                            reasoning_effort=_reasoning_effort(setting),
                        ),
                    )
                )
    if not transports:
        raise EvalConfigurationError(
            "nothing to evaluate: pass --model and/or --include-gemini-baseline"
        )
    return transports


def _reasoning_effort(setting: str) -> ReasoningEffort | None:
    if setting == DEFAULT_SETTING:
        return None
    for effort in REASONING_EFFORTS:
        if effort == setting:
            return effort
    raise EvalConfigurationError(f"unknown reasoning effort {setting!r}")


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paid, opt-in comparison of candidate models on the agent pipeline."
    )
    parser.add_argument("--model", action="append", default=[], help="OpenRouter slug")
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="OpenRouter provider slug allowed for this run (repeatable)",
    )
    parser.add_argument("--include-gemini-baseline", action="store_true")
    parser.add_argument(
        "--confirm-scratch-db",
        action="store_true",
        help="EVAL_DATABASE_URL is a scratch database that may be truncated",
    )
    parser.add_argument(
        "--reasoning-effort",
        action="append",
        choices=[DEFAULT_SETTING, *REASONING_EFFORTS],
        help=(
            "OpenRouter reasoning.effort to compare (repeatable); "
            f"{DEFAULT_SETTING!r} sends none. Defaults to {DEFAULT_SETTING!r} only."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=1,
        help="run every (setting, scenario) pair this many times",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        choices=SCENARIO_CATEGORIES,
        help="run only the scenarios in this category (repeatable); all by default",
    )
    parser.add_argument("--output", help="also write the report to this file")
    args = parser.parse_args(argv)
    args.reasoning_effort = args.reasoning_effort or [DEFAULT_SETTING]
    return args


async def _run_all(
    conn: psycopg.Connection[Any],
    targets: Sequence[EvalTarget],
    *,
    scenarios: Sequence[Scenario],
    repeat: int,
) -> list[ScenarioResult]:
    """Every target runs every given scenario, `repeat` times. The targets
    are interleaved within each repeat, so a provider's slow spell falls on
    every setting alike rather than on whichever ran then."""
    results: list[ScenarioResult] = []
    for _ in range(repeat):
        for target in targets:
            for scenario in scenarios:
                results.append(
                    await run_scenario(
                        conn,
                        transport=target.transport,
                        model=target.model,
                        scenario=scenario,
                        setting=target.setting,
                    )
                )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.confirm_scratch_db:
        raise EvalConfigurationError(
            "this script TRUNCATES every table in the database at "
            "EVAL_DATABASE_URL; pass --confirm-scratch-db only for a scratch "
            "database"
        )
    url = require_scratch_database(
        os.environ.get("EVAL_DATABASE_URL"),
        production_url=os.environ.get("DATABASE_URL"),
    )
    targets = build_transports(
        args.model,
        args.provider,
        include_gemini_baseline=args.include_gemini_baseline,
        reasoning_efforts=args.reasoning_effort,
    )
    with psycopg.connect(url, autocommit=True) as conn:
        require_only_seeded_hotels(conn)
        results = asyncio.run(
            _run_all(
                conn,
                targets,
                scenarios=scenarios_in(args.category),
                repeat=args.repeat,
            )
        )
    report = "\n\n".join(
        (
            render_model_summary(results),
            render_results_table(results),
            render_replies(results),
        )
    )
    sys.stdout.write(report + "\n")
    if args.output:
        Path(args.output).write_text(report + "\n", encoding="utf-8")
    return 0


def _cli() -> int:
    try:
        return main()
    except (EvalConfigurationError, LlmConfigurationError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
