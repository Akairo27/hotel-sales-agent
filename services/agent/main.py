"""FastAPI entrypoint for agent-service — ARCHITECTURE.md §2, §7.

Health-check plus the WhatsApp Cloud API webhook (services/agent/webhook.py)
— signature verification, idempotent inbound logging, and spend/rate-cap
enforcement. No booking/payment code path here — CLAUDE.md rule 10 requires
asking about before adding anything touching payment/booking confirmation.

Run locally with: uvicorn services.agent.main:app --reload
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.agent.webhook import router as webhook_router

# httpx/httpcore log every request URL at INFO/DEBUG -- this app's own
# webhook and WhatsApp-send calls carry a live API key or access token in
# a header (services/agent/llm/config.py's LLM_API_KEY,
# whatsapp_send.py's access token), the same leak pattern client.py's and
# webhook.py's own comments already guard the *content* of our own log
# lines against. google_genai logs its own SDK internals -- including,
# per client.py's own retry-loop comment, a before_sleep hook that would
# have logged Google's raw response body had client.py not taken over
# retries itself. urllib3 is connection-pool chatter no one reads. Pinned
# to WARNING unconditionally, regardless of LOG_LEVEL, so raising our own
# app's level (e.g. to DEBUG, to chase down a real bug) can never also
# turn these on by accident.
_THIRD_PARTY_LOGGERS_AT_WARNING = ("httpx", "httpcore", "google_genai", "urllib3")


class LoggingConfigurationError(Exception):
    """Raised when LOG_LEVEL is set to something that isn't a recognized
    logging level name."""


def configure_logging() -> None:
    """Configures the root logger once, from LOG_LEVEL — CLAUDE.md §8's
    "structured logging (JSON)" requirement has nothing to reach the
    journal through without this: nothing in this codebase ever called
    logging.basicConfig/dictConfig, so Python's root logger stayed at its
    default level (WARNING), silently dropping every logger.info() call
    in the codebase (webhook.py's message_rate_cap_blocked,
    reply_turn_finished, ...) regardless of what LOG_LEVEL was set to in
    the environment — confirmed missing in production, during
    verification of the PR that added reply_turn_finished.

    stdout, not stderr: journald captures both for a systemd service, but
    stdout is where this app's own structured JSON belongs, distinct from
    an interpreter-level crash. Third-party loggers are pinned to WARNING
    unconditionally — see _THIRD_PARTY_LOGGERS_AT_WARNING above.

    Called from this module's lifespan handler below, not at import time:
    every test that imports `app` (most of tests/integration/
    test_webhook.py) would otherwise mutate process-wide logging state as
    a side effect of nothing more than a plain TestClient(app) — with no
    `with` block, Starlette's TestClient never drives the ASGI lifespan
    protocol, confirmed directly against the installed version, so this
    never fires there. Only a real ASGI server (uvicorn) or a test that
    explicitly enters `with TestClient(app):` triggers it.

    Raises:
        LoggingConfigurationError: LOG_LEVEL is set to something that
            isn't a recognized logging level name. Unset is not an
            error — it defaults to INFO.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name)
    if level is None:
        raise LoggingConfigurationError(
            f"LOG_LEVEL={level_name!r} is not a recognized logging level"
        )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    for name in _THIRD_PARTY_LOGGERS_AT_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app  # unused: configure_logging() takes no arguments
    configure_logging()
    yield


app = FastAPI(title="hotel-sales-agent", lifespan=_lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check. No database round-trip, no external call — this
    only proves the process itself is up."""
    return {"status": "ok"}
