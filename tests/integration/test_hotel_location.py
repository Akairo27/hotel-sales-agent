"""Verifies migration 0028's hotel location columns against a real Postgres
instance: city, zone, district_name and weekend_days on hotels.

The database is the source of truth (CLAUDE.md rule 3), so every bound the
admin form also checks is proven here to hold in SQL. Authorization needs no
new policy (migration 0014's table-level grants and admin-only write
policies already cover a new column); the tests below pin that, and pin that
the two backend roles cannot see the new columns.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.usefixtures("db_conn")

MAX_DISTRICT_NAME_LENGTH = 60

_ZONES_BY_CITY = {
    "makkah": ("makkah_central", "makkah_outside"),
    "madinah": (
        "madinah_central",
        "madinah_north",
        "madinah_west",
        "madinah_south",
        "madinah_outside",
    ),
}
_VALID_CITY_ZONE_PAIRS = [
    (city, zone) for city, zones in _ZONES_BY_CITY.items() for zone in zones
]
_LOCATION_COLUMNS = ("city", "weekend_days", "zone", "district_name")
_COLUMNS_BEFORE_THIS_MIGRATION = (
    "id",
    "hotel_name",
    "created_at",
    "distance_to_haram_meters",
    "star_rating",
    "address_text",
    "check_in_time",
    "check_out_time",
    "is_active",
)


def _seed_hotel(conn: psycopg.Connection[Any], **columns: Any) -> int:
    """Inserts a hotel with the given column values (weekend_days is cast to
    smallint[] so an empty or NULL-holding list binds without guessing)."""
    names = ["hotel_name", *columns]
    placeholders = [
        sql.SQL("%s::smallint[]") if name == "weekend_days" else sql.SQL("%s")
        for name in names
    ]
    row = conn.execute(
        sql.SQL("INSERT INTO hotels ({}) VALUES ({}) RETURNING id").format(
            sql.SQL(", ").join(sql.Identifier(name) for name in names),
            sql.SQL(", ").join(placeholders),
        ),
        ["Test Hotel", *columns.values()],
    ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_user(conn: psycopg.Connection[Any], *, role: str) -> str:
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role) VALUES (%s, 'Test User', %s)",
        (user_id, role),
    )
    return user_id


def _read_location(conn: psycopg.Connection[Any], hotel_id: int) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT city, zone, district_name, weekend_days FROM hotels WHERE id = %s",
        (hotel_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_a_hotel_with_only_a_name_gets_the_defaults(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Existing rows predate these columns, so this is also what they look
    like after the migration: nothing recorded, except the owner's default
    weekend of Friday and Saturday (ISO weekdays 5 and 6)."""
    hotel_id = _seed_hotel(db_conn)

    assert _read_location(db_conn, hotel_id) == (None, None, None, [5, 6])


@pytest.mark.parametrize("city", ["makkah", "madinah", None])
def test_city_accepts_the_closed_list_and_null(
    db_conn: psycopg.Connection[Any], city: str | None
) -> None:
    hotel_id = _seed_hotel(db_conn, city=city)

    assert _read_location(db_conn, hotel_id)[0] == city


@pytest.mark.parametrize("city", ["jeddah", "", "Makkah", " makkah", "makkah "])
def test_city_rejects_values_outside_the_closed_list(
    db_conn: psycopg.Connection[Any], city: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="hotels_city_valid"):
        _seed_hotel(db_conn, city=city)


@pytest.mark.parametrize(("city", "zone"), _VALID_CITY_ZONE_PAIRS)
def test_every_zone_is_accepted_with_its_own_city(
    db_conn: psycopg.Connection[Any], city: str, zone: str
) -> None:
    hotel_id = _seed_hotel(db_conn, city=city, zone=zone)

    assert _read_location(db_conn, hotel_id)[:2] == (city, zone)


