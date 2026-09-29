"""services/agent/main.py — the health-check endpoint and startup logging
configuration.

health() is called directly rather than through TestClient: it's a
one-line handler, and services/agent/main.py's own wiring is a single
`@app.get` line, not worth a heavier test for.

configure_logging is tested directly for the same reason: it mutates
process-wide logging state (root logger level, an added handler,
third-party logger levels), so every test below restores that state
exactly via _clean_root_logger, rather than letting it leak into whatever
the rest of this suite (or pytest's own caplog) expects the root logger to
look like.

test_lifespan_configures_logging_only_when_the_asgi_server_actually_starts
is the one test that does use TestClient (already a real dependency
throughout tests/integration/) -- it proves the wiring itself, not just
the bare function: that a plain `TestClient(app)` (no `with`) never
triggers configure_logging as a side effect of every integration test's
`from services.agent.main import app`, and that `with TestClient(app):`
does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.agent import main as main_module
from services.agent.llm.errors import LlmConfigurationError
from services.agent.main import (
    _THIRD_PARTY_LOGGERS_AT_WARNING,
    LoggingConfigurationError,
    StartupConfigurationError,
    app,
    configure_logging,
    health,
    validate_startup_configuration,
)
from services.agent.webhook import WebhookConfigurationError
from services.agent.whatsapp_send import WhatsAppSendConfigurationError


def test_health_returns_ok() -> None:
    assert asyncio.run(health()) == {"status": "ok"}


@pytest.fixture
def _clean_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    original_third_party_levels = {
        name: logging.getLogger(name).level for name in _THIRD_PARTY_LOGGERS_AT_WARNING
    }
    yield
    root.setLevel(original_level)
    root.handlers[:] = original_handlers
    for name, level in original_third_party_levels.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_sets_root_level_from_env(
    monkeypatch: pytest.MonkeyPatch, _clean_root_logger: None
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_defaults_to_info_when_unset(
    monkeypatch: pytest.MonkeyPatch, _clean_root_logger: None
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, _clean_root_logger: None
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_rejects_an_unrecognized_level(
    monkeypatch: pytest.MonkeyPatch, _clean_root_logger: None
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "not-a-level")
    with pytest.raises(LoggingConfigurationError, match="NOT-A-LEVEL"):
        configure_logging()


def test_configure_logging_pins_third_party_loggers_to_warning_even_at_debug(
    monkeypatch: pytest.MonkeyPatch, _clean_root_logger: None
) -> None:
    """The whole point of the pin: raising this app's own level to DEBUG
    to chase a real bug must never also turn on httpx's per-request URL
    logging or google_genai's SDK-internal logging (see client.py's own
    retry-loop comment for the response-body-leak shape that would take,
    were the SDK's own retry logging ever enabled)."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()
    for name in _THIRD_PARTY_LOGGERS_AT_WARNING:
        assert logging.getLogger(name).level == logging.WARNING


def test_configure_logging_writes_to_stdout_not_stderr(
    monkeypatch: pytest.MonkeyPatch,
    _clean_root_logger: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()

    logging.getLogger("services.agent.webhook").info("probe-message")

    captured = capsys.readouterr()
    assert "probe-message" in captured.out
    assert "probe-message" not in captured.err


def test_lifespan_configures_logging_only_when_the_asgi_server_actually_starts(
    monkeypatch: pytest.MonkeyPatch, _clean_root_logger: None
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    # Startup validation has its own tests below; this one is about logging.
    monkeypatch.setattr(main_module, "validate_startup_configuration", lambda: None)
    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    # No `with`: every integration test's `from services.agent.main
    # import app` does exactly this when building its own TestClient
    # (tests/integration/test_webhook.py's webhook_client fixture) --
    # confirming it stays a no-op is what keeps this test suite's
    # process-wide logging state untouched by importing that module.
    TestClient(app).get("/health")
    assert root.level == logging.WARNING

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert root.level == logging.DEBUG


# Placeholder values only -- never real credentials.
_VALID_ENV = {
    "WHATSAPP_VERIFY_TOKEN": "test-verify-token",
    "WHATSAPP_APP_SECRET": "test-app-secret",
    "WHATSAPP_PHONE_NUMBER_ID": "test-phone-number-id",
    "WHATSAPP_ACCESS_TOKEN": "test-access-token",
    "LLM_MODEL": "gemini-3.7-flash",
    "LLM_API_KEY": "test-llm-key",
    "MAX_CONVERSATION_TURNS": "20",
    "LLM_MAX_TOKENS_PER_CONVERSATION": "50000",
    "LLM_MAX_SPEND_PER_DAY_USD": "5.00",
    "MAX_MESSAGES_PER_NUMBER_PER_DAY": "50",
    "DATABASE_URL": "postgresql://placeholder.invalid/db",
}


@pytest.fixture
def _valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _VALID_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.usefixtures("_valid_env")
def test_validate_startup_configuration_accepts_a_complete_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And never opens a database connection: DATABASE_URL points nowhere
    real, and psycopg.connect is made to fail loudly if called."""

    def _no_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("startup validation must not connect to the database")

    monkeypatch.setattr("psycopg.connect", _no_connect)

    validate_startup_configuration()


@pytest.mark.usefixtures("_valid_env")
@pytest.mark.parametrize(
    ("missing", "error"),
    [
        ("WHATSAPP_VERIFY_TOKEN", WebhookConfigurationError),
        ("WHATSAPP_APP_SECRET", WebhookConfigurationError),
        ("WHATSAPP_PHONE_NUMBER_ID", WhatsAppSendConfigurationError),
        ("WHATSAPP_ACCESS_TOKEN", WhatsAppSendConfigurationError),
        ("LLM_MODEL", LlmConfigurationError),
        ("LLM_API_KEY", LlmConfigurationError),
        ("MAX_CONVERSATION_TURNS", LlmConfigurationError),
        ("LLM_MAX_TOKENS_PER_CONVERSATION", LlmConfigurationError),
        ("LLM_MAX_SPEND_PER_DAY_USD", LlmConfigurationError),
        ("MAX_MESSAGES_PER_NUMBER_PER_DAY", LlmConfigurationError),
        ("DATABASE_URL", StartupConfigurationError),
    ],
)
def test_validate_startup_configuration_refuses_each_missing_setting(
    monkeypatch: pytest.MonkeyPatch, missing: str, error: type[Exception]
) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(error, match=missing) as exc_info:
        validate_startup_configuration()

    for value in _VALID_ENV.values():
        assert value not in str(exc_info.value)


@pytest.mark.usefixtures("_valid_env", "_clean_root_logger")
def test_the_app_refuses_to_start_with_a_broken_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, not just the function: the ASGI lifespan runs the
    validation, so the server fails at boot rather than on the first
    customer message."""
    monkeypatch.delenv("WHATSAPP_APP_SECRET")

    with pytest.raises(WebhookConfigurationError), TestClient(app):
        pass
