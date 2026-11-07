# Changelog

## Unreleased

Out of the box in chat agents (PHO-224, Keiki iMessage agents first).

- `phone_get_screenshot_url`: a short-lived HTTPS link to one screenshot, for
  agents that send a person the screen rather than read image bytes.
- `phone_start_handoff`, `phone_get_handoff`, `phone_end_handoff`: a one-time,
  revocable link that hands the phone to a person for a few minutes, with a
  Keiki iMessage app card (`presentation`) in the structured result.
- `phone_start_device` with nothing named starts the account's saved Android
  phone, or returns the session already running it (also after a 409
  `profile_running`, and it waits while the phone is still being saved);
  `temporary: true` is the old throwaway emulator.
- OAuth mode names the scope (`scope="phone:drive"`) in its 401 challenge, so a
  client that registers itself asks for it.
- The new tools return `structuredContent` with an output schema.
- `examples/keiki`: a Keiki agent (type-checked against `@onkeiki/agents`
  0.1.7) and a Keiki-style OAuth client the tests sign in with.

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
