"""Staff replies from the dashboard (staff notification step 3, owner
decisions 2026-10-02; ARCHITECTURE.md §7).

The staff member holding a takeover writes a reply on the dashboard, which
saves it through their own session (staff_queue_reply, migration 0035)
and then calls POST /internal/staff-replies/{id}/send on this service.
That claims the reply once, runs it through the output guard's staff-reply
mode, writes the amounts it states to audit_log, and sends it -- all
before the first WhatsApp call, in one transaction, so a reply is never
sent without its audit row and a failure before the send leaves it
unclaimed for the dashboard to try again.

The endpoint is internal and token-protected exactly as the takeover
acknowledgement (services/agent/takeover_ack.py), and it takes nothing but
a reply id: the text sent is the reply's own stored body.

Outside WhatsApp's 24-hour customer service window the reply is not sent
and is recorded as failed (outside_window), for the acknowledgement's
reason; the dashboard offers the re-engagement template instead. A failed
send is never retried: it may have reached the customer anyway.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from services.agent import webhook
from services.agent.output_guard.staff_replies import (
    StaffReplyInspection,
    audit_record,
    inspect_staff_reply,
)
from services.agent.takeover_ack import (
    require_internal_token,
    within_customer_service_window,
)

logger = logging.getLogger(__name__)

router = APIRouter()

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

# staff_replies.failure_reason (migration 0035).
FAILURE_OUTSIDE_WINDOW = "outside_window"
FAILURE_SEND_FAILED = "send_failed"

_RECORD_SENT = "UPDATE staff_replies SET sent_at = now() WHERE id = %s"
_RECORD_FAILED = (
    "UPDATE staff_replies SET failed_at = now(), failure_reason = %s WHERE id = %s"
)


@dataclass(frozen=True)
class ClaimedStaffReply:
    conversation_id: int
    customer_phone: str
    body: str


def claim_staff_reply(
    conn: psycopg.Connection[Any], *, staff_reply_id: int
) -> ClaimedStaffReply | None:
    """Claims an unclaimed reply whose takeover is still active; returns
    what to send and where, or None when there is nothing to claim. A
    single conditional UPDATE, so of two concurrent calls only one wins.

    Raises:
        psycopg.Error: the write failed.
    """
    row = conn.execute(
        "UPDATE staff_replies AS r SET claimed_at = now() "
        "FROM conversation_takeovers AS t, conversations AS c "
        "WHERE r.id = %s AND t.id = r.takeover_id AND c.id = r.conversation_id "
        "AND t.ended_at IS NULL AND r.claimed_at IS NULL "
        "RETURNING r.conversation_id, c.customer_phone, r.body",
        (staff_reply_id,),
    ).fetchone()
    if row is None:
        return None
    return ClaimedStaffReply(conversation_id=row[0], customer_phone=row[1], body=row[2])


def unclaimed_status(conn: psycopg.Connection[Any], *, staff_reply_id: int) -> str:
    """Why claim_staff_reply found nothing: not_found, already_claimed, or
    takeover_ended (an unclaimed reply whose takeover has ended).

    Raises:
        psycopg.Error: the read failed.
    """
    row = conn.execute(
        "SELECT r.claimed_at IS NOT NULL, t.ended_at IS NOT NULL "
        "FROM staff_replies AS r "
        "JOIN conversation_takeovers AS t ON t.id = r.takeover_id "
        "WHERE r.id = %s",
        (staff_reply_id,),
    ).fetchone()
    if row is None:
        return STATUS_NOT_FOUND
    claimed: bool = row[0]
    ended: bool = row[1]
    return STATUS_TAKEOVER_ENDED if ended and not claimed else STATUS_ALREADY_CLAIMED


def record_stated_amounts(
    conn: psycopg.Connection[Any],
    *,
    staff_reply_id: int,
    inspection: StaffReplyInspection,
) -> None:
    """Writes the amounts a claimed reply states to audit_log
    (staff_reply_record_amounts, migration 0035), or nothing when it
    states none.

    Raises:
        psycopg.Error: the write failed.
    """
    if not inspection.stated_amounts:
        return
    records = [audit_record(amount) for amount in inspection.stated_amounts]
    conn.execute(
        "SELECT staff_reply_record_amounts(%s, %s)", (staff_reply_id, Jsonb(records))
    )


def record_outcome_or_log_failure(
    conn: psycopg.Connection[Any], *, staff_reply_id: int, failure_reason: str | None
) -> None:
    """Stamps sent_at, or failed_at with failure_reason, on a claimed reply;
    a failed write is logged at ERROR, never raised -- the send already
    happened or failed either way."""
    try:
        if failure_reason is None:
            conn.execute(_RECORD_SENT, (staff_reply_id,))
        else:
            conn.execute(_RECORD_FAILED, (failure_reason, staff_reply_id))
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "staff_reply_outcome_not_recorded",
                    "staff_reply_id": staff_reply_id,
                    "failure_reason": failure_reason,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )


def _claim_and_audit(
    conn: psycopg.Connection[Any], *, staff_reply_id: int
) -> tuple[ClaimedStaffReply, StaffReplyInspection] | str:
    """In one transaction: claims the reply, inspects it, audits its
    amounts, and checks the 24-hour window. Returns what to send, or the
    final status when there is nothing to send (an outside-window reply is
    recorded as failed in the same transaction).

    Raises:
        psycopg.Error: any step failed; the transaction is rolled back, so
            the reply stays unclaimed.
    """
    with conn.transaction():
        claim = claim_staff_reply(conn, staff_reply_id=staff_reply_id)
        if claim is None:
            return unclaimed_status(conn, staff_reply_id=staff_reply_id)
        inspection = inspect_staff_reply(
            claim.body, conversation_id=claim.conversation_id
        )
        record_stated_amounts(
            conn, staff_reply_id=staff_reply_id, inspection=inspection
        )
        if not within_customer_service_window(
            conn, conversation_id=claim.conversation_id
        ):
            conn.execute(_RECORD_FAILED, (FAILURE_OUTSIDE_WINDOW, staff_reply_id))
            return STATUS_OUTSIDE_WINDOW
    return claim, inspection


async def send_staff_reply(
    conn: psycopg.Connection[Any], *, staff_reply_id: int
) -> str:
    """Claims, audits, sends and records one staff reply; returns its status.

    Raises:
        psycopg.Error: a step before the send failed (_claim_and_audit).
    """
    prepared = _claim_and_audit(conn, staff_reply_id=staff_reply_id)
    if isinstance(prepared, str):
        return prepared
    claim, inspection = prepared
    delivered = await webhook.send_staff_reply_or_log_failure(
        conn,
        conversation_id=claim.conversation_id,
        customer_phone=claim.customer_phone,
        staff_reply_id=staff_reply_id,
        inspection=inspection,
    )
    record_outcome_or_log_failure(
        conn,
        staff_reply_id=staff_reply_id,
        failure_reason=None if delivered else FAILURE_SEND_FAILED,
    )
    return STATUS_SENT if delivered else STATUS_FAILED


@router.post("/internal/staff-replies/{staff_reply_id}/send")
async def send_staff_reply_endpoint(
    staff_reply_id: int, request: Request
) -> JSONResponse:
    """Sends one staff reply for the dashboard. The response is
    {"status": ...}: sent, outside_window, already_claimed or
    takeover_ended (200), not_found (404), failed (502: the send failed),
    unavailable (503: the database could not be reached; the reply was not
    claimed, so the dashboard may offer to try again).

    Raises:
        HTTPException(401): the Bearer token is missing or wrong
            (takeover_ack.require_internal_token).
    """
    require_internal_token(request)
    try:
        with webhook.get_db_connection() as conn:
            status = await send_staff_reply(conn, staff_reply_id=staff_reply_id)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "staff_reply_send_failed",
                    "staff_reply_id": staff_reply_id,
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
                "event": "staff_reply_send_finished",
                "staff_reply_id": staff_reply_id,
                "status": status,
            }
        )
    )
    return JSONResponse({"status": status}, status_code=_HTTP_STATUS[status])
