import base64
import re

import pytest

from fake_api import BOUNDS, CAPTURE_SIZE
from support import connected, start, stdio_server, text_of

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "phone_list_available_devices", "phone_start_device", "phone_stop_device",
    "phone_get_screen_size", "phone_take_screenshot", "phone_save_screenshot",
    "phone_list_elements_on_screen", "phone_click_on_screen_at_coordinates",
    "phone_long_press_on_screen_at_coordinates", "phone_swipe_on_screen", "phone_type_keys",
    "phone_press_button", "phone_list_apps", "phone_launch_app", "phone_get_foreground_app",
}


async def test_tool_names_follow_mobile_mcp(api):
    async with connected(stdio_server(api)) as s:
        tools = await s.list_tools()
    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS
    assert all(n.startswith("phone_") for n in names)
    by_name = {t.name: t for t in tools.tools}
    assert by_name["phone_take_screenshot"].annotations.read_only_hint is True
    assert by_name["phone_stop_device"].annotations.destructive_hint is True
    assert "device" in by_name["phone_click_on_screen_at_coordinates"].input_schema["required"]


async def test_hosted_server_has_no_file_tool(api):
    async with connected(stdio_server(api, allow_files=False)) as s:
        tools = await s.list_tools()
    assert "phone_save_screenshot" not in {t.name for t in tools.tools}


async def test_list_then_start_then_list(api):
    async with connected(stdio_server(api)) as s:
        before = text_of(await s.call_tool("phone_list_available_devices", {}))
        assert "No phone is running" in before
        assert "My iPhone" in before and "device_id=iph-a1b2c3" in before
        assert "Balance: $12.50 at $0.05 per minute" in before

        sid = await start(s)
        assert sid == "s_0001"
        created = api.sessions[sid]["created"]
        assert created == {"timeout_seconds": 1800}          # nothing named: the account's default phone

        after = text_of(await s.call_tool("phone_list_available_devices", {}))
        assert f"{sid}: Android emulator (android/emulator), ready, screen 390x844" in after


async def test_start_names_the_iphone(api):
    api.ready_after = 3
    async with connected(stdio_server(api)) as s:
        text = text_of(await s.call_tool("phone_start_device",
                                         {"platform": "ios", "kind": "device", "device_id": "iph-a1b2c3", "minutes": 5}))
    assert "Started iPhone as device s_0001" in text and "Screen 390x844" in text
    assert api.sessions["s_0001"]["created"] == {"timeout_seconds": 300, "platform": "ios", "kind": "device",
                                                 "device_id": "iph-a1b2c3"}


async def test_device_id_alone_is_looked_up_in_the_catalog(api):
    async with connected(stdio_server(api)) as s:
        await start(s, device_id="iph-a1b2c3")
        await start(s, device_id="prof-1", minutes=10)
        r = await s.call_tool("phone_start_device", {"device_id": "nope"})
    assert api.sessions["s_0001"]["created"] == {"timeout_seconds": 1800, "platform": "ios", "kind": "device",
                                                 "device_id": "iph-a1b2c3"}
    assert api.sessions["s_0002"]["created"] == {"timeout_seconds": 600, "platform": "android", "kind": "emulator",
                                                 "profile_id": "prof-1"}
    assert r.is_error and "ids are: prof-1, iph-a1b2c3" in text_of(r)


