"""phone_wake and phone_open_url (PHO-335).

A cloud iPhone that has been left alone goes to sleep, and the phone's host
then refuses to touch it. Until these two tools, an agent that met the lock
screen had nothing to call, and a link had to be typed into Safari by hand.

The fake API answers a locked iPhone the way phone-cloud's op route does: a
plain 502 with no reason. Each locked test also runs against an API that
passes the host's own `409 locked` on, because the server reads both.
"""
import re
from pathlib import Path

import pytest

from fake_api import HOST_LOCKED, NOT_DONE, SAFARI
from phone_harness_mcp.phone import LOCKED, MAYBE_ASLEEP
from phone_harness_mcp.server import (ALREADY_AWAKE, AWAKE, INSTRUCTIONS, NO_LINK, NO_WAKE, NOT_UP, STILL_DARK,
                                      STILL_LOCKED, STILL_STANDBY, URL_MAX)
from support import connected, stdio_server, text_of

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parent.parent
LINK = "https://example.com/sign-in?token=s3cr3t-s3cr3t-s3cr3t"
OPENED = "Opened the link in the phone's browser. Give the page a moment to load, then read the screen."


def failed(tool, sentence):
    """What a client is handed when a tool raises: the SDK names the tool, then the sentence."""
    return f"Error executing tool {tool}: {sentence}"


def sent(api, name):
    return [(op, kw) for _sid, op, kw in api.ops if op == name]


# --- what a client is told about the two tools -----------------------------------

async def test_both_tools_are_listed_with_their_arguments(api):
    async with connected(stdio_server(api)) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    wake, link = tools["phone_wake"], tools["phone_open_url"]
    assert wake.input_schema["required"] == ["device"]
    assert set(wake.input_schema["properties"]) == {"device"}
    assert link.input_schema["required"] == ["device", "url"]
    assert set(link.input_schema["properties"]) == {"device", "url"}
    assert link.input_schema["properties"]["url"]["type"] == "string"
    # Neither only reads, and neither destroys anything. Waking twice is waking once.
    assert wake.annotations.read_only_hint is False and wake.annotations.destructive_hint is False
    assert wake.annotations.idempotent_hint is True
    assert link.annotations.read_only_hint is False and link.annotations.destructive_hint is False


async def test_the_hosted_server_has_them_too(api):
    async with connected(stdio_server(api, allow_files=False)) as s:
        names = {t.name for t in (await s.list_tools()).tools}
    assert {"phone_wake", "phone_open_url"} <= names


async def test_the_descriptions_say_what_a_model_has_to_know(api):
    async with connected(stdio_server(api)) as s:
        tools = {t.name: t.description for t in (await s.list_tools()).tools}
    wake, link = tools["phone_wake"], tools["phone_open_url"]
    # There is no passcode, so there is none to ask the user for.
    assert "set up with no passcode" in wake and "never ask the user for a code" in wake
    assert "lock screen" in wake and "StandBy" in wake and "screenshot is black" in wake
    assert "does nothing to a phone that is already awake" in wake
    assert "Android emulator has no such control" in wake
    # https only, and what to do first when the phone is locked.
    assert "must start with https://" in link and str(URL_MAX) in link
    assert "call phone_wake first if it is locked" in link
    assert "Cloud iPhones only" in link


def test_the_instructions_say_when_to_wake_and_that_there_is_no_passcode():
    paragraph = next(p for p in INSTRUCTIONS.split("\n\n") if "phone_wake" in p)
    assert "asleep (a black screenshot), on its lock screen or showing the StandBy clock" in paragraph
    assert "do nothing or are refused" in paragraph      # a black screen takes a tap and shows nothing
    assert "set up with no passcode, so never ask the user for one" in paragraph
    assert "phone_open_url opens an https link" in paragraph
    # One paragraph of its own, so another branch's paragraph merges beside it.
    assert INSTRUCTIONS.count("phone_wake") == 1 and INSTRUCTIONS.count("phone_open_url") == 1


