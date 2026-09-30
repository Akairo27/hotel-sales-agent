"""Integration tests for services/agent/startup_sweep.py against a real
Postgres instance.

Every test runs twice, like tests/integration/test_webhook.py: once on the
test's privileged connection and once as hotel_agent, the role the agent
really connects as (migration 0027) -- which proves the sweep needs nothing
beyond the messages columns that role can read (it has only
escalations.id, which is why the sweep cannot see an existing escalation).
Seeding and assertions stay on the privileged db_conn.

started_at is fixed at noon Asia/Riyadh time so no message seeded a few
minutes earlier can fall on the previous Riyadh day (the rate-cap count
is per day).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from services.agent import webhook as webhook_module
from services.agent.fixed_texts import FALLBACK
from services.agent.llm.config import LlmSettings
from services.agent.startup_sweep import (
    MAX_CONVERSATIONS_PER_SWEEP,
    STARTUP_SWEEP_LOOKBACK_HOURS_ENV,
    run_startup_sweep,
)
from services.agent.whatsapp_send import WhatsAppSendError, WhatsAppSendSettings
from tests.integration._seed import seed_conversation, seed_message

pytestmark = pytest.mark.usefixtures("db_conn")

_STARTED_AT = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)  # 12:00 in Riyadh
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_TEST_WHATSAPP_SETTINGS = WhatsAppSendSettings(
    phone_number_id="test-phone-number-id",
    access_token="test-access-token",
    timeout_ms=10_000,
)


def _llm_settings(*, max_messages_per_number_per_day: int = 1_000) -> LlmSettings:
    return LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=20,
        max_tokens_per_conversation=1_000_000,
        max_spend_per_day_usd=Decimal("1000"),
        max_messages_per_number_per_day=max_messages_per_number_per_day,
        max_tokens_per_number_per_day=10_000_000,
    )


@dataclass
class _RecordingSender:
    """Records every send and hands out a distinct message id per success
    (messages.whatsapp_message_id is unique); fails every send if told to."""

    fail: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, *, to_phone: str, body: str) -> str:
        self.calls.append((to_phone, body))
        if self.fail:
            raise WhatsAppSendError("simulated API error")
        return f"wamid.SWEEP-{len(self.calls)}"


@contextlib.contextmanager
def _nullcontext(conn: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    yield conn


@contextlib.contextmanager
def _agent_connection(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(params=["privileged", "hotel_agent"])
def sender(
    request: pytest.FixtureRequest,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingSender:
    """Wires webhook.py's connection, settings and sender the sweep uses,
    and returns the sender to inspect."""
    if request.param == "hotel_agent":
        agent_url: str = request.getfixturevalue("agent_database_url")
        monkeypatch.setattr(
            webhook_module, "get_db_connection", lambda: _agent_connection(agent_url)
        )
    else:
        monkeypatch.setattr(
            webhook_module, "get_db_connection", lambda: _nullcontext(db_conn)
        )
    monkeypatch.setattr(webhook_module, "get_llm_settings", _llm_settings)
    monkeypatch.setattr(
        webhook_module, "get_whatsapp_send_settings", lambda: _TEST_WHATSAPP_SETTINGS
    )
    recording_sender = _RecordingSender()
    monkeypatch.setattr(
        webhook_module, "get_whatsapp_sender", lambda _settings: recording_sender
    )
    monkeypatch.delenv(STARTUP_SWEEP_LOOKBACK_HOURS_ENV, raising=False)
    return recording_sender


def _sweep() -> None:
    asyncio.run(run_startup_sweep(started_at=_STARTED_AT))


def _seed_inbound(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    ago: timedelta,
    phone: str = _PHONE,
) -> None:
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="a room for tonight?",
        customer_phone=phone,
        created_at=_STARTED_AT - ago,
    )


def _escalations(db_conn: psycopg.Connection[Any]) -> list[tuple[str, dict[str, Any]]]:
    rows = db_conn.execute(
        "SELECT reason, notes FROM escalations ORDER BY id"
    ).fetchall()
    return [(reason, json.loads(notes)) for reason, notes in rows]


def _finished_event(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    events: list[dict[str, Any]] = [
        entry
        for entry in (
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "services.agent.startup_sweep"
        )
        if entry["event"] == "startup_sweep_finished"
    ]
    (event,) = events
    return event


def test_sweep_answers_a_message_lost_before_the_start(
    sender: _RecordingSender,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=3))
    caplog.set_level(logging.INFO, logger="services.agent.startup_sweep")

    _sweep()

    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _escalations(db_conn) == [
        ("unanswered_at_startup", {"source": "startup_sweep"})
    ]
    assert _finished_event(caplog) == {
        "event": "startup_sweep_finished",
        "unanswered": 1,
        "skipped_rate_capped": 0,
        "skipped_over_limit": 0,
        "statuses": {"escalated": 1},
    }


def test_sweep_leaves_an_answered_message_alone(
    sender: _RecordingSender, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=3))
    seed_message(
        db_conn,
        conversation_id,
        direction="outbound",
        body="Sure.",
        customer_phone=_PHONE,
        created_at=_STARTED_AT - timedelta(minutes=2),
    )

    _sweep()

    assert sender.calls == []
    assert _escalations(db_conn) == []


@pytest.mark.parametrize(
    "ago",
    [
        pytest.param(timedelta(seconds=2), id="stored-by-the-new-process"),
        pytest.param(timedelta(hours=-1), id="after-the-start"),
        pytest.param(timedelta(hours=20, seconds=1), id="older-than-the-window"),
    ],
)
def test_sweep_leaves_messages_outside_its_window_alone(
    sender: _RecordingSender, db_conn: psycopg.Connection[Any], ago: timedelta
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=ago)

    _sweep()

    assert sender.calls == []
    assert _escalations(db_conn) == []


def test_sweep_honours_a_configured_lookback(
    sender: _RecordingSender,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STARTUP_SWEEP_LOOKBACK_HOURS_ENV, "2")
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(hours=3))

    _sweep()

    assert sender.calls == []


def test_sweep_skips_a_message_silent_by_the_daily_rate_cap(
    sender: _RecordingSender,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cap 1: the second message that day was the first past the cap (it
    got the fallback, seeded here); the third was silent on purpose."""
    monkeypatch.setattr(
        webhook_module,
        "get_llm_settings",
        lambda: _llm_settings(max_messages_per_number_per_day=1),
    )
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=10))
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=9))
    seed_message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=FALLBACK.english,
        customer_phone=_PHONE,
        created_at=_STARTED_AT - timedelta(minutes=8),
    )
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=7))
    caplog.set_level(logging.INFO, logger="services.agent.startup_sweep")

    _sweep()

    assert sender.calls == []
    assert _finished_event(caplog)["skipped_rate_capped"] == 1


