"""Shared fixtures for tests that need a real Postgres instance.

TEST_DATABASE_URL must point at an already-existing, otherwise-empty
Postgres 17 database. If it is unset, every test depending on these
fixtures is skipped explicitly rather than faked with a mock connection.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_ROLES_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN BYPASSRLS;
    END IF;
END
$$;
"""

# A minimal stand-in for the pieces of real Supabase's `auth` schema this
# schema's migrations reference (app_users.id's FK target, and auth.uid()
# in RLS policies) — Supabase provides both for real; plain Postgres does
# not. Lives outside "public" so DROP SCHEMA public CASCADE in _schema
# below never touches it, same reasoning as _ROLES_SQL being cluster-wide.
# auth.uid() mirrors Supabase's real implementation: it reads the
# session-local "request.jwt.claim.sub" GUC that PostgREST sets per
# request from the verified JWT — tests set it directly to simulate being
# signed in as a given user.
_AUTH_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$
    SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
GRANT SELECT ON auth.users TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION auth.uid() TO anon, authenticated, service_role;
"""

_TABLES_TO_TRUNCATE = (
    "audit_log",
    "app_users",
    "quotes",
    "price_overrides",
    "price_rules",
    "holds",
    "room_night_inventory",
    "allotments",
    "seasons",
    "hotel_amenities",
    "room_types",
    "hotels",
)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """The Postgres DSN for tests that require a real database connection."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if dsn is None:
        pytest.skip("TEST_DATABASE_URL not set — skipping real-database tests")
    return dsn


@pytest.fixture(scope="session")
def _schema(test_database_url: str) -> None:
    """Recreate the public schema and apply every migration, once per session.

    Postgres roles are cluster-wide, not per-database, so they are created
    if absent rather than by the schema reset.
    """
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(_ROLES_SQL)
        conn.execute(_AUTH_SCHEMA_SQL)
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(migration.read_text(encoding="utf-8"))
    return None


@pytest.fixture
def db_conn(test_database_url: str, _schema: None) -> Iterator[psycopg.Connection[Any]]:
    """A fresh, truncated-clean autocommit connection at the same privilege
    level services/inventory holds through DATABASE_URL in production.

    Autocommit keeps a failed constraint from poisoning the rest of a test;
    code that needs an atomic multi-statement transaction opens one
    explicitly with ``conn.transaction()``. Tests that verify RLS actually
    denies an unprivileged role connect as anon/authenticated instead of
    using this fixture.
    """
    conn: psycopg.Connection[Any] = psycopg.connect(test_database_url, autocommit=True)
    table_list = sql.SQL(", ").join(sql.Identifier(t) for t in _TABLES_TO_TRUNCATE)
    truncate = sql.SQL("TRUNCATE {tables} RESTART IDENTITY CASCADE").format(
        tables=table_list
    )
    conn.execute(truncate)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def sign_in_as(db_conn: psycopg.Connection[Any]) -> Iterator[Callable[[str], None]]:
    """Returns a function that switches db_conn to the `authenticated` role
    with auth.uid() resolving to the given user id — simulating an RLS
    policy evaluating that specific user's own request, the same way
    PostgREST does it against real Supabase.
    """

    def _sign_in(user_id: str) -> None:
        db_conn.execute("SET SESSION AUTHORIZATION authenticated")
        db_conn.execute(
            sql.SQL("SET request.jwt.claim.sub = {}").format(sql.Literal(user_id))
        )

    try:
        yield _sign_in
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
        db_conn.execute("RESET request.jwt.claim.sub")
