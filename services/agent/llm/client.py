"""The one place google-genai is imported for actual model calls —
CLAUDE.md §9: "The model is accessed through one interface module... No
direct SDK calls scattered across the code."

This module is also the only place that translates between
services.agent.llm.model_types (the provider-neutral vocabulary the rest
of services/agent/llm/ speaks) and a specific provider's own wire format.
Adding a second transport (a different provider, or a fallback model)
means a new class here implementing ModelTransport — never a change to
conversation.py, context.py, or tools.py.

ModelTransport is a Protocol so conversation.py's tool-calling loop can be
tested against a fake transport, with no network access and no API key,
per this PR's plan (no live Gemini calls in CI).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Protocol

import httpx
from google import genai
from google.genai import errors, types

from services.agent.llm.config import LlmSettings
from services.agent.llm.errors import ModelUnavailableError, UsageUnavailableError
from services.agent.llm.model_types import (
    ModelResponse,
    ModelTurn,
    ModelUsage,
    ToolCall,
    ToolDeclaration,
    ToolResultTurn,
    Turn,
    UserTurn,
)
from services.agent.llm.tools import AGENT_TOOLS

logger = logging.getLogger(__name__)

# Retries are owned by THIS module, not google-genai's own tenacity-based
# layer (google.genai._api_client.retry_args), even though the SDK
# provides one. Two reasons, the second discovered while adding per-
# attempt logging below:
#
# 1. (Original reasoning, unchanged from the incident that introduced
#    retrying at all.) Left unconfigured, HttpOptions.retry_options
#    resolves to stop_after_attempt(1) with reraise=True: exactly one
#    try, no retry, which is what let three consecutive 503/504s reach a
#    real customer as silent, unretried failures.
# 2. types.HttpRetryOptions has no hook for observing individual
#    attempts (checked against the installed google-genai==2.20.0:
#    attempts/initial_delay/max_delay/exp_base/jitter/http_status_codes
#    only) -- the SDK's own retry_args() hardcodes
#    tenacity.before_sleep_log(logger, logging.INFO) internally, with no
#    way to substitute a safe callback. That default logs
#    f"{exc.__class__.__name__}: {exc}", and for errors.APIError str(exc)
#    includes exc.details -- Google's raw response body, the same leak
#    _wrap_as_model_unavailable below exists to prevent. So visibility
#    into retries requires owning the loop, not just configuring the
#    SDK's.
#
# The numbers below are deliberately tighter than the SDK's own defaults
# (5 attempts, up to 60s between them): those are tuned for one isolated
# call, but this transport sits inside conversation.py's tool-calling
# loop, which can make up to MAX_TOOL_ITERATIONS (4) of these in a single
# turn. This is a WhatsApp conversation, not a batch job -- a customer
# waiting silently past ~30 seconds assumes the bot is broken, and
# escalating to a human at that point (webhook.py's _escalate_and_notify)
# is a better outcome than a longer wait that may still fail anyway. 3
# attempts (2 retries) at up to 10s each (config.py's timeout_ms) plus up
# to ~5s of backoff between them bounds one call at roughly the same
# ~30-35s ceiling, not the SDK's multi-minute worst case.
_RETRY_ATTEMPTS = 3  # including the initial call -- 2 retries.
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 5.0
_RETRY_EXP_BASE = 2.0
_RETRY_JITTER = 1.0

# Google's own transient classification (google.genai._api_client's
# _RETRY_HTTP_STATUS_CODES, based on Google Cloud Storage's published
# retry-strategy) -- pinned here explicitly, not left to the SDK's
# default, so a future SDK upgrade cannot silently change what this
# service retries. 408/429/500/502/503/504 are all server- or
# infrastructure-caused and safe to retry; anything else (400
# API_KEY_INVALID, 403, 404, ...) is a permanent, caller-caused failure
# that retrying can never fix, and google.genai.errors.APIError's own
# 4xx-vs-5xx split (ClientError/ServerError) already keeps those out of
# this set without this module needing to re-derive the split itself.
_RETRY_HTTP_STATUS_CODES = (408, 429, 500, 502, 503, 504)

# Also Google's own classification (google.genai._api_client's
# _HTTPX_TRANSIENT_EXC, checked against the installed version rather
# than re-derived): a request that never reached Google at all --
# connection-level, not an HTTP response -- is transient the same way a
# 503 is.
_RETRYABLE_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.ConnectError)

# Backoff jitter has no security purpose, but ruff/bandit (S311) flags the
# module-level random functions unconditionally regardless of use case --
# random.SystemRandom sidesteps that honestly (os.urandom-backed, not the
# seedable Mersenne Twister) rather than silencing the check.
_jitter_random = random.SystemRandom()


class ModelTransport(Protocol):
    """What conversation.py needs from a model backend — small enough for
    a test fake to implement without touching any provider's real SDK."""

    async def generate(
        self,
        *,
        turns: list[Turn],
        system_instruction: str,
    ) -> ModelResponse: ...


