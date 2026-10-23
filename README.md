# Phone Harness MCP

**[Phone Harness](https://phone-harness.com)** · let your agent drive a real phone in the cloud.

A [Model Context Protocol](https://modelcontextprotocol.io) server for
**Phone Harness Cloud**: a real iPhone or an Android, reached over HTTPS. The
agent sees the screen, taps, types, swipes, opens apps and reads the result.
Nothing to pair, no adb, no Mac required, any OS.

Tool names follow [mobile-mcp](https://github.com/mobile-next/mobile-mcp)
(`phone_take_screenshot`, `phone_click_on_screen_at_coordinates`, …), so an
agent that has driven a phone before already knows how.

Use it two ways:

- **Run it yourself** with `uvx phone-harness-mcp` over stdio. It signs in as
  you: an API key, or the sign-in `phone-harness cloud login` left on the machine.
- **Hosted** at `https://mcp.phone-harness.com/mcp`. Nothing to install; your
  client sends your API key as a bearer token.

## Requirements

- A Phone Harness Cloud account, and an API key from the
  [dashboard](https://phone-harness.com/dashboard) (or the
  [phone-harness CLI](https://github.com/ShawnPana/phone-harness) signed in with
  `phone-harness cloud login`).
- Python 3.10+ with [uv](https://docs.astral.sh/uv/) for the local server. The
  hosted server needs nothing.

## Getting started

Most clients take this **standard config**:

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

[<img src="https://img.shields.io/badge/VS_Code-Install-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white" alt="Install in VS Code">](https://vscode.dev/redirect/mcp/install?name=phone-harness&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22phone-harness-mcp%22%5D%7D)
[<img src="https://img.shields.io/badge/VS_Code_Insiders-Install-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white" alt="Install in VS Code Insiders">](https://insiders.vscode.dev/redirect/mcp/install?name=phone-harness&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22phone-harness-mcp%22%5D%7D&quality=insiders)
[<img src="https://img.shields.io/badge/Cursor-Install-000000?style=flat-square&logo=cursor" alt="Install in Cursor">](cursor://anysphere.cursor-deeplink/mcp/install?name=phone-harness&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwaG9uZS1oYXJuZXNzLW1jcCJdfQ==)

<details><summary><b>Claude Code</b></summary>

Local:

```bash
claude mcp add phone-harness -e PHONE_HARNESS_API_KEY=pck_… -- uvx phone-harness-mcp
```

Hosted:

```bash
claude mcp add --transport http phone-harness https://mcp.phone-harness.com/mcp \
  --header "Authorization: Bearer pck_…"
```
</details>

<details><summary><b>Claude Desktop</b></summary>

Settings → Connectors → Add custom connector, URL `https://mcp.phone-harness.com/mcp`.
Or add the standard config to `claude_desktop_config.json` to run it locally.
</details>

<details><summary><b>Codex</b></summary>

```bash
codex mcp add phone-harness --env PHONE_HARNESS_API_KEY=pck_… -- uvx phone-harness-mcp
```

Or in `~/.codex/config.toml`, hosted:

```toml
[mcp_servers.phone-harness]
url = "https://mcp.phone-harness.com/mcp"
bearer_token_env_var = "PHONE_HARNESS_API_KEY"
```
</details>

<details><summary><b>Cursor</b></summary>

Click the Cursor badge above, or Settings → MCP → Add new MCP Server and paste
the standard config into `~/.cursor/mcp.json`. For the hosted server:

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
</details>

<details><summary><b>VS Code</b></summary>

Click a VS Code badge above, or:

```bash
code --add-mcp '{"name":"phone-harness","command":"uvx","args":["phone-harness-mcp"],"env":{"PHONE_HARNESS_API_KEY":"pck_…"}}'
```
</details>

<details><summary><b>Gemini CLI</b></summary>

Add the standard config under `mcpServers` in `~/.gemini/settings.json`.
</details>

<details><summary><b>Copilot CLI</b></summary>

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
</details>

<details><summary><b>Windsurf</b></summary>

Add the standard config to `~/.codeium/windsurf/mcp_config.json`.
</details>

<details><summary><b>Goose</b></summary>

Advanced settings → Extensions → Add custom extension, type STDIO, command
`uvx phone-harness-mcp`, environment variable `PHONE_HARNESS_API_KEY`.
</details>

<details><summary><b>Kiro</b></summary>

Add the standard config to `.kiro/settings/mcp.json`.
</details>

<details><summary><b>LM Studio</b></summary>

Program → Install → Edit mcp.json, paste the standard config.
</details>

<details><summary><b>opencode</b></summary>

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
</details>

<details><summary><b>Warp</b></summary>

Settings → AI → Manage MCP Servers → + Add, paste the standard config.
</details>

<details><summary><b>Any other client</b></summary>

Local: command `uvx`, arguments `phone-harness-mcp`, environment `PHONE_HARNESS_API_KEY`.
Hosted: streamable HTTP at `https://mcp.phone-harness.com/mcp` with header
`Authorization: Bearer <API key>`.
</details>

## What the agent does with it

```
● phone_list_available_devices        → no phone running; an iPhone and an Android are available
● phone_start_device                  → s_7f3a: Android emulator, 390x844, 30 min
● phone_launch_app name="Weather"
● phone_list_elements_on_screen       → "San Francisco" at (195, 212) …
● phone_take_screenshot               → forecast is up
● phone_stop_device
```

## Tools

Every tool but the first two takes `device`, an id from `phone_list_available_devices`.

| Tool | Does |
| --- | --- |
| `phone_list_available_devices` | Running phones and their ids, plus the phones the account can start. |
| `phone_start_device` | Start a phone and wait until it is ready. `platform` + `kind` (`android`/`emulator`, `ios`/`device`), `device_id` for a particular iPhone, `minutes` (default 30). Rented phones bill by the minute. |
| `phone_stop_device` | End the session; billing stops. |
| `phone_get_screen_size` | The coordinate space taps use. |
| `phone_take_screenshot` | JPEG, long side ≤ `max_size` (1024) or `scale`, preceded by a line that converts image points to screen coordinates. |
| `phone_save_screenshot` | Write a PNG or JPEG to a path. Local server only. |
| `phone_list_elements_on_screen` | Visible text with tap-ready centers (`text` or `json`). Accessibility tree on Android, text recognition on iPhone. |
| `phone_click_on_screen_at_coordinates` | Tap `x, y`. |
| `phone_long_press_on_screen_at_coordinates` | Hold for `duration` ms (800). |
| `phone_swipe_on_screen` | `direction` is the finger's motion; from `x, y` or the center, `distance` px or 40% of the screen. |
| `phone_type_keys` | Type into the focused field; `submit` presses Enter. |
| `phone_press_button` | `HOME`, `BACK` (Android), `APP_SWITCHER`, `ENTER`. |
| `phone_list_apps` | Installed apps (Android). |
| `phone_launch_app` | Open an app by name or package id. |
| `phone_get_foreground_app` | Which app is in front. |

Not yet: opening a URL (`url.open` is not served by the session route today),
installing apps, clipboard, screen recording.

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
  --stateful                HTTP mode: one MCP session per client
```

## Standalone and hosted server

`--port` serves streamable HTTP at `/mcp`:

```bash
uvx phone-harness-mcp --port 8931
```

In this mode the server **holds no credential**. Every request must carry
`Authorization: Bearer <API key>`, which is forwarded unchanged to Phone
Harness Cloud, so one process can serve many accounts and leaks none. A
request without a bearer gets a 401 with a `WWW-Authenticate` challenge.

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
`mcp.phone-harness.com` runs exactly this binary.

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
- Phones are rented by the minute. The `phone_start_device` description says
  so; turn on your client's tool-approval setting if you want to confirm it.

Report vulnerabilities per [SECURITY.md](SECURITY.md).

## Development

```bash
uv sync
uv run pytest            # a fake Phone Harness Cloud API; no account needed
uv run phone-harness-mcp --help
```

Layout: `src/phone_harness_mcp/{cli,server,phone,cloud,screenshots,auth}.py`.
`cloud.py` is the HTTP client, `phone.py` turns API answers into sentences for
the model, `server.py` is the tool set, `auth.py` is hosted-mode bearer handling.

## License

MIT
