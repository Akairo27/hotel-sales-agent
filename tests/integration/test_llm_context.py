"""Integration tests for context.py against a real Postgres instance —
the two closed decisions from ARCHITECTURE.md §7 this module exists to
enforce: the last-10-message window, and that the customer's phone
number never reaches the model's context.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from services.agent.llm.context import (
    build_contents,
    load_conversation_state,
    load_recent_messages,
)
from services.agent.llm.prompt import render_system_instruction
from tests.integration._seed import seed_conversation, seed_message

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"
# Every representation the same phone number could plausibly take —
# structurally impossible for any of these to appear given build_contents
# and render_system_instruction never read customer_phone at all, but the
# test proves that rather than assuming it.
_PHONE_VARIANTS = (_PHONE, _PHONE.lstrip("+"), "0" + _PHONE[4:], _PHONE[4:])


def test_load_recent_messages_returns_only_the_window_oldest_first(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    for i in range(12):
        direction = "inbound" if i % 2 == 0 else "outbound"
        seed_message(db_conn, conversation_id, direction=direction, body=f"message {i}")

    messages = load_recent_messages(db_conn, conversation_id, limit=10)

    assert [m.body for m in messages] == [f"message {i}" for i in range(2, 12)]


def test_build_contents_maps_message_direction_to_model_role(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_message(db_conn, conversation_id, direction="inbound", body="hello")
    seed_message(db_conn, conversation_id, direction="outbound", body="hi there")

    messages = load_recent_messages(db_conn, conversation_id, limit=10)
    contents = build_contents(messages)

    assert [content.role for content in contents] == ["user", "model"]


def test_customer_phone_never_appears_in_any_built_content_or_the_system_instruction(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Exhaustive, not a spot check. Every part of every built Content is
    scanned, plus the rendered system instruction — a leak sitting in the
    one part a sampled assertion happened to skip must still fail this
    test.
    """
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    bodies = [
        "I'd like to check availability for two nights",
        "What is the price for a double room?",
        "Can I get a discount on that?",
        "Thank you, that works for me",
    ]
    for i, body in enumerate(bodies):
        direction = "inbound" if i % 2 == 0 else "outbound"
        seed_message(db_conn, conversation_id, direction=direction, body=body)

    state = load_conversation_state(db_conn, conversation_id)
    assert state.customer_phone == _PHONE

    messages = load_recent_messages(db_conn, conversation_id, limit=10)
    contents = build_contents(messages)

    haystacks: list[str] = [render_system_instruction(customer_name="Ahmed")]
    for content in contents:
        assert content.parts is not None
        for part in content.parts:
            assert part.text is not None
            haystacks.append(part.text)

    assert len(haystacks) == 1 + len(bodies)  # every part really was scanned
    for haystack in haystacks:
        for variant in _PHONE_VARIANTS:
            assert variant not in haystack
