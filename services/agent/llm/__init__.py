"""services/agent/llm — the single interface to the model, CLAUDE.md §9.

Nothing outside this package should import google.genai directly, and
nothing outside this package should build a tool declaration, dispatch a
tool call, or construct the system prompt. Everything a caller needs is
re-exported here.
"""

from __future__ import annotations

from services.agent.llm.client import GeminiTransport, ModelTransport
from services.agent.llm.config import LlmSettings, load_llm_settings
from services.agent.llm.context import ConversationState
from services.agent.llm.conversation import AgentReply, ToolCallRecord, generate_reply
from services.agent.llm.errors import (
    ConversationNotFoundError,
    InvalidToolArgumentsError,
    LlmConfigurationError,
    LlmError,
    ModelUnavailableError,
    ToolLoopLimitError,
    TurnCapExceededError,
    UnknownToolError,
)

__all__ = [
    "AgentReply",
    "ConversationNotFoundError",
    "ConversationState",
    "GeminiTransport",
    "InvalidToolArgumentsError",
    "LlmConfigurationError",
    "LlmError",
    "LlmSettings",
    "ModelTransport",
    "ModelUnavailableError",
    "ToolCallRecord",
    "ToolLoopLimitError",
    "TurnCapExceededError",
    "UnknownToolError",
    "generate_reply",
    "load_llm_settings",
]
