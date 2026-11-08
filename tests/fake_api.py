"""Enough of api.phone-harness.com to drive the server end to end, in one thread.

Shapes follow phone-cloud's docs/api.md: bearer auth, `GET /me`, the session
lifecycle, and `POST /sessions/{sid}/op` with its 400/404/409 contract. Every
op lands in `.ops`, every request in `.requests`, so a test can assert what the
server actually sent. The same process also plays the OAuth token endpoint, so
the CLI-login refresh path can be tested without Clerk.
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
        self._n = 0
        self._server = None
        self._thread = None
        self.me_patch = None          # merged over GET /me; None drops a key
        self.profile_id = "prof-1"    # the account's saved phone; None: this service keeps none
        self.saving_requests = 0      # a stopped saved-phone session stays "closing" this many requests
        self.links = True             # False: an older service with no screenshot or handoff routes
        self.shots = {}               # id -> (token, png bytes)
        self.handoffs = {}            # id -> record, oldest first
        self.live_views = {}          # id -> record and the body it was asked with
        self.oauth_scopes = {}        # bearer -> its OAuth scope string, for tokens a test's authorization server issued

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
        split = urllib.parse.urlsplit(h.path)
        path = split.path
        auths = h.headers.get_all("Authorization") or []
        bearer = auths[0][7:] if auths and auths[0].lower().startswith("bearer ") else None
        with self.lock:
            self.requests.append((method, path, bearer))
            self._tick_closing()
        try:
            if path == "/oauth/token" and method == "POST":
                return self._token_endpoint(h)
            if path == "/health":
                return self._send(h, 200, {"ok": True})
            if path.startswith("/screenshots/") and method == "GET":
                return self._screenshot(h, path, split.query)
            if len(auths) > 1:
                raise _Reply(400, {"error": "one Authorization header"})
            user = self.tokens.get(bearer) if bearer else None
            # Like the API: a self-registered OAuth client's token signs in only
            # when it carries the scope the MCP server advertises.
            if user and bearer in self.oauth_scopes and "phone:drive" not in self.oauth_scopes[bearer].split():
                user = None
            if not user:
                raise _Reply(401, {"error": "sign in"})
            status, body, headers = self.route(method, path, user, h)
            self._send(h, status, body, headers)
        except _Reply as r:
            self._send(h, r.status, r.body, r.headers)

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
        if len(parts) >= 2 and parts[0] == "sessions":
            s = self.sessions.get(parts[1])
            if not s or s["uid"] != user["uid"] or s["state"] == "closed":
                raise _Reply(404, {"error": "no such session"})
            if len(parts) == 2 and method == "GET":
                self.tick(s)
                return 200, self.public(s), None
            if len(parts) == 2 and method == "DELETE":
                if s.get("profile") and self.saving_requests:
                    # the saved phone is being stored: closing, still holding it
                    s.update(state="closing", close_requested=time.time(), closing_left=self.saving_requests)
                else:
                    s["state"] = "closed"
                return 200, {"released": True}, None
            if len(parts) == 3 and parts[2] in ("screenshots", "handoffs", "live-views") and not self.links:
                raise _Reply(404, {"error": f"no route {method} {path}"})
            if len(parts) == 3 and parts[2] == "screenshots" and method == "POST":
                return self.screenshot_link(s, self._body(h))
            if len(parts) == 3 and parts[2] == "handoffs":
                return self.handoff_route(s, method, self._body(h) if method == "POST" else {})
            if len(parts) == 3 and parts[2] == "live-views" and method == "POST":
                return self.live_view(s, self._body(h))
            if len(parts) == 3 and parts[2] == "ops" and method == "GET":
                if s["state"] != "ready":
                    raise _Reply(409, {"error": "the phone is not ready"})
                return 200, {"ops": [op for op in PUBLIC_OPS if op not in self.unsupported(s)]}, None
            if len(parts) == 3 and parts[2] == "op" and method == "POST":
                return self.run_op(s, self._body(h))
        raise _Reply(404, {"error": "not found"})

    def me(self, user):
        holder = self._profile_holder()
        saved = []
        if self.profile_id:
            saved = [{"platform": "android", "kind": "emulator", "label": "Saved phone", "id": self.profile_id,
                      "state": "running" if holder else "stored", "session": holder["id"] if holder else None}]
        body = {"uid": user["uid"], "email": user["email"], "balance_cents": 1250, "price_cents_per_minute": 5,
                "billing_mode": "credits", "session_limit": 2,
                "unnamed": {"platform": "android", "kind": "emulator", "temporary": True},
                "available": saved + [
                    {"platform": "android", "kind": "emulator", "label": "Android emulator", "temporary": True},
                    {"platform": "ios", "kind": "device", "label": "My iPhone", "id": "iph-a1b2c3",
                     "state": "available"},
                ]}
        if self.profile_id:
            body["profile"] = {"id": self.profile_id, "state": "running" if holder else "stored",
                               "session": holder["id"] if holder else None}
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
        profile = body.get("profile_id")
        if profile is not None:
            if profile != self.profile_id:
                raise _Reply(403, {"error": "That profile belongs to another account"})
            holder = self._profile_holder()
            if holder:
                raise _Reply(409, {"error": "Your phone is already running", "code": "profile_running",
                                   "session": holder["id"]})
        timeout = int(body.get("timeout_seconds") or 300)
        self._n += 1
        sid = f"s_{self._n:04d}"
        s = {"id": sid, "uid": user["uid"], "state": "provisioning", "platform": platform, "kind": kind,
             "device": "iPhone" if platform == "ios" else "Android emulator", "polls": 0,
             "timeout_seconds": timeout, "expires_at": time.time() + timeout, "created": body,
             "progress": {"phase": "booting"}, "profile": profile}
        self.sessions[sid] = s
        return 202, self.public(s), None

    def tick(self, s):
        if s["state"] == "provisioning":
            s["polls"] += 1
            if s["polls"] >= self.ready_after:
                s["state"] = "ready"
                s.pop("progress", None)

    def _profile_holder(self):
        """The session holding the saved phone: live, or closing while it is saved."""
        return next((s for s in self.sessions.values()
                     if s.get("profile") and s["state"] != "closed"), None)

    def _tick_closing(self):
        for s in self.sessions.values():
            if s["state"] == "closing" and s.get("closing_left") is not None:
                s["closing_left"] -= 1
                if s["closing_left"] <= 0:
                    s["state"] = "closed"

    # --- links -------------------------------------------------------------------

    def screenshot_link(self, s, body):
        if s["state"] != "ready":
            raise _Reply(409, {"error": "session is not ready"})
        ttl = body.get("ttl_seconds", 600)
        if type(ttl) is not int or not 30 <= ttl <= 900:
            raise _Reply(400, {"error": "ttl_seconds must be a whole number from 30 to 900"})
        shot = f"{len(self.shots) + 1:016x}"
        token = "phs_" + base64.urlsafe_b64encode(shot.encode()).decode().rstrip("=")
        png = base64.b64decode(self._png)
        self.shots[shot] = (token, png)
        return 201, {"id": shot, "url": f"{self.url}/screenshots/{shot}.png?token={token}",
                     "expires_at": time.time() + ttl, "content_type": "image/png", "bytes": len(png),
                     "width": CAPTURE_SIZE[0], "height": CAPTURE_SIZE[1],
                     "screen": {"w": BOUNDS["w"], "h": BOUNDS["h"]}}, None

    def _screenshot(self, h, path, query):
        shot = path.rsplit("/", 1)[1].split(".", 1)[0]
        token = dict(urllib.parse.parse_qsl(query)).get("token")
        found = self.shots.get(shot)
        if not found or found[0] != token:
            return self._send(h, 404, {"error": "This screenshot link has expired."})
        h.send_response(200)
        h.send_header("Content-Type", "image/png")
        h.send_header("Content-Length", str(len(found[1])))
        h.end_headers()
        h.wfile.write(found[1])

    def live_view(self, s, body):
        """POST /sessions/{id}/live-views, as phone-cloud answers it."""
        if s["state"] != "ready":
            raise _Reply(409, {"error": "session is not ready"})
        if set(body) - {"mode", "ttl_seconds"}:
            raise _Reply(400, {"error": "a live view takes only mode and ttl_seconds"})
        mode, ttl = body.get("mode", "view"), body.get("ttl_seconds")
        if mode not in ("view", "control"):
            raise _Reply(400, {"error": "mode must be view or control"})
        if ttl is not None and (type(ttl) is not int or not 60 <= ttl <= 21600):
            raise _Reply(400, {"error": "ttl_seconds must be a whole number from 60 to 21600"})
        lid = f"{len(self.live_views) + 1:016x}"
        record = {"id": lid, "mode": mode, "state": "live", "reason": None, "created_at": time.time(),
                  "expires_at": time.time() + (21600 if ttl is None else ttl), "ended_at": None, "opened_at": None,
                  "inputs": 0, "last_input_at": None}
        self.live_views[lid] = {**record, "sid": s["id"], "body": dict(body)}
        return 201, {**record, "url": f"{self.url}/live/{lid}#token=phv_{lid}"}, None

    def handoff_route(self, s, method, body):
        mine = [r for r in self.handoffs.values() if r["sid"] == s["id"]]
        if method == "POST":
            if s["state"] != "ready":
                raise _Reply(409, {"error": "session is not ready"})
            ttl = body.get("ttl_seconds", 600)
            if type(ttl) is not int or not 60 <= ttl <= 1800:
                raise _Reply(400, {"error": "ttl_seconds must be a whole number from 60 to 1800"})
            for r in mine:
                if r["state"] in ("waiting", "active"):
                    r.update(state="revoked", reason="replaced", ended_at=time.time())
            hid = f"{len(self.handoffs) + 1:016x}"
            record = {"id": hid, "sid": s["id"], "state": "waiting", "reason": None, "created_at": time.time(),
                      "expires_at": time.time() + ttl, "claimed_at": None, "ended_at": None, "inputs": 0,
                      "last_input_at": None}
            self.handoffs[hid] = record
            view = {k: v for k, v in record.items() if k != "sid"}
            return 201, {**view, "url": f"{self.url}/handoff/{hid}?token=phl_{hid}"}, None
        if method == "GET":
            return 200, {"handoffs": [{k: v for k, v in r.items() if k != "sid"} for r in reversed(mine)]}, None
        if method == "DELETE":
            live = [r for r in mine if r["state"] in ("waiting", "active")]
            for r in live:
                r.update(state="revoked", reason="revoked", ended_at=time.time())
            return 200, {"revoked": len(live)}, None
        raise _Reply(404, {"error": "not found"})

    @staticmethod
    def public(s):
        out = {k: v for k, v in s.items() if k not in ("uid", "polls", "created", "closing_left")}
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

    def answer(self, s, op, kw):
        if op == "screen.capture":
            return {"png_b64": self._png, "bounds": dict(BOUNDS)}
        if op in ("screen.bounds", "screen.require"):
            return dict(BOUNDS)
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


class _Reply(Exception):
    def __init__(self, status, body, headers=None):
        self.status, self.body, self.headers = status, body, headers
