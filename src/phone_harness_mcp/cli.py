"""`phone-harness-mcp`: stdio for a local client, `--port` for a hosted server.

    uvx phone-harness-mcp                      # stdio; signs in as this machine
    phone-harness-mcp --port 8000              # streamable HTTP at /mcp; each
                                               # request brings its own bearer

Over stdio the server uses this machine's credential: PHONE_HARNESS_API_KEY,
`--api-key`, or the sign-in from `phone-harness cloud login`. Over HTTP it
uses none of those, ever: the bearer on each request is forwarded to Phone
Harness Cloud, so one process can serve many accounts and leaks none.
"""
from __future__ import annotations

import sys

import click

from . import __version__
from .cloud import API_URL, api_url, local_credentials
from .server import build_server, hosted_cloud, stdio_cloud


def _security(host: str, allowed_hosts: str | None, allowed_origins: str | None):
    """Host-header policy for HTTP mode (DNS-rebinding protection), or None for the SDK's default."""
    from mcp.server.transport_security import TransportSecuritySettings
    if allowed_hosts is None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            click.echo(f"phone-harness-mcp: listening on {host} without --allowed-hosts; the Host header is not "
                       "checked. Behind a reverse proxy pass --allowed-hosts <public hostname>.", err=True)
        return None
    hosts = [h.strip() for h in allowed_hosts.split(",") if h.strip()]
    if hosts == ["*"]:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [o.strip() for o in (allowed_origins or "").split(",") if o.strip()]
    if not origins:
        for h in hosts:
            base = h[:-2] if h.endswith(":*") else h
            origins += [f"https://{base}", f"http://{base}"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=origins)


DEFAULT_OAUTH_SCOPE = "phone:drive"


RESOURCE_METADATA = "/.well-known/oauth-protected-resource"


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _both(first, second, owned: tuple[str, ...], lifespans):
    """One ASGI app out of two: requests under an `owned` path go to `second`,
    everything else to `first`. Each MCP app starts its session manager in its
    own lifespan, so both lifespans run for as long as this app does."""
    import contextlib

    from starlette.applications import Starlette
    from starlette.routing import Mount

    async def dispatch(scope, receive, send):
        path = scope.get("path", "")
        await (second if any(_under(path, p) for p in owned) else first)(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            for app in lifespans:
                await stack.enter_async_context(app.router.lifespan_context(app))
            yield

    return Starlette(routes=[Mount("/", app=dispatch)], lifespan=lifespan)


def build_http_app(*, api: str | None, mcp_path: str, host: str, allowed_hosts: str | None,
                   allowed_origins: str | None, public_url: str | None, oauth_issuer: str | None,
                   stateful: bool, oauth_scope: str | None = DEFAULT_OAUTH_SCOPE,
                   drive_path: str | None = None, well_known: dict[str, str] | None = None):
    """The ASGI app for `--port`. Separate from `main` so tests can serve it.

    `drive_path` adds a second MCP endpoint over the same phones that has no
    reservation tools and names no price, for a directory whose rules bar a
    plugin from selling (ChatGPT's). It is its own OAuth protected resource.
    `well_known` maps a name to the plain text served at /.well-known/<name>,
    which is how a directory checks who owns this hostname."""
    from urllib.parse import urlsplit

    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse

    if oauth_issuer and not public_url:
        raise click.UsageError("--oauth-issuer needs --public-url, the URL clients reach this server at.")
    mcp_path = "/" + mcp_path.strip("/")
    if drive_path:
        drive_path = "/" + drive_path.strip("/")
        if _under(drive_path, mcp_path) or _under(mcp_path, drive_path):
            raise click.UsageError("--drive-path and --path must not sit inside one another.")

    def surface(path: str, url: str | None, reservations: bool):
        """One MCP endpoint: the Starlette app (it owns the lifespan) and the app to serve."""
        auth = token_verifier = None
        if oauth_issuer:
            from mcp.server.auth.settings import AuthSettings
            from .auth import CloudTokenVerifier
            # The scope is advertised in the protected-resource metadata, so a client
            # asks the authorization server for it; the API refuses a dynamic client's
            # token without it, and the verifier reports it for every bearer the API
            # accepts, API keys included.
            scopes = [oauth_scope] if oauth_scope else []
            auth = AuthSettings(issuer_url=oauth_issuer, resource_server_url=url,
                                validate_token_resource=False, required_scopes=scopes)
            token_verifier = CloudTokenVerifier(api, scopes=scopes)
        server = build_server(cloud_for=hosted_cloud(api), allow_files=False, auth=auth,
                              token_verifier=token_verifier, reservations=reservations)
        if reservations:   # the main endpoint also carries the pages that are not MCP
            @server.custom_route("/health", methods=["GET"])
            async def health(request: Request):
                return JSONResponse({"ok": True, "version": __version__})

            @server.custom_route("/", methods=["GET"])
            async def index(request: Request):
                return JSONResponse({"name": "phone-harness", "version": __version__, "mcp": mcp_path,
                                     "docs": "https://github.com/phone-harness/mcp"})

            for name, text in (well_known or {}).items():
                async def proof(request: Request, text=text):
                    return PlainTextResponse(text)
                server.custom_route(f"/.well-known/{name}", methods=["GET"])(proof)

        app = server.streamable_http_app(streamable_http_path=path, json_response=True, stateless_http=not stateful,
                                         transport_security=_security(host, allowed_hosts, allowed_origins),
                                         host=host)
        if oauth_issuer:
            return app, app
        from .auth import RequireBearer
        return app, RequireBearer(app, path)

    main_app, main = surface(mcp_path, public_url, True)
    if not drive_path:
        return main
    drive_url = urlsplit(public_url)._replace(path=drive_path).geturl() if public_url else None
    drive_app, drive = surface(drive_path, drive_url, False)
    return _both(main, drive, (drive_path, RESOURCE_METADATA + drive_path), (main_app, drive_app))


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="phone-harness-mcp")
@click.option("--api-key", metavar="KEY",
              help="Phone Harness API key (pck_…). Default: $PHONE_HARNESS_API_KEY, else the sign-in from "
                   "`phone-harness cloud login`. stdio mode only.")
