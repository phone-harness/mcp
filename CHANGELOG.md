# Changelog

## Unreleased

- `phone_set_orientation` turns a cloud iPhone to `portrait`, `landscape_left`
  or `landscape_right`. A cloud iPhone lies flat in a rack, so this is the
  only way its screen turns. The result gives the screen's new size, measured
  from a fresh screenshot, and says that everything read before the turn is
  stale. When the app in front supports only one way up, the result says the
  screen did not turn with the phone. On an Android emulator, and against an
  API or a phone host that does not have the op yet, it says the screen stays
  as it is.
- Two tools for a cloud iPhone: `phone_wake` and `phone_open_url`.
  `phone_wake` wakes a phone that is asleep, on its lock screen or showing the
  StandBy clock, and gets it past the lock screen. Cloud iPhones are set up
  with no passcode, so nothing is typed. `phone_open_url` opens an https link
  in the phone's browser whole, with no typing; anything that is not one line
  of https, at most 4096 characters, is refused before it is sent, and the
  link is never repeated in a result or an error.
  A tap, typing, an app launch or a link refused because the iPhone is locked
  now says to call `phone_wake`. On an Android emulator, and against an API or
  a phone host that does not have these yet, each tool says what to do instead.
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
