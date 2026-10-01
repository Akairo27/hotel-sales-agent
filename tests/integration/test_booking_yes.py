"""services/agent/booking_yes.py against a real Postgres: each check on a
tapped yes button and each state condition on a typed bare yes, and the
reads run as hotel_agent, the role the webhook connects as.

Timestamps are relative to the present: validity is measured against the
database's own now(). The tap or typed yes is stored before it is
decided on, as the webhook stores it."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.agent.booking_buttons import ButtonTap, button_id
from services.agent.booking_yes import (
    BookingDecision,
    ButtonMismatch,
    CustomerMessage,
    OfferNewerPrice,
    PassOn,
    decide_booking_yes,
)
from services.agent.llm.booking_follow_up import QuoteSummary
from services.agent.llm.config import DEFAULT_QUOTE_VALIDITY_MINUTES
from tests.integration._seed import (
    seed_conversation,
    seed_hotel_and_room_type,
    seed_message,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"
_OTHER_PHONE = "+966500000002"
_VALIDITY = timedelta(minutes=DEFAULT_QUOTE_VALIDITY_MINUTES)
_CHECK_IN = date(2026, 10, 20)
_ENGLISH_OFFER = (
    "Test Hotel, Standard room, 2 nights.\n"
    "Shall I pass this to a colleague to confirm your booking?"
)
_INDONESIAN_OFFER = (
    "Test Hotel, kamar Standard, 2 malam.\n"
    "Mau saya teruskan ke rekan saya untuk konfirmasi pemesanan?"
)
_OFFER_ID = "wamid.OFFER"
_ANSWER_ID = "wamid.ANSWER"


def _minutes_ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _message(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    direction: str,
    body: str,
    minutes_ago: float,
    whatsapp_message_id: str | None = None,
    phone: str = _PHONE,
) -> None:
    seed_message(
        db_conn,
        conversation_id,
        direction=direction,
        body=body,
        customer_phone=phone,
        created_at=_minutes_ago(minutes_ago),
        whatsapp_message_id=whatsapp_message_id,
    )


def _quote(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    minutes_ago: float,
    phone: str = _PHONE,
) -> int:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    return seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=phone,
        created_at=_minutes_ago(minutes_ago),
        check_in=_CHECK_IN,
        check_out=_CHECK_IN + timedelta(days=2),
    )


def _offered_stay(
    db_conn: psycopg.Connection[Any],
    *,
    quoted: float = 19,
    offer: str = _ENGLISH_OFFER,
    offer_id: str = _OFFER_ID,
    phone: str = _PHONE,
) -> tuple[int, int]:
    """A conversation: the customer asks, a quote is made `quoted` minutes
    ago, and our reply offers it. Returns (conversation_id, quote_id)."""
    conversation_id = seed_conversation(db_conn, customer_phone=phone)
    _message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="price?",
        minutes_ago=quoted + 1,
        phone=phone,
    )
    quote_id = _quote(db_conn, conversation_id, minutes_ago=quoted, phone=phone)
    _message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=offer,
        minutes_ago=quoted - 0.1,
        whatsapp_message_id=offer_id,
        phone=phone,
    )
    return conversation_id, quote_id


def _answer(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    text: str,
    *,
    whatsapp_message_id: str = _ANSWER_ID,
    minutes_ago: float = 1,
) -> None:
    _message(
        db_conn,
        conversation_id,
        direction="inbound",
        body=text,
        minutes_ago=minutes_ago,
        whatsapp_message_id=whatsapp_message_id,
    )


def _tap(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    raw_id: str,
    *,
    context_id: str | None = _OFFER_ID,
    validity: timedelta = _VALIDITY,
) -> BookingDecision | None:
    _answer(db_conn, conversation_id, "Yes, confirm")
    tap = ButtonTap(
        button_id=raw_id, title="Yes, confirm", context_message_id=context_id
    )
    return decide_booking_yes(
        db_conn,
        CustomerMessage(whatsapp_message_id=_ANSWER_ID, text=tap.title, button=tap),
        conversation_id=conversation_id,
        quote_validity=validity,
    )


def _typed(
    conn: psycopg.Connection[Any],
    conversation_id: int,
    text: str,
    *,
    whatsapp_message_id: str = _ANSWER_ID,
) -> BookingDecision | None:
    return decide_booking_yes(
        conn,
        CustomerMessage(
            whatsapp_message_id=whatsapp_message_id, text=text, button=None
        ),
        conversation_id=conversation_id,
        quote_validity=_VALIDITY,
    )


def _summary(quote_id: int) -> QuoteSummary:
    return QuoteSummary(
        quote_id=quote_id,
        hotel_name="Test Hotel",
        room_type_name="Standard",
        check_in=_CHECK_IN,
        check_out=_CHECK_IN + timedelta(days=2),
        rooms=1,
        total_halalas=20_000,
    )


def test_a_yes_tap_on_the_current_offer_is_passed_on(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)

    decision = _tap(db_conn, conversation_id, button_id("yes", quote_id))

    assert decision == PassOn(quote=_summary(quote_id), language="en", source="button")


def test_the_tap_is_answered_in_the_offer_s_language(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn, offer=_INDONESIAN_OFFER)

    decision = _tap(db_conn, conversation_id, button_id("yes", quote_id))

    assert isinstance(decision, PassOn)
    assert decision.language == "id"


def test_a_question_tap_is_left_to_the_model(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)

    assert _tap(db_conn, conversation_id, button_id("question", quote_id)) is None


def test_a_button_id_this_system_never_made_is_a_mismatch(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _ = _offered_stay(db_conn)

    decision = _tap(db_conn, conversation_id, "booking:yes:abc")

    assert decision == ButtonMismatch(problem="unknown_button_id")


@pytest.mark.parametrize(
    "context_id", [None, "wamid.UNKNOWN", "wamid.PRICE-QUESTION", "wamid.OTHER-OFFER"]
)
def test_a_tap_on_a_message_that_is_not_our_offer_here_is_a_mismatch(
    db_conn: psycopg.Connection[Any], context_id: str | None
) -> None:
    """Missing, unknown, the customer's own message, or our message in
    another conversation."""
    _offered_stay(db_conn, phone=_OTHER_PHONE, offer_id="wamid.OTHER-OFFER")
    conversation_id, quote_id = _offered_stay(db_conn)
    _message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="and breakfast?",
        minutes_ago=5,
        whatsapp_message_id="wamid.PRICE-QUESTION",
    )

    decision = _tap(
        db_conn, conversation_id, button_id("yes", quote_id), context_id=context_id
    )

    assert decision == ButtonMismatch(problem="unknown_offer_message")


def test_a_tap_naming_another_conversation_s_quote_is_a_mismatch(
    db_conn: psycopg.Connection[Any],
) -> None:
    _, other_quote_id = _offered_stay(
        db_conn, phone=_OTHER_PHONE, offer_id="wamid.OTHER-OFFER"
    )
    conversation_id, _ = _offered_stay(db_conn)

    decision = _tap(db_conn, conversation_id, button_id("yes", other_quote_id))

    assert decision == ButtonMismatch(problem="foreign_quote")


def test_a_tap_naming_no_quote_at_all_is_a_mismatch(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)

    decision = _tap(db_conn, conversation_id, button_id("yes", quote_id + 1000))

    assert decision == ButtonMismatch(problem="foreign_quote")


def test_a_tap_after_the_quote_expired_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn, quoted=45)

    assert _tap(db_conn, conversation_id, button_id("yes", quote_id)) is None


def test_a_tap_on_a_quote_from_an_earlier_session_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The window is a day here, so only the session boundary (6 idle
    hours) can exclude it."""
    conversation_id, quote_id = _offered_stay(db_conn, quoted=8 * 60)

    decision = _tap(
        db_conn,
        conversation_id,
        button_id("yes", quote_id),
        validity=timedelta(days=1),
    )

    assert decision is None


