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
    floating model alias (CLAUDE.md §9).
    """

    def __init__(self, settings: LlmSettings) -> None:
        self._model = settings.model
        self._client = genai.Client(
            api_key=settings.api_key,
            http_options=types.HttpOptions(timeout=settings.timeout_ms),
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
            ModelUnavailableError: the SDK reported an API error, or the
                request timed out or failed at the transport level
                (CLAUDE.md §8: every external call has a timeout and
                explicit failure handling).
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
        except (errors.APIError, httpx.HTTPError) as exc:
            raise ModelUnavailableError(f"model call failed: {exc}") from exc
