"""Configuration and named constants for services/agent/llm — CLAUDE.md §9.

ALLOWED_MODELS is the reviewed pin: LLM_MODEL must name one of these exact
values or settings fail to load. This is deliberately not "whatever
LLM_MODEL says" — CLAUDE.md §9 requires the exact model version to be
pinned and reviewed, so a deployment cannot silently move to an unreviewed
model just by changing an environment variable.

"gemini-3.7-flash" was confirmed live via `client.models.list()` against a
real API key (not recalled from training data) — it reports a dated
snapshot version ("3.7-flash-08-2026", not a "-latest"/"-preview" floating
alias) and supports generateContent. Every entry point that needs a model
(load_llm_settings) fails loudly with LlmConfigurationError if LLM_MODEL
names anything else; there is no silent fallback.

A model reachable through OpenRouter is added the same reviewed way: one
OPENROUTER_ROUTES entry naming its exact OpenRouter slug, the providers
approved to serve it, and its token rates. The one route below makes
GLM-5.3 selectable, but the running Gemini deployment changes only when
LLM_MODEL is deliberately set to it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, get_args

from services.agent.llm.errors import LlmConfigurationError
from services.agent.llm.pricing import GEMINI_FLASH_RATES, TokenRates

GEMINI_MODELS: frozenset[str] = frozenset({"gemini-3.7-flash"})


# OpenRouter's documented reasoning.effort values ("Reasoning Tokens" page,
# read 2026-09-30); "none" disables reasoning entirely -- GLM-5.3's
# providers reject it with HTTP 400 (eval run 36674059327).
ReasoningEffort = Literal["max", "xhigh", "high", "medium", "low", "minimal", "none"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = get_args(ReasoningEffort)


@dataclass(frozen=True)
class OpenRouterRoute:
    """Everything reviewed about serving one model through OpenRouter.

    providers is an allowlist of OpenRouter provider slugs, deny-by-default
    (CLAUDE.md rule 10's posture applied to data processors): a provider
    not named here can never receive a customer's conversation. An empty
    tuple means no provider has been approved yet, and
    OpenRouterTransport refuses to construct from it.

    token_rates must be the HIGHEST rates among the approved providers, so
    the daily spend cap (CLAUDE.md §9) errs toward tripping early, never
    late, whichever approved provider OpenRouter picks.

    reasoning_effort, when set, is sent as OpenRouter's reasoning.effort on
    every call; None sends no reasoning field (the model's own default).
    Every approved provider must accept it: the routing sets
    require_parameters, so a provider that does not is never picked.
    """

    providers: tuple[str, ...]
    token_rates: TokenRates
    reasoning_effort: ReasoningEffort | None = None


# The dated snapshot slug, not the bare "z-ai/glm-5.3" alias (CLAUDE.md §9);
# OpenRouter accepted both in a live call on 2026-09-24. Providers in
# priority order, per ARCHITECTURE.md §10's recorded decision. The rates are
# Crusoe's, the higher of the two (InferenceNet: 0.90 / 3.00): OpenRouter's
# endpoint data on 2026-09-24, and a live call billed exactly this rate.
OPENROUTER_ROUTES: Mapping[str, OpenRouterRoute] = {
    "z-ai/glm-5.3-20260816": OpenRouterRoute(
        providers=("crusoe", "inference-net"),
        token_rates=TokenRates(
            input_usd_per_million_tokens=Decimal("1.40"),
            output_usd_per_million_tokens=Decimal("4.40"),
        ),
        # Owner decision 2026-09-30, from eval run 36674059327 (9 scenarios
        # x 3 repeats per setting): the same tool-calling results as the
        # default (15/15 right stay, every quote priced), median turn 2.5s
        # instead of 7.6s, worst 4.7s instead of 59.4s, and about 80% fewer
        # output tokens. Both providers accepted it; "none" was rejected.
        reasoning_effort="low",
    ),
}

ALLOWED_MODELS: frozenset[str] = GEMINI_MODELS | frozenset(OPENROUTER_ROUTES)

# ARCHITECTURE.md §7: "the last 10 messages only" is sent as context on
# every call — not the full conversation history.
MESSAGE_WINDOW = 10

# A conversation session ends after this long with no message in either
# direction (see services.agent.llm.session). Chosen by the owner
# (2026-09-24): long enough that a customer stepping away for a few hours
# keeps their context, short enough that "hello" the next day starts fresh.
SESSION_IDLE_GAP = timedelta(hours=6)

# How long a quoted price stays valid (owner decision 2026-09-30): the
# output guard accepts a stated amount only from a quote this recent
# (services/agent/output_guard/quotes.py), and the current-stay line tells
# the model until when it may repeat the price. QUOTE_VALIDITY_MINUTES in
# the environment overrides it.
DEFAULT_QUOTE_VALIDITY_MINUTES = 30

# token_usage rows are stamped with the application clock at the start of a
# request (webhook.receive_message's `now`), while messages -- and so a
# session's start -- are stamped by the database when inserted. A session's
# first turn therefore records its usage a moment BEFORE its own first
# message: the fast path's latency plus any clock skew. The per-session
# token sum looks back this far past the session start so that turn still
# counts. An earlier session's usage is always at least SESSION_IDLE_GAP
# older than the start, so this can never pull it in.
SESSION_CLOCK_SKEW_TOLERANCE = timedelta(minutes=1)

# Model calls allowed in one turn before the loop stops and raises rather
# than continuing to spend tokens (CLAUDE.md §9's per-conversation cap).
# Raised from 4 to 6 by the owner (2026-09-29): since a bad tool argument
# became a tool error the model corrects, the correction path alone
# (rejected call, search_hotels, retry, answer) used all 4, so any extra
# step failed the turn. TURN_BUDGET_SECONDS below remains the real bound
# on how long a customer waits.
MAX_TOOL_ITERATIONS = 6

# The customer's WhatsApp display name is customer-controlled text that
# ends up inside the model's SYSTEM instruction (prompt.py), not inside a
# user-turn message — injection_resistance's "treat customer text as
# ordinary conversation" framing doesn't cover it by itself. Capping
# length is half of that defense (see prompt.sanitize_customer_name for
# the other half, character filtering); 60 is generous for any real
# name and short enough that an injected paragraph cannot fit.
MAX_CUSTOMER_NAME_LENGTH = 60

# The ceiling for a single model-call attempt. Raised from 10_000
# (2026-09-24): a real incident showed one retryable failure plus one
# successful retry can each legitimately take close to 10s under
# transient provider latency, and TURN_BUDGET_SECONDS below (not this
# value) is now what actually bounds how long a customer waits --
# client.py's per-attempt timeout is min(this, the turn's remaining
# budget), so raising this cap only lets a single healthy-but-slow
# attempt finish; it does not by itself let a turn run any longer.
MODEL_ATTEMPT_TIMEOUT_MS = 30_000

# The total wall-clock time one turn may spend across every model-call
# attempt and retry, in every tool-calling iteration combined (CLAUDE.md
# §8's "a hanging call must not hang a customer conversation", applied to
# the whole turn rather than one call). services.agent.llm.client checks
# the remaining budget against this before every attempt -- not just the
# first -- and raises TurnBudgetExceededError once it is exhausted,
# instead of starting an attempt it cannot see through. Chosen by the
# owner (2026-09-28) well under ops/hotel-agent.service's TimeoutStopSec
# (120s), so a turn that hits its budget always escalates cleanly rather
# than being killed mid-write by a service restart -- see
# ARCHITECTURE.md's note on both numbers together.
TURN_BUDGET_SECONDS = 75.0


@dataclass(frozen=True)
class LlmSettings:
    """A validated, ready-to-use model configuration."""

    model: str
    api_key: str
    timeout_ms: int
    max_conversation_turns: int
    max_tokens_per_conversation: int
    max_spend_per_day_usd: Decimal
    max_messages_per_number_per_day: int
    max_tokens_per_number_per_day: int
    token_rates: TokenRates = GEMINI_FLASH_RATES
    openrouter_route: OpenRouterRoute | None = None
    quote_validity: timedelta = timedelta(minutes=DEFAULT_QUOTE_VALIDITY_MINUTES)


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        raise LlmConfigurationError(f"{key} is not set")
    return value


def _require_positive_int(env: Mapping[str, str], key: str) -> int:
    raw = _require(env, key)
    try:
        value = int(raw)
    except ValueError as exc:
        raise LlmConfigurationError(f"{key}={raw!r} is not an integer") from exc
    if value <= 0:
        raise LlmConfigurationError(f"{key}={value} must be positive")
    return value


def _optional_positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    """key's positive integer value, or default when it is not set."""
    if not env.get(key, ""):
        return default
    return _require_positive_int(env, key)


def _require_positive_decimal(env: Mapping[str, str], key: str) -> Decimal:
    raw = _require(env, key)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise LlmConfigurationError(f"{key}={raw!r} is not a decimal number") from exc
    # Decimal("inf")/"nan" parse without raising InvalidOperation above, so
    # they need their own check: an infinite cap would never trip
    # DailySpendCapExceededError (CLAUDE.md §9's mandatory daily cap
    # silently disabled), and NaN raises InvalidOperation on the plain
    # `<=` comparison below instead of the intended LlmConfigurationError.
    if not value.is_finite():
        raise LlmConfigurationError(f"{key}={value} must be a finite number")
    if value <= 0:
        raise LlmConfigurationError(f"{key}={value} must be positive")
    return value


def load_llm_settings(env: Mapping[str, str] | None = None) -> LlmSettings:
    """Builds LlmSettings from environment variables.

    A model with an OPENROUTER_ROUTES entry is served through OpenRouter
    and authenticated with OPENROUTER_API_KEY; any other allowed model is
    served by Gemini with LLM_API_KEY.

    Raises:
        LlmConfigurationError: LLM_MODEL is unset or not in
            ALLOWED_MODELS, the selected model's API key (LLM_API_KEY, or
            OPENROUTER_API_KEY for an OpenRouter model) is unset, or a
            numeric setting is missing, non-numeric, or not positive.
    """
    active_env = env if env is not None else os.environ

    model = _require(active_env, "LLM_MODEL")
    if model not in ALLOWED_MODELS:
        raise LlmConfigurationError(
            f"LLM_MODEL={model!r} is not in the reviewed allow-list "
            f"{sorted(ALLOWED_MODELS)!r}"
        )

    route = OPENROUTER_ROUTES.get(model)
    if route is None:
        api_key = _require(active_env, "LLM_API_KEY")
        token_rates = GEMINI_FLASH_RATES
    else:
        api_key = _require(active_env, "OPENROUTER_API_KEY")
        token_rates = route.token_rates

    return LlmSettings(
        model=model,
        api_key=api_key,
        token_rates=token_rates,
        openrouter_route=route,
        timeout_ms=MODEL_ATTEMPT_TIMEOUT_MS,
        max_conversation_turns=_require_positive_int(
            active_env, "MAX_CONVERSATION_TURNS"
        ),
        max_tokens_per_conversation=_require_positive_int(
            active_env, "LLM_MAX_TOKENS_PER_CONVERSATION"
        ),
        max_spend_per_day_usd=_require_positive_decimal(
            active_env, "LLM_MAX_SPEND_PER_DAY_USD"
        ),
        max_messages_per_number_per_day=_require_positive_int(
            active_env, "MAX_MESSAGES_PER_NUMBER_PER_DAY"
        ),
        max_tokens_per_number_per_day=_require_positive_int(
            active_env, "LLM_MAX_TOKENS_PER_NUMBER_PER_DAY"
        ),
        quote_validity=timedelta(
            minutes=_optional_positive_int(
                active_env, "QUOTE_VALIDITY_MINUTES", DEFAULT_QUOTE_VALIDITY_MINUTES
            )
        ),
    )
