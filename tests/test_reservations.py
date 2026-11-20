"""The reservation tools against the fake API: what they send, what they say, and that none of them pays.

No network, no Stripe, no money. `api.reservation_calls` holds each request the
server made, with its headers and body.
"""
import json
import re
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from fake_api import CHECKOUT, GRANTED_REFUSAL, reservation, rid
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from support import connected, stdio_server, text_of

from phone_harness_mcp import reservations
from phone_harness_mcp.cli import build_http_app
from phone_harness_mcp.cloud import local_credentials
from phone_harness_mcp.server import INSTRUCTIONS, build_server, stdio_cloud

pytestmark = pytest.mark.anyio

USE_CASE = "Testing our listings app on real iPhones."
RESERVE = {"phone_type": "iphone", "plan": "monthly", "quantity": 2, "use_case": USE_CASE}
TOOLS = ("phone_reserve", "phone_list_reservations", "phone_cancel_reservation")
KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


async def call(session, tool, **args):
    return await session.call_tool(tool, args)


def key_in(text):
    return re.search(r'idempotency_key="([^"]+)"', text).group(1)


# --- what the tools are -------------------------------------------------------------


async def test_three_tools_that_say_they_never_pay(api):
    async with connected(stdio_server(api)) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert set(TOOLS) <= set(tools)
    reserve, listing, cancel = (tools[name] for name in TOOLS)
    schema = reserve.input_schema
    assert schema["required"] == ["phone_type", "plan", "quantity", "use_case"]
    assert schema["properties"]["phone_type"]["enum"] == ["iphone", "iphone_esim", "android"]
    assert schema["properties"]["plan"]["enum"] == ["monthly", "annual"]
    assert {"first_name", "last_name", "idempotency_key"} <= set(schema["properties"])
    assert "email" not in schema["properties"]                 # the account's address is the API's to fill in
    assert "never pays" in reserve.description and "a person must open the link" in reserve.description
    assert reserve.annotations.read_only_hint is False and reserve.annotations.destructive_hint is False
    assert listing.annotations.read_only_hint is True
    assert "required" not in listing.input_schema               # no id lists them all
    assert cancel.annotations.destructive_hint is True and cancel.annotations.idempotent_hint is True
    assert cancel.input_schema["required"] == ["reservation_id"]
    assert "only when the user has asked" in cancel.description
    for tool in (reserve, listing, cancel):
        assert "$" not in tool.description                      # every amount comes from the API with the quote
    assert "only a person can pay it" in INSTRUCTIONS


# --- reserving ----------------------------------------------------------------------


async def test_reserve_sends_the_request_the_bearer_and_a_key_of_its_own(api):
    async with connected(stdio_server(api)) as s:
        first = await call(s, "phone_reserve", **RESERVE)
        second = await call(s, "phone_reserve", **RESERVE)
    assert not first.is_error and not second.is_error, text_of(first) + text_of(second)
    (method, path, headers, body), (_, _, again, _) = api.reservation_calls
    assert (method, path) == ("POST", "/reservations")
    assert body == RESERVE
    assert headers["authorization"] == "Bearer pck_test"
    assert headers["content-type"] == "application/json"
    assert KEY.match(headers["idempotency-key"])
    assert again["idempotency-key"] != headers["idempotency-key"]        # one key for each call
    assert len(api.reservations) == 2
    assert key_in(text_of(first)) == headers["idempotency-key"]