def _newer_offer(
    db_conn: psycopg.Connection[Any], conversation_id: int, *, quotes: int = 1
) -> int:
    """The customer asks again, `quotes` quotes are made in that turn, and
    our reply offers the last."""
    _message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="other dates?",
        minutes_ago=10,
    )
    newer = [
        _quote(db_conn, conversation_id, minutes_ago=9 - index * 0.1)
        for index in range(quotes)
    ]
    _message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=_ENGLISH_OFFER,
        minutes_ago=8,
        whatsapp_message_id="wamid.OFFER-2",
    )
    return newer[-1]


def test_a_tap_on_a_replaced_offer_offers_the_newer_price(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    newer_id = _newer_offer(db_conn, conversation_id)

    decision = _tap(db_conn, conversation_id, button_id("yes", quote_id))

    assert decision == OfferNewerPrice(
        tapped_quote_id=quote_id, quote_id=newer_id, language="en"
    )


def test_a_tap_on_a_replaced_offer_is_left_to_the_model_when_two_stays_replaced_it(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    _newer_offer(db_conn, conversation_id, quotes=2)

    assert _tap(db_conn, conversation_id, button_id("yes", quote_id)) is None


def test_a_bare_typed_yes_to_the_offer_is_passed_on(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    _answer(db_conn, conversation_id, "ok")

    decision = _typed(db_conn, conversation_id, "ok")

    assert decision == PassOn(quote=_summary(quote_id), language="en", source="text")


def test_a_typed_yes_is_answered_in_the_offer_s_language_not_its_own(
    db_conn: psycopg.Connection[Any],
) -> None:
    """ "ok" reads as English; the offer was Indonesian."""
    conversation_id, _ = _offered_stay(db_conn, offer=_INDONESIAN_OFFER)
    _answer(db_conn, conversation_id, "ok")

    decision = _typed(db_conn, conversation_id, "ok")

    assert isinstance(decision, PassOn)
    assert decision.language == "id"


def test_anything_but_a_bare_yes_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _ = _offered_stay(db_conn)
    _answer(db_conn, conversation_id, "ok thanks")

    assert _typed(db_conn, conversation_id, "ok thanks") is None


def test_a_yes_to_a_reply_that_did_not_offer_the_booking_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _ = _offered_stay(db_conn, offer="Test Hotel, 2 nights.")
    _answer(db_conn, conversation_id, "ok")

    assert _typed(db_conn, conversation_id, "ok") is None


def test_a_yes_after_a_clarifying_exchange_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The latest reply offers the booking again, but it is not the
    quote's own reply."""
    conversation_id, _ = _offered_stay(db_conn)
    _message(
        db_conn, conversation_id, direction="inbound", body="breakfast?", minutes_ago=10
    )
    _message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=_ENGLISH_OFFER,
        minutes_ago=9,
        whatsapp_message_id="wamid.OFFER-AGAIN",
    )
    _answer(db_conn, conversation_id, "ok")

    assert _typed(db_conn, conversation_id, "ok") is None


def test_a_yes_followed_by_another_message_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _ = _offered_stay(db_conn)
    _answer(db_conn, conversation_id, "ok", minutes_ago=2)
    _answer(
        db_conn,
        conversation_id,
        "wait, 3 nights",
        whatsapp_message_id="wamid.LATER",
        minutes_ago=1,
    )

    assert _typed(db_conn, conversation_id, "ok") is None


def test_a_yes_that_is_not_the_message_answering_the_offer_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _ = _offered_stay(db_conn)
    _answer(db_conn, conversation_id, "ok")

    decision = _typed(db_conn, conversation_id, "ok", whatsapp_message_id="wamid.NOT")

    assert decision is None


def test_a_typed_yes_after_the_quote_expired_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _ = _offered_stay(db_conn, quoted=45)
    _answer(db_conn, conversation_id, "ok")

    assert _typed(db_conn, conversation_id, "ok") is None


def test_a_typed_yes_to_two_stays_offered_at_once_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _message(db_conn, conversation_id, direction="inbound", body="?", minutes_ago=20)
    _quote(db_conn, conversation_id, minutes_ago=19.5)
    _quote(db_conn, conversation_id, minutes_ago=19)
    _message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=_ENGLISH_OFFER,
        minutes_ago=18,
        whatsapp_message_id=_OFFER_ID,
    )
    _answer(db_conn, conversation_id, "ok")

    assert _typed(db_conn, conversation_id, "ok") is None


def test_a_typed_yes_with_no_quote_is_left_to_the_model(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _answer(db_conn, conversation_id, "ok")

    assert _typed(db_conn, conversation_id, "ok") is None


def test_every_read_runs_as_hotel_agent(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    conversation_id, quote_id = _offered_stay(db_conn)
    _newer_offer(db_conn, conversation_id)
    _answer(db_conn, conversation_id, "ok", whatsapp_message_id="wamid.TYPED")
    tap = ButtonTap(
        button_id=button_id("yes", quote_id),
        title="Yes, confirm",
        context_message_id=_OFFER_ID,
    )

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        tapped = decide_booking_yes(
            agent,
            CustomerMessage(whatsapp_message_id=_ANSWER_ID, text=tap.title, button=tap),
            conversation_id=conversation_id,
            quote_validity=_VALIDITY,
        )
        typed = _typed(agent, conversation_id, "ok", whatsapp_message_id="wamid.TYPED")

    assert isinstance(tapped, OfferNewerPrice)
    assert isinstance(typed, PassOn)
