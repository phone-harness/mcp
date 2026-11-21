"""Enough of api.phone-harness.com to drive the server end to end, in one thread.

Shapes follow phone-cloud's docs/api.md: bearer auth, `GET /me`, the session
lifecycle, and `POST /sessions/{sid}/op` with its 400/404/409 contract. Every
op lands in `.ops`, every request in `.requests`, so a test can assert what the
server actually sent. The same process also plays the OAuth token endpoint, so
the CLI-login refresh path can be tested without Clerk.

It also plays the reservation routes (docs/api.md, "Reservations"): a phone is
asked for, the answer is a quote and a Stripe Checkout link, and nothing here
ever charges anyone. `.reservation_calls` keeps each of those requests with its
headers and body.
"""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUBLIC_OPS = ("screen.capture", "screen.bounds", "screen.require", "screen.text", "screen.text_pixels",
              "input.tap", "input.press", "input.drag", "input.scroll", "input.keys", "input.text",
              "nav.home", "nav.back", "nav.recents", "apps.launch", "apps.current", "apps.list", "tree")

BOUNDS = {"x": 0, "y": 0, "w": 390, "h": 844, "id": "mock"}
# The capture is twice the coordinate space on each side, like a Retina iPhone,
# so the mapping text has something to say.
CAPTURE_SIZE = (780, 1688)
TEXT = [
    {"text": "Hello Cloud", "confidence": 1.0, "source": "tree", "x": 195.0, "y": 200.0, "w": 200.0, "h": 30.0},
    {"text": "Continue", "confidence": 1.0, "source": "tree", "x": 195.0, "y": 500.0, "w": 160.0, "h": 44.0},
]


CHECKOUT = "https://checkout.stripe.com/c/pay/cs_test_"
DEPOSIT = 2000
# (standard, intro) cents for one phone for one billing period. For a short
# while the API took a reservation for a cloud Android, before it had a price.
# It refuses one now, and these tools never ask, but one made then can still
# be on the account.
PRICES = {("iphone", "monthly"): (8000, 5000), ("iphone", "annual"): (76800, 48000),
          ("iphone_esim", "monthly"): (10000, 6500), ("iphone_esim", "annual"): (96000, 62400),
          ("android", "monthly"): (None, None), ("android", "annual"): (None, None)}
DETAIL = {
    "pending_payment": "This reservation is not finished. Pay the deposit to hold a place.",
    "queued": "You are in the queue. Cancel any time and the deposit comes back in full.",
    "cancelling": "You have left the queue. Your deposit is on its way back to your card.",
    "granted": "Your phone is yours. The deposit went towards your first bill.",
    "ended": "The plan for this phone has ended.",
    "cancelled": "You left the queue and the deposit was refunded in full.",
}
NEVER_PAID = "This reservation was never paid for, so nothing was charged and no place is held."
GRANTED_REFUSAL = ("Your phone has been handed over, and its deposit went towards your first bill. "
                   "Manage the plan from your account.")


def rid(n):
    return "rsv_" + f"{n:024x}"