async def test_the_result_is_the_link_the_quote_and_that_a_person_must_pay(api):
    async with connected(stdio_server(api)) as s:
        text = text_of(await call(s, "phone_reserve", **{**RESERVE, "quantity": 3}))
    assert f"Started reservation {rid(1)}: 3 x Cloud iPhone, monthly." in text
    assert "It holds no place in the queue yet: nothing is reserved until the deposit is paid." in text
    assert "A person has to open this Stripe Checkout link and pay the deposit." in text
    assert "This tool cannot pay, and you must not pay or enter card details for them:" in text
    assert CHECKOUT + "000001" in text.splitlines()
    assert "The link works for another 24 hours." in text
    assert "- Deposit due now: $60.00 (3 x $20.00), refundable in full while the reservation is queued." in text
    assert ("- First month, charged when the phones are handed over: 1 x $50.00 (intro price, once per customer) "
            "+ 2 x $80.00 (standard price) = $210.00. The deposit comes off it, leaving $150.00.") in text
    assert "You are in the queue" not in text and "position" not in text.lower()


async def test_an_annual_plan_and_a_phone_with_no_price_yet(api):
    async with connected(stdio_server(api)) as s:
        annual = text_of(await call(s, "phone_reserve", phone_type="iphone_esim", plan="annual", quantity=2,
                                    use_case=USE_CASE))
        android = text_of(await call(s, "phone_reserve", phone_type="android", plan="monthly", quantity=1,
                                     use_case=USE_CASE))
    assert "2 x Cloud iPhone with eSIM, annual" in annual
    assert ("First year, charged when the phones are handed over: 1 x $624.00 (intro price, once per customer) "
            "+ 1 x $960.00 (standard price) = $1,584.00. The deposit comes off it, leaving $1,544.00.") in annual
    assert "- Deposit due now: $20.00 (1 x $20.00), refundable in full while the reservation is queued." in android
    assert "- First month: the price is confirmed before the phones are handed over." in android
    assert api.reservation_calls[0][3]["phone_type"] == "iphone_esim"


@pytest.mark.parametrize("change, needle", [
    ({"quantity": 0}, "quantity must be between 1 and 50"),
    ({"quantity": 51}, "quantity must be between 1 and 50"),
    ({"quantity": -2}, "quantity must be between 1 and 50"),
    ({"quantity": "many"}, "quantity"),
    ({"quantity": None}, "quantity"),
    ({"use_case": ""}, "use_case is required"),
    ({"use_case": "   "}, "use_case is required"),
    ({"use_case": "x" * 2001}, "the most is 2000"),
    ({"phone_type": "ipad"}, "'iphone', 'iphone_esim' or 'android'"),
    ({"plan": "weekly"}, "'monthly' or 'annual'"),
    ({"idempotency_key": "short"}, "16 to 128"),
    ({"idempotency_key": "has spaces in the key!"}, "16 to 128"),
])
async def test_arguments_are_checked_before_anything_is_sent(api, change, needle):
    args = {k: v for k, v in {**RESERVE, **change}.items() if v is not None}
    async with connected(stdio_server(api)) as s:
        r = await call(s, "phone_reserve", **args)
    assert r.is_error and needle in text_of(r), text_of(r)
    assert api.reservation_calls == [] and api.reservations == {}


async def test_a_key_that_is_given_repeats_the_same_reservation(api):
    key = "retry-key_0123456789"
    async with connected(stdio_server(api)) as s:
        first = await call(s, "phone_reserve", **RESERVE, idempotency_key=key)
        again = await call(s, "phone_reserve", **RESERVE, idempotency_key=key)
        other = await call(s, "phone_reserve", **{**RESERVE, "quantity": 5}, idempotency_key=key)
    assert not first.is_error and not again.is_error
    assert [c[2]["idempotency-key"] for c in api.reservation_calls] == [key, key, key]
    assert f"Started reservation {rid(1)}" in text_of(first) and f"Started reservation {rid(1)}" in text_of(again)
    assert other.is_error and "already used for a different request" in text_of(other)
    assert len(api.reservations) == 1


