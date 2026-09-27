"""Generates tests/hijri_lookup_fixtures.json — pinned Gregorian -> Hijri
ground truth for admin/lib/hijriLookup.conformance.test.ts, computed through
lib/hijri.py (the one Hijri conversion module, CLAUDE.md rule 6) rather than
hand-typed. Same role as tests/generate_season_conformance_fixtures.py: the
one thing standing between "the admin-side lookup looks right" and "the
admin-side lookup agrees with the real Umm al-Qura calendar".

Run manually and commit the result whenever a case is added:

    uv run python -m tests.generate_hijri_lookup_fixtures
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from lib.hijri import to_hijri

OUTPUT_PATH = Path(__file__).resolve().parent / "hijri_lookup_fixtures.json"

# A handful of real dates, not exhaustive coverage — the reference table
# admin/lib/hijriLookup.ts reads (hijriCalendarReference.json) is itself
# already generated from and tested against lib/hijri.py directly, so this
# fixture only needs to prove the TypeScript reverse lookup agrees with it
# at a few representative points, not re-verify the calendar itself.
_DATES = [
    date(2026, 3, 20),  # already pinned in tests/unit/test_hijri.py
    date(2026, 9, 28),  # a realistic allotment-entry date, near "today"
    date(2018, 9, 11),  # HIJRI_FIRST_YEAR (1440), month 1, day 1
    date(2018, 9, 10),  # the day before -> last day of the buffer year 1439
    date(2038, 12, 27),  # near HIJRI_LAST_SELECTABLE_YEAR (1460)'s final month
]


def build_fixtures() -> dict[str, object]:
    return {
        "cases": [
            {
                "gregorian": gregorian_date.isoformat(),
                "hijri": asdict(to_hijri(gregorian_date)),
            }
            for gregorian_date in _DATES
        ]
    }


if __name__ == "__main__":
    OUTPUT_PATH.write_text(
        json.dumps(build_fixtures(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
