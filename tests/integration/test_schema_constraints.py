"""Verifies the phase-1 schema's own guarantees hold against a real Postgres
instance — not that application code respects them, but that the database
itself refuses to be misused. See CLAUDE.md rule 3: application-level checks
are advisory only; the DB constraint is the source of truth.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.integration._seed import seed_actor

pytestmark = pytest.mark.usefixtures("db_conn")

# A minimal, structurally valid min_profit_by_lead_time: one band covering
# the whole domain (0 to open-ended). Used wherever a test needs *a* valid
# value and isn't specifically exercising the band-shape constraints. Bound
# as a query parameter (never string-interpolated into SQL) like every
# other jsonb value in this file.
_VALID_MIN_PROFIT = (
    '{"bands": [{"min_lead_days": 0, "max_lead_days": null, '
    '"min_profit_halalas": 1000}]}'
)
_VALID_DEMAND_CURVE = (
    '{"occupancy_bands": [{"min": 0, "max": 1, "multiplier_bps": 10000}], '
    '"lead_time_bands": [{"min_lead_days": 0, "max_lead_days": null, '
    '"multiplier_bps": 10000}]}'
)
# A minimal, structurally valid quotes.nights: one override-applied night
# (the simplest valid shape — no computation-detail fields required). See
# db/migrations/0009_quotes_nights_audit.sql.
_VALID_QUOTE_NIGHTS = (
    '[{"date": "2026-09-01", "season_id": 1, "ask": 20000, "min_allowed": 10000, '
    '"override_applied": true}]'
)


def _returning_id(
    conn: psycopg.Connection[Any], query: str, params: tuple[Any, ...] = ()
) -> int:
    """Runs an INSERT ... RETURNING id and returns that id.

    Centralises the None-check psycopg's Optional fetchone() return type
    otherwise forces at every call site.
    """
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(row[0])


def _seed_single_room_night(conn: psycopg.Connection[Any]) -> int:
    """Inserts one hotel/room-type/allotment with a single-room night and
    returns the allotment id."""
    hotel_id = _returning_id(
        conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )
    allotment_id = _returning_id(
        conn,
        "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
        "cost_per_night) VALUES (%s, %s, '2026-09-01', 1, 10000) RETURNING id",
        (hotel_id, room_type_id),
    )
    conn.execute(
        "INSERT INTO room_night_inventory (allotment_id, stay_date, total) "
        "VALUES (%s, '2026-09-01', 1)",
        (allotment_id,),
    )
    return allotment_id


def test_inventory_never_oversold_blocks_direct_oversell(
    db_conn: psycopg.Connection[Any],
) -> None:
    allotment_id = _seed_single_room_night(db_conn)

    with pytest.raises(psycopg.errors.CheckViolation, match="inventory_never_oversold"):
        db_conn.execute(
            "UPDATE room_night_inventory SET held = held + 999 WHERE allotment_id = %s",
            (allotment_id,),
        )


def test_inventory_never_oversold_blocks_negative_reserved(
    db_conn: psycopg.Connection[Any],
) -> None:
    allotment_id = _seed_single_room_night(db_conn)

    with pytest.raises(psycopg.errors.CheckViolation, match="inventory_never_oversold"):
        db_conn.execute(
            "UPDATE room_night_inventory SET reserved = -1 WHERE allotment_id = %s",
            (allotment_id,),
        )


def test_allotment_cost_cannot_be_negative(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="allotments_cost_non_negative"
    ):
        db_conn.execute(
            "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
            "cost_per_night) VALUES (%s, %s, '2026-09-01', 1, -5)",
            (hotel_id, room_type_id),
        )


def test_room_night_inventory_date_must_match_its_allotment(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The composite foreign key on (allotment_id, stay_date) means a night
    can never be recorded against a date its allotment does not own."""
    allotment_id = _seed_single_room_night(db_conn)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db_conn.execute(
            "INSERT INTO room_night_inventory (allotment_id, stay_date, total) "
            "VALUES (%s, '2026-09-02', 1)",
            (allotment_id,),
        )


