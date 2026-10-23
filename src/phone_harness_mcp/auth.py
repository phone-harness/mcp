"""Who is calling, when this server is hosted.

Hosted (`--port`), the server holds no credential of its own. Each HTTP
request must carry `Authorization: Bearer …`, and that bearer is forwarded
unchanged to Phone Harness Cloud, the only party that can say whether it is
good. In OAuth terms this process is a resource server and nothing more: it
never mints, stores or decodes a token.

Two pieces:

- `RequireBearer`: ASGI middleware for API-key mode. A request without a
  bearer gets 401 and a `WWW-Authenticate` challenge, so a client shows the
  user a sentence instead of a stack trace.
- `CloudTokenVerifier`: the SDK's `TokenVerifier` protocol, for OAuth mode. It
  asks the API `GET /me` with the bearer and caches the answer briefly. Scopes
  are not inspected here: the API is the policy.
"""
from __future__ import annotations

import hashlib
import time

import anyio
from mcp.server.auth.provider import AccessToken
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .cloud import Cloud, CloudError, StaticToken, Unreachable

CHALLENGE = 'Bearer realm="phone-harness"'
HELP = "Send `Authorization: Bearer <Phone Harness API key>` with every request."


def bearer_of(ctx) -> str | None:
    """The bearer on the HTTP request behind this tool call; None over stdio."""
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError):
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get("authorization") or ""
    if not value.lower().startswith("bearer "):
        return None
    return value[7:].strip() or None


class RequireBearer:
    """401 for a request under `path` that carries no bearer. Everything else passes through."""

    def __init__(self, app, path: str = "/mcp", resource_metadata_url: str | None = None):
        self.app = app
        self.path = path.rstrip("/") or "/"
        self.resource_metadata_url = resource_metadata_url

    def _challenge(self) -> str:
        if self.resource_metadata_url:
            return f'{CHALLENGE}, resource_metadata="{self.resource_metadata_url}"'
        return CHALLENGE

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (scope["path"] == self.path or scope["path"].startswith(self.path + "/")):
            await self.app(scope, receive, send)
            return
        values = Headers(scope=scope).getlist("authorization")
        if len(values) > 1:
            response = JSONResponse({"error": "bad_request", "error_description": "one Authorization header"},
                                    status_code=400)
        elif not values or not values[0].lower().startswith("bearer ") or not values[0][7:].strip():
            response = JSONResponse({"error": "unauthorized", "error_description": HELP}, status_code=401,
                                    headers={"WWW-Authenticate": self._challenge()})
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


class CloudTokenVerifier:
    """Is this bearer good for Phone Harness Cloud? Ask the API, remember briefly."""

    def __init__(self, api_url: str | None = None, scopes: list[str] | None = None,
                 ttl: float = 60, negative_ttl: float = 10):
        self.api_url = api_url
        # The scopes a bearer the API accepts is deemed to hold. The API is the
        # policy: if `GET /me` answered, this credential may drive phones, whether
        # it is an API key (no scopes of its own) or an OAuth token the API already
        # checked for the scope it requires.
        self.scopes = list(scopes or [])
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    def _me(self, token: str) -> dict | None:
        try:
            return Cloud(StaticToken(token), self.api_url).request("GET", "/me")
        except CloudError as e:
            if e.status in (401, 403):
                return None
            raise
        except Unreachable:
            return None

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        me = await anyio.to_thread.run_sync(self._me, token)
        if not isinstance(me, dict):
            self._cache[key] = (now + self.negative_ttl, None)
            return None
        access = AccessToken(token=token, client_id="phone-harness-cloud", scopes=list(self.scopes),
                             subject=str(me.get("uid") or me.get("id") or "") or None)
        self._cache[key] = (now + self.ttl, access)
        if len(self._cache) > 4096:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return access
