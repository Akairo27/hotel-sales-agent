from __future__ import annotations

from decimal import Decimal

import pytest

from services.agent.llm.config import LlmSettings, load_llm_settings
from services.agent.llm.errors import LlmConfigurationError

_VALID_ENV = {
    "LLM_MODEL": "test-model-v1",
    "LLM_API_KEY": "test-key",
    "MAX_CONVERSATION_TURNS": "20",
    "LLM_MAX_TOKENS_PER_CONVERSATION": "50000",
    "LLM_MAX_SPEND_PER_DAY_USD": "5.00",
    "MAX_MESSAGES_PER_NUMBER_PER_DAY": "50",
}


def _env_with_allowed_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(
        "services.agent.llm.config.ALLOWED_MODELS", frozenset({"test-model-v1"})
    )
    return dict(_VALID_ENV)


def test_load_llm_settings_with_a_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env_with_allowed_model(monkeypatch)
    settings = load_llm_settings(env)
    assert settings == LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=settings.timeout_ms,
        max_conversation_turns=20,
        max_tokens_per_conversation=50_000,
        max_spend_per_day_usd=Decimal("5.00"),
        max_messages_per_number_per_day=50,
    )


def test_load_llm_settings_rejects_a_model_not_on_the_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.agent.llm.config.ALLOWED_MODELS", frozenset())
    with pytest.raises(LlmConfigurationError, match="not in the reviewed allow-list"):
        load_llm_settings(dict(_VALID_ENV))


def test_load_llm_settings_requires_llm_model(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env_with_allowed_model(monkeypatch)
    del env["LLM_MODEL"]
    with pytest.raises(LlmConfigurationError, match="LLM_MODEL"):
        load_llm_settings(env)


def test_load_llm_settings_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env_with_allowed_model(monkeypatch)
    del env["LLM_API_KEY"]
    with pytest.raises(LlmConfigurationError, match="LLM_API_KEY"):
        load_llm_settings(env)


@pytest.mark.parametrize(
    "raw_value",
    ["", "not-a-number", "0", "-5"],
    ids=["empty", "nan", "zero", "negative"],
)
def test_load_llm_settings_rejects_bad_max_conversation_turns(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    env = _env_with_allowed_model(monkeypatch)
    env["MAX_CONVERSATION_TURNS"] = raw_value
    with pytest.raises(LlmConfigurationError, match="MAX_CONVERSATION_TURNS"):
        load_llm_settings(env)


def test_load_llm_settings_rejects_bad_max_tokens_per_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env_with_allowed_model(monkeypatch)
    env["LLM_MAX_TOKENS_PER_CONVERSATION"] = "-1"
    with pytest.raises(LlmConfigurationError, match="LLM_MAX_TOKENS_PER_CONVERSATION"):
        load_llm_settings(env)


@pytest.mark.parametrize(
    "raw_value",
    ["", "not-a-decimal", "0", "-5.00", "Infinity", "-Infinity", "NaN"],
    ids=[
        "empty",
        "unparseable",
        "zero",
        "negative",
        "infinity",
        "negative-infinity",
        "nan",
    ],
)
def test_load_llm_settings_rejects_bad_max_spend_per_day_usd(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    env = _env_with_allowed_model(monkeypatch)
    env["LLM_MAX_SPEND_PER_DAY_USD"] = raw_value
    with pytest.raises(LlmConfigurationError, match="LLM_MAX_SPEND_PER_DAY_USD"):
        load_llm_settings(env)


@pytest.mark.parametrize(
    "raw_value",
    ["", "not-a-number", "0", "-5"],
    ids=["empty", "nan", "zero", "negative"],
)
def test_load_llm_settings_rejects_bad_max_messages_per_number_per_day(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    env = _env_with_allowed_model(monkeypatch)
    env["MAX_MESSAGES_PER_NUMBER_PER_DAY"] = raw_value
    with pytest.raises(LlmConfigurationError, match="MAX_MESSAGES_PER_NUMBER_PER_DAY"):
        load_llm_settings(env)
