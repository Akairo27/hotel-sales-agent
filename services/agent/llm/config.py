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
approved to serve it, and its token rates. OPENROUTER_ROUTES is empty
today, so no OpenRouter model is selectable and nothing about the running
Gemini deployment changes until a route is deliberately added.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from services.agent.llm.errors import LlmConfigurationError
from services.agent.llm.pricing import GEMINI_FLASH_RATES, TokenRates

GEMINI_MODELS: frozenset[str] = frozenset({"gemini-3.7-flash"})


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
    """

    providers: tuple[str, ...]
    token_rates: TokenRates


OPENROUTER_ROUTES: Mapping[str, OpenRouterRoute] = {}

ALLOWED_MODELS: frozenset[str] = GEMINI_MODELS | frozenset(OPENROUTER_ROUTES)

# ARCHITECTURE.md §7: "the last 10 messages only" is sent as context on
# every call — not the full conversation history.
MESSAGE_WINDOW = 10

# A customer turn against check_availability/get_quote resolves in at most
# a couple of tool calls. Beyond this, the loop stops and raises rather
# than continuing to spend tokens (CLAUDE.md §9's per-conversation cap).
MAX_TOOL_ITERATIONS = 4

# The customer's WhatsApp display name is customer-controlled text that
# ends up inside the model's SYSTEM instruction (prompt.py), not inside a
# user-turn message — injection_resistance's "treat customer text as
# ordinary conversation" framing doesn't cover it by itself. Capping
# length is half of that defense (see prompt.sanitize_customer_name for
# the other half, character filtering); 60 is generous for any real
# name and short enough that an injected paragraph cannot fit.
MAX_CUSTOMER_NAME_LENGTH = 60

# Lowered from 20_000: this is a WhatsApp conversation, not a batch job.
# A customer waiting silently past ~30 seconds assumes the bot is
# broken, and escalating to a human at that point (see webhook.py's
# _escalate_and_notify) is a better outcome than a longer wait that may
# still fail. See client.py's own retry constants for the matching
# reasoning on attempt count and backoff.
_DEFAULT_TIMEOUT_MS = 10_000


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
    token_rates: TokenRates = GEMINI_FLASH_RATES
    openrouter_route: OpenRouterRoute | None = None


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
        timeout_ms=_DEFAULT_TIMEOUT_MS,
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
    )
