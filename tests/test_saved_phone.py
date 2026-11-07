"""phone_start_device with nothing named: the saved Android phone, reused.

This is a change for every client of the server: nothing named used to start a
temporary emulator, and now starts the account's saved Android phone (apps and
logins kept), or returns the session already holding it. `temporary: true` is
the old behaviour, asked for by name. The compatibility tests pin both.
"""
import pytest

import phone_harness_mcp.server as server_module
from support import connected, start, stdio_server, text_of

pytestmark = pytest.mark.anyio

SAVED = {"timeout_seconds": 1800, "platform": "android", "kind": "emulator", "profile_id": "prof-1"}


async def test_nothing_named_starts_the_saved_phone_then_reconnects(api):
    async with connected(stdio_server(api)) as s:
        first = text_of(await s.call_tool("phone_start_device", {}))
        again = await s.call_tool("phone_start_device", {})
    assert first.startswith("Started the saved phone as device s_0001.")
    assert api.sessions["s_0001"]["created"] == SAVED
    assert not again.is_error
    assert text_of(again).startswith("The saved phone is already running as device s_0001;")
    assert list(api.sessions) == ["s_0001"]                      # no second phone was started


async def test_a_start_that_loses_the_race_reconnects(api):
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
    posts = [p for m, p, _ in api.requests if m == "POST" and p == "/sessions"]
    assert len(posts) >= 2                                        # at least one 409 before the start


async def test_naming_the_saved_phone_by_id_also_reconnects(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_start_device", {"device_id": "prof-1"})
    assert f"already running as device {sid}" in text_of(r)


# --- compatibility ------------------------------------------------------------------

async def test_temporary_is_the_previous_default(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_start_device", {"temporary": True})
        sid = text_of(r).split("as device ", 1)[1].split(".", 1)[0]
        again = await s.call_tool("phone_start_device", {"temporary": True})
    created = api.sessions[sid]["created"]
    assert "profile_id" not in created and created["timeout_seconds"] == 1800
    assert text_of(r).startswith("Started Android emulator as device")      # not "the saved phone"
    assert not again.is_error and len(api.sessions) == 2                       # temporary phones are not reused


async def test_temporary_refuses_what_it_cannot_be(api):
    async with connected(stdio_server(api)) as s:
        with_id = await s.call_tool("phone_start_device", {"temporary": True, "device_id": "iph-a1b2c3"})
        iphone = await s.call_tool("phone_start_device", {"temporary": True, "platform": "ios", "kind": "device"})
    assert with_id.is_error and iphone.is_error and "throwaway Android" in text_of(with_id)
    assert not api.sessions


async def test_a_service_that_keeps_no_saved_phone_gets_the_old_default(api):
    """An API that reports no `profile` (an older one, or one without saved phones)."""
    api.profile_id = None
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
    assert api.sessions[sid]["created"] == {"timeout_seconds": 1800}


async def test_naming_a_phone_works_as_before(api):
    async with connected(stdio_server(api)) as s:
        iphone = await start(s, platform="ios", kind="device", device_id="iph-a1b2c3", minutes=5)
        android = await start(s, platform="android", kind="emulator", minutes=5)
    assert api.sessions[iphone]["created"] == {"timeout_seconds": 300, "platform": "ios", "kind": "device",
                                               "device_id": "iph-a1b2c3"}
    assert api.sessions[android]["created"] == {"timeout_seconds": 300, "platform": "android", "kind": "emulator"}


async def test_start_keeps_its_arguments(api):
    """Only `temporary` is new, and it is optional."""
    async with connected(stdio_server(api)) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    schema = tools["phone_start_device"].input_schema
    assert set(schema["properties"]) == {"platform", "kind", "device_id", "minutes", "temporary"}
    assert not schema.get("required")
