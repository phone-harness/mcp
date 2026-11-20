# Changelog

## Unreleased

- Three tools to reserve a phone the account does not have yet:
  `phone_reserve`, `phone_list_reservations` and `phone_cancel_reservation`.
  `phone_reserve` returns the API's quote (the refundable deposit due now, and
  the plan's first billing period at the intro and standard prices) and a
  Stripe Checkout link. A person opens the link and pays; no tool pays, and
  nothing is reserved until the deposit is paid. Each call carries an
  `Idempotency-Key`, so a call repeated with the same key is the same
  reservation. Cancelling refunds the deposit still held and is refused once
  every phone has been handed over. Both stdio and hosted mode register them.
  They need a Phone Harness Cloud API that has the reservation routes.

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
