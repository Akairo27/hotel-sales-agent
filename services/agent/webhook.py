"""The WhatsApp Cloud API webhook — ARCHITECTURE.md §2, §7, §8; PLAN.md
phase 4.

Verifies the channel, stores the inbound message, enforces CLAUDE.md §9's
message-rate cap, and acks fast — then a FastAPI BackgroundTasks job
enforces the spend/turn caps, calls the model, runs the output guard on
whatever it produced, and sends the result over the WhatsApp Cloud API.
The output guard and the send still ship together, deliberately: a guard
with nothing downstream to protect is untested in the one way that
matters (does a real send ever bypass it), and a send with no guard in
front of it is exactly the "wrong price reaches a customer" failure mode
CLAUDE.md rule 8 exists to prevent. Building either alone would mean
shipping half of a safety property.

Fast-ack / background split (added after a real incident: one turn's
Gemini call took 32s end-to-end after retrying internally, and WhatsApp's
own delivery retried the same message mid-flight because our response
hadn't come back yet — services.agent.llm.client's own retries are the
right fix for the retry itself, but nothing bounds how long a webhook
response can take without this split). Order matters and is deliberate:

1. Verify X-Hub-Signature-256 against the raw body, before touching the
   database at all. An unsigned or forged request leaves no trace.
2. Store the inbound message. A message that later gets capped or
   blocked is not a lost message — it stays in the database so a "the
   bot never replied" complaint can be investigated. Also the dedup
   boundary: a retried WhatsApp delivery for the same whatsapp_message_id
   short-circuits here every time, whether the first delivery is still
   being generated in the background or long since finished.
3. Check the per-number-per-day message rate (services.agent.llm.caps).

   Everything through step 3 is synchronous, on one DB connection, and
   fast — confirmed against production timing (comfortably under 1s).
   The response returns here, {"status": "accepted"}, once steps 1-3
   pass. Everything below runs afterward, in _generate_and_deliver_reply
   (a FastAPI BackgroundTasks job), on its own DB connection: the one
   used above is already closed by the time Starlette schedules the
   background job.

4. Call generate_reply. Its tool-calling loop can raise any of several
   exceptions (see conversation.py's own docstring) after one or more
   real, already-paid-for model calls happened in the same turn — not
   only before any of them, since the per-conversation and daily spend
   caps are re-checked before every model call, not just the first, and
   the model transport itself (services.agent.llm.client) can fail on
   any call after its own retries are exhausted. TurnCapExceededError is
   the one exception that never follows a model call in the same turn —
   it is raised before generate_reply loads even the message window.
5. Whatever generate_reply raises or returns, record any usage it
   reports and increment turn_count before deciding how to respond. See
   _handle_generate_reply_failure below: both writes happen exactly
   once, driven by whether the exception is carrying usage
   (services.agent.llm.errors.read_usage_so_far), not by a
   per-exception-type checklist — a new exception type added to
   generate_reply's loop in the future is covered automatically, without
   touching this module.
5a. Any exception in _handle_generate_reply_failure's _ESCALATION_REASONS
    — CLAUDE.md §9's three caps (TurnCapExceededError,
    TokenSpendCapExceededError, DailySpendCapExceededError), or
    ModelUnavailableError once the transport's own retries are exhausted
    — after step 5's recording above, is escalated to a human and
    answered with the exact same bilingual fallback message a blocked
    reply gets in step 6 (_escalate_and_notify), rather than just a
    logged status — CLAUDE.md §9's "beyond the cap, escalate to a
    human" applied literally, and the same "never leave a customer with
    silence" reasoning extended to a transient failure the customer has
    no way to distinguish from one. Every other exception falls through
    to the generic, loudly-logged status in
    _status_for_generate_reply_error instead.
6. Run services.agent.output_guard.enforcement.enforce_outbound_text on
   the candidate reply. Allowed: send it. Blocked: never send it, never
   ask the model to rephrase it (a second, unmanipulated attempt is not
   guaranteed, and it would spend more tokens on a turn that already
   failed) — send OUTPUT_GUARD_FALLBACK_MESSAGE instead, a fixed,
   non-LLM-generated string, through the exact same enforce_outbound_text
   call (its own module docstring already names this as a canned
   template's intended path, not a bypass). enforce_outbound_text already
   opens the escalation for the blocked reply on its own — this module
   adds nothing to that, just acts on the verdict.
7. Send via services.agent.whatsapp_send. A failure here — or the
   fallback itself somehow being blocked, which test_output_guard_
   fallback_message_is_always_allowed (tests/integration/
   test_output_guard.py) exists specifically to make structurally
   impossible, not just unlikely — is handled the same way as every
   other failure in this module: logged loudly, never a 500 (moot for
   the background job anyway — see below).

Background-job reliability: FastAPI's BackgroundTasks runs as part of
the same ASGI call Starlette is already handling — after the response is
sent, but before the app's callable returns — so a graceful `systemctl
restart` (SIGTERM) waits for an in-flight background job exactly the way
it waits for any other in-flight request, bounded by systemd's
TimeoutStopSec (90s on this service today, comfortable margin over the
worst observed ~32s turn). A hard kill (SIGKILL, OOM, crash) loses
whatever was in flight — the same exposure a synchronous in-flight
request already had, not a new regression, but not newly solved by this
split either. See _generate_and_deliver_reply's own docstring for the
one new safety net this split does add: an outermost catch that still
escalates and notifies on literally anything unexpected, since there is
no HTTP response left to carry a failure back to Meta once the ack has
already been sent.

This module never lets a 500 escape from generate_reply, the output
guard, the send, or any of the three usage/message-recording writes, for
one reason that applies uniformly regardless of which step failed or why:
migration 0024's idempotent message insert makes any Meta retry resolve
as a duplicate before ever reaching generate_reply or any of these writes
again (see _insert_inbound_message below) — so a 500 here can never be
fixed by the retry it provokes, and can only turn an already-spent,
unrecorded, or undelivered turn into a *permanently* unrecorded or
undelivered one. Turning every such failure into a 200 instead is not the
same as hiding it: everything that is not one of the expected, named
outcomes is logged at ERROR with the exception's own type name, message,
and full traceback — a real bug or a database outage stays exactly as
visible in the logs as it would be behind a 500, it just stops provoking
a retry that cannot help.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from services.agent.llm.caps import (
    MessageRateCapExceededError,
    check_message_rate_cap,
    increment_turn_count,
    record_token_usage,
)
from services.agent.llm.client import (
    GeminiTransport,
    ModelTransport,
    OpenRouterTransport,
)
from services.agent.llm.config import LlmSettings, load_llm_settings
from services.agent.llm.conversation import UsageTotals, generate_reply
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    ModelUnavailableError,
    TokenSpendCapExceededError,
    TurnCapExceededError,
    UsageUnavailableError,
    read_usage_so_far,
)
from services.agent.output_guard.enforcement import (
    OUTPUT_GUARD_FALLBACK_MESSAGE,
    enforce_outbound_text,
    open_escalation,
)
from services.agent.whatsapp_send import (
    WhatsAppCloudApiSender,
    WhatsAppSender,
    WhatsAppSendSettings,
    load_whatsapp_send_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_SIGNATURE_HEADER = "x-hub-signature-256"
_SIGNATURE_PREFIX = "sha256="


class WebhookConfigurationError(Exception):
    """Raised when WHATSAPP_VERIFY_TOKEN or WHATSAPP_APP_SECRET is unset.

    A separate exception from services.agent.llm's LlmConfigurationError:
    this is webhook-channel configuration, a different domain from the
    model configuration that error type documents.
    """


@dataclass(frozen=True)
class WebhookSettings:
    """The two WhatsApp Cloud API secrets this module needs. Separate from
    LlmSettings — a different configuration domain, loaded independently."""

    verify_token: str
    app_secret: str


def load_webhook_settings() -> WebhookSettings:
    """Raises: WebhookConfigurationError if either variable is unset."""
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if not verify_token:
        raise WebhookConfigurationError("WHATSAPP_VERIFY_TOKEN is not set")
    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not app_secret:
        raise WebhookConfigurationError("WHATSAPP_APP_SECRET is not set")
    return WebhookSettings(verify_token=verify_token, app_secret=app_secret)


def get_webhook_settings() -> WebhookSettings:
    """A separate function from load_webhook_settings so tests can
    monkeypatch just this call site, the same pattern as get_llm_settings
    and get_model_transport below."""
    return load_webhook_settings()


def get_llm_settings() -> LlmSettings:
    return load_llm_settings()


def get_model_transport(settings: LlmSettings) -> ModelTransport:
    route = settings.openrouter_route
    if route is None:
        return GeminiTransport(settings)
    return OpenRouterTransport(
        model=settings.model,
        api_key=settings.api_key,
        providers=route.providers,
        timeout_ms=settings.timeout_ms,
    )


def get_whatsapp_send_settings() -> WhatsAppSendSettings:
    return load_whatsapp_send_settings()


def get_whatsapp_sender(settings: WhatsAppSendSettings) -> WhatsAppSender:
    return WhatsAppCloudApiSender(settings)


@contextlib.contextmanager
def get_db_connection() -> Iterator[psycopg.Connection[Any]]:
    """One connection per caller, closed when that caller is done with it.

    Autocommit: each write below (the conversation upsert, the message
    insert, the token_usage insert) is independently meaningful and must
    survive even if a later step is capped — the same reasoning
    tests/conftest.py's db_conn fixture gives for autocommit. No pooling
    in this PR; that is an operational concern for whichever PR first
    deploys this service, not a correctness concern this one needs to
    solve.

    Called twice per turn since the fast-ack/background split: once for
    receive_message's own fast path, and once more inside
    _generate_and_deliver_reply's background job, which cannot reuse the
    first connection — it is already closed by the time Starlette
    schedules that job.
    """
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _signature_is_valid(
    *, body: bytes, signature_header: str | None, app_secret: str
) -> bool:
    """Verifies X-Hub-Signature-256 against the raw request body —
    CLAUDE.md §8: verify Meta's signature on every webhook call, reject
    unsigned. Must run before any parsing or database access: a forged or
    replayed request must leave zero trace."""
    if signature_header is None or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    provided_digest = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided_digest, expected_digest)


@dataclass(frozen=True)
class InboundMessage:
    customer_phone: str
    customer_name: str | None
    whatsapp_message_id: str
    body: str


def _normalize_phone(wa_id: str) -> str:
    """WhatsApp's wa_id carries no leading '+'; every customer_phone
    column in this schema does (quotes.customer_phone, migration 0008
    onward) — one normalization point so the format never drifts."""
    return wa_id if wa_id.startswith("+") else f"+{wa_id}"


def _parse_inbound_message(payload: dict[str, Any]) -> InboundMessage | None:
    """Extracts the first inbound text message from a WhatsApp Cloud API
    webhook payload, or None for any event this module does not handle —
    delivery/read status callbacks, non-text message types, or a payload
    that does not match the expected shape at all. Meta sends many event
    shapes to the same URL; treating an unrecognized one as a no-op
    (rather than raising) is the correct, expected behavior for a webhook
    consumer that only acts on inbound text messages today.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None
        message = messages[0]
        if message.get("type") != "text":
            return None
        contacts = value.get("contacts") or []
        wa_id = contacts[0]["wa_id"] if contacts else message["from"]
        customer_name = None
        if contacts:
            customer_name = (contacts[0].get("profile") or {}).get("name")
        return InboundMessage(
            customer_phone=_normalize_phone(wa_id),
            customer_name=customer_name,
            whatsapp_message_id=message["id"],
            body=message["text"]["body"],
        )
    except KeyError, IndexError, TypeError:
        return None


