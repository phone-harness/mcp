"""phone_set_orientation (PHO-337).

A cloud iPhone lies flat in a rack and cannot be stood up, so which way up its
screen is has to be asked for. Turning it changes the screen's size and every
coordinate, so the tool's answer is mostly about that.
"""
import re
from pathlib import Path

import pytest

from fake_api import BOUNDS, NOT_DONE
from phone_harness_mcp import server
from phone_harness_mcp.phone import LOCKED, LOCKED_UPRIGHT, MAYBE_ASLEEP, MAYBE_LOCKED_UPRIGHT
from phone_harness_mcp.server import INSTRUCTIONS, NO_TURN, STALE
from support import connected, stdio_server, text_of

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """A turn that has not been drawn yet is waited for. Not in a test."""
    monkeypatch.setattr(server, "TURN_SETTLE", 0)

ROOT = Path(__file__).resolve().parent.parent
TOOL = "phone_set_orientation"
TALL, WIDE = f"{BOUNDS['w']}x{BOUNDS['h']}", f"{BOUNDS['h']}x{BOUNDS['w']}"


def failed(sentence):
    return f"Error executing tool {TOOL}: {sentence}"


def ops(api):
    return [(op, kw) for _sid, op, kw in api.ops]


# --- what a client is told about the tool -----------------------------------------

async def test_the_tool_is_listed_with_the_three_ways_up(api):
    async with connected(stdio_server(api)) as s:
        tool = {t.name: t for t in (await s.list_tools()).tools}[TOOL]
    assert tool.input_schema["required"] == ["device", "orientation"]
    assert set(tool.input_schema["properties"]) == {"device", "orientation"}
    # The two landscapes are different answers, and there is no plain "landscape".
    assert tool.input_schema["properties"]["orientation"]["enum"] == ["portrait", "landscape_left", "landscape_right"]
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True            # turning it the way it already is changes nothing


async def test_the_hosted_server_has_it_too(api):
    async with connected(stdio_server(api, allow_files=False)) as s:
        assert TOOL in {t.name for t in (await s.list_tools()).tools}


async def test_the_description_says_coordinates_go_stale(api):
    async with connected(stdio_server(api)) as s:
        text = {t.name: t.description for t in (await s.list_tools()).tools}[TOOL]
    assert "portrait, landscape_left or landscape_right" in text
    assert "screen size and every coordinate change with the turn" in text
    assert "The phone has to be awake" in text
    assert "Portrait Orientation Lock has to be off: with it on the phone stays upright" in text
    assert "take a new screenshot or list the elements again before the next tap" in text
    assert "An app that supports only one way up stays that way" in text
    assert "An Android emulator cannot be turned from here" in text


async def test_the_screen_size_tool_says_the_size_changes_with_a_turn(api):
    async with connected(stdio_server(api)) as s:
        text = {t.name: t.description for t in (await s.list_tools()).tools}["phone_get_screen_size"]
    assert "It changes when the phone is turned, so ask again after phone_set_orientation." in text


def test_the_instructions_name_it_once_in_the_iphone_paragraph():
    paragraph = next(p for p in INSTRUCTIONS.split("\n\n") if TOOL in p)
    assert "phone_wake" in paragraph                    # the same paragraph, so the two branches stay one edit
    assert "phone_set_orientation turns an iPhone to portrait or to either landscape" in paragraph
    assert "every coordinate change with it, so read the screen again" in paragraph
    assert INSTRUCTIONS.count(TOOL) == 1


def test_the_docs_list_it():
    readme = (ROOT / "README.md").read_text()
    assert re.search(rf"^\| `{TOOL}` \| .*`portrait`, `landscape_left` or `landscape_right`", readme, re.MULTILINE)
    assert f"`{TOOL}`" in (ROOT / "CHANGELOG.md").read_text().split("## 0.1.0")[0]
    hosting = (ROOT / "docs" / "self-hosting.md").read_text()
    assert f"`{TOOL}`" in hosting and "`device.rotate` with `to`" in hosting
    assert "Portrait Orientation Lock" in hosting


# --- turning ----------------------------------------------------------------------
#
# phone-host answers a turn with {device, orientation, turned}: how the phone is
# held now, how what is on screen is drawn (it has looked), and how many quarter
# turns it took. The fake answers the same.

@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("to,size", [("landscape_left", WIDE), ("landscape_right", WIDE), ("portrait", TALL)])
async def test_each_way_up_is_one_op_and_the_answer_gives_the_new_size(api, to, size, stale):
    """`stale` is phone-host as it is: the size it reports is the one the session
    started with, turned or not. The host says how the screen is drawn, and the
    reported size is turned to match."""
    api.stale_bounds = stale
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": to})
    assert not r.is_error
    assert text_of(r) == f"Turned the phone to {to}. The screen is now {size}. {STALE}"
    assert ops(api) == [("device.rotate", {"to": to}), ("screen.bounds", {})]       # the turn, then how big


