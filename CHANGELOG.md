# Changelog

## Unreleased

Screenshot links, handoffs and the saved Android phone (PHO-224).

- **Compatibility:** `phone_start_device` with nothing named starts the
  account's saved Android phone, or returns the session already holding it
  (also after a 409 `profile_running`, and it waits while the phone is still
  being saved), instead of a temporary emulator. This changes what every
  client gets by default. `temporary: true` asks for the previous behavior.
- `phone_get_screenshot_url` (Cloud Android): an HTTPS link to one
  screenshot, 10 minutes by default and 15 at most; no image bytes in the
  result and no account key in the link.
- `phone_start_handoff`, `phone_get_handoff`, `phone_end_handoff` (Cloud
  Android): a one-time, revocable link that hands the phone to a person for a
  few minutes, on a page that shows the screen live; its state; taking it back.
- OAuth mode names `scope="phone:drive"` in its 401 challenge, so a client
  that registers itself asks for it.
- The new tools return `structuredContent` with an output schema.

## 0.1.0 (2026-10-23)

First release: cloud phones only.

- Fifteen `phone_*` tools shaped after mobile-mcp: list and start and stop
  phones, screen size, screenshot (JPEG, scaled, with a coordinate mapping
  line), save screenshot (stdio only), list on-screen text, tap, long press,
  swipe, type, system buttons, list and launch apps, foreground app.
- stdio mode signs in with `PHONE_HARNESS_API_KEY`, `--api-key`, or the
  sign-in left by `phone-harness cloud login` (refreshed when it expires).
- `--port` serves stateless streamable HTTP at `/mcp`; every request must
  carry its own `Authorization: Bearer`, forwarded to the API. `--oauth-issuer`
  publishes protected-resource metadata and verifies tokens through the API.
- A phone busy with another op (409) is retried for up to 12 s; a 503 from the
  phone's input service once after 2 s.
- `app.py` and `vercel.json`: the hosted server as a Vercel Function, configured
  by environment variables.
- `--oauth-scope` (default `phone:drive`): the scope the protected-resource
  metadata advertises; the verifier credits it to every bearer the API accepts.
