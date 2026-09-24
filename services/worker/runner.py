"""One pass of the worker — the scheduled jobs ARCHITECTURE.md §6 assigns to it.

Run as `python -m services.worker`. One invocation is one pass, then exit:
cadence and overlap protection belong to the scheduler (the systemd timer in
ops/hotel-worker.timer), and a crash cannot leave a half-alive loop behind.
release_expired_holds is safe to run concurrently with itself or with a
confirm (each hold is claimed by a conditional UPDATE), so a second scheduler
that overlaps this one can double the work but never a release.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

import psycopg

from services.worker.config import DATABASE_CONNECT_TIMEOUT_SECONDS
from services.worker.errors import WorkerConfigurationError
from services.worker.hold_expiry import release_expired_holds

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1

_MILLISECONDS_PER_SECOND = 1000


def _connect() -> psycopg.Connection[Any]:
    """Opens the worker's database connection from DATABASE_URL.

    Raises:
        WorkerConfigurationError: DATABASE_URL is unset or empty.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise WorkerConfigurationError("DATABASE_URL is not set")
    return psycopg.connect(
        dsn, autocommit=True, connect_timeout=DATABASE_CONNECT_TIMEOUT_SECONDS
    )


def main() -> int:
    """Runs one worker pass and returns the process exit code.

    Every pass logs one structured line: what it did and how long it took,
    so a timer that stopped firing shows up as a gap in the journal rather
    than going unnoticed. Database failures are logged by type and SQLSTATE
    only — a driver message can carry connection details.

    Raises:
        WorkerConfigurationError: DATABASE_URL is unset or empty.
    """
    logging.basicConfig(stream=sys.stdout, format="%(message)s", level=logging.INFO)
    started = time.monotonic()
    try:
        with _connect() as conn:
            released = release_expired_holds(conn, datetime.now(UTC))
    except psycopg.Error as exc:
        logger.error(
            json.dumps(
                {
                    "event": "worker_run_failed",
                    "error_type": type(exc).__name__,
                    "sqlstate": exc.sqlstate,
                }
            )
        )
        return EXIT_FAILED

    logger.info(
        json.dumps(
            {
                "event": "worker_run_finished",
                "released_hold_ids": released,
                "released_count": len(released),
                "duration_ms": round(
                    (time.monotonic() - started) * _MILLISECONDS_PER_SECOND
                ),
            }
        )
    )
    return EXIT_OK
