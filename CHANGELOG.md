# Changelog

## 0.1.0 (unreleased)

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
