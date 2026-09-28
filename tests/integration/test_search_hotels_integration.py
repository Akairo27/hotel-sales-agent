"""Integration tests for dispatch_search_hotels against a real database —
name matching (including Arabic normalization), the closed-list and
star-rating filters, the completeness/is_active gate, and the turn-scoped
resolved_stays guard dispatch_tool builds from a real search result.

Unit coverage for argument validation and the cost-containment whitelist
lives in tests/unit/test_llm_dispatch.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from services.agent.llm.dispatch import (
    HOTEL_RESULT_KEYS,
    MAX_SEARCH_HOTELS_RESULTS,
    ROOM_TYPE_RESULT_KEYS,
    SEARCH_HOTELS_RESULT_KEYS,
    dispatch_search_hotels,
    dispatch_tool,
)
from services.agent.llm.errors import InvalidToolArgumentsError
from tests.integration._seed import seed_hotel, seed_room_type

pytestmark = pytest.mark.usefixtures("db_conn")

_NOW = datetime(2026, 9, 28, tzinfo=UTC)

_COMPLETE_PROFILE: dict[str, Any] = {
    "city": "makkah",
    "zone": "makkah_central",
    "star_rating": 4,
    "distance_to_haram_meters": 350,
    "address_text": "شارع إبراهيم الخليل",
}


def test_returns_a_hotel_matching_by_exact_name(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id = seed_hotel(db_conn, hotel_name="فندق الاختبار", **_COMPLETE_PROFILE)
    room_type_id = seed_room_type(
        db_conn,
        hotel_id,
        room_type_name="جناح ملكي",
        capacity_adults=4,
        bed_configuration="double",
    )

    result = dispatch_search_hotels(db_conn, {"hotel_name": "فندق الاختبار"})

    assert result.keys() == SEARCH_HOTELS_RESULT_KEYS
    assert result["truncated"] is False
    assert len(result["hotels"]) == 1
    hotel = result["hotels"][0]
    assert hotel.keys() == HOTEL_RESULT_KEYS
    assert hotel["hotel_id"] == hotel_id
    assert hotel["city"] == "makkah"
    assert len(hotel["room_types"]) == 1
    room_type = hotel["room_types"][0]
    assert room_type.keys() == ROOM_TYPE_RESULT_KEYS
    assert room_type == {
        "room_type_id": room_type_id,
        "room_type_name": "جناح ملكي",
        "capacity_adults": 4,
        "bed_configuration": "double",
    }


def test_returns_every_hotel_with_a_duplicate_name_never_just_one(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The direct regression test for the incident this tool exists to
    prevent: two real hotels sharing a name must both come back, so the
    model has to ask rather than silently pick one."""
    first_id = seed_hotel(db_conn, hotel_name="فندق الاختبار", **_COMPLETE_PROFILE)
    second_id = seed_hotel(db_conn, hotel_name="فندق الاختبار", **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"hotel_name": "فندق الاختبار"})

    returned_ids = {hotel["hotel_id"] for hotel in result["hotels"]}
    assert returned_ids == {first_id, second_id}


@pytest.mark.parametrize(
    ("stored_name", "search_term"),
    [
        ("فندق الاختبار", "فندق الإختبار"),  # bare alef stored, hamza-below searched
        ("فندق إلاختبار", "فندق الاختبار"),  # hamza-below stored, bare alef searched
        ("فندق آلاختبار", "فندق الاختبار"),  # madda-above stored, bare alef searched
    ],
)
def test_matches_alef_hamza_variants_either_direction(
    db_conn: psycopg.Connection[Any], stored_name: str, search_term: str
) -> None:
    seed_hotel(db_conn, hotel_name=stored_name, **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"hotel_name": search_term})

    assert len(result["hotels"]) == 1


def test_matches_regardless_of_tashkeel_diacritics(
    db_conn: psycopg.Connection[Any],
) -> None:
    seed_hotel(db_conn, hotel_name="فُندُق الاختبار", **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"hotel_name": "فندق الاختبار"})

    assert len(result["hotels"]) == 1


def test_matches_taa_marbuta_against_haa(db_conn: psycopg.Connection[Any]) -> None:
    seed_hotel(db_conn, hotel_name="فندق المدينه", **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"hotel_name": "فندق المدينة"})

    assert len(result["hotels"]) == 1


def test_matches_alef_maksura_against_yaa(db_conn: psycopg.Connection[Any]) -> None:
    seed_hotel(db_conn, hotel_name="فندق المصطفى", **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"hotel_name": "فندق المصطفي"})

    assert len(result["hotels"]) == 1


def test_filters_by_city_alone(db_conn: psycopg.Connection[Any]) -> None:
    makkah_id = seed_hotel(db_conn, hotel_name="Makkah Hotel", **_COMPLETE_PROFILE)
    seed_hotel(
        db_conn,
        hotel_name="Madinah Hotel",
        **{**_COMPLETE_PROFILE, "city": "madinah", "zone": "madinah_central"},
    )

    result = dispatch_search_hotels(db_conn, {"city": "makkah"})

    assert {hotel["hotel_id"] for hotel in result["hotels"]} == {makkah_id}


