"""The booking buttons and the bare typed yes end to end, through the real
webhook endpoint against a real Postgres (owner decisions 2026-10-01): a
quote reply goes out with buttons; a yes tap or a bare typed yes is
passed on and confirmed with no model call; a replaced offer gets the
newer price; a mismatched tap gets the fallback and an escalation; a
question tap, an expired quote or any other text goes to the model; a
refused button send is retried as plain text.

Every test runs as the privileged test role and as hotel_agent, the role
the webhook connects as (the webhook_client fixture). The WhatsApp sender
and the model are fakes, as in tests/integration/test_webhook.py; the
output guard and the database are real.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.agent import webhook as webhook_module
from services.agent.booking_buttons import booking_offer_buttons, button_id
from services.agent.booking_confirmation import render_booking_passed_on
from services.agent.fixed_texts import FALLBACK, NEWER_PRICE
from services.agent.llm.booking_follow_up import QuoteSummary
from services.agent.llm.config import LlmSettings
from services.agent.llm.model_types import (
    ModelResponse,
    ModelTurn,
    ModelUsage,
    ToolCall,
    Turn,
    UserTurn,
)
from services.agent.main import app
from services.agent.whatsapp_send import (
    ReplyButton,
    WhatsAppMessageRejectedError,
    WhatsAppSendError,
    WhatsAppSendSettings,
)
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_night,
    seed_conversation,
    seed_hotel,
    seed_hotel_and_room_type,
    seed_message,
    seed_price_rule,
    seed_quote,
    seed_room_type,
    seed_season,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_APP_SECRET = "test-app-secret"
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_OTHER_PHONE = "+966500000002"
_OFFER_ID = "wamid.OFFER"
_CHECK_IN = date(2026, 10, 20)
_OFFER = (
    "Test Hotel, Standard room, 2 nights.\n"
    "Shall I pass this to a colleague to confirm your booking?"
)
_YES_TITLE = "Yes, confirm"


@dataclass
class _RecordingSender:
    """Records every send as (kind, body, buttons), with a distinct
    message id per success (messages.whatsapp_message_id is unique). A
    button send raises buttons_error, and a text send text_error, when
    set."""

    buttons_error: Exception | None = None
    text_error: Exception | None = None
    sends: list[tuple[str, str, tuple[ReplyButton, ...]]] = field(default_factory=list)

    async def send_text(self, *, to_phone: str, body: str) -> str:
        assert to_phone == _WA_ID
        self.sends.append(("text", body, ()))
        if self.text_error is not None:
            raise self.text_error
        return f"wamid.OUT-{len(self.sends)}"

    async def send_template(
        self,
        *,
        to_phone: str,
        template_name: str,
        language_code: str,
        body_parameters: tuple[str, ...],
    ) -> str:
        del to_phone, template_name, language_code, body_parameters
        raise AssertionError("send_template is not expected in this test")

    async def send_reply_buttons(
        self, *, to_phone: str, body: str, buttons: tuple[ReplyButton, ...]
    ) -> str:
        assert to_phone == _WA_ID
        self.sends.append(("buttons", body, buttons))
        if self.buttons_error is not None:
            raise self.buttons_error
        return f"wamid.OUT-{len(self.sends)}"


# What the send client raises for Meta refusing a message as invalid
# (nothing sent), and for a timeout (it may have been sent).
_REJECTED = WhatsAppMessageRejectedError(
    "WhatsApp send failed: HTTPStatusError (status=400, code=131009)"
)
_TIMED_OUT = WhatsAppSendError("WhatsApp send failed: ReadTimeout")


@dataclass
class _ScriptedTransport:
    """Returns each scripted response in order; records what it was sent."""

    script: list[ModelResponse] = field(default_factory=list)
    turns_seen: list[list[Turn]] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del system_instruction, deadline
        self.turns_seen.append(list(turns))
        return self.script[len(self.turns_seen) - 1]


def _usage() -> ModelUsage:
    return ModelUsage(prompt_tokens=25, candidates_tokens=5, total_tokens=30)


def _text_response(text: str) -> ModelResponse:
    return ModelResponse(turn=ModelTurn(text=text, tool_calls=()), usage=_usage())


def _tool_response(name: str, args: dict[str, Any]) -> ModelResponse:
    call = ToolCall(id=f"call_{name}", name=name, args=args)
    return ModelResponse(turn=ModelTurn(text=None, tool_calls=(call,)), usage=_usage())


def _settings() -> LlmSettings:
    return LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=20,
        max_tokens_per_conversation=1_000_000,
        max_spend_per_day_usd=Decimal("1000"),
        max_messages_per_number_per_day=1_000,
        max_tokens_per_number_per_day=10_000_000,
    )


@contextlib.contextmanager
def _shared(conn: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    yield conn


@contextlib.contextmanager
def _agent_connection(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class _Harness:
    client: TestClient
    sender: _RecordingSender
    transport: _ScriptedTransport


@pytest.fixture(params=["privileged", "hotel_agent"])
def harness(
    request: pytest.FixtureRequest,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> _Harness:
    """The real app, wired to the test database as either role, a fixed
    webhook secret, a recording sender and a scripted model."""
    monkeypatch.setattr(
        webhook_module,
        "get_webhook_settings",
        lambda: webhook_module.WebhookSettings(
            verify_token="test-verify-token", app_secret=_APP_SECRET
        ),
    )
    if request.param == "hotel_agent":
        agent_url: str = request.getfixturevalue("agent_database_url")
        monkeypatch.setattr(
            webhook_module, "get_db_connection", lambda: _agent_connection(agent_url)
        )
    else:
        monkeypatch.setattr(
            webhook_module, "get_db_connection", lambda: _shared(db_conn)
        )
    sender = _RecordingSender()
    transport = _ScriptedTransport()
    monkeypatch.setattr(webhook_module, "get_llm_settings", _settings)
    monkeypatch.setattr(webhook_module, "get_model_transport", lambda _s: transport)
    monkeypatch.setattr(
        webhook_module,
        "get_whatsapp_send_settings",
        lambda: WhatsAppSendSettings(
            phone_number_id="test-phone-number-id",
            access_token="test-access-token",
            timeout_ms=10_000,
        ),
    )
    monkeypatch.setattr(webhook_module, "get_whatsapp_sender", lambda _s: sender)
    return _Harness(client=TestClient(app), sender=sender, transport=transport)


def _deliver(harness: _Harness, message: dict[str, Any]) -> Any:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [
                                {"wa_id": _WA_ID, "profile": {"name": "Test Customer"}}
                            ],
                            "messages": [
                                {"from": _WA_ID, "timestamp": "1700000000", **message}
                            ],
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return harness.client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )


def _typed(message_id: str, text: str) -> dict[str, Any]:
    return {"id": message_id, "type": "text", "text": {"body": text}}


def _tap(
    message_id: str,
    raw_id: str,
    *,
    context_id: str = _OFFER_ID,
    title: str = _YES_TITLE,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "interactive",
        "context": {"from": "15550000000", "id": context_id},
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": raw_id, "title": title},
        },
    }


def _minutes_ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _offered_stay(
    db_conn: psycopg.Connection[Any],
    *,
    quoted: float = 19,
    phone: str = _PHONE,
    offer_id: str = _OFFER_ID,
) -> tuple[int, int]:
    """The customer asked, a quote was made `quoted` minutes ago, and our
    reply offered it. Returns (conversation_id, quote_id)."""
    conversation_id = seed_conversation(db_conn, customer_phone=phone)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="price?",
        customer_phone=phone,
        created_at=_minutes_ago(quoted + 1),
    )
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    quote_id = seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=phone,
        created_at=_minutes_ago(quoted),
        check_in=_CHECK_IN,
        check_out=_CHECK_IN + timedelta(days=2),
    )
    seed_message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=_OFFER,
        customer_phone=phone,
        created_at=_minutes_ago(quoted - 0.1),
        whatsapp_message_id=offer_id,
    )
    return conversation_id, quote_id


def _newer_offer(db_conn: psycopg.Connection[Any], conversation_id: int) -> int:
    """A second turn after the first offer: another quote and its offer."""
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="other dates?",
        created_at=_minutes_ago(10),
    )
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    newer_id = seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        created_at=_minutes_ago(9),
    )
    seed_message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=_OFFER,
        created_at=_minutes_ago(8.9),
        whatsapp_message_id="wamid.OFFER-2",
    )
    return newer_id


def _confirmation(quote_id: int) -> str:
    return render_booking_passed_on(
        QuoteSummary(
            quote_id=quote_id,
            hotel_name="Test Hotel",
            room_type_name="Standard",
            check_in=_CHECK_IN,
            check_out=_CHECK_IN + timedelta(days=2),
            rooms=1,
            total_halalas=20_000,
        ),
        "en",
    )


def _escalations(db_conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    rows = db_conn.execute(
        "SELECT reason, notes, quote_id FROM escalations WHERE customer_phone = %s "
        "ORDER BY id",
        (_PHONE,),
    ).fetchall()
    return [(reason, json.loads(notes), quote_id) for reason, notes, quote_id in rows]


def _bodies(db_conn: psycopg.Connection[Any], direction: str) -> list[str]:
    rows = db_conn.execute(
        "SELECT body FROM messages WHERE customer_phone = %s AND direction = %s "
        "ORDER BY created_at, id",
        (_PHONE, direction),
    ).fetchall()
    return [body for (body,) in rows]


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    parsed = (
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "services.agent.webhook"
    )
    return [entry for entry in parsed if entry.get("event") == event]


def _seed_priceable_hotel(db_conn: psycopg.Connection[Any]) -> tuple[int, int]:
    """A searchable hotel with one free, priceable night on 2030-01-10."""
    hotel_id = seed_hotel(
        db_conn,
        city="makkah",
        zone="makkah_central",
        star_rating=4,
        distance_to_haram_meters=350,
        address_text="Test address",
    )
    room_type_id = seed_room_type(db_conn, hotel_id, room_type_name="Standard")
    seed_season(
        db_conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )
    seed_allotment_night(
        db_conn, hotel_id, room_type_id, date(2030, 1, 10), total_rooms=5
    )
    seed_price_rule(
        db_conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    return hotel_id, room_type_id


def test_a_reply_offering_one_priced_stay_goes_out_with_buttons(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    hotel_id, room_type_id = _seed_priceable_hotel(db_conn)
    reply = "Test Hotel, Standard room, 10 January.\n" + _OFFER.splitlines()[-1]
    harness.transport.script = [
        _tool_response("search_hotels", {"hotel_name": "Test Hotel"}),
        _tool_response(
            "get_quote",
            {
                "hotel_id": hotel_id,
                "room_type_id": room_type_id,
                "check_in": "2030-01-10",
                "check_out": "2030-01-11",
                "rooms": 1,
            },
        ),
        _text_response(reply),
    ]
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    response = _deliver(harness, _typed("wamid.ASK", "price for 10 January?"))

    assert response.json() == {"status": "accepted"}
    ((quote_id,),) = db_conn.execute("SELECT id FROM quotes").fetchall()
    assert harness.sender.sends == [
        ("buttons", reply, booking_offer_buttons(quote_id, "en").buttons)
    ]
    assert _bodies(db_conn, "outbound") == [reply]
    (sent,) = _events(caplog, "booking_offer_buttons_sent")
    assert (sent["quote_id"], sent["whatsapp_message_id"]) == (quote_id, "wamid.OUT-1")
    assert _escalations(db_conn) == []


def test_a_yes_tap_passes_the_quote_on_and_confirms_it_without_the_model(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, quote_id = _offered_stay(db_conn)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    response = _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    assert response.json() == {"status": "accepted"}
    assert harness.transport.turns_seen == []
    assert harness.sender.sends == [("text", _confirmation(quote_id), ())]
    assert _escalations(db_conn) == [("booking_requested", {}, quote_id)]
    assert _bodies(db_conn, "inbound")[-1] == _YES_TITLE
    assert _bodies(db_conn, "outbound")[-1] == _confirmation(quote_id)
    (handled,) = _events(caplog, "booking_yes_handled_in_code")
    assert handled == {
        "event": "booking_yes_handled_in_code",
        "conversation_id": handled["conversation_id"],
        "quote_id": quote_id,
        "already_requested": False,
        "source": "button",
    }


def test_a_second_tap_confirms_again_without_a_second_request(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    _, quote_id = _offered_stay(db_conn)

    _deliver(harness, _tap("wamid.TAP-1", button_id("yes", quote_id)))
    _deliver(harness, _tap("wamid.TAP-2", button_id("yes", quote_id)))

    assert [body for _, body, _ in harness.sender.sends] == [
        _confirmation(quote_id),
        _confirmation(quote_id),
    ]
    assert _escalations(db_conn) == [("booking_requested", {}, quote_id)]


def test_a_redelivered_tap_is_answered_once(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    _, quote_id = _offered_stay(db_conn)

    _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))
    response = _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    assert response.json() == {"status": "duplicate"}
    assert len(harness.sender.sends) == 1


def test_a_tap_on_a_replaced_offer_gets_the_newer_price_with_new_buttons(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    newer_id = _newer_offer(db_conn, conversation_id)

    _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    assert harness.transport.turns_seen == []
    assert harness.sender.sends == [
        ("buttons", NEWER_PRICE.english, booking_offer_buttons(newer_id, "en").buttons)
    ]
    assert _escalations(db_conn) == []


def test_a_definitely_rejected_button_send_is_retried_as_plain_text(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    _newer_offer(db_conn, conversation_id)
    harness.sender.buttons_error = _REJECTED
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    assert [(kind, body) for kind, body, _ in harness.sender.sends] == [
        ("buttons", NEWER_PRICE.english),
        ("text", NEWER_PRICE.english),
    ]
    assert _bodies(db_conn, "outbound")[-1] == NEWER_PRICE.english
    assert len(_events(caplog, "booking_offer_buttons_rejected")) == 1
    assert _escalations(db_conn) == []
    (finished,) = _events(caplog, "reply_turn_finished")
    assert finished["status"] == "processed"


def test_a_timed_out_button_send_is_not_retried_and_ends_in_the_funnel(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The offer may already have reached the customer, so it is never sent
    a second time (owner decision 2026-10-01): the fallback and an
    escalation follow instead."""
    conversation_id, quote_id = _offered_stay(db_conn)
    _newer_offer(db_conn, conversation_id)
    harness.sender.buttons_error = _TIMED_OUT
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    assert [(kind, body) for kind, body, _ in harness.sender.sends] == [
        ("buttons", NEWER_PRICE.english),
        ("text", FALLBACK.english),
    ]
    assert NEWER_PRICE.english not in _bodies(db_conn, "outbound")
    assert _bodies(db_conn, "outbound")[-1] == FALLBACK.english
    assert _events(caplog, "booking_offer_buttons_rejected") == []
    assert _escalations(db_conn) == [("delivery_failed", {}, None)]
    (finished,) = _events(caplog, "reply_turn_finished")
    assert finished["status"] == "escalated"


