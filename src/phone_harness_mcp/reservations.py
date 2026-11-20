"""Reserving a phone the account does not have yet: three tools, and no payment.

`phone_reserve` asks Phone Harness Cloud for phones and answers with the quote
and a Stripe Checkout link for a refundable deposit. A person opens that link
and pays. Nothing here can pay, and until someone does, the reservation holds
no place in the queue, so no result says "reserved" before the API says
`queued`. `phone_list_reservations` reads where things stand, and
`phone_cancel_reservation` gives a place up, which refunds the deposit still
held.

Like the driving tools, these forward the caller's own credential and keep no
state. Every amount in a result is one the API sent: no price is written here.
"""
from __future__ import annotations

import functools
import json
import math
import re
import time
import urllib.parse
import uuid
from typing import Literal

import anyio
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from .cloud import Cloud, CloudError, NotSignedIn, Unreachable
from .phone import PhoneError, Phones

MAX_QUANTITY = 50            # the API's limit for one reservation
MAX_USE_CASE = 2000          # and for the free text
TIMEOUT = 75                 # the API talks to Stripe before it answers
# The only place the API sends anyone to pay.
CHECKOUT_HOST = "checkout.stripe.com"
# Loose on purpose: enough to be safe in a URL path. The API decides what exists.
RESERVATION_ID = re.compile(r"rsv_[A-Za-z0-9]{1,64}")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
LABELS = {"iphone": "Cloud iPhone", "iphone_esim": "Cloud iPhone with eSIM", "android": "Cloud Android"}
TIERS = {"intro": "intro price, once per customer", "standard": "standard price"}
PAY = ("A person has to open this Stripe Checkout link and pay the deposit. This tool cannot pay, "
       "and you must not pay or enter card details for them:")
NOT_CHECKOUT = (f"The payment link the API sent is not a Stripe Checkout page (https://{CHECKOUT_HOST}), so it "
                "is not given here. Nobody should pay anywhere else.")


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def _amount(cents, currency="usd") -> str:
    """Integer cents as money. Dollars get the sign; any other currency its code."""
    if isinstance(cents, bool) or not isinstance(cents, (int, float)):
        return "?"
    if str(currency or "usd").lower() == "usd":
        return f"${cents / 100:,.2f}"
    return f"{cents / 100:,.2f} {str(currency).upper()}"


def _phones(reservation: dict) -> str:
    """'3 x Cloud iPhone with eSIM, monthly'."""
    kind = reservation.get("phone_type")
    return f"{reservation.get('quantity', '?')} x {LABELS.get(kind, kind or 'phone')}, {reservation.get('plan') or '?'}"


def _ago(ts) -> str:
    try:
        seconds = int(time.time() - float(ts))
    except (TypeError, ValueError):
        return "at an unknown time"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def _in(ts) -> str | None:
    """How far off a time is: '45 minutes', '23 hours', '3 days'. None once it has passed."""
    try:
        left = float(ts) - time.time()
    except (TypeError, ValueError):
        return None
    if left <= 0:
        return None
    if left < 90 * 60:
        return f"{max(1, math.ceil(left / 60))} minutes"
    if left < 48 * 3600:
        return f"{round(left / 3600)} hours"
    return f"{round(left / 86400)} days"


