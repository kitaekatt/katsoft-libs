# llm-kit relay: opt-in and the hub's OAuth sign-in

This document covers two parts of `llm_kit.relay` (katsoft-llm-kit 1.16.0 and
later): the local opt-in that every relay and hub listener needs, and the
single-user OAuth 2.1 sign-in the hub serves so that the claude.ai custom
connector can connect. The wire protocol between relays, the hub and clients
is `llm-kit-relay-protocol.md`; this document does not change it.

Code:

- `packages/katsoft-llm-kit/src/llm_kit/relay/config.py`: the opt-in flags and
  `hub.public_url`.
- `packages/katsoft-llm-kit/src/llm_kit/relay/hub_oauth.py`: the authorization
  server, the token store and the client metadata fetcher.
- `packages/katsoft-llm-kit/src/llm_kit/relay/hub.py`: the `/mcp` gate
  (`BearerGate`) and `HubServer`.

Tests: `tests/llm-kit/test_relay_hub_oauth.py`, and the opt-in tests in
`test_relay_config.py`, `test_relay_server.py`, `test_relay_hub.py` and
`test_relay_cli.py`.

## 1. Opt-in

Nothing in the library listens unless the machine's own settings file turns it
on. The file is `relay.yaml` at `$XDG_CONFIG_HOME/llm-kit/relay.yaml` (an
absolute `XDG_CONFIG_HOME` only), else `~/.config/llm-kit/relay.yaml`. The
`--config` option of `llm-kit relay` names another file.

```yaml
enabled: true            # the relay: llm-kit relay serve
hub:
  enabled: true          # the hub: llm-kit relay hub (needs enabled: true too)
  public_url: https://hub.example.ts.net   # optional: turns on the OAuth sign-in
```

Rules:

- A missing file, or a missing key, means off. The relay needs `enabled: true`.
  The hub needs `enabled: true` and `hub: {enabled: true}`.
- A flag that is not a YAML boolean, an unknown key, or a file that does not
  parse is a configuration error. It is never read as off or as on.
- No environment variable and no command-line flag turns either one on.
- `RelayServer` and `HubServer` check the file when they are constructed and
  again when they start, so a library caller cannot skip it, and turning a
  flag off before a start takes effect. The check comes before any state is
  created: no state directory, token or registry file is written for a refused
  start.
- `llm-kit relay serve` and `llm-kit relay hub` exit 2 with a configuration
  error that names the file and the missing key.

The contract the llm-kit plugin calls (all in `llm_kit.relay.config`):

| Name | Meaning |
|---|---|
| `relay_enabled(config_path=None, *, environ=None) -> bool` | `enabled: true` in the file |
| `hub_enabled(config_path=None, *, environ=None) -> bool` | `enabled: true` and `hub.enabled: true` |
| `enablement(config_path=None, *, environ=None) -> Enablement` | both flags and the file path |
| `require_enabled(role, config_path) -> None` | raises `RelayNotEnabled` (a `RelayConfigError`) naming the file and key |

With no `config_path` the helpers read the convention path above. They raise
`RelayConfigError` on a malformed file.

Not gated, because they open no listener: `llm-kit relay client <verb>` (it
calls a relay that is already running, and needs that relay's token file),
`llm-kit relay status`, and `llm-kit relay pause|resume|status --offline`
(they read or write `pause.json` in the state directory).

## 2. When the sign-in is on

The hub serves the sign-in only when it has a public URL: `hub.public_url` in
`relay.yaml`, or `llm-kit relay hub --public-url https://<host>` (the option
wins). The value must be `https://<host>[:port]` with no path, query, fragment
or user info; it is normalized to a lower-case origin without the default
port. Without a public URL the hub accepts its bearer only.

The public URL names the origin that a tunnel (for example Tailscale Funnel on
port 443) serves the hub at. It changes no bind: the hub still listens on
`127.0.0.1:4711` by default and refuses any non-loopback address. The library
starts no tunnel.

Every URL the hub publishes is built from the public URL, never from a request
header. The canonical MCP URL, which is the connector URL and the access
token audience, is `<public URL>/mcp`.

## 3. Endpoints

| Request | Purpose |
|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` (and without `/mcp`) | Protected resource metadata (RFC 9728) |
| `GET /.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414) |
| `GET /.well-known/openid-configuration` | The same document |
| `POST /register` | Dynamic client registration (RFC 7591), JSON |
| `GET /authorize` | The consent page |
| `POST /authorize` | The owner's decision |
| `POST /token` | `authorization_code` and `refresh_token` grants, form-encoded |

