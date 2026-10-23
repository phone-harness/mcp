"""Sessions and ops: the API's vocabulary, in the words the tools use.

A "device" to the tools is a session id at the API. Every op goes to
`POST /sessions/{id}/op`, and the API's own executor runs it on whichever
phone the session holds, an Android emulator or a real iPhone, so this module
knows nothing about either. What a phone cannot do comes back as `Unsupported`.

Errors are translated here, once, into sentences a model can act on.
"""
from __future__ import annotations

import base64
import time

from mcp.server.mcpserver.exceptions import ToolError

from .cloud import Cloud, CloudError, NotSignedIn, Unreachable

READY_WAIT = 240        # seconds to wait for a starting phone before giving it back, as the CLI does
BUSY_WAIT = 12          # seconds of retrying a 409 "busy" before the model is told
DEFAULT_MINUTES = 30


class PhoneError(ToolError):
    """A message for the model: what went wrong and what to do about it.

    A `ToolError`, so the SDK hands the sentence to the model as an `is_error`
    result instead of hiding it behind "Error executing tool"."""


class Unsupported(PhoneError):
    """This phone cannot do that, and retrying will not help."""


def minutes_left(session: dict) -> int | None:
    expires = session.get("expires_at")
    if not expires:
        return None
    try:
        return max(0, int((float(expires) - time.time()) // 60))
    except (TypeError, ValueError):
        return None


def describe(session: dict) -> str:
    """One line for a running phone: id, what it is, state, screen, time left."""
    what = session.get("device") or f"{session.get('platform') or ''} {session.get('kind') or ''}".strip() or "phone"
    bits = [f"{session['id']}: {what} ({session.get('platform')}/{session.get('kind')})", str(session.get("state", "?"))]
    screen = session.get("screen")
    if isinstance(screen, dict) and screen.get("w") and screen.get("h"):
        bits.append(f"screen {screen['w']}x{screen['h']}")
    left = minutes_left(session)
    if left is not None:
        bits.append(f"{left} min left")
    return ", ".join(bits)


class Phones:
    """The account's phones, as seen through one signed-in `Cloud`."""

    def __init__(self, cloud: Cloud):
        self.cloud = cloud

    # --- the account ---------------------------------------------------------

    def me(self) -> dict:
        return self._call("GET", "/me")

    def running(self) -> list[dict]:
        live = self._call("GET", "/sessions")
        if isinstance(live, dict):
            live = live.get("sessions") or []
        return [s for s in live if isinstance(s, dict) and s.get("id")]

    # --- sessions -------------------------------------------------------------

    def start(self, body: dict) -> dict:
        return self._call("POST", "/sessions", body)

    def session(self, sid: str) -> dict:
        return self._call("GET", f"/sessions/{sid}", sid=sid)

    def stop(self, sid: str) -> dict:
        return self._call("DELETE", f"/sessions/{sid}", sid=sid)

    # --- ops ------------------------------------------------------------------

    def ops(self, sid: str) -> list[str]:
        return list(self._call("GET", f"/sessions/{sid}/ops", sid=sid).get("ops") or [])

    def op(self, sid: str, op: str, /, **kw):
        """Run one op. A phone busy with another op is retried for a while
        first: one op at a time is the API's contract, not a fault."""
        deadline = time.monotonic() + BUSY_WAIT
        retried_outage = False
        while True:
            try:
                return self.cloud.request("POST", f"/sessions/{sid}/op", {"op": op, "kw": kw}, timeout=90)["result"]
            except CloudError as e:
                if e.status == 409 and e.retry_after is not None and time.monotonic() < deadline:
                    time.sleep(min(max(e.retry_after, 0.2), 2))
                    continue
                if e.status == 503 and not retried_outage:
                    retried_outage = True        # the phone's input service is restarting; once more
                    time.sleep(2)
                    continue
                raise self._translate(e, sid) from None
            except (NotSignedIn, Unreachable) as e:
                raise PhoneError(str(e)) from None

    def capture(self, sid: str) -> tuple[bytes, dict | None]:
        """The screen as PNG bytes, and the phone's coordinate bounds."""
        result = self.op(sid, "screen.capture")
        if not isinstance(result, dict) or not result.get("png_b64"):
            raise PhoneError("the phone returned no screenshot")
        try:
            png = base64.b64decode(result["png_b64"])
        except (ValueError, TypeError):
            raise PhoneError("the phone returned a screenshot that could not be decoded") from None
        bounds = result.get("bounds")
        return png, bounds if isinstance(bounds, dict) else None

    def bounds(self, sid: str) -> dict:
        result = self.op(sid, "screen.bounds")
        if not isinstance(result, dict) or not result.get("w") or not result.get("h"):
            raise PhoneError("the phone did not report its screen size")
        return result

    # --- errors ---------------------------------------------------------------

    def _call(self, method, path, body=None, sid=None):
        try:
            return self.cloud.request(method, path, body)
        except CloudError as e:
            raise self._translate(e, sid) from None
        except (NotSignedIn, Unreachable) as e:
            raise PhoneError(str(e)) from None

    @staticmethod
    def _translate(e: CloudError, sid: str | None) -> PhoneError:
        if e.status == 401:
            return PhoneError(f"Phone Harness Cloud refused the credential ({e.error}). Check the API key, or run "
                              "`phone-harness cloud login` again.")
        if e.status == 404 and sid:
            return PhoneError(f"device {sid} is not running: it ended, or it is not this account's. "
                              "phone_list_available_devices shows what is running.")
        if e.status == 400 and e.unsupported:
            return Unsupported(e.error)
        if e.status in (400, 403, 404, 409, 410, 413):
            text = e.error
            if e.code and e.code not in text:
                text = f"{text} ({e.code})"
            return PhoneError(text)
        return PhoneError(f"Phone Harness Cloud answered {e.status}: {e.error}")
