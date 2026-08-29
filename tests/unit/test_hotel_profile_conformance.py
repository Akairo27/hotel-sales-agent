"""Pins services/agent/hotel_profile.py's required-field set to
admin/lib/hotelDetails.ts's missingHotelProfileFields, the same way
hotelDetails.conformance.test.ts pins the TS vocabularies to the migration's
CHECK constraints: read the other side's source directly instead of
hand-duplicating a list that can silently drift.

There is no SQL constraint to parse here — the required set is a business
rule ("what the agent needs before it can honestly offer a hotel"), not a
column NOT NULL — so the TS function body itself is the ground truth.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.agent.hotel_profile import REQUIRED_HOTEL_PROFILE_FIELDS

_TS_PATH = Path(__file__).resolve().parents[2] / "admin" / "lib" / "hotelDetails.ts"

_FUNCTION_NAME = "missingHotelProfileFields"
_NULL_CHECK = re.compile(r"hotel\.([a-z_]+) === null")


def _ts_required_fields() -> list[str]:
    source = _TS_PATH.read_text(encoding="utf-8")
    start = source.index(f"export function {_FUNCTION_NAME}")
    # The function's own parameter type also closes with "\n}", as
    # "}): string[] {" — only the real end of the function body is a "}"
    # alone on its own line, i.e. followed by a newline rather than "):".
    end = source.index("\n}\n", start)
    body = source[start:end]
    return _NULL_CHECK.findall(body)


def test_required_fields_match_admin_missing_hotel_profile_fields() -> None:
    assert list(REQUIRED_HOTEL_PROFILE_FIELDS) == _ts_required_fields()
