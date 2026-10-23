# Security

Report a vulnerability to support@phone-harness.com. Please do not open a
public issue for it. You will hear back within three business days.

## What this server is

A thin HTTPS client of Phone Harness Cloud. It never holds a phone, never
runs a shell, and keeps no state between tool calls.

- **stdio**: the server signs in as the machine it runs on (an API key from the
  environment or `--api-key`, else the sign-in left by `phone-harness cloud
  login`). Like any local MCP server, it is not a security boundary between
  the agent and that account; the agent can do what the account can do.
- **HTTP (`--port`)**: the server holds no credential of its own. Each request
  must carry `Authorization: Bearer …`, which is forwarded unchanged to the
  API; a request without one is answered 401. The process's own environment
  is never consulted, so a hosted server cannot leak the operator's account.
- **OAuth (`--oauth-issuer`)**: the server is a resource server only. It
  publishes protected-resource metadata and asks the API whether a token is
  good; it never mints, stores or decodes tokens.

## Hosting it

Bind to loopback or pass `--allowed-hosts <public hostname>` so the Host
header is checked (DNS-rebinding protection). Terminate TLS in front of it.
Screenshots and on-screen text are personal data; the server does not log
them, and your proxy should not log request bodies.