def test_sweep_answers_a_lost_first_message_past_the_rate_cap(
    sender: _RecordingSender,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first message past the cap should have had the fallback; if its
    notice was lost, the sweep sends it."""
    monkeypatch.setattr(
        webhook_module,
        "get_llm_settings",
        lambda: _llm_settings(max_messages_per_number_per_day=1),
    )
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=10))
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=9))

    _sweep()

    assert sender.calls == [(_WA_ID, FALLBACK.english)]


def test_sweep_sends_one_notice_for_several_lost_messages_in_a_conversation(
    sender: _RecordingSender, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=5))
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=4))

    _sweep()

    assert len(sender.calls) == 1
    assert len(_escalations(db_conn)) == 1


def test_sweep_answers_at_most_the_limit_most_recent_first(
    sender: _RecordingSender,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    phones = [f"+9665100000{n:02d}" for n in range(MAX_CONVERSATIONS_PER_SWEEP + 1)]
    for minutes_ago, phone in enumerate(phones, start=1):
        conversation_id = seed_conversation(db_conn, customer_phone=phone)
        _seed_inbound(
            db_conn, conversation_id, ago=timedelta(minutes=minutes_ago), phone=phone
        )
    caplog.set_level(logging.INFO, logger="services.agent.startup_sweep")

    _sweep()

    assert [to for to, _body in sender.calls] == [
        phone.removeprefix("+") for phone in phones[:MAX_CONVERSATIONS_PER_SWEEP]
    ]
    event = _finished_event(caplog)
    assert event["unanswered"] == MAX_CONVERSATIONS_PER_SWEEP + 1
    assert event["skipped_over_limit"] == 1


def test_a_second_start_does_not_answer_the_same_message_again(
    sender: _RecordingSender, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=3))

    _sweep()
    _sweep()

    assert len(sender.calls) == 1
    assert len(_escalations(db_conn)) == 1


def test_sweep_logs_a_failed_send_and_carries_on(
    sender: _RecordingSender,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender.fail = True
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _seed_inbound(db_conn, conversation_id, ago=timedelta(minutes=3))
    caplog.set_level(logging.INFO, logger="services.agent.startup_sweep")

    _sweep()

    assert len(sender.calls) == 1
    assert _escalations(db_conn) == [
        ("unanswered_at_startup", {"source": "startup_sweep"})
    ]
    assert _finished_event(caplog)["statuses"] == {"escalated_undelivered": 1}


def test_sweep_logs_and_returns_when_the_database_is_unreachable(
    sender: _RecordingSender,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _unreachable() -> Any:
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(webhook_module, "get_db_connection", _unreachable)
    caplog.set_level(logging.ERROR, logger="services.agent.startup_sweep")

    _sweep()

    assert sender.calls == []
    (record,) = caplog.records
    assert json.loads(record.getMessage())["event"] == "startup_sweep_failed"
