"""The hosted server as a Vercel Function.

Vercel runs the ASGI app in `app.py` at the repo root, which imports `app`
from here. Configuration is environment variables, because a function has no
command line:

    PHONE_HARNESS_MCP_ALLOWED_HOSTS   Host header values to accept; default "*",
                                      since Vercel's edge owns TLS and routing and
                                      the function is not reachable by raw IP
    PHONE_HARNESS_MCP_PUBLIC_URL      the URL clients use, e.g.
                                      https://mcp.phone-harness.com/mcp (OAuth only)
    PHONE_HARNESS_MCP_OAUTH_ISSUER    switches OAuth on; unset means API keys only
    PHONE_HARNESS_MCP_OAUTH_SCOPE     the scope advertised to OAuth clients; default phone:drive
    PHONE_HARNESS_CLOUD_API           the API; default https://api.phone-harness.com
    PHONE_HARNESS_MCP_DRIVE_PATH      a second endpoint with no reservation tools and
                                      no prices, for ChatGPT's plugin directory, whose
                                      rules bar a plugin from selling; default /chatgpt
    PHONE_HARNESS_MCP_OPENAI_CHALLENGE  the token OpenAI's plugin dashboard asks for at
                                      /.well-known/openai-apps-challenge to check who
                                      owns this hostname; unset means no such page

Requests are stateless and answered as JSON, so any instance can take any
request. `phone_start_device` waits up to four minutes for a phone, which
fits inside the function's five-minute maximum duration (vercel.json).
"""
from __future__ import annotations

import os

from .cli import DEFAULT_OAUTH_SCOPE, build_http_app
from .cloud import api_url


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _well_known() -> dict[str, str]:
    token = _env("PHONE_HARNESS_MCP_OPENAI_CHALLENGE")
    return {"openai-apps-challenge": token} if token else {}


app = build_http_app(
    api=api_url(),
    mcp_path="/mcp",
    host="0.0.0.0",
    allowed_hosts=_env("PHONE_HARNESS_MCP_ALLOWED_HOSTS", "*"),
    allowed_origins=_env("PHONE_HARNESS_MCP_ALLOWED_ORIGINS"),
    public_url=_env("PHONE_HARNESS_MCP_PUBLIC_URL"),
    oauth_issuer=_env("PHONE_HARNESS_MCP_OAUTH_ISSUER"),
    oauth_scope=_env("PHONE_HARNESS_MCP_OAUTH_SCOPE", DEFAULT_OAUTH_SCOPE),
    stateful=False,
    drive_path=_env("PHONE_HARNESS_MCP_DRIVE_PATH", "/chatgpt"),
    well_known=_well_known(),
)