async def test_the_two_landscapes_are_sent_as_different_words(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
        await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_right"})
    assert [kw for op, kw in ops(api) if op == "device.rotate"] == [{"to": "landscape_left"}, {"to": "landscape_right"}]


async def test_there_and_back_again(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        wide = text_of(await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"}))
        size = text_of(await s.call_tool("phone_get_screen_size", {"device": sid}))
        tall = text_of(await s.call_tool(TOOL, {"device": sid, "orientation": "portrait"}))
    assert f"The screen is now {WIDE}." in wide and size.startswith(f"Screen is {WIDE}")
    assert f"The screen is now {TALL}." in tall


async def test_the_next_swipe_uses_the_size_after_the_turn(api):
    """What goes stale is every coordinate, the screen's middle included."""
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        before = text_of(await s.call_tool("phone_swipe_on_screen", {"device": sid, "direction": "left"}))
        await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_right"})
        after = text_of(await s.call_tool("phone_swipe_on_screen", {"device": sid, "direction": "left"}))
    assert before == "Swiped left from (273, 422) to (117, 422)."
    assert after == "Swiped left from (591, 195) to (253, 195)."


@pytest.mark.parametrize("stale", [False, True])
async def test_an_app_that_does_not_turn_is_said_plainly(api, stale):
    """The phone turns and the app in front does not (the Home Screen of a small
    iPhone): nothing on screen moved, so nothing is stale. The host says so, and
    no screenshot is taken to find out."""
    api.app_turns, api.stale_bounds = False, stale
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert not r.is_error
    assert text_of(r) == (f"The phone is turned to landscape_left, but what is on screen did not turn with it and "
                          f"is still {TALL}. An app that supports only one way up stays that way: open one that "
                          "turns, or carry on with the screen as it is.")
    assert STALE not in text_of(r)
    assert [op for op, _kw in ops(api)] == ["device.rotate", "screen.bounds"]


async def test_turning_it_the_way_it_already_is_says_the_same_thing_twice(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        first = text_of(await s.call_tool(TOOL, {"device": sid, "orientation": "portrait"}))
        second = text_of(await s.call_tool(TOOL, {"device": sid, "orientation": "portrait"}))
    assert first == second == f"Turned the phone to portrait. The screen is now {TALL}. {STALE}"


async def test_a_phone_held_another_way_than_asked_did_not_turn(api):
    api.rotate_answers = {"device": "portrait", "orientation": "portrait", "turned": 0}
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_right"})
    assert r.is_error
    assert text_of(r) == failed("The phone did not turn to landscape_right: it is held portrait. Call "
                                "phone_set_orientation once more, then take a screenshot to see which way up it is.")


async def test_how_the_screen_is_drawn_is_not_how_the_phone_is_held(api):
    """`orientation` in the host's answer is the screen's, so "portrait" there
    after a turn to landscape is an app that did not turn, not a phone that did not."""
    api.rotate_answers = {"device": "landscape_left", "orientation": "portrait", "turned": 1}
    api.app_turns = False
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert not r.is_error and "what is on screen did not turn with it" in text_of(r)


async def test_a_turn_whose_size_cannot_be_read_still_says_whether_the_screen_followed(api):
    api.fail_bounds = True
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        followed = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
        api.app_turns = False
        stayed = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_right"})
    assert not followed.is_error and text_of(followed) == f"Turned the phone to landscape_left. {STALE}"
    assert not stayed.is_error and text_of(stayed) == (
        "The phone is turned to landscape_right, but what is on screen did not turn with it. An app that supports "
        "only one way up stays that way: open one that turns, or carry on with the screen as it is.")


@pytest.mark.parametrize("answer", [
    {"device": "landscape_left", "orientation": "landscape_left", "turned": 1, "w": 844, "h": 390},
    {"orientation": "landscape_left", "bounds": {"x": 0, "y": 0, "w": 844, "h": 390}},
    {"w": 844.0, "h": 390.0},
])
async def test_a_host_that_answers_with_the_size_is_not_asked_for_it_again(api, answer):
    api.rotate_answers = answer
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert text_of(r) == f"Turned the phone to landscape_left. The screen is now 844x390. {STALE}"
    assert ops(api) == [("device.rotate", {"to": "landscape_left"})]


# A host that does not say how the screen is drawn: the screen is measured from a screenshot.

@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("answer", [None, True, {}, "landscape_left", {"orientation": None}, {"device": "landscape_left"},
                                    {"orientation": "sideways"}, {"w": True, "h": 390}, {"w": 0, "h": 0}])
async def test_an_answer_that_does_not_say_is_followed_by_a_screenshot(api, answer, stale):
    """The shape of the host's answer is its own. Whatever it is, the size is
    found out, and from the image, because the reported size can be an old one."""
    api.rotate_answers, api.stale_bounds = answer, stale
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert not r.is_error
    assert text_of(r) == f"Turned the phone to landscape_left. The screen is now {WIDE}. {STALE}"
    assert [op for op, _kw in ops(api)] == ["device.rotate", "screen.capture"]


@pytest.mark.parametrize("lag,captures", [(0, 1), (1, 2)])
async def test_a_turn_still_being_drawn_is_looked_at_once_more(api, lag, captures):
    api.rotate_answers, api.turn_lag = True, lag
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert text_of(r) == f"Turned the phone to landscape_left. The screen is now {WIDE}. {STALE}"
    assert [op for op, _kw in ops(api)] == ["device.rotate"] + ["screen.capture"] * captures


async def test_an_app_that_does_not_turn_is_looked_at_twice_and_no_more(api):
    api.rotate_answers, api.app_turns = True, False
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert f"did not turn with it and is still {TALL}" in text_of(r)
    assert [op for op, _kw in ops(api)] == ["device.rotate", "screen.capture", "screen.capture"]


async def test_a_turn_that_cannot_be_looked_at_is_still_a_turn(api):
    api.rotate_answers, api.fail_capture = True, True
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert not r.is_error
    assert text_of(r) == f"Turned the phone to landscape_left. {STALE}"


# --- a phone that will not turn ------------------------------------------------------
#
# The host refuses a turn on a locked phone, and when iOS's Portrait Orientation
# Lock is on. It says which with a code. The API's op route answers a plain 502.

async def test_a_502_from_a_turn_names_both_things_that_stop_one(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.orientation_lock = True
        upright = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
        api.orientation_lock = False
        api.locked.add(sid)
        asleep = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    want = failed(f"Phone Harness Cloud answered 502: {NOT_DONE} {MAYBE_ASLEEP} {MAYBE_LOCKED_UPRIGHT}")
    assert upright.is_error and text_of(upright) == want
    assert asleep.is_error and text_of(asleep) == want
    assert "phone_wake" in want and "Portrait Orientation Lock" in want and "Control Center" in want


async def test_an_api_that_passes_the_hosts_code_on_says_which(api):
    api.locked_code = True
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.orientation_lock = True
        upright = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
        api.orientation_lock = False
        api.locked.add(sid)
        asleep = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert upright.is_error and text_of(upright) == failed(LOCKED_UPRIGHT)
    assert asleep.is_error and text_of(asleep) == failed(LOCKED)
    assert "turned off in Control Center" in LOCKED_UPRIGHT


async def test_portrait_orientation_lock_does_not_stop_a_turn_to_where_the_phone_is(api):
    api.orientation_lock = True
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "portrait"})
    assert not r.is_error and text_of(r) == f"Turned the phone to portrait. The screen is now {TALL}. {STALE}"


async def test_a_502_from_another_op_says_nothing_about_that_lock(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 1, "y": 2})
    assert r.is_error and "phone_wake" in text_of(r) and "Portrait Orientation Lock" not in text_of(r)


# --- what is not a way up -------------------------------------------------------------

@pytest.mark.parametrize("args", [
    {}, {"orientation": "landscape"}, {"orientation": "upside_down"}, {"orientation": "portrait_upside_down"},
    {"orientation": "left"}, {"orientation": "LANDSCAPE_LEFT"}, {"orientation": "landscape-left"},
    {"orientation": ""}, {"orientation": None}, {"orientation": 90}, {"orientation": True},
    {"orientation": ["portrait"]}, {"to": "portrait"},
])
async def test_anything_else_is_a_schema_error_and_nothing_is_sent(api, args):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, **args})
    assert r.is_error and "orientation" in text_of(r)
    assert api.ops == []