def test_the_docs_list_both_and_no_longer_say_no_tool_opens_a_url():
    readme = (ROOT / "README.md").read_text()
    hosting = (ROOT / "docs" / "self-hosting.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    for name in ("phone_wake", "phone_open_url"):
        assert re.search(rf"^\| `{name}` \| ", readme, re.MULTILINE), name
        assert f"`{name}`" in changelog, name
    assert "no tool that opens a URL" not in readme and "no tool that opens a URL" not in hosting
    assert "There is no tool that installs an app, reads the clipboard, or records the screen." in hosting
    unreleased = changelog.split("## 0.1.0")[0]
    assert "no passcode" in unreleased and "https" in unreleased


# --- waking ----------------------------------------------------------------------

async def test_wake_sends_one_op_and_says_the_phone_is_awake(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        api.ops.clear()
        r = await s.call_tool("phone_wake", {"device": sid})
    assert not r.is_error and text_of(r) == AWAKE
    assert api.ops == [(sid, "device.wake", {})]
    assert sid not in api.locked


async def test_waking_a_phone_that_is_awake_presses_nothing_and_says_so(api):
    """A model that woke the phone because a tap failed learns the phone was not the reason."""
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        first = text_of(await s.call_tool("phone_wake", {"device": sid}))
        second = text_of(await s.call_tool("phone_wake", {"device": sid}))
    assert first == second == ALREADY_AWAKE
    assert "nothing was pressed" in ALREADY_AWAKE


@pytest.mark.parametrize("answer", [
    {"state": "ready", "was": "asleep", "woke": True, "through": "home"},       # two Home presses from sleep
    {"state": "ready", "was": "standby", "woke": True, "through": "app"},      # StandBy: an app, then Home
    {"state": "ready", "was": "locked", "woke": True, "through": "home"},
    {"state": " Ready "}, {"state": "ready", "woke": None},
    "ready", True, None, {},
])
async def test_every_way_of_saying_awake_is_read(api, answer):
    """The host answers {state, was, woke, through}. A bare state word says it
    too, and a host that reports nothing has not failed."""
    api.wake_answers = answer
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool("phone_wake", {"device": sid})
    assert not r.is_error and text_of(r) == AWAKE
    assert "may be on its Home Screen" in AWAKE        # getting past StandBy ends there


@pytest.mark.parametrize("answer,says", [
    ({"state": "locked", "was": "locked", "woke": True, "through": "home"}, STILL_LOCKED),
    ({"state": "standby", "was": "standby", "woke": True, "through": "app"}, STILL_STANDBY),
    ({"state": "asleep", "was": "asleep", "woke": True, "through": "home"}, STILL_DARK),
    ({"state": "not-running", "was": "not-running", "woke": False, "through": None}, NOT_UP),
    ("locked", STILL_LOCKED), ({"state": "StandBy"}, STILL_STANDBY),
])
async def test_a_phone_the_wake_did_not_bring_back_is_an_error_that_says_what_next(api, answer, says):
    api.wake_answers = answer
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool("phone_wake", {"device": sid})
    assert r.is_error and text_of(r) == failed("phone_wake", says)


def test_each_of_those_sentences_says_what_to_do():
    assert "passcode has been set" in STILL_LOCKED and "tell the user" in STILL_LOCKED
    assert "StandBy" in STILL_STANDBY and "tell the user" in STILL_STANDBY
    assert "take a screenshot" in STILL_DARK and "phone_press_button" in STILL_DARK
    assert "phone_list_available_devices" in NOT_UP


async def test_a_passcode_someone_set_is_not_got_past(api):
    """A cloud iPhone is set up without one, and an account can be allowed to
    set one. Nothing here types it."""
    api.passcode = True
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        api.ops.clear()
        r = await s.call_tool("phone_wake", {"device": sid})
    assert r.is_error and text_of(r) == failed("phone_wake", STILL_LOCKED)
    assert [op for _sid, op, _kw in api.ops] == ["device.wake"]      # one wake, and no typing after it


async def test_a_state_this_server_does_not_know_is_reported_as_it_is(api):
    api.wake_answers = {"state": "charging"}
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool("phone_wake", {"device": sid})
    assert r.is_error
    assert text_of(r) == failed("phone_wake", "The phone did not wake: it reports 'charging'. Take a screenshot to "
                                           "see it, and try phone_wake once more.")


async def test_an_android_emulator_has_no_wake_control(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("android")
        r = await s.call_tool("phone_wake", {"device": sid})
    assert r.is_error and text_of(r) == failed("phone_wake", NO_WAKE[0])
    assert "Android emulator" in NO_WAKE[0] and "phone_press_button" in NO_WAKE[0]


async def test_an_iphone_whose_host_has_no_wake_yet_says_yet(api):
    api.old_host = ("device.wake",)
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool("phone_wake", {"device": sid})
    assert r.is_error and text_of(r) == failed("phone_wake", NO_WAKE[1])
    assert "no wake control yet" in NO_WAKE[1] and "HOME" in NO_WAKE[1]


async def test_an_api_that_does_not_serve_the_op_yet_is_not_the_models_mistake(api):
    """Until phone-cloud serves `device.wake` and `url.open` on its op route, it
    answers 400 with the list of names it does serve. That list is not shown."""
    api.not_served = ("device.wake", "url.open")
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        woke = await s.call_tool("phone_wake", {"device": sid})
        opened = await s.call_tool("phone_open_url", {"device": sid, "url": LINK})
        droid = api.running("android")
        on_android = await s.call_tool("phone_wake", {"device": droid})
    assert woke.is_error and text_of(woke) == failed("phone_wake", NO_WAKE[1])
    assert opened.is_error and text_of(opened) == failed("phone_open_url", NO_LINK[1])
    assert on_android.is_error and text_of(on_android) == failed("phone_wake", NO_WAKE[0])
    for r in (woke, opened, on_android):
        assert "op must be one of" not in text_of(r) and "screen.capture" not in text_of(r)


async def test_wake_on_a_phone_that_is_not_running(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_wake", {"device": "s_nope"})
    assert r.is_error and "device s_nope is not running" in text_of(r)
    assert not api.ops


# --- a locked iPhone, met by the other tools ---------------------------------------

TOUCHES = [
    ("phone_click_on_screen_at_coordinates", {"x": 10, "y": 20}),
    ("phone_long_press_on_screen_at_coordinates", {"x": 10, "y": 20}),
    ("phone_swipe_on_screen", {"direction": "up", "x": 100, "y": 400, "distance": 200}),
    ("phone_type_keys", {"text": "hello"}),
    ("phone_press_button", {"button": "APP_SWITCHER"}),
    ("phone_press_button", {"button": "ENTER"}),
    ("phone_launch_app", {"name": "Notes"}),
    ("phone_open_url", {"url": LINK}),
]


@pytest.mark.parametrize("tool,args", TOUCHES)
async def test_a_locked_iphone_points_at_phone_wake(api, tool, args):
    """What phone-cloud's op route answers for a locked iPhone is a 502 with no
    reason. The server adds the one reason a tool here can fix."""
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        r = await s.call_tool(tool, {"device": sid, **args})
    assert r.is_error
    assert text_of(r) == failed(tool, f"Phone Harness Cloud answered 502: {NOT_DONE} {MAYBE_ASLEEP}")
    assert "call phone_wake and try again" in MAYBE_ASLEEP and "StandBy" in MAYBE_ASLEEP


@pytest.mark.parametrize("tool,args", TOUCHES)
async def test_an_api_that_passes_locked_on_is_told_plainly(api, tool, args):
    api.locked_code = True
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        n = len(api.requests)
        r = await s.call_tool(tool, {"device": sid, **args})
    assert r.is_error and text_of(r) == failed(tool, LOCKED)
    assert "Call phone_wake, then try again" in LOCKED and "no passcode" in LOCKED
    assert HOST_LOCKED not in text_of(r)        # "unlock it on the phone" is not something a customer can do
    assert len([q for q in api.requests[n:] if q[0] == "POST"]) == 1     # refused once, not retried as busy


@pytest.mark.parametrize("coded", [False, True])
async def test_locked_then_woken_then_the_tap_lands(api, coded):
    api.locked_code = coded
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        refused = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 10, "y": 20})
        assert refused.is_error and "phone_wake" in text_of(refused)
        assert text_of(await s.call_tool("phone_wake", {"device": sid})) == AWAKE
        r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 10, "y": 20})
    assert not r.is_error and text_of(r) == "Tapped (10, 20)."


