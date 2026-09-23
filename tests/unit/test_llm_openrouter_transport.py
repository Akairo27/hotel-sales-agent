"""OpenRouterTransport's own responsibility: translate provider-neutral
turns into OpenRouter's OpenAI-compatible wire format and back, keep the
provider-routing block deny-by-default, retry a transient failure with the
same attempt count and logging as GeminiTransport, and turn a final
failure into ModelUnavailableError without ever leaking a response body or
the API key (CLAUDE.md §8).

No real network access or API key is used: httpx.MockTransport (part of
the already-pinned httpx) stands in for OpenRouter, the same boundary-only
substitution test_llm_client.py makes for the Gemini SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from google.genai import types

from services.agent.llm.client import (
    OPENROUTER_CHAT_COMPLETIONS_URL,
    OpenRouterTransport,
    _OpenRouterAssistantMessage,
)
from services.agent.llm.errors import (
    LlmConfigurationError,
    ModelUnavailableError,
    UsageUnavailableError,
)
from services.agent.llm.model_types import (
    ModelResponse,
    ModelTurn,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    Turn,
    UserTurn,
)
from services.agent.llm.tools import AGENT_TOOLS

_API_KEY = "test-openrouter-key-do-not-leak"
_PROVIDERS = ("provider-a", "provider-b")
_MODEL = "vendor/model-1"
_SECRET_BODY_TEXT = "do-not-leak-this-provider-error-detail"

Handler = Callable[[httpx.Request], httpx.Response]


def _make_transport(handler: Handler) -> OpenRouterTransport:
    return OpenRouterTransport(
        model=_MODEL,
        api_key=_API_KEY,
        providers=_PROVIDERS,
        timeout_ms=10_000,
        http_transport=httpx.MockTransport(handler),
    )


def _generate(
    transport: OpenRouterTransport,
    turns: list[Turn] | None = None,
) -> ModelResponse:
    return asyncio.run(
        transport.generate(
            turns=turns if turns is not None else [UserTurn("hi")],
            system_instruction="be helpful",
        )
    )


def _disable_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("services.agent.llm.client.asyncio.sleep", _no_sleep)


def _usage(prompt: int = 10, completion: int = 5, total: int = 15) -> dict[str, object]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _body(message: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "choices": [{"finish_reason": "stop", "message": message}],
        "usage": _usage(),
        **extra,
    }


def _text_body(text: str = "Hello there") -> dict[str, Any]:
    return _body({"role": "assistant", "content": text})


def _tool_call_message(
    *, call_id: str | None = "call_abc", arguments: object = None
) -> dict[str, Any]:
    call: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "get_quote",
            "arguments": (
                arguments
                if arguments is not None
                else json.dumps({"hotel_id": 1, "check_in": "2026-10-01"})
            ),
        },
    }
    if call_id is not None:
        call["id"] = call_id
    return {"role": "assistant", "content": None, "tool_calls": [call]}


def _sequence(
    outcomes: list[httpx.Response | Exception],
) -> tuple[Handler, list[httpx.Request]]:
    """Returns/raises each outcome in order, one per HTTP call, and the
    list of requests seen so a test can assert exactly how many calls
    happened and what they carried."""
    remaining = list(outcomes)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler, requests


def _ok(body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(200, json=body if body is not None else _text_body())


def _error_response(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": _SECRET_BODY_TEXT}})


def _retry_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]


# --- construction -----------------------------------------------------------


def test_construction_refuses_an_empty_provider_allowlist() -> None:
    with pytest.raises(LlmConfigurationError, match="no OpenRouter provider"):
        OpenRouterTransport(
            model=_MODEL, api_key=_API_KEY, providers=(), timeout_ms=10_000
        )


def test_construction_refuses_an_empty_api_key() -> None:
    with pytest.raises(LlmConfigurationError, match="OPENROUTER_API_KEY"):
        OpenRouterTransport(
            model=_MODEL, api_key="", providers=_PROVIDERS, timeout_ms=10_000
        )


# --- request shape ----------------------------------------------------------


def test_request_targets_the_chat_completions_endpoint_with_a_bearer_key() -> None:
    handler, requests = _sequence([_ok()])

    _generate(_make_transport(handler))

    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == OPENROUTER_CHAT_COMPLETIONS_URL
    assert request.headers["authorization"] == f"Bearer {_API_KEY}"


def test_request_body_carries_the_model_tools_and_deny_by_default_routing() -> None:
    handler, requests = _sequence([_ok()])

    _generate(_make_transport(handler))

    body = json.loads(requests[0].content)
    assert body["model"] == _MODEL
    assert body["tool_choice"] == "auto"
    assert body["provider"] == {
        "order": ["provider-a", "provider-b"],
        "only": ["provider-a", "provider-b"],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
        "require_parameters": True,
    }
    assert "ignore" not in body["provider"]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": declaration.name,
                "description": declaration.description,
                "parameters": declaration.parameters,
            },
        }
        for declaration in AGENT_TOOLS
    ]


def test_request_messages_start_with_the_system_instruction_then_the_turns() -> None:
    handler, requests = _sequence([_ok()])

    _generate(
        _make_transport(handler),
        [UserTurn("first"), ModelTurn(text="answer", tool_calls=()), UserTurn("next")],
    )

    assert json.loads(requests[0].content)["messages"] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next"},
    ]


def test_a_history_model_turn_with_no_tool_calls_has_no_tool_calls_key() -> None:
    handler, requests = _sequence([_ok()])

    _generate(_make_transport(handler), [ModelTurn(text="answer", tool_calls=())])

    assistant = json.loads(requests[0].content)["messages"][1]
    assert "tool_calls" not in assistant


def test_a_model_turn_from_another_transport_is_rebuilt_from_its_tool_calls() -> None:
    """A Gemini turn's provider_state is a google.genai Content, never this
    transport's own type -- it must be ignored, not echoed."""
    handler, requests = _sequence([_ok()])
    foreign_turn = ModelTurn(
        text=None,
        tool_calls=(ToolCall(id="call_0", name="get_quote", args={"rooms": 2}),),
        provider_state=types.Content(role="model", parts=[]),
    )

    _generate(_make_transport(handler), [UserTurn("q"), foreign_turn])

    assistant = json.loads(requests[0].content)["messages"][2]
    assert assistant == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "get_quote", "arguments": '{"rooms": 2}'},
            }
        ],
    }


