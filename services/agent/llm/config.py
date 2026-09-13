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
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from services.agent.llm.errors import LlmConfigurationError

ALLOWED_MODELS: frozenset[str] = frozenset({"gemini-3.7-flash"})

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

_DEFAULT_TIMEOUT_MS = 20_000


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

    Raises:
        LlmConfigurationError: LLM_MODEL is unset or not in
            ALLOWED_MODELS, LLM_API_KEY is unset, or a numeric setting is
            missing, non-numeric, or not positive.
    """
    active_env = env if env is not None else os.environ

    model = _require(active_env, "LLM_MODEL")
    if model not in ALLOWED_MODELS:
        raise LlmConfigurationError(
            f"LLM_MODEL={model!r} is not in the reviewed allow-list "
            f"{sorted(ALLOWED_MODELS)!r}"
        )

    return LlmSettings(
        model=model,
        api_key=_require(active_env, "LLM_API_KEY"),
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
