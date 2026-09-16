"""The one place google-genai is imported for actual model calls —
CLAUDE.md §9: "The model is accessed through one interface module... No
direct SDK calls scattered across the code."

ModelTransport is a Protocol so conversation.py's tool-calling loop can be
tested against a fake transport, with no network access and no API key,
per this PR's plan (no live Gemini calls in CI).
"""

from __future__ import annotations

from typing import Protocol

import httpx
from google import genai
from google.genai import errors, types

from services.agent.llm.config import LlmSettings
from services.agent.llm.errors import ModelUnavailableError
from services.agent.llm.tools import AGENT_TOOLS

# google-genai already wraps every HTTP request in tenacity
# (google.genai._api_client.retry_args) -- but only when HttpOptions.
# retry_options is set. Left unset (as this module did before), it
# resolves to stop_after_attempt(1) with reraise=True: exactly one try,
# no retry at all, which is what let three consecutive 503/504s reach a
# real customer as silent, unretried failures. The fix is configuring
# the SDK's existing retry layer, not stacking a second one on top of
# it -- see this module's own investigation notes in the PR for why.
#
# The numbers here are deliberately tighter than the SDK's own defaults
# (5 attempts, up to 60s between them): those are tuned for one
# isolated call, but this transport sits inside conversation.py's
# tool-calling loop, which can make up to MAX_TOOL_ITERATIONS (4) of
# these in a single turn. This is a WhatsApp conversation, not a batch
# job -- a customer waiting silently past ~30 seconds assumes the bot
# is broken, and escalating to a human at that point (webhook.py's
# _escalate_and_notify) is a better outcome than a longer wait that may
# still fail anyway. 3 attempts (2 retries) at up to 10s each (config.
# py's timeout_ms) plus up to ~5s of backoff between them bounds one
# call at roughly the same ~30-35s ceiling, not the SDK's multi-minute
# worst case.
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


class ModelTransport(Protocol):
    """What conversation.py needs from a model backend — small enough for
    a test fake to implement without touching the real SDK."""

    async def generate(
        self,
        *,
        contents: list[types.Content],
        system_instruction: str,
    ) -> types.GenerateContentResponse: ...


class GeminiTransport:
    """The real transport, over google-genai's async client.

    The pinned model and API key come from a validated LlmSettings
    (config.py) — never a bare environment lookup here, and never a
    floating model alias (CLAUDE.md §9). retry_options below turns on
    the SDK's own tenacity-based retry (off by default — see this
    module's constants above) for transient failures only.
    """

    def __init__(self, settings: LlmSettings) -> None:
        self._model = settings.model
        self._client = genai.Client(
            api_key=settings.api_key,
            http_options=types.HttpOptions(
                timeout=settings.timeout_ms,
                retry_options=types.HttpRetryOptions(
                    attempts=_RETRY_ATTEMPTS,
                    initial_delay=_RETRY_INITIAL_DELAY_SECONDS,
                    max_delay=_RETRY_MAX_DELAY_SECONDS,
                    exp_base=_RETRY_EXP_BASE,
                    jitter=_RETRY_JITTER,
                    http_status_codes=list(_RETRY_HTTP_STATUS_CODES),
                ),
            ),
        )

    async def generate(
        self,
        *,
        contents: list[types.Content],
        system_instruction: str,
    ) -> types.GenerateContentResponse:
        """Calls the model. Automatic function calling is disabled: this
        module is always the one that decides whether and how a tool call
        is executed (CLAUDE.md rule 1) — the SDK must never run one on its
        own.

        Raises:
            ModelUnavailableError: the SDK's own retries (this class's
                retry_options) were exhausted on a transient failure, or
                it hit a permanent one (CLAUDE.md §8: every external
                call has a timeout and explicit failure handling).
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=list(AGENT_TOOLS),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        try:
            return await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except errors.APIError as exc:
            # Never interpolate str(exc) or exc.details here: this call
            # carries a live API key (services/agent/llm/config.py's
            # LLM_API_KEY, sent as the x-goog-api-key header), and
            # APIError's own __str__ includes exc.details -- Google's
            # full raw response body, verbatim -- which is exactly the
            # leak pattern whatsapp_send.py's WhatsAppSendError fix
            # closed for the Graph API. .code (the HTTP status) and
            # .status (Google's own short status string, e.g.
            # "UNAVAILABLE") are the only fields safe to log: neither
            # ever carries request content.
            raise ModelUnavailableError(
                f"model call failed: {type(exc).__name__} "
                f"(code={exc.code}, status={exc.status})"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"model call failed: {type(exc).__name__}"
            ) from exc
