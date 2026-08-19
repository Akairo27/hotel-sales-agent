"""Fails loudly if a real Supabase project's default privileges diverge
from what tests/supabase_default_acl_baseline.sql assumes.

Every privilege/RLS test in tests/integration/ relies on
tests/conftest.py's _schema fixture reproducing Supabase's real default
ACL locally, via that file. That reproduction is only as good as our own
understanding of what Supabase actually does — if Supabase ever changes
its provisioning template, the local simulation drifts silently out of
sync with reality, and every test built on top of it goes back to
proving nothing, the same way the pre-simulation suite did for
service_role's leaked UPDATE/DELETE on quotes/audit_log (see
db/migrations/0013's commit message). This script is what catches that:
it creates a real, throwaway table/function directly in a pristine
Supabase project's public schema, reads their actual default privileges
via has_table_privilege/has_function_privilege/has_schema_privilege —
the same functions Postgres itself uses to answer "can this role do
this" — and compares them against the exact same expectation
tests/supabase_default_acl_baseline.sql grants. A mismatch is Supabase's
template changing under us, not a bug in this repo's own migrations.

Requires a project that has NEVER had db/migrations/ applied to it and
never will — the whole point is reading Supabase's own pristine
provisioning, not anything this repo's migrations have touched. Point it
at a dedicated reference project, not the real target project or any dev
project migrations get applied to.

Not a pytest test (no test_ prefix, no pytest fixtures) — lives in
tests/ anyway, alongside supabase_default_acl_baseline.sql and
conftest.py, since it exists to keep that one file honest and has no
other home in this repo.

Usage:
    ACL_BASELINE_DATABASE_URL=postgresql://postgres:<password>@\
db.<ref>.supabase.co:5432/postgres \
        uv run python tests/verify_default_acl_baseline.py

Exits 0 if every check matches, 1 (with a readable report) otherwise.
Intended to run in the periodic gate on any PR touching db/migrations/
(see PLAN.md) — not on every regular test run, since it needs real
network access to a live Supabase project that tests/conftest.py's local
suite has no other reason to depend on.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

import psycopg

# Mirrors exactly what tests/supabase_default_acl_baseline.sql grants —
# anon, authenticated, and service_role all get every privilege on new
# tables/sequences/functions in public by default on a real Supabase
# project; anon additionally gets schema USAGE. Kept as readable
# has_*_privilege checks against a real, newly created object rather
# than as raw ACL strings, because this exact area has already produced
# more than one non-obvious surprise this session (see
# db/migrations/0012's own comment) — asserting observable behavior is
# more robust than asserting a particular ACL bitmask representation.
_ROLES = ("anon", "authenticated", "service_role")


@dataclass(frozen=True)
class _Check:
    description: str
    role: str
    expected: bool
    actual: bool

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def _fetch_bool(
    conn: psycopg.Connection[Any], query: str, params: tuple[str, ...] = ()
) -> bool:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return bool(row[0])


def _check_table_defaults(conn: psycopg.Connection[Any]) -> list[_Check]:
    conn.execute(
        "CREATE TABLE public._acl_baseline_check_table "
        "(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
    )
    try:
        return [
            _Check(
                "new table SELECT",
                role,
                True,
                _fetch_bool(
                    conn,
                    "SELECT has_table_privilege(%s, "
                    "'public._acl_baseline_check_table', 'SELECT')",
                    (role,),
                ),
            )
            for role in _ROLES
        ]
    finally:
        conn.execute("DROP TABLE public._acl_baseline_check_table")


def _check_function_defaults(conn: psycopg.Connection[Any]) -> list[_Check]:
    conn.execute(
        "CREATE FUNCTION public._acl_baseline_check_fn() RETURNS boolean "
        "LANGUAGE sql AS $$ SELECT true $$"
    )
    try:
        return [
            _Check(
                "new function EXECUTE",
                role,
                True,
                _fetch_bool(
                    conn,
                    "SELECT has_function_privilege(%s, "
                    "'public._acl_baseline_check_fn()', 'EXECUTE')",
                    (role,),
                ),
            )
            for role in _ROLES
        ]
    finally:
        conn.execute("DROP FUNCTION public._acl_baseline_check_fn()")


def _check_anon_schema_usage(conn: psycopg.Connection[Any]) -> _Check:
    return _Check(
        "schema USAGE",
        "anon",
        True,
        _fetch_bool(conn, "SELECT has_schema_privilege('anon', 'public', 'USAGE')"),
    )


def main() -> int:
    dsn = os.environ.get("ACL_BASELINE_DATABASE_URL")
    if not dsn:
        sys.stderr.write(
            "ACL_BASELINE_DATABASE_URL not set — point it at a dedicated, "
            "never-migrated Supabase project's direct Postgres connection.\n"
        )
        return 2

    checks: list[_Check] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        checks.extend(_check_table_defaults(conn))
        checks.extend(_check_function_defaults(conn))
        checks.append(_check_anon_schema_usage(conn))

    failures = [c for c in checks if not c.ok]
    for check in checks:
        status = "ok" if check.ok else "MISMATCH"
        sys.stdout.write(
            f"[{status}] {check.description} / {check.role}: "
            f"expected={check.expected} actual={check.actual}\n"
        )

    if failures:
        sys.stderr.write(
            f"\n{len(failures)} default-privilege check(s) no longer match "
            "tests/supabase_default_acl_baseline.sql. Supabase's own "
            "provisioning template has likely changed — update that file "
            "(and re-verify every test built on top of it), do not just "
            "silence this script.\n"
        )
        return 1

    sys.stdout.write(f"\nAll {len(checks)} checks match the simulated baseline.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