async def test_start_refuses_half_a_name(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_start_device", {"platform": "ios"})
    assert r.is_error and "go together" in text_of(r)
    assert not api.sessions


async def test_screenshot_is_jpeg_with_a_mapping(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        r = await s.call_tool("phone_take_screenshot", {"device": sid})
        assert not r.is_error
        note, image = r.content
        assert image.type == "image" and image.mime_type == "image/jpeg"
        data = base64.b64decode(image.data)
        assert data[:3] == b"\xff\xd8\xff"
        m = re.match(r"Screenshot is (\d+)x(\d+)\. Screen coordinates are 390x844\. To tap something you see in "
                     r"this screenshot, multiply its x by ([\d.]+) and y by ([\d.]+)\.", note.text)
        assert m, note.text
        w, h = int(m.group(1)), int(m.group(2))
        assert max(w, h) == 1024 and w == round(CAPTURE_SIZE[0] * 1024 / CAPTURE_SIZE[1])
        assert abs(float(m.group(3)) - 390 / w) < 0.002 and abs(float(m.group(4)) - 844 / h) < 0.002

        # No shrink: the capture is twice the coordinate space, so the factor is one half.
        r = await s.call_tool("phone_take_screenshot", {"device": sid, "max_size": 4000})
        assert "Screenshot is 780x1688" in r.content[0].text and "multiply its x by 0.5 and y by 0.5" in r.content[0].text


async def test_save_screenshot_writes_png(api, tmp_path):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        target = tmp_path / "shots" / "home.png"
        text = text_of(await s.call_tool("phone_save_screenshot", {"device": sid, "save_to": str(target)}))
    assert f"Saved 780x1688 PNG to {target}" in text
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


async def test_screen_size_and_elements(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        assert text_of(await s.call_tool("phone_get_screen_size", {"device": sid})).startswith("Screen is 390x844")
        text = text_of(await s.call_tool("phone_list_elements_on_screen", {"device": sid}))
        assert text.splitlines()[0] == "Screen is 390x844. 2 text elements, top to bottom; (x, y) is the center to tap:"
        assert '- "Hello Cloud" at (195, 200), 200x30' in text
        assert '- "Continue" at (195, 500), 160x44' in text
        assert "misread" not in text
        as_json = text_of(await s.call_tool("phone_list_elements_on_screen", {"device": sid, "format": "json"}))
        assert '"text": "Continue"' in as_json


async def test_touch_tools_send_the_right_ops(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        api.ops.clear()
        assert text_of(await s.call_tool("phone_click_on_screen_at_coordinates",
                                         {"device": sid, "x": 100.4, "y": 200.6})) == "Tapped (100, 201)."
        await s.call_tool("phone_long_press_on_screen_at_coordinates", {"device": sid, "x": 10, "y": 20, "duration": 1500})
        assert text_of(await s.call_tool("phone_swipe_on_screen", {"device": sid, "direction": "up"})) \
            == "Swiped up from (195, 591) to (195, 253)."
        await s.call_tool("phone_swipe_on_screen", {"device": sid, "direction": "left", "x": 300, "y": 100, "distance": 100})
        assert text_of(await s.call_tool("phone_type_keys", {"device": sid, "text": "hello", "submit": True})) \
            == "Typed 5 characters and pressed Enter."
        assert text_of(await s.call_tool("phone_press_button", {"device": sid, "button": "HOME"})) == "Pressed HOME."
        await s.call_tool("phone_press_button", {"device": sid, "button": "APP_SWITCHER"})
    assert [op for _sid, op, _kw in api.ops].count("screen.bounds") == 1   # only the swipe that needed the size
    ops = [(op, kw) for _sid, op, kw in api.ops if op != "screen.bounds"]
    assert ops == [
        ("input.tap", {"x": 100, "y": 201}),
        ("input.press", {"x": 10, "y": 20, "duration": 1.5}),
        ("input.drag", {"x1": 195, "y1": 591, "x2": 195, "y2": 253, "duration": 0.3, "steps": 10}),
        ("input.drag", {"x1": 350, "y1": 100, "x2": 250, "y2": 100, "duration": 0.3, "steps": 10}),
        ("input.text", {"s": "hello"}),
        ("input.keys", {"combo": "enter"}),
        ("nav.home", {}),
        ("nav.recents", {}),
    ]


async def test_swipe_stays_on_screen(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        text = text_of(await s.call_tool("phone_swipe_on_screen", {"device": sid, "direction": "down", "y": 800, "distance": 400}))
    assert text == "Swiped down from (195, 600) to (195, 843)."


async def test_back_on_an_iphone_explains_itself(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s, platform="ios", kind="device")
        r = await s.call_tool("phone_press_button", {"device": sid, "button": "BACK"})
        assert r.is_error and "no Back button" in text_of(r)
        r = await s.call_tool("phone_list_apps", {"device": sid})
        assert r.is_error and "phone_launch_app" in text_of(r)


async def test_apps(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        assert text_of(await s.call_tool("phone_launch_app", {"device": sid, "name": "Notes"})) == "Launched com.example.notes."
        assert text_of(await s.call_tool("phone_get_foreground_app", {"device": sid})) == "Foreground app: com.example.mock"
        assert text_of(await s.call_tool("phone_list_apps", {"device": sid})) == "com.example.mock\ncom.android.chrome"


async def test_busy_phone_is_retried_quietly(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        api.busy_times = 2
        api.outage_times = 1
        n = len(api.requests)
        r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 1, "y": 2})
    assert not r.is_error
    posts = [p for p in api.requests[n:] if p[0] == "POST"]
    assert len(posts) == 4                       # busy, busy, 503, then the one that landed
    assert api.ops[-1][1:] == ("input.tap", {"x": 1, "y": 2})


async def test_unknown_device(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": "s_nope", "x": 1, "y": 2})
    assert r.is_error and "device s_nope is not running" in text_of(r)


async def test_bad_credential(api):
    async with connected(stdio_server(api, token="pck_wrong")) as s:
        r = await s.call_tool("phone_list_available_devices", {})
    assert r.is_error and "refused the credential" in text_of(r)


async def test_stop(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        assert text_of(await s.call_tool("phone_stop_device", {"device": sid})) == f"Stopped device {sid}."
        r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 1, "y": 2})
        assert r.is_error and "not running" in text_of(r)