def test_filters_by_star_rating_range(db_conn: psycopg.Connection[Any]) -> None:
    economy_id = seed_hotel(
        db_conn, hotel_name="Economy", **{**_COMPLETE_PROFILE, "star_rating": 2}
    )
    seed_hotel(db_conn, hotel_name="Luxury", **{**_COMPLETE_PROFILE, "star_rating": 5})

    result = dispatch_search_hotels(db_conn, {"max_star_rating": 3})

    assert {hotel["hotel_id"] for hotel in result["hotels"]} == {economy_id}


def test_excludes_an_inactive_hotel(db_conn: psycopg.Connection[Any]) -> None:
    seed_hotel(db_conn, hotel_name="فندق مغلق", is_active=False, **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"hotel_name": "فندق مغلق"})

    assert result["hotels"] == []


def test_excludes_a_hotel_missing_city_or_zone(
    db_conn: psycopg.Connection[Any],
) -> None:
    """city/zone joined the completeness requirement alongside distance/
    star/address (2026-09-28) -- a hotel search_hotels itself cannot
    honestly place must not be offered to a customer either."""
    seed_hotel(
        db_conn,
        hotel_name="ملف غير مكتمل",
        **{**_COMPLETE_PROFILE, "city": None, "zone": None},
    )

    result = dispatch_search_hotels(db_conn, {"hotel_name": "ملف غير مكتمل"})

    assert result["hotels"] == []


def test_excludes_a_hotel_missing_star_rating(db_conn: psycopg.Connection[Any]) -> None:
    seed_hotel(
        db_conn,
        hotel_name="بلا تصنيف",
        **{**_COMPLETE_PROFILE, "star_rating": None},
    )

    result = dispatch_search_hotels(db_conn, {"hotel_name": "بلا تصنيف"})

    assert result["hotels"] == []


def test_truncates_at_the_named_cap_and_reports_it(
    db_conn: psycopg.Connection[Any],
) -> None:
    for i in range(MAX_SEARCH_HOTELS_RESULTS + 1):
        seed_hotel(db_conn, hotel_name=f"Hotel {i}", **_COMPLETE_PROFILE)

    result = dispatch_search_hotels(db_conn, {"city": "makkah"})

    assert len(result["hotels"]) == MAX_SEARCH_HOTELS_RESULTS
    assert result["truncated"] is True


def test_dispatch_tool_resolves_a_stay_then_allows_check_availability(
    db_conn: psycopg.Connection[Any],
) -> None:
    """End-to-end proof of the guard: a pair search_hotels actually
    returned through dispatch_tool must then be accepted by
    check_availability in the same (simulated) turn."""
    hotel_id = seed_hotel(db_conn, hotel_name="فندق الاختبار", **_COMPLETE_PROFILE)
    room_type_id = seed_room_type(db_conn, hotel_id, room_type_name="جناح ملكي")
    resolved_stays: set[tuple[int, int]] = set()

    dispatch_tool(
        db_conn,
        "search_hotels",
        {"hotel_name": "فندق الاختبار"},
        now=_NOW,
        customer_phone=None,
        conversation_id=None,
        resolved_stays=resolved_stays,
    )
    assert (hotel_id, room_type_id) in resolved_stays

    result = dispatch_tool(
        db_conn,
        "check_availability",
        {
            "hotel_id": hotel_id,
            "room_type_id": room_type_id,
            "check_in": "2026-10-01",
            "check_out": "2026-10-02",
            "rooms": 1,
        },
        now=_NOW,
        customer_phone=None,
        conversation_id=None,
        resolved_stays=resolved_stays,
    )
    assert (
        result["available"] is False
    )  # no allotment seeded -- just proves it wasn't rejected


def test_dispatch_tool_still_rejects_a_pair_from_a_different_hotel(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id = seed_hotel(db_conn, hotel_name="فندق الاختبار", **_COMPLETE_PROFILE)
    seed_room_type(db_conn, hotel_id, room_type_name="جناح ملكي")
    other_hotel_id, other_room_type_id = hotel_id + 1, 999
    resolved_stays: set[tuple[int, int]] = set()

    dispatch_tool(
        db_conn,
        "search_hotels",
        {"hotel_name": "فندق الاختبار"},
        now=_NOW,
        customer_phone=None,
        conversation_id=None,
        resolved_stays=resolved_stays,
    )

    with pytest.raises(InvalidToolArgumentsError, match="was not returned"):
        dispatch_tool(
            db_conn,
            "check_availability",
            {
                "hotel_id": other_hotel_id,
                "room_type_id": other_room_type_id,
                "check_in": "2026-10-01",
                "check_out": "2026-10-02",
                "rooms": 1,
            },
            now=_NOW,
            customer_phone=None,
            conversation_id=None,
            resolved_stays=resolved_stays,
        )