async def test_looking_at_a_locked_iphone_and_pressing_home_still_work(api):
    """The lock screen is how a model learns the phone is locked, so it has to be able to see it."""
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.locked.add(sid)
        shot = await s.call_tool("phone_take_screenshot", {"device": sid})
        text = await s.call_tool("phone_list_elements_on_screen", {"device": sid})
        home = await s.call_tool("phone_press_button", {"device": sid, "button": "HOME"})
    assert not shot.is_error and not text.is_error and not home.is_error


async def test_a_502_on_an_android_emulator_says_nothing_about_waking(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("android")
        api.locked.add(sid)             # the fake's way of failing these ops with a 502
        r = await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 10, "y": 20})
    assert r.is_error and text_of(r) == failed("phone_click_on_screen_at_coordinates",
                                               f"Phone Harness Cloud answered 502: {NOT_DONE}")


async def test_the_hint_is_only_asked_for_after_a_502(api):
    """Wording the error costs one more request, and only an op that failed pays it."""
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        n = len(api.requests)
        await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 10, "y": 20})
        assert [q[:2] for q in api.requests[n:]] == [("POST", f"/sessions/{sid}/op")]
        api.locked.add(sid)
        n = len(api.requests)
        await s.call_tool("phone_click_on_screen_at_coordinates", {"device": sid, "x": 10, "y": 20})
    assert [q[:2] for q in api.requests[n:]] == [("POST", f"/sessions/{sid}/op"), ("GET", f"/sessions/{sid}")]


