"""Verifies migration 0025's constraints, cascade behaviour, append-only
grants, and RLS for token_usage against a real Postgres instance — see
CLAUDE.md rule 3: the DB constraint is the source of truth, not
application discipline.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tests.integration._seed import seed_conversation

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"


def _insert_token_usage(
    conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    prompt_tokens: int = 10,
    candidates_tokens: int = 5,
    total_tokens: int = 15,
) -> int:
    row = conn.execute(
        "INSERT INTO token_usage (conversation_id, customer_phone, prompt_tokens, "
        "candidates_tokens, total_tokens) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (conversation_id, _PHONE, prompt_tokens, candidates_tokens, total_tokens),
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("prompt_tokens", "token_usage_prompt_tokens_non_negative"),
        ("candidates_tokens", "token_usage_candidates_tokens_non_negative"),
        ("total_tokens", "token_usage_total_tokens_non_negative"),
    ],
)
def test_token_usage_counts_cannot_be_negative(
    db_conn: psycopg.Connection[Any], column: str, constraint: str
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    kwargs: dict[str, Any] = {
        "prompt_tokens": 10,
        "candidates_tokens": 5,
        "total_tokens": 15,
        column: -1,
    }

    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        _insert_token_usage(db_conn, conversation_id, **kwargs)


def test_token_usage_is_deleted_when_conversation_is_deleted(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _insert_token_usage(db_conn, conversation_id)

    db_conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))

    assert db_conn.execute("SELECT count(*) FROM token_usage").fetchone() == (0,)


def test_token_usage_is_append_only_update_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Same class of protection as quotes' append-only lockdown (migration
    0013) — a usage log must never be mutated after the fact, enforced by
    never granting service_role UPDATE, not by application discipline."""
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    usage_id = _insert_token_usage(db_conn, conversation_id)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "UPDATE token_usage SET total_tokens = 0 WHERE id = %s", (usage_id,)
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_token_usage_is_append_only_delete_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    usage_id = _insert_token_usage(db_conn, conversation_id)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("DELETE FROM token_usage WHERE id = %s", (usage_id,))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_rls_denies_anon_on_token_usage(db_conn: psycopg.Connection[Any]) -> None:
    """anon has real Supabase-default schema USAGE on public (see
    tests/supabase_default_acl_baseline.sql), so Postgres resolves the
    table name fine; what denies it is InsufficientPrivilege, not
    UndefinedTable — same expectation as
    test_rls_denies_anon_on_agent_tables."""
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM token_usage")
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
