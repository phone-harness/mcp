"""The MCP server: one tool per thing an agent does to a phone.

Tool names follow mobile-mcp (`mobile_take_screenshot` here is
`phone_take_screenshot`), so an agent that has driven a phone through that
server already knows these. Every driving tool takes `device`, the id from
`phone_list_available_devices`; there is no implicit current device, so the
server keeps no state between calls and can sit behind a load balancer.
"""
from __future__ import annotations

import functools
import json
import time
from pathlib import Path
from typing import Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import TextContent, ToolAnnotations

from . import __version__, screenshots
from .auth import bearer_of
from .cloud import Cloud, StaticToken
from .phone import DEFAULT_MINUTES, READY_WAIT, PhoneError, Phones, Unsupported, describe, minutes_left
from .reservations import register as register_reservations

INSTRUCTIONS = """Drive a Phone Harness Cloud phone: a real iPhone or an Android emulator, reached over HTTPS.

Start with phone_list_available_devices. If a phone is running, pass its id as `device` to every other tool. If none is, phone_start_device starts one. Cloud Android is pay as you go at 5 cents per minute; a cloud iPhone is a flat monthly subscription ($80/mo, $100/mo with eSIM, or 20% off billed annually at $64/mo, $80/mo with eSIM) and is not billed by the minute. Call phone_stop_device when the task is done.

To find something to tap, prefer phone_list_elements_on_screen (visible text with tap-ready centers) over a screenshot; take a screenshot to understand layout, or when there is no text. Coordinates are screen pixels; the screenshot tool says how to convert from the image it returns. After a tap or typing, read the screen again rather than assuming it worked.

A cloud iPhone that has been left alone can be asleep (a black screenshot), on its lock screen or showing the StandBy clock. Taps, typing and app launches then do nothing or are refused. phone_wake brings it back. These phones are set up with no passcode, so never ask the user for one. phone_open_url opens an https link in an iPhone's browser whole, with no typing: use it for a long link or a sign-in link. phone_set_orientation turns an iPhone to portrait or to either landscape; the screen size and every coordinate change with it, so read the screen again afterwards.

An account that has no cloud iPhone yet, or needs more of them, reserves them with a $20 per phone refundable deposit. phone_reserve returns a quote, which is authoritative, and a Stripe Checkout link. Reservations are for iPhones only (with or without eSIM). Cloud Android emulators are on the $20 plan and are not reserved. The iPhone prices above are the standard ones: a customer's first phone is at the intro price, so tell the user what the quote from phone_reserve says, not a price from memory. Give the link to the user: only a person can pay it, no tool here pays, and nothing is reserved until they do. Places in the queue go in the order deposits were paid, and nothing moves one up. phone_list_reservations shows each reservation's status and place; the website has no list of reservations, so that tool is the list. phone_cancel_reservation gives a place up, and the deposit still held goes back automatically to the card that paid; it needs confirm=true, which is the user's yes, so ask first. When a phone is handed over, its deposit becomes a credit on the first bill and is no longer refundable. An account can have five unpaid reservations an hour, and cancelling one does not free a place."""

READ_ONLY = ToolAnnotations(read_only_hint=True)
TOUCH = ToolAnnotations(read_only_hint=False, destructive_hint=False)
# Waking a phone that is awake, or turning one the way it already is, changes
# nothing: both are safe to repeat.
REPEATABLE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

BUTTONS = {
    "HOME": ("nav.home", {}),
    "BACK": ("nav.back", {}),
    "APP_SWITCHER": ("nav.recents", {}),
    "ENTER": ("input.keys", {"combo": "enter"}),
}
NO_BACK = ("This phone has no Back button (an iPhone). Use the app's own back control, usually at the top "
           "left, or swipe right from the left edge of the screen.")
NO_APP_LIST = "This phone does not list its apps. Launch one by its name with phone_launch_app."
NO_FOREGROUND = ("This phone does not report which app is in front. Take a screenshot or list the elements on screen "
                 "to see where you are.")

