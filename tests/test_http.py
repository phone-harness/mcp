"""Hosted mode: the server holds no credential; each request brings its own bearer."""
import http.client
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

from phone_harness_mcp import __version__
from phone_harness_mcp.cli import build_http_app
from support import text_of

pytestmark = pytest.mark.anyio


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hosted(api):
    port = free_port()
    app = build_http_app(api=api.url, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None, allowed_origins=None,
                         public_url=None, oauth_issuer=None, stateful=False)
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


def test_health_is_public(hosted):
    with urllib.request.urlopen(f"{hosted}/health", timeout=5) as r:
        body = json.loads(r.read())
    assert body["ok"] is True and body["version"] == __version__


def test_no_bearer_is_a_401_challenge(hosted):
    req = urllib.request.Request(f"{hosted}/mcp", data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    assert e.value.headers["WWW-Authenticate"].startswith('Bearer realm="phone-harness"')
    assert "Authorization: Bearer" in json.loads(e.value.read())["error_description"]


def test_two_authorization_headers_are_refused(hosted):
    host, port = hosted.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Accept", "application/json, text/event-stream")
    conn.putheader("Authorization", "Bearer pck_test")
    conn.putheader("Authorization", "Bearer pck_other")
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    response = conn.getresponse()
    assert response.status == 400
    assert json.loads(response.read())["error_description"] == "one Authorization header"


async def test_bearer_is_forwarded_to_the_api(hosted, api):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_test"}, timeout=30) as http:
        async with streamable_http_client(f"{hosted}/mcp", http_client=http) as (read, write, *_rest):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = await s.list_tools()
                assert "phone_save_screenshot" not in {t.name for t in tools.tools}
                r = await s.call_tool("phone_list_available_devices", {})
                assert not r.is_error and "No phone is running" in text_of(r)
                r = await s.call_tool("phone_start_device", {})
                assert not r.is_error and "as device s_0001" in text_of(r)
                r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": "s_0001", "x": 5, "y": 6})
                assert not r.is_error
    bearers = {b for _m, _p, b in api.requests}
    assert bearers == {"pck_test"}
    assert api.ops[-1][1:] == ("input.tap", {"x": 5, "y": 6})


async def test_a_bad_bearer_is_the_apis_refusal(hosted, api):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_nope"}, timeout=30) as http:
        async with streamable_http_client(f"{hosted}/mcp", http_client=http) as (read, write, *_rest):
            async with ClientSession(read, write) as s:
                await s.initialize()
                r = await s.call_tool("phone_list_available_devices", {})
    assert r.is_error and "refused the credential" in text_of(r)


@pytest.fixture
def vercel_app(api, monkeypatch):
    """The Vercel entrypoint, configured through the environment like the function is."""
    import importlib
    import phone_harness_mcp.vercel as vercel
    monkeypatch.setenv("PHONE_HARNESS_CLOUD_API", api.url)
    monkeypatch.delenv("PHONE_HARNESS_MCP_OAUTH_ISSUER", raising=False)
    module = importlib.reload(vercel)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning"))
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


def test_vercel_entrypoint_serves_index_health_and_a_401(vercel_app):
    with urllib.request.urlopen(f"{vercel_app}/", timeout=5) as r:
        body = json.loads(r.read())
    assert body["mcp"] == "/mcp" and body["version"] == __version__
    with urllib.request.urlopen(f"{vercel_app}/health", timeout=5) as r:
        assert json.loads(r.read())["ok"] is True
    req = urllib.request.Request(f"{vercel_app}/mcp", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401


async def test_vercel_entrypoint_forwards_the_bearer(vercel_app, api):
    # Any Host header is accepted: Vercel's edge owns routing, so the check is off there.
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_test", "Host": "mcp.phone-harness.com"},
                                  timeout=30) as http:
        async with streamable_http_client(f"{vercel_app}/mcp", http_client=http) as (read, write, *_rest):
            async with ClientSession(read, write) as s:
                await s.initialize()
                r = await s.call_tool("phone_list_available_devices", {})
    assert not r.is_error and "No phone is running" in text_of(r)
    assert {b for _m, _p, b in api.requests} == {"pck_test"}


@pytest.fixture
def oauth_hosted(api):
    """HTTP mode with OAuth switched on: the SDK publishes protected-resource metadata
    and verifies every bearer through the API."""
    port = free_port()
    public = f"http://127.0.0.1:{port}/mcp"
    app = build_http_app(api=api.url, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None, allowed_origins=None,
                         public_url=public, oauth_issuer="https://issuer.example", stateful=False,
                         oauth_scope="phone:drive")
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


def test_oauth_mode_publishes_resource_metadata(oauth_hosted):
    found = None
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        try:
            with urllib.request.urlopen(f"{oauth_hosted}{path}", timeout=5) as r:
                found = json.loads(r.read())
                break
        except urllib.error.HTTPError:
            continue
    assert found, "no protected resource metadata served"
    assert found["authorization_servers"] == ["https://issuer.example"]
    assert found["scopes_supported"] == ["phone:drive"]
    assert found["resource"].rstrip("/") == f"{oauth_hosted}/mcp"


def test_oauth_mode_challenge_names_the_metadata(oauth_hosted):
    req = urllib.request.Request(f"{oauth_hosted}/mcp", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    challenge = e.value.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer") and "resource_metadata=" in challenge


async def test_oauth_mode_still_takes_an_api_key(oauth_hosted, api):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_test"}, timeout=30) as http:
        async with streamable_http_client(f"{oauth_hosted}/mcp", http_client=http) as (read, write, *_rest):
            async with ClientSession(read, write) as s:
                await s.initialize()
                r = await s.call_tool("phone_list_available_devices", {})
    assert not r.is_error and "No phone is running" in text_of(r)
    assert ("GET", "/me", "pck_test") in api.requests          # the verifier asked the API


def test_oauth_mode_refuses_a_bearer_the_api_refuses(oauth_hosted):
    req = urllib.request.Request(f"{oauth_hosted}/mcp", data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Accept": "application/json, text/event-stream",
                                                         "Authorization": "Bearer pck_nope"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
