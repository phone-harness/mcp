"""The reservation tools against a real phone-cloud running on this machine.

Skipped unless PHONE_CLOUD_LOCAL_STATE names the state file that phone-cloud's
tests/local_reservations.py writes. That script is the real API process on a
throwaway PostgreSQL with Stripe faked, so nothing here reaches Stripe, a card
or a phone. Beside the API it gives a "control" address that does what only
Stripe, or a person at Stripe's page, can do: pay a Checkout, let one run out,
end a plan. test_reservations.py is the same tools against a fake API.

    # in a phone-cloud checkout that has the reservation API
    python3 tests/local_reservations.py --port 8793 --control 8794 --state /tmp/local-cloud.json
    # here
    PHONE_CLOUD_LOCAL_STATE=/tmp/local-cloud.json uv run pytest tests/test_reservations_live.py

The tools are driven both ways the server runs: wired to one credential, as
over stdio, and hosted, where each HTTP request brings its own bearer. Every
amount asserted is read from the API's own answers; this file names none.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from support import connected, text_of

from phone_harness_mcp.cli import build_http_app
from phone_harness_mcp.cloud import StaticToken
from phone_harness_mcp.reservations import _amount
from phone_harness_mcp.server import build_server, stdio_cloud

STATE = os.environ.get("PHONE_CLOUD_LOCAL_STATE")
pytestmark = [pytest.mark.anyio,
              pytest.mark.skipif(not STATE, reason="set PHONE_CLOUD_LOCAL_STATE to the state file of a local "
                                                   "phone-cloud (its tests/local_reservations.py)")]

USE_CASE = "An integration run of the Phone Harness MCP server."
RESERVE = {"phone_type": "iphone", "plan": "monthly", "quantity": 2, "use_case": USE_CASE}
RID = re.compile(r"rsv_[a-f0-9]{24}")


class Local:
    """The local phone-cloud: its API, its operator, and the stand-in for Stripe's side."""

    def __init__(self, path):
        self.state = json.loads(Path(path).read_text())
        self.api, self.control = self.state["api"], self.state["control"]

    @staticmethod
    def http(method, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def operator(self, method, path, body=None, key=None):
        headers = {"Authorization": "Bearer " + self.state["operator_token"],
                   "X-Phone-Cloud-Actor": self.state["operator_actor"]}
        if key:
            headers["Idempotency-Key"] = key
        return self.http(method, self.api + path, body, headers)

    def account(self, name="Dana Okafor"):
        """A new account and an API key for it. `name` None is an account with no name on file."""
        # An address of its own, at the made-up domain this local cloud's accounts use.
        domain = self.state["accounts"][0]["email"].partition("@")[2]
        code, made = self.operator("POST", "/admin/users", {
            "email": f"mcp-{uuid.uuid4().hex[:12]}@{domain}", "name": name})
        assert code == 201, (code, made)
        code, key = self.operator("POST", f"/admin/users/{made['uid']}/keys", {"label": "mcp integration"})
        assert code == 201, (code, key)
        return key["key"]

    def pay(self, rid):
        """A person pays the deposit on Stripe's page, and Stripe's webhook lands."""
        code, paid = self.http("POST", f"{self.control}/pay/{rid}")
        assert (code, paid.get("webhook")) == (200, 200), (code, paid)

    def expire(self, rid):
        code, gone = self.http("POST", f"{self.control}/expire/{rid}")
        assert (code, gone.get("webhook")) == (200, 200), (code, gone)

    def queue_row(self, rid):
        code, listed = self.operator("GET", "/admin/reservations")
        assert code == 200, (code, listed)
        return next(row for row in listed["queue"] if row["id"] == rid)

    def grant(self, rid, phones=None):
        """The operator hands phones over: the plan starts and their deposit comes off its first bill."""
        body = {"confirm": self.queue_row(rid)["confirm"], "dry_run": False}
        if phones is not None:
            body["phones"] = phones
        code, done = self.operator("POST", f"/admin/reservations/{rid}/grant", body,
                                   key="mcp-grant-" + uuid.uuid4().hex)
        assert code == 200 and done.get("granted") is True, (code, done)
        return done

    def end_plan(self, subscription):
        code, ended = self.http("POST", f"{self.control}/subscription/{subscription}/cancel")
        assert (code, ended.get("webhook")) == (200, 200), (code, ended)

    def refunds(self, rid):
        code, stripe = self.http("GET", self.control + "/stripe")
        assert code == 200, code
        return [r for r in stripe["refunds"] if (r.get("metadata") or {}).get("reservation_id") == rid]


@pytest.fixture(scope="module")
def local():
    cloud = Local(STATE)
    code, health = cloud.http("GET", cloud.api + "/health")
    assert (code, health.get("ok")) == (200, True), "the local phone-cloud is not answering"
    return cloud


@pytest.fixture(scope="module")
def hosted(local):
    """The hosted server in front of the local API: no credential of its own."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = build_http_app(api=local.api, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None,
                         allowed_origins=None, public_url=None, oauth_issuer=None, stateful=False)
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


@pytest.fixture(params=["stdio", "hosted"])
def both(request, local, hosted):
    return _client(request.param, local, hosted)


@pytest.fixture
def client(local, hosted):
    return _client("stdio", local, hosted)


def _client(mode, local, hosted):
    @asynccontextmanager
    async def signed_in(key):
        if mode == "stdio":
            server = build_server(cloud_for=stdio_cloud(StaticToken(key), local.api), allow_files=False)
            async with connected(server) as session:
                yield session
            return
        async with (httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=90) as http,
                    streamable_http_client(f"{hosted}/mcp", http_client=http) as (read, write, *_rest),
                    ClientSession(read, write) as session):
            await session.initialize()
            yield session
    return signed_in


async def call(session, tool, **args):
    return await session.call_tool(tool, args)


async def read(session, rid):
    r = await call(session, "phone_list_reservations", reservation_id=rid, format="json")
    assert not r.is_error, text_of(r)
    return json.loads(text_of(r))


async def reserve(session, **change):
    """Reserve through the tool; -> (the reservation as the API holds it, the tool's text)."""
    r = await call(session, "phone_reserve", **{**RESERVE, **change})
    assert not r.is_error, text_of(r)
    text = text_of(r)
    return await read(session, RID.search(text).group(0)), text


async def test_reserve_pay_list_and_cancel_with_the_deposit_refunded(both, local):
    async with both(local.account()) as s:
        made, text = await reserve(s)
        rid, quote = made["reservation_id"], made["quote"]
        total = quote["deposit"]["total_cents"]
        assert made["status"] == "pending_payment" and made["position"] is None and made["paid_at"] is None
        assert made["checkout_url"].startswith("https://checkout.stripe.com/")
        assert total > 0 and total == quote["deposit"]["per_phone_cents"] * 2
        assert quote["first_period_cents"] == sum(line["quantity"] * line["unit_cents"] for line in quote["lines"])
        assert quote["due_at_handover_cents"] == quote["first_period_cents"] - total

        # What the model is told: the link, the quote, and that only a person pays.
        assert f"Started reservation {rid}: 2 x Cloud iPhone, monthly." in text
        assert "nothing is reserved until the deposit is paid" in text
        assert "A person has to open this Stripe Checkout link and pay the deposit." in text
        assert "This tool cannot pay" in text
        assert made["checkout_url"] in text.splitlines()
        assert f"- Deposit due now: {_amount(total)} (2 x {_amount(quote['deposit']['per_phone_cents'])})" in text
        for line in quote["lines"]:
            assert f"{line['quantity']} x {_amount(line['unit_cents'])} ({line['tier']} price" in text
        assert f"= {_amount(quote['first_period_cents'])}." in text
        assert f"leaving {_amount(quote['due_at_handover_cents'])}." in text

        # Unpaid, it holds no place.
        listed = text_of(await call(s, "phone_list_reservations"))
        assert f"- {rid}: 2 x Cloud iPhone, monthly. pending_payment; deposit of {_amount(total)} not paid." in listed
        assert "position" not in listed

        # A person pays on Stripe's page; only then is there a place in the queue.
        local.pay(rid)
        queued = await read(s, rid)
        assert queued["status"] == "queued" and isinstance(queued["position"], int) and queued["position"] >= 1
        assert queued["paid_at"] is not None and queued["checkout_url"] is None
        assert queued["deposit"]["paid_cents"] == total == queued["deposit"]["refundable_cents"]
        shown = text_of(await call(s, "phone_list_reservations", reservation_id=rid))
        assert "Status: queued." in shown and f"Position {queued['position']} in the queue." in shown
        assert "checkout.stripe.com" not in shown

        done = await call(s, "phone_cancel_reservation", reservation_id=rid)
        assert not done.is_error, text_of(done)
        assert f"Cancelled reservation {rid}. {_amount(total)} is being refunded to the card that paid." \
            in text_of(done)
        after = await read(s, rid)
        assert (after["status"], after["position"], after["can_cancel"]) == ("cancelled", None, False)
        assert after["deposit"]["refunded_cents"] == total
        assert [refund["amount"] for refund in local.refunds(rid)] == [total]

        again = await call(s, "phone_cancel_reservation", reservation_id=rid)      # twice refunds once
        assert not again.is_error and "was already cancelled" in text_of(again)
        assert len(local.refunds(rid)) == 1


async def test_a_cancel_after_the_grant_is_refused_and_an_ended_plan_says_so(both, local):
    async with both(local.account()) as s:
        made, _text = await reserve(s, quantity=1)
        rid, total = made["reservation_id"], made["quote"]["deposit"]["total_cents"]
        local.pay(rid)
        granted = local.grant(rid)
        after = await read(s, rid)
        assert (after["status"], after["granted"], after["queued"], after["can_cancel"]) == ("granted", 1, 0, False)
        assert after["deposit"]["credited_cents"] == total and after["deposit"]["refundable_cents"] == 0

        refused = await call(s, "phone_cancel_reservation", reservation_id=rid)
        assert refused.is_error
        assert "Not cancelled." in text_of(refused) and "(already_granted)" in text_of(refused)
        assert (await read(s, rid))["status"] == "granted"
        assert local.refunds(rid) == []

        local.end_plan(granted["subscription"])
        ended = await read(s, rid)
        assert (ended["status"], ended.get("ended")) == ("ended", 1)
        shown = text_of(await call(s, "phone_list_reservations", reservation_id=rid))
        assert "Status: ended." in shown and "the plan has ended for 1" in shown
        still = await call(s, "phone_cancel_reservation", reservation_id=rid)
        assert still.is_error and "Not cancelled." in text_of(still)
        assert local.refunds(rid) == []


async def test_part_handed_over_refunds_only_the_phones_still_waiting(client, local):
    async with client(local.account()) as s:
        made, _text = await reserve(s, quantity=3)
        rid, each = made["reservation_id"], made["quote"]["deposit"]["per_phone_cents"]
        local.pay(rid)
        local.grant(rid, phones=1)
        partly = await read(s, rid)
        assert (partly["status"], partly["granted"], partly["queued"]) == ("queued", 1, 2)
        assert partly["deposit"]["refundable_cents"] == each * 2
        assert "1 of 3 handed over, 2 still waiting." in text_of(
            await call(s, "phone_list_reservations", reservation_id=rid))
        done = text_of(await call(s, "phone_cancel_reservation", reservation_id=rid))
        assert f"Left the queue for the phones still waiting on {rid}. {_amount(each * 2)} is being refunded" in done
        after = await read(s, rid)
        assert (after["status"], after["granted"], after["queued"]) == ("granted", 1, 0)
        assert (after["deposit"]["refunded_cents"], after["deposit"]["credited_cents"]) == (each * 2, each)
        assert [refund["amount"] for refund in local.refunds(rid)] == [each * 2]


async def test_the_same_key_is_the_same_reservation(client, local):
    key = "mcp-live-" + uuid.uuid4().hex
    async with client(local.account()) as s:
        first, text = await reserve(s, idempotency_key=key)
        again, _text = await reserve(s, idempotency_key=key)
        assert first["reservation_id"] == again["reservation_id"]
        assert first["checkout_url"] == again["checkout_url"]
        assert f'idempotency_key="{key}"' in text
        assert len(json.loads(text_of(await call(s, "phone_list_reservations", format="json")))) == 1
        other = await call(s, "phone_reserve", **{**RESERVE, "quantity": 5}, idempotency_key=key)
        assert other.is_error and "already used for a different request" in text_of(other)
        # Paid, the same key says where it stands and offers nothing more to pay.
        local.pay(first["reservation_id"])
        paid = text_of(await call(s, "phone_reserve", **RESERVE, idempotency_key=key))
        assert "no new one was started" in paid and "Status: queued." in paid
        assert "checkout.stripe.com" not in paid


async def test_an_account_with_no_name_on_file_is_asked_for_one(client, local):
    async with client(local.account(name=None)) as s:
        bare = await call(s, "phone_reserve", **RESERVE)
        assert bare.is_error
        assert "ask the user for their first and last name" in text_of(bare)
        assert json.loads(text_of(await call(s, "phone_list_reservations", format="json"))) == []
        made, _text = await reserve(s, first_name="Grace", last_name="Hopper")
        local.pay(made["reservation_id"])
        row = local.queue_row(made["reservation_id"])                  # what the operator reads
        assert (row["name"], row["use_case"]) == ("Grace Hopper", USE_CASE)


async def test_one_account_cannot_read_or_cancel_anothers(both, local):
    async with both(local.account()) as mine:
        made, _text = await reserve(mine)
        rid = made["reservation_id"]
        local.pay(rid)
        async with both(local.account("Bo Stranger")) as theirs:
            assert text_of(await call(theirs, "phone_list_reservations")).startswith("No reservations")
            for tool in ("phone_list_reservations", "phone_cancel_reservation"):
                r = await call(theirs, tool, reservation_id=rid)
                assert r.is_error and f"No reservation {rid} on this account" in text_of(r)
        assert (await read(mine, rid))["status"] == "queued"
        assert local.refunds(rid) == []


async def test_too_many_unfinished_reservations_are_refused_until_one_is_paid(client, local):
    async with client(local.account()) as s:
        made, refused = [], None
        for _ in range(12):
            r = await call(s, "phone_reserve", **RESERVE)
            if r.is_error:
                refused = r
                break
            made.append(RID.search(text_of(r)).group(0))
        assert refused is not None, "twelve unpaid reservations were all accepted"
        assert len(made) >= 2 and "(rate_limited)" in text_of(refused)
        # Giving one up is not a way back in. Paying for one is.
        gone = await call(s, "phone_cancel_reservation", reservation_id=made[0])
        assert not gone.is_error and "nothing was charged or refunded" in text_of(gone)
        assert (await call(s, "phone_reserve", **RESERVE)).is_error
        local.pay(made[1])
        await reserve(s)


async def test_an_android_is_reserved_before_it_has_a_price(client, local):
    if local.state.get("android_priced"):
        pytest.skip("this local phone-cloud was started with --android-priced")
    async with client(local.account()) as s:
        made, text = await reserve(s, phone_type="android", quantity=1)
    assert (made["phone_type"], made["quote"]["priced"], made["quote"]["lines"]) == ("android", False, [])
    assert "- First month: the price is confirmed before the phones are handed over." in text
    assert f"- Deposit due now: {_amount(made['quote']['deposit']['total_cents'])}" in text


async def test_a_checkout_that_ran_out_is_cancelled_with_nothing_charged(client, local):
    async with client(local.account()) as s:
        made, _text = await reserve(s)
        rid = made["reservation_id"]
        local.expire(rid)
        gone = await read(s, rid)
        assert (gone["status"], gone["paid_at"], gone["checkout_url"]) == ("cancelled", None, None)
        r = await call(s, "phone_cancel_reservation", reservation_id=rid)
        assert not r.is_error and "nothing was charged or refunded" in text_of(r)
        assert local.refunds(rid) == []


async def test_a_key_the_api_refuses(both, local):
    async with both("pck_not_a_real_key") as s:
        for tool, args in (("phone_reserve", RESERVE), ("phone_list_reservations", {}),
                           ("phone_cancel_reservation", {"reservation_id": "rsv_" + "0" * 24})):
            r = await call(s, tool, **args)
            assert r.is_error and "refused the credential" in text_of(r)