# --- JSON-Schema -> Gemini types.Schema translation ------------------------

_JSON_SCHEMA_TYPE_TO_GEMINI: dict[str, types.Type] = {
    "object": types.Type.OBJECT,
    "string": types.Type.STRING,
    "integer": types.Type.INTEGER,
    "number": types.Type.NUMBER,
    "boolean": types.Type.BOOLEAN,
    "array": types.Type.ARRAY,
}

_KNOWN_JSON_SCHEMA_KEYS = frozenset(
    {"type", "properties", "required", "description", "minimum"}
)


def _expect_optional_str(value: object, *, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise NotImplementedError(
        f"_json_schema_to_gemini_schema expected a string for {field!r}, "
        f"got {type(value).__name__}"
    )


def _expect_optional_number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    # bool is a subclass of int in Python -- excluded explicitly so a
    # stray boolean is never silently accepted as a numeric minimum.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise NotImplementedError(
        f"_json_schema_to_gemini_schema expected a number for {field!r}, "
        f"got {type(value).__name__}"
    )


def _expect_optional_str_list(value: object, *, field: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise NotImplementedError(
        f"_json_schema_to_gemini_schema expected a list of strings for {field!r}, "
        f"got {type(value).__name__}"
    )


def _json_schema_to_gemini_schema(schema: dict[str, object]) -> types.Schema:
    """Translates one services.agent.llm.model_types.ToolDeclaration's
    plain-JSON-Schema parameters into Gemini's own types.Schema tree.

    Scoped to exactly what tools.py uses today (object/string/integer/
    number/boolean; properties/required/description/minimum) -- raises
    rather than silently drop a field or a type this translator does not
    yet handle, so a future tools.py addition (an array-typed property,
    an enum, ...) fails loudly here instead of reaching Gemini silently
    wrong.

    Raises:
        NotImplementedError: schema uses a key or a "type" value this
            function does not yet translate.
    """
    unknown_keys = set(schema.keys()) - _KNOWN_JSON_SCHEMA_KEYS
    if unknown_keys:
        raise NotImplementedError(
            "_json_schema_to_gemini_schema does not yet handle keys: "
            f"{sorted(unknown_keys)}"
        )
    schema_type = schema.get("type")
    if schema_type not in _JSON_SCHEMA_TYPE_TO_GEMINI:
        raise NotImplementedError(
            f"_json_schema_to_gemini_schema does not yet handle type={schema_type!r}"
        )
    properties = schema.get("properties")
    nested_properties = None
    if properties is not None:
        if not isinstance(properties, dict):
            raise NotImplementedError(
                "_json_schema_to_gemini_schema expected a dict for 'properties', "
                f"got {type(properties).__name__}"
            )
        nested_properties = {
            key: _json_schema_to_gemini_schema(value)
            for key, value in properties.items()
        }
    return types.Schema(
        type=_JSON_SCHEMA_TYPE_TO_GEMINI[schema_type],
        description=_expect_optional_str(
            schema.get("description"), field="description"
        ),
        minimum=_expect_optional_number(schema.get("minimum"), field="minimum"),
        required=_expect_optional_str_list(schema.get("required"), field="required"),
        properties=nested_properties,
    )


def _tool_declaration_to_gemini(
    declaration: ToolDeclaration,
) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=declaration.name,
        description=declaration.description,
        parameters=_json_schema_to_gemini_schema(declaration.parameters),
    )


# --- Turn <-> Gemini Content translation ------------------------------------


def _model_turn_to_gemini_content(turn: ModelTurn) -> types.Content:
    """A ModelTurn this same transport produced earlier in the same turn
    carries the exact Gemini Content Google returned as provider_state
    (see model_types.ModelTurn's own docstring) -- reused verbatim,
    preserving whatever Gemini attaches to it (e.g. thought_signature,
    reasoning continuity across a tool-calling turn) that a
    reconstruction from text/tool_calls alone would silently drop.

    A ModelTurn with no such provider_state -- loaded from message
    history (services.agent.llm.context), or produced by a different
    transport -- is reconstructed from text/tool_calls instead; history
    turns are always tool_calls=() (this codebase does not store
    tool-call history), so this exactly matches what context.py already
    built for a past reply before this module existed.
    """
    if isinstance(turn.provider_state, types.Content):
        return turn.provider_state
    parts: list[types.Part] = []
    if turn.text:
        parts.append(types.Part.from_text(text=turn.text))
    for call in turn.tool_calls:
        parts.append(types.Part.from_function_call(name=call.name, args=call.args))
    return types.Content(role="model", parts=parts)