async def test_a_key_whose_deposit_is_already_paid_says_where_it_stands(api):
    key = "paid-already_0123456789"
    row = api.hold(reservation(1, quantity=2, status="queued", position=4, paid_days_ago=2))
    api.reservation_keys[("u_1", key)] = (RESERVE, row["reservation_id"])
    async with connected(stdio_server(api)) as s:
        text = text_of(await call(s, "phone_reserve", **RESERVE, idempotency_key=key))
    assert "already has a reservation; no new one was started" in text
    assert "Status: queued." in text and "Position 4 in the queue." in text
    assert "checkout.stripe.com" not in text and "Started reservation" not in text


async def test_a_name_is_sent_when_given_and_asked_for_when_the_account_has_none(api):
    api.name_on_file = False
    async with connected(stdio_server(api)) as s:
        bare = await call(s, "phone_reserve", **RESERVE)
        named = await call(s, "phone_reserve", **RESERVE, first_name=" Dana ", last_name="Okafor")
    assert bare.is_error
    for needle in ("A first name is required", "ask the user for their first and last name",
                   "first_name and last_name", "Do not invent one"):
        assert needle in text_of(bare)
    assert not named.is_error, text_of(named)
    assert api.reservation_calls[-1][3] == {**RESERVE, "first_name": "Dana", "last_name": "Okafor"}
    assert all("email" not in c[3] for c in api.reservation_calls)
    assert len(api.reservations) == 1


async def test_a_lost_answer_is_asked_for_again_under_the_same_key(api):
    api.reservation_drop = 1
    async with connected(stdio_server(api)) as s:
        r = await call(s, "phone_reserve", **RESERVE)
    assert not r.is_error, text_of(r)
    first, second = api.reservation_calls
    assert first[2]["idempotency-key"] == second[2]["idempotency-key"] and first[3] == second[3]
    assert len(api.reservations) == 1                                    # one reservation, not two


async def test_an_api_that_cannot_be_reached_says_how_to_continue(api):
    api.reservation_drop = 2
    async with connected(stdio_server(api)) as s:
        r = await call(s, "phone_reserve", **RESERVE)
    text = text_of(r)
    assert r.is_error and "could not reach" in text
    assert key_in(text) == api.reservation_calls[0][2]["idempotency-key"]
    assert "instead of starting a second reservation" in text


@pytest.mark.parametrize("fail, needles", [
    ((403, {"error": "Reservations are not open yet.", "code": "reservations_closed"}, None),
     ["Reservations are not open yet."]),
    ((403, {"error": "That phone and plan cannot be reserved yet.", "code": "plan_unavailable"}, None),
     ["That phone and plan cannot be reserved yet."]),
    ((429, {"error": "Too many unfinished reservations. Finish one, or try again in an hour.",
            "code": "rate_limited", "retry_after_seconds": 3600}, {"Retry-After": "3600"}),
     ["Too many unfinished reservations"]),
    ((403, {"error": "this sign-in can drive phones, not manage the account; use the dashboard or an API key",
            "code": "scope_limited"}, None), ["use the dashboard or an API key"]),
    ((400, {"error": "Choose between 1 and 50 phones"}, None), ["Choose between 1 and 50 phones"]),
    ((502, {"error": "Stripe could not complete this request; try again shortly.", "code": "payments_unavailable"},
      None), ["Stripe could not complete this request", "Nothing was charged", 'idempotency_key="']),
    ((500, {"error": "The phone service could not complete this request."}, None),
     ["answered 500", "could not complete this request"]),
])
async def test_a_refusal_to_reserve_is_passed_on_in_the_apis_words(api, fail, needles):
    api.reservation_fail = fail
    async with connected(stdio_server(api)) as s:
        r = await call(s, "phone_reserve", **RESERVE)
    assert r.is_error
    for needle in needles:
        assert needle in text_of(r), text_of(r)
    assert api.reservations == {}