# --- opening a link ----------------------------------------------------------------

async def test_open_url_sends_the_link_whole_and_does_not_repeat_it(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.ops.clear()
        r = await s.call_tool("phone_open_url", {"device": sid, "url": LINK})
    assert not r.is_error and text_of(r) == OPENED
    assert api.ops == [(sid, "url.open", {"url": LINK})]
    assert "s3cr3t" not in text_of(r) and SAFARI not in text_of(r)


async def test_spaces_around_a_link_are_dropped_before_it_is_sent(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.ops.clear()
        r = await s.call_tool("phone_open_url", {"device": sid, "url": f"  {LINK}\n"})
    assert not r.is_error
    assert api.ops == [(sid, "url.open", {"url": LINK})]


async def test_the_longest_link_a_phone_takes_is_sent(api):
    longest = "https://example.com/" + "a" * (URL_MAX - len("https://example.com/"))
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool("phone_open_url", {"device": sid, "url": longest})
    assert not r.is_error and sent(api, "url.open") == [("url.open", {"url": longest})]


NOT_HTTPS = "url must be an https:// link. Other schemes (http, file, javascript, an app's own) are refused."
NOT_ONE_LINE = ("url must be one line of plain ASCII with no spaces. Percent-encode anything else "
                "(a space is %20).")


@pytest.mark.parametrize("url,says", [
    ("http://example.com/s3cr3t", NOT_HTTPS),
    ("javascript:alert('s3cr3t')", NOT_HTTPS),
    ("file:///etc/passwd?s3cr3t", NOT_HTTPS),
    ("data:text/html,<b>s3cr3t</b>", NOT_HTTPS),
    ("shortcuts://run-shortcut?name=s3cr3t", NOT_HTTPS),
    ("itms-services://?action=download-manifest&url=s3cr3t", NOT_HTTPS),
    ("prefs:root=General", NOT_HTTPS),
    ("HTTPS://example.com/s3cr3t", NOT_HTTPS),
    ("https:/example.com/s3cr3t", NOT_HTTPS),
    ("example.com/s3cr3t", NOT_HTTPS),
    ("https://", NOT_HTTPS),
    ("", NOT_HTTPS),
    ("   ", NOT_HTTPS),
    ("https://example.com/a b?s3cr3t", NOT_ONE_LINE),
    ("https://example.com/a\tb?s3cr3t", NOT_ONE_LINE),
    ("https://example.com/a\nhttps://evil.example/s3cr3t", NOT_ONE_LINE),
    ("https://example.com/\x00s3cr3t", NOT_ONE_LINE),
    ("https://example.com/\x7fs3cr3t", NOT_ONE_LINE),
    ("https://example.com/café?s3cr3t", NOT_ONE_LINE),
    ("https://example.com/ s3cr3t", NOT_ONE_LINE),
    ("https://example.com/" + "s3cr3t" * 700, f"url is 4220 characters long, and a phone takes at most {URL_MAX}."),
])
async def test_a_link_a_phone_would_refuse_is_refused_here_and_nothing_is_sent(api, url, says):
    """The same checks the phone's host makes. The link is never repeated: it
    is often a one-time sign-in link, and what reaches an error reaches a log."""
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.ops.clear()
        r = await s.call_tool("phone_open_url", {"device": sid, "url": url})
    assert r.is_error and text_of(r) == failed("phone_open_url", says)
    assert "s3cr3t" not in text_of(r)
    assert api.ops == []


@pytest.mark.parametrize("args", [{}, {"url": None}, {"url": 7}, {"url": ["https://example.com"]}])
async def test_a_url_that_is_not_text_is_a_schema_error_and_nothing_is_sent(api, args):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.ops.clear()
        r = await s.call_tool("phone_open_url", {"device": sid, **args})
    assert r.is_error and "url" in text_of(r)
    assert api.ops == []


async def test_an_android_emulator_cannot_be_handed_a_link(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("android")
        r = await s.call_tool("phone_open_url", {"device": sid, "url": LINK})
    assert r.is_error and text_of(r) == failed("phone_open_url", NO_LINK[0])
    assert "Android emulator" in NO_LINK[0] and "phone_launch_app" in NO_LINK[0] and "phone_type_keys" in NO_LINK[0]
    assert "s3cr3t" not in text_of(r)


async def test_an_iphone_whose_host_cannot_open_links_says_yet(api):
    api.old_host = ("url.open",)
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        r = await s.call_tool("phone_open_url", {"device": sid, "url": LINK})
    assert r.is_error and text_of(r) == failed("phone_open_url", NO_LINK[1])
    assert "Safari" in NO_LINK[1] and "yet" in NO_LINK[1]


async def test_a_phone_the_api_will_not_describe_gets_the_plain_sentence(api):
    """Wording the refusal asks what kind of phone it is. When that fails too,
    the refusal is still given, without a guess at the phone."""
    api.old_host = ("device.wake", "url.open")
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.sessions[sid]["platform"] = None
        woke = await s.call_tool("phone_wake", {"device": sid})
        opened = await s.call_tool("phone_open_url", {"device": sid, "url": LINK})
    assert woke.is_error and text_of(woke) == failed("phone_wake", NO_WAKE[2])
    assert opened.is_error and text_of(opened) == failed("phone_open_url", NO_LINK[2])


async def test_open_url_on_a_phone_that_is_not_running(api):
    async with connected(stdio_server(api)) as s:
        r = await s.call_tool("phone_open_url", {"device": "s_nope", "url": LINK})
    assert r.is_error and "device s_nope is not running" in text_of(r)
    assert "s3cr3t" not in text_of(r)


async def test_a_busy_phone_is_retried_for_these_too(api):
    async with connected(stdio_server(api)) as s:
        sid = api.running("ios")
        api.busy_times = 2
        r = await s.call_tool("phone_wake", {"device": sid})
        api.busy_times = 1
        opened = await s.call_tool("phone_open_url", {"device": sid, "url": LINK})
    assert not r.is_error and not opened.is_error
