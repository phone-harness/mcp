# Phone Harness in a Keiki iMessage agent

A [Keiki](https://onkeiki.com) agent that uses each person's own Phone Harness
cloud phone over iMessage. A person texts the agent; the first time, it sends
them a sign-in link for their Phone Harness account; from then on the agent
starts their saved Android phone, drives it, sends them screenshots, and hands
them the phone when they have to type a password or a code themselves.

Nothing is shared between people: each signs in to their own account, and the
deployer's account is never used.

## Setup

You need a Keiki account (invite-only at the time of writing), an org-wide Keiki
API key, and Bun. Each person who texts the agent needs a Phone Harness account.

```sh
cd examples/keiki
bun install
cp .env.example .env      # set KEIKI_API_KEY
bun run sync
```

Then assign a line to the agent in the Keiki dashboard. Turn on **Dynamic UI**
(the iMessage Apps add-on) if you want handoff links to arrive as app cards.

The part that connects Phone Harness is one entry in the agent config
([src/index.ts](src/index.ts)):

```ts
mcp: {
  enabled: true,
  userConnections: [{
    kind: 'mcp',
    name: 'phone_harness',            // ^[a-z][a-z0-9_]*$: no hyphens
    description: "Use each person's own Phone Harness cloud phone.",
    endpoint: 'https://mcp.phone-harness.com/mcp',
  }],
},
sandbox: { enabled: true, persistent: true },   // code mode runs here
```

There is no `oauth` block and no secret to sync. The server publishes OAuth
metadata naming its authorization server, which supports dynamic client
registration, so Keiki registers itself (callback
`https://onkeiki.com/api/webapp/user-oauth/callback`). Keiki's config has no
scope field; the server's 401 challenge and metadata both name `phone:drive`,
which is the scope the sign-in asks the person to approve. A token with that
scope can drive the person's phones; it cannot mint API keys, pay, or erase a
phone.

The prompt ([src/prompt.ts](src/prompt.ts)) tells the agent how to use the
tools below. Keep its rules if you write your own.

## What the agent does with it

| The person says | The agent calls | What the person gets |
| --- | --- | --- |
| anything, the first time | `connect_account` for `phone_harness` | a sign-in link for their Phone Harness account |
| "book me a table on…" | `phone_start_device` with nothing named | their saved Android phone, with their apps and logins; if it is already running, that same session |
| "show me" | `phone_get_screenshot_url`, then `download_url` and `send_file` | the screenshot as a picture in the thread |
| (a password, a code, a CAPTCHA) | `phone_start_handoff`, then the card or the link | a page that shows the phone and takes their taps and typing |
| "done" | `phone_get_handoff`, then a screenshot | the agent carries on from where they left it |
| | `phone_stop_device` | the phone is saved and the per-minute charge stops |

### Screenshots as links

In code mode the worker reads tool results as data, and image bytes are of no
use to it (and too big for a result). `phone_get_screenshot_url` answers with
text and `structuredContent`:

```json
{ "url": "https://api.phone-harness.com/screenshots/3f2a…​.png?token=phs_…",
  "expires_at": 1793000000.5, "content_type": "image/png",
  "width": 1080, "height": 2400, "screen": { "w": 1080, "h": 2400 } }
```

The link shows only that picture, works for 10 minutes by default (`minutes`,
at most 15), keeps working if the phone is stopped, and carries no account key.
Download it and send the file; send the link itself only where files cannot be
attached.

### Handing the phone over

`phone_start_handoff` answers with the link and a ready iMessage app card:

```json
{ "handoff_id": "8c41…", "url": "https://api.phone-harness.com/handoff/8c41…?token=phl_…",
  "expires_at": 1793000600.0, "state": "waiting",
  "presentation": { "type": "app", "id": "phone-handoff-8c41…", "url": "https://api.phone-harness.com/handoff/8c41…?token=phl_…",
                    "caption": "Take over the phone", "subcaption": "Works once, for 10 min" } }
```

`presentation` has the shape of Keiki's `ToolAppPresentation` (`type`, `id`,
`url`, `caption`, `subcaption`). Send it as the card when Dynamic UI is on
(`imessage_send_app` with its `url` works too); otherwise send the `url` as its
own message. The page works in Safari and in Keiki Vault, needs no sign-in, and
ignores the `#kv=` fragment Keiki adds.

The link works once: the first browser to open it holds control, and anyone
opening it after that is turned away. It works for 10 minutes by default
(`minutes`, at most 30, never past the session), on that phone only. The person
taps **Done** to hand the phone back; `phone_get_handoff` then reports
`ended`. `phone_end_handoff` takes it back early, and a new handoff turns off
the old link. While the person has the phone the agent should not drive it.

### What a link can and cannot do

- Neither link carries an API key or a sign-in. A screenshot link opens one
  picture; a handoff link opens the handoff page for one phone and nothing else.
- A forwarded screenshot link shows that one picture until it expires.
- A forwarded handoff link is the case to think about: whoever opens it first
  holds the phone for the rest of its few minutes. The person who should have
  it sees "already opened"; ask for a new link, which turns the old one off at
  once. Send handoff links only in a direct conversation with the phone's
  owner, never in a group.
- Someone holding a handoff can tap, swipe, type, and press Home and Back, as on
  their own phone. They cannot list or launch apps by name, read files, end the
  session, or reach another phone.
- A link preview fetching the page does not use the link up; only opening it
  does.

## Testing without Keiki

Keiki's signup is invite-only, so the repository tests the same path with a
stand-in: [keiki_style_client.py](keiki_style_client.py) connects the way Keiki
does (the 401 challenge, protected-resource metadata, dynamic registration with
Keiki's callback, authorization code with PKCE and the `resource` parameter),
and `tests/test_keiki.py` runs it, and the SDK's own OAuth client, against the
server and a stand-in authorization server:

```sh
uv run pytest tests/test_keiki.py
```

`tsc --noEmit` against `@onkeiki/agents` 0.1.7 type-checks `src/`.
