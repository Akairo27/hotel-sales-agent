"""services/worker/runner.py against a real database — the scheduled pass
must release an expired hold exactly once, however many times it runs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from services.inventory.operations import create_hold
from services.worker import runner
from tests.integration._seed import seed_allotment_nights, seed_hotel_and_room_type

pytestmark = pytest.mark.usefixtures("db_conn")

# Far enough in the past that the real clock main() reads is after the
# hold's expiry, whatever day this suite runs on.
_HELD_AT = datetime(2026, 6, 1, tzinfo=UTC)
_CHECK_IN = date(2026, 6, 5)
_CHECK_OUT = date(2026, 6, 6)


def test_main_releases_an_expired_hold_exactly_once(
    db_conn: psycopg.Connection[Any],
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, _CHECK_IN, nights=1, total_rooms=2
    )
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _HELD_AT,
        idempotency_key="worker-runner",
    )
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    caplog.set_level(logging.INFO, logger=runner.logger.name)

    first_exit = runner.main()
    second_exit = runner.main()

    assert (first_exit, second_exit) == (runner.EXIT_OK, runner.EXIT_OK)
    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == runner.logger.name
    ]
    assert [event["released_hold_ids"] for event in events] == [[hold_id], []]
    held = db_conn.execute("SELECT held FROM room_night_inventory").fetchone()
    assert held == (0,)