def test_at_most_one_default_season(db_conn: psycopg.Connection[Any]) -> None:
    db_conn.execute(
        "INSERT INTO seasons (season_name, calendar_type, start_month, start_day, "
        "end_month, end_day, priority, is_default) "
        "VALUES ('Default', 'gregorian', 1, 1, 12, 31, 0, true)"
    )

    with pytest.raises(psycopg.errors.UniqueViolation, match="seasons_single_default"):
        db_conn.execute(
            "INSERT INTO seasons (season_name, calendar_type, start_month, start_day, "
            "end_month, end_day, priority, is_default) "
            "VALUES ('Another Default', 'hijri', 1, 1, 12, 30, 0, true)"
        )


def test_holds_cannot_be_released_and_confirmed_at_once(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="holds_not_released_and_confirmed"
    ):
        db_conn.execute(
            "INSERT INTO holds (hotel_id, room_type_id, check_in, check_out, rooms, "
            "expires_at, released_at, confirmed_at, requires_full_payment, "
            "idempotency_key) "
            "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, now(), now(), now(), "
            "false, 'released-and-confirmed')",
            (hotel_id, room_type_id),
        )


def test_holds_idempotency_key_must_be_unique(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )
    db_conn.execute(
        "INSERT INTO holds (hotel_id, room_type_id, check_in, check_out, rooms, "
        "expires_at, requires_full_payment, idempotency_key) "
        "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, now(), false, 'dup-key')",
        (hotel_id, room_type_id),
    )

    with pytest.raises(
        psycopg.errors.UniqueViolation, match="holds_idempotency_key_unique"
    ):
        db_conn.execute(
            "INSERT INTO holds (hotel_id, room_type_id, check_in, check_out, rooms, "
            "expires_at, requires_full_payment, idempotency_key) "
            "VALUES (%s, %s, '2026-09-03', '2026-09-04', 1, now(), false, 'dup-key')",
            (hotel_id, room_type_id),
        )


def test_holds_expires_at_is_timezone_aware(db_conn: psycopg.Connection[Any]) -> None:
    """confirm_hold compares `now >= expires_at` with a timezone-aware `now`
    (services/inventory/hold_windows.py enforces that at the boundary). If
    this column were ever changed to a naive `timestamp`, psycopg would
    return a naive datetime and that comparison would raise TypeError —
    see CLAUDE.md rule 6: every timestamp in this system is UTC-aware.
    """
    row = db_conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'holds' AND column_name = 'expires_at'"
    ).fetchone()
    assert row == ("timestamp with time zone",)