@click.option("--api-url", metavar="URL", default=None,
              help=f"Phone Harness Cloud API. Default: $PHONE_HARNESS_CLOUD_API or {API_URL}.")
@click.option("--port", type=int, metavar="PORT",
              help="Serve streamable HTTP on this port instead of stdio. Every request must then carry its own "
                   "Authorization: Bearer header; this machine's credential is not used.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface to bind in HTTP mode.")
@click.option("--path", "mcp_path", default="/mcp", show_default=True, help="URL path of the MCP endpoint.")
@click.option("--allowed-hosts", metavar="HOSTS",
              help="Host header values to accept, comma separated (host, host:port or host:*); '*' disables the "
                   "check. Default: loopback only when bound to loopback.")
@click.option("--allowed-origins", metavar="ORIGINS", help="Origin header values to accept, comma separated.")
@click.option("--public-url", metavar="URL", help="The URL clients reach this server at, e.g. "
              "https://mcp.phone-harness.com/mcp. Required with --oauth-issuer.")
@click.option("--oauth-issuer", metavar="URL",
              help="Publish OAuth protected-resource metadata naming this authorization server, and accept its "
                   "tokens (verified through the API). Without it, HTTP mode takes API keys only.")
@click.option("--oauth-scope", metavar="SCOPE", default=DEFAULT_OAUTH_SCOPE, show_default=True,
              help="The scope the protected-resource metadata advertises, which OAuth clients then request.")
@click.option("--drive-path", metavar="PATH", default=None,
              help="HTTP mode: a second MCP endpoint over the same phones with no reservation tools and no "
                   "prices, for a directory whose rules bar a plugin from selling. Off by default.")
@click.option("--stateful", is_flag=True, help="HTTP mode: keep one MCP session per client instead of answering "
              "each request on its own.")
def main(api_key, api_url, port, host, mcp_path, allowed_hosts, allowed_origins, public_url, oauth_issuer,
         oauth_scope, drive_path, stateful):
    """MCP server for Phone Harness Cloud: drive a real iPhone or an Android emulator from any MCP client."""
    api = (api_url or globals()["api_url"]()).rstrip("/")
    if port is None:
        server = build_server(cloud_for=stdio_cloud(local_credentials(api_key), api), allow_files=True)
        server.run("stdio")
        return
    if api_key:
        click.echo("phone-harness-mcp: --api-key is ignored with --port; each request carries its own bearer.", err=True)
    import uvicorn
    app = build_http_app(api=api, mcp_path=mcp_path, host=host, allowed_hosts=allowed_hosts,
                         allowed_origins=allowed_origins, public_url=public_url, oauth_issuer=oauth_issuer,
                         stateful=stateful, oauth_scope=oauth_scope, drive_path=drive_path)
    click.echo(f"phone-harness-mcp {__version__}: http://{host}:{port}{mcp_path} -> {api}", err=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
