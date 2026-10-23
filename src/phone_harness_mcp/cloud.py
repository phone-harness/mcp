"""Phone Harness Cloud over HTTPS: the one thing this server talks to.

The MCP server owns no phone. Every tool call becomes one or two requests to
api.phone-harness.com under the caller's own credential, and the API runs the
op on the phone the session holds. That keeps this process stateless, and it
means a bug here can never reach a phone it was not handed a key for.

Two kinds of credential:

- a bearer the caller hands us: an API key (`pck_…`), or whatever the HTTP
  client sent in `Authorization` when this server is hosted;
- the sign-in `phone-harness cloud login` left on this machine, reused so a
  developer who already has the CLI set up gets the MCP for free. Its access
  token is refreshed through the CLI's own public OAuth client when it
  expires.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__

API_URL = "https://api.phone-harness.com"
OAUTH_ISSUER = "https://clerk.phone-harness.com"
# The "phone-harness CLI" OAuth app. A public client, so its id is not a
# secret; its refresh tokens are what `phone-harness cloud login` stores.
OAUTH_CLIENT_ID = "Fu2QHJcGewhL7uKh"
USER_AGENT = f"phone-harness-mcp/{__version__}"
SIGN_IN_HELP = ("Not signed in to Phone Harness Cloud. Set PHONE_HARNESS_API_KEY to an API key from "
                "https://phone-harness.com/dashboard, or run `phone-harness cloud login`.")


def api_url() -> str:
    return (os.environ.get("PHONE_HARNESS_CLOUD_API") or API_URL).rstrip("/")


class CloudError(Exception):
    """The API answered with an error status. `body` is its JSON, or {'error': text}."""

    def __init__(self, status: int, body, headers=None):
        self.status = status
        self.body = body if isinstance(body, dict) else {"error": str(body)}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        super().__init__(self.error)

    @property
    def error(self) -> str:
        return str(self.body.get("error") or f"the API answered {self.status}")

    @property
    def code(self):
        return self.body.get("code")

    @property
    def unsupported(self) -> bool:
        return bool(self.body.get("unsupported"))

    @property
    def retry_after(self) -> float | None:
        value = self.body.get("retry_after_seconds")
        if value is None:
            value = self.headers.get("retry-after")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


class Unreachable(Exception):
    """No answer from the API at all."""


class NotSignedIn(Exception):
    """This machine has nothing to sign in with."""


class StaticToken:
    """A bearer somebody handed us. Nothing to refresh."""

    def __init__(self, token: str):
        self._token = token

    def token(self) -> str:
        return self._token

    def refreshed(self):
        return None


class CliLogin:
    """The sign-in `phone-harness cloud login` left in the CLI's config dir."""

    def __init__(self, config_dir: Path | None = None, issuer: str | None = None, client_id: str | None = None):
        self.config_dir = config_dir or cli_config_dir()
        self.issuer = (issuer or os.environ.get("PHONE_HARNESS_CLOUD_OAUTH_ISSUER") or OAUTH_ISSUER).rstrip("/")
        self.client_id = client_id or os.environ.get("PHONE_HARNESS_CLOUD_OAUTH_CLIENT_ID") or OAUTH_CLIENT_ID

    @property
    def path(self) -> Path:
        return self.config_dir / "auth.json"

    def _record(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def token(self) -> str:
        record = self._record()
        token = record.get("access_token")
        if token and time.time() < (record.get("expires_at") or 0) - 60:
            return token
        fresh = self.refreshed()
        if fresh:
            return fresh
        raise NotSignedIn(SIGN_IN_HELP)

    def refreshed(self):
        """A fresh access token from the refresh token on disk, or None when the sign-in is gone."""
        record = self._record()
        if not record.get("refresh_token"):
            return None
        form = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": self.client_id,
                                       "refresh_token": record["refresh_token"]}).encode()
        req = urllib.request.Request(f"{self.issuer}/oauth/token", data=form, method="POST",
                                     headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tok = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, ValueError, OSError):
            return None
        if not isinstance(tok, dict) or not tok.get("access_token"):
            return None
        record.update({"access_token": tok["access_token"],
                       # a refresh may or may not rotate the refresh token
                       "refresh_token": tok.get("refresh_token") or record.get("refresh_token"),
                       "expires_at": time.time() + int(tok.get("expires_in") or 3600)})
        self._write(record)
        return tok["access_token"]

    def _write(self, record):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass    # a token we could not save still works for this process


def cli_config_dir() -> Path:
    home = os.environ.get("PHONE_HARNESS_HOME")
    if home:
        return Path(home) / "config"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "phone-harness"


def local_credentials(api_key: str | None = None):
    """What this machine can sign in with: an explicit key, the environment, else the CLI's login."""
    key = api_key or os.environ.get("PHONE_HARNESS_API_KEY")
    if key and key.strip():
        return StaticToken(key.strip())
    return CliLogin()


class Cloud:
    """A signed-in HTTP client for the API. Synchronous; the server calls it from a worker thread."""

    def __init__(self, credentials, url: str | None = None, timeout: float = 40):
        self.credentials = credentials
        self.url = (url or api_url()).rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, timeout: float | None = None) -> dict:
        token = self.credentials.token()
        try:
            return self._send(method, path, body, token, timeout)
        except CloudError as e:
            # A token can be refused before its stated expiry (clock skew, a
            # revoked grant): one refresh, one retry, then it is a 401.
            if e.status != 401:
                raise
            fresh = self.credentials.refreshed()
            if not fresh:
                raise
            return self._send(method, path, body, fresh, timeout)

    def _send(self, method, path, body, token, timeout):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"error": raw.decode(errors="replace")[:200] or f"HTTP {e.code}"}
            raise CloudError(e.code, parsed, dict(e.headers)) from None
        except urllib.error.URLError as e:
            raise Unreachable(f"could not reach {self.url}: {e.reason}") from None
        except OSError as e:
            raise Unreachable(f"could not reach {self.url}: {e}") from None
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise CloudError(502, {"error": "the API answered with something that is not JSON"}) from None
