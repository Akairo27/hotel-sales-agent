from __future__ import annotations

from decimal import Decimal

import pytest

from services.agent.llm.config import (
    ALLOWED_MODELS,
    OPENROUTER_ROUTES,
    LlmSettings,
    OpenRouterRoute,
    load_llm_settings,
)
from services.agent.llm.errors import LlmConfigurationError
from services.agent.llm.pricing import GEMINI_FLASH_RATES, TokenRates

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


_OPENROUTER_MODEL = "vendor/model-1"
_OPENROUTER_ROUTE = OpenRouterRoute(
    providers=("provider-a",),
    token_rates=TokenRates(
        input_usd_per_million_tokens=Decimal("0.50"),
        output_usd_per_million_tokens=Decimal("2.00"),
    ),
)


def _openrouter_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(
        "services.agent.llm.config.OPENROUTER_ROUTES",
        {_OPENROUTER_MODEL: _OPENROUTER_ROUTE},
    )
    monkeypatch.setattr(
        "services.agent.llm.config.ALLOWED_MODELS", frozenset({_OPENROUTER_MODEL})
    )
    env = dict(_VALID_ENV)
    env["LLM_MODEL"] = _OPENROUTER_MODEL
    env["LLM_API_KEY"] = "gemini-key-must-not-be-used"
    env["OPENROUTER_API_KEY"] = "test-openrouter-key"
    return env


def test_a_gemini_model_uses_gemini_rates_and_has_no_openrouter_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_llm_settings(_env_with_allowed_model(monkeypatch))
    assert settings.token_rates == GEMINI_FLASH_RATES
    assert settings.openrouter_route is None


def test_an_openrouter_model_takes_its_key_route_and_rates_from_its_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_llm_settings(_openrouter_env(monkeypatch))
    assert settings.model == _OPENROUTER_MODEL
    assert settings.api_key == "test-openrouter-key"
    assert settings.openrouter_route == _OPENROUTER_ROUTE
    assert settings.token_rates == _OPENROUTER_ROUTE.token_rates


def test_an_openrouter_model_does_not_need_the_gemini_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _openrouter_env(monkeypatch)
    del env["LLM_API_KEY"]
    assert load_llm_settings(env).api_key == "test-openrouter-key"


def test_an_openrouter_model_requires_the_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _openrouter_env(monkeypatch)
    del env["OPENROUTER_API_KEY"]
    with pytest.raises(LlmConfigurationError, match="OPENROUTER_API_KEY"):
        load_llm_settings(env)


_GLM_MODEL = "z-ai/glm-5.3-20260816"


def test_the_shipped_route_is_the_recorded_glm_decision() -> None:
    """Pins ARCHITECTURE.md §10's decision (Crusoe primary, InferenceNet
    secondary, GLM-5.3 only, Crusoe's rates as the higher of the two): any
    change to the reviewed route must show up here as a deliberate diff."""
    assert set(OPENROUTER_ROUTES) == {_GLM_MODEL}
    assert _GLM_MODEL in ALLOWED_MODELS
    route = OPENROUTER_ROUTES[_GLM_MODEL]
    assert route.providers == ("crusoe", "inference-net")
    assert route.token_rates == TokenRates(
        input_usd_per_million_tokens=Decimal("1.40"),
        output_usd_per_million_tokens=Decimal("4.40"),
    )


def test_the_shipped_route_loads_with_the_openrouter_key_only() -> None:
    env = dict(_VALID_ENV)
    env["LLM_MODEL"] = _GLM_MODEL
    env["OPENROUTER_API_KEY"] = "test-openrouter-key"
    del env["LLM_API_KEY"]

    settings = load_llm_settings(env)

    assert settings.model == _GLM_MODEL
    assert settings.api_key == "test-openrouter-key"
    assert settings.openrouter_route == OPENROUTER_ROUTES[_GLM_MODEL]
    assert settings.token_rates == OPENROUTER_ROUTES[_GLM_MODEL].token_rates