def test_hold_room_type_must_belong_to_its_hotel(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The composite foreign key on (room_type_id, hotel_id) means a hold can
    never be recorded against a room type from a different hotel."""
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Hotel A') RETURNING id"
    )
    other_hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Hotel B') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db_conn.execute(
            "INSERT INTO holds (hotel_id, room_type_id, check_in, check_out, rooms, "
            "expires_at, requires_full_payment, idempotency_key) "
            "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, now(), false, "
            "'cross-hotel-room-type')",
            (other_hotel_id, room_type_id),
        )


def test_rls_denies_anon_even_when_granted_table_select(
    db_conn: psycopg.Connection[Any],
) -> None:
    """RLS is the last line of defence: even an explicit table-level GRANT
    must not expose rows to anon, because zero policies are defined."""
    _seed_single_room_night(db_conn)

    db_conn.execute("GRANT USAGE ON SCHEMA public TO anon")
    db_conn.execute("GRANT SELECT ON hotels TO anon")
    try:
        db_conn.execute("SET SESSION AUTHORIZATION anon")
        rows = db_conn.execute("SELECT * FROM hotels").fetchall()
        assert rows == []
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
        db_conn.execute("REVOKE SELECT ON hotels FROM anon")
        db_conn.execute("REVOKE USAGE ON SCHEMA public FROM anon")


def test_rls_denies_authenticated_even_when_granted_table_select(
    db_conn: psycopg.Connection[Any],
) -> None:
    """authenticated already holds SELECT on hotels permanently — migration
    0014 grants it so the dashboard can read the table — and this session
    sets no request.jwt.claim.sub, so current_app_role() is NULL and
    hotels_select_for_active_users hides every row anyway. That is the
    assertion: the grant is real, and RLS still returns nothing.

    Neither the grant nor schema USAGE is set up or torn down here. Both
    belong to migrations (0014 and 0010), not to this test: a teardown
    REVOKE would strip them for the rest of the session, breaking every
    later test that reads hotels as authenticated rather than undoing
    anything this test did.
    """
    _seed_single_room_night(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        rows = db_conn.execute("SELECT * FROM hotels").fetchall()
        assert rows == []
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_service_role_bypasses_rls(db_conn: psycopg.Connection[Any]) -> None:
    """service_role is the one role our own backend connects as — it must
    see rows RLS would otherwise hide, or every service query would break."""
    _seed_single_room_night(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        rows = db_conn.execute("SELECT * FROM hotels").fetchall()
        assert len(rows) == 1
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_price_rules_only_one_global_rule_allowed(
    db_conn: psycopg.Connection[Any],
) -> None:
    # The first INSERT succeeds and fires migration 0018's AFTER INSERT
    # audit trigger, which requires app.actor_id to be set — the second,
    # rejected INSERT never reaches that trigger at all, since a
    # UniqueViolation stops the row from being inserted in the first
    # place, so it needs no actor of its own.
    seed_actor(db_conn)
    db_conn.execute(
        "INSERT INTO price_rules (scope, target_margin_bps, min_profit_by_lead_time, "
        "demand_curve) VALUES ('global', 3000, %s::jsonb, %s::jsonb)",
        (_VALID_MIN_PROFIT, _VALID_DEMAND_CURVE),
    )

    with pytest.raises(
        psycopg.errors.UniqueViolation, match="price_rules_single_global"
    ):
        db_conn.execute(
            "INSERT INTO price_rules (scope, target_margin_bps, "
            "min_profit_by_lead_time, demand_curve) "
            "VALUES ('global', 4000, %s::jsonb, %s::jsonb)",
            (_VALID_MIN_PROFIT, _VALID_DEMAND_CURVE),
        )


def test_price_rules_scope_id_must_be_null_for_global(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_scope_id_matches_scope"
    ):
        db_conn.execute(
            "INSERT INTO price_rules (scope, scope_id, target_margin_bps, "
            "min_profit_by_lead_time, demand_curve) "
            "VALUES ('global', %s, 3000, %s::jsonb, %s::jsonb)",
            (hotel_id, _VALID_MIN_PROFIT, _VALID_DEMAND_CURVE),
        )


def test_price_rules_scope_id_required_for_non_global_scope(
    db_conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_scope_id_matches_scope"
    ):
        db_conn.execute(
            "INSERT INTO price_rules (scope, scope_id, target_margin_bps) "
            "VALUES ('hotel', NULL, 3000)"
        )


def test_price_rules_global_rule_must_set_every_field(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The global rule is the base of the inheritance chain — it has
    nothing to fall through to, so a partially-configured global rule
    would leave some field unresolvable for every other scope."""
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_global_is_complete"
    ):
        db_conn.execute(
            "INSERT INTO price_rules (scope, target_margin_bps) VALUES ('global', 3000)"
        )