async def test_an_api_from_before_reservations_says_so(api):
    """Signed in, an unknown route is a 404. That is not "no such reservation"."""
    async with connected(stdio_server(api)) as s:
        api.reservation_fail = (404, {"error": "no route POST /reservations"}, None)
        made = await call(s, "phone_reserve", **RESERVE)
        api.reservation_fail = (404, {"error": "no route GET /reservations"}, None)
        listed = await call(s, "phone_list_reservations")
    for r in (made, listed):
        assert r.is_error and "does not take reservations yet" in text_of(r)
        assert "No reservation" not in text_of(r) and "No such" not in text_of(r)


@pytest.mark.parametrize("answer", [{}, {"ok": True}, []])
async def test_an_answer_that_is_not_a_reservation_is_not_passed_off_as_one(api, answer):
    api.hold(reservation(1, status="queued", position=1, paid_days_ago=1))
    async with connected(stdio_server(api)) as s:
        api.reservation_fail = (200, answer, None)
        made = await call(s, "phone_reserve", **RESERVE)
        api.reservation_fail = (200, answer, None)
        read = await call(s, "phone_list_reservations", reservation_id=rid(1))
        api.reservation_fail = (200, answer, None)
        cancel = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
    for r in (made, read, cancel):
        assert r.is_error and "something that is not a reservation" in text_of(r), text_of(r)
    assert 'idempotency_key="' in text_of(made)
    assert "Started reservation" not in text_of(made) and "Cancelled" not in text_of(cancel)


@pytest.mark.parametrize("bad", [
    "https://checkout.stripe.com.example.net/pay", "http://checkout.stripe.com/c/pay/x",
    "https://checkout.stripe.com@example.net/pay", "file:///etc/passwd",
    "https://example.net/?next=https://checkout.stripe.com/",
])
async def test_a_link_that_is_not_stripe_checkout_is_never_handed_over(api, bad):
    api.checkout = bad
    async with connected(stdio_server(api)) as s:
        made = await call(s, "phone_reserve", **RESERVE)
        read = await call(s, "phone_list_reservations", reservation_id=rid(1))
    assert made.is_error and "is not a Stripe Checkout page" in text_of(made)
    assert "is not a Stripe Checkout page" in text_of(read)
    assert bad not in text_of(made) and bad not in text_of(read)
    assert "A person has to open" not in text_of(read)


# --- listing ------------------------------------------------------------------------


async def test_the_list_says_where_each_one_stands(api):
    api.hold(
        reservation(1, "iphone", "monthly", 3, "queued", position=4, paid_days_ago=2),
        reservation(2, "iphone_esim", "annual", 1, "pending_payment"),
        reservation(3, "android", "monthly", 2, "cancelled", paid_days_ago=5),
        reservation(4, "iphone", "monthly", 3, "queued", position=1, granted=1, paid_days_ago=9),
        reservation(5, "iphone", "annual", 1, "granted", granted=1, paid_days_ago=30),
        reservation(6, "iphone", "monthly", 2, "cancelling", paid_days_ago=1),
        reservation(7, "iphone", "monthly", 1, "cancelled"),
    )
    async with connected(stdio_server(api)) as s:
        text = text_of(await call(s, "phone_list_reservations"))
        as_json = json.loads(text_of(await call(s, "phone_list_reservations", format="json")))
    head, *lines = text.splitlines()
    assert head == "7 reservations, newest first:"
    assert [line.split(":")[0] for line in lines] == [f"- {rid(n)}" for n in (7, 6, 5, 4, 3, 2, 1)]
    by_id = {line.split(":")[0][2:]: line.split(": ", 1)[1] for line in lines}
    assert by_id[rid(1)] == ("3 x Cloud iPhone, monthly. queued, position 4 in the queue; "
                             "deposit of $60.00 paid 2 days ago, $60.00 refundable.")
    assert by_id[rid(2)] == ("1 x Cloud iPhone with eSIM, annual. pending_payment; deposit of $20.00 not paid. "
                             "It holds no place until a person pays; pass its reservation_id to get the "
                             "Checkout link.")
    assert by_id[rid(3)] == ("2 x Cloud Android, monthly. cancelled; deposit of $40.00 paid 5 days ago, "
                             "$40.00 refunded.")
    assert by_id[rid(4)] == ("3 x Cloud iPhone, monthly. queued, position 1 in the queue, 1 of 3 handed over, "
                             "2 still waiting; deposit of $60.00 paid 9 days ago, $40.00 refundable, "
                             "$20.00 went onto a first bill.")
    assert by_id[rid(5)] == ("1 x Cloud iPhone, annual. granted, 1 of 1 handed over; deposit of $20.00 paid "
                             "30 days ago, $20.00 went onto a first bill.")
    assert by_id[rid(6)] == ("2 x Cloud iPhone, monthly. cancelling; deposit of $40.00 paid 1 day ago, "
                             "$40.00 refund not confirmed yet.")
    assert by_id[rid(7)] == "1 x Cloud iPhone, monthly. cancelled; nothing was charged."
    assert "checkout.stripe.com" not in text                   # a link is given with one reservation, not in the list
    assert [row["reservation_id"] for row in as_json] == [rid(n) for n in (7, 6, 5, 4, 3, 2, 1)]
    assert as_json[-1]["deposit"]["paid_cents"] == 6000 and "uid" not in as_json[-1]
    assert [c[:2] for c in api.reservation_calls] == [("GET", "/reservations")] * 2
    assert api.reservation_calls[0][2]["authorization"] == "Bearer pck_test"