def reservation(n, phone_type="iphone", plan="monthly", quantity=1, status="pending_payment", *,
                position=None, granted=0, paid_days_ago=None, intro=True, uid="u_1"):
    """One reservation as the API shows it, in whichever state `status` names. `uid` is the
    fake's own note of whose it is and is never sent."""
    standard, intro_cents = PRICES[(phone_type, plan)]
    lines = []
    if standard is not None:
        rest = quantity
        if intro and intro_cents is not None:
            lines.append({"tier": "intro", "quantity": 1, "unit_cents": intro_cents})
            rest -= 1
        if rest:
            lines.append({"tier": "standard", "quantity": rest, "unit_cents": standard})
    first = sum(line["quantity"] * line["unit_cents"] for line in lines) if lines else None
    total = DEPOSIT * quantity
    unpaid = status == "pending_payment" or (status == "cancelled" and paid_days_ago is None)
    credited = 0 if unpaid else DEPOSIT * granted
    held = total - credited if status in ("queued", "cancelling") else 0
    refunded = total - credited if status in ("cancelled", "granted", "ended") and not unpaid else 0
    pending = status == "pending_payment"
    return {
        "uid": uid, "reservation_id": rid(n), "status": status, "phone_type": phone_type, "plan": plan,
        "quantity": quantity, "created_at": time.time() - 86400 * (paid_days_ago or 0) - 60,
        "paid_at": None if unpaid else time.time() - 86400 * (paid_days_ago or 0),
        "position": position if status == "queued" else None,
        "queued": quantity - granted if status == "queued" else 0,
        "granted": 0 if unpaid else granted,
        "ended": granted if status == "ended" else 0,       # phones whose plan has since ended
        "deposit": {"paid_cents": 0 if unpaid else total, "refundable_cents": held,
                    "credited_cents": credited, "refunded_cents": refunded, "currency": "usd"},
        "can_cancel": status in ("pending_payment", "queued"),
        "checkout_url": CHECKOUT + f"{n:06d}" if pending else None,
        "expires_at": time.time() + 86400 if pending else None,
        "quote": {"currency": "usd", "interval": "month" if plan == "monthly" else "year",
                  "deposit": {"per_phone_cents": DEPOSIT, "phones": quantity, "total_cents": total,
                              "refundable": True},
                  "priced": standard is not None, "lines": lines, "first_period_cents": first,
                  "due_at_handover_cents": first - total if first is not None else None},
        "detail": NEVER_PAID if status == "cancelled" and unpaid else DETAIL[status],
    }


def solid_png(w, h, rgb=(240, 240, 245)):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data))
    row = b"\x00" + bytes(rgb) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


