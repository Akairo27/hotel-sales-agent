"""services/worker/runner.py — one pass of the scheduled worker."""

from __future__ import annotations

import contextlib
import json
import logging
import runpy
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from services.worker import runner
from services.worker.config import DATABASE_CONNECT_TIMEOUT_SECONDS
from services.worker.errors import WorkerConfigurationError


def _log_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == runner.logger.name
    ]


def _use_fake_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_connect", lambda: contextlib.nullcontext(object()))


def test_main_logs_released_holds_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use_fake_connection(monkeypatch)
    monkeypatch.setattr(runner, "release_expired_holds", lambda _conn, _now: [3, 5])
    caplog.set_level(logging.INFO, logger=runner.logger.name)

    assert runner.main() == runner.EXIT_OK

    (event,) = _log_events(caplog)
    assert event["event"] == "worker_run_finished"
    assert event["released_hold_ids"] == [3, 5]
    assert event["released_count"] == 2
    assert isinstance(event["duration_ms"], int)


def test_main_passes_a_utc_aware_now_to_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connection(monkeypatch)
    seen: list[datetime] = []

    def fake_release(_conn: object, now: datetime) -> list[int]:
        seen.append(now)
        return []

    monkeypatch.setattr(runner, "release_expired_holds", fake_release)

    runner.main()

    (now,) = seen
    assert now.utcoffset() == UTC.utcoffset(now)


def test_main_reports_a_connection_failure_without_the_driver_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing_connect() -> None:
        raise psycopg.OperationalError("password authentication failed for user leaky")

    monkeypatch.setattr(runner, "_connect", failing_connect)
    caplog.set_level(logging.INFO, logger=runner.logger.name)

    assert runner.main() == runner.EXIT_FAILED

    (event,) = _log_events(caplog)
    assert event == {
        "event": "worker_run_failed",
        "error_type": "OperationalError",
        "sqlstate": None,
    }
    assert "leaky" not in caplog.text


def test_main_reports_a_job_failure_with_its_sqlstate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use_fake_connection(monkeypatch)

    def deadlocking_release(_conn: object, _now: datetime) -> list[int]:
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(runner, "release_expired_holds", deadlocking_release)
    caplog.set_level(logging.INFO, logger=runner.logger.name)

    assert runner.main() == runner.EXIT_FAILED

    (event,) = _log_events(caplog)
    assert event["error_type"] == "DeadlockDetected"
    assert event["sqlstate"] == "40P01"


def test_main_does_not_swallow_an_unrecognised_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connection(monkeypatch)

    def broken_release(_conn: object, _now: datetime) -> list[int]:
        raise RuntimeError("a bug, not an operational failure")

    monkeypatch.setattr(runner, "release_expired_holds", broken_release)

    with pytest.raises(RuntimeError):
        runner.main()


@pytest.mark.parametrize("value", [None, ""])
def test_connect_requires_database_url(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", value)

    with pytest.raises(WorkerConfigurationError):
        runner.main()


def test_connect_sets_an_explicit_timeout_and_autocommit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    connection = object()

    def fake_connect(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return connection

    monkeypatch.setenv("DATABASE_URL", "postgresql://example@localhost/example")
    monkeypatch.setattr(psycopg, "connect", fake_connect)

    assert runner._connect() is connection

    assert calls == [
        (
            ("postgresql://example@localhost/example",),
            {"autocommit": True, "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS},
        )
    ]


def test_module_entry_point_exits_with_main_return_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "main", lambda: 7)

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("services.worker", run_name="__main__")

    assert exit_info.value.code == 7