def _find_or_create_conversation(
    conn: psycopg.Connection[Any], *, customer_phone: str
) -> int:
    """Race-safe under concurrent webhook deliveries for the same number:
    migration 0026's UNIQUE (customer_phone) backs this ON CONFLICT, so
    two simultaneous deliveries for one number always resolve to the same
    conversation row instead of racing into two."""
    row = conn.execute(
        "INSERT INTO conversations (customer_phone) VALUES (%s) "
        "ON CONFLICT (customer_phone) "
        "DO UPDATE SET customer_phone = EXCLUDED.customer_phone "
        "RETURNING id",
        (customer_phone,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "INSERT ... ON CONFLICT DO UPDATE ... RETURNING id returned no row"
        )
    return int(row[0])


def _insert_inbound_message(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    whatsapp_message_id: str,
    body: str,
) -> int | None:
    """Idempotent: migration 0024's partial unique index on
    whatsapp_message_id makes a duplicate delivery a no-op here, returning
    None instead of inserting a second row — CLAUDE.md's "the same
    WhatsApp message_id processed twice must produce one booking"
    requirement applies at the logging layer too."""
    row = conn.execute(
        "INSERT INTO messages "
        "(conversation_id, customer_phone, direction, whatsapp_message_id, body) "
        "VALUES (%s, %s, 'inbound', %s, %s) "
        "ON CONFLICT (whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL "
        "DO NOTHING RETURNING id",
        (conversation_id, customer_phone, whatsapp_message_id, body),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _insert_outbound_message(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    whatsapp_message_id: str,
    body: str,
) -> None:
    """Records one outbound message. Called only after a real WhatsApp
    Cloud API send actually succeeded (see _send_or_log_failure) — a row
    here is always proof of an attempted delivery that got a message id
    back, never merely an intention to send. No idempotency handling
    needed, unlike _insert_inbound_message: each send produces a fresh
    WhatsApp-assigned id, and this function is only ever reached once per
    inbound delivery (a retried inbound delivery short-circuits on
    _insert_inbound_message's own idempotent insert, long before this
    point — see the module docstring).
    """
    conn.execute(
        "INSERT INTO messages "
        "(conversation_id, customer_phone, direction, whatsapp_message_id, body) "
        "VALUES (%s, %s, 'outbound', %s, %s)",
        (conversation_id, customer_phone, whatsapp_message_id, body),
    )


async def _send_or_log_failure(
    sender: WhatsAppSender,
    *,
    conn: psycopg.Connection[Any],
    conversation_id: int,
    customer_phone: str,
    text: str,
) -> str | None:
    """Attempts the real WhatsApp send; on any failure (deliberately not
    narrowed to WhatsAppSendError — see _record_usage_or_log_failure's
    own docstring for the identical reasoning), logs at ERROR with the
    conversation id, exception type, message, and full traceback, and
    returns None rather than propagating. On success, attempts to record
    the outbound message (_insert_outbound_message) and always returns
    the WhatsApp-assigned message id regardless of whether that recording
    succeeded: the send itself already happened — the customer already
    has the message — so a failure to log it afterward must not be
    reported the same way as the send itself failing, and must not
    propagate either, for the same reasons as every other write in this
    module. This function therefore never raises.
    """
    to_phone = customer_phone.removeprefix("+")
    try:
        whatsapp_message_id = await sender.send_text(to_phone=to_phone, body=text)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "whatsapp_send_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return None

    try:
        _insert_outbound_message(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            whatsapp_message_id=whatsapp_message_id,
            body=text,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "outbound_message_not_recorded",
                    "conversation_id": conversation_id,
                    "whatsapp_message_id": whatsapp_message_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
    return whatsapp_message_id


def _record_usage_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    usage: UsageTotals,
    now: datetime,
) -> bool:
    """Attempts record_token_usage; on any failure (deliberately not
    narrowed to psycopg.Error — see the call site this was extracted
    from), logs at ERROR with the conversation id, the usage figures,
    the exception's type and message, and a full traceback, instead of
    propagating — then returns False rather than raising. Returns True
    on success.

    Shared by every call site in this module that must not lose usage to
    an unhandled 500 — the module docstring explains why: the normal
    post-reply write, and _handle_generate_reply_failure's write for
    whatever usage a generate_reply exception is carrying. A database
    outage or a genuine bug hitting this specific write must be exactly
    as loud as one hitting the generic exception funnel below — CLAUDE.md
    forbids folding a real fault quietly into a structured-but-empty log
    line.
    """
    try:
        record_token_usage(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            usage=usage,
            now=now,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "record_token_usage_failed",
                    "conversation_id": conversation_id,
                    "prompt_tokens": usage.prompt_tokens,
                    "candidates_tokens": usage.candidates_tokens,
                    "total_tokens": usage.total_tokens,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return False
    return True


def _increment_turn_count_or_log_failure(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> None:
    """Attempts increment_turn_count; on any failure, logs at ERROR with
    the conversation id and the exception's type/message/traceback
    instead of propagating. Called at the same two points as
    _record_usage_or_log_failure above, under the same condition — see
    caps.increment_turn_count's own docstring for why. No return value:
    unlike usage recording, nothing downstream branches on whether this
    particular write succeeded.
    """
    try:
        increment_turn_count(conn, conversation_id=conversation_id)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "increment_turn_count_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )


_STATUS_ACCEPTED = "accepted"
_STATUS_CAPPED = "capped"
_STATUS_USAGE_UNAVAILABLE = "usage_unavailable"
_STATUS_TURN_FAILED = "turn_failed"
_STATUS_ESCALATED = "escalated"

# One exception type, one reason -- a human reading escalations must be
# able to tell what actually stopped the conversation without
# re-deriving it from notes. Covers CLAUDE.md §9's three caps plus
# ModelUnavailableError (raised only after client.py's own retries are
# exhausted -- see that module's constants for why retrying again here
# would just stack a second layer on the SDK's).
_ESCALATION_REASONS: dict[type[Exception], str] = {
    TurnCapExceededError: "turn_cap_exceeded",
    TokenSpendCapExceededError: "token_spend_cap_exceeded",
    DailySpendCapExceededError: "daily_spend_cap_exceeded",
    ModelUnavailableError: "model_unavailable",
}

# Used only by _escalate_unexpected_background_failure -- not in
# _ESCALATION_REASONS above, since it is not keyed off any one exception
# class: it is whatever _generate_and_deliver_reply's outermost catch
# receives, a category deliberately left open the same way migration
# 0024's comment on escalations.reason leaves the full set of reasons
# open rather than guessing at it ahead of the code that raises each one.
_REASON_INTERNAL_ERROR = "internal_error"


def _status_for_generate_reply_error(exc: Exception) -> str:
    """Maps a generate_reply exception to this endpoint's response
    status. Purely a label for the response body and for which extra log
    event (if any) to emit — never a factor in whether usage gets
    recorded, which is unconditional in _handle_generate_reply_failure
    below, driven by whether the exception is carrying usage at all, not
    by its type.

    None of the exception types in _ESCALATION_REASONS above are named
    here: all of them are intercepted earlier, in
    _handle_generate_reply_failure, before this function is ever called
    for them — see _escalate_and_notify below.

    An exception type not named here still gets its usage recorded and
    still never produces a 500; it only falls into the generic
    _STATUS_TURN_FAILED bucket instead of a specific one — logged loudly
    enough (event, exception type, message, full traceback) to be found
    the moment it happens. The point of always returning 200 is to stop
    Meta's retry-into-a-permanent-gap trap, not to make a real bug quiet.
    """
    if isinstance(exc, UsageUnavailableError):
        return _STATUS_USAGE_UNAVAILABLE
    return _STATUS_TURN_FAILED


async def _escalate_and_notify(
    conn: psycopg.Connection[Any],
    exc: Exception,
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
) -> str:
    """Opens an escalation and sends the bilingual fallback message
    through the exact same guard-checked send path a guard block uses
    (CLAUDE.md §9's "beyond the cap, escalate to a human" plus rule 8's
    "never leave a customer with silence"). reason is the caller's to
    supply, not derived here: _handle_generate_reply_failure passes
    _ESCALATION_REASONS[type(exc)] for CLAUDE.md §9's three caps or a
    transport failure (ModelUnavailableError);
    _escalate_unexpected_background_failure passes
    _REASON_INTERNAL_ERROR for anything else that escapes every other
    handler in this module. A customer waiting with nobody notified is
    the same outcome regardless of which of these stopped the
    conversation, so all of them get identical treatment.

    Whatever usage this turn incurred is the caller's responsibility to
    have already recorded before this runs (or there was none, for
    TurnCapExceededError -- it fires before any model call this turn,
    errors.read_usage_so_far's own docstring -- or for a failure with no
    prior model call this turn); this function only ever escalates and
    notifies, never records usage itself.

    Never raises: any failure along this path (opening the escalation,
    the guard check, loading WhatsApp send settings, the send itself) is
    logged at ERROR and falls back to _STATUS_CAPPED, the same
    never-a-500 posture as every other write in this module.
    """
    try:
        escalation_id = open_escalation(
            conn,
            conversation_id=conversation_id,
            reason=reason,
            notes={"detail": str(exc)},
        )
        # exception_type is always safe to log. exception_message is
        # included only for ModelUnavailableError: client.py's own
        # message carries just the exception name plus Google's HTTP
        # code/status (never the raw response body -- see client.py's
        # comment on why), while TokenSpendCapExceededError/
        # DailySpendCapExceededError's messages include the account's
        # estimated USD spend, and an internal_error's message is
        # unvetted by definition -- neither belongs in this log event.
        log_fields: dict[str, Any] = {
            "event": "conversation_escalated",
            "conversation_id": conversation_id,
            "reason": reason,
            "escalation_id": escalation_id,
            "exception_type": type(exc).__name__,
        }
        if isinstance(exc, ModelUnavailableError):
            log_fields["exception_message"] = str(exc)
        logger.error(json.dumps(log_fields))

        verdict = enforce_outbound_text(
            conn, conversation_id=conversation_id, text=OUTPUT_GUARD_FALLBACK_MESSAGE
        )
        if not verdict.allowed:
            # Structurally impossible -- the same guarantee
            # test_output_guard_fallback_message_is_always_allowed
            # (tests/integration/test_output_guard.py) exists to prove.
            # Logged loudly if it ever isn't, mirroring the
            # fallback_message_blocked branch in _process_turn below.
            logger.error(
                json.dumps(
                    {
                        "event": "conversation_escalation_fallback_blocked",
                        "conversation_id": conversation_id,
                        "reason": reason,
                        "escalation_id": escalation_id,
                        "fallback_escalation_id": verdict.escalation_id,
                    }
                )
            )
            return _STATUS_CAPPED

        whatsapp_settings = get_whatsapp_send_settings()
        sender = get_whatsapp_sender(whatsapp_settings)
        await _send_or_log_failure(
            sender,
            conn=conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            text=OUTPUT_GUARD_FALLBACK_MESSAGE,
        )
    except Exception as unexpected_exc:
        logger.error(
            json.dumps(
                {
                    "event": "conversation_escalation_failed",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "exception_type": type(unexpected_exc).__name__,
                    "exception_message": str(unexpected_exc),
                }
            ),
            exc_info=unexpected_exc,
        )
        return _STATUS_CAPPED

    return _STATUS_ESCALATED


async def _handle_generate_reply_failure(
    conn: psycopg.Connection[Any],
    exc: Exception,
    *,
    conversation_id: int,
    customer_phone: str,
    now: datetime,
) -> str:
    """The single funnel for everything generate_reply can raise.

    Records whatever usage the exception is carrying — present only when
    one or more real model calls already happened this turn before it
    was raised, per errors.read_usage_so_far and conversation.py's own
    docstring — and increments turn_count alongside it (same condition,
    same reasoning as caps.increment_turn_count's own docstring: a real
    model call happened, so this turn counts, regardless of how it
    ends). Any exception in _ESCALATION_REASONS -- CLAUDE.md §9's three
    caps, or ModelUnavailableError once client.py's own retries are
    exhausted -- is then handled separately by _escalate_and_notify
    above, after that recording (TurnCapExceededError carries no usage
    to record; the others might).

    Adding a new exception type to generate_reply's tool-calling loop in
    the future needs no change here: read_usage_so_far works on any
    exception, and an unnamed type simply falls into the loudly-logged
    _STATUS_TURN_FAILED bucket in _status_for_generate_reply_error above.
    """
    usage_so_far = read_usage_so_far(exc)
    if usage_so_far is not None and usage_so_far.total_tokens > 0:
        _record_usage_or_log_failure(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            usage=usage_so_far,
            now=now,
        )
        _increment_turn_count_or_log_failure(conn, conversation_id=conversation_id)

    if isinstance(
        exc,
        (
            TurnCapExceededError,
            TokenSpendCapExceededError,
            DailySpendCapExceededError,
            ModelUnavailableError,
        ),
    ):
        return await _escalate_and_notify(
            conn,
            exc,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            reason=_ESCALATION_REASONS[type(exc)],
        )

    status = _status_for_generate_reply_error(exc)
    if status == _STATUS_USAGE_UNAVAILABLE:
        logger.error(
            json.dumps(
                {"event": "usage_unavailable", "conversation_id": conversation_id}
            )
        )
    else:
        # _status_for_generate_reply_error now only ever returns
        # _STATUS_USAGE_UNAVAILABLE or _STATUS_TURN_FAILED -- every cap
        # exceeded exception is handled above before this line is ever
        # reached. Deliberately loud: CLAUDE.md forbids folding a real
        # bug or a database outage quietly into a generic bucket.
        # exc_info=exc attaches the full traceback to the log record —
        # the type name and message are also in the JSON body so they
        # are greppable without needing the traceback rendered alongside
        # them.
        logger.error(
            json.dumps(
                {
                    "event": "turn_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )

    return status


async def _process_turn(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_name: str | None,
    customer_phone: str,
    llm_settings: LlmSettings,
    now: datetime,
) -> str:
    """Steps 4-7 of the module docstring: the model call through the
    output-guard-checked send. Unchanged internally from what
    receive_message ran synchronously before the fast-ack/background
    split -- only the caller and the connection's lifetime moved.
    Returns the same status labels the old inline response body used;
    _generate_and_deliver_reply logs whatever this returns, since no
    caller reads it as an HTTP response anymore.
    """
    transport = get_model_transport(llm_settings)
    try:
        reply = await generate_reply(
            conn,
            conversation_id=conversation_id,
            customer_name=customer_name,
            transport=transport,
            settings=llm_settings,
            now=now,
        )
    except Exception as exc:
        return await _handle_generate_reply_failure(
            conn,
            exc,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            now=now,
        )

    usage_recorded = _record_usage_or_log_failure(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        usage=reply.usage,
        now=now,
    )
    _increment_turn_count_or_log_failure(conn, conversation_id=conversation_id)

    try:
        verdict = enforce_outbound_text(
            conn, conversation_id=conversation_id, text=reply.text
        )
        if verdict.allowed:
            text_to_send = reply.text
        else:
            text_to_send = OUTPUT_GUARD_FALLBACK_MESSAGE
            fallback_verdict = enforce_outbound_text(
                conn,
                conversation_id=conversation_id,
                text=OUTPUT_GUARD_FALLBACK_MESSAGE,
            )
            if not fallback_verdict.allowed:
                # Should be structurally impossible -- see the module
                # docstring and test_output_guard_fallback_message_
                # is_always_allowed (tests/integration/
                # test_output_guard.py). Not routed around with a
                # second fallback attempt: that is the exact
                # infinite-regress trap this design avoids. Both
                # escalations already exist (enforce_outbound_text
                # opened one for each call); this log is what makes
                # the second one impossible to miss immediately.
                logger.error(
                    json.dumps(
                        {
                            "event": "fallback_message_blocked",
                            "conversation_id": conversation_id,
                            "original_escalation_id": verdict.escalation_id,
                            "fallback_escalation_id": (fallback_verdict.escalation_id),
                        }
                    )
                )
                return "fallback_blocked" if usage_recorded else "usage_not_recorded"

        whatsapp_settings = get_whatsapp_send_settings()
        sender = get_whatsapp_sender(whatsapp_settings)
        whatsapp_message_id = await _send_or_log_failure(
            sender,
            conn=conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            text=text_to_send,
        )
    except Exception as exc:
        # Everything above this point -- both enforce_outbound_text
        # calls, loading WhatsApp send settings, constructing the
        # sender -- can in principle fail (a DB blip, a missing env
        # var). _send_or_log_failure itself never raises (see its own
        # docstring), but is included here too as defense in depth,
        # the same reasoning as everywhere else in this module: any
        # exception after real spend must be loud, never a 500.
        logger.error(
            json.dumps(
                {
                    "event": "reply_delivery_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return "delivery_failed" if usage_recorded else "usage_not_recorded"

    if whatsapp_message_id is None:
        return "send_failed" if usage_recorded else "usage_not_recorded"

    if not usage_recorded:
        return "usage_not_recorded"
    return "processed" if verdict.allowed else _STATUS_ESCALATED


async def _escalate_unexpected_background_failure(
    exc: Exception, *, conversation_id: int, customer_phone: str
) -> None:
    """Last-resort safety net for _generate_and_deliver_reply: something
    escaped every handler already in this module -- a bug, or the fresh
    DB connection _process_turn was using itself failing partway
    through. Before the fast-ack/background split, that would have
    surfaced as an HTTP 500 Meta could see and retry -- moot even then,
    since idempotent dedup means a retry can never reach generate_reply
    again (see the module docstring) -- but now there is no response at
    all left to carry it, so this is the one place that must still open
    an escalation and attempt the fallback send. Uses a fresh connection
    of its own, since the one the failing turn was using may itself be
    why this failed.

    Mirrors _escalate_and_notify's own shape via reuse, not duplication
    (CLAUDE.md §2's "one way to do each thing") -- reason is
    _REASON_INTERNAL_ERROR, the one case that dict-keyed function's
    reason parameter exists to let a caller supply directly rather than
    derive from the exception's type.

    Never raises: there is nothing left to hand a failure to.
    """
    try:
        with get_db_connection() as conn:
            await _escalate_and_notify(
                conn,
                exc,
                conversation_id=conversation_id,
                customer_phone=customer_phone,
                reason=_REASON_INTERNAL_ERROR,
            )
    except Exception as unexpected_exc:
        logger.error(
            json.dumps(
                {
                    "event": "background_reply_escalation_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(unexpected_exc).__name__,
                    "exception_message": str(unexpected_exc),
                }
            ),
            exc_info=unexpected_exc,
        )


async def _generate_and_deliver_reply(
    *,
    conversation_id: int,
    customer_name: str | None,
    customer_phone: str,
    llm_settings: LlmSettings,
    now: datetime,
) -> None:
    """The slow half of receive_message: steps 4-7 of the module
    docstring, run via FastAPI BackgroundTasks after the fast ack.
    Opens its own DB connection -- the one receive_message used for the
    fast path is already closed by the time Starlette runs this job
    (after the response is sent).

    Never raises: unlike the request this used to run inside, there is
    no HTTP response left downstream to carry a failure back to Meta
    once this is scheduled -- nothing reads an exception from here.
    Every known failure mode is already handled inside _process_turn
    (_handle_generate_reply_failure, the output-guard/send try/except);
    this function's own outermost try/except exists only for what none
    of those catch -- e.g. get_db_connection() itself failing -- so it
    still gets an ERROR log and a best-effort escalation
    (_escalate_unexpected_background_failure) instead of vanishing into
    Starlette's own background-task exception log with no trace and no
    customer notification.
    """
    try:
        with get_db_connection() as conn:
            status = await _process_turn(
                conn,
                conversation_id=conversation_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                llm_settings=llm_settings,
                now=now,
            )
        logger.info(
            json.dumps(
                {
                    "event": "reply_turn_finished",
                    "conversation_id": conversation_id,
                    "status": status,
                }
            )
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "background_reply_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        await _escalate_unexpected_background_failure(
            exc, conversation_id=conversation_id, customer_phone=customer_phone
        )


@router.get("/webhook/whatsapp")
async def verify_subscription(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta's subscription verification handshake.

    Raises:
        HTTPException(403): hub.mode is not "subscribe", or
            hub.verify_token does not match WHATSAPP_VERIFY_TOKEN.
    """
    settings = get_webhook_settings()
    if hub_mode != "subscribe" or not hmac.compare_digest(
        hub_verify_token, settings.verify_token
    ):
        raise HTTPException(status_code=403, detail="verification failed")
    return PlainTextResponse(content=hub_challenge)


@router.post("/webhook/whatsapp")
async def receive_message(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Processes one WhatsApp Cloud API webhook delivery — see this
    module's own docstring for the fast-ack/background split and for why
    the fast path stops where it does.

    Raises:
        HTTPException(401): the signature is missing or does not match
            X-Hub-Signature-256 — checked before any database access.
        HTTPException(400): the signed body is not valid JSON.
    """
    webhook_settings = get_webhook_settings()
    body = await request.body()
    if not _signature_is_valid(
        body=body,
        signature_header=request.headers.get(_SIGNATURE_HEADER),
        app_secret=webhook_settings.app_secret,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None

    inbound = _parse_inbound_message(payload)
    if inbound is None:
        return JSONResponse({"status": "ignored"})

    llm_settings = get_llm_settings()
    now = datetime.now(UTC)

    with get_db_connection() as conn:
        conversation_id = _find_or_create_conversation(
            conn, customer_phone=inbound.customer_phone
        )
        message_id = _insert_inbound_message(
            conn,
            conversation_id=conversation_id,
            customer_phone=inbound.customer_phone,
            whatsapp_message_id=inbound.whatsapp_message_id,
            body=inbound.body,
        )
        if message_id is None:
            return JSONResponse({"status": "duplicate"})

        try:
            check_message_rate_cap(
                conn,
                customer_phone=inbound.customer_phone,
                now=now,
                settings=llm_settings,
            )
        except MessageRateCapExceededError:
            logger.info(
                json.dumps(
                    {
                        "event": "message_rate_cap_blocked",
                        "conversation_id": conversation_id,
                    }
                )
            )
            return JSONResponse({"status": "rate_limited"})

    background_tasks.add_task(
        _generate_and_deliver_reply,
        conversation_id=conversation_id,
        customer_name=inbound.customer_name,
        customer_phone=inbound.customer_phone,
        llm_settings=llm_settings,
        now=now,
    )
    return JSONResponse({"status": _STATUS_ACCEPTED})
