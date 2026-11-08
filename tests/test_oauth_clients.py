"""Generic MCP clients against the hosted server in OAuth mode.

Three clients, none of them tied to one product: a client written step by step
from the MCP authorization spec (spec_client.py), the SDK's own OAuth client,
and a plain API-key bearer. The authorization server is the stand-in in
mock_oauth.py; the API is the fake one, which, like the real API, signs in a
self-registered client only with phone:drive.
"""
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx2
import pytest
import uvicorn
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from fake_api import BOUNDS, CAPTURE_SIZE, solid_png
from mock_oauth import MockAuthServer
from phone_harness_mcp.cli import build_http_app
from spec_client import CALLBACK, SpecClient, parse_challenge
from support import text_of

pytestmark = pytest.mark.anyio

NEW_TOOLS = {"phone_get_screenshot_url", "phone_start_handoff", "phone_get_handoff", "phone_end_handoff"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def auth(api):
    server = MockAuthServer(api).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def hosted(api, auth):
    """The hosted server in OAuth mode, its authorization server the stand-in."""
    port = free_port()
    public = f"http://127.0.0.1:{port}/mcp"
    app = build_http_app(api=api.url, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None, allowed_origins=None,
                         public_url=public, oauth_issuer=auth.url, stateful=False, oauth_scope="phone:drive")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started
    try:
        yield public
    finally:
        srv.should_exit = True
        thread.join(5)


async def test_a_spec_client_signs_in_and_finds_the_tools(hosted, api, auth):
    client = SpecClient(hosted)
    challenge = client.discover()
    # The challenge names the metadata and the scope, so a client that asks
    # for exactly what it is told gets a token the API accepts.
    assert challenge["scheme"] == "Bearer"
    assert challenge["resource_metadata"].endswith("/.well-known/oauth-protected-resource/mcp")
    assert challenge["scope"] == "phone:drive"
    assert client.resource_metadata["authorization_servers"] == [auth.url]
    assert client.resource_metadata["scopes_supported"] == ["phone:drive"]
    assert client.resource_metadata["resource"].rstrip("/") == hosted
    assert "S256" in client.as_metadata["code_challenge_methods_supported"]

    registration = client.register()
    assert auth.registrations[-1]["redirect_uris"] == [CALLBACK]
    assert registration["token_endpoint_auth_method"] == "none"           # a public client: no secret

    location = client.approve(client.authorize_url())
    asked = auth.authorizations[-1]
    assert (asked["scope"], asked["code_challenge_method"], asked["resource"]) == ("phone:drive", "S256",
                                                                                  client.resource_metadata["resource"])
    assert location.startswith(CALLBACK + "?")
    tokens = client.finish(location)
    assert tokens["scope"] == "phone:drive"

    async with client.session() as s:
        listed = await s.list_tools()
        r = await s.call_tool("phone_list_available_devices", {})
    names = {t.name for t in listed.tools}
    assert NEW_TOOLS <= names
    assert "phone_save_screenshot" not in names                  # the hosted server writes no files
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", n) for n in names)   # identifiers in any client language
    by_name = {t.name: t for t in listed.tools}
    for name in ("phone_get_screenshot_url", "phone_start_handoff", "phone_get_handoff"):
        assert by_name[name].output_schema, f"{name} has no output schema"
    assert by_name["phone_get_handoff"].annotations.read_only_hint is True
    assert by_name["phone_start_handoff"].annotations.open_world_hint is True
    assert not r.is_error and "Saved phone" in text_of(r)
    # The verifier asked the API about the token it was handed, and nothing else.
    assert {b for _m, p, b in api.requests if p == "/me"} == {tokens["access_token"]}


async def test_a_loopback_callback_works_too(hosted, api, auth):
    """Desktop clients register a loopback callback (RFC 8252) instead of an https one."""
    client = SpecClient(hosted, redirect_uri="http://127.0.0.1:53682/callback")
    client.connect()
    async with client.session() as s:
        r = await s.call_tool("phone_list_available_devices", {})
    assert not r.is_error


async def test_a_token_without_the_scope_is_refused_with_the_scope_named(hosted, api, auth):
    client = SpecClient(hosted, scope="openid profile")
    client.connect()
    req = urllib.request.Request(hosted, data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          "Authorization": f"Bearer {client.tokens['access_token']}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 401
    assert parse_challenge(e.value.headers["WWW-Authenticate"])["scope"] == "phone:drive"   # what to ask for next


async def test_the_sdk_oauth_client_connects(hosted, api, auth):
    """The SDK's reference client: registration, PKCE and the scope choice are its own."""
    class Memory:
        def __init__(self):
            self.tokens = self.info = None

        async def get_tokens(self) -> OAuthToken | None:
            return self.tokens

        async def set_tokens(self, tokens: OAuthToken) -> None:
            self.tokens = tokens

        async def get_client_info(self) -> OAuthClientInformationFull | None:
            return self.info

        async def set_client_info(self, info: OAuthClientInformationFull) -> None:
            self.info = info

    seen = {}

    async def redirect(url: str) -> None:
        seen["url"] = url

    async def callback() -> AuthorizationCodeResult:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(SpecClient.approve(seen["url"])).query))
        return AuthorizationCodeResult(code=query["code"], state=query.get("state"), iss=query.get("iss"))

    provider = OAuthClientProvider(
        server_url=hosted,
        client_metadata=OAuthClientMetadata(client_name="sdk-client", redirect_uris=["http://127.0.0.1:53682/callback"],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=Memory(), redirect_handler=redirect, callback_handler=callback)
    async with httpx2.AsyncClient(auth=provider, timeout=60) as http:
        async with streamable_http_client(hosted, http_client=http) as (read, write, *_rest):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = await s.list_tools()
    assert NEW_TOOLS <= {t.name for t in tools.tools}
    assert auth.authorizations[-1]["scope"].split()[0] == "phone:drive"


async def test_structured_results_without_bytes_or_keys(hosted, api, auth):
    """A client that reads structuredContent: the phone, a screenshot link fetched with no
    credential, a handoff link, its status, then stop. No image bytes, no key in a URL."""
    client = SpecClient(hosted)
    token = client.connect()
    async with client.session() as s:
        started = await s.call_tool("phone_start_device", {})
        assert not started.is_error, text_of(started)
        sid = text_of(started).split("as device ", 1)[1].split(";", 1)[0].split(".", 1)[0]
        assert api.sessions[sid]["created"]["profile_id"] == "prof-1"

        shot = await s.call_tool("phone_get_screenshot_url", {"device": sid})
        assert not shot.is_error, text_of(shot)
        assert all(c.type == "text" for c in shot.content)
        link = shot.structured_content
        assert token not in link["url"] and "pck_" not in link["url"]
        with urllib.request.urlopen(link["url"], timeout=10) as r:      # no Authorization header
            assert r.read() == solid_png(*CAPTURE_SIZE)
        assert (link["width"], link["height"], link["screen"]) == (*CAPTURE_SIZE, {"w": BOUNDS["w"], "h": BOUNDS["h"]})

        handoff = await s.call_tool("phone_start_handoff", {"device": sid, "minutes": 5})
        assert not handoff.is_error, text_of(handoff)
        assert set(handoff.structured_content) == {"handoff_id", "url", "expires_at", "state"}
        assert token not in handoff.structured_content["url"]

        status = await s.call_tool("phone_get_handoff", {"device": sid})
        assert status.structured_content["state"] == "waiting"
        assert "Turned off" in text_of(await s.call_tool("phone_end_handoff", {"device": sid}))
        assert not (await s.call_tool("phone_stop_device", {"device": sid})).is_error
