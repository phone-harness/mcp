"""An OAuth authorization server for tests, standing in for Clerk.

What a self-registering MCP client meets at the authorization server the
hosted server's metadata names: RFC 8414 metadata,
RFC 7591 dynamic client registration, the authorization-code grant with PKCE
(S256 only) and RFC 8707 resource indicators. The consent screen is skipped:
an authorization request from a registered client is approved at once, as if
the person had pressed Allow. Every access token it issues is handed to the
fake API (`FakeCloud.tokens`) with the scope it was granted, and the fake API,
like the real one, signs in a self-registered client only with phone:drive.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCOPES = ("openid", "profile", "email", "phone:drive")


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class MockAuthServer:
    def __init__(self, api, user=None):
        self.api = api
        self.user = user or {"uid": "u_1", "email": "me@example.com"}
        self.clients = {}          # client_id -> registration
        self.codes = {}            # code -> grant
        self.refresh = {}          # refresh token -> grant
        self.registrations = []    # every registration request body, in order
        self.authorizations = []   # every authorization request's query, in order
        self.token_requests = []   # every token request's form, in order
        self.lock = threading.Lock()
        self._server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        auth = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                auth.handle(self, "GET")

            def do_POST(self):
                auth.handle(self, "POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    # --- plumbing ----------------------------------------------------------------

    @staticmethod
    def _send(h, status, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else b""
        h.send_response(status)
        if body is not None:
            h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def metadata(self):
        base = self.url
        return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token", "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                "scopes_supported": list(SCOPES), "client_id_metadata_document_supported": True}

    def handle(self, h, method):
        split = urllib.parse.urlsplit(h.path)
        path, query = split.path, dict(urllib.parse.parse_qsl(split.query))
        if method == "GET" and path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return self._send(h, 200, self.metadata())
        if method == "POST" and path == "/oauth/register":
            n = int(h.headers.get("Content-Length") or 0)
            try:
                body = json.loads(h.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(h, 400, {"error": "invalid_client_metadata"})
            return self.register(h, body)
        if method == "GET" and path == "/oauth/authorize":
            return self.authorize(h, query)
        if method == "POST" and path == "/oauth/token":
            n = int(h.headers.get("Content-Length") or 0)
            form = dict(urllib.parse.parse_qsl(h.rfile.read(n).decode()))
            return self.token(h, form)
        return self._send(h, 404, {"error": "not_found"})

    # --- RFC 7591 ----------------------------------------------------------------

    def register(self, h, body):
        with self.lock:
            self.registrations.append(body)
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            return self._send(h, 400, {"error": "invalid_redirect_uri"})
        for uri in uris:
            parsed = urllib.parse.urlsplit(uri)
            loopback = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
            if not (parsed.scheme == "https" or loopback) or parsed.fragment:
                return self._send(h, 400, {"error": "invalid_redirect_uri"})
        client_id = "dcr_" + secrets.token_hex(8)
        record = {**body, "client_id": client_id, "client_id_issued_at": int(time.time()),
                  "token_endpoint_auth_method": body.get("token_endpoint_auth_method") or "client_secret_basic"}
        if record["token_endpoint_auth_method"] != "none":
            record["client_secret"] = "cs_" + secrets.token_hex(16)
        with self.lock:
            self.clients[client_id] = record
        return self._send(h, 201, record)

    # --- the authorization-code grant ----------------------------------------------

    def authorize(self, h, q):
        with self.lock:
            self.authorizations.append(dict(q))
        client = self.clients.get(q.get("client_id"))
        if client is None or q.get("redirect_uri") not in client["redirect_uris"]:
            # Never redirect to an unregistered URI.
            return self._send(h, 400, {"error": "invalid_request"})
        def back(**params):
            url = q["redirect_uri"] + ("&" if "?" in q["redirect_uri"] else "?") + urllib.parse.urlencode(params)
            return self._send(h, 302, None, {"Location": url})
        if q.get("response_type") != "code":
            return back(error="unsupported_response_type", state=q.get("state", ""))
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return back(error="invalid_request", error_description="PKCE S256 required", state=q.get("state", ""))
        scopes = [s for s in (q.get("scope") or "").split() if s in SCOPES]
        code = "code_" + secrets.token_urlsafe(16)
        with self.lock:
            self.codes[code] = {"client_id": client["client_id"], "redirect_uri": q["redirect_uri"],
                                "challenge": q["code_challenge"], "scope": " ".join(scopes),
                                "resource": q.get("resource"), "at": time.time()}
        return back(code=code, state=q.get("state", ""), iss=self.url)

    def token(self, h, form):
        with self.lock:
            self.token_requests.append(dict(form))
        if form.get("grant_type") == "authorization_code":
            with self.lock:
                grant = self.codes.pop(form.get("code"), None)      # one use
            if (grant is None or grant["client_id"] != form.get("client_id")
                    or grant["redirect_uri"] != form.get("redirect_uri")
                    or pkce_challenge(form.get("code_verifier") or "") != grant["challenge"]
                    or (grant["resource"] and form.get("resource") and form["resource"] != grant["resource"])):
                return self._send(h, 400, {"error": "invalid_grant"})
            return self._issue(h, grant)
        if form.get("grant_type") == "refresh_token":
            with self.lock:
                grant = self.refresh.pop(form.get("refresh_token"), None)
            if grant is None or grant["client_id"] != form.get("client_id"):
                return self._send(h, 400, {"error": "invalid_grant"})
            return self._issue(h, grant)
        return self._send(h, 400, {"error": "unsupported_grant_type"})

    def _issue(self, h, grant):
        access, refresh = "at_" + secrets.token_urlsafe(24), "rt_" + secrets.token_urlsafe(24)
        with self.lock:
            self.refresh[refresh] = grant
            self.api.tokens[access] = dict(self.user)
            self.api.oauth_scopes[access] = grant["scope"]
        return self._send(h, 200, {"access_token": access, "token_type": "Bearer", "expires_in": 3600,
                                   "refresh_token": refresh, "scope": grant["scope"]})
