"""Network MCP schemas, real handlers/HTTP, bounded output and caller isolation."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from phone_harness_mcp.cloud import Cloud, StaticToken
from phone_harness_mcp import network
from phone_harness_mcp.phone import PhoneError
from support import connected, stdio_server, text_of

CID = "a" * 32
PHONE = "iph-fixture"
PEM = "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----"
KEY = "-----BEGIN PRIVATE KEY-----\nSYNTHETIC-SECRET-MARKER\n-----END PRIVATE KEY-----"


@pytest.fixture
def network_api():
    state = SimpleNamespace(requests=[], status=200, oversized=False)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): self.dispatch()
        def do_PUT(self): self.dispatch()
        def do_DELETE(self): self.dispatch()
        def dispatch(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"null")
            bearer = self.headers.get("Authorization", "")
            state.requests.append((self.command, self.path, bearer, body))
            code = 404 if bearer == "Bearer pck_other" else state.status
            parsed = urlsplit(self.path)
            raw = b"{\"ok\":true}"
            extra = {}
            if parsed.path.endswith("/har") and code == 200:
                whole = b'{"log":{"entries":[{"synthetic":true}]}}'
                q = parse_qs(parsed.query)
                offset, limit = int(q["offset"][0]), int(q["limit"][0])
                raw = whole[offset:offset + limit]
                code = 206
                extra["Content-Range"] = f"bytes {offset}-{offset + len(raw)-1}/{len(whole)}"
            elif code != 200:
                raw = json.dumps({"error": KEY}).encode()  # must never be echoed, even by a broken upstream
            if state.oversized:
                raw = b"x" * (1024 * 1024 + 1)
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Type", "application/json")
            if code == 302:
                self.send_header("Location", state.url + "/must-not-follow")
            for k, v in extra.items(): self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.anyio
async def test_four_generic_tools_have_bounded_schemas_and_composition(network_api):
    async with connected(stdio_server(network_api, allow_files=False)) as session:
        all_tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in ("phone_trust_cert", "phone_route", "phone_proxy_session", "phone_read_traffic"):
            assert name in all_tools
            schema = all_tools[name].input_schema
            assert "device_id" in schema["required"]
            assert "action" in schema["required"]
            assert "Stan" not in all_tools[name].description
        trust = all_tools["phone_trust_cert"].input_schema
        assert "ca_key_pem" not in json.dumps(trust)
        assert "route" in json.dumps(trust)
        route = all_tools["phone_route"].input_schema
        assert "trust_cert" in json.dumps(route)
        assert "ca_key_pem" not in json.dumps(route)
        proxy = all_tools["phone_proxy_session"].input_schema
        assert "ca_key_pem" in json.dumps(proxy)
        traffic = json.dumps(all_tools["phone_read_traffic"].input_schema)
        assert all(name in traffic for name in ("domain", "path", "method", "status", "body_limit", "65536"))


@pytest.mark.anyio
async def test_cert_only_byo_and_atomic_hosted_recipe_handlers(network_api):
    async with connected(stdio_server(network_api, allow_files=False)) as s:
        for tool, args in (
            ("phone_trust_cert", {"action": "install", "config": {"ca_pem": PEM, "duration_seconds": 300}}),
            ("phone_trust_cert", {"action": "remove"}),
            ("phone_route", {"action": "install", "config": {"domains": "all", "target": {"proxy": "proxy.example:8080"}, "trust_cert": {"ca_pem": PEM}, "duration_seconds": 300}}),
            ("phone_route", {"action": "remove", "include_trust_cert": True}),
            ("phone_proxy_session", {"action": "start", "config": {"domains": ["api.listings.example"], "duration_seconds": 900}}),
            ("phone_route", {"action": "install", "config": {"domains": ["api.listings.example"], "target": {"proxy_session_id": CID}, "trust_cert": {"proxy_session_id": CID}, "duration_seconds": 900}}),
            ("phone_trust_cert", {"action": "status"}),
            ("phone_proxy_session", {"action": "stop", "session_id": CID}),
        ):
            result = await s.call_tool(tool, {"device_id": PHONE, **args})
            assert not result.is_error, text_of(result)
    requests = network_api.requests
    assert requests[0][3] == {"ca_pem": PEM, "duration_seconds": 300}
    assert requests[2][3]["trust_cert"] == {"ca_pem": PEM}
    assert requests[3][3] == {"include_trust_cert": True}
    assert requests[5][3]["target"] == {"proxy_session_id": CID}
    assert requests[-1][3] == {"id": CID}
    assert all(b == "Bearer pck_test" for _, _, b, _ in requests)


@pytest.mark.anyio
async def test_uploaded_key_only_proxy_and_no_error_echo(network_api, caplog):
    async with connected(stdio_server(network_api)) as s:
        config = {"domains": "all", "duration_seconds": 300, "ca_pem": PEM, "ca_key_pem": KEY}
        result = await s.call_tool("phone_proxy_session", {"device_id": PHONE, "action": "start", "config": config})
        assert not result.is_error, text_of(result)
        assert network_api.requests[-1][3]["ca_key_pem"] == KEY
        network_api.status = 400
        result = await s.call_tool("phone_proxy_session", {"device_id": PHONE, "action": "start", "config": config})
        assert result.is_error
        assert "SYNTHETIC-SECRET-MARKER" not in text_of(result)
        for tool, body in (("phone_trust_cert", {"ca_pem": PEM, "duration_seconds": 300, "ca_key_pem": KEY}),
                           ("phone_route", {"domains": "all", "target": {"proxy": "public.example:80"}, "duration_seconds": 300, "ca_key_pem": KEY}),
                           ("phone_proxy_session", {**config, "duration_seconds": 1})):
            before = len(network_api.requests)
            result = await s.call_tool(tool, {"device_id": PHONE, "action": "start" if tool == "phone_proxy_session" else "install", "config": body})
            assert result.is_error
            assert "SYNTHETIC-SECRET-MARKER" not in text_of(result)
            assert len(network_api.requests) == before
    assert "SYNTHETIC-SECRET-MARKER" not in caplog.text


@pytest.mark.anyio
async def test_filters_flow_cap_har_reassembly_and_purge(network_api):
    async with connected(stdio_server(network_api)) as s:
        common = {"device_id": PHONE, "session_id": CID}
        result = await s.call_tool("phone_read_traffic", {**common, "action": "list", "filters": {"domain": "api.listings.example", "path": "/items", "method": "POST", "status": 201, "limit": 5, "cursor": 2}})
        assert not result.is_error, text_of(result)
        assert parse_qs(urlsplit(network_api.requests[-1][1]).query) == {"domain": ["api.listings.example"], "path": ["/items"], "method": ["POST"], "status": ["201"], "limit": ["5"], "cursor": ["2"]}
        result = await s.call_tool("phone_read_traffic", {**common, "action": "get", "flow_id": "2", "body_limit": 7})
        assert not result.is_error
        assert network_api.requests[-1][1].endswith("/flows/2?body_limit=7")
        parts, offset = [], 0
        while offset is not None:
            result = await s.call_tool("phone_read_traffic", {**common, "action": "har", "offset": offset, "chunk_bytes": 11})
            assert not result.is_error, text_of(result)
            chunk = json.loads(text_of(result))
            assert "pck_" not in json.dumps(chunk)
            parts.append(base64.b64decode(chunk["data"]))
            offset = chunk["next_offset"]
        assert json.loads(b"".join(parts))["log"]["entries"] == [{"synthetic": True}]
        result = await s.call_tool("phone_read_traffic", {**common, "action": "purge"})
        assert not result.is_error
        assert network_api.requests[-1][0] == "DELETE"


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [("body_limit", 65537), ("chunk_bytes", 65537), ("offset", -1), ("session_id", "../other"), ("device_id", "x/../other")])
async def test_schema_caps_and_path_escape_rejected_before_http(network_api, field, value):
    async with connected(stdio_server(network_api)) as s:
        result = await s.call_tool("phone_read_traffic", {"device_id": PHONE, "session_id": CID, "action": "list", field: value})
        assert result.is_error
    assert not network_api.requests


@pytest.mark.anyio
async def test_account_key_and_owner_refusals(network_api):
    for token, expected in (("oauth_fixture", "account API key"), ("pck_other", "HTTP 404")):
        async with connected(stdio_server(network_api, token=token)) as s:
            result = await s.call_tool("phone_read_traffic", {"device_id": PHONE, "session_id": CID, "action": "list"})
            assert result.is_error and expected in text_of(result)
    assert [r[2] for r in network_api.requests] == ["Bearer pck_other"]


def test_transport_ignores_proxies_refuses_redirect_and_bounds_output(network_api, monkeypatch):
    cloud = Cloud(StaticToken("pck_test"), network_api.url)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    assert network.request(cloud, "GET", "/fixture") == {"ok": True}
    network_api.status = 302
    with pytest.raises(PhoneError): network.request(cloud, "PUT", "/fixture", {"ca_key_pem": KEY})
    assert not any("must-not-follow" in r[1] for r in network_api.requests)
    network_api.status, network_api.oversized = 200, True
    with pytest.raises(PhoneError, match="size cap"): network.request(cloud, "GET", "/fixture")
    for url in ("http://public.example", "https://user:pass@public.example", "https://public.example/path"):
        with pytest.raises(PhoneError, match="HTTPS API origin"):
            network.request(Cloud(StaticToken("pck_test"), url), "GET", "/fixture")


@pytest.mark.anyio
async def test_unknown_top_level_keys_and_missing_fields_do_not_echo_keys(network_api, caplog):
    async with connected(stdio_server(network_api)) as s:
        by_name = {t.name: t for t in (await s.list_tools()).tools}
        for name in ("phone_trust_cert", "phone_route", "phone_proxy_session", "phone_read_traffic"):
            assert by_name[name].input_schema["additionalProperties"] is False
            for args in ({"device_id": PHONE, "action": "status", "ca_key_pem": KEY}, {"ca_key_pem": KEY}):
                result = await s.call_tool(name, args)
                assert result.is_error
                assert "SYNTHETIC-SECRET-MARKER" not in text_of(result)
    assert not network_api.requests
    assert "SYNTHETIC-SECRET-MARKER" not in caplog.text