class FakeCloud:
    def __init__(self, ready_after=1):
        self.ready_after = ready_after          # GETs of a provisioning session before it is ready
        self.tokens = {"pck_test": {"uid": "u_1", "email": "me@example.com"}}
        self.refresh_tokens = {"rt_old": ("at_new", "rt_new")}
        self.sessions = {}
        self.ops = []           # (sid, op, kw)
        self.requests = []      # (method, path, bearer)
        self.busy_times = 0     # the next N ops answer 409 busy
        self.outage_times = 0   # the next N ops answer 503
        self.lock = threading.Lock()
        self._png = base64.b64encode(solid_png(*CAPTURE_SIZE)).decode()
        self._png_wide = base64.b64encode(solid_png(CAPTURE_SIZE[1], CAPTURE_SIZE[0])).decode()
        self.turned = set()           # ids of sessions whose screen is on its side: a landscape app, a turned phone
        self.stale_bounds = False     # True: the size reported is the one the session started with, turned or not
        self._n = 0
        self._server = None
        self._thread = None
        self.me_patch = None          # merged over GET /me; None drops a key
        self.reservations = {}        # reservation_id -> reservation, in the order made
        self.reservation_keys = {}    # (uid, Idempotency-Key) -> (body, reservation_id)
        self.reservation_calls = []   # (method, path, headers with lowercase names, body)
        self.reservation_fail = None  # (status, body, headers): the next reservation request answers this
        self.reservation_drop = 0     # the next N creates are made, and then never answered
        self.account_name = "Ada King"  # the name on file; None for an account that has none
        self.reserved_names = {}      # reservation_id -> [first, last] as the API would store them
        self.reservations_live = True  # False: an API from before reservations; every route under it is a 404
        self.checkout = None          # set: every new reservation's checkout_url is this instead

    # --- lifecycle --------------------------------------------------------------

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

            def do_DELETE(self):
                fake.handle(self, "DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    # --- plumbing ---------------------------------------------------------------

    @staticmethod
    def _send(h, status, body, headers=None):
        data = json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, str(v))
        h.end_headers()
        h.wfile.write(data)

    def _body(self, h):
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        if n > (1 << 20):
            raise _Reply(413, {"error": "body too large"})
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise _Reply(400, {"error": "bad json"}) from None
        if not isinstance(data, dict):
            raise _Reply(400, {"error": "body must be an object"})
        return data

    def handle(self, h, method):
        path = urllib.parse.urlsplit(h.path).path
        auths = h.headers.get_all("Authorization") or []
        bearer = auths[0][7:] if auths and auths[0].lower().startswith("bearer ") else None
        with self.lock:
            self.requests.append((method, path, bearer))
        try:
            if path == "/oauth/token" and method == "POST":
                return self._token_endpoint(h)
            if path == "/health":
                return self._send(h, 200, {"ok": True})
            if len(auths) > 1:
                raise _Reply(400, {"error": "one Authorization header"})
            user = self.tokens.get(bearer) if bearer else None
            if not user:
                raise _Reply(401, {"error": "sign in"})
            status, body, headers = self.route(method, path, user, h)
            self._send(h, status, body, headers)
        except _Reply as r:
            self._send(h, r.status, r.body, r.headers)
        except _Dropped:
            h.close_connection = True       # the request was handled; its answer never arrives

    def _token_endpoint(self, h):
        n = int(h.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(h.rfile.read(n).decode()))
        if form.get("grant_type") != "refresh_token":
            return self._send(h, 400, {"error": "unsupported_grant_type"})
        grant = self.refresh_tokens.get(form.get("refresh_token"))
        if not grant or not form.get("client_id"):
            return self._send(h, 400, {"error": "invalid_grant"})
        access, refresh = grant
        self.tokens.setdefault(access, {"uid": "u_1", "email": "me@example.com"})
        return self._send(h, 200, {"access_token": access, "refresh_token": refresh, "expires_in": 3600})

    # --- routes -----------------------------------------------------------------

    def route(self, method, path, user, h):
        parts = path.strip("/").split("/")
        if path == "/me" and method == "GET":
            return 200, self.me(user), None
        if path == "/sessions" and method == "GET":
            return 200, [self.public(s) for s in self.sessions.values()
                         if s["uid"] == user["uid"] and s["state"] != "closed"], None
        if path == "/sessions" and method == "POST":
            return self.create(user, self._body(h))
        if parts[0] == "reservations":
            return self.reservation_route(method, path, parts, user, h)
        if len(parts) >= 2 and parts[0] == "sessions":
            s = self.sessions.get(parts[1])
            if not s or s["uid"] != user["uid"] or s["state"] == "closed":
                raise _Reply(404, {"error": "no such session"})
            if len(parts) == 2 and method == "GET":
                self.tick(s)
                return 200, self.public(s), None
            if len(parts) == 2 and method == "DELETE":
                s["state"] = "closed"
                return 200, {"released": True}, None
            if len(parts) == 3 and parts[2] == "ops" and method == "GET":
                if s["state"] != "ready":
                    raise _Reply(409, {"error": "the phone is not ready"})
                return 200, {"ops": [op for op in PUBLIC_OPS if op not in self.unsupported(s)]}, None
            if len(parts) == 3 and parts[2] == "op" and method == "POST":
                return self.run_op(s, self._body(h))
        raise _Reply(404, {"error": "not found"})

    def me(self, user):
        body = {"uid": user["uid"], "email": user["email"], "balance_cents": 1250, "price_cents_per_minute": 5,
                "billing_mode": "credits", "session_limit": 2,
                "unnamed": {"platform": "android", "kind": "emulator", "temporary": True},
                "available": [
                    {"platform": "android", "kind": "emulator", "label": "Saved phone", "id": "prof-1", "state": "stored"},
                    {"platform": "android", "kind": "emulator", "label": "Android emulator", "temporary": True},
                    {"platform": "ios", "kind": "device", "label": "My iPhone", "id": "iph-a1b2c3",
                     "state": "available"},
                ]}
        for key, value in (self.me_patch or {}).items():
            if value is None:
                body.pop(key, None)
            else:
                body[key] = value
        return body

    def create(self, user, body):
        if "provider" in body:
            raise _Reply(400, {"error": "provider was replaced by platform and kind"})
        if ("platform" in body) != ("kind" in body):
            raise _Reply(400, {"error": "platform and kind go together"})
        platform, kind = body.get("platform", "android"), body.get("kind", "emulator")
        if (platform, kind) not in (("android", "emulator"), ("ios", "device")):
            raise _Reply(400, {"error": "no phone of that platform and kind is available to this account"})
        timeout = int(body.get("timeout_seconds") or 300)
        self._n += 1
        sid = f"s_{self._n:04d}"
        s = {"id": sid, "uid": user["uid"], "state": "provisioning", "platform": platform, "kind": kind,
             "device": "iPhone" if platform == "ios" else "Android emulator", "polls": 0,
             "timeout_seconds": timeout, "expires_at": time.time() + timeout, "created": body,
             "progress": {"phase": "booting"}}
        self.sessions[sid] = s
        return 202, self.public(s), None

    def tick(self, s):
        if s["state"] == "provisioning":
            s["polls"] += 1
            if s["polls"] >= self.ready_after:
                s["state"] = "ready"
                s.pop("progress", None)

    @staticmethod
    def public(s):
        out = {k: v for k, v in s.items() if k not in ("uid", "polls", "created")}
        out["screen"] = {"w": BOUNDS["w"], "h": BOUNDS["h"]} if s["state"] == "ready" else None
        return out

    @staticmethod
    def unsupported(s):
        return {"nav.back", "apps.list", "tree"} if s["platform"] == "ios" else {"nav.back"}

    def run_op(self, s, body):
        if s["state"] != "ready":
            raise _Reply(409, {"error": "the phone is not ready"})
        op, kw = body.get("op"), body.get("kw") or {}
        if not isinstance(op, str) or op not in PUBLIC_OPS:
            raise _Reply(400, {"error": "op must be one of: " + ", ".join(PUBLIC_OPS)})
        if not isinstance(kw, dict) or not all(isinstance(k, str) for k in kw):
            raise _Reply(400, {"error": "kw must be an object with string keys"})
        if "op" in kw or "path" in kw:
            raise _Reply(400, {"error": "kw must not contain op or path"})
        with self.lock:
            if self.busy_times:
                self.busy_times -= 1
                raise _Reply(409, {"error": "the phone is busy with another operation", "retry_after_seconds": 0.2},
                             {"Retry-After": "1"})
            if self.outage_times:
                self.outage_times -= 1
                raise _Reply(503, {"error": "InputServiceDown"})
            self.ops.append((s["id"], op, kw))
        if op in self.unsupported(s):
            raise _Reply(400, {"error": f"this phone cannot {op!r}", "unsupported": True})
        return 200, {"result": self.answer(s, op, kw)}, None

    def landscape(self, s):
        return s["id"] in self.turned

    def bounds(self, s):
        """The screen's size as the API reports it. phone-host keeps the size the
        session started with (`stale_bounds`); the screenshot itself always turns."""
        b = dict(BOUNDS)
        if self.landscape(s) and not self.stale_bounds:
            b["w"], b["h"] = BOUNDS["h"], BOUNDS["w"]
        return b

    def answer(self, s, op, kw):
        if op == "screen.capture":
            return {"png_b64": self._png_wide if self.landscape(s) else self._png, "bounds": self.bounds(s)}
        if op in ("screen.bounds", "screen.require"):
            return self.bounds(s)
        if op in ("screen.text", "screen.text_pixels"):
            return [dict(b) for b in TEXT]
        if op == "tree":
            return [{"text": b["text"], "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]} for b in TEXT]
        if op == "apps.launch":
            if not kw.get("name"):
                raise _Reply(400, {"error": "apps.launch did not accept those arguments"})
            return "com.example." + str(kw["name"]).lower()
        if op == "apps.current":
            if s["platform"] == "ios":      # the iPhone host's own wording for an op it has but cannot do
                raise _Reply(400, {"error": "operation is unavailable on this phone"})
            return "com.example.mock"
        if op == "apps.list":
            return ["com.example.mock", "com.android.chrome"]
        return None


    # --- reservations -----------------------------------------------------------

    @staticmethod
    def shown(row):
        return {k: v for k, v in row.items() if k != "uid"}

    def hold(self, *rows):
        """Put reservations on an account, oldest first."""
        for row in rows:
            self.reservations[row["reservation_id"]] = row
        return rows[0] if len(rows) == 1 else rows

    def reservation_route(self, method, path, parts, user, h):
        body = self._body(h) if method == "POST" else None
        with self.lock:
            self.reservation_calls.append((method, path, {k.lower(): v for k, v in h.headers.items()}, body))
            fail, self.reservation_fail = self.reservation_fail, None
        if fail:
            raise _Reply(*fail)
        if not self.reservations_live:
            raise _Reply(404, {"error": f"no route {method} {path}"})
        mine = [r for r in self.reservations.values() if r["uid"] == user["uid"]]
        if len(parts) == 1 and method == "POST":
            return self.reserve(user, body, h.headers.get("Idempotency-Key"))
        if len(parts) == 1 and method == "GET":
            return 200, {"reservations": [self.shown(r) for r in reversed(mine)]}, None
        row = next((r for r in mine if r["reservation_id"] == parts[1]), None) if len(parts) > 1 else None
        if row is None:
            raise _Reply(404, {"error": "No such reservation"})
        if len(parts) == 2 and method == "GET":
            return 200, self.shown(row), None
        if len(parts) == 3 and parts[2] == "cancel" and method == "POST":
            return self.cancel_reservation(row)
        raise _Reply(404, {"error": "not found"})

    def reserve(self, user, body, key):
        if set(body) - {"first_name", "last_name", "email", "use_case", "phone_type", "plan", "quantity"}:
            raise _Reply(400, {"error": "Unknown field"})
        if body.get("phone_type") == "android":
            raise _Reply(400, {"error": "Cloud Android cannot be reserved. Choose a phone: iphone, iphone_esim",
                               "code": "phone_not_reservable"})
        if (body.get("phone_type"), body.get("plan")) not in PRICES:
            raise _Reply(400, {"error": "Choose a phone: iphone, iphone_esim"})
        if type(body.get("quantity")) is not int or not 1 <= body["quantity"] <= 50:
            raise _Reply(400, {"error": "Choose between 1 and 50 phones"})
        known = self.reservation_keys.get((user["uid"], key)) if key else None
        if known:
            sent, made = known
            if sent != body:
                raise _Reply(409, {"error": "Idempotency-Key was already used with a different request",
                                   "code": "idempotency_conflict"})
            status, row = 200, self.reservations[made]
        else:
            row = reservation(len(self.reservations) + 1, body["phone_type"], body["plan"], body["quantity"],
                              uid=user["uid"])
            if self.checkout:
                row["checkout_url"] = self.checkout
            self.reservations[row["reservation_id"]] = row
            # A signed-in account need not say who it is: a name in the body is
            # used, else the one on file, else none at all.
            first, _, last = (self.account_name or "").partition(" ")
            self.reserved_names[row["reservation_id"]] = [body.get("first_name") or first or None,
                                                          body.get("last_name") or last or None]
            if key:
                self.reservation_keys[(user["uid"], key)] = (body, row["reservation_id"])
            status = 201
        if self.reservation_drop:
            self.reservation_drop -= 1
            raise _Dropped()
        return status, {**self.shown(row), "status_url": "https://phone-harness.com/reservation?id="
                                                          + row["reservation_id"]}, None

    def cancel_reservation(self, row):
        status, deposit = row["status"], row["deposit"]
        if status in ("granted", "ended"):
            raise _Reply(409, {"error": GRANTED_REFUSAL, "code": "already_granted"})
        refunded = 0
        if status in ("queued", "cancelling"):
            refunded = deposit["refundable_cents"]
            deposit.update(refundable_cents=0, refunded_cents=deposit["refunded_cents"] + refunded)
            handed = row["granted"]
            row.update(status="granted" if handed else "cancelled", position=None, queued=0, can_cancel=False,
                       detail=(f"{handed} phone handed over. You left the queue for the other "
                               f"{row['quantity'] - handed}, and that part of the deposit was refunded."
                               if handed else DETAIL["cancelled"]))
        elif status == "pending_payment":
            row.update(status="cancelled", can_cancel=False, checkout_url=None, expires_at=None, detail=NEVER_PAID)
        return 200, {**self.shown(row), "refunded_cents": refunded}, None


class _Reply(Exception):
    def __init__(self, status, body, headers=None):
        self.status, self.body, self.headers = status, body, headers


class _Dropped(Exception):
    """Handled, and then the connection closes with no answer."""
