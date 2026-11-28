"""What a plugin directory's review asks of the server.

ChatGPT's plugin directory bars a plugin from selling subscriptions or linking
to a checkout, wants every tool to state all three annotation hints, and
checks who owns the hostname with a token at a well-known URL. The hosted
server answers with a second endpoint that has no reservation tools and names
no price; the endpoint everyone else uses is unchanged.
"""
import contextlib
import importlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from phone_harness_mcp.cli import build_http_app
from phone_harness_mcp.cloud import Cloud, StaticToken
from phone_harness_mcp.server import DRIVE_ONLY_INSTRUCTIONS, INSTRUCTIONS, build_server
from support import connected, stdio_server

pytestmark = pytest.mark.anyio

RESERVING = {"phone_reserve", "phone_list_reservations", "phone_cancel_reservation"}
TOKEN = "challenge-token_123"


@contextlib.contextmanager
def serving(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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


def hosted_app(api, **kw):
    args = dict(api=api.url, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None, allowed_origins=None,
                public_url=None, oauth_issuer=None, stateful=False, drive_path="/chatgpt",
                well_known={"openai-apps-challenge": TOKEN})
    args.update(kw)
    return build_http_app(**args)


async def listed(url):
    """The tool names and the instructions an MCP client is given at `url`."""
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_test"}, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, *_rest):
            async with ClientSession(read, write) as s:
                hello = await s.initialize()
                tools = await s.list_tools()
    return {t.name for t in tools.tools}, hello.instructions


def post(url, body=b"{}"):
    return urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "Accept": "application/json, text/event-stream"})


# --- the tools themselves ---------------------------------------------------------

async def test_every_tool_states_all_three_hints(api):
    async with connected(stdio_server(api)) as s:
        tools = (await s.list_tools()).tools
    assert tools
    for t in tools:
        a = t.annotations
        assert a is not None, t.name
        for hint in ("read_only_hint", "destructive_hint", "open_world_hint"):
            assert isinstance(getattr(a, hint), bool), f"{t.name} leaves {hint} unsaid"
        if a.read_only_hint:
            assert a.destructive_hint is False, t.name


async def test_every_tool_carries_its_title_in_the_annotations_too(api):
    cloud = Cloud(StaticToken("pck_test"), api.url)
    for server in (stdio_server(api), build_server(cloud_for=lambda ctx: cloud, allow_files=False, reservations=False)):
        async with connected(server) as s:
            tools = (await s.list_tools()).tools
        assert tools
        for t in tools:
            assert t.title, t.name
            assert t.annotations.title == t.title, t.name


async def test_what_touches_the_screen_is_open_world(api):
    async with connected(stdio_server(api)) as s:
        by_name = {t.name: t.annotations for t in (await s.list_tools()).tools}
    for name in ("phone_take_screenshot", "phone_list_elements_on_screen", "phone_click_on_screen_at_coordinates",
                 "phone_type_keys", "phone_open_url", "phone_launch_app"):
        assert by_name[name].open_world_hint is True, name
    for name in ("phone_list_available_devices", "phone_start_device", "phone_stop_device", "phone_get_screen_size",
                 "phone_wake", "phone_reserve"):
        assert by_name[name].open_world_hint is False, name


async def test_without_reservations_nothing_is_sold(api):
    cloud = Cloud(StaticToken("pck_test"), api.url)
    full = build_server(cloud_for=lambda ctx: cloud, allow_files=False)
    plain = build_server(cloud_for=lambda ctx: cloud, allow_files=False, reservations=False)
    async with connected(full) as s:
        every = {t.name for t in (await s.list_tools()).tools}
    async with connected(plain) as s:
        hello_names = {t.name for t in (await s.list_tools()).tools}
    assert RESERVING <= every
    assert hello_names == every - RESERVING


def test_the_plain_instructions_name_no_price():
    for word in ("$", "cents", "Stripe", "deposit", "phone_reserve", "subscription"):
        assert word not in DRIVE_ONLY_INSTRUCTIONS, word
    assert "phone_list_available_devices" in DRIVE_ONLY_INSTRUCTIONS
    assert "phone_reserve" in INSTRUCTIONS and "$80/mo" in INSTRUCTIONS


# --- two endpoints on one server --------------------------------------------------------

