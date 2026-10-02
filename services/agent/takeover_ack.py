"""The takeover acknowledgement: the one message the bot sends during a
takeover (owner decisions D5 and D6, 2026-10-02; ARCHITECTURE.md §7).

Right after a staff member wins a takeover, the dashboard's server action
calls POST /internal/takeovers/{id}/acknowledge on this service. That sends
fixed_texts.TAKEN_OVER to the customer, in their language, through the
output guard, at most once per takeover and only while it is active.

The endpoint is internal: the service listens on 127.0.0.1 only and nginx
proxies nothing but /webhook/ to it (ops/nginx-hotel-admin.conf). It also
requires AGENT_INTERNAL_TOKEN, shared with the dashboard, as a Bearer token.
It takes nothing but a takeover id -- no request content ever becomes
message text -- and the atomic claim on the takeover row is what makes a
double call, or a call for an ended takeover, send nothing.

Outside WhatsApp's 24-hour customer service window the notice is not sent
at all and recorded as failed: Meta refuses a free-form message there, and
may report that only later through a status webhook this system does not
process (ARCHITECTURE.md §7's residuals), so trying would only record a
false "sent". The dashboard tells staff to call the customer instead.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services.agent import webhook
from services.agent.fixed_texts import TAKEN_OVER

logger = logging.getLogger(__name__)

router = APIRouter()

# `openssl rand -hex 32` gives 64 characters; anything under 32 is refused
# at startup as too guessable.
MIN_TOKEN_LENGTH = 32
_BEARER_PREFIX = "Bearer "

# WhatsApp's customer service window: a free-form message is accepted only
# within 24 hours of the customer's last message.
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)

# The log label for the send (webhook.send_fixed_text_or_log_failure).
_PURPOSE = "takeover_acknowledgement"

_RECORD_SENT = "UPDATE conversation_takeovers SET ack_sent_at = now() WHERE id = %s"
_RECORD_FAILED = "UPDATE conversation_takeovers SET ack_failed_at = now() WHERE id = %s"

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_OUTSIDE_WINDOW = "outside_window"
STATUS_ALREADY_CLAIMED = "already_claimed"
STATUS_TAKEOVER_ENDED = "takeover_ended"
STATUS_NOT_FOUND = "not_found"
STATUS_UNAVAILABLE = "unavailable"

_HTTP_STATUS = {
    STATUS_SENT: 200,
    STATUS_OUTSIDE_WINDOW: 200,
    STATUS_ALREADY_CLAIMED: 200,
    STATUS_TAKEOVER_ENDED: 200,
    STATUS_NOT_FOUND: 404,
    STATUS_FAILED: 502,
    STATUS_UNAVAILABLE: 503,
}


class InternalApiConfigurationError(Exception):
    """Raised when AGENT_INTERNAL_TOKEN is unset or shorter than
    MIN_TOKEN_LENGTH. Never carries the value."""


@dataclass(frozen=True)
class InternalApiSettings:
    token: str


def load_internal_api_settings(
    env: Mapping[str, str] | None = None,
) -> InternalApiSettings:
    """Reads AGENT_INTERNAL_TOKEN.

    Raises:
        InternalApiConfigurationError: it is unset, or shorter than
            MIN_TOKEN_LENGTH characters.
    """
    active_env = os.environ if env is None else env
    token = active_env.get("AGENT_INTERNAL_TOKEN", "")
    if not token:
        raise InternalApiConfigurationError("AGENT_INTERNAL_TOKEN is not set")
    if len(token) < MIN_TOKEN_LENGTH:
        raise InternalApiConfigurationError(
            f"AGENT_INTERNAL_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
        )
    return InternalApiSettings(token=token)


def get_internal_api_settings() -> InternalApiSettings:
    """A separate function so tests can monkeypatch this call site, as
    webhook.get_webhook_settings."""
    return load_internal_api_settings()


def _token_problem(authorization: str | None, expected_token: str) -> str | None:
    """Why the Authorization header fails -- "missing", "malformed" (not a
    Bearer token) or "mismatch" -- or None when it carries the token."""
    if authorization is None:
        return "missing"
    if not authorization.startswith(_BEARER_PREFIX):
        return "malformed"
    provided = authorization.removeprefix(_BEARER_PREFIX).encode("utf-8")
    if not hmac.compare_digest(provided, expected_token.encode("utf-8")):
        return "mismatch"
    return None


@dataclass(frozen=True)
class ClaimedAcknowledgement:
    conversation_id: int
    customer_phone: str


def claim_acknowledgement(
    conn: psycopg.Connection[Any], *, takeover_id: int
) -> ClaimedAcknowledgement | None:
    """Claims the acknowledgement of an active takeover nobody has claimed
    yet; returns where to send it, or None when there is nothing to claim.
    A single conditional UPDATE, so of two concurrent calls only one wins.

    Raises:
        psycopg.Error: the write failed.
    """
    row = conn.execute(
        "UPDATE conversation_takeovers AS t SET ack_claimed_at = now() "
        "FROM conversations AS c "
        "WHERE t.id = %s AND c.id = t.conversation_id "
        "AND t.ended_at IS NULL AND t.ack_claimed_at IS NULL "
        "RETURNING t.conversation_id, c.customer_phone",
        (takeover_id,),
    ).fetchone()
    if row is None:
        return None
    return ClaimedAcknowledgement(conversation_id=row[0], customer_phone=row[1])


def unclaimed_status(conn: psycopg.Connection[Any], *, takeover_id: int) -> str:
    """Why claim_acknowledgement found nothing: not_found, takeover_ended
    or already_claimed.

    Raises:
        psycopg.Error: the read failed.
    """
    row = conn.execute(
        "SELECT ended_at IS NOT NULL FROM conversation_takeovers WHERE id = %s",
        (takeover_id,),
    ).fetchone()
    if row is None:
        return STATUS_NOT_FOUND
    ended: bool = row[0]
    return STATUS_TAKEOVER_ENDED if ended else STATUS_ALREADY_CLAIMED


def within_customer_service_window(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> bool:
    """Whether the customer wrote within CUSTOMER_SERVICE_WINDOW, by the
    database clock that stamped the message.

    Raises:
        psycopg.Error: the read failed.
    """
    row = conn.execute(
        "SELECT EXISTS (SELECT FROM messages WHERE conversation_id = %s "
        "AND direction = 'inbound' AND created_at > now() - %s)",
        (conversation_id, CUSTOMER_SERVICE_WINDOW),
    ).fetchone()
    if row is None:
        raise RuntimeError("SELECT EXISTS returned no row")
    within: bool = row[0]
    return within


def record_outcome_or_log_failure(
    conn: psycopg.Connection[Any], *, takeover_id: int, delivered: bool
) -> None:
    """Stamps ack_sent_at or ack_failed_at on a claimed acknowledgement; a
    failed write is logged at ERROR, never raised -- the send already
    happened or failed either way."""
    statement = _RECORD_SENT if delivered else _RECORD_FAILED
    try:
        conn.execute(statement, (takeover_id,))
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "takeover_acknowledgement_not_recorded",
                    "takeover_id": takeover_id,
                    "delivered": delivered,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )


async def acknowledge(conn: psycopg.Connection[Any], *, takeover_id: int) -> str:
    """Claims, sends and records the acknowledgement of one takeover;
    returns its status.

    Raises:
        psycopg.Error: the claim or a read before the send failed.
    """
    claim = claim_acknowledgement(conn, takeover_id=takeover_id)
    if claim is None:
        return unclaimed_status(conn, takeover_id=takeover_id)
    if not within_customer_service_window(conn, conversation_id=claim.conversation_id):
        record_outcome_or_log_failure(conn, takeover_id=takeover_id, delivered=False)
        return STATUS_OUTSIDE_WINDOW
    delivered = await webhook.send_fixed_text_or_log_failure(
        conn,
        conversation_id=claim.conversation_id,
        customer_phone=claim.customer_phone,
        notice=TAKEN_OVER,
        purpose=_PURPOSE,
    )
    record_outcome_or_log_failure(conn, takeover_id=takeover_id, delivered=delivered)
    return STATUS_SENT if delivered else STATUS_FAILED


@router.post("/internal/takeovers/{takeover_id}/acknowledge")
async def acknowledge_takeover(takeover_id: int, request: Request) -> JSONResponse:
    """Sends the takeover acknowledgement for the dashboard. The response is
    {"status": ...}: sent, outside_window, already_claimed or
    takeover_ended (200), not_found (404), failed (502: the send failed),
    unavailable (503: the database could not be reached; nothing was
    claimed unless the failure came after the claim, so the dashboard may
    offer to try again).

    Raises:
        HTTPException(401): the Bearer token is missing or wrong -- checked
            before any database access, logged with the reason only.
    """
    problem = _token_problem(
        request.headers.get("authorization"), get_internal_api_settings().token
    )
    if problem is not None:
        logger.warning(
            json.dumps({"event": "internal_request_rejected", "problem": problem})
        )
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        with webhook.get_db_connection() as conn:
            status = await acknowledge(conn, takeover_id=takeover_id)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "takeover_acknowledgement_failed",
                    "takeover_id": takeover_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        status = STATUS_UNAVAILABLE
    logger.info(
        json.dumps(
            {
                "event": "takeover_acknowledgement_finished",
                "takeover_id": takeover_id,
                "status": status,
            }
        )
    )
    return JSONResponse({"status": status}, status_code=_HTTP_STATUS[status])