Protected resource metadata: `resource` is the canonical MCP URL,
`authorization_servers` is `[<public URL>]`, `bearer_methods_supported` is
`["header"]`.

Authorization server metadata: `issuer` is the public URL without a trailing
slash; absolute `authorization_endpoint`, `token_endpoint` and
`registration_endpoint`; `response_types_supported` `["code"]`;
`grant_types_supported` `["authorization_code", "refresh_token"]`;
`token_endpoint_auth_methods_supported` `["none"]`;
`code_challenge_methods_supported` `["S256"]`;
`client_id_metadata_document_supported` `true`;
`authorization_response_iss_parameter_supported` `true`.

Every response carries `Cache-Control: no-store`. A request body over 16 KiB
is refused with HTTP 413. A repeated parameter is `invalid_request`.

## 4. The /mcp gate

- A request needs `Authorization: Bearer <token>`. The token is the hub bearer
  (`hub.bearer` in the state directory, compared in constant time) or a live
  access token this hub issued for the canonical MCP URL. Both are served as
  the same origin, `claude.ai/<hub user>`; the hub calls its relays with its
  own relay token and hub bearer either way.
- Without one, the answer is HTTP 401 with
  `WWW-Authenticate: Bearer resource_metadata="<public URL>/.well-known/oauth-protected-resource/mcp"`
  and the protocol's `unauthenticated` envelope. A token that is presented but
  unknown, expired or for another resource adds `error="invalid_token"`.
- The `Host` header must name loopback (a literal address or `localhost`, any
  port) or the public URL's host; anything else is HTTP 421. The MCP
  transport's own check also accepts those hosts, and the `Origin` values
  `https://claude.ai`, `https://claude.com` and the public URL.

## 5. Clients

Every client is public: it has no secret, and PKCE `S256` is required. Its
redirect URI must be exactly `https://claude.ai/api/mcp/auth_callback` or
`https://claude.com/api/mcp/auth_callback`.

- Client ID Metadata Document: a `client_id` that is an https URL with a path
  (no user info, fragment or dot segments). The hub checks the redirect URI
  first, then fetches the document: the host is resolved once and every
  address must be public; the connection goes to the checked address with
  certificate checks against the host; only HTTP 200 is used (a redirect is
  refused, not followed); the body is capped at 64 KiB and the whole fetch at
  5 seconds; the body must be a JSON object. Its `client_id` must equal the
  URL and its `redirect_uris` must hold the requested redirect URI. The hub
  logs every `client_id` it sees. Documents are not cached.
- Dynamic registration: `redirect_uris` must be a non-empty list of the
  callbacks above (else `invalid_redirect_uri`); `grant_types` and
  `response_types`, when given, must be drawn from those the hub supports
  (else `invalid_client_metadata`). A client that asks for a secret-based
  `token_endpoint_auth_method` is registered as `none`, and the response says
  so. The hub keeps at most 32 registered clients; the oldest goes first.

## 6. Approval

`GET /authorize` validates the request in this order. Until the client and
its redirect URI are known, a refusal is an HTML page and never a redirect.
After that, a refusal goes back to the redirect URI with `error`, `state` and
`iss`: `unsupported_response_type` unless `response_type=code`,
`invalid_request` without an S256 `code_challenge`, and `invalid_target`
unless `resource` is the canonical MCP URL (compared without case in the
scheme and host, without the default port and without a trailing slash).

The consent page names the client, its `client_id` and the host it returns
to, and asks for an approval code. When the hub shows the page it prints two
lines on its stderr, for example:

```text
12:04:31 llm-kit hub oauth: sign-in request R-7K2Q: 'Claude' (https://...) returning to claude.ai
12:04:31 llm-kit hub oauth: >>> approval code for R-7K2Q: 4XKM-92QD (valid 10 minutes, one use) <<<
```

