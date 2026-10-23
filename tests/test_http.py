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
