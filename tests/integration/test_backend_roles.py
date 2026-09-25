"""Migration 0027's backend roles, hotel_agent and hotel_worker.

The point of these roles is that the backend no longer runs as `postgres`,
which owns every table, bypasses RLS, and could UPDATE, DELETE and TRUNCATE the
append-only quotes and audit_log tables (migration 0013's lockdown revoked
those from service_role only, and nothing connects as service_role). So this
module does not trust the migration's comments: it reads the catalog to prove
what each role is and holds, makes real attempts at the writes that must be
denied, and runs the real code paths as the roles.

Real flows as hotel_agent for the webhook (conversations, messages, token
usage, escalations, the output guard) are covered by
tests/integration/test_webhook.py, whose webhook_client fixture runs every
test there a second time with the app connected as hotel_agent.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from services.agent.output_guard.quotes import load_allowed_amounts
from services.inventory.operations import check_availability, create_hold
from services.pricing.compute import compute_quote
from services.worker import runner
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_night,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel_and_room_type,
    seed_price_rule,
    seed_season,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_ROLES = ("hotel_agent", "hotel_worker")

# Every table and view in the public schema. A relation added or removed
# without deciding what each backend role may do with it fails
# test_every_public_relation_is_classified, on purpose.
_PUBLIC_RELATIONS = frozenset(
    {
        "allotments",
        "allotments_for_dashboard",
        "app_users",
        "audit_log",
        "bookings",
        "conversations",
        "escalations",
        "holds",
        "hotel_amenities",
        "hotels",
        "messages",
        "price_overrides",
        "price_rules",
        "price_rules_for_dashboard",
        "quotes",
        "room_night_inventory",
        "room_types",
        "seasons",
        "token_usage",
    }
)

# What each role may do, per relation and privilege: the columns it covers,
# or "*" for every column. This mirrors docs/plans/role-fix.md section 5 and
# migration 0027. Anything not listed here is expected to be denied.
_ALL = "*"
_MANIFEST: dict[str, dict[str, dict[str, tuple[str, ...] | str]]] = {
    "hotel_agent": {
        "conversations": {
            "SELECT": _ALL,
            "INSERT": ("customer_phone",),
            "UPDATE": (
                "customer_phone",
                "turn_count",
                "active_quote_id",
                "concession_count",
                "last_message_at",
            ),
        },
        "messages": {
            "SELECT": _ALL,
            "INSERT": (
                "conversation_id",
                "customer_phone",
                "direction",
                "whatsapp_message_id",
                "body",
            ),
        },
        "escalations": {
            "SELECT": ("id",),
            "INSERT": ("conversation_id", "customer_phone", "reason", "notes"),
        },
        "token_usage": {
            "SELECT": _ALL,
            "INSERT": (
                "conversation_id",
                "customer_phone",
                "prompt_tokens",
                "candidates_tokens",
                "total_tokens",
                "created_at",
            ),
        },
        "quotes": {
            "SELECT": _ALL,
            "INSERT": (
                "hotel_id",
                "room_type_id",
                "check_in",
                "check_out",
                "rooms",
                "ask_price_total",
                "min_allowed_total",
                "nights",
                "negotiation_open",
                "customer_phone",
                "conversation_id",
            ),
        },
        "allotments": {"SELECT": _ALL},
        "room_night_inventory": {"SELECT": _ALL},
        "seasons": {"SELECT": _ALL},
        "price_rules": {"SELECT": _ALL},
        "price_overrides": {"SELECT": _ALL},
    },
    "hotel_worker": {
        "holds": {"SELECT": _ALL, "UPDATE": ("released_at",)},
        "room_night_inventory": {"SELECT": _ALL, "UPDATE": ("held", "reserved")},
        "allotments": {"SELECT": _ALL},
    },
}

# The only functions each role may execute: the two validators the quotes
# CHECK constraint calls (a role that writes a table whose CHECK calls a
# function needs EXECUTE on it).
_EXECUTABLE_FUNCTIONS = {
    "hotel_agent": {"quotes_is_valid_night_record", "quotes_all_nights_are_complete"},
    "hotel_worker": set[str](),
}

_STATEMENT_TIMEOUTS = {"hotel_agent": "10s", "hotel_worker": "30s"}

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=True)


def _dsn_for(role: str, agent_database_url: str, worker_database_url: str) -> str:
    return agent_database_url if role == "hotel_agent" else worker_database_url


def test_every_public_relation_is_classified(db_conn: psycopg.Connection[Any]) -> None:
    rows = db_conn.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p')"
    ).fetchall()
    actual = {row[0] for row in rows}

    assert actual == _PUBLIC_RELATIONS, (
        f"added: {sorted(actual - _PUBLIC_RELATIONS)}, "
        f"removed: {sorted(_PUBLIC_RELATIONS - actual)}. Decide what each backend "
        "role may do with the relation (a migration and _MANIFEST), then update "
        "_PUBLIC_RELATIONS."
    )


@pytest.mark.parametrize("role", _ROLES)
def test_role_attributes(db_conn: psycopg.Connection[Any], role: str) -> None:
    row = db_conn.execute(
        "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
        "rolreplication, rolinherit, rolvaliduntil FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()

    assert row == (True, False, False, False, False, False, False, None)


@pytest.mark.parametrize("role", _ROLES)
def test_role_is_a_member_of_nothing_and_owns_nothing(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    memberships = db_conn.execute(
        "SELECT count(*) FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %s",
        (role,),
    ).fetchone()
    owned = db_conn.execute(
        "SELECT count(*) FROM ("
        "SELECT relowner AS owner FROM pg_class "
        "UNION ALL SELECT proowner FROM pg_proc "
        "UNION ALL SELECT nspowner FROM pg_namespace "
        "UNION ALL SELECT typowner FROM pg_type"
        ") owned WHERE owner = (SELECT oid FROM pg_roles WHERE rolname = %s)",
        (role,),
    ).fetchone()

    assert memberships == (0,)
    assert owned == (0,)


@pytest.mark.parametrize("role", _ROLES)
def test_role_has_a_statement_timeout(
    role: str, agent_database_url: str, worker_database_url: str
) -> None:
    with _connect(_dsn_for(role, agent_database_url, worker_database_url)) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone() == (
            _STATEMENT_TIMEOUTS[role],
        )


def _actual_column_privileges(
    conn: psycopg.Connection[Any], role: str
) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT c.relname, a.attname, p.privilege "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
        "AND NOT a.attisdropped "
        "CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) "
        "AS p(privilege) "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p') "
        "AND has_column_privilege(%s::name, c.oid, a.attnum, p.privilege)",
        (role,),
    ).fetchall()
    return {(table, column, privilege) for table, column, privilege in rows}


def _expected_column_privileges(
    conn: psycopg.Connection[Any], role: str
) -> set[tuple[str, str, str]]:
    columns_by_table: dict[str, list[str]] = {}
    for table, column in conn.execute(
        "SELECT c.relname, a.attname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
        "AND NOT a.attisdropped WHERE n.nspname = 'public'"
    ):
        columns_by_table.setdefault(table, []).append(column)

    expected: set[tuple[str, str, str]] = set()
    for table, privileges in _MANIFEST[role].items():
        for privilege, columns in privileges.items():
            names = columns_by_table[table] if columns == _ALL else list(columns)
            expected |= {(table, name, privilege) for name in names}
    return expected


@pytest.mark.parametrize("role", _ROLES)
def test_role_privileges_match_the_manifest_exactly(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    actual = _actual_column_privileges(db_conn, role)
    expected = _expected_column_privileges(db_conn, role)

    assert sorted(actual - expected) == [], "granted beyond the manifest"
    assert sorted(expected - actual) == [], "in the manifest but not granted"


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("privilege", ["DELETE", "TRUNCATE", "TRIGGER"])
def test_role_can_never_delete_truncate_or_trigger(
    db_conn: psycopg.Connection[Any], role: str, privilege: str
) -> None:
    granted = db_conn.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p') "
        "AND has_table_privilege(%s::name, c.oid, %s)",
        (role, privilege),
    ).fetchall()

    assert granted == []


@pytest.mark.parametrize("role", _ROLES)
def test_role_executes_only_the_functions_it_needs(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    rows = db_conn.execute(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prokind = 'f' "
        "AND has_function_privilege(%s::name, p.oid, 'EXECUTE')",
        (role,),
    ).fetchall()

    assert {row[0] for row in rows} == _EXECUTABLE_FUNCTIONS[role]


@pytest.mark.parametrize("role", _ROLES)
def test_role_has_no_sequence_or_schema_creation_privileges(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    sequences = db_conn.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'S' AND ("
        "has_sequence_privilege(%(role)s::name, c.oid, 'USAGE') "
        "OR has_sequence_privilege(%(role)s::name, c.oid, 'SELECT') "
        "OR has_sequence_privilege(%(role)s::name, c.oid, 'UPDATE'))",
        {"role": role},
    ).fetchall()
    schema = db_conn.execute(
        "SELECT has_schema_privilege(%(role)s::name, 'public', 'USAGE'), "
        "has_schema_privilege(%(role)s::name, 'public', 'CREATE')",
        {"role": role},
    ).fetchone()

    assert sequences == []
    assert schema == (True, False)


# Real attempts at the writes the append-only lockdown must refuse. quotes
# and audit_log are append-only (migration 0013); neither backend role may
# read or write audit_log at all, and the worker has no business in quotes.
_APPEND_ONLY_DENIED = (
    "UPDATE quotes SET rooms = rooms",
    "DELETE FROM quotes",
    "TRUNCATE quotes",
    "SELECT count(*) FROM audit_log",
    "INSERT INTO audit_log (table_name) VALUES ('x')",
    "UPDATE audit_log SET table_name = table_name",
    "DELETE FROM audit_log",
    "TRUNCATE audit_log",
)


@pytest.mark.parametrize("statement", _APPEND_ONLY_DENIED)
@pytest.mark.parametrize("role", _ROLES)
def test_append_only_tables_cannot_be_modified_by_either_role(
    role: str, statement: str, agent_database_url: str, worker_database_url: str
) -> None:
    with (
        _connect(_dsn_for(role, agent_database_url, worker_database_url)) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)


def test_worker_cannot_read_quotes(worker_database_url: str) -> None:
    with (
        _connect(worker_database_url) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT count(*) FROM quotes")


def test_the_roles_are_subject_to_rls(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    """A table with RLS enabled and no policy for the role returns no rows to
    it, even though the role holds SELECT: proof it is not bypassing RLS."""
    db_conn.execute("CREATE TABLE rls_probe (id integer)")
    try:
        db_conn.execute("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
        db_conn.execute("INSERT INTO rls_probe VALUES (1)")
        db_conn.execute("GRANT SELECT ON TABLE rls_probe TO hotel_agent")
        with _connect(agent_database_url) as agent:
            assert agent.execute("SELECT count(*) FROM rls_probe").fetchone() == (0,)

        db_conn.execute(
            "CREATE POLICY rls_probe_agent_select ON rls_probe "
            "FOR SELECT TO hotel_agent USING (true)"
        )
        with _connect(agent_database_url) as agent:
            assert agent.execute("SELECT count(*) FROM rls_probe").fetchone() == (1,)
    finally:
        db_conn.execute("DROP TABLE IF EXISTS rls_probe")


def test_every_write_policy_has_a_paired_select_policy(
    db_conn: psycopg.Connection[Any],
) -> None:
    """CLAUDE.md rule 11: without a paired SELECT policy a write is silently
    invisible to its own WHERE clause or, for an upsert, denied outright."""
    rows = db_conn.execute(
        "SELECT tablename, unnest(roles)::text, cmd FROM pg_policies "
        "WHERE schemaname = 'public'"
    ).fetchall()
    commands: dict[tuple[str, str], set[str]] = {}
    for table, role, command in rows:
        if role in _ROLES:
            commands.setdefault((table, role), set()).add(command)

    assert commands, "no policies found for the backend roles"
    for (table, role), granted in commands.items():
        assert "ALL" not in granted, f"{role} has an ALL policy on {table}"
        assert "DELETE" not in granted, f"{role} has a DELETE policy on {table}"
        if granted & {"INSERT", "UPDATE"}:
            assert "SELECT" in granted, f"{role} writes {table} without a SELECT policy"


def test_agent_prices_records_and_reads_back_a_quote(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    """compute_quote inserts into quotes (whose CHECK calls the two validators),
    reads seasons, price rules, overrides, allotments and room-night inventory,
    and the output guard reads the quote back: all as hotel_agent."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_season(
        db_conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )
    seed_price_rule(
        db_conn,
        scope="global",
        target_margin_bps=2000,
        min_profit_by_lead_time=flat_min_profit(2000),
        demand_curve=flat_demand_curve(10_000),
    )
    seed_allotment_night(
        db_conn,
        hotel_id,
        room_type_id,
        date(2026, 9, 1),
        total_rooms=5,
        cost_per_night=10_000,
    )
    conversation_id = seed_conversation(db_conn)

    with _connect(agent_database_url) as agent:
        available = check_availability(
            agent, hotel_id, room_type_id, date(2026, 9, 1), date(2026, 9, 2), 1
        )
        quote = compute_quote(
            agent,
            hotel_id,
            room_type_id,
            date(2026, 9, 1),
            date(2026, 9, 2),
            1,
            _NOW,
            customer_phone="+966500000001",
            conversation_id=conversation_id,
        )
        allowed = load_allowed_amounts(agent, conversation_id)

    assert available is True
    assert quote.ask_price_total == 12_000
    assert quote.ask_price_total in allowed.amounts_halalas
    assert db_conn.execute("SELECT count(*) FROM quotes").fetchone() == (1,)


def test_worker_pass_releases_an_expired_hold_as_hotel_worker(
    db_conn: psycopg.Connection[Any],
    worker_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The scheduled worker's whole pass, as the role it runs as in
    production: locks the night rows, sets held and reserved, and marks the
    hold released, exactly once."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, date(2026, 6, 5), nights=1, total_rooms=2
    )
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        date(2026, 6, 5),
        date(2026, 6, 6),
        2,
        datetime(2026, 6, 1, tzinfo=UTC),
        idempotency_key="backend-roles-worker",
    )
    monkeypatch.setenv("DATABASE_URL", worker_database_url)
    caplog.set_level(logging.INFO, logger=runner.logger.name)

    first_exit = runner.main()
    second_exit = runner.main()

    assert (first_exit, second_exit) == (runner.EXIT_OK, runner.EXIT_OK)
    held = db_conn.execute("SELECT held FROM room_night_inventory").fetchone()
    released = db_conn.execute(
        "SELECT released_at IS NOT NULL FROM holds WHERE id = %s", (hold_id,)
    ).fetchone()
    assert held == (0,)
    assert released == (True,)