async def test_no_reservations(api):
    async with connected(stdio_server(api)) as s:
        assert text_of(await call(s, "phone_list_reservations")) == \
            "No reservations on this account. phone_reserve starts one."
        assert json.loads(text_of(await call(s, "phone_list_reservations", format="json"))) == []
        api.hold(reservation(1, status="queued", position=1, paid_days_ago=1))
        assert text_of(await call(s, "phone_list_reservations")).splitlines()[0] == "1 reservation:"


async def test_one_reservation_in_full(api):
    api.hold(reservation(1, quantity=3, status="queued", position=4, paid_days_ago=2), reservation(2, quantity=2),
             reservation(3, quantity=3, status="queued", position=1, granted=1, paid_days_ago=9))
    async with connected(stdio_server(api)) as s:
        queued = text_of(await call(s, "phone_list_reservations", reservation_id=rid(1)))
        unpaid = text_of(await call(s, "phone_list_reservations", reservation_id=rid(2)))
        partly = text_of(await call(s, "phone_list_reservations", reservation_id=rid(3)))
        as_json = json.loads(text_of(await call(s, "phone_list_reservations", reservation_id=rid(2), format="json")))
        missing = await call(s, "phone_list_reservations", reservation_id=rid(99))
    assert api.reservation_calls[0][:2] == ("GET", "/reservations/" + rid(1))
    assert queued.splitlines() == [
        f"Reservation {rid(1)}: 3 x Cloud iPhone, monthly.",
        "Status: queued. You are in the queue. Cancel any time and the deposit comes back in full.",
        "Position 4 in the queue.",
        "Deposit of $60.00 paid 2 days ago, $60.00 refundable.",
        ("First month, charged when the phones are handed over: 1 x $50.00 (intro price, once per customer) + "
         "2 x $80.00 (standard price) = $210.00. The deposit comes off it, leaving $150.00."),
        "phone_cancel_reservation gives it up and refunds the deposit still held."]
    assert "Status: pending_payment." in unpaid
    assert "A person has to open this Stripe Checkout link and pay the deposit." in unpaid
    assert CHECKOUT + "000002" in unpaid.splitlines()
    assert "- Deposit due now: $40.00 (2 x $20.00), refundable in full while the reservation is queued." in unpaid
    assert "1 of 3 handed over, 2 still waiting." in partly
    assert "Deposit of $60.00 paid 9 days ago, $40.00 refundable, $20.00 went onto a first bill." in partly
    assert as_json["reservation_id"] == rid(2) and as_json["checkout_url"] == CHECKOUT + "000002"
    assert missing.is_error and f"No reservation {rid(99)} on this account" in text_of(missing)