def _turns_to_gemini_contents(turns: list[Turn]) -> list[types.Content]:
    contents: list[types.Content] = []
    for turn in turns:
        if isinstance(turn, UserTurn):
            contents.append(
                types.Content(role="user", parts=[types.Part.from_text(text=turn.text)])
            )
        elif isinstance(turn, ModelTurn):
            contents.append(_model_turn_to_gemini_content(turn))
        elif isinstance(turn, ToolResultTurn):
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=result.name, response=result.result
                        )
                        for result in turn.results
                    ],
                )
            )
        else:
            raise NotImplementedError(f"unhandled turn type: {type(turn).__name__}")
    return contents


def _synthesize_tool_call_id(call: types.FunctionCall, index: int) -> str:
    """Gemini's FunctionCall.id is frequently unset (this module matched
    by order/name before ToolCall.id existed at all) -- always populated
    here regardless, synthesized from position when Gemini doesn't
    supply its own, since a provider-neutral ToolCall.id must be usable
    uniformly even though only some providers' own APIs (an
    OpenAI-compatible one, not Gemini's) actually require it for
    correlation."""
    return call.id or f"call_{index}"


def _gemini_response_to_model_response(
    response: types.GenerateContentResponse,
) -> ModelResponse:
    """Translates one Gemini response into the provider-neutral shape
    the rest of services/agent/llm/ speaks.

    Raises:
        UsageUnavailableError: usage_metadata is absent, or one of its
            three required counts is None -- an untelemetered call is
            not a free call; treating it as zero would let real spend go
            uncounted against every cap this module and caps.py check.
            (Moved here from conversation.py's own _usage_from: reading
            usage_metadata is Gemini-specific.)
    """
    usage = response.usage_metadata
    if usage is None:
        raise UsageUnavailableError("model response carried no usage_metadata")
    if (
        usage.prompt_token_count is None
        or usage.candidates_token_count is None
        or usage.total_token_count is None
    ):
        raise UsageUnavailableError("model response usage_metadata is incomplete")
    # thoughts_token_count (extended-thinking tokens) is folded into
    # candidates_tokens, not tracked on its own: Gemini bills thinking
    # tokens at the output rate, and pricing.estimate_cost_usd's
    # two-bucket formula would silently undercount any call that used
    # extended thinking if this weren't added in. Its absence is not a
    # sign of a broken response -- a call that used none legitimately
    # reports none -- so it defaults to 0 rather than raising.
    # total_token_count is left untouched: the SDK already includes
    # thinking tokens in that figure.
    candidates_tokens = usage.candidates_token_count + (usage.thoughts_token_count or 0)
    model_usage = ModelUsage(
        prompt_tokens=usage.prompt_token_count,
        candidates_tokens=candidates_tokens,
        total_tokens=usage.total_token_count,
    )

    calls = response.function_calls or []
    tool_calls = tuple(
        ToolCall(
            id=_synthesize_tool_call_id(call, index),
            name=call.name or "",
            args=call.args or {},
        )
        for index, call in enumerate(calls)
    )
    # response.function_calls already guarantees candidates[0].content is
    # not None whenever it returns a non-empty list (see its own
    # implementation) -- so provider_state is only ever None here for a
    # text-only final reply, never for a turn carrying tool_calls; the
    # fallback-reconstruction branch in _model_turn_to_gemini_content
    # is for history turns, not a live gap this could hit.
    model_content = response.candidates[0].content if response.candidates else None
    turn = ModelTurn(
        text=response.text or None,
        tool_calls=tool_calls,
        provider_state=model_content,
    )
    return ModelResponse(turn=turn, usage=model_usage)


def _is_retryable(exc: errors.APIError | httpx.HTTPError) -> bool:
    """True for the same transient failures google-genai's own (now
    unused) retry_options would have retried -- see this module's top
    comment for why the classification moved here instead of staying in
    the SDK's config."""
    if isinstance(exc, errors.APIError):
        return exc.code in _RETRY_HTTP_STATUS_CODES
    return isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS)


