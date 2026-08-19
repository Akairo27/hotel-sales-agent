"""Verifies migration 0012's schema-level default-privilege lockdown.

Every migration through 0011 relied on a deny-list: create the table, then
explicitly REVOKE ALL ... FROM anon, authenticated. That only works as
long as nobody forgets the REVOKE line on a future table. Migration 0012
flips the default itself — ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN
SCHEMA public REVOKE ALL ... FROM anon, authenticated — so a brand-new
table with *no* grant statements of its own must already deny both roles,
before any per-table discipline is applied at all.

This is a trip-wire, same spirit as test_cost_tables_rls.py: it does not
prove today's protection (that's migration 0012 itself, confirmed for
real against a live Supabase project — see that migration's comment).
Its job is to fail loudly the day something reintroduces a default-open
posture in `public` — a superuser re-running the old
`ALTER DEFAULT PRIVILEGES ... GRANT ALL ...` by accident, or a future
migration granting broadly "just to unblock a screen".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.usefixtures("db_conn")

_SCRATCH_TABLE = "_default_privileges_lockdown_check"
_SCRATCH_FUNCTION = "_default_privileges_lockdown_fn_check"


@pytest.fixture
def _scratch_table_with_no_grants(db_conn: psycopg.Connection[Any]) -> Iterator[None]:
    """A table created the same way every real migration does — as
    postgres, in public — but with zero GRANT/REVOKE statements of its
    own, so the schema-level default is what's under test, not per-table
    discipline."""
    create = sql.SQL(
        "CREATE TABLE {table} (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
    ).format(table=sql.Identifier(_SCRATCH_TABLE))
    drop = sql.SQL("DROP TABLE {table}").format(table=sql.Identifier(_SCRATCH_TABLE))
    db_conn.execute(create)
    try:
        yield
    finally:
        db_conn.execute(drop)


def test_authenticated_cannot_select_ungranted_table(
    db_conn: psycopg.Connection[Any], _scratch_table_with_no_grants: None
) -> None:
    """authenticated has schema USAGE (migration 0010) but the default ACL
    no longer grants it anything on a table that never grants it anything
    itself — permission denied at the table, not a missing schema."""
    select = sql.SQL("SELECT * FROM {table}").format(
        table=sql.Identifier(_SCRATCH_TABLE)
    )
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(select)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_anon_cannot_select_ungranted_table(
    db_conn: psycopg.Connection[Any], _scratch_table_with_no_grants: None
) -> None:
    """anon has schema USAGE (tests/supabase_default_acl_baseline.sql
    simulates the real Supabase default) but the default ACL grants it
    nothing on a table that never grants it anything itself — permission
    denied at the table, same failure mode as authenticated."""
    select = sql.SQL("SELECT * FROM {table}").format(
        table=sql.Identifier(_SCRATCH_TABLE)
    )
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(select)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


@pytest.fixture
def _scratch_function_with_no_grants(
    db_conn: psycopg.Connection[Any],
) -> Iterator[None]:
    """A function created the same way every real migration does — as
    postgres, in public — but with zero GRANT statements of its own.
    Isolates the functions half of migration 0012: Postgres grants
    EXECUTE on every new function to the PUBLIC pseudo-role automatically
    (every role, anon/authenticated included, is implicitly a PUBLIC
    member), a default that a naive schema-scoped ALTER DEFAULT
    PRIVILEGES REVOKE does not actually suppress — confirmed empirically
    while writing migration 0012, see its comment."""
    create = sql.SQL(
        "CREATE FUNCTION {fn}() RETURNS boolean LANGUAGE sql AS $$ SELECT true $$"
    ).format(fn=sql.Identifier(_SCRATCH_FUNCTION))
    drop = sql.SQL("DROP FUNCTION {fn}()").format(fn=sql.Identifier(_SCRATCH_FUNCTION))
    db_conn.execute(create)
    try:
        yield
    finally:
        db_conn.execute(drop)


def test_authenticated_cannot_execute_ungranted_function(
    db_conn: psycopg.Connection[Any], _scratch_function_with_no_grants: None
) -> None:
    """authenticated has schema USAGE (migration 0010) but the default ACL
    no longer grants it EXECUTE — via PUBLIC or by name — on a function
    that never grants it anything itself."""
    call = sql.SQL("SELECT {fn}()").format(fn=sql.Identifier(_SCRATCH_FUNCTION))
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(call)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_anon_cannot_execute_ungranted_function(
    db_conn: psycopg.Connection[Any], _scratch_function_with_no_grants: None
) -> None:
    """anon has schema USAGE (simulated per the table test above) but no
    EXECUTE grant on this function — permission denied, same failure
    mode as authenticated."""
    call = sql.SQL("SELECT {fn}()").format(fn=sql.Identifier(_SCRATCH_FUNCTION))
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(call)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
