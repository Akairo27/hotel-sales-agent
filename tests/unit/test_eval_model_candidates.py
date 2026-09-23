"""The safety guards, retry counting, transport assembly and command line
of tests/eval_model_candidates.py -- everything that runs before or around
a paid model call, none of which needs a database or a network.
"""

from __future__ import annotations

import json
import logging

import pytest

from services.agent.llm.client import GeminiTransport, OpenRouterTransport
from services.agent.llm.errors import LlmConfigurationError
from tests.eval_model_candidates import (
    EvalConfigurationError,
    _cli,
    _RetryCounter,
    build_transports,
    main,
    require_scratch_database,
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "services.agent.llm.client", 30, "x.py", 1, message, (), None
    )


def _retry_event(**fields: object) -> logging.LogRecord:
    return _record(json.dumps({"event": "model_call_retry", **fields}))


def test_a_missing_scratch_database_url_is_refused() -> None:
    with pytest.raises(EvalConfigurationError, match="EVAL_DATABASE_URL is not set"):
        require_scratch_database(None, production_url=None)
    with pytest.raises(EvalConfigurationError, match="EVAL_DATABASE_URL is not set"):
        require_scratch_database("", production_url="postgresql://prod")


def test_the_production_database_url_is_refused() -> None:
    with pytest.raises(EvalConfigurationError, match="production database"):
        require_scratch_database(
            "postgresql://same", production_url="postgresql://same"
        )


def test_a_different_database_url_is_accepted() -> None:
    assert (
        require_scratch_database(
            "postgresql://scratch", production_url="postgresql://prod"
        )
        == "postgresql://scratch"
    )
    assert (
        require_scratch_database("postgresql://scratch", production_url=None)
        == "postgresql://scratch"
    )


def test_main_refuses_to_run_without_the_scratch_confirmation_flag() -> None:
    with pytest.raises(EvalConfigurationError, match="--confirm-scratch-db"):
        main(["--model", "vendor/model-1"])


def test_the_command_line_reports_a_configuration_error_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["eval_model_candidates"])

    assert _cli() == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--confirm-scratch-db" in captured.err


def test_the_command_line_also_reports_a_missing_provider_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["eval_model_candidates", "--confirm-scratch-db", "--model", "vendor/model-1"],
    )
    monkeypatch.setenv("EVAL_DATABASE_URL", "postgresql://scratch")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    assert _cli() == 2

    assert "no OpenRouter provider" in capsys.readouterr().err


def test_there_must_be_something_to_evaluate() -> None:
    with pytest.raises(EvalConfigurationError, match="nothing to evaluate"):
        build_transports([], [], include_gemini_baseline=False)


def test_openrouter_candidates_need_the_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(EvalConfigurationError, match="OPENROUTER_API_KEY"):
        build_transports(
            ["vendor/model-1"], ["provider-a"], include_gemini_baseline=False
        )


def test_openrouter_candidates_fail_closed_without_a_provider_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    with pytest.raises(LlmConfigurationError, match="no OpenRouter provider"):
        build_transports(["vendor/model-1"], [], include_gemini_baseline=False)


def test_openrouter_candidates_get_the_providers_passed_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    transports = build_transports(
        ["vendor/model-1", "vendor/model-2"],
        ["provider-a"],
        include_gemini_baseline=False,
    )

    assert [label for label, _ in transports] == ["vendor/model-1", "vendor/model-2"]
    assert all(isinstance(t, OpenRouterTransport) for _, t in transports)


def test_the_gemini_baseline_needs_a_gemini_model_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "vendor/not-gemini")
    monkeypatch.setenv("LLM_API_KEY", "test-gemini-key")
    with pytest.raises(EvalConfigurationError, match="not a Gemini model"):
        build_transports([], [], include_gemini_baseline=True)

    monkeypatch.setenv("LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(EvalConfigurationError, match="LLM_API_KEY"):
        build_transports([], [], include_gemini_baseline=True)


def test_the_gemini_baseline_is_a_real_gemini_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.setenv("LLM_API_KEY", "test-gemini-key")

    ((label, transport),) = build_transports([], [], include_gemini_baseline=True)

    assert label == "gemini-3.7-flash"
    assert isinstance(transport, GeminiTransport)


def test_retry_counter_counts_retries_and_separates_malformed_replies() -> None:
    counter = _RetryCounter()

    counter.emit(_retry_event(exception_type="ServerError", status_code=503))
    counter.emit(_retry_event(exception_type="ReadTimeout"))
    counter.emit(_retry_event(exception_type="OpenRouterCallError"))
    counter.emit(_retry_event(exception_type="OpenRouterCallError", status_code=502))

    assert counter.retries == 4
    assert counter.malformed_retries == 1


def test_retry_counter_ignores_anything_that_is_not_a_retry_event() -> None:
    counter = _RetryCounter()

    counter.emit(_record("not json at all"))
    counter.emit(_record(json.dumps(["a", "list"])))
    counter.emit(_record(json.dumps({"event": "something_else"})))

    assert counter.retries == 0
    assert counter.malformed_retries == 0
