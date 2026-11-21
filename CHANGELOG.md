# Changelog

## Unreleased

- The coordinate note under `phone_take_screenshot` follows a screen that is
  on its side. A landscape screenshot was described as covering the portrait
  size the session started with, so taps worked out from it missed.
- Three tools to reserve an iPhone the account does not have yet:
  `phone_reserve`, `phone_list_reservations` and `phone_cancel_reservation`.
  Reservations are for iPhones only (with or without eSIM): `phone_type` is
  `iphone` or `iphone_esim`, and a request for an Android is refused before
  anything is sent.
  `phone_reserve` returns the API's quote (the refundable deposit due now, and
  the plan's first billing period at the intro and standard prices) and a
  Stripe Checkout link. A person opens the link and pays; no tool pays, and
  nothing is reserved until the deposit is paid. `quantity` defaults to 1, and
  a name is optional. Each call carries an `Idempotency-Key`, so a call
  repeated with the same key is the same reservation.
  `phone_cancel_reservation` needs `confirm` set to true; without it nothing
  is cancelled and the answer says what would be given up and refunded. A
  cancel refunds the deposit still held and is refused once every phone has
  been handed over. Both stdio and hosted mode register the three. Against an
  API with no reservation routes yet, each says so and changes nothing.

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