def _retry_delay_seconds(attempt: int) -> float:
    """The wait before the next attempt, given this 1-indexed attempt
    just failed. Same formula as tenacity.wait_exponential_jitter (what
    the SDK's own now-unused retry layer would have used, verified
    against the installed tenacity==9.1.4's source): min(initial *
    exp_base ** (attempt - 1) + uniform(0, jitter), max_delay). Pure
    function of the module's own pinned constants -- no I/O, no clock
    read beyond random.uniform -- so it's cheap to unit-test directly."""
    exponential = _RETRY_INITIAL_DELAY_SECONDS * (_RETRY_EXP_BASE ** (attempt - 1))
    jitter = _jitter_random.uniform(0, _RETRY_JITTER)
    return min(exponential + jitter, _RETRY_MAX_DELAY_SECONDS)


def _log_retry_attempt(
    *, attempt: int, exc: errors.APIError | httpx.HTTPError, elapsed_ms: int
) -> None:
    """WARNING for one retried attempt. Deliberately only attempt number,
    exception type, and elapsed time -- never str(exc): for
    errors.APIError that includes exc.details, Google's raw response
    body, the exact leak _wrap_as_model_unavailable below also guards
    against. Nothing about the prompt or the model's response is in
    scope here either way -- this function never sees either."""
    logger.warning(
        json.dumps(
            {
                "event": "model_call_retry",
                "attempt": attempt,
                "max_attempts": _RETRY_ATTEMPTS,
                "exception_type": type(exc).__name__,
                "elapsed_ms": elapsed_ms,
            }
        )
    )


def _wrap_as_model_unavailable(
    exc: errors.APIError | httpx.HTTPError,
) -> ModelUnavailableError:
    """Builds the final, retries-exhausted failure. Never interpolates
    str(exc) or exc.details here: this call carries a live API key
    (services/agent/llm/config.py's LLM_API_KEY, sent as the
    x-goog-api-key header), and APIError's own __str__ includes
    exc.details -- Google's full raw response body, verbatim -- which is
    exactly the leak pattern whatsapp_send.py's WhatsAppSendError fix
    closed for the Graph API. .code (the HTTP status) and .status
    (Google's own short status string, e.g. "UNAVAILABLE") are the only
    fields safe to log: neither ever carries request content."""
    if isinstance(exc, errors.APIError):
        message = (
            f"model call failed: {type(exc).__name__} "
            f"(code={exc.code}, status={exc.status})"
        )
    else:
        message = f"model call failed: {type(exc).__name__}"
    return ModelUnavailableError(message)


class GeminiTransport:
    """The real transport, over google-genai's async client.

    The pinned model and API key come from a validated LlmSettings
    (config.py) — never a bare environment lookup here, and never a
    floating model alias (CLAUDE.md §9). Retries for transient failures
    are handled by generate() itself, not the SDK -- see this module's
    top comment for why.
    """

    def __init__(self, settings: LlmSettings) -> None:
        self._model = settings.model
        self._client = genai.Client(
            api_key=settings.api_key,
            http_options=types.HttpOptions(timeout=settings.timeout_ms),
        )
        self._gemini_tools = [
            types.Tool(
                function_declarations=[
                    _tool_declaration_to_gemini(declaration)
                    for declaration in AGENT_TOOLS
                ]
            )
        ]

    async def generate(
        self,
        *,
        turns: list[Turn],
        system_instruction: str,
    ) -> ModelResponse:
        """Calls the model, retrying transient failures up to
        _RETRY_ATTEMPTS times with logged backoff between attempts.
        Automatic function calling is disabled: this module is always
        the one that decides whether and how a tool call is executed
        (CLAUDE.md rule 1) — the SDK must never run one on its own.

        Raises:
            ModelUnavailableError: every attempt failed on a transient
                error, or a single attempt hit a permanent one (CLAUDE.md
                §8: every external call has a timeout and explicit
                failure handling).
            UsageUnavailableError: see
                _gemini_response_to_model_response -- raised only after
                a successful attempt, never counted as a retryable
                failure.
        """
        contents = _turns_to_gemini_contents(turns)
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=list(self._gemini_tools),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

        last_exc: errors.APIError | httpx.HTTPError
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model, contents=contents, config=config
                )
                return _gemini_response_to_model_response(response)
            except (errors.APIError, httpx.HTTPError) as exc:
                last_exc = exc
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if attempt == _RETRY_ATTEMPTS or not _is_retryable(last_exc):
                raise _wrap_as_model_unavailable(last_exc) from last_exc
            _log_retry_attempt(attempt=attempt, exc=last_exc, elapsed_ms=elapsed_ms)
            await asyncio.sleep(_retry_delay_seconds(attempt))
        raise AssertionError("unreachable: the loop above always returns or raises")