def test_a_tool_result_turn_becomes_one_tool_message_per_result() -> None:
    handler, requests = _sequence([_ok()])
    results = ToolResultTurn(
        results=(
            ToolResult(call_id="call_0", name="get_quote", result={"priced": True}),
            ToolResult(call_id="call_1", name="check_availability", result={"ok": 1}),
        )
    )

    _generate(_make_transport(handler), [UserTurn("q"), results])

    assert json.loads(requests[0].content)["messages"][2:] == [
        {"role": "tool", "tool_call_id": "call_0", "content": '{"priced": true}'},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": 1}'},
    ]


def test_an_unhandled_turn_type_fails_loudly() -> None:
    handler, _ = _sequence([_ok()])

    with pytest.raises(NotImplementedError, match="unhandled turn type"):
        _generate(_make_transport(handler), [cast(Turn, object())])


# --- response translation ---------------------------------------------------


def test_a_text_reply_is_translated_with_its_usage() -> None:
    handler, _ = _sequence([_ok(_body({"role": "assistant", "content": "Hi!"}))])

    response = _generate(_make_transport(handler))

    assert response.turn.text == "Hi!"
    assert response.turn.tool_calls == ()
    assert (
        response.usage.prompt_tokens,
        response.usage.candidates_tokens,
        response.usage.total_tokens,
    ) == (10, 5, 15)


def test_reasoning_tokens_are_not_added_on_top_of_completion_tokens() -> None:
    """OpenRouter's docs: reasoning tokens are output tokens already inside
    completion_tokens. Adding them again would double-count every cap."""
    body = _text_body()
    body["usage"] = {
        **_usage(prompt=10, completion=20, total=30),
        "completion_tokens_details": {"reasoning_tokens": 7},
    }
    handler, _ = _sequence([_ok(body)])

    response = _generate(_make_transport(handler))

    assert response.usage.candidates_tokens == 20


def test_a_tool_call_reply_is_decoded_with_its_arguments_and_id() -> None:
    handler, _ = _sequence([_ok(_body(_tool_call_message()))])

    turn = _generate(_make_transport(handler)).turn

    assert turn.text is None
    assert turn.tool_calls == (
        ToolCall(
            id="call_abc",
            name="get_quote",
            args={"hotel_id": 1, "check_in": "2026-10-01"},
        ),
    )


