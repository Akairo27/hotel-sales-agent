"""Paid, opt-in comparison of candidate models on the real agent pipeline.

Not a pytest module and not part of CI: it makes real, billed API calls,
the same posture as tests/generate_season_conformance_fixtures.py. Run it
by hand:

    EVAL_DATABASE_URL=... OPENROUTER_API_KEY=... \\
    python -m tests.eval_model_candidates --confirm-scratch-db \\
        --model deepseek/deepseek-v4-pro-0813 --provider <approved-slug> ...

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
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql

from services.agent.llm.client import (
    GeminiTransport,
    ModelTransport,
    OpenRouterTransport,
)
from services.agent.llm.config import GEMINI_MODELS, LlmSettings
from services.agent.llm.conversation import generate_reply
from services.agent.llm.errors import LlmConfigurationError, LlmError
from services.agent.llm.model_types import ModelResponse, Turn
from services.agent.output_guard.enforcement import enforce_outbound_text
from tests.conftest import _TABLES_TO_TRUNCATE
from tests.eval_scenarios import (
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
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel_and_room_type,
    seed_message,
    seed_price_rule,
    seed_season,
)

EVAL_TIMEOUT_MS = 10_000
EVAL_TOTAL_ROOMS = 5
EVAL_COST_PER_NIGHT_HALALAS = 10_000
EVAL_TARGET_MARGIN_BPS = 2_000
EVAL_MIN_PROFIT_HALALAS = 1_000
SEEDED_HOTEL_NAME = "Test Hotel"

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
    """Counts every model call, including ones that raise."""

    def __init__(self, inner: ModelTransport) -> None:
        self._inner = inner
        self.calls = 0

    async def generate(
        self, *, turns: list[Turn], system_instruction: str
    ) -> ModelResponse:
        self.calls += 1
        return await self._inner.generate(
            turns=turns, system_instruction=system_instruction
        )


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
            "SELECT COUNT(*) FROM hotels WHERE hotel_name <> %s", (SEEDED_HOTEL_NAME,)
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


def seed_eval_database(conn: psycopg.Connection[Any]) -> None:
    """Truncates every table, then seeds one hotel with one room type,
    a default season, one wide window of inventory, and one global price
    rule -- the same shape tests/integration/test_llm_dispatch_integration.py
    prices against. The connection must be autocommit (seed_price_rule's
    session-scoped actor setting depends on it)."""
    conn.execute(
        sql.SQL("TRUNCATE {tables} RESTART IDENTITY CASCADE").format(
            tables=sql.SQL(", ").join(sql.Identifier(t) for t in _TABLES_TO_TRUNCATE)
        )
    )
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
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
    seed_allotment_nights(
        conn,
        hotel_id,
        room_type_id,
        ALLOTMENT_WINDOW_START,
        nights=ALLOTMENT_WINDOW_NIGHTS,
        total_rooms=EVAL_TOTAL_ROOMS,
        cost_per_night=EVAL_COST_PER_NIGHT_HALALAS,
    )
    seed_price_rule(
        conn,
        scope="global",
        target_margin_bps=EVAL_TARGET_MARGIN_BPS,
        min_profit_by_lead_time=flat_min_profit(EVAL_MIN_PROFIT_HALALAS),
        demand_curve=flat_demand_curve(),
    )


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
    )


async def run_scenario(
    conn: psycopg.Connection[Any],
    *,
    transport: ModelTransport,
    model: str,
    scenario: Scenario,
) -> ScenarioResult:
    """Seeds a fresh database, runs the real generate_reply for the
    scenario's customer message, judges the reply with the real output
    guard, and records the outcome. Only LlmError (the model-side failure
    family: unavailable, malformed usage, tool-loop limit, bad tool
    arguments, ...) is recorded as an error; anything else is a bug in the
    harness or its seed and is left to crash the run."""
    seed_eval_database(conn)
    conversation_id = seed_conversation(conn)
    seed_message(
        conn, conversation_id, direction="inbound", body=scenario.customer_message
    )
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
            settings=eval_settings(model),
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
            total_tokens=0,
        )
    finally:
        client_logger.removeHandler(retry_counter)
    latency = time.perf_counter() - started
    verdict = enforce_outbound_text(
        conn, conversation_id=conversation_id, text=reply.text
    )
    return ScenarioResult(
        scenario_key=scenario.key,
        model=model,
        error_type=None,
        stay_tool_ok=stay_tool_call_matches(scenario, reply.tool_calls),
        quote_ok=quote_was_priced(scenario, reply.tool_calls),
        guard_allowed=verdict.allowed,
        leaked=reply_leaked(scenario, reply.text),
        retries=retry_counter.retries,
        malformed_retries=retry_counter.malformed_retries,
        model_calls=counting.calls,
        latency_seconds=latency,
        total_tokens=reply.usage.total_tokens,
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise EvalConfigurationError(f"{name} is not set")
    return value


def build_transports(
    models: Sequence[str], providers: Sequence[str], *, include_gemini_baseline: bool
) -> list[tuple[str, ModelTransport]]:
    """The OpenRouter candidates get the providers passed on the command
    line -- never OPENROUTER_ROUTES, which stays empty until a provider is
    approved. Synthetic scenario data is all that is ever sent."""
    transports: list[tuple[str, ModelTransport]] = []
    if include_gemini_baseline:
        gemini_model = _require_env("LLM_MODEL")
        if gemini_model not in GEMINI_MODELS:
            raise EvalConfigurationError(
                f"LLM_MODEL={gemini_model!r} is not a Gemini model; "
                "the baseline needs the deployed Gemini one"
            )
        settings = eval_settings(gemini_model, api_key=_require_env("LLM_API_KEY"))
        transports.append((gemini_model, GeminiTransport(settings)))
    if models:
        api_key = _require_env("OPENROUTER_API_KEY")
        for model in models:
            transports.append(
                (
                    model,
                    OpenRouterTransport(
                        model=model,
                        api_key=api_key,
                        providers=tuple(providers),
                        timeout_ms=EVAL_TIMEOUT_MS,
                    ),
                )
            )
    if not transports:
        raise EvalConfigurationError(
            "nothing to evaluate: pass --model and/or --include-gemini-baseline"
        )
    return transports


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
    return parser.parse_args(argv)


async def _run_all(
    conn: psycopg.Connection[Any],
    transports: Sequence[tuple[str, ModelTransport]],
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for model, transport in transports:
        for scenario in SCENARIOS:
            results.append(
                await run_scenario(
                    conn, transport=transport, model=model, scenario=scenario
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
    transports = build_transports(
        args.model, args.provider, include_gemini_baseline=args.include_gemini_baseline
    )
    with psycopg.connect(url, autocommit=True) as conn:
        require_only_seeded_hotels(conn)
        results = asyncio.run(_run_all(conn, transports))
    sys.stdout.write(render_results_table(results) + "\n\n")
    sys.stdout.write(render_model_summary(results) + "\n")
    return 0


def _cli() -> int:
    try:
        return main()
    except (EvalConfigurationError, LlmConfigurationError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