@pytest.mark.parametrize("bad", ["../sessions", "rsv_", "rsv_abc/cancel", "rsv_abc?x=1", " ", "sid_0001"])
async def test_an_id_that_is_not_one_is_refused_before_anything_is_sent(api, bad):
    async with connected(stdio_server(api)) as s:
        read = await call(s, "phone_list_reservations", reservation_id=bad)
        cancel = await call(s, "phone_cancel_reservation", reservation_id=bad)
    assert read.is_error and cancel.is_error
    assert "reservation_id is an id like rsv_" in text_of(cancel)
    assert api.reservation_calls == []


# --- cancelling ---------------------------------------------------------------------


async def test_cancel_refunds_the_deposit_and_says_how_much(api):
    api.hold(reservation(1, quantity=2, status="queued", position=4, paid_days_ago=2))
    async with connected(stdio_server(api)) as s:
        done = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
        again = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
    assert not done.is_error, text_of(done)
    method, path, headers, body = api.reservation_calls[0]
    assert (method, path, body) == ("POST", f"/reservations/{rid(1)}/cancel", {})
    assert headers["authorization"] == "Bearer pck_test"
    assert text_of(done) == (f"Cancelled reservation {rid(1)}. $40.00 is being refunded to the card that paid. "
                             "You left the queue and the deposit was refunded in full.")
    assert text_of(again) == (f"Reservation {rid(1)} was already cancelled: $40.00 was refunded before. "
                              "Nothing more to refund.")            # twice refunds once
    assert api.reservations[rid(1)]["deposit"]["refunded_cents"] == 4000


async def test_cancel_of_an_unpaid_reservation_has_nothing_to_refund(api):
    api.hold(reservation(1, quantity=3))
    async with connected(stdio_server(api)) as s:
        text = text_of(await call(s, "phone_cancel_reservation", reservation_id=rid(1)))
    assert text == (f"Reservation {rid(1)} is cancelled. Its deposit was never paid, so nothing was charged "
                    "or refunded.")
    assert api.reservations[rid(1)]["status"] == "cancelled"


async def test_cancel_after_the_grant_is_refused_in_the_apis_words(api):
    api.hold(reservation(1, status="granted", granted=1, paid_days_ago=20))
    async with connected(stdio_server(api)) as s:
        r = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
    assert r.is_error
    assert f"Not cancelled. {GRANTED_REFUSAL} (already_granted)" in text_of(r)
    assert api.reservations[rid(1)]["status"] == "granted"
    assert api.reservations[rid(1)]["deposit"]["refunded_cents"] == 0


async def test_a_plan_that_has_ended_says_so_and_cannot_be_cancelled(api):
    api.hold(reservation(1, quantity=2, status="ended", granted=2, paid_days_ago=90))
    async with connected(stdio_server(api)) as s:
        one = text_of(await call(s, "phone_list_reservations", reservation_id=rid(1)))
        listed = text_of(await call(s, "phone_list_reservations"))
        r = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
    assert one.splitlines() == [
        f"Reservation {rid(1)}: 2 x Cloud iPhone, monthly.",
        "Status: ended. The plan for this phone has ended.",
        "2 of 2 handed over, the plan has ended for 2.",
        "Deposit of $40.00 paid 90 days ago, $40.00 went onto a first bill."]
    assert "ended, 2 of 2 handed over, the plan has ended for 2;" in listed
    assert r.is_error and f"Not cancelled. {GRANTED_REFUSAL} (already_granted)" in text_of(r)