def test_a_missing_tool_call_id_is_synthesized_and_echoed_consistently() -> None:
    handler, requests = _sequence([_ok(_body(_tool_call_message(call_id=None))), _ok()])
    transport = _make_transport(handler)

    first = _generate(transport).turn
    _generate(transport, [UserTurn("q"), first])

    assert first.tool_calls[0].id == "call_0"
    echoed = json.loads(requests[1].content)["messages"][2]
    assert echoed["tool_calls"][0]["id"] == "call_0"


def test_arguments_already_decoded_by_the_provider_are_accepted() -> None:
    message = _tool_call_message(arguments={"hotel_id": 3})
    handler, requests = _sequence([_ok(_body(message)), _ok()])
    transport = _make_transport(handler)

    turn = _generate(transport).turn
    _generate(transport, [UserTurn("q"), turn])

    assert turn.tool_calls[0].args == {"hotel_id": 3}
    echoed = json.loads(requests[1].content)["messages"][2]
    assert echoed["tool_calls"][0]["function"]["arguments"] == '{"hotel_id": 3}'


def test_reasoning_fields_are_sent_back_unchanged_on_the_next_request() -> None:
    """The OpenRouter analogue of the Gemini thought-signature test: the
    assistant message that carried a tool call, reasoning and all, must
    come back on the follow-up request exactly as OpenRouter sent it --
    and nothing outside the documented reasoning fields rides along."""
    reasoning_details = [{"type": "reasoning.text", "text": "think", "index": 0}]
    message = {
        **_tool_call_message(),
        "reasoning": "think",
        "reasoning_details": reasoning_details,
        "refusal": None,
        "some_unreviewed_field": "must-not-be-echoed",
    }
    handler, requests = _sequence([_ok(_body(message)), _ok()])
    transport = _make_transport(handler)

    first = _generate(transport).turn
    assert isinstance(first.provider_state, _OpenRouterAssistantMessage)
    result_turn = ToolResultTurn(
        results=(ToolResult(call_id="call_abc", name="get_quote", result={"a": 1}),)
    )
    _generate(transport, [UserTurn("q"), first, result_turn])

    echoed = json.loads(requests[1].content)["messages"][2]
    assert echoed == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "get_quote",
                    "arguments": json.dumps({"hotel_id": 1, "check_in": "2026-10-01"}),
                },
            }
        ],
        "reasoning": "think",
        "reasoning_details": reasoning_details,
    }


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "not-an-object",
        {"prompt_tokens": 1, "completion_tokens": 2},
        {"prompt_tokens": 1, "completion_tokens": "2", "total_tokens": 3},
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
    ],
)
def test_missing_or_incomplete_usage_raises_and_is_not_retried(
    usage: object,
) -> None:
    body = _text_body()
    body["usage"] = usage
    handler, requests = _sequence([_ok(body)])

    with pytest.raises(UsageUnavailableError):
        _generate(_make_transport(handler))

    assert len(requests) == 1


# --- retries ----------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_a_transient_status_is_retried_and_the_retry_is_logged(
    status: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _disable_retry_backoff(monkeypatch)
    caplog.set_level(logging.WARNING, logger="services.agent.llm.client")
    handler, requests = _sequence([_error_response(status), _ok()])

    response = _generate(_make_transport(handler))

    assert response.turn.text == "Hello there"
    assert len(requests) == 2
    (record,) = _retry_records(caplog)
    assert record["event"] == "model_call_retry"
    assert record["attempt"] == 1
    assert record["max_attempts"] == 3
    assert record["exception_type"] == "OpenRouterCallError"
    assert record["status_code"] == status
    assert isinstance(record["elapsed_ms"], int)
    assert _SECRET_BODY_TEXT not in json.dumps(record)


