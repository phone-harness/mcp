"""A client that connects to the hosted server the way Keiki does.

Keiki (onkeiki.com) gives each person their own MCP connection: an
`mcp.userConnections` entry names the endpoint, and with no `oauth` block it
registers itself with the authorization server (RFC 7591) and runs the
authorization-code grant with PKCE, its callback at
https://onkeiki.com/api/webapp/user-oauth/callback. Its signup is invite-only,
so this is the stand-in: every step written out from the MCP authorization
spec rather than borrowed from the SDK's own client, so a server that only
works with one client library does not pass by accident.

    client = KeikiStyleClient("https://mcp.phone-harness.com/mcp")
    challenge = client.discover()          # 401 -> resource metadata -> AS metadata
    client.register()                      # dynamic client registration
    location = client.authorize_url()      # the link the person would open
    client.finish(approve(location))       # the code from the redirect -> tokens
    async with client.session() as mcp:    # streamable HTTP with the bearer
        ...
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

KEIKI_REDIRECT = "https://onkeiki.com/api/webapp/user-oauth/callback"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "keiki-style-client", "version": "0"}}}


def parse_challenge(value: str) -> dict:
    """`Bearer a="b", c="d"` -> {"scheme": "Bearer", "a": "b", "c": "d"}."""
    scheme, _, rest = value.partition(" ")
    out = {"scheme": scheme}
    out.update(dict(re.findall(r'([A-Za-z_]+)="([^"]*)"', rest)))
    return out


def _json(url, data=None, headers=None, form=False):
    body = None
    head = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            head["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            head["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=head, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class KeikiStyleClient:
    def __init__(self, mcp_url: str, redirect_uri: str = KEIKI_REDIRECT, client_name: str = "Keiki",
                 scope: str | None = None, rewrite=None):
        """`rewrite(url) -> url` is applied to every address the server's
        metadata hands back before it is fetched: for running this on the host
        behind a proxy, where the public address does not reach back in."""
        self.mcp_url = mcp_url
        self.redirect_uri = redirect_uri
        self.client_name = client_name
        self.scope_override = scope
        self.rewrite = rewrite or (lambda url: url)
        self.challenge = self.resource_metadata = self.as_metadata = self.registration = None
        self.tokens = None
        self.verifier = secrets.token_urlsafe(48)
        self.state = secrets.token_urlsafe(16)

    # --- discovery ---------------------------------------------------------------

    def discover(self) -> dict:
        """POST initialize with no credential; the 401 says where to go next."""
        req = urllib.request.Request(self.mcp_url, data=json.dumps(INITIALIZE).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        try:
            urllib.request.urlopen(req, timeout=15)
            raise AssertionError("the MCP endpoint answered without a credential")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.challenge = parse_challenge(e.headers["WWW-Authenticate"])
        prm_url = self.challenge.get("resource_metadata")
        if not prm_url:
            # RFC 9728 well-known location, path-aware first
            split = urllib.parse.urlsplit(self.mcp_url)
            prm_url = f"{split.scheme}://{split.netloc}/.well-known/oauth-protected-resource{split.path}"
        self.resource_metadata = _json(self.rewrite(prm_url))
        issuer = self.resource_metadata["authorization_servers"][0]
        split = urllib.parse.urlsplit(self.rewrite(issuer))
        path = split.path.rstrip("/")
        for well_known in (f"/.well-known/oauth-authorization-server{path}", f"/.well-known/openid-configuration{path}",
                           f"{path}/.well-known/openid-configuration"):
            try:
                self.as_metadata = _json(f"{split.scheme}://{split.netloc}{well_known}")
                break
            except urllib.error.HTTPError:
                continue
        if self.as_metadata is None:
            raise AssertionError("no authorization server metadata")
        return self.challenge

    @property
    def scope(self) -> str | None:
        """MCP's scope selection: the challenge's, else the resource's scopes_supported."""
        if self.scope_override is not None:
            return self.scope_override
        if self.challenge and self.challenge.get("scope"):
            return self.challenge["scope"]
        supported = (self.resource_metadata or {}).get("scopes_supported")
        return " ".join(supported) if supported else None

    # --- registration and the grant --------------------------------------------------

    def register(self) -> dict:
        body = {"client_name": self.client_name, "redirect_uris": [self.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
        if self.scope:
            body["scope"] = self.scope
        self.registration = _json(self.rewrite(self.as_metadata["registration_endpoint"]), body)
        return self.registration

    def authorize_url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        params = {"response_type": "code", "client_id": self.registration["client_id"],
                  "redirect_uri": self.redirect_uri, "state": self.state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "resource": self.resource_metadata["resource"]}
        if self.scope:
            params["scope"] = self.scope
        return self.as_metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    def approve(self, url: str) -> str:
        """Stands in for the person pressing Allow: the authorization server's
        redirect back to the callback, which is not fetched."""
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(self.rewrite(url), timeout=15)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307):
                return e.headers["Location"]
            raise
        raise AssertionError("the authorization server did not redirect")

    def finish(self, location: str) -> dict:
        """The callback's query -> tokens."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        if not location.startswith(self.redirect_uri):
            raise AssertionError(f"redirected somewhere else: {location}")
        if query.get("state") != self.state:
            raise AssertionError("state mismatch")
        if query.get("iss") and query["iss"] != self.as_metadata["issuer"]:
            raise AssertionError("issuer mismatch")
        if "code" not in query:
            raise AssertionError(f"no code: {query}")
        self.tokens = _json(self.rewrite(self.as_metadata["token_endpoint"]), {
            "grant_type": "authorization_code", "code": query["code"], "redirect_uri": self.redirect_uri,
            "client_id": self.registration["client_id"], "code_verifier": self.verifier,
            "resource": self.resource_metadata["resource"]}, form=True)
        return self.tokens

    def connect(self) -> str:
        """All of the above -> the access token."""
        self.discover()
        self.register()
        self.finish(self.approve(self.authorize_url()))
        return self.tokens["access_token"]

    @asynccontextmanager
    async def session(self):
        headers = {"Authorization": f"Bearer {self.tokens['access_token']}"}
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
            async with streamable_http_client(self.mcp_url, http_client=http) as (read, write, *_rest):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    yield s


# Keiki's limits on an iMessage app card (onkeiki.com/docs/imessage-apps).
def check_presentation(p: dict, https: bool = True):
    assert p["type"] == "app"
    assert isinstance(p["id"], str) and 1 <= len(p["id"]) <= 128
    assert isinstance(p["url"], str) and len(p["url"]) <= 2048
    parsed = urllib.parse.urlsplit(p["url"])
    assert parsed.username is None and parsed.password is None          # no embedded credentials
    if https:
        assert parsed.scheme == "https"
    assert 1 <= len(p["caption"]) <= 60
    assert 1 <= len(p["subcaption"]) <= 80