def test_price_rules_global_rule_accepts_a_complete_valid_config(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The positive case: a fully valid config is not rejected by the
    band-shape constraints — they reject malformed bands, not bands."""
    seed_actor(db_conn)
    db_conn.execute(
        "INSERT INTO price_rules (scope, target_margin_bps, min_profit_by_lead_time, "
        "demand_curve) VALUES ('global', 3000, %s::jsonb, %s::jsonb)",
        (_VALID_MIN_PROFIT, _VALID_DEMAND_CURVE),
    )
    row = db_conn.execute("SELECT count(*) FROM price_rules").fetchone()
    assert row == (1,)


def _insert_price_rule(
    conn: psycopg.Connection[Any], min_profit: str, demand_curve: str
) -> None:
    conn.execute(
        "INSERT INTO price_rules (scope, target_margin_bps, "
        "min_profit_by_lead_time, demand_curve) "
        "VALUES ('global', 3000, %s::jsonb, %s::jsonb)",
        (min_profit, demand_curve),
    )


def test_price_rules_min_profit_bands_reject_a_gap(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A gap between bands (here: lead day 5 to 10 is covered by neither)
    must fail at INSERT time — there is no safe default for "no profit
    floor defined at this lead time" to fall back to at pricing time."""
    bands = (
        '{"bands": [{"min_lead_days": 0, "max_lead_days": 5, '
        '"min_profit_halalas": 5000}, {"min_lead_days": 10, '
        '"max_lead_days": null, "min_profit_halalas": 2000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_min_profit_bands_valid"
    ):
        _insert_price_rule(db_conn, bands, _VALID_DEMAND_CURVE)


def test_price_rules_min_profit_bands_reject_an_overlap(
    db_conn: psycopg.Connection[Any],
) -> None:
    bands = (
        '{"bands": [{"min_lead_days": 0, "max_lead_days": 10, '
        '"min_profit_halalas": 5000}, {"min_lead_days": 5, '
        '"max_lead_days": null, "min_profit_halalas": 2000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_min_profit_bands_valid"
    ):
        _insert_price_rule(db_conn, bands, _VALID_DEMAND_CURVE)


def test_price_rules_min_profit_bands_reject_missing_zero_start(
    db_conn: psycopg.Connection[Any],
) -> None:
    bands = (
        '{"bands": [{"min_lead_days": 1, "max_lead_days": null, '
        '"min_profit_halalas": 5000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_min_profit_bands_valid"
    ):
        _insert_price_rule(db_conn, bands, _VALID_DEMAND_CURVE)


def test_price_rules_min_profit_bands_reject_missing_open_end(
    db_conn: psycopg.Connection[Any],
) -> None:
    bands = (
        '{"bands": [{"min_lead_days": 0, "max_lead_days": 30, '
        '"min_profit_halalas": 5000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_min_profit_bands_valid"
    ):
        _insert_price_rule(db_conn, bands, _VALID_DEMAND_CURVE)


def test_price_rules_min_profit_bands_reject_negative_profit(
    db_conn: psycopg.Connection[Any],
) -> None:
    bands = (
        '{"bands": [{"min_lead_days": 0, "max_lead_days": null, '
        '"min_profit_halalas": -1}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_min_profit_bands_valid"
    ):
        _insert_price_rule(db_conn, bands, _VALID_DEMAND_CURVE)


def test_price_rules_min_profit_bands_reject_missing_bands_key(
    db_conn: psycopg.Connection[Any],
) -> None:
    """An object with no "bands" key at all (e.g. an empty '{}') must be
    rejected, not silently treated as if no bands were defined — that
    ambiguity is exactly what let this slip through before this fix."""
    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_min_profit_bands_valid"
    ):
        _insert_price_rule(db_conn, "{}", _VALID_DEMAND_CURVE)