# --- phones that cannot be turned -------------------------------------------------------

async def test_an_android_emulator_cannot_be_turned_from_here(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("android")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert r.is_error and text_of(r) == failed(NO_TURN[0])
    assert "Android emulator" in NO_TURN[0] and "stays the way it is" in NO_TURN[0]
    assert [op for op, _kw in ops(api)] == ["device.rotate"]          # refused, so no size was asked for


async def test_an_iphone_whose_host_cannot_turn_it_says_yet(api):
    """The host offers the op only once turning is known to be safe: until then every iPhone answers this."""
    api.old_host = ("device.rotate",)
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert r.is_error and text_of(r) == failed(NO_TURN[1])
    assert "yet" in NO_TURN[1]


async def test_an_api_that_does_not_serve_the_op_yet_says_the_same(api):
    api.not_served = ("device.rotate",)
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "portrait"})
    assert r.is_error and text_of(r) == failed(NO_TURN[1])
    assert "op must be one of" not in text_of(r)


async def test_a_phone_the_api_will_not_describe_gets_the_plain_sentence(api):
    api.old_host = ("device.rotate",)
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.sessions[sid]["platform"] = None
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "portrait"})
    assert r.is_error and text_of(r) == failed(NO_TURN[2])


async def test_a_phone_that_is_not_running(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool(TOOL, {"device": "s_nope", "orientation": "portrait"})
    assert r.is_error and "device s_nope is not running" in text_of(r)
    assert api.ops == []


async def test_a_busy_phone_is_retried_for_this_too(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.busy_times = 2
        r = await s.call_tool(TOOL, {"device": sid, "orientation": "landscape_left"})
    assert not r.is_error and f"The screen is now {WIDE}." in text_of(r)
