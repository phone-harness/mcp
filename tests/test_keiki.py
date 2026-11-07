"""Out of the box in a Keiki iMessage agent (PHO-224).

A Keiki agent reaches the hosted server as a per-person MCP connection: it
registers itself, the person signs in once, and its code-mode worker calls
the tools and reads `structuredContent`. These tests are that agent's path,
against the fake API and a stand-in authorization server:

- the OAuth connect and tool discovery a self-registering client goes through;
- the saved Android phone, started or reattached, with nothing named;
- a screenshot as an HTTPS link, never image bytes or a key in a URL;
- a handoff link for the person, with a ready iMessage app card.
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

import phone_harness_mcp.server as server_module
from fake_api import BOUNDS, CAPTURE_SIZE, solid_png
from keiki_style_client import KEIKI_REDIRECT, KeikiStyleClient, check_presentation, parse_challenge
from mock_oauth import MockAuthServer
from phone_harness_mcp.cli import build_http_app
from support import connected, start, stdio_server, text_of

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


# --- connect and discover ----------------------------------------------------------

async def test_a_keiki_style_client_signs_in_and_finds_the_tools(hosted, api, auth):
    client = KeikiStyleClient(hosted)
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
    assert auth.registrations[-1]["redirect_uris"] == [KEIKI_REDIRECT]
    assert registration["token_endpoint_auth_method"] == "none"

    location = client.approve(client.authorize_url())
    asked = auth.authorizations[-1]
    assert (asked["scope"], asked["code_challenge_method"], asked["resource"]) == ("phone:drive", "S256",
                                                                                  client.resource_metadata["resource"])
    assert location.startswith(KEIKI_REDIRECT + "?")
    tokens = client.finish(location)
    assert tokens["scope"] == "phone:drive"

    async with client.session() as s:
        listed = await s.list_tools()
        r = await s.call_tool("phone_list_available_devices", {})
    names = {t.name for t in listed.tools}
    assert NEW_TOOLS <= names
    assert "phone_save_screenshot" not in names                  # the hosted server writes no files
    # Keiki's code mode calls tools by name from JavaScript.
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", n) for n in names)
    by_name = {t.name: t for t in listed.tools}
    for name in ("phone_get_screenshot_url", "phone_start_handoff", "phone_get_handoff"):
        assert by_name[name].output_schema, f"{name} has no output schema for code mode"
    assert by_name["phone_get_handoff"].annotations.read_only_hint is True
    assert by_name["phone_start_handoff"].annotations.open_world_hint is True
    assert not r.is_error and "Saved phone" in text_of(r)
    # The verifier asked the API about the token it was handed, and nothing else.
    assert {b for _m, p, b in api.requests if p == "/me"} == {tokens["access_token"]}


async def test_a_token_without_the_scope_is_refused_with_the_scope_named(hosted, api, auth):
    client = KeikiStyleClient(hosted, scope="openid profile")
    client.connect()
    req = urllib.request.Request(hosted, data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          "Authorization": f"Bearer {client.tokens['access_token']}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 401
    challenge = parse_challenge(e.value.headers["WWW-Authenticate"])
    assert challenge["scope"] == "phone:drive"                  # what to ask for on the retry


async def test_the_sdk_oauth_client_connects_out_of_the_box(hosted, api, auth):
    """The reference client: registration, PKCE and the scope choice are its own."""
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
        location = KeikiStyleClient(hosted).approve(seen["url"])
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        return AuthorizationCodeResult(code=query["code"], state=query.get("state"), iss=query.get("iss"))

    provider = OAuthClientProvider(
        server_url=hosted,
        client_metadata=OAuthClientMetadata(client_name="sdk-client", redirect_uris=[KEIKI_REDIRECT],
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


async def test_the_whole_agent_turn_in_code_mode(hosted, api, auth):
    """What a Keiki worker does with the structured results: no image bytes, no key in a URL."""
    client = KeikiStyleClient(hosted)
    token = client.connect()
    async with client.session() as s:
        started = await s.call_tool("phone_start_device", {})
        assert not started.is_error, text_of(started)
        sid = text_of(started).split("as device ", 1)[1].split(";", 1)[0].split(".", 1)[0]
        assert api.sessions[sid]["created"]["profile_id"] == "prof-1"

        shot = await s.call_tool("phone_get_screenshot_url", {"device": sid})
        assert not shot.is_error, text_of(shot)
        assert all(c.type == "text" for c in shot.content)           # nothing but words in the result
        link = shot.structured_content
        assert token not in link["url"] and "pck_" not in link["url"]
        with urllib.request.urlopen(link["url"], timeout=10) as r:      # Keiki's download_url: no credential
            assert r.read() == solid_png(*CAPTURE_SIZE)
        assert (link["width"], link["height"], link["screen"]) == (*CAPTURE_SIZE, {"w": BOUNDS["w"], "h": BOUNDS["h"]})

        handoff = await s.call_tool("phone_start_handoff", {"device": sid, "minutes": 5})
        assert not handoff.is_error, text_of(handoff)
        data = handoff.structured_content
        check_presentation(data["presentation"], https=False)
        assert data["presentation"]["url"] == data["url"] and token not in data["url"]

        status = await s.call_tool("phone_get_handoff", {"device": sid})
        assert status.structured_content["state"] == "waiting"
        ended = await s.call_tool("phone_end_handoff", {"device": sid})
        assert "Turned off" in text_of(ended)
        stopped = await s.call_tool("phone_stop_device", {"device": sid})
        assert not stopped.is_error


# --- the saved Android phone ---------------------------------------------------------

async def test_nothing_named_starts_the_saved_phone_then_reattaches(api):
    async with connected(stdio_server(api)) as s:
        first = text_of(await s.call_tool("phone_start_device", {}))
        assert first.startswith("Started the saved phone as device s_0001.")
        again = await s.call_tool("phone_start_device", {})
    assert not again.is_error
    assert text_of(again).startswith("The saved phone is already running as device s_0001;")
    assert list(api.sessions) == ["s_0001"]                      # no second phone was started


async def test_a_start_that_loses_the_race_reattaches(api):
    """/me said the saved phone was free; by the POST it was not (409 profile_running)."""
    holder = api.create({"uid": "u_1"}, {"platform": "android", "kind": "emulator", "profile_id": "prof-1"})[1]
    api.me_patch = {"profile": {"id": "prof-1", "state": "stored", "session": None}}
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_start_device", {})
    assert not r.is_error, text_of(r)
    assert f"already running as device {holder['id']}" in text_of(r)
    posts = [p for m, p, _ in api.requests if m == "POST" and p == "/sessions"]
    assert len(posts) == 1 and len(api.sessions) == 1


async def test_a_saved_phone_still_being_saved_is_waited_for(api, monkeypatch):
    monkeypatch.setattr(server_module, "SAVED_RETRY_SECONDS", 0.05)
    api.saving_requests = 4
    async with connected(stdio_server(api)) as s:
        first = await start(s)
        await s.call_tool("phone_stop_device", {"device": first})
        assert api.sessions[first]["state"] == "closing"
        r = await s.call_tool("phone_start_device", {})
    assert not r.is_error, text_of(r)
    assert text_of(r).startswith("Started the saved phone as device s_0002.")
    refused = [p for m, p, _ in api.requests if m == "POST" and p == "/sessions"]
    assert len(refused) >= 2                                      # at least one 409 before the start


async def test_temporary_is_a_throwaway_emulator(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s, temporary=True)
        refused = await s.call_tool("phone_start_device", {"temporary": True, "device_id": "iph-a1b2c3"})
        also = await s.call_tool("phone_start_device", {"temporary": True, "platform": "ios", "kind": "device"})
    assert api.sessions[sid]["created"] == {"timeout_seconds": 1800, "platform": "android", "kind": "emulator"}
    assert refused.is_error and also.is_error and "throwaway Android" in text_of(refused)


async def test_without_a_saved_phone_nothing_named_is_the_endpoint_rule(api):
    api.profile_id = None
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
    assert api.sessions[sid]["created"] == {"timeout_seconds": 1800}


async def test_naming_the_saved_phone_by_id_also_reattaches(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_start_device", {"device_id": "prof-1"})
    assert f"already running as device {sid}" in text_of(r)


# --- screenshot links ------------------------------------------------------------------

async def test_a_screenshot_url_is_text_and_structured_data(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_get_screenshot_url", {"device": sid, "minutes": 2})
        too_long = await s.call_tool("phone_get_screenshot_url", {"device": sid, "minutes": 16})
        too_short = await s.call_tool("phone_get_screenshot_url", {"device": sid, "minutes": 0})
    assert not r.is_error
    text = text_of(r)
    url = r.structured_content["url"]
    assert text.splitlines()[0] == f"Screenshot link (this one image, for 2 min): {url}"
    assert "multiply its x by 0.5 and y by 0.5" in text
    assert url.startswith(api.url + "/screenshots/") and "token=phs_" in url
    assert r.structured_content["expires_at"] - time.time() <= 121
    assert too_long.is_error and too_short.is_error and "1 to 15 minutes" in text_of(too_long)


async def test_an_older_service_says_it_has_no_links(api):
    api.links = False
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        shot = await s.call_tool("phone_get_screenshot_url", {"device": sid})
        handoff = await s.call_tool("phone_start_handoff", {"device": sid})
    assert shot.is_error and "does not offer screenshot links yet" in text_of(shot)
    assert handoff.is_error and "does not offer handoffs yet" in text_of(handoff)


async def test_a_screenshot_link_for_a_device_that_is_not_running(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_get_screenshot_url", {"device": "s_nope"})
    assert r.is_error and "device s_nope is not running" in text_of(r)


# --- handoffs ------------------------------------------------------------------------------

async def test_a_handoff_carries_an_imessage_app_card(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_start_handoff", {"device": sid, "minutes": 30})
        bad = await s.call_tool("phone_start_handoff", {"device": sid, "minutes": 31})
    assert not r.is_error
    data = r.structured_content
    assert data["state"] == "waiting"
    assert data["expires_at"] - time.time() <= 1801
    check_presentation(data["presentation"], https=False)
    assert data["presentation"]["id"] == f"phone-handoff-{data['handoff_id']}"
    assert data["presentation"]["subcaption"] == "Works once, for 30 min"
    assert data["url"] in text_of(r) and "Don't drive the phone" in text_of(r)
    assert bad.is_error and "1 to 30 minutes" in text_of(bad)


async def test_handoff_status_follows_the_person(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        none = await s.call_tool("phone_get_handoff", {"device": sid})
        first = (await s.call_tool("phone_start_handoff", {"device": sid})).structured_content
        waiting = await s.call_tool("phone_get_handoff", {"device": sid})
        api.handoffs[first["handoff_id"]].update(state="active", claimed_at=time.time(), inputs=3)
        active = await s.call_tool("phone_get_handoff", {"device": sid})
        api.handoffs[first["handoff_id"]].update(state="ended", reason="done", ended_at=time.time())
        ended = await s.call_tool("phone_get_handoff", {"device": sid})
        again = (await s.call_tool("phone_start_handoff", {"device": sid})).structured_content
        await s.call_tool("phone_start_handoff", {"device": sid})
        replaced = api.handoffs[again["handoff_id"]]
        nothing = await s.call_tool("phone_end_handoff", {"device": sid})
        twice = await s.call_tool("phone_end_handoff", {"device": sid})
    assert none.structured_content["state"] == "none" and "No handoff" in text_of(none)
    assert waiting.structured_content["state"] == "waiting" and "has not opened" in text_of(waiting)
    assert active.structured_content["inputs"] == 3 and "Don't drive it" in text_of(active)
    assert "handed the phone back" in text_of(ended) and "Take a screenshot" in text_of(ended)
    assert (replaced["state"], replaced["reason"]) == ("revoked", "replaced")
    assert "Turned off" in text_of(nothing) and "No handoff link was live" in text_of(twice)
