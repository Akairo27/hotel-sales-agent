"""services/agent/fixed_texts.py's customer_language against a real
Postgres instance: which written message decides the language of a fixed
text. The detection rules themselves are unit-tested in
tests/unit/test_fixed_texts.py; the webhook using the result is tested in
tests/integration/test_webhook.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.agent.fixed_texts import customer_language, media_placeholder
from tests.integration._seed import seed_conversation, seed_message

pytestmark = pytest.mark.usefixtures("db_conn")

_NOW = datetime.now(UTC)


def _inbound(
    conn: psycopg.Connection[Any], conversation_id: int, body: str, *, ago: timedelta
) -> None:
    seed_message(
        conn,
        conversation_id,
        direction="inbound",
        body=body,
        created_at=_NOW - ago,
    )


def test_the_latest_written_message_decides(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id = seed_conversation(db_conn)
    _inbound(db_conn, conversation_id, "أبغى غرفة", ago=timedelta(minutes=5))
    _inbound(
        db_conn, conversation_id, "Halo, berapa harga kamar?", ago=timedelta(minutes=1)
    )

    assert customer_language(db_conn, conversation_id) == "id"


def test_media_placeholders_are_skipped_even_with_a_caption(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A voice note or an image is not a written message; a caption does not
    make it one."""
    conversation_id = seed_conversation(db_conn)
    _inbound(db_conn, conversation_id, "أبغى غرفة", ago=timedelta(minutes=5))
    _inbound(
        db_conn, conversation_id, media_placeholder("audio"), ago=timedelta(minutes=3)
    )
    _inbound(
        db_conn,
        conversation_id,
        media_placeholder("image") + " is this room free?",
        ago=timedelta(minutes=1),
    )

    assert customer_language(db_conn, conversation_id) == "ar"


def test_the_agents_own_messages_never_count(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id = seed_conversation(db_conn)
    _inbound(db_conn, conversation_id, "I need a room", ago=timedelta(minutes=5))
    seed_message(
        db_conn,
        conversation_id,
        direction="outbound",
        body="تفضل، وش التواريخ؟",
        created_at=_NOW - timedelta(minutes=1),
    )

    assert customer_language(db_conn, conversation_id) == "en"


def test_a_written_message_from_an_earlier_session_still_counts(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The whole conversation, not only the current session (owner
    decision, 2026-09-30): a returning customer keeps their language."""
    conversation_id = seed_conversation(db_conn)
    _inbound(db_conn, conversation_id, "أبغى غرفة", ago=timedelta(hours=30))
    _inbound(
        db_conn, conversation_id, media_placeholder("audio"), ago=timedelta(minutes=1)
    )

    assert customer_language(db_conn, conversation_id) == "ar"


def test_no_written_message_means_no_language(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id = seed_conversation(db_conn)
    _inbound(
        db_conn, conversation_id, media_placeholder("audio"), ago=timedelta(minutes=1)
    )

    assert customer_language(db_conn, conversation_id) is None