The owner types the code (case, spaces and dashes do not matter) and chooses
Approve or Deny. The code is bound to that request (label `R-xxxx`, shown on
the page and on the terminal), lives 10 minutes and allows 5 wrong tries;
the fifth ends the request with `access_denied`. Deny ends it with
`access_denied`. At most 16 requests wait at once; the oldest goes first.
The form carries a per-request CSRF token and a `__Host-llm-kit-consent`
cookie (`Secure`, `HttpOnly`, `SameSite=Lax`); both must match. The page
cannot be framed. An approval redirects with `code`, `state` and `iss`.

Why a code on the terminal and not a passphrase: the code exists only in the
hub process and on its terminal, for 10 minutes and one use. A passphrase
would be a long-lived secret in a file. Anyone can find the public URL (TLS
certificates are logged in public), add it to their own claude.ai connector
and reach the consent page; with a terminal code they cannot approve it
without the owner's terminal. They can still make the hub print sign-in
requests; a request you did not start is one to leave alone. Keep the hub's
stderr on a terminal (for example a tmux window), not in a log file that
other tools read.

## 7. Tokens

| Item | Lifetime | Where |
|---|---|---|
| Approval code | 10 minutes, one use | hub memory, printed on stderr |
| Authorization code | 120 seconds, one use | hub memory |
| Access token | 1 hour; audience the canonical MCP URL | SHA-256 hash in `hub-oauth.json` |
| Refresh token | 30 days, rotated on every use | SHA-256 hash in `hub-oauth.json` |

- The code exchange needs `client_id`, `redirect_uri` and `code_verifier`
  that match the authorization request; `resource`, when given, must be the
  canonical MCP URL (`invalid_target`). A code is spent by its first
  presentation, whatever the outcome. Presenting it again is `invalid_grant`
  and revokes the tokens it bought.
- A refresh returns a new access token and a new refresh token. Presenting a
  rotated refresh token is `invalid_grant` and revokes every token of that
  sign-in; other sign-ins keep working. A refresh with another `client_id` or
  `resource` is refused without spending the token.
- A `client_id` may come in an `Authorization: Basic` header (RFC 6749
  section 2.3.1 encoding); its secret is ignored.
- Other errors: `invalid_request` (missing parameter, wrong content type),
  `unsupported_grant_type`, `invalid_client` (Basic header that disagrees).
  The token endpoint makes no network call, so it answers in milliseconds.
- Tokens issued for one public URL are refused after the hub moves to
  another.
- `hub-oauth.json` is in the state directory, mode `0600`, written
  atomically; a loose mode or an unreadable file stops the hub. It holds the
  registered clients and token hashes, never a token. To sign every client
  out, stop the hub and delete the file.
- Nothing logs a token, an authorization code or a verifier.

## 8. Pause

While the hub is paused (`llm-kit relay pause --scope hub`), `/register`,
`/authorize` and `/token` refuse (`temporarily_unavailable`, HTTP 503; an
HTML page for `/authorize`). The metadata documents still answer. `/mcp`
keeps its own rule: `tools/list` answers and every tool call is refused
`paused`.

## 9. Running the hub behind a tunnel

1. Turn both on in `relay.yaml` and name the public URL:

   ```yaml
   enabled: true
   hub:
     enabled: true
     public_url: https://<tunnel host>
   ```

2. Run the relay and the hub in the foreground, each in its own terminal:
   `llm-kit relay serve` and `llm-kit relay hub`. The hub's first stdout
   line is a JSON object whose `public_url` is the connector URL and whose
   `oauth` is `true`.
3. Point the tunnel's https port 443 at `http://127.0.0.1:4711`.
4. In claude.ai, add a custom connector with the URL `<public URL>/mcp` and
   connect. Approve on the page with the code from the hub's terminal.

## 10. Not known about claude.ai

These were not measured and the hub accepts either answer:

- whether claude.ai uses a client metadata document or registration (both
  work), and the `client_id` URL it uses (logged);
- whether it sends `resource` and `scope` on the token request (both
  optional there; a requested scope is echoed);
- whether the tunnel passes the public `Host` header or the loopback one
  (both are accepted), and whether claude.ai sends an `Origin` header on
  `/mcp` (claude.ai and claude.com are accepted).