def checkout_url(reservation: dict) -> str | None:
    """The reservation's payment link, or None when it is not Stripe Checkout.

    Someone is about to type a card number into this page. The API only ever
    sends people to Stripe's own Checkout host, so a link anywhere else is
    never handed to a model as the place to pay."""
    url = reservation.get("checkout_url")
    if not isinstance(url, str) or any(ch <= " " or ch == "\x7f" for ch in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host, user = parts.hostname, parts.username or parts.password
    except ValueError:
        return None
    if parts.scheme != "https" or host != CHECKOUT_HOST or user:
        return None
    return url


def _quote(reservation: dict) -> tuple[str | None, str]:
    """The quote the API stored with a reservation, as two sentences: the
    deposit due now (None when the API named no amount), and the plan's first
    billing period one price at a time. A phone with no price yet says the
    price comes later."""
    quote = reservation.get("quote") or {}
    currency = quote.get("currency") or "usd"
    deposit = quote.get("deposit") or {}
    period = {"month": "month", "year": "year"}.get(quote.get("interval"), "billing period")
    due = None
    total = deposit.get("total_cents")
    if total is not None:
        per, phones = deposit.get("per_phone_cents"), deposit.get("phones")
        each = f" ({phones} x {_amount(per, currency)})" if per is not None and phones else ""
        back = ", refundable in full while the reservation is queued" if deposit.get("refundable") else ""
        due = f"Deposit due now: {_amount(total, currency)}{each}{back}."
    priced = quote.get("lines") or []
    if not quote.get("priced") or not priced:
        return due, f"First {period}: the price is confirmed before the phones are handed over."
    parts = " + ".join(f"{line.get('quantity')} x {_amount(line.get('unit_cents'), currency)} "
                       f"({TIERS.get(line.get('tier'), line.get('tier') or 'price')})" for line in priced)
    first, later = quote.get("first_period_cents"), quote.get("due_at_handover_cents")
    plan = f"First {period}, charged when the phones are handed over: {parts}"
    if first is not None:
        plan += f" = {_amount(first, currency)}"
    plan += "."
    if later is not None:
        plan += f" The deposit comes off it, leaving {_amount(later, currency)}."
    return due, plan


def _quote_lines(reservation: dict) -> list[str]:
    return ["Quote:", *(f"- {sentence}" for sentence in _quote(reservation) if sentence)]


def _handed_over(reservation: dict) -> str:
    """'1 of 3 handed over, 2 still waiting'."""
    waiting = f", {reservation['queued']} still waiting" if reservation.get("queued") else ""
    ended = f", the plan has ended for {reservation['ended']}" if reservation.get("ended") else ""
    return f"{reservation['granted']} of {reservation.get('quantity')} handed over{waiting}{ended}"


def _deposit(reservation: dict) -> str:
    """A reservation's deposit in a few words: what is due, held, or gone back."""
    deposit = reservation.get("deposit") or {}
    currency = deposit.get("currency") or "usd"
    status = reservation.get("status")
    if status == "pending_payment":
        due = ((reservation.get("quote") or {}).get("deposit") or {}).get("total_cents")
        return f"deposit of {_amount(due, currency)} not paid" if due is not None else "deposit not paid"
    paid, held = deposit.get("paid_cents") or 0, deposit.get("refundable_cents") or 0
    credited, refunded = deposit.get("credited_cents") or 0, deposit.get("refunded_cents") or 0
    if not paid:
        return "nothing was charged"
    when = f" {_ago(reservation['paid_at'])}" if reservation.get("paid_at") else ""
    words = [f"deposit of {_amount(paid, currency)} paid{when}"]
    if status == "cancelling":
        words.append(f"{_amount(held, currency)} refund not confirmed yet")
    elif held:
        words.append(f"{_amount(held, currency)} refundable")
    if credited:
        words.append(f"{_amount(credited, currency)} went onto a first bill")
    if refunded:
        words.append(f"{_amount(refunded, currency)} refunded")
    return ", ".join(words)


def _line(reservation: dict) -> str:
    """One reservation on one line, for the list."""
    status = reservation.get("status")
    place = ""
    if reservation.get("position") is not None:
        place = f", position {reservation['position']} in the queue"
    if reservation.get("granted"):
        place += f", {_handed_over(reservation)}"
    note = ""
    if status == "pending_payment":
        note = " It holds no place until a person pays; pass its reservation_id to get the Checkout link."
    return f"- {reservation.get('reservation_id')}: {_phones(reservation)}. {status}{place}; {_deposit(reservation)}.{note}"


def _pay_lines(reservation: dict) -> list[str]:
    """Where a person pays an unpaid reservation's deposit, and until when."""
    url = checkout_url(reservation)
    if not url:
        return [NOT_CHECKOUT] if reservation.get("checkout_url") else [
            "There is no payment link for it yet. Ask again with its reservation_id in a moment."]
    lines = [PAY, url]
    if reservation.get("expires_at"):
        left = _in(reservation["expires_at"])
        # By this machine's clock, which may be wrong: say so, and still give the link.
        lines.append(f"The link works for another {left}." if left else
                     "The link has expired. If it no longer opens, reserve again for a new one.")
    return lines


def describe(reservation: dict) -> str:
    """One reservation in full: where it stands, its deposit, its quote, and how to pay if it is unpaid."""
    rid, status = reservation.get("reservation_id"), reservation.get("status")
    lines = [f"Reservation {rid}: {_phones(reservation)}.", f"Status: {status}. {reservation.get('detail') or ''}".rstrip()]
    if reservation.get("position") is not None:
        lines.append(f"Position {reservation['position']} in the queue.")
    if reservation.get("granted"):
        lines.append(_sentence(_handed_over(reservation)))
    if status == "pending_payment":
        lines += ["", *_pay_lines(reservation), "", *_quote_lines(reservation)]
    else:
        lines.append(_sentence(_deposit(reservation)))
        if status == "queued":            # what the plan will cost once a phone is handed over
            lines.append(_quote(reservation)[1])
    if reservation.get("can_cancel"):
        lines.append("phone_cancel_reservation gives it up" + (
            " and refunds the deposit still held." if status == "queued" else "."))
    return "\n".join(lines)


def refusal(e: CloudError, rid: str | None = None, key: str | None = None) -> PhoneError:
    """A reservation route's error, as a sentence a model can act on.

    The API's `error` is written for a customer and is passed on as it is; its
    `code` decides what is added. `key` is the Idempotency-Key of a create that
    can be safely repeated."""
    if e.status == 401:
        return Phones._translate(e, None)
    said = e.error
    again = (f' Call phone_reserve again with the same arguments and idempotency_key="{key}" to continue this '
             "reservation instead of starting a second one.") if key else ""
    if e.status == 404 and rid:
        return PhoneError(f"No reservation {rid} on this account. phone_list_reservations shows the account's "
                          "reservations.")
    if e.status == 404:
        # No id was named, so it is the route that is missing: an API from before reservations.
        return PhoneError("This Phone Harness Cloud API does not take reservations yet, so a phone cannot be "
                          "reserved through it.")
    if e.code == "idempotency_conflict":
        return PhoneError("That idempotency_key was already used for a different request. Leave it out to start a "
                          "new reservation, or repeat the arguments it was first used with.")
    if e.code == "payments_unavailable":
        if rid:
            return PhoneError(f"{said} Nothing changed. Call phone_cancel_reservation again in a moment.")
        return PhoneError(f"{said} Nothing was charged.{again}")
    if e.code == "refund_unconfirmed":
        return PhoneError(f"{said} Call phone_cancel_reservation again to retry the refund; it is never sent twice.")
    if e.code in ("already_granted", "grant_in_progress"):
        return PhoneError(f"Not cancelled. {said} ({e.code})")
    if e.status == 400 and not e.code and "name" in said.lower():
        return PhoneError(f"{said}. The account has no name on file: ask the user for their first and last name "
                          "and call phone_reserve again with first_name and last_name. Do not invent one.")
    if e.status in (400, 403, 409, 429):
        return PhoneError(said if not e.code or e.code in said else f"{said} ({e.code})")
    return PhoneError(f"Phone Harness Cloud answered {e.status}: {said}{again}")


def _a_reservation(answer, then: str) -> dict:
    """The API's answer when it is a reservation; a sentence for the model when it is not."""
    if isinstance(answer, dict) and answer.get("reservation_id"):
        return answer
    raise PhoneError(f"Phone Harness Cloud answered with something that is not a reservation; {then}.")


class Reservations:
    """The account's reservations, as seen through one signed-in `Cloud`."""

    def __init__(self, cloud: Cloud):
        self.cloud = cloud

    def create(self, body: dict, key: str) -> dict:
        headers = {"Idempotency-Key": key}
        try:
            try:
                made = self.cloud.request("POST", "/reservations", body, timeout=TIMEOUT, headers=headers)
            except Unreachable:
                # The answer was lost, which does not say the request was. The
                # same key and body is the same reservation, never a second one.
                made = self.cloud.request("POST", "/reservations", body, timeout=TIMEOUT, headers=headers)
            return _a_reservation(made, f'call phone_reserve again with the same arguments and '
                                        f'idempotency_key="{key}" to find out whether one was started')
        except CloudError as e:
            raise refusal(e, key=key) from None
        except Unreachable as e:
            raise PhoneError(f'{e}. The request may still have arrived: call phone_reserve again with the same '
                             f'arguments and idempotency_key="{key}" to continue it instead of starting a second '
                             "reservation.") from None
        except NotSignedIn as e:
            raise PhoneError(str(e)) from None

    def all(self) -> list[dict]:
        found = self._call("GET", "/reservations")
        rows = found.get("reservations") if isinstance(found, dict) else found
        return [r for r in rows or [] if isinstance(r, dict)]

    def one(self, rid: str) -> dict:
        return _a_reservation(self._call("GET", f"/reservations/{rid}", rid=rid), "ask again in a moment")

    def cancel(self, rid: str) -> dict:
        return _a_reservation(self._call("POST", f"/reservations/{rid}/cancel", rid=rid),
                              "phone_list_reservations shows where it stands now")

    def _call(self, method, path, rid=None):
        try:
            return self.cloud.request(method, path, timeout=TIMEOUT)
        except CloudError as e:
            raise refusal(e, rid=rid) from None
        except (NotSignedIn, Unreachable) as e:
            raise PhoneError(str(e)) from None


def _id(reservation_id: str) -> str:
    rid = (reservation_id or "").strip()
    if not RESERVATION_ID.fullmatch(rid):
        raise PhoneError("reservation_id is an id like rsv_0123456789abcdef01234567; phone_list_reservations "
                         "shows the account's.")
    return rid


def _cancelled(done: dict, rid: str) -> str:
    """What a cancel did, from the API's answer to it."""
    deposit = done.get("deposit") or {}
    currency = deposit.get("currency") or "usd"
    refunded, status = done.get("refunded_cents") or 0, done.get("status")
    back = f" {_amount(refunded, currency)} is being refunded to the card that paid."
    detail = f" {done['detail']}" if done.get("detail") else ""
    if status == "cancelled" and refunded:
        return f"Cancelled reservation {rid}.{back}{detail}"
    if status == "cancelled" and deposit.get("refunded_cents"):
        return (f"Reservation {rid} was already cancelled: {_amount(deposit['refunded_cents'], currency)} was "
                "refunded before. Nothing more to refund.")
    if status == "cancelled":
        return f"Reservation {rid} is cancelled. Its deposit was never paid, so nothing was charged or refunded."
    if status == "granted":
        return f"Left the queue for the phones still waiting on {rid}.{back if refunded else ''}{detail}"
    return (f"Reservation {rid} is now {status}.{detail} If a refund is owed and has not arrived, call "
            "phone_cancel_reservation again; it is never sent twice.")


async def _thread(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def register(server, cloud_for) -> None:
    """Add the reservation tools. `cloud_for(ctx)` yields the caller's `Cloud`, as for every other tool."""

    def reservations(ctx) -> Reservations:
        return Reservations(cloud_for(ctx))

    @server.tool(name="phone_reserve", title="Reserve a phone",
                 annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    async def reserve(ctx: Context,
                      phone_type: Literal["iphone", "iphone_esim", "android"],
                      plan: Literal["monthly", "annual"],
                      quantity: int,
                      use_case: str,
                      first_name: str | None = None,
                      last_name: str | None = None,
                      idempotency_key: str | None = None) -> str:
        """Ask for cloud phones the account does not have yet, and get the quote and a Stripe Checkout link for the refundable deposit. This tool never pays: a person must open the link and pay the deposit, and until they do nothing is reserved and no place in the queue is held. `phone_type` is iphone, iphone_esim (with a real number) or android; `plan` is monthly or annual; `quantity` is 1 to 50; `use_case` is what the user wants the phones for, in their words. Ask the user for these rather than guessing. `first_name` and `last_name` are the person reserving, needed when the account has no name on file (the error says so). The result names an `idempotency_key`; pass it back only to repeat a call that failed part way."""
        if isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
            raise PhoneError(f"quantity must be between 1 and {MAX_QUANTITY}.")
        use_case = (use_case or "").strip()
        if not use_case:
            raise PhoneError("use_case is required: say in a sentence what the user wants the phones for.")
        if len(use_case) > MAX_USE_CASE:
            raise PhoneError(f"use_case is {len(use_case)} characters; the most is {MAX_USE_CASE}.")
        if idempotency_key is not None and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise PhoneError("idempotency_key must be 16 to 128 letters, digits, underscores or hyphens.")
        body: dict = {"phone_type": phone_type, "plan": plan, "quantity": quantity, "use_case": use_case}
        for field, value in (("first_name", first_name), ("last_name", last_name)):
            if value is not None and value.strip():
                body[field] = value.strip()
        key = idempotency_key or uuid.uuid4().hex
        made = await _thread(reservations(ctx).create, body, key)
        rid = made.get("reservation_id")
        if made.get("status") != "pending_payment":
            # A key that was used before answers with what that reservation is now.
            return f"That idempotency_key already has a reservation; no new one was started.\n{describe(made)}"
        if not checkout_url(made):
            raise PhoneError(f"Reservation {rid} was started. {NOT_CHECKOUT} phone_list_reservations shows "
                             "where it stands.")
        started = (f"Started reservation {rid}: {_phones(made)}. It holds no place in the queue yet: nothing is "
                   "reserved until the deposit is paid.")
        after = ("Once the deposit is paid, phone_list_reservations shows it as queued with its place. "
                 "phone_cancel_reservation gives the place up and refunds the deposit.")
        return "\n".join([started, "", *_pay_lines(made), "", *_quote_lines(made), "", after,
                          f'(idempotency_key="{key}")'])

    @server.tool(name="phone_list_reservations", title="List reservations",
                 annotations=ToolAnnotations(read_only_hint=True))
    async def list_reservations(ctx: Context, reservation_id: str | None = None,
                                format: Literal["text", "json"] = "text") -> str:
        """The account's phone reservations, newest first: status, place in the queue, deposit, and when it was paid. With `reservation_id`, that one in full, with its quote and, while it is unpaid, the Stripe Checkout link a person has to open to pay the deposit. `pending_payment` means the deposit is not paid and no place is held; `queued` means it is paid and waiting."""
        r = reservations(ctx)
        if reservation_id:
            found = await _thread(r.one, _id(reservation_id))
            return json.dumps(found) if format == "json" else describe(found)
        rows = await _thread(r.all)
        if format == "json":
            return json.dumps(rows)
        if not rows:
            return "No reservations on this account. phone_reserve starts one."
        head = "1 reservation:" if len(rows) == 1 else f"{len(rows)} reservations, newest first:"
        return "\n".join([head, *(_line(row) for row in rows)])

    @server.tool(name="phone_cancel_reservation", title="Cancel a reservation",
                 annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True))
    async def cancel_reservation(ctx: Context, reservation_id: str) -> str:
        """Cancel a phone reservation: give up its place in the queue and refund the deposit still held to the card that paid. Call it only when the user has asked to cancel. A queued reservation is refunded in full; an unpaid one is closed with nothing to refund. Once every phone has been handed over it is refused, because that deposit went onto the first bill."""
        rid = _id(reservation_id)
        done = await _thread(reservations(ctx).cancel, rid)
        return _cancelled(done, rid)
