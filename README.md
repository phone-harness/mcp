# Phone Harness MCP 🔌

**Give any MCP agent a real iPhone or an Android emulator in the cloud.**

Add Phone Harness as a custom connector in Claude, ChatGPT, Cursor, Codex, or any MCP client, and your agent can see the screen, tap, type, swipe, and open apps on a real iPhone or an Android emulator.

Nothing to install. Paste one URL, sign in, and go:

    https://mcp.phone-harness.com/mcp

- **Works in any MCP client.** Claude, ChatGPT, Cursor, Codex, VS Code, and more.
- **Nothing to set up.** No Mac, no adb, no pairing. Just a URL and a sign-in.
- **Real iPhones.** Dedicated cloud iPhones, and Android emulators, driven the way a person uses them.

Sign-in is OAuth and asks for the `phone:drive` scope.

## Add as a custom connector

### Claude

On claude.ai, open **Customize → Connectors** and add a custom connector (**Add → Custom → Web**, or **Add custom connector**). Name it Phone Harness, paste `https://mcp.phone-harness.com/mcp`, choose sign-in rather than No sign in, and add it. On Team and Enterprise, an Owner adds it under **Organization settings → Connectors**; members then connect.

In Claude Code:

```bash
claude mcp add --transport http phone-harness https://mcp.phone-harness.com/mcp
```

Then run `/mcp` and sign in. That saves the server for the current project; add `--scope user` to keep it in every project.

### ChatGPT

On ChatGPT web, open **Plugins → + → Add custom MCP server**. Name it, paste `https://mcp.phone-harness.com/mcp`, choose sign-in, acknowledge the warning, and select **Create as a plugin**. Install the plugin, then @ mention it in a chat.

Custom MCP servers on ChatGPT web are for Business, Enterprise, and Edu workspaces. An admin creates the plugin. Pro accounts can connect a custom server for read and fetch tools only, so driving the phone needs Business, Enterprise, or Edu.

### Cursor

Add this to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project), then restart Cursor. Cursor signs in with OAuth when the server asks.

```json
{
  "mcpServers": {
    "phone-harness": {
      "url": "https://mcp.phone-harness.com/mcp"
    }
  }
}
```

### Codex

```bash
codex mcp add phone-harness --url https://mcp.phone-harness.com/mcp
codex mcp login phone-harness
```

In the Codex IDE extension (gear menu → MCP servers) or the ChatGPT desktop app (Settings → MCP servers): **Add server → Streamable HTTP**, paste the URL, save, restart, and select **Authenticate**.

## Tools

The hosted server registers these tools. Every tool except the first two takes `device`, an id from `phone_list_available_devices`.

Cloud Android is pay as you go at 5¢/min. A cloud iPhone is a flat monthly subscription ($80/mo, $100/mo with eSIM) and is not billed by the minute. `minutes` on `phone_start_device` is how long the session runs (default 30), not a billing unit.

| Tool | Does |
| --- | --- |
| `phone_list_available_devices` | Running phones and their ids, the phones the account can start, and the account balance when the API reports one. |
| `phone_start_device` | Start a phone and wait until it is ready. With nothing named, a temporary Android emulator starts. `platform` and `kind` go together (`android`/`emulator`, or `ios`/`device` for the account's iPhone). `device_id` picks a particular iPhone or a saved Android phone (apps and logins kept). |
| `phone_stop_device` | End the session. Anything not saved on the phone is lost. Stopping cloud Android stops the per-minute charge. |
| `phone_get_screen_size` | Screen size in the coordinate space taps and swipes use. |
| `phone_take_screenshot` | JPEG of the screen. Long side at most `max_size` (default 1024), or `scale`. The text before the image converts a point in the image to screen coordinates. |
| `phone_list_elements_on_screen` | Visible text with tap-ready centers (`text` or `json`). Accessibility tree on Android; text recognition on iPhone. |
| `phone_click_on_screen_at_coordinates` | Tap `x`, `y`. |
| `phone_long_press_on_screen_at_coordinates` | Press and hold `x`, `y` for `duration` milliseconds (default 800, clamped to 100–10000). |
| `phone_swipe_on_screen` | Swipe. `direction` is the way the finger moves (`up` reveals what is below). From `x`, `y` or the center, for `distance` pixels or 40% of the screen. |
| `phone_type_keys` | Type `text` into the focused field. `submit` presses Enter. |
| `phone_press_button` | `HOME`, `BACK` (Android only), `APP_SWITCHER`, or `ENTER`. |
| `phone_list_apps` | Installed apps: package ids on Android, bundle ids on an iPhone, when the phone can list them. |
| `phone_launch_app` | Open an app by the name on its icon, or on Android by package id. |
| `phone_get_foreground_app` | Which app is in front, when the phone reports it. |

There is no tool that opens a URL. A local stdio server also registers `phone_save_screenshot` (write a PNG or JPEG on that machine). Tool names follow [mobile-mcp](https://github.com/mobile-next/mobile-mcp).

## Run it yourself

`uvx phone-harness-mcp`, self-hosting, Vercel, Docker, and configuration are in [docs/self-hosting.md](docs/self-hosting.md).

## Development

```bash
uv sync
uv run pytest
uv run phone-harness-mcp --help
```

Report vulnerabilities per [SECURITY.md](SECURITY.md).

## License

MIT