# What a phone that cannot do one of these is told to do instead, as (an
# Android emulator, an iPhone, a phone the API would not describe). An Android
# emulator never can. An iPhone can once its host has the op, so "yet".
NO_WAKE = (
    ("This phone is an Android emulator, which has no wake control. Press HOME with phone_press_button, then "
     "take a screenshot to see where it is."),
    ("This iPhone has no wake control yet. Press HOME with phone_press_button, which wakes a sleeping iPhone, "
     "then take a screenshot to see whether it did."),
    ("This phone has no wake control. Press HOME with phone_press_button, then take a screenshot to see where "
     "it is."),
)
NO_LINK = (
    ("This phone is an Android emulator, which cannot be handed a link. Launch its browser with "
     "phone_launch_app, tap the address bar, and type the address with phone_type_keys."),
    ("This iPhone cannot be handed a link yet. Launch Safari with phone_launch_app, tap the address bar, and "
     "type the address with phone_type_keys."),
    ("This phone cannot be handed a link. Launch its browser with phone_launch_app, tap the address bar, and "
     "type the address with phone_type_keys."),
)

NO_TURN = (
    "This phone is an Android emulator, which cannot be turned from here. Its screen stays the way it is.",
    "This iPhone cannot be turned from here yet. Its screen stays the way it is.",
    "This phone cannot be turned from here. Its screen stays the way it is.",
)
# The ways up a phone can be asked to be. The two landscapes are different
# answers: iOS's landscapeLeft and landscapeRight.
Orientation = Literal["portrait", "landscape_left", "landscape_right"]
STALE = ("Everything read before the turn is stale: take a new screenshot or list the elements on screen before "
         "the next tap.")

URL_MAX = 4096          # the limit the phone's host keeps; a signed storage link is under half of it

# What phone_wake says for each state the phone's host can leave a phone in.
AWAKE = ("The phone is awake and takes input. Take a screenshot or list the elements on screen to see where it "
         "is: it may be on its Home Screen and not where it was left.")
ALREADY_AWAKE = ("The phone was already awake, so nothing was pressed. Take a screenshot or list the elements on "
                 "screen to see what it shows.")
STILL_LOCKED = ("The phone is still on its lock screen after the wake. Call phone_wake once more. If it stays "
                "locked, a passcode has been set on this phone: no tool here enters one, so tell the user.")
STILL_STANDBY = ("The phone is still showing the StandBy clock after the wake. Call phone_wake once more. If it "
                 "stays there, tell the user the phone is stuck in StandBy.")
STILL_DARK = ("The phone's screen is still black after the wake. An app that shows nothing looks the same, and "
              "taps are let through: take a screenshot, then press HOME with phone_press_button.")
NOT_UP = ("The phone's session is not up, so it could not be woken. phone_list_available_devices shows whether "
          "it is still running.")
NOT_WOKEN = {"locked": STILL_LOCKED, "standby": STILL_STANDBY, "asleep": STILL_DARK, "not-running": NOT_UP}


def _cannot(platform: str | None, android: str, iphone: str, unknown: str) -> str:
    return {"android": android, "ios": iphone}.get(platform or "", unknown)


def checked_url(url) -> str:
    """The link, if a phone may be handed it: the checks the phone's host
    makes, made here first so the model hears which one failed. No message
    repeats the link, because it is often a one-time sign-in link."""
    link = url.strip() if isinstance(url, str) else ""
    if not link.startswith("https://") or len(link) == len("https://"):
        raise PhoneError("url must be an https:// link. Other schemes (http, file, javascript, an app's own) "
                         "are refused.")
    if len(link) > URL_MAX:
        raise PhoneError(f"url is {len(link)} characters long, and a phone takes at most {URL_MAX}.")
    if any(ord(c) < 33 or ord(c) > 126 for c in link):
        raise PhoneError("url must be one line of plain ASCII with no spaces. Percent-encode anything else "
                         "(a space is %20).")
    return link


def _size(value) -> tuple[int, int] | None:
    """(w, h) from an object that carries a screen size, or None."""
    if not isinstance(value, dict):
        return None
    w, h = value.get("w"), value.get("h")
    if all(isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 for n in (w, h)):
        return _point(w), _point(h)
    return None