def test_the_zone_list_in_the_database_is_exactly_the_seven_approved_zones(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The owner approved seven zones (ARCHITECTURE.md section 4). The list
    is read back from the live constraint, so a zone added to the migration
    without a decision, or dropped from it, fails here rather than passing
    unnoticed against a test-local copy."""
    row = db_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'public.hotels'::regclass AND conname = 'hotels_zone_valid'"
    ).fetchone()
    assert row is not None

    zones_in_constraint = set(re.findall(r"'([a-z_]+)'", str(row[0])))
    assert zones_in_constraint == {zone for _, zone in _VALID_CITY_ZONE_PAIRS}
    assert len(_VALID_CITY_ZONE_PAIRS) == 7


@pytest.mark.parametrize(
    ("city", "zone"),
    [
        ("makkah", "madinah_central"),
        ("makkah", "madinah_outside"),
        ("madinah", "makkah_central"),
        ("madinah", "makkah_outside"),
    ],
)
def test_a_zone_of_the_other_city_is_rejected(
    db_conn: psycopg.Connection[Any], city: str, zone: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="hotels_zone_matches_city"):
        _seed_hotel(db_conn, city=city, zone=zone)


@pytest.mark.parametrize("zone", ["makkah_central", "madinah_north"])
def test_a_zone_without_a_city_is_rejected(
    db_conn: psycopg.Connection[Any], zone: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="hotels_zone_matches_city"):
        _seed_hotel(db_conn, zone=zone)


@pytest.mark.parametrize(
    ("city", "zone"),
    [
        ("makkah", "makkah_north"),
        ("madinah", "madinah_east"),
        ("makkah", "makkah_"),
    ],
)
def test_an_unknown_zone_of_a_valid_city_is_rejected(
    db_conn: psycopg.Connection[Any], city: str, zone: str
) -> None:
    """The zone here carries the right city prefix, so only the closed list
    can be what rejects it."""
    with pytest.raises(psycopg.errors.CheckViolation, match="hotels_zone_valid"):
        _seed_hotel(db_conn, city=city, zone=zone)


def test_changing_the_city_away_from_the_zone_is_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The tie between zone and city holds on UPDATE too, not only on the
    INSERT that first set them."""
    hotel_id = _seed_hotel(db_conn, city="makkah", zone="makkah_central")

    with pytest.raises(psycopg.errors.CheckViolation, match="hotels_zone_matches_city"):
        db_conn.execute("UPDATE hotels SET city = 'madinah' WHERE id = %s", (hotel_id,))


@pytest.mark.parametrize(
    "weekend_days",
    [[5, 6], [6, 7], [1], [1, 2, 3, 4, 5, 6, 7], []],
)
def test_weekend_days_accepts_iso_weekdays_including_none(
    db_conn: psycopg.Connection[Any], weekend_days: list[int]
) -> None:
    hotel_id = _seed_hotel(db_conn, weekend_days=weekend_days)

    assert _read_location(db_conn, hotel_id)[3] == weekend_days


@pytest.mark.parametrize(
    "weekend_days",
    [
        [0],
        [8],
        [-1],
        [5, 8],
        [None],
        [5, None],
        [[5], [6]],
        [1, 2, 3, 4, 5, 6, 7, 1],
    ],
)
def test_weekend_days_rejects_values_outside_the_iso_weekdays(
    db_conn: psycopg.Connection[Any], weekend_days: list[Any]
) -> None:
    """Out-of-range values, a NULL element, a two-dimensional array and more
    than seven entries are all refused by the one CHECK."""
    with pytest.raises(
        psycopg.errors.CheckViolation, match="hotels_weekend_days_valid"
    ):
        _seed_hotel(db_conn, weekend_days=weekend_days)


def test_weekend_days_cannot_be_null(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id = _seed_hotel(db_conn)

    with pytest.raises(psycopg.errors.NotNullViolation):
        db_conn.execute(
            "UPDATE hotels SET weekend_days = NULL WHERE id = %s", (hotel_id,)
        )


@pytest.mark.parametrize(
    "district_name",
    ["العزيزية", "x", "م" * MAX_DISTRICT_NAME_LENGTH, None],
)
def test_district_name_accepts_a_short_name_and_null(
    db_conn: psycopg.Connection[Any], district_name: str | None
) -> None:
    hotel_id = _seed_hotel(db_conn, district_name=district_name)

    assert _read_location(db_conn, hotel_id)[2] == district_name


@pytest.mark.parametrize(
    "district_name",
    [
        "",
        "   ",
        "\t",
        "\n",
        "\r\n",
        " \t \n ",
        "م" * (MAX_DISTRICT_NAME_LENGTH + 1),
    ],
)
def test_district_name_rejects_blank_and_over_long_values(
    db_conn: psycopg.Connection[Any], district_name: str
) -> None:
    with pytest.raises(
        psycopg.errors.CheckViolation, match="hotels_district_name_valid"
    ):
        _seed_hotel(db_conn, district_name=district_name)


def test_the_migration_is_additive(db_conn: psycopg.Connection[Any]) -> None:
    """Every column the table had before this migration is still there, and
    the four new ones are."""
    rows = db_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'hotels'"
    ).fetchall()
    columns = {row[0] for row in rows}

    assert set(_COLUMNS_BEFORE_THIS_MIGRATION) <= columns
    assert set(_LOCATION_COLUMNS) <= columns


def test_the_new_columns_and_the_distance_column_are_documented(
    db_conn: psycopg.Connection[Any],
) -> None:
    def comment(column: str) -> str:
        row = db_conn.execute(
            "SELECT col_description('public.hotels'::regclass, a.attnum) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = 'public.hotels'::regclass AND a.attname = %s",
            (column,),
        ).fetchone()
        assert row is not None
        return str(row[0] or "")

    for column in _LOCATION_COLUMNS:
        assert comment(column), f"hotels.{column} has no COMMENT"

    distance = comment("distance_to_haram_meters")
    assert "Masjid al-Haram" in distance
    assert "Al-Masjid an-Nabawi" in distance


def test_an_admin_can_insert_and_update_the_location_columns(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """No new policy: migration 0014's admin-only write policies and its
    table-level grants already cover a new column."""
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    inserted = db_conn.execute(
        "INSERT INTO hotels (hotel_name, city, zone, district_name, weekend_days) "
        "VALUES ('Admin Hotel', 'madinah', 'madinah_north', 'الحي', '{4,5}') "
        "RETURNING id"
    ).fetchone()
    assert inserted is not None
    hotel_id = int(inserted[0])

    cursor = db_conn.execute(
        "UPDATE hotels SET city = 'makkah', zone = 'makkah_outside', "
        "weekend_days = '{5,6}' WHERE id = %s",
        (hotel_id,),
    )
    assert cursor.rowcount == 1
    assert _read_location(db_conn, hotel_id) == (
        "makkah",
        "makkah_outside",
        "الحي",
        [5, 6],
    )


def test_sales_can_read_but_not_change_the_location_columns(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn, city="makkah", zone="makkah_central")
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    assert _read_location(db_conn, hotel_id)[:2] == ("makkah", "makkah_central")
    cursor = db_conn.execute(
        "UPDATE hotels SET city = 'madinah', zone = 'madinah_central' WHERE id = %s",
        (hotel_id,),
    )
    assert cursor.rowcount == 0
    assert _read_location(db_conn, hotel_id)[:2] == ("makkah", "makkah_central")


@pytest.mark.parametrize("column", _LOCATION_COLUMNS)
def test_hotel_worker_cannot_read_the_new_columns(
    db_conn: psycopg.Connection[Any], column: str
) -> None:
    """hotel_worker has no privilege on hotels at all (migration 0027),
    unchanged since -- nothing added to that table can reach it."""
    row = db_conn.execute(
        "SELECT has_column_privilege('hotel_worker', 'public.hotels', %s, 'SELECT')",
        (column,),
    ).fetchone()
    assert row == (False,)


@pytest.mark.parametrize("column", _LOCATION_COLUMNS)
def test_hotel_agent_can_read_the_new_columns(
    db_conn: psycopg.Connection[Any], column: str
) -> None:
    """hotel_agent gained full-table SELECT on hotels in migration 0030
    (search_hotels needs city/zone at minimum) -- these columns are no
    exception, matching migration 0027's own "full grant, boundary
    enforced in code" pattern already established for this role's other
    reference-table reads."""
    row = db_conn.execute(
        "SELECT has_column_privilege('hotel_agent', 'public.hotels', %s, 'SELECT')",
        (column,),
    ).fetchone()
    assert row == (True,)
