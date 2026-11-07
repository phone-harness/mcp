"""phone_get_screenshot_url and the handoff tools, over stdio against the fake API."""
import time

import pytest

from support import connected, start, stdio_server, text_of

pytestmark = pytest.mark.anyio


async def test_a_screenshot_url_is_text_and_structured_data(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_get_screenshot_url", {"device": sid, "minutes": 2})
        too_long = await s.call_tool("phone_get_screenshot_url", {"device": sid, "minutes": 16})
        too_short = await s.call_tool("phone_get_screenshot_url", {"device": sid, "minutes": 0})
    assert not r.is_error
    text = text_of(r)
    url = r.structured_content["url"]
    # A client that ignores structuredContent still has the link, in the text.
    assert text.splitlines()[0] == f"Screenshot link (this one image, for 2 min): {url}"
    assert "multiply its x by 0.5 and y by 0.5" in text
    assert url.startswith(api.url + "/screenshots/") and "token=phs_" in url
    assert r.structured_content["expires_at"] - time.time() <= 121
    assert ("POST", f"/sessions/{sid}/screenshots", "pck_test") in api.requests
    assert too_long.is_error and too_short.is_error and "1 to 15 minutes" in text_of(too_long)


async def test_a_handoff_is_a_link_and_nothing_more(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_start_handoff", {"device": sid, "minutes": 30})
        bad = await s.call_tool("phone_start_handoff", {"device": sid, "minutes": 31})
    assert not r.is_error
    data = r.structured_content
    assert set(data) == {"handoff_id", "url", "expires_at", "state"}
    assert data["state"] == "waiting" and data["expires_at"] - time.time() <= 1801
    assert data["url"] in text_of(r) and "Don't drive the phone" in text_of(r)
    assert "pck_" not in data["url"]
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


async def test_an_older_service_says_it_has_no_links(api):
    api.links = False
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        shot = await s.call_tool("phone_get_screenshot_url", {"device": sid})
        handoff = await s.call_tool("phone_start_handoff", {"device": sid})
        status = await s.call_tool("phone_get_handoff", {"device": sid})
    assert shot.is_error and "does not offer screenshot links yet" in text_of(shot)
    assert handoff.is_error and "does not offer handoffs yet" in text_of(handoff)
    assert status.is_error and "does not offer handoffs yet" in text_of(status)


async def test_links_for_a_device_that_is_not_running(api):
    async with connected(stdio_server(api)) as s:
        shot = await s.call_tool("phone_get_screenshot_url", {"device": "s_nope"})
        handoff = await s.call_tool("phone_start_handoff", {"device": "s_nope"})
    assert shot.is_error and "device s_nope is not running" in text_of(shot)
    assert handoff.is_error and "device s_nope is not running" in text_of(handoff)