async def test_cancel_with_part_handed_over_refunds_only_what_is_still_waiting(api):
    api.hold(reservation(1, quantity=3, status="queued", position=1, granted=1, paid_days_ago=9))
    async with connected(stdio_server(api)) as s:
        text = text_of(await call(s, "phone_cancel_reservation", reservation_id=rid(1)))
    assert text == (f"Left the queue for the phones still waiting on {rid(1)}. $40.00 is being refunded to the "
                    "card that paid. 1 phone handed over. You left the queue for the other 2, and that part of "
                    "the deposit was refunded.")


async def test_a_refund_still_owed_is_asked_for_again(api):
    api.hold(reservation(1, quantity=2, status="cancelling", paid_days_ago=3))
    async with connected(stdio_server(api)) as s:
        text = text_of(await call(s, "phone_cancel_reservation", reservation_id=rid(1)))
    assert text.startswith(f"Cancelled reservation {rid(1)}. $40.00 is being refunded")


@pytest.mark.parametrize("fail, needles", [
    ((409, {"error": "Your phone is being handed over right now. Try again in a few minutes.",
            "code": "grant_in_progress", "retry_after_seconds": 120}, {"Retry-After": "120"}),
     ["Not cancelled. Your phone is being handed over right now. Try again in a few minutes."]),
    ((502, {**reservation(1, quantity=2, status="cancelling", paid_days_ago=3), "code": "refund_unconfirmed",
            "error": "You have left the queue, but the refund could not be started. Try again in a few minutes."},
      None), ["the refund could not be started", "phone_cancel_reservation again", "never sent twice"]),
    ((502, {"error": "Stripe could not complete this request; try again shortly.", "code": "payments_unavailable"},
      None), ["Stripe could not complete this request", "Nothing changed", "phone_cancel_reservation again"]),
])
async def test_a_cancel_that_did_not_finish_says_what_to_do(api, fail, needles):
    api.hold(reservation(1, quantity=2, status="queued", position=1, paid_days_ago=3))
    api.reservation_fail = fail
    async with connected(stdio_server(api)) as s:
        r = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
    assert r.is_error
    for needle in needles:
        assert needle in text_of(r), text_of(r)
    assert 'idempotency_key="' not in text_of(r)                # that is for reserving, not for this


# --- whose they are -----------------------------------------------------------------


async def test_another_accounts_reservation_is_not_there(api):
    api.tokens["pck_other"] = dict(api.tokens["pck_test"], uid="u_2")       # a second account
    api.hold(reservation(1, status="queued", position=1, paid_days_ago=1, uid="u_2"))
    async with connected(stdio_server(api)) as s:
        listed = text_of(await call(s, "phone_list_reservations"))
        read = await call(s, "phone_list_reservations", reservation_id=rid(1))
        cancel = await call(s, "phone_cancel_reservation", reservation_id=rid(1))
    assert listed.startswith("No reservations on this account.")
    assert read.is_error and cancel.is_error
    assert f"No reservation {rid(1)} on this account" in text_of(cancel)
    assert api.reservations[rid(1)]["status"] == "queued"
    async with connected(stdio_server(api, token="pck_other")) as s:
        assert rid(1) in text_of(await call(s, "phone_list_reservations"))


async def test_not_signed_in_sends_nothing(api, monkeypatch, tmp_path):
    monkeypatch.delenv("PHONE_HARNESS_API_KEY", raising=False)
    monkeypatch.setenv("PHONE_HARNESS_HOME", str(tmp_path))
    server = build_server(cloud_for=stdio_cloud(local_credentials(), api.url), allow_files=False)
    async with connected(server) as s:
        results = [await call(s, "phone_reserve", **RESERVE), await call(s, "phone_list_reservations"),
                   await call(s, "phone_list_reservations", reservation_id=rid(1)),
                   await call(s, "phone_cancel_reservation", reservation_id=rid(1))]
    for r in results:
        assert r.is_error and "Not signed in to Phone Harness Cloud" in text_of(r)
    assert api.requests == [] and api.reservation_calls == []   # never an anonymous reservation


