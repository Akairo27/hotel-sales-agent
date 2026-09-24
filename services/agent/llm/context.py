"""Builds the model's conversation context — ARCHITECTURE.md §7.

Two closed decisions this module exists to enforce:
- "The last 10 messages only are sent with every call, not the full
  history." MESSAGE_WINDOW (services/agent/llm/config.py) is that number,
  and the window never reaches back past the start of the current session
  (services/agent/llm/session.py): a customer returning after an idle gap
  starts from a clean context.
- "Customer identity: name only, without the phone number. The phone
  number stays entirely outside the model's context — identity matching
  and reading/writing customer_phone happen in the application code
  around the model call (services/agent/llm/), not in the text the model
  sees." build_contents below builds Content objects from message bodies
  only — customer_phone never appears in a role/parts pair here or
  anywhere else in this module. The name, when known, is handled
  separately: prompt.render_system_instruction receives it directly, as a
  caller-supplied value, never read from a conversations column (no such
  column exists — see the module docstring below on why).

conversations has no name column: ARCHITECTURE.md/PLAN.md's phase 4 gets a
customer's name, if any, from the WhatsApp profile metadata on the
inbound message, not from a stored column — this module's caller (the
not-yet-built webhook) is expected to pass it straight through to
generate_reply from that payload, never write it to the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from services.agent.llm.errors import ConversationNotFoundError
from services.agent.llm.model_types import ModelTurn, Turn, UserTurn
from services.agent.llm.session import load_session_start

_INBOUND = "inbound"


@dataclass(frozen=True)
class ConversationState:
    """The negotiation- and turn-relevant state for one conversation, read
    fresh from the database — never inferred from chat history
    (ARCHITECTURE.md §7)."""

    id: int
    customer_phone: str
    active_quote_id: int | None
    concession_count: int
    turn_count: int


@dataclass(frozen=True)
class MessageRecord:
    direction: str
    body: str


def load_conversation_state(
    conn: psycopg.Connection[Any], conversation_id: int
) -> ConversationState:
    """Reads a conversation's current state.

    Raises:
        ConversationNotFoundError: no conversation with this id exists.
    """
    row = conn.execute(
        "SELECT customer_phone, active_quote_id, concession_count, turn_count "
        "FROM conversations WHERE id = %s",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise ConversationNotFoundError(
            f"conversation {conversation_id} does not exist"
        )
    customer_phone, active_quote_id, concession_count, turn_count = row
    return ConversationState(
        id=conversation_id,
        customer_phone=customer_phone,
        active_quote_id=active_quote_id,
        concession_count=concession_count,
        turn_count=turn_count,
    )


def load_recent_messages(
    conn: psycopg.Connection[Any], conversation_id: int, *, limit: int
) -> list[MessageRecord]:
    """Reads the most recent `limit` messages of the conversation's
    current session, oldest first — the exact window ARCHITECTURE.md §7
    fixes at 10 messages, not the full conversation history, and never
    reaching back across an idle gap (see session.load_session_start).
    """
    session_start = load_session_start(conn, conversation_id)
    if session_start is None:
        return []
    rows = conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE conversation_id = %s AND created_at >= %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (conversation_id, session_start, limit),
    ).fetchall()
    messages = [MessageRecord(direction=row[0], body=row[1]) for row in rows]
    messages.reverse()
    return messages


def build_contents(messages: list[MessageRecord]) -> list[Turn]:
    """Turns message rows into the provider-neutral turn history sent to
    the model (services.agent.llm.model_types) — client.py is the only
    place that translates this into a specific provider's wire format.

    Only `direction` and `body` are read — no message row's
    customer_phone column is ever touched here, so it cannot leak into a
    Turn by accident. A past outbound reply becomes a ModelTurn with no
    tool_calls: this codebase does not store tool-call history, only the
    final text of what was actually sent.
    """
    return [
        UserTurn(text=message.body)
        if message.direction == _INBOUND
        else ModelTurn(text=message.body, tool_calls=())
        for message in messages
    ]
