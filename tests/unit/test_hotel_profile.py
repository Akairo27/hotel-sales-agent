"""services/agent/hotel_profile.py — pure completeness-gate logic."""

from __future__ import annotations

from typing import TypedDict

from services.agent.hotel_profile import (
    is_hotel_profile_complete,
    missing_hotel_profile_fields,
)


class _ProfileFields(TypedDict):
    distance_to_haram_meters: int | None
    star_rating: int | None
    address_text: str | None


_COMPLETE: _ProfileFields = {
    "distance_to_haram_meters": 350,
    "star_rating": 4,
    "address_text": "العزيزية، مكة المكرمة",
}


def test_complete_profile_has_no_missing_fields() -> None:
    assert missing_hotel_profile_fields(**_COMPLETE) == []


def test_complete_profile_is_complete() -> None:
    assert is_hotel_profile_complete(**_COMPLETE) is True


def test_missing_distance_is_reported() -> None:
    fields: _ProfileFields = {**_COMPLETE, "distance_to_haram_meters": None}
    assert missing_hotel_profile_fields(**fields) == ["distance_to_haram_meters"]
    assert is_hotel_profile_complete(**fields) is False


def test_missing_star_rating_is_reported() -> None:
    fields: _ProfileFields = {**_COMPLETE, "star_rating": None}
    assert missing_hotel_profile_fields(**fields) == ["star_rating"]
    assert is_hotel_profile_complete(**fields) is False


def test_missing_address_is_reported() -> None:
    fields: _ProfileFields = {**_COMPLETE, "address_text": None}
    assert missing_hotel_profile_fields(**fields) == ["address_text"]
    assert is_hotel_profile_complete(**fields) is False


def test_all_fields_missing_reports_all_in_declared_order() -> None:
    fields: _ProfileFields = {
        "distance_to_haram_meters": None,
        "star_rating": None,
        "address_text": None,
    }
    assert missing_hotel_profile_fields(**fields) == [
        "distance_to_haram_meters",
        "star_rating",
        "address_text",
    ]
    assert is_hotel_profile_complete(**fields) is False


def test_check_in_and_check_out_times_are_not_required() -> None:
    # ARCHITECTURE.md §4 / admin/lib/hotelDetails.ts's missingHotelProfileFields
    # only checks distance, star rating, and address — check-in/check-out
    # times are nullable and never block completeness. This test exists so a
    # future "widen the gate" change is a deliberate decision, not a
    # copy-paste accident that silently starts blocking hotels that were
    # previously fine.
    assert is_hotel_profile_complete(**_COMPLETE) is True
