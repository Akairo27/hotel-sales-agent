"""Hotel profile completeness — ARCHITECTURE.md §4.

Mirrors admin/lib/hotelDetails.ts's missingHotelProfileFields exactly (same
three columns, same "any NULL means incomplete" rule) — pinned by
tests/unit/test_hotel_profile_conformance.py so the two cannot drift apart
silently. The admin dashboard uses that TS function to flag an incomplete
profile to the client; this is the other consumer ARCHITECTURE.md §4 calls
out as still missing: "ربط ما يعرضه الوكيل باكتمال الملف مسؤولية المرحلة ٤"
— what the agent (search_alternatives and any other tool that surfaces a
hotel to a customer) is allowed to offer must be gated on this, because an
incomplete profile gives the model no honest basis to describe the hotel.

Pure: no I/O, no clock reads. The columns are nullable by design (migration
0023 ran against existing rows, and there is no honest default for a real
hotel's distance from the Haram) — completeness is a business rule checked
in code, not a NOT NULL constraint.
"""

from __future__ import annotations

REQUIRED_HOTEL_PROFILE_FIELDS = (
    "distance_to_haram_meters",
    "star_rating",
    "address_text",
)


def missing_hotel_profile_fields(
    *,
    distance_to_haram_meters: int | None,
    star_rating: int | None,
    address_text: str | None,
) -> list[str]:
    """Names of the required profile columns that are still NULL.

    Returns an empty list when the profile is complete.
    """
    values: dict[str, int | str | None] = {
        "distance_to_haram_meters": distance_to_haram_meters,
        "star_rating": star_rating,
        "address_text": address_text,
    }
    return [field for field in REQUIRED_HOTEL_PROFILE_FIELDS if values[field] is None]


def is_hotel_profile_complete(
    *,
    distance_to_haram_meters: int | None,
    star_rating: int | None,
    address_text: str | None,
) -> bool:
    """Whether the agent has an honest basis to offer this hotel.

    False means at least one of REQUIRED_HOTEL_PROFILE_FIELDS is NULL —
    callers building an agent-facing hotel query (search_alternatives and
    friends) must filter these out, not just the admin dashboard's banner.
    """
    return not missing_hotel_profile_fields(
        distance_to_haram_meters=distance_to_haram_meters,
        star_rating=star_rating,
        address_text=address_text,
    )
