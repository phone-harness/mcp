"""PHO-355 through the real MCP protocol and a local fake cloud."""
import json

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fake_api import _Reply
from support import connected, stdio_server, text_of
from test_http import hosted

pytestmark = pytest.mark.anyio


def remake(ok):
    return {"ok": ok, "reason": "manual", "detail": None if ok else "remake failed",
            "attempts": 2, "took_s": 0.4, "at": 1234.5,
            "surface_rematch": False, "rsd": {"ok": True}}


async def test_schema_requires_only_the_device_and_explains_scope(api):
    async with connected(stdio_server(api, allow_files=False)) as session:
        tools = (await session.list_tools()).tools
    tool = next(t for t in tools if t.name == "phone_reset_input")
    assert set(tool.input_schema["properties"]) == {"device"}
    assert tool.input_schema["required"] == ["device"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    for phrase in ("taps or swipes are sent but do not land", "touch/HID", "dismiss an alert",
                   "erase the device", "restart the session", "ok=false"):
        assert phrase in tool.description


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("locked", [True, False])
async def test_structured_result_and_dispatch_without_wake(api, ok, locked):
    sid = api.running()
    if locked:
        api.locked.add(sid)
    api.reset_answer = remake(ok)
    async with connected(stdio_server(api)) as session:
        result = await session.call_tool("phone_reset_input", {"device": sid})
    assert result.structured_content == api.reset_answer
    assert json.loads(text_of(result)) == api.reset_answer
    assert result.is_error is (not ok)
    assert api.ops == [(sid, "input.reset", {})]
    assert [(m, p) for m, p, _ in api.requests] == [("POST", f"/sessions/{sid}/op")]
    assert (sid in api.locked) is locked


@pytest.mark.parametrize("answer", [None, True, {}, {"ok": 1}, {"ok": "false"}])
async def test_malformed_result_is_not_recovery(api, answer):
    sid = api.running()
    api.reset_answer = answer
    async with connected(stdio_server(api)) as session:
        result = await session.call_tool("phone_reset_input", {"device": sid})
    assert result.is_error
    assert "recovery is unconfirmed" in text_of(result)


@pytest.mark.parametrize("missing", ["host", "cloud", "android"])
async def test_unsupported_has_no_fallback(api, missing):
    sid = api.running(platform="android" if missing == "android" else "ios")
    if missing == "host":
        api.old_host = ("input.reset",)
    elif missing == "cloud":
        api.not_served = ("input.reset",)
    async with connected(stdio_server(api)) as session:
        result = await session.call_tool("phone_reset_input", {"device": sid})
    assert result.is_error
    assert all(op == "input.reset" and kw == {} for _sid, op, kw in api.ops)


@pytest.mark.parametrize("status,code", [(502, None), (409, "locked")])
async def test_reset_failure_does_not_add_wake_advice_or_probe_platform(api, monkeypatch, status, code):
    sid = api.running()

    def refused(*_args):
        raise _Reply(status, {"error": "reset unavailable", "code": code})

    monkeypatch.setattr(api, "answer", refused)
    async with connected(stdio_server(api)) as session:
        result = await session.call_tool("phone_reset_input", {"device": sid})
    assert result.is_error
    assert "phone_wake" not in text_of(result)
    assert [(m, p) for m, p, _ in api.requests] == [("POST", f"/sessions/{sid}/op")]


@pytest.mark.parametrize("token,uid", [("fixture-view-only", "u_1"), ("pck_test", "u_2")])
async def test_requires_the_session_owners_credential(api, token, uid):
    sid = api.running(uid=uid)
    async with connected(stdio_server(api, token=token)) as session:
        result = await session.call_tool("phone_reset_input", {"device": sid})
    assert result.is_error
    assert api.ops == []


async def test_missing_device_does_not_dispatch(api):
    async with connected(stdio_server(api)) as session:
        result = await session.call_tool("phone_reset_input", {})
    assert result.is_error
    assert api.requests == []


@pytest.mark.parametrize("ok", [True, False])
async def test_hosted_http_keeps_the_bearer_and_structured_result(api, hosted, ok):
    sid = api.running()
    api.reset_answer = remake(ok)
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_test"}, timeout=30) as http:
        async with streamable_http_client(f"{hosted}/mcp", http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("phone_reset_input", {"device": sid})
    assert result.structured_content == api.reset_answer
    assert result.is_error is (not ok)
    assert api.ops == [(sid, "input.reset", {})]
    assert {bearer for _m, _p, bearer in api.requests} == {"pck_test"}