def test_exhausted_retries_raise_model_unavailable_without_leaking_anything(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _disable_retry_backoff(monkeypatch)
    caplog.set_level(logging.WARNING, logger="services.agent.llm.client")
    handler, requests = _sequence([_error_response(503) for _ in range(3)])

    with pytest.raises(ModelUnavailableError) as excinfo:
        _generate(_make_transport(handler))

    assert len(requests) == 3
    assert str(excinfo.value) == "model call failed: OpenRouterCallError (status=503)"
    assert len(_retry_records(caplog)) == 2
    everything = str(excinfo.value) + json.dumps(_retry_records(caplog))
    assert _SECRET_BODY_TEXT not in everything
    assert _API_KEY not in everything


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 412, 413, 422])
def test_a_permanent_status_fails_immediately_without_a_retry(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="services.agent.llm.client")
    handler, requests = _sequence([_error_response(status)])

    with pytest.raises(ModelUnavailableError, match=f"status={status}"):
        _generate(_make_transport(handler))

    assert len(requests) == 1
    assert _retry_records(caplog) == []


@pytest.mark.parametrize(
    "transport_error",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")],
)
def test_a_timeout_or_connect_error_is_retried_and_logs_no_status(
    transport_error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _disable_retry_backoff(monkeypatch)
    caplog.set_level(logging.WARNING, logger="services.agent.llm.client")
    handler, requests = _sequence([transport_error, _ok()])

    _generate(_make_transport(handler))

    assert len(requests) == 2
    (record,) = _retry_records(caplog)
    assert record["exception_type"] == type(transport_error).__name__
    assert "status_code" not in record


def test_a_timeout_that_never_recovers_raises_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_retry_backoff(monkeypatch)
    handler, requests = _sequence([httpx.ReadTimeout("slow") for _ in range(3)])

    with pytest.raises(ModelUnavailableError) as excinfo:
        _generate(_make_transport(handler))

    assert len(requests) == 3
    assert str(excinfo.value) == "model call failed: ReadTimeout"


def test_a_non_transient_transport_error_is_not_retried() -> None:
    handler, requests = _sequence([httpx.RemoteProtocolError("bad framing")])

    with pytest.raises(ModelUnavailableError, match="RemoteProtocolError"):
        _generate(_make_transport(handler))

    assert len(requests) == 1


def test_a_provider_error_inside_a_200_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter reports a failure after the response started as HTTP 200
    with finish_reason "error" -- it must never be returned as a reply."""
    _disable_retry_backoff(monkeypatch)
    failing = {
        "choices": [
            {
                "finish_reason": "error",
                "message": {"role": "assistant", "content": "partial output"},
                "error": {"code": 502, "message": _SECRET_BODY_TEXT},
            }
        ],
        "usage": _usage(),
    }
    handler, requests = _sequence([_ok(failing) for _ in range(3)])

    with pytest.raises(ModelUnavailableError) as excinfo:
        _generate(_make_transport(handler))

    assert len(requests) == 3
    assert str(excinfo.value) == "model call failed: OpenRouterCallError (status=502)"


def test_a_200_with_a_non_json_body_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_retry_backoff(monkeypatch)
    handler, requests = _sequence(
        [httpx.Response(200, content=b"<html>gateway</html>"), _ok()]
    )

    response = _generate(_make_transport(handler))

    assert response.turn.text == "Hello there"
    assert len(requests) == 2


_MALFORMED_BODIES: list[object] = [
    ["not", "an", "object"],
    {"usage": _usage()},
    {"choices": [], "usage": _usage()},
    {"choices": "nope", "usage": _usage()},
    {"choices": ["nope"], "usage": _usage()},
    {"choices": [{"finish_reason": "stop"}], "usage": _usage()},
    _body({"role": "assistant", "content": ["not", "text"]}),
    _body({"role": "assistant", "content": None, "tool_calls": "nope"}),
    _body({"role": "assistant", "content": None, "tool_calls": ["nope"]}),
    _body({"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]}),
    _body(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "x", "function": {"arguments": "{}"}}],
        }
    ),
    _body(_tool_call_message(arguments="{not json")),
    _body(_tool_call_message(arguments="[1, 2]")),
    _body(_tool_call_message(arguments=42)),
]


@pytest.mark.parametrize("payload", _MALFORMED_BODIES)
def test_a_malformed_200_body_is_retried_then_reported_as_unavailable(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_retry_backoff(monkeypatch)
    handler, requests = _sequence([_ok(cast(Any, payload)) for _ in range(3)])

    with pytest.raises(ModelUnavailableError, match="OpenRouterCallError"):
        _generate(_make_transport(handler))

    assert len(requests) == 3