def _turned(asked: str, result, size: tuple[int, int] | None) -> str:
    """What turning the phone came to, as a sentence. `result` is the phone's
    answer to `device.rotate` and `size` the screen's size after it. A phone can
    be turned and the app in front still not turn with it, so the size is what
    says whether anything on screen moved."""
    got = result.get("orientation") if isinstance(result, dict) else None
    if isinstance(got, str) and got.strip().lower() != asked:
        raise PhoneError(f"The phone did not turn to {asked}: it reports {got.strip().lower()}. Call "
                         "phone_set_orientation once more, then take a screenshot to see which way up it is.")
    if size is None:
        return f"Turned the phone to {asked}. {STALE}"
    w, h = size
    if (w > h) != (asked != "portrait"):
        return (f"The phone is turned to {asked}, but what is on screen did not turn with it and is still "
                f"{w}x{h}. An app that supports only one way up stays that way: open one that turns, or carry "
                "on with the screen as it is.")
    return f"Turned the phone to {asked}. The screen is now {w}x{h}. {STALE}"


def _woke(result) -> str:
    """What `device.wake` answered, as a sentence. The phone's host answers
    {state, was, woke, through}: the state it left the phone in (ready, locked,
    standby, asleep or not-running), the state it found, and whether it had to
    press anything. A bare state word is read too, and a host that reports
    nothing has not failed."""
    report = result if isinstance(result, dict) else {"state": result}
    state = report.get("state")
    state = state.strip().lower() if isinstance(state, str) else ""
    if state in NOT_WOKEN:
        raise PhoneError(NOT_WOKEN[state])
    if state and state != "ready":
        raise PhoneError(f"The phone did not wake: it reports {state!r}. Take a screenshot to see it, and try "
                         "phone_wake once more.")
    return ALREADY_AWAKE if report.get("woke") is False else AWAKE


def stdio_cloud(credentials, url: str | None = None):
    """One account for the whole process: this machine's own sign-in."""
    cloud = Cloud(credentials, url)
    return lambda ctx: cloud


def hosted_cloud(url: str | None = None):
    """Whoever is calling: the bearer on this HTTP request, and nothing else."""
    def pick(ctx):
        token = bearer_of(ctx)
        if not token:
            raise PhoneError("This server needs `Authorization: Bearer <Phone Harness API key>` on every request.")
        return Cloud(StaticToken(token), url)
    return pick