async def test_a_refused_credential_says_so(api):
    async with connected(stdio_server(api, token="pck_wrong")) as s:
        results = [await call(s, "phone_reserve", **RESERVE), await call(s, "phone_list_reservations"),
                   await call(s, "phone_cancel_reservation", reservation_id=rid(1))]
    for r in results:
        assert r.is_error and "refused the credential" in text_of(r)
    assert api.reservations == {}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hosted(api):
    port = free_port()
    app = build_http_app(api=api.url, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None, allowed_origins=None,
                         public_url=None, oauth_issuer=None, stateful=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


async def test_hosted_each_caller_reserves_and_cancels_as_themselves(hosted, api):
    api.tokens["pck_other"] = dict(api.tokens["pck_test"], uid="u_2")       # a second account

    async def as_caller(bearer, steps):
        async with (httpx2.AsyncClient(headers={"Authorization": f"Bearer {bearer}"}, timeout=30) as http,
                    streamable_http_client(f"{hosted}/mcp", http_client=http) as (read, write, *_rest),
                    ClientSession(read, write) as s):
            await s.initialize()
            return [await s.call_tool(tool, args) for tool, args in steps]

    made, mine = await as_caller("pck_test", [("phone_reserve", RESERVE), ("phone_list_reservations", {})])
    assert not made.is_error and CHECKOUT + "000001" in text_of(made)
    assert rid(1) in text_of(mine)
    theirs, refused = await as_caller("pck_other", [("phone_list_reservations", {}),
                                                    ("phone_cancel_reservation", {"reservation_id": rid(1)})])
    assert text_of(theirs).startswith("No reservations on this account.")
    assert refused.is_error and f"No reservation {rid(1)} on this account" in text_of(refused)
    [gone] = await as_caller("pck_test", [("phone_cancel_reservation", {"reservation_id": rid(1)})])
    assert not gone.is_error and "nothing was charged or refunded" in text_of(gone)
    assert [c[2]["authorization"] for c in api.reservation_calls] == [
        "Bearer pck_test", "Bearer pck_test", "Bearer pck_other", "Bearer pck_other", "Bearer pck_test"]


# --- the wording helpers ------------------------------------------------------------


def test_money_is_whatever_currency_the_api_names():
    assert reservations._amount(2000) == "$20.00"
    assert reservations._amount(201600, "usd") == "$2,016.00"
    assert reservations._amount(2000, "eur") == "20.00 EUR"
    assert reservations._amount(None) == "?"


def test_a_quote_is_only_what_the_api_sent():
    used = reservation(1, quantity=2, intro=False)              # the intro phone was used before
    assert reservations._quote(used) == (
        "Deposit due now: $40.00 (2 x $20.00), refundable in full while the reservation is queued.",
        ("First month, charged when the phones are handed over: 2 x $80.00 (standard price) = $160.00. "
         "The deposit comes off it, leaving $120.00."))
    assert reservations._quote({}) == (
        None, "First billing period: the price is confirmed before the phones are handed over.")


def test_a_checkout_link_has_to_be_stripes():
    good = "https://checkout.stripe.com/c/pay/cs_live_a1B2#fragment"
    assert reservations.checkout_url({"checkout_url": good}) == good
    for bad in (None, "", 7, "https://checkout.stripe.com/c/pay/x\n", "https://CHECKOUT.stripe.com.evil.example/",
                "https://user@checkout.stripe.com/pay", "javascript:alert(1)", "https://[bad"):
        assert reservations.checkout_url({"checkout_url": bad}) is None, bad


def test_an_expired_link_says_to_reserve_again():
    row = {**reservation(1), "expires_at": time.time() - 60}
    assert reservations._pay_lines(row) == [
        reservations.PAY, row["checkout_url"],
        "The link has expired. If it no longer opens, reserve again for a new one."]
    assert reservations._pay_lines({**row, "checkout_url": None}) == [
        "There is no payment link for it yet. Ask again with its reservation_id in a moment."]
