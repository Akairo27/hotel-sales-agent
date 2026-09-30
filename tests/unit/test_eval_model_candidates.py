"""The safety guards, retry counting, transport assembly and command line
of tests/eval_model_candidates.py -- everything that runs before or around
a paid model call, none of which needs a database or a network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

import pytest

from services.agent.llm.client import GeminiTransport, OpenRouterTransport
from services.agent.llm.config import MODEL_ATTEMPT_TIMEOUT_MS
from services.agent.llm.errors import LlmConfigurationError
from services.agent.llm.model_types import ModelResponse, ModelTurn, ModelUsage, Turn
from tests import eval_model_candidates as eval_module
from tests.eval_model_candidates import (
    EVAL_TIMEOUT_MS,
    EvalConfigurationError,
    EvalTarget,
    _cli,
    _CountingTransport,
    _parse_args,
    _reasoning_effort,
    _RetryCounter,
    build_transports,
    main,
    require_scratch_database,
)
from tests.eval_scenarios import SCENARIOS, Scenario


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

    assert [target.model for target in transports] == [
        "vendor/model-1",
        "vendor/model-2",
    ]
    assert all(target.setting == "default" for target in transports)
    assert all(
        isinstance(target.transport, OpenRouterTransport) for target in transports
    )


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

    (target,) = build_transports([], [], include_gemini_baseline=True)

    assert target.model == "gemini-3.7-flash"
    assert target.setting == "default"
    assert isinstance(target.transport, GeminiTransport)


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


def test_each_reasoning_setting_gets_its_own_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "default" sends no reasoning field (production today); any other
    setting is sent as OpenRouter's reasoning.effort."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    targets = build_transports(
        ["vendor/model-1"],
        ["provider-a"],
        include_gemini_baseline=False,
        reasoning_efforts=["default", "low", "none"],
    )

    assert [target.setting for target in targets] == ["default", "low", "none"]
    efforts = [
        cast(OpenRouterTransport, target.transport)._reasoning_effort
        for target in targets
    ]
    assert efforts == [None, "low", "none"]


def test_an_unknown_reasoning_setting_is_refused() -> None:
    with pytest.raises(EvalConfigurationError, match="unknown reasoning effort"):
        _reasoning_effort("extreme")


def test_the_command_line_defaults_to_one_run_of_the_default_setting() -> None:
    args = _parse_args(["--model", "vendor/model-1"])
    assert args.reasoning_effort == ["default"]
    assert args.repeat == 1
    assert args.output is None


def test_the_command_line_takes_several_settings_and_a_repeat_count() -> None:
    args = _parse_args(
        [
            "--reasoning-effort",
            "default",
            "--reasoning-effort",
            "low",
            "--reasoning-effort",
            "none",
            "--repeat",
            "3",
            "--output",
            "results.md",
        ]
    )
    assert args.reasoning_effort == ["default", "low", "none"]
    assert args.repeat == 3
    assert args.output == "results.md"


@pytest.mark.parametrize("argv", [["--repeat", "0"], ["--reasoning-effort", "huge"]])
def test_the_command_line_refuses_a_bad_repeat_or_setting(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parse_args(argv)


def test_the_harness_uses_productions_per_attempt_timeout() -> None:
    """A shorter ceiling would cut a slow reasoning setting short."""
    assert EVAL_TIMEOUT_MS == MODEL_ATTEMPT_TIMEOUT_MS


@dataclass
class _ScriptedUsageTransport:
    usages: list[ModelUsage]
    served: int = 0

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del turns, system_instruction, deadline
        usage = self.usages[self.served]
        self.served += 1
        return ModelResponse(turn=ModelTurn(text="ok", tool_calls=()), usage=usage)


def _call(transport: _CountingTransport) -> None:
    asyncio.run(transport.generate(turns=[], system_instruction="", deadline=0.0))


def test_the_counting_transport_adds_up_input_output_and_reasoning() -> None:
    counting = _CountingTransport(
        _ScriptedUsageTransport(
            [
                ModelUsage(10, 40, 50, reasoning_tokens=30),
                ModelUsage(20, 5, 25, reasoning_tokens=None),
                ModelUsage(30, 60, 90, reasoning_tokens=45),
            ]
        )
    )

    for _ in range(3):
        _call(counting)

    assert counting.calls == 3
    assert (counting.input_tokens, counting.output_tokens) == (60, 105)
    assert counting.reasoning_tokens == 75


def test_reasoning_stays_unknown_when_no_call_reports_it() -> None:
    counting = _CountingTransport(
        _ScriptedUsageTransport([ModelUsage(10, 5, 15), ModelUsage(10, 5, 15)])
    )

    _call(counting)
    _call(counting)

    assert counting.reasoning_tokens is None


def test_every_setting_runs_every_scenario_each_repeat_interleaved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[tuple[str, str]] = []

    async def _fake_run_scenario(
        _conn: Any,
        *,
        transport: Any,
        model: str,
        scenario: Scenario,
        setting: str,
    ) -> Any:
        del transport, model
        order.append((setting, scenario.key))
        return None

    monkeypatch.setattr(eval_module, "run_scenario", _fake_run_scenario)
    targets = [
        EvalTarget("vendor/model-1", "default", cast(Any, None)),
        EvalTarget("vendor/model-1", "low", cast(Any, None)),
    ]

    asyncio.run(eval_module._run_all(cast(Any, None), targets, repeat=2))

    keys = [scenario.key for scenario in SCENARIOS]
    one_repeat = [("default", key) for key in keys] + [("low", key) for key in keys]
    assert order == one_repeat * 2
