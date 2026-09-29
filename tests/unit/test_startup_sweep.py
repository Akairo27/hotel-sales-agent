"""Unit tests for services/agent/startup_sweep.py's settings loader — no
database. The sweep itself is tested against real Postgres in
tests/integration/test_startup_sweep.py."""

from __future__ import annotations

from datetime import timedelta

import pytest

from services.agent.startup_sweep import (
    DEFAULT_LOOKBACK_HOURS,
    MAX_LOOKBACK_HOURS,
    STARTUP_SWEEP_LOOKBACK_HOURS_ENV,
    StartupSweepConfigurationError,
    load_startup_sweep_settings,
)


@pytest.mark.parametrize("env", [{}, {STARTUP_SWEEP_LOOKBACK_HOURS_ENV: ""}])
def test_lookback_defaults_to_twenty_hours_when_unset(env: dict[str, str]) -> None:
    assert DEFAULT_LOOKBACK_HOURS == 20
    assert load_startup_sweep_settings(env).lookback == timedelta(hours=20)


@pytest.mark.parametrize("hours", [1, 12, MAX_LOOKBACK_HOURS])
def test_lookback_accepts_whole_hours_inside_the_customer_service_window(
    hours: int,
) -> None:
    settings = load_startup_sweep_settings(
        {STARTUP_SWEEP_LOOKBACK_HOURS_ENV: str(hours)}
    )
    assert settings.lookback == timedelta(hours=hours)


@pytest.mark.parametrize("raw", ["0", "-3", str(MAX_LOOKBACK_HOURS + 1), "1.5", "x"])
def test_lookback_rejects_anything_else(raw: str) -> None:
    with pytest.raises(
        StartupSweepConfigurationError, match=STARTUP_SWEEP_LOOKBACK_HOURS_ENV
    ):
        load_startup_sweep_settings({STARTUP_SWEEP_LOOKBACK_HOURS_ENV: raw})