async def _thread(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def _point(value) -> int:
    return int(round(float(value)))


def _devices_text(live: list[dict], me: dict) -> str:
    lines = []
    if live:
        lines.append("Running phones (pass the id as `device`):")
        lines.extend("- " + describe(s) for s in live)
    else:
        lines.append("No phone is running. phone_start_device starts one.")
    catalog = me.get("available") or []
    if catalog:
        lines.append("")
        lines.append("Phones this account can start:")
        for entry in catalog:
            bits = [f"platform={entry.get('platform')}", f"kind={entry.get('kind')}"]
            if entry.get("id"):
                bits.append(f"device_id={entry['id']}")
            if entry.get("state"):
                bits.append(str(entry["state"]))
            if entry.get("temporary"):
                bits.append("temporary, billed per minute")
            lines.append(f"- {entry.get('label') or 'phone'}: " + ", ".join(bits))
    line = _balance_line(me)
    if line:
        lines.extend(["", line])
    return "\n".join(lines)


def _balance_line(me: dict) -> str | None:
    """The account's money, and Android minutes only when usage is metered.

    Cloud Android spends credit at `price_cents_per_minute`. A cloud iPhone is
    a flat monthly subscription: `/me` then sends `billing_mode` "disabled",
    and it can still send `price_cents_per_minute`. That rate is not a charge,
    so the footer stays the dollar balance. A missing `billing_mode` is metered,
    which is what older service responses look like.
    """
    balance = me.get("balance_cents")
    if not isinstance(balance, (int, float)) or isinstance(balance, bool):
        return None
    line = f"Balance: ${balance / 100:.2f}"
    price = me.get("price_cents_per_minute")
    metered = me.get("billing_mode") != "disabled"
    if metered and isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        minutes = int(balance // price)
        line += f" (about {minutes} min at ${price / 100:.2f}/min)"
    return line


def _elements_text(boxes: list[dict], bounds: dict | None) -> str:
    boxes = [b for b in boxes if isinstance(b, dict) and str(b.get("text") or "").strip()]
    if not boxes:
        return "No text on screen. Take a screenshot to see what is there."
    boxes.sort(key=lambda b: (round(float(b.get("y", 0)) / 12), float(b.get("x", 0))))
    head = f"Screen is {bounds['w']}x{bounds['h']}. " if bounds and bounds.get("w") else ""
    lines = [head + f"{len(boxes)} text elements, top to bottom; (x, y) is the center to tap:"]
    for b in boxes:
        size = ""
        if b.get("w") and b.get("h"):
            size = f", {_point(b['w'])}x{_point(b['h'])}"
        lines.append(f'- "{b["text"]}" at ({_point(b.get("x", 0))}, {_point(b.get("y", 0))}){size}')
    if any(b.get("source") == "pixels" for b in boxes):
        lines.append("Positions come from reading the pixels, so a word may be misread.")
    return "\n".join(lines)


def build_server(*, cloud_for, allow_files: bool, auth=None, token_verifier=None) -> MCPServer:
    """`cloud_for(ctx)` yields the signed-in `Cloud` for one tool call.
    `allow_files` adds the tool that writes to this machine's disk: stdio only."""
    server = MCPServer(name="phone-harness", title="Phone Harness", version=__version__,
                       instructions=INSTRUCTIONS, website_url="https://phone-harness.com",
                       auth=auth, token_verifier=token_verifier)

    def phones(ctx) -> Phones:
        return Phones(cloud_for(ctx))

    # --- phones ---------------------------------------------------------------

    @server.tool(name="phone_list_available_devices", title="List phones", annotations=READ_ONLY)
    async def list_available_devices(ctx: Context) -> str:
        """List the phones running on this account, whose ids the other tools take as `device`, and the phones the account can start."""
        ph = phones(ctx)
        live = await _thread(ph.running)
        me = await _thread(ph.me)
        return _devices_text(live, me)

    @server.tool(name="phone_start_device", title="Start a phone",
                 annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
    async def start_device(ctx: Context,
                           platform: Literal["android", "ios"] | None = None,
                           kind: Literal["emulator", "device"] | None = None,
                           device_id: str | None = None,
                           minutes: int = DEFAULT_MINUTES) -> str:
        """Start a phone and wait until it is ready; returns its device id. With nothing named, the account's default phone (a temporary Android emulator) starts. `platform` and `kind` go together (android/emulator, or ios/device for the account's own iPhone). `device_id` is an id from phone_list_available_devices: a particular iPhone, or the account's saved Android phone (apps and logins kept between sessions). `minutes` (default 30) is how long the session runs. Cloud Android bills 5 cents per minute; a cloud iPhone is a flat monthly subscription ($80/mo, $100/mo with eSIM, or 20% off billed annually at $64/mo, $80/mo with eSIM) and is not billed by the minute. Stop it with phone_stop_device when done."""
        if (platform is None) != (kind is None):
            raise PhoneError("platform and kind go together: android with emulator, or ios with device.")
        ph = phones(ctx)
        body: dict = {"timeout_seconds": max(1, int(minutes)) * 60}
        if device_id:
            if platform is None:
                # The id says which catalog entry is meant; the catalog says what it is.
                catalog = (await _thread(ph.me)).get("available") or []
                entry = next((e for e in catalog if e.get("id") == device_id), None)
                if entry is None:
                    known = ", ".join(str(e["id"]) for e in catalog if e.get("id")) or "none"
                    raise PhoneError(f"No phone with id {device_id!r} on this account; ids are: {known}.")
                platform, kind = entry.get("platform"), entry.get("kind")
            # The API names an iPhone by device_id and the saved Android phone by profile_id.
            body["device_id" if platform == "ios" else "profile_id"] = device_id
        if platform:
            body["platform"] = platform
            body["kind"] = kind
        session = await _thread(ph.start, body)
        sid = session["id"]
        started = time.monotonic()
        shown = None
        while session.get("state") != "ready":
            if session.get("state") in ("error", "closing"):
                raise PhoneError(f"The phone did not start: {session.get('error') or session['state']}")
            elapsed = time.monotonic() - started
            if elapsed >= READY_WAIT:
                try:
                    await _thread(ph.stop, sid)
                except PhoneError:
                    pass
                raise PhoneError(f"Gave up after {READY_WAIT}s waiting for the phone; asked the service to end session {sid}.")
            progress = session.get("progress") or {}
            phase = progress.get("phase") or "provisioning"
            if progress.get("queue_position"):
                phase += f", position {progress['queue_position']} in the queue"
            if phase != shown:
                shown = phase
                try:
                    await ctx.report_progress(elapsed, READY_WAIT, phase)
                except Exception:   # a client without a progress token
                    pass
            await anyio.sleep(2)
            session = await _thread(ph.session, sid)
        screen = session.get("screen")
        size = f" Screen {screen['w']}x{screen['h']}." if isinstance(screen, dict) and screen.get("w") else ""
        left = minutes_left(session)
        ends = f" It ends in {left} min unless phone_stop_device is called first." if left is not None else ""
        return (f"Started {session.get('device') or 'the phone'} as device {sid}.{size}{ends} "
                f"Pass device=\"{sid}\" to the other tools.")

    @server.tool(name="phone_stop_device", title="Stop a phone",
                 annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True))
    async def stop_device(ctx: Context, device: str) -> str:
        """End a running phone session. Stopping a cloud Android session stops its per-minute charge. A cloud iPhone is a monthly subscription and is not billed by the session. Anything not saved on the phone is lost."""
        result = await _thread(phones(ctx).stop, device)
        pending = " Cleanup is still finishing." if result.get("cleanup_pending") else ""
        return f"Stopped device {device}.{pending}"

    # --- the screen -------------------------------------------------------------

    @server.tool(name="phone_get_screen_size", title="Screen size", annotations=READ_ONLY)
    async def get_screen_size(ctx: Context, device: str) -> str:
        """The phone's screen size in the coordinate space the tap and swipe tools use."""
        b = await _thread(phones(ctx).bounds, device)
        return f"Screen is {_point(b['w'])}x{_point(b['h'])}, origin top-left. Taps and swipes take these coordinates."

    @server.tool(name="phone_take_screenshot", title="Take a screenshot", annotations=READ_ONLY, structured_output=False)
    async def take_screenshot(ctx: Context, device: str, max_size: int = screenshots.DEFAULT_MAX_SIZE,
                              scale: float | None = None) -> list:
        """Take a screenshot of the phone. Use it to understand what is on screen when the text list is not enough. The image is usually smaller than the screen; the text before it says how to convert a point you see into screen coordinates. Do not reuse an old screenshot: the screen changes."""
        png, bounds = await _thread(phones(ctx).capture, device)
        data, size, mime = await _thread(screenshots.shrink, png, max_size, scale)
        screen = (_point(bounds["w"]), _point(bounds["h"])) if bounds and bounds.get("w") else screenshots.image_size(png)
        note = screenshots.describe_mapping(size, screen) or f"Screenshot is {size[0]}x{size[1]}."
        return [TextContent(type="text", text=note), Image(data=data, format=mime.split("/")[1]).to_image_content()]

    if allow_files:
        @server.tool(name="phone_save_screenshot", title="Save a screenshot", annotations=TOUCH)
        async def save_screenshot(ctx: Context, device: str, save_to: str, max_size: int | None = None,
                                  scale: float | None = None) -> str:
            """Save a screenshot to a file on this machine: PNG when the name ends in .png, else JPEG. Full size unless `max_size` or `scale` says otherwise."""
            png, _bounds = await _thread(phones(ctx).capture, device)
            path = Path(save_to).expanduser()
            fmt = "PNG" if path.suffix.lower() == ".png" else "JPEG"
            if fmt == "PNG" and not max_size and not scale:
                data, size = png, screenshots.image_size(png)
            else:
                data, size, _mime = await _thread(screenshots.shrink, png, max_size, scale, fmt)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return f"Saved {size[0]}x{size[1]} {fmt} to {path}."

    @server.tool(name="phone_list_elements_on_screen", title="List what is on screen", annotations=READ_ONLY)
    async def list_elements_on_screen(ctx: Context, device: str, format: Literal["text", "json"] = "text") -> str:
        """List the text visible on screen with the center of each piece, ready to tap with phone_click_on_screen_at_coordinates. On Android the positions come from the accessibility tree; on an iPhone from reading the pixels. Call it again after the screen changes."""
        ph = phones(ctx)
        boxes = await _thread(ph.op, device, "screen.text")
        boxes = boxes if isinstance(boxes, list) else []
        if format == "json":
            return json.dumps(boxes)
        bounds = None
        try:
            bounds = await _thread(ph.bounds, device)
        except PhoneError:
            pass
        return _elements_text(boxes, bounds)

    # --- touching it ------------------------------------------------------------

    @server.tool(name="phone_click_on_screen_at_coordinates", title="Tap", annotations=TOUCH)
    async def click_on_screen_at_coordinates(ctx: Context, device: str, x: float, y: float) -> str:
        """Tap the screen at (x, y) in screen coordinates, such as a center from phone_list_elements_on_screen."""
        await _thread(phones(ctx).op, device, "input.tap", x=_point(x), y=_point(y))
        return f"Tapped ({_point(x)}, {_point(y)})."

    @server.tool(name="phone_long_press_on_screen_at_coordinates", title="Long press", annotations=TOUCH)
    async def long_press_on_screen_at_coordinates(ctx: Context, device: str, x: float, y: float,
                                                  duration: int = 800) -> str:
        """Press and hold at (x, y) in screen coordinates for `duration` milliseconds (default 800)."""
        seconds = max(0.1, min(float(duration), 10000.0) / 1000.0)
        await _thread(phones(ctx).op, device, "input.press", x=_point(x), y=_point(y), duration=seconds)
        return f"Long-pressed ({_point(x)}, {_point(y)}) for {int(seconds * 1000)} ms."

    @server.tool(name="phone_swipe_on_screen", title="Swipe", annotations=TOUCH)
    async def swipe_on_screen(ctx: Context, device: str, direction: Literal["up", "down", "left", "right"],
                              x: float | None = None, y: float | None = None,
                              distance: float | None = None) -> str:
        """Swipe in a direction: the way the finger moves, so "up" reveals what is below. Starts at (x, y), or the screen center, and travels `distance` pixels (default 40% of the screen)."""
        ph = phones(ctx)
        w = h = None
        if x is None or y is None or not distance:
            # Only ask the phone its size when the gesture depends on it.
            b = await _thread(ph.bounds, device)
            w, h = float(b["w"]), float(b["h"])
        cx = float(x) if x is not None else w / 2
        cy = float(y) if y is not None else h / 2
        span = float(distance) if distance else 0.4 * (h if direction in ("up", "down") else w)
        dx = {"left": -1, "right": 1}.get(direction, 0) * span
        dy = {"up": -1, "down": 1}.get(direction, 0) * span

        def clamp(v, hi):
            return min(max(v, 0.0), hi - 1) if hi else max(v, 0.0)
        x1, y1 = clamp(cx - dx / 2, w), clamp(cy - dy / 2, h)
        x2, y2 = clamp(cx + dx / 2, w), clamp(cy + dy / 2, h)
        await _thread(ph.op, device, "input.drag", x1=_point(x1), y1=_point(y1), x2=_point(x2), y2=_point(y2),
                      duration=0.3, steps=10)
        return f"Swiped {direction} from ({_point(x1)}, {_point(y1)}) to ({_point(x2)}, {_point(y2)})."

    @server.tool(name="phone_type_keys", title="Type text", annotations=TOUCH)
    async def type_keys(ctx: Context, device: str, text: str, submit: bool = False) -> str:
        """Type text into the focused field. Tap the field first so the keyboard is up; text sent with nothing focused goes nowhere. `submit` presses Enter afterwards."""
        ph = phones(ctx)
        await _thread(ph.op, device, "input.text", s=text)
        if submit:
            await _thread(ph.op, device, "input.keys", combo="enter")
        return f"Typed {len(text)} characters" + (" and pressed Enter." if submit else ".")

    @server.tool(name="phone_press_button", title="Press a button", annotations=TOUCH)
    async def press_button(ctx: Context, device: str, button: Literal["HOME", "BACK", "APP_SWITCHER", "ENTER"]) -> str:
        """Press a system button: HOME, BACK (Android; an iPhone has none), APP_SWITCHER, or ENTER."""
        op, kw = BUTTONS[button]
        try:
            await _thread(phones(ctx).op, device, op, **kw)
        except Unsupported as e:
            raise PhoneError(NO_BACK if button == "BACK" else str(e)) from None
        return f"Pressed {button}."

    # --- waking it, and handing it a link -----------------------------------------

    @server.tool(name="phone_wake", title="Wake the phone", annotations=REPEATABLE)
    async def wake(ctx: Context, device: str) -> str:
        """Wake a cloud iPhone that is asleep, on its lock screen or showing the StandBy clock, and get it past the lock screen. Cloud iPhones are set up with no passcode, so nothing is typed: never ask the user for a code. Call it when a screenshot is black or shows a lock screen or a clock where the Home Screen should be, or when a tap, typing or an app launch is refused because the phone is locked. It does nothing to a phone that is already awake. Then read the screen again: the phone may be on its Home Screen. An Android emulator has no such control."""
        ph = phones(ctx)
        try:
            result = await _thread(ph.op, device, "device.wake")
        except Unsupported:
            raise PhoneError(_cannot(await _thread(ph.platform, device), *NO_WAKE)) from None
        return _woke(result)

    @server.tool(name="phone_open_url", title="Open a link", annotations=TOUCH)
    async def open_url(ctx: Context, device: str, url: str) -> str:
        """Open an https link on the phone, in its browser (Safari on an iPhone). The link arrives whole, with no typing and no clipboard, so use it for a long link or a sign-in link. `url` must start with https:// and have no spaces, at most 4096 characters. The phone has to be awake: call phone_wake first if it is locked. Cloud iPhones only; on an Android emulator, launch the browser and type the address."""
        link = checked_url(url)
        ph = phones(ctx)
        try:
            await _thread(ph.op, device, "url.open", url=link)
        except Unsupported:
            raise PhoneError(_cannot(await _thread(ph.platform, device), *NO_LINK)) from None
        return "Opened the link in the phone's browser. Give the page a moment to load, then read the screen."

    @server.tool(name="phone_set_orientation", title="Turn the screen", annotations=REPEATABLE)
    async def set_orientation(ctx: Context, device: str, orientation: Orientation) -> str:
        """Turn a cloud iPhone to portrait, landscape_left or landscape_right, as if it had been turned in the hand. The phone lies flat in a rack, so this is the only way its screen turns. The two landscapes are the two ways round. An app that supports only one way up stays that way. The screen size and every coordinate change with the turn: the result gives the new size, and anything read before it is stale, so take a new screenshot or list the elements again before the next tap. An Android emulator cannot be turned from here."""
        ph = phones(ctx)
        try:
            result = await _thread(ph.op, device, "device.rotate", to=orientation)
        except Unsupported:
            raise PhoneError(_cannot(await _thread(ph.platform, device), *NO_TURN)) from None
        size = _size(result) or _size(result.get("bounds") if isinstance(result, dict) else None)
        if size is None:
            try:
                size = _size(await _thread(ph.bounds, device))
            except PhoneError:
                size = None         # the turn happened; only its size is unknown
        return _turned(orientation, result, size)

    # --- apps -------------------------------------------------------------------

    @server.tool(name="phone_list_apps", title="List apps", annotations=READ_ONLY)
    async def list_apps(ctx: Context, device: str) -> str:
        """List the apps installed on the phone: package ids on Android, bundle ids on an iPhone. Any of them can be passed to phone_launch_app."""
        try:
            apps = await _thread(phones(ctx).op, device, "apps.list", include_system=False)
        except Unsupported:
            raise PhoneError(NO_APP_LIST) from None
        apps = [str(a) for a in apps] if isinstance(apps, list) else []
        return "\n".join(apps) if apps else "No user apps are installed."

    @server.tool(name="phone_launch_app", title="Launch an app", annotations=TOUCH)
    async def launch_app(ctx: Context, device: str, name: str) -> str:
        """Open an app by the name on its icon ("Settings", "Notes", "Chrome") or, on Android, its package id."""
        opened = await _thread(phones(ctx).op, device, "apps.launch", name=name)
        return f"Launched {opened or name}."

    @server.tool(name="phone_get_foreground_app", title="Foreground app", annotations=READ_ONLY)
    async def get_foreground_app(ctx: Context, device: str) -> str:
        """Which app is in front right now. Not every phone reports it; then take a screenshot instead."""
        try:
            current = await _thread(phones(ctx).op, device, "apps.current")
        except Unsupported:
            raise PhoneError(NO_FOREGROUND) from None
        except PhoneError as e:
            if "unavailable" in str(e):
                raise PhoneError(NO_FOREGROUND) from None
            raise
        return f"Foreground app: {current}" if current else "No app is in the foreground; the phone is on its home screen."

    # --- reserving phones the account does not have yet -------------------------

    register_reservations(server, cloud_for)

    return server
