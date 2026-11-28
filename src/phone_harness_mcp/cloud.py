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

A request acts in one organization. Every account is an organization of one,
and the API uses the caller's own unless a request names another the caller is
in, with `X-Phone-Harness-Org`. A `Cloud` made with `org` names it on every
request, and one made without sends no such header. An API key is bound to
its organization by the API and needs none.
"""
from __future__ import annotations

import json
import os
import re
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
# The header that names the organization a request acts in, and an
# organization's id as the API writes one.
ORG_HEADER = "X-Phone-Harness-Org"
ORG_ID = re.compile(r"org_[0-9a-f]{24}")


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


class NotAnOrganization(Exception):
    """What was given as the organization to act in is not one organization's id."""


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


def checked_org(value: str, where: str) -> str:
    """`value`, if it is an organization's id: the check made before one is
    forwarded, so that what the API is sent is an id or nothing. The sentence
    names `where` the value came from and never the value itself, which may
    be an API key put in the wrong place."""
    if not ORG_ID.fullmatch(value):
        raise NotAnOrganization(f"{where} must be an organization id: org_ followed by 24 lowercase hex "
                                "characters. Leave it out to act in this sign-in's own organization.")
    return value


def local_org() -> str | None:
    """The organization this machine acts in: PHONE_HARNESS_ORG, or None, its
    sign-in's own, when that is unset or empty."""
    value = (os.environ.get("PHONE_HARNESS_ORG") or "").strip()
    return checked_org(value, "PHONE_HARNESS_ORG") if value else None


class Cloud:
    """A signed-in HTTP client for the API. Synchronous; the server calls it from a worker thread."""

    def __init__(self, credentials, url: str | None = None, timeout: float = 40, *, org: str | None = None):
        self.credentials = credentials
        self.url = (url or api_url()).rstrip("/")
        self.timeout = timeout
        # The organization every request names, already checked; None names
        # none. It is only ever handed in, never read from the environment
        # here, so a client made without one sends no such header.
        self.org = org

    def request(self, method: str, path: str, body=None, timeout: float | None = None,
                headers: dict | None = None) -> dict:
        """`headers` adds to a request (an `Idempotency-Key`); it never replaces the bearer or the organization."""
        token = self.credentials.token()
        try:
            return self._send(method, path, body, token, timeout, headers)
        except CloudError as e:
            # A token can be refused before its stated expiry (clock skew, a
            # revoked grant): one refresh, one retry, then it is a 401.
            if e.status != 401:
                raise
            fresh = self.credentials.refreshed()
            if not fresh:
                raise
            return self._send(method, path, body, fresh, timeout, headers)

    def _send(self, method, path, body, token, timeout, extra=None):
        headers = {**(extra or {}), "Authorization": f"Bearer {token}", "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        if self.org:
            headers[ORG_HEADER] = self.org
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
