# Self-hosting and local stdio

The hosted server is [https://mcp.phone-harness.com/mcp](https://mcp.phone-harness.com/mcp). Adding it as a custom connector is in the [README](../README.md). This page is for running the same server yourself.

The server does not hold a phone. Every tool call goes to Phone Harness Cloud over HTTPS. An iPhone there is a real device (`ios` / `device`). Android is an emulator (`android` / `emulator`): a temporary one by default, or a saved phone that keeps apps and logins.

Cloud Android emulators are free to start, then 5¢ per phone-minute, pay as you go. A Cloud iPhone is a flat monthly subscription, not billed by the minute: $80/mo, or $100/mo with an eSIM, with 20% off billed annually ($64/mo, or $80/mo with an eSIM). See [phone-harness.com/pricing](https://phone-harness.com/pricing) for current rates.

## Requirements

- A Phone Harness Cloud account, and an API key from the
  [dashboard](https://phone-harness.com/dashboard) (or the
  [phone-harness CLI](https://github.com/ShawnPana/phone-harness) signed in with
  `phone-harness cloud login`).
- Python 3.10+ with [uv](https://docs.astral.sh/uv/) to run it on a machine.
  The hosted server needs nothing installed.

## Local stdio

```json
{
  "mcpServers": {
    "phone-harness": {
      "command": "uvx",
      "args": ["phone-harness-mcp"],
      "env": { "PHONE_HARNESS_API_KEY": "pck_…" }
    }
  }
}
```

Leave out `env` if the machine is already signed in with `phone-harness cloud login`.

Over stdio the server also registers `phone_save_screenshot`. It writes a PNG when the path ends in `.png`, otherwise a JPEG, to `save_to` on that machine. Full size unless `max_size` or `scale` is set. The hosted server does not register this tool.

There is no tool that opens a URL, installs an app, reads the clipboard, or records the screen.

[<img src="https://img.shields.io/badge/VS_Code-Install-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white" alt="Install in VS Code">](https://vscode.dev/redirect/mcp/install?name=phone-harness&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22phone-harness-mcp%22%5D%7D)
[<img src="https://img.shields.io/badge/VS_Code_Insiders-Install-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white" alt="Install in VS Code Insiders">](https://insiders.vscode.dev/redirect/mcp/install?name=phone-harness&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22phone-harness-mcp%22%5D%7D&quality=insiders)
[<img src="https://img.shields.io/badge/Cursor-Install-000000?style=flat-square&logo=cursor" alt="Install in Cursor">](cursor://anysphere.cursor-deeplink/mcp/install?name=phone-harness&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwaG9uZS1oYXJuZXNzLW1jcCJdfQ==)

### Claude Code

```bash
claude mcp add phone-harness -e PHONE_HARNESS_API_KEY=pck_… -- uvx phone-harness-mcp
```

Hosted, with an API key instead of the OAuth sign-in in the README:

```bash
claude mcp add --transport http phone-harness https://mcp.phone-harness.com/mcp \
  --header "Authorization: Bearer pck_…"
```

### Claude Desktop

Add the standard config above to `claude_desktop_config.json`.

### Codex

```bash
codex mcp add phone-harness --env PHONE_HARNESS_API_KEY=pck_… -- uvx phone-harness-mcp
```

Or in `~/.codex/config.toml`, hosted with an API key:

```toml
[mcp_servers.phone-harness]
url = "https://mcp.phone-harness.com/mcp"
bearer_token_env_var = "PHONE_HARNESS_API_KEY"
```

### Cursor

Click the Cursor badge above, or add the standard config to `~/.cursor/mcp.json`. For the hosted server with an API key:

```json
{
  "mcpServers": {
    "phone-harness": {
      "url": "https://mcp.phone-harness.com/mcp",
      "headers": { "Authorization": "Bearer pck_…" }
    }
  }
}
```

### VS Code

Click a VS Code badge above, or:

```bash
code --add-mcp '{"name":"phone-harness","command":"uvx","args":["phone-harness-mcp"],"env":{"PHONE_HARNESS_API_KEY":"pck_…"}}'
```

### Gemini CLI

Add the standard config under `mcpServers` in `~/.gemini/settings.json`.

### Copilot CLI

Run `/mcp add`, or put this in `~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "phone-harness": {
      "type": "local",
      "command": "uvx",
      "args": ["phone-harness-mcp"],
      "env": { "PHONE_HARNESS_API_KEY": "pck_…" },
      "tools": ["*"]
    }
  }
}
```

### Windsurf

Add the standard config to `~/.codeium/windsurf/mcp_config.json`.

### Kiro

Add the standard config to `.kiro/settings/mcp.json`.

### opencode

```json
{
  "mcp": {
    "phone-harness": {
      "type": "local",
      "command": ["uvx", "phone-harness-mcp"],
      "environment": { "PHONE_HARNESS_API_KEY": "pck_…" },
      "enabled": true
    }
  }
}
```

### Warp

Settings → AI → Manage MCP Servers → + Add, paste the standard config.

### Any other client

Local: command `uvx`, arguments `phone-harness-mcp`, environment `PHONE_HARNESS_API_KEY`.
Hosted: streamable HTTP at `https://mcp.phone-harness.com/mcp` with header
`Authorization: Bearer <API key>`, or OAuth as in the README.

## Configuration

```
phone-harness-mcp [OPTIONS]

  --api-key KEY             API key. Default $PHONE_HARNESS_API_KEY, else the CLI's sign-in. stdio only.
  --api-url URL             Default $PHONE_HARNESS_CLOUD_API or https://api.phone-harness.com
  --port PORT               Serve streamable HTTP instead of stdio (see below)
  --host HOST               Interface for --port  [default: 127.0.0.1]
  --path PATH               MCP endpoint path  [default: /mcp]
  --allowed-hosts HOSTS     Host header values to accept; '*' disables the check
  --allowed-origins ORIGINS Origin header values to accept
  --public-url URL          The URL clients reach the server at (with --oauth-issuer)
  --oauth-issuer URL        Publish OAuth protected-resource metadata for this issuer
  --oauth-scope SCOPE       The scope advertised to OAuth clients  [default: phone:drive]
  --stateful                HTTP mode: one MCP session per client
```

## HTTP server

`--port` serves streamable HTTP at `/mcp`:

```bash
uvx phone-harness-mcp --port 8931
```

In this mode the server holds no credential. Every request must carry
`Authorization: Bearer …`, an API key or an OAuth access token, which is
forwarded unchanged to Phone Harness Cloud, so one process can serve many
accounts and leaks none. A request without a bearer gets a 401 with a
`WWW-Authenticate` challenge. `phone_save_screenshot` is not registered.

With `--oauth-issuer` the server also publishes OAuth protected-resource
metadata, so clients that support OAuth discover the authorization server,
register themselves, and sign the user in through a consent screen that asks
for the `phone:drive` scope. `mcp.phone-harness.com` is this server deployed
on Vercel with that OAuth mode on, and Clerk as the authorization server.

```json
{
  "mcpServers": {
    "phone-harness": {
      "url": "http://localhost:8931/mcp",
      "headers": { "Authorization": "Bearer pck_…" }
    }
  }
}
```

Behind a reverse proxy pass the public hostname so the Host header is
checked (DNS-rebinding protection): `--host 0.0.0.0 --allowed-hosts mcp.example.com`.

### Vercel

The repo is also a Vercel project: `app.py` exposes the same server as one
Python function, stateless, answering JSON. Import the repo in Vercel and add
the domain. `mcp.phone-harness.com` runs this way.

| Environment variable | Default | Means |
| --- | --- | --- |
| `PHONE_HARNESS_CLOUD_API` | `https://api.phone-harness.com` | The API to forward to. |
| `PHONE_HARNESS_MCP_ALLOWED_HOSTS` | `*` | Host header check; off on Vercel, whose edge owns routing. |
| `PHONE_HARNESS_MCP_ALLOWED_ORIGINS` | unset | Origin header values to accept. Unused while allowed hosts is `*`. |
| `PHONE_HARNESS_MCP_OAUTH_ISSUER` | unset | Set to the OAuth authorization server to publish protected-resource metadata. Unset means API keys only. |
| `PHONE_HARNESS_MCP_PUBLIC_URL` | unset | The URL clients use, required with the issuer. |
| `PHONE_HARNESS_MCP_OAUTH_SCOPE` | `phone:drive` | The scope advertised to OAuth clients. |

`vercel.json` sets `maxDuration` to 300 s so `phone_start_device` can wait
for a phone to boot. The server itself gives up after 240 s and asks the
service to end that session.

### Docker

```bash
docker build -t phone-harness-mcp .
docker run --rm -p 8000:8000 phone-harness-mcp --port 8000 --host 0.0.0.0 --allowed-hosts '*'
```

## Security

- The server is a thin HTTPS client. It never holds a phone, and over HTTP it
  never holds a credential: each request's bearer is the only identity.
- Over stdio it uses this machine's sign-in, like any local MCP server; it is
  not a security boundary between you and your own account.
- Screenshots and text read from the phone pass through unlogged. API keys
  are never written to logs.
- Cloud Android emulators are free to start, then 5¢ per phone-minute, pay as
  you go. A Cloud iPhone is a flat monthly subscription, not billed by the
  minute: $80/mo, or $100/mo with an eSIM, with 20% off billed annually
  ($64/mo, or $80/mo with an eSIM). See
  [phone-harness.com/pricing](https://phone-harness.com/pricing) for current
  rates. The `phone_start_device` description says so; turn on your client's
  tool-approval setting if you want to confirm a start.

Report vulnerabilities per [SECURITY.md](../SECURITY.md).

## Development

```bash
uv sync
uv run pytest            # a fake Phone Harness Cloud API; no account needed
uv run phone-harness-mcp --help
```

Layout: `src/phone_harness_mcp/{cli,server,phone,cloud,screenshots,auth,vercel}.py`.
`cloud.py` is the HTTP client, `phone.py` turns API answers into sentences for
the model, `server.py` is the tool set, `auth.py` is hosted-mode bearer handling.
