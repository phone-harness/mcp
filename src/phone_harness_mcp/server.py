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

INSTRUCTIONS = """Drive a Phone Harness Cloud phone: a real iPhone or an Android, reached over HTTPS.

Start with phone_list_available_devices. If a phone is running, pass its id as `device` to every other tool. If none is, phone_start_device starts one; a rented phone bills the account by the minute, so call phone_stop_device when the task is done.

To find something to tap, prefer phone_list_elements_on_screen (visible text with tap-ready centers) over a screenshot; take a screenshot to understand layout, or when there is no text. Coordinates are screen pixels; the screenshot tool says how to convert from the image it returns. After a tap or typing, read the screen again rather than assuming it worked."""

READ_ONLY = ToolAnnotations(read_only_hint=True)
TOUCH = ToolAnnotations(read_only_hint=False, destructive_hint=False)

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
    balance = me.get("balance_cents")
    if isinstance(balance, (int, float)) and me.get("billing_mode") != "disabled":
        line = f"Balance: ${balance / 100:.2f}"
        price = me.get("price_cents_per_minute")
        if isinstance(price, (int, float)) and price:
            line += f" at ${price / 100:.2f} per minute"
        lines.extend(["", line])
    return "\n".join(lines)


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
        """Start a phone and wait until it is ready; returns its device id. With nothing named, the account's default phone (a temporary Android emulator) starts. `platform` and `kind` go together (android/emulator, or ios/device for the account's own iPhone). `device_id` is an id from phone_list_available_devices: a particular iPhone, or the account's saved Android phone (apps and logins kept between sessions). A rented phone bills the account by the minute for up to `minutes` (default 30), so stop it with phone_stop_device when done."""
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
        """End a running phone session so it stops billing. Anything not saved on the phone is lost."""
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

    return server