def test_price_rules_demand_curve_lead_time_bands_reject_a_gap(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Same shared validation function as min_profit's bands, applied to
    demand_curve.lead_time_bands (value field multiplier_bps instead of
    min_profit_halalas) — proves it's wired up for both jsonb columns."""
    demand_curve = (
        '{"occupancy_bands": [{"min": 0, "max": 1, "multiplier_bps": 10000}], '
        '"lead_time_bands": [{"min_lead_days": 0, "max_lead_days": 5, '
        '"multiplier_bps": 11000}, {"min_lead_days": 10, "max_lead_days": null, '
        '"multiplier_bps": 10000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="price_rules_demand_curve_lead_time_bands_valid",
    ):
        _insert_price_rule(db_conn, _VALID_MIN_PROFIT, demand_curve)


def test_price_rules_demand_curve_occupancy_bands_reject_a_gap(
    db_conn: psycopg.Connection[Any],
) -> None:
    demand_curve = (
        '{"occupancy_bands": [{"min": 0, "max": 0.5, "multiplier_bps": 10000}, '
        '{"min": 0.6, "max": 1, "multiplier_bps": 12500}], '
        '"lead_time_bands": [{"min_lead_days": 0, "max_lead_days": null, '
        '"multiplier_bps": 10000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="price_rules_demand_curve_occupancy_bands_valid",
    ):
        _insert_price_rule(db_conn, _VALID_MIN_PROFIT, demand_curve)


def test_price_rules_demand_curve_occupancy_bands_reject_unbounded_end(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Occupancy is closed 0..1, unlike lead time — a band left open-ended
    (or one that doesn't reach 1.0) must be rejected."""
    demand_curve = (
        '{"occupancy_bands": [{"min": 0, "max": 0.9, "multiplier_bps": 10000}], '
        '"lead_time_bands": [{"min_lead_days": 0, "max_lead_days": null, '
        '"multiplier_bps": 10000}]}'
    )
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="price_rules_demand_curve_occupancy_bands_valid",
    ):
        _insert_price_rule(db_conn, _VALID_MIN_PROFIT, demand_curve)


def test_price_overrides_min_allowed_cannot_exceed_ask(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_overrides_min_allowed_not_above_ask"
    ):
        db_conn.execute(
            "INSERT INTO price_overrides (hotel_id, room_type_id, stay_date, "
            "ask_price_override, min_allowed_override, expires_at) "
            "VALUES (%s, %s, '2026-09-01', 10000, 20000, now())",
            (hotel_id, room_type_id),
        )


def test_quotes_min_allowed_cannot_exceed_ask(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="quotes_min_allowed_not_above_ask"
    ):
        db_conn.execute(
            "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
            "ask_price_total, min_allowed_total, nights, negotiation_open) "
            "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, 10000, 20000, %s::jsonb, "
            "true)",
            (hotel_id, room_type_id, _VALID_QUOTE_NIGHTS),
        )


def _seed_quote(db_conn: psycopg.Connection[Any]) -> int:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )
    return _returning_id(
        db_conn,
        "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
        "ask_price_total, min_allowed_total, nights, negotiation_open) "
        "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, 20000, 10000, %s::jsonb, "
        "true) RETURNING id",
        (hotel_id, room_type_id, _VALID_QUOTE_NIGHTS),
    )


def test_quotes_is_append_only_update_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    """quotes is INSERT-only per ARCHITECTURE.md §4 — enforced by never
    granting service_role UPDATE on this table, not by application
    discipline. See CLAUDE.md rule 3: the constraint is the authority."""
    quote_id = _seed_quote(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "UPDATE quotes SET ask_price_total = 1 WHERE id = %s", (quote_id,)
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_quotes_is_append_only_delete_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    quote_id = _seed_quote(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("DELETE FROM quotes WHERE id = %s", (quote_id,))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_quotes_nights_empty_array_rejected(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="quotes_nights_are_complete"
    ):
        db_conn.execute(
            "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
            "ask_price_total, min_allowed_total, nights, negotiation_open) "
            "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, 10000, 10000, '[]'::jsonb, "
            "true)",
            (hotel_id, room_type_id),
        )


def test_quotes_nights_computed_night_missing_a_detail_field_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A night with override_applied=false must carry every computation
    field — missing even one (occupancy, here) means the record can't
    answer "why was this priced this way", so it must not save."""
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )
    incomplete_night = (
        '[{"date": "2026-09-01", "season_id": 1, "ask": 12000, "min_allowed": 12000, '
        '"override_applied": false, "cost_per_night": 10000, '
        '"target_margin_bps": 2000, "target_margin_rule_id": 1, '
        '"price_after_margin": 12000, "occupancy_multiplier_bps": 10000, '
        '"lead_time_multiplier_bps": 10000, "demand_factor_bps": 10000, '
        '"demand_curve_rule_id": 1, "min_profit_halalas": 2000, '
        '"min_profit_rule_id": 1}]'
        # "occupancy" deliberately omitted
    )

    with pytest.raises(
        psycopg.errors.CheckViolation, match="quotes_nights_are_complete"
    ):
        db_conn.execute(
            "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
            "ask_price_total, min_allowed_total, nights, negotiation_open) "
            "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, 12000, 12000, %s::jsonb, "
            "true)",
            (hotel_id, room_type_id, incomplete_night),
        )


def test_quotes_nights_override_night_needs_no_computation_detail(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The positive case for the override shape: override_applied=true
    with only date/season_id/ask/min_allowed is accepted — there is
    genuinely no computation to report for an overridden night."""
    hotel_id = _returning_id(
        db_conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )

    db_conn.execute(
        "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
        "ask_price_total, min_allowed_total, nights, negotiation_open) "
        "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, 20000, 10000, "
        "%s::jsonb, true)",
        (hotel_id, room_type_id, _VALID_QUOTE_NIGHTS),
    )
    row = db_conn.execute("SELECT count(*) FROM quotes").fetchone()
    assert row == (1,)


def _seed_bare_hotel(conn: psycopg.Connection[Any]) -> int:
    return _returning_id(
        conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("distance_to_haram_meters", 0, "hotels_distance_to_haram_positive"),
        ("distance_to_haram_meters", -250, "hotels_distance_to_haram_positive"),
        ("star_rating", 0, "hotels_star_rating_valid"),
        ("star_rating", 6, "hotels_star_rating_valid"),
        ("address_text", "   ", "hotels_address_text_not_blank"),
        ("address_text", "", "hotels_address_text_not_blank"),
    ],
)
def test_hotel_detail_columns_reject_invalid_values(
    db_conn: psycopg.Connection[Any], column: str, value: object, constraint: str
) -> None:
    """Migration 0023's detail columns. A blank address is rejected as well
    as an empty one: `length(btrim(...)) > 0` is what stops a row of spaces
    being stored as if it were a real address."""
    hotel_id = _seed_bare_hotel(db_conn)

    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        db_conn.execute(
            sql.SQL("UPDATE hotels SET {} = %s WHERE id = %s").format(
                sql.Identifier(column)
            ),
            (value, hotel_id),
        )


def test_hotel_detail_columns_accept_null(db_conn: psycopg.Connection[Any]) -> None:
    """Each CHECK spells out `IS NULL OR ...` deliberately: the columns are
    nullable because migrations are forward-only and there is no honest
    default for a real hotel's distance from the Haram. This pins that
    NULL really is accepted, rather than leaving it to a bare comparison
    that would admit NULL by accident."""
    hotel_id = _seed_bare_hotel(db_conn)

    db_conn.execute(
        "UPDATE hotels SET distance_to_haram_meters = NULL, star_rating = NULL, "
        "address_text = NULL WHERE id = %s",
        (hotel_id,),
    )
    row = db_conn.execute(
        "SELECT distance_to_haram_meters, star_rating, address_text FROM hotels "
        "WHERE id = %s",
        (hotel_id,),
    ).fetchone()
    assert row == (None, None, None)


def test_hotel_is_active_defaults_to_true(db_conn: psycopg.Connection[Any]) -> None:
    """Retiring a hotel has to be a flag: migration 0014 grants DELETE on
    hotels to no role but service_role, and existing rows predate 0023."""
    hotel_id = _seed_bare_hotel(db_conn)

    row = db_conn.execute(
        "SELECT is_active FROM hotels WHERE id = %s", (hotel_id,)
    ).fetchone()
    assert row == (True,)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("capacity_adults", 0, "room_types_capacity_adults_valid"),
        ("capacity_adults", 21, "room_types_capacity_adults_valid"),
        ("size_sqm", 0, "room_types_size_sqm_positive"),
        ("size_sqm", -12, "room_types_size_sqm_positive"),
        ("bed_configuration", "king", "room_types_bed_configuration_valid"),
        ("bed_configuration", "", "room_types_bed_configuration_valid"),
    ],
)
def test_room_type_detail_columns_reject_invalid_values(
    db_conn: psycopg.Connection[Any], column: str, value: object, constraint: str
) -> None:
    room_type_id = _returning_id(
        db_conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (_seed_bare_hotel(db_conn),),
    )

    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        db_conn.execute(
            sql.SQL("UPDATE room_types SET {} = %s WHERE id = %s").format(
                sql.Identifier(column)
            ),
            (value, room_type_id),
        )


def test_hotel_amenities_reject_an_unknown_amenity(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The closed list is the whole point of storing amenities as rows: it
    is what stops the agent describing a facility the hotel does not have.
    Widening it is a one-line forward migration, never an ad-hoc insert."""
    hotel_id = _seed_bare_hotel(db_conn)

    with pytest.raises(
        psycopg.errors.CheckViolation, match="hotel_amenities_known_amenity"
    ):
        db_conn.execute(
            "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, 'helipad')",
            (hotel_id,),
        )


def test_hotel_amenities_reject_a_duplicate(db_conn: psycopg.Connection[Any]) -> None:
    """(hotel_id, amenity) is the primary key, so "has wifi" cannot be
    recorded twice and a removal never has to delete more than one row."""
    hotel_id = _seed_bare_hotel(db_conn)
    db_conn.execute(
        "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, 'wifi')",
        (hotel_id,),
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute(
            "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, 'wifi')",
            (hotel_id,),
        )
