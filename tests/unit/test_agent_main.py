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

from services.agent.main import (
    _THIRD_PARTY_LOGGERS_AT_WARNING,
    LoggingConfigurationError,
    app,
    configure_logging,
    health,
)


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