async def test_the_second_endpoint_drives_phones_and_sells_nothing(api):
    with serving(hosted_app(api)) as base:
        main_tools, main_words = await listed(f"{base}/mcp")
        plain_tools, plain_words = await listed(f"{base}/chatgpt")
    assert RESERVING <= main_tools and "phone_reserve" in main_words
    assert plain_tools == main_tools - RESERVING
    assert plain_words == DRIVE_ONLY_INSTRUCTIONS and main_words == INSTRUCTIONS


def test_both_endpoints_want_a_bearer_and_the_rest_is_open(api):
    with serving(hosted_app(api)) as base:
        for path in ("/mcp", "/chatgpt"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(post(base + path), timeout=5)
            assert e.value.code == 401, path
        with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
        with urllib.request.urlopen(f"{base}/", timeout=5) as r:
            assert json.loads(r.read())["mcp"] == "/mcp"


def test_the_challenge_is_the_token_and_nothing_else(api):
    with serving(hosted_app(api)) as base:
        with urllib.request.urlopen(f"{base}/.well-known/openai-apps-challenge", timeout=5) as r:
            assert r.read().decode() == TOKEN
            assert r.headers["Content-Type"].startswith("text/plain")


def test_no_token_means_no_challenge_page(api):
    with serving(hosted_app(api, well_known=None)) as base:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/.well-known/openai-apps-challenge", timeout=5)
        assert e.value.code == 404


def test_without_a_second_path_the_app_is_as_before(api):
    with serving(hosted_app(api, drive_path=None)) as base:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(post(f"{base}/chatgpt"), timeout=5)
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(post(f"{base}/mcp"), timeout=5)
        assert e.value.code == 401


def test_paths_inside_one_another_are_refused(api):
    import click
    with pytest.raises(click.UsageError):
        hosted_app(api, drive_path="/mcp/chatgpt")


# --- OAuth: each endpoint is its own protected resource -----------------------------------

def test_each_endpoint_publishes_its_own_resource(api):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    public = f"https://mcp.example.test:{port}/mcp"
    app = hosted_app(api, public_url=public, oauth_issuer="https://issuer.example", oauth_scope="phone:drive")
    with serving(app) as base:
        seen = {}
        for path in ("/mcp", "/chatgpt"):
            with urllib.request.urlopen(f"{base}/.well-known/oauth-protected-resource{path}", timeout=5) as r:
                seen[path] = json.loads(r.read())
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(post(base + path), timeout=5)
            assert e.value.code == 401
            challenge = e.value.headers["WWW-Authenticate"]
            assert f"oauth-protected-resource{path}" in challenge, challenge
    assert seen["/mcp"]["resource"].rstrip("/") == public
    assert seen["/chatgpt"]["resource"].rstrip("/") == f"https://mcp.example.test:{port}/chatgpt"
    for meta in seen.values():
        assert meta["authorization_servers"] == ["https://issuer.example"]
        assert meta["scopes_supported"] == ["phone:drive"]


async def test_oauth_mode_serves_both_endpoints_to_a_good_bearer(api):
    app = hosted_app(api, public_url="https://mcp.example.test/mcp", oauth_issuer="https://issuer.example")
    with serving(app) as base:
        main_tools, _ = await listed(f"{base}/mcp")
        plain_tools, _ = await listed(f"{base}/chatgpt")
    assert RESERVING <= main_tools and not (RESERVING & plain_tools)


# --- the Vercel function --------------------------------------------------------------------

@pytest.fixture
def vercel(api, monkeypatch):
    import phone_harness_mcp.vercel as module
    monkeypatch.setenv("PHONE_HARNESS_CLOUD_API", api.url)
    monkeypatch.delenv("PHONE_HARNESS_MCP_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("PHONE_HARNESS_MCP_DRIVE_PATH", raising=False)

    def load(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(module).app
    return load


async def test_vercel_serves_the_chatgpt_endpoint_by_default(vercel):
    with serving(vercel()) as base:
        main_tools, _ = await listed(f"{base}/mcp")
        plain_tools, words = await listed(f"{base}/chatgpt")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/.well-known/openai-apps-challenge", timeout=5)
        assert e.value.code == 404
    assert RESERVING <= main_tools and not (RESERVING & plain_tools)
    assert "$" not in words


def test_vercel_serves_the_challenge_from_the_environment(vercel):
    with serving(vercel(PHONE_HARNESS_MCP_OPENAI_CHALLENGE=f" {TOKEN}\n")) as base:
        with urllib.request.urlopen(f"{base}/.well-known/openai-apps-challenge", timeout=5) as r:
            assert r.read().decode() == TOKEN