def test_when_the_retry_is_refused_too_the_turn_ends_in_the_funnel(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    _newer_offer(db_conn, conversation_id)
    harness.sender.buttons_error = _REJECTED
    harness.sender.text_error = _TIMED_OUT
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    assert [kind for kind, _, _ in harness.sender.sends] == ["buttons", "text", "text"]
    assert harness.sender.sends[-1][1] == FALLBACK.english
    assert _escalations(db_conn) == [("delivery_failed", {}, None)]
    (finished,) = _events(caplog, "reply_turn_finished")
    assert finished["status"] == "escalated_undelivered"


def test_a_tap_after_the_quote_expired_goes_to_the_model(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    _, quote_id = _offered_stay(db_conn, quoted=45)
    harness.transport.script = [_text_response("Let me check today's price.")]

    _deliver(harness, _tap("wamid.TAP", button_id("yes", quote_id)))

    (turns,) = harness.transport.turns_seen
    assert turns[-1] == UserTurn(text=_YES_TITLE)
    assert harness.sender.sends == [("text", "Let me check today's price.", ())]
    assert _escalations(db_conn) == []


def test_a_question_tap_goes_to_the_model(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    _, quote_id = _offered_stay(db_conn)
    harness.transport.script = [_text_response("Of course, what would you like?")]

    _deliver(
        harness,
        _tap(
            "wamid.TAP",
            button_id("question", quote_id),
            title="I have a question",
        ),
    )

    (turns,) = harness.transport.turns_seen
    assert turns[-1] == UserTurn(text="I have a question")
    assert harness.sender.sends == [("text", "Of course, what would you like?", ())]
    assert _escalations(db_conn) == []


@pytest.mark.parametrize(
    ("problem", "raw_id_for", "context_id"),
    [
        ("unknown_button_id", lambda _quote, _other: "booking:yes:abc", _OFFER_ID),
        (
            "unknown_offer_message",
            lambda quote, _other: button_id("yes", quote),
            "wamid.OTHER-OFFER",
        ),
        ("foreign_quote", lambda _quote, other: button_id("yes", other), _OFFER_ID),
    ],
)
def test_a_mismatched_tap_gets_the_fallback_and_an_escalation(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    problem: str,
    raw_id_for: Any,
    context_id: str,
) -> None:
    _, other_quote_id = _offered_stay(
        db_conn, phone=_OTHER_PHONE, offer_id="wamid.OTHER-OFFER"
    )
    _, quote_id = _offered_stay(db_conn)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _deliver(
        harness,
        _tap("wamid.TAP", raw_id_for(quote_id, other_quote_id), context_id=context_id),
    )

    assert harness.transport.turns_seen == []
    assert harness.sender.sends == [("text", FALLBACK.english, ())]
    assert _escalations(db_conn) == [
        ("booking_button_mismatch", {"problem": problem}, None)
    ]
    (mismatch,) = _events(caplog, "booking_button_mismatch")
    assert mismatch["problem"] == problem


def test_a_bare_typed_yes_is_passed_on_without_the_model(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, quote_id = _offered_stay(db_conn)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _deliver(harness, _typed("wamid.YES", "ok"))

    assert harness.transport.turns_seen == []
    assert harness.sender.sends == [("text", _confirmation(quote_id), ())]
    assert _escalations(db_conn) == [("booking_requested", {}, quote_id)]
    (handled,) = _events(caplog, "booking_yes_handled_in_code")
    assert handled["source"] == "text"


def test_a_typed_yes_with_more_words_goes_to_the_model(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    _offered_stay(db_conn)
    harness.transport.script = [_text_response("Happy to help.")]

    _deliver(harness, _typed("wamid.YES", "ok thanks"))

    assert len(harness.transport.turns_seen) == 1
    assert harness.sender.sends == [("text", "Happy to help.", ())]
    assert _escalations(db_conn) == []


def test_a_booking_the_model_passes_on_is_confirmed_with_the_fixed_text(
    harness: _Harness,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner decision 2026-10-01: after request_booking_follow_up the
    customer gets the code-rendered confirmation, never the model's words,
    in the language of the offer they answered."""
    _, quote_id = _offered_stay(db_conn)
    harness.transport.script = [
        _tool_response("request_booking_follow_up", {}),
        _text_response("All done, my friend!"),
    ]
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _deliver(harness, _typed("wamid.YES", "ok thanks"))

    assert len(harness.transport.turns_seen) == 2
    assert harness.sender.sends == [("text", _confirmation(quote_id), ())]
    assert "All done, my friend!" not in _bodies(db_conn, "outbound")
    assert _escalations(db_conn) == [("booking_requested", {}, quote_id)]
    (confirmed,) = _events(caplog, "booking_request_confirmed_in_code")
    assert confirmed["quote_id"] == quote_id
    (finished,) = _events(caplog, "reply_turn_finished")
    assert finished["status"] == "processed"


def test_a_confirmation_the_model_writes_without_the_tool_is_blocked(
    harness: _Harness, db_conn: psycopg.Connection[Any]
) -> None:
    """Eval run 36819478043's failure: the price is right, but nothing was
    passed on. The customer gets the fallback and staff a real
    escalation; no booking request is opened."""
    _, quote_id = _offered_stay(db_conn)
    harness.transport.script = [_text_response(_confirmation(quote_id))]

    _deliver(harness, _typed("wamid.YES", "ok thanks"))

    assert harness.sender.sends == [("text", FALLBACK.english, ())]
    ((reason, notes, escalation_quote_id),) = _escalations(db_conn)
    assert reason == "output_guard_violation_booking_claim"
    assert notes["booking_claims"] == ["passed your request"]
    assert escalation_quote_id is None
