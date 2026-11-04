# llm-kit relay protocol (`relay-protocol/1`)

Status: frozen 2026-11-03 (harness-messaging build plan, unit F0). This is the
wire contract between the per-machine relay, the hub, the
`llm-kit relay client` CLI, the Claude Code channel server and the `/direct`
grant hooks. Every later relay unit builds against it.

The contract has three parts that must agree:

- `packages/katsoft-llm-kit/src/llm_kit/relay/protocol.py`: constants, the verb
  table, the state machine, the grammars, the TypedDicts and the `RelayClient`
  protocol. Standard library only.
- `packages/katsoft-llm-kit/src/llm_kit/relay/schemas/*.json`: one JSON Schema
  per request, response, envelope and shared object, each with examples.
- This document.

`tests/llm-kit/test_relay_protocol.py` checks that the three agree. A
disagreement is a defect to fix, not a choice.

Ports, state and config paths, token provisioning and machine-label
resolution belong to the relay configuration (unit C0), not to this protocol.

## 1. Versioning

Every envelope carries `protocol: "relay-protocol/1"`. Within
`relay-protocol/1` a change may only add:

- a new verb;
- a new optional request field, accepted by relays before any client sends it
  (request schemas refuse unknown fields);
- a new response field (clients ignore unknown response fields; response
  schemas allow them);
- a new error code, a new `origin_proof` type, a new enum member on the
  response side.

Removing or renaming a verb, field, code or state, or changing what an
existing one means, needs `relay-protocol/2`.

## 2. Transport binding

Every relay verb is `POST /v1/<verb>` with `Content-Type: application/json`.
The body is a request envelope of at most 65536 UTF-8 bytes. The reply body is
always a response envelope. The HTTP status is 200 for an ok response and the
status in the error table (section 5) for an error. The envelope is
authoritative; the status is advisory.

The hub's MCP tools are the eight section-7 verbs under the same names. A
tool's arguments are exactly the verb's request params and its result is
exactly the verb's response result.

## 3. Authentication, origins and grants

### Headers

| Header | Where | Meaning |
|---|---|---|
| `X-Relay-Token` | every request to a relay | The relay token. Without a valid one, the relay refuses `unauthenticated`. |
| `Authorization: Bearer <hub bearer>` | every request to the hub's MCP endpoint; also every hub-origin request to a relay (section 20) | The hub bearer. It authenticates the connector to the hub, and a hub-origin request to a relay. By itself it never authorizes a relay call. |
| `X-Relay-Grant` | optional on a granted verb | A grant id. When present, that grant must cover the call. When absent, the relay picks a covering live grant of the origin's session, and refuses `grant_required` if there is none. |

### Origins

The envelope's `origin` names who the request is for:

| Kind | Form | Who |
|---|---|---|
| channel | `channel:<machine>/claude/<session>` | a session's channel server |
| direct | `direct:<machine>/claude/<session>` | the client CLI acting for a `/direct` session |
| hub | `claude.ai/<user>` | the hub acting for the claude.ai connector |
| operator | `operator:<machine>` | the local operator CLI (pause, resume, status) |
| relay | `relay:<machine>` | the relay itself, in audit rows only; reserved for peer forwarding, which this protocol does not define |

A relay accepts a `direct` or `operator` origin only for its own machine and a
`direct` or `channel` origin only with harness `claude`. A channel origin must
match its registration (section 9).

### Who may call what

Each verb has a tier, the origin kinds that may call it (anything else is
`origin_not_allowed`), and the origin kinds that need a covering grant:

| Tier | Verbs | Origins | Grant needed for |
|---|---|---|---|
| channel | `register`, `heartbeat`, `deregister`, `poll`, `ack`, `reply` | channel | none (bound by the channel nonce) |
| read | `list_machines`, `list_sessions`, `read_session`, `get_replies`, `list_questions` | direct, hub | direct |
| llm | `ask_llm` | direct, hub | direct |
| write | `send_message`, `answer_question` | direct, hub | direct AND hub |
| grant | `grant_mint`, `grant_check` | direct | none |
| control | `pause`, `resume` | operator | none |
| control | `status` | channel, direct, hub, operator | none |

Rules that follow from the table:

- Every write verb needs a covering grant at EVERY ingress. No write path needs
  only the hub bearer or only the relay token. The hub refuses a write tool
  call without a grant at its own ingress, and the relay, which owns the grant
  table, checks and consumes it again.
- `relay-protocol/1` defines no grant producer for the hub origin. Until a
  later revision adds one (OAuth-bound or a signed proof), every hub
  `send_message` and `answer_question` is refused `grant_required`.
- Every `/direct` client verb needs a grant, reads included (owner ruling 9).
- Hub reads and `ask_llm` need no grant (design section 9, gate 4); `ask_llm`
  still needs the relay's consent policy (`consent_refused`).

### Alternative A: a guardrail, not a security boundary

Under alternative A (chosen 2026-11-03), grants are minted by the `/direct`
UserPromptSubmit hook through `grant_mint` with `origin_proof: {"type": "none"}`.
The relay token, the hub bearer and every origin are claims a same-user
process can make: a model with Bash can read the token, forge hook input and
call the relay over HTTP. Under A the grant check is a guardrail against
accidental model-initiated calls. It is NOT a security boundary against a
same-user model. In particular, any same-user process that holds the relay
token can mint a grant for any session by claiming that session's `direct`
origin (section 20).

### Extension points for alternative B

- `origin_proof` is a tagged object. `{"type": "none"}` is the A proof and
  admits no other field. `{"type": "enclave-signature", ...}` is reserved for B:
  the schema admits it (with `key_id`, `alg`, `challenge`, `signature`,
  `signed` and fields B adds later), and a `relay-protocol/1` relay refuses it
  `proof_unsupported`. A new proof type is a new branch, so A clients keep
  working when B lands.
- `signed` is an optional block on the request envelope, the response envelope
  and every delivered message: `audience`, `issued_at`, `not_after` (freshness),
  `nonce`, `key_id`, `alg`, `signature`. All fields are optional and reserved.
  A peer that does not verify signatures accepts a well-formed block and MUST
  NOT treat any of it as authentication. B decides which fields a proof type
  requires.

## 4. Envelopes

Request:

```json
{"protocol": "relay-protocol/1", "id": "req-0001", "verb": "send_message",
 "origin": "direct:m5pro/claude/<session>", "params": {"...": "..."}, "signed": {}}
```

`id` is chosen by the caller (`[A-Za-z0-9._:-]{1,128}`) and echoed back.
`signed` is optional.

Response, ok:

```json
{"protocol": "relay-protocol/1", "id": "req-0001", "ok": true, "result": {"...": "..."}}
```

Response, error (`id` is null only when the request could not be parsed):

```json
{"protocol": "relay-protocol/1", "id": "req-0001", "ok": false,
 "error": {"code": "grant_required", "message": "...", "retryable": false, "details": {}}}
```

`retryable` always equals the code's value in the table below. `details` is an
open object, for example `{"candidates": [...]}` for `target_ambiguous`,
`{"scope": "relay", "paused_since": ...}` for `paused`, the `llm-kit consent set`
command for `consent_refused`, or `{"message_id": ...}` for a refused send.

## 5. Error codes

| Code | Retryable | HTTP | Meaning |
|---|---|---|---|
| `bad_request` | no | 400 | Envelope or params fail their schema, or an identity is malformed. |
| `unsupported_protocol` | no | 400 | `protocol` is not `relay-protocol/1`. |
| `unknown_verb` | no | 404 | No such verb. |
| `not_implemented` | no | 501 | The verb exists but this relay or hub does not serve it. |
| `unauthenticated` | no | 401 | Missing or wrong relay token or hub bearer. |
| `origin_not_allowed` | no | 403 | The origin kind may not call this verb, or names another machine. |
| `grant_required` | no | 403 | No grant reference and no covering live grant. |
| `grant_unknown` | no | 403 | The named grant does not exist. |
| `grant_wrong_session` | no | 403 | The grant belongs to another session (cross-session replay). |
| `grant_wrong_verb` | no | 403 | The grant names another verb. |
| `grant_wrong_target` | no | 403 | The grant does not cover this target. |
| `grant_expired` | no | 403 | The grant is past `expires_at`. |
| `grant_used_up` | no | 403 | The grant has no uses left. |
| `proof_unsupported` | no | 403 | This relay does not accept the `origin_proof` type. |
| `target_invalid` | no | 400 | The target does not parse (section 7). |
| `target_not_found` | no | 404 | No session or endpoint matches. |
| `target_ambiguous` | no | 409 | A selector matches more than one; `details.candidates` lists them. |
| `not_opted_in` | no | 403 | The session does not accept messages (read-only, or the kind is not in `accepts`). |
| `session_ended` | no | 410 | The session's process is gone. |
| `hop_limit` | no | 403 | Hop count above 1. |
| `too_large` | no | 413 | Body over 8192 UTF-8 bytes. |
| `idempotency_conflict` | no | 409 | A message or reply id reused with different content. |
| `invalid_transition` | no | 409 | The message's state (section 12) or the question's state does not allow the operation. |
| `message_not_found` | no | 404 | No such message. |
| `question_not_found` | no | 404 | No such question. |
| `answer_invalid` | no | 422 | The answer's input fails the question's request schema. |
| `registration_unknown` | no | 404 | Unknown or lapsed registration; the channel must `register` again. |
| `registration_conflict` | yes | 409 | A live registration from another process holds this session. |
| `already_running` | no | 409 | A second `serve` of the same relay or hub role was refused because the singleton lock is held. |
| `nonce_mismatch` | no | 403 | The channel nonce does not match the registration. |
| `consent_refused` | no | 403 | The consent policy does not allow this endpoint's cost class. |
| `endpoint_refused` | no | 403 | The endpoint is not a loopback entry, or no explicit endpoint was named. |
| `rate_limited` | yes | 429 | A per-session or global rate limit, or a relay with its in-flight request limit reached. |
| `paused` | yes | 503 | The relay or hub is paused (section 16). |
| `machine_unreachable` | yes | 502 | A relay did not answer (also raised by a `RelayClient` on transport failure). |
| `upstream_unavailable` | yes | 502 | The LLM endpoint did not answer. |
| `internal` | no | 500 | A relay defect. |

## 6. Check order

A relay (and the hub, for its own ingress) applies its checks in this order;
the first failing stage decides the error: envelope (`bad_request`,
`unsupported_protocol`), auth (`unauthenticated`), verb (`unknown_verb`),
params (`bad_request`), origin (`origin_not_allowed`), pause (`paused`), grant
(the `grant_*` codes), then the verb's own refusals. A call refused at any
stage has no effect and consumes no grant use. (The HTTP transport checks the
relay token even earlier, before it reads the body; section 20.)

## 7. Addresses, selectors, identities and ids

```
address   = machine "/" harness "/" session
harness   = "claude" | "codex" | "llm"
machine   = lowercase DNS label, not one of: claude codex llm latest task cwd q
session   = [A-Za-z0-9][A-Za-z0-9._-]{0,127}, never "latest"
target    = address | [machine "/"] [("claude" | "codex") "/"] selector
selector  = "latest" | "task:" project "/" stub | "cwd:" (project | absolute-path)
```

- `<machine>` is the fleet label (C0 derives it from the env.json machines
  registry). `<session>` is the harness's own id; for harness `llm` it is the
  endpoint entry name, for example `m5pro/llm/qwen38-m5-32k-local`.
- The two target forms are disjoint: a session id never contains `:` and is
  never `latest`.
- A selector resolves on the relay to exactly one address. No match is
  `target_not_found`; several are `target_ambiguous` with the candidates.
  `latest` is the most recently active session; `task:<project>/<stub>` uses
  task-kit's session registration; `cwd:<project>` matches the last component
  of a session's cwd, and `cwd:/abs/path` matches the cwd exactly.
- `to` (`send_message`), `address` (`read_session`, `get_replies`) and grant
  targets accept a target. Results always carry resolved addresses.

Ids: `msg_` and `rpl_` ids (1-64 of `[A-Za-z0-9_-]` after the prefix) are
chosen by the sender and are idempotency keys; `grt_` and `reg_` ids are
assigned by the relay; a question id is `q:<machine>/<local id>`, assigned by
the relay and routable by machine.

Question ids. For a job-ledger question (the only kind relay-protocol/1
has) the local id is `<ledger-key>:<run-id>:<interrupt-id>`, so the whole id
is `q:<machine>/<ledger-key>:<run-id>:<interrupt-id>`:

- `<machine>` is the serving relay's label.
- `<ledger-key>` is the watched ledger's name. `relay.yaml` lists ledger
  paths only, so the relay derives the name: the file stem with every
  character outside `[A-Za-z0-9_.-]` replaced by `-`; ledgers whose stems
  collide each get `-<first 8 hex of sha1(path)>` appended. A config change
  that introduces a collision renames the affected keys, and an id from before
  the change answers `question_not_found` (list again). It is also the
  `source.ledger` field and the `ledger` field of the `ledgers` report below.
- `<run-id>` is the job run id verbatim (it may itself contain `:` or `/`).
- `<interrupt-id>` is the ledger's decimal interrupt id.

An id is parsed by splitting the ledger key off at the first `:` and the
interrupt id at the last `:`. The whole id is at most 266 characters (the
`question_id` schema limit); an interrupt whose id would be longer is left out
of the listing and named in its ledger's `skipped` list.

## 8. Verbs

| Verb | Client verb | Grant target param |
|---|---|---|
| `register` | | |
| `heartbeat` | | |
| `deregister` | | |
| `poll` | | |
| `ack` | | |
| `reply` | | |
| `list_machines` | `machines` | none |
| `list_sessions` | `sessions` | `machine` |
| `read_session` | `read` | `address` |
| `ask_llm` | `ask` | `endpoint` |
| `send_message` | `send` | `to` |
| `get_replies` | `replies` | `message_id` or `address` |
| `list_questions` | `questions` | `machine` |
| `answer_question` | `answer` | `id` |
| `grant_mint` | | |
| `grant_check` | | |
| `pause` | | |
| `resume` | | |
| `status` | | |

The client verbs are the `/direct` and `llm-kit relay client` names; each maps
one to one to its wire verb, so a capability added to the hub gains its
`/direct` form in the same change. The request and response of every verb are
`schemas/<verb>.request.json` and `schemas/<verb>.response.json`.

## 9. Channel handshake

`register`: the channel server sends `session_id`, `harness` (`claude`),
`pid` (the harness process it serves), `cwd`, `version`, `accepts` (message
kinds; empty means read-only) and a random `channel_nonce` (16-128 chars),
plus optional `title`, `task` and `transcript_path`. The origin must be
`channel:<this machine>/claude/<session_id>`. The result gives the
`registration_id`, the `address`, and the timing the channel follows:
`heartbeat_s` (30), `lease_s` (90), `ack_timeout_s` (60), `max_wait_s` (25),
plus `paused` and `replaced`.

- Every later channel call carries `registration_id`, `session_id` and
  `channel_nonce`. A wrong nonce is `nonce_mismatch`; an unknown or lapsed
  registration is `registration_unknown`, and the channel registers again.
- A second `register` for the same session from the same `pid` replaces the
  old registration (`replaced: true`, new nonce). From another live pid with
  an unexpired lease it is `registration_conflict`.
- `register`, `heartbeat` and `poll` each renew the lease. A registration
  whose lease lapses is ended; its unfinished messages become
  `session-ended`.

`heartbeat` (every `heartbeat_s`) renews the lease and updates metadata.

Session-id change (after `/clear` or a resume in the same process): the
channel sends `heartbeat` with the NEW `session_id` and
`previous_session_id` set to the old one. The registration keeps its id and
takes the new address (`session_changed: true`). Messages to the old address
in `queued` or `delivered`, and in `acked` for kinds that expect a reply, move
to `session-ended` with `state_reason: "session_changed"`
(`ended_messages` counts them). They are never redirected to the new
conversation; the sender resends to the new address if it still applies.

`deregister` ends the registration on a clean exit the same way.

## 10. Delivery

- `poll` long-polls up to `wait_s` (at most 25 s) and returns up to
  `max_messages` (at most 16) `delivered_message` objects. Each poll that
  returns a message moves it `queued -> delivered`.
- `ack` acknowledges ONE message (`delivered -> acked`). Acks are per message,
  never cumulative. Acking an already acked, answered or declined message
  returns its current state; acking a message in any other state is
  `invalid_transition`.
- Delivery is at-least-once. A message not acked within `ack_timeout_s` is
  delivered again on a later poll (`delivered -> delivered`, `attempt`
  increases). The channel MUST ack only after it has pushed the message into
  the session, and on a redelivery of an id it already pushed it MUST ack
  without pushing again. A resend of the same message id by its sender is not
  a new message (section 13), so a message is pushed into a session once.
- An undelivered message past `expires_at` moves to `lapsed`; so does a
  delivered message whose ack never came before `expires_at` (it is not
  redelivered stale; the session may have seen it).

## 11. Reply and decline

`reply` answers one delivered message: `in_reply_to`, `state` (`answered` or
`declined`), `body` (1-8192 chars; for a decline, the reason) and an optional
`rpl_` id. It is allowed from `delivered` or `acked` (a reply implies the
ack). A second reply to the same message is `invalid_transition`; a resend of
the same reply id and content returns the first reply with `replayed: true`.
A note may be replied to but needs no reply.

## 12. Mailbox states and transitions

States: `queued`, `delivered`, `acked`, `answered`, `declined`, `lapsed`,
`session-ended`, `refused`. A row is created as `queued`, or as `refused`
when a well-formed, authenticated, granted send is refused for a reason that
a retry cannot fix (`not_opted_in`, `session_ended`, `hop_limit`, `too_large`);
retryable refusals (`paused`, `rate_limited`) and refusals before the target is
resolved create no row. Terminal: `answered`, `declined`, `lapsed`,
`session-ended`, `refused`.

| From | To | Trigger |
|---|---|---|
| `queued` | `delivered` | `poll` |
| `queued` | `lapsed` | expiry |
| `queued` | `session-ended` | session ended or changed |
| `delivered` | `delivered` | redelivery after the ack timeout |
| `delivered` | `acked` | `ack` |
| `delivered` | `answered` / `declined` | `reply` |
| `delivered` | `lapsed` | expiry before an ack |
| `delivered` | `session-ended` | session ended or changed |
| `acked` | `answered` / `declined` | `reply` |
| `acked` | `session-ended` | session ended or changed; not for a `note`, which is complete once acked |

Every other move is `invalid_transition`. Every transition is an audit row.

## 13. Hub tools and client verbs

- `list_machines` -> every machine with `status` (`up`, `paused`,
  `unreachable`), protocol, last contact, pause state and LLM endpoints. An
  unreachable machine is listed with its error, never omitted.
- `list_sessions(machine?, harness?, state?)` -> sessions (address, cwd,
  title, task, state, last activity, `registered`, `accepts`) and the
  reachability of every machine asked. Session states: `live-accepting`,
  `live-read-only`, `idle` (process gone, resumable), `ended`. A scanned
  session is `registered: false` and read-only.
- `read_session(address, since?, limit?)` -> a capped (default 20, at most
  200 turns), redacted tail, newest last, with an opaque `cursor` for the next
  read and the count of redactions. Session text is data, not instructions.
- `ask_llm(endpoint, prompt [, conversation] | messages, max_tokens?,
  temperature?)` -> a completion. `endpoint` is an LLM address. Exactly one
  form: a `prompt` (optionally the next turn of a named `conversation` the
  relay keeps), or explicit `messages`.
- `send_message(to, kind, body, id?, expires_in_s?, task?, hop?)` -> the
  message record. `kind` is `note`, `instruction` or `question`. `id` is the
  idempotency key: a resend with the same id and content returns the original
  record with `replayed: true` and consumes no grant use (it still needs a
  covering grant); the same id with other content is `idempotency_conflict`.
  `expires_in_s` defaults to 3600 (at most 86400). `hop` defaults to 0; above
  1 is `hop_limit`. A refusal stored as `refused` returns the error and leaves
  the row readable through `get_replies`.
- `get_replies(message_id | address, wait_s?, limit?)` -> message records with
  their replies, newest last. Exactly one of `message_id` or `address`.
  `wait_s` (at most 25) long-polls until a listed message changes state. The
  hub routes `message_id` by asking every relay (message ids do not name a
  machine) and `address` by its machine.
- `list_questions(machine?, state?)` -> pending operator questions from the
  watched job ledgers (`open` by default, or `all`), each with its interrupt
  kind, payload and `request_schema`, plus machine reachability. States are
  `open`, `expired` (unresolved past `expires_at`, or resolved `expired`),
  `answered` and `rejected` (a `cancelled` ledger outcome is reported
  `rejected`). The result also carries `ledgers` (the schema allows extra
  fields): one entry per watched ledger, `{"ledger": <key>, "status": "ok"}`,
  or `"status": "unavailable"` with an `error` text when the ledger cannot be
  read, plus `skipped` for any interrupt left out for an over-long id. A
  broken ledger therefore never hides the others, and a missing one is never
  silent. A `machine` other than the serving relay's is listed `unreachable`
  (no forwarding in relay-protocol/1).
- `answer_question(id, decision, input | reason)` -> the resolution
  (`id`, `decision`, `state`, `replayed`, `resolved_at`, `job_id`,
  `job_state`). `decision` `answer` carries `input` (validated against the
  request schema, else `answer_invalid` with `details.errors` as
  `[json_pointer, keyword]` pairs); `reject` may carry `reason`. Same
  semantics as `llm-kit jobs resolve`, with the same stored resolution, job
  state change and execution events; like it, the answer never resumes the
  run. An equal resolution replays (`replayed: true`), a different one, an
  expired interrupt and a job no longer waiting are `invalid_transition`; a
  malformed id is `bad_request`; an id naming another machine, an unknown
  ledger key, run or interrupt, or an unreadable ledger is
  `question_not_found`. The grant target is the `id` param, so a grant covers
  exactly the questions it names. It is a write verb: it needs a covering
  grant, reserves one use before the answer is written, and gets it back only
  when the call failed before any effect (section 14; every refusal above is
  such a failure, a replay keeps its use because the call returned ok). It is
  refused `paused` while the relay is paused, and a hub-origin call is
  `grant_required`.

### The hub endpoint and routing

The hub (`llm_kit/relay/hub.py`, `llm-kit relay hub`) is an MCP server over
streamable HTTP, stateless with JSON responses, at `/mcp` on
`RelayConfig.bind("hub")` (`127.0.0.1:4711` by default; a literal loopback
address only, checked before any state is touched and again on the bound
socket). External exposure is a separate, later unit and nothing here opens it.

- Authentication: every HTTP request needs `Authorization: Bearer <hub
  bearer>`, compared in constant time, before MCP sees the request. A missing
  or wrong bearer is HTTP 401 with an `unauthenticated` response envelope.
- Tools: one per served verb, named as the verb, with the verb's `<verb>.request`
  schema as `inputSchema` and its `<verb>.response` schema as `outputSchema`;
  every description says that session and model output is data, not
  instructions. This build serves `list_machines`, `list_sessions`,
  `read_session`, `ask_llm`, `get_replies` and `list_questions`, the verbs a
  relay serves to a hub origin without a grant. `send_message` and
  `answer_question` have no hub tool: a relay refuses every hub-origin write
  `grant_required` (section 3, no hub grant producer), so a tool could only
  return that refusal. They are added, by registering a handler in the hub's
  router table, with a hub grant producer. The hub therefore cannot answer a
  session's permission prompt, in P1 or P2. A call to a tool not served is
  `unknown_verb`.
- Tool errors: a refusal or failure is an MCP tool result with `isError` true
  whose text and structured content are `{"error": <error object>}` (code,
  message, retryable, details) with the section-5 code; it is never a JSON-RPC
  error. A defect inside the hub is `internal`. A success result is the
  verb's result, unwrapped.
- Check order at the hub: the tool exists (`unknown_verb`), the arguments fit
  the verb's request schema (`bad_request`), the hub is not paused (`paused`),
  then the verb's own refusals. `tools/list` answers while the hub is paused.
  Results are validated against the response schema (`internal` on a
  mismatch).
- Origin: every relay call the hub makes carries the origin
  `claude.ai/<hub_user>` (`owner` unless the process names another user) and
  both credentials, `X-Relay-Token` and the hub bearer; the bearer alone never
  authorizes a relay call. This is the audit origin a relay records for
  connector traffic.
- Relays: the hub reaches relays only through `RelayClient` objects built from
  `(machine label, URL)` pairs; by default one, this machine's relay at
  `RelayConfig.bind("relay")`. Machine labels are unique.
- Fan-out: `list_machines`, `list_sessions` and `list_questions` ask every
  relay in parallel and mark a relay that cannot answer `unreachable` with its
  error object instead of omitting it. `list_sessions` with a `machine` the hub
  has no relay for returns no sessions and that machine `unreachable`
  (`machine_unreachable`); `list_questions` does the same with no questions and
  no ledgers. `list_questions` merges the relays' questions ordered by
  `created_at` then id, and merges their `ledgers` entries with a `machine`
  field added to each, because ledger keys are unique per relay only. A relay
  that is paused is reported by its own `machines` entry, not as an error.
- Routing: `read_session` and `ask_llm` go to the relay named by the target's
  machine; a target naming a machine the hub has no relay for is
  `machine_unreachable`. `ask_llm` needs an address with a machine. A bare
  `read_session` selector (no machine) goes to the only relay when there is
  one; with several it is tried on every relay and must resolve on exactly
  one: more than one is `target_ambiguous` with the candidate addresses, none
  with some relay `machine_unreachable` is `machine_unreachable` naming those
  machines, and otherwise the first relay's error is returned.
- `get_replies` routes by its `address` the same way (a machine in the address
  names the relay; a bare selector must resolve on exactly one). A `message_id`
  names no machine, so the hub asks every relay, without `wait_s`, and exactly
  one may hold it: more than one is `target_ambiguous` with the machines as
  candidates, none with an unreachable relay is `machine_unreachable`, and
  none otherwise is the relay's `message_not_found`. A `wait_s` long-poll then
  runs against the holding relay only, so ambiguity is decided before any wait
  and the caller waits at most `wait_s` plus one probe.
- State: `llm-kit relay hub` creates the state directory and the relay token
  and hub bearer when absent (the same files the relay creates, race-safe),
  but not the loopback registry, which only the relay writes. It does not need
  the relay to be running: an unreachable relay is reported per call. The hub
  role is a singleton per state directory (`already_running`).
- Pause: `llm-kit relay pause --scope hub` is read from disk on every call, so
  it takes effect at once and survives a restart; relays independently refuse
  hub-origin calls while the hub is paused (section 16).

## 14. Grants

Grant object: `grant_id`, `session` (the address of the `/direct` session
that holds it), `verb` (one section-7 verb), `targets` (resolved targets),
`issued_at`, `expires_at`, `uses`, `uses_remaining`, `origin_proof`, `state`
(`live`, `used-up`, `expired`).

- `grant_mint(session, verb, targets, expires_in_s, uses, origin_proof,
  command?)`: origin `direct` only, and `session` must equal the origin's
  address (a session mints only for itself). `expires_in_s` is 1-600; `uses`
  is 1-32. Selectors in `targets` are resolved at mint (`target_not_found`,
  `target_ambiguous`). `command` records the typed `/direct` line for audit.
  Under A, `origin_proof` is `{"type": "none"}`.
- `grant_check(session, verb, target?, grant_id?)`: whether a live grant
  covers the call, WITHOUT consuming it (the PreToolUse hook's visible
  refusal). Returns `covered`, the `grant_id` that would be used, and the
  refusal `code` when not covered.
- Coverage: the request's verb equals the grant's verb, the origin's session
  equals the grant's session, the grant is live with uses left, and the value
  of the verb's target param (section 8) is in `targets`. A request that names
  no target is covered only by a grant whose `targets` is empty.
- Consumption: one use is reserved atomically BEFORE the call's effect, so
  two concurrent calls cannot share the last use (the loser is
  `grant_used_up` and has no effect). A call that returns ok has consumed its
  use. A reserved use is given back only when the call failed before any
  effect (section 20, "Grants" states the rule); a call whose effect may have
  happened keeps it spent. `send_message` reserves in the mailbox transaction
  that inserts the message, which is its effect.
- With a named grant, the first failing check in this order is the code:
  `grant_unknown`, `grant_wrong_session`, `grant_wrong_verb`,
  `grant_wrong_target`, `grant_expired`, `grant_used_up`. With no named grant
  and no covering live grant, the code is `grant_required`.

## 15. Audit row

Append-only. Fields: `seq` (strictly increasing), `at`, `machine`, `ingress`
(`relay` or `hub`), `event` (`call` for every request, `transition` for every
mailbox state change), `origin` (the identity; `relay:<machine>` for the
relay's own expiry and scan events), `verb` (a verb, or `expire` / `scan`),
`outcome` (`ok`, `refused`, `error`), and optional `request_id`, `target`,
`grant_id`, `message_id`, `from_state`, `to_state`, `code`, `detail`. A
`/direct` call is recorded with origin `direct:<machine>/claude/<session>`,
distinct from `claude.ai/<user>`.

## 16. Pause

`pause(scope, reason?)` and `resume(scope)`, origin `operator`, set and clear
a persisted flag; `scope` is `relay` or `hub`. Both return the pause state
(`relay` and `hub`, each `paused`, `paused_since`, `reason`).

- Relay paused: `send_message` and `answer_question` are refused `paused`
  from every origin; `poll` returns no messages with `paused: true` at once.
  Registration, heartbeats, acks, replies, reads, `ask_llm`, grants and
  `status` continue. Expiry keeps running, so a queued message can lapse
  during a pause. Queued messages are delivered after `resume`. A waiting
  `poll` sees the pause within the relay's poll interval (1 s), so delivery
  stops well inside one heartbeat. Every `poll` is an audit row and audit rows
  are never deleted, so a channel server polls a paused relay slowly instead of
  once a second (the llm-kit channel server waits 5 s between paused polls;
  delivery resumes within that gap after `resume`).
- Hub paused: the hub refuses every tool call `paused`, and relays refuse
  every request whose origin kind is `hub`. `/direct` (origin `direct`) is not
  affected by a hub pause.
- The flags survive a restart (C0 persists them).

## 17. Limits

| Constant | Value |
|---|---|
| `MAX_REQUEST_BYTES` | 65536 |
| `MAX_BODY_BYTES` | 8192 |
| `MAX_HOP` | 1 |
| `HEARTBEAT_INTERVAL_S` | 30 |
| `REGISTRATION_LEASE_S` | 90 |
| `ACK_TIMEOUT_S` | 60 |
| `MAX_POLL_WAIT_S`, `MAX_REPLY_WAIT_S` | 25 |
| `MAX_POLL_BATCH` | 16 |
| `DEFAULT_EXPIRES_IN_S`, `MAX_EXPIRES_IN_S` | 3600, 86400 |
| `MAX_GRANT_TTL_S`, `MAX_GRANT_USES` | 600, 32 |
| `DEFAULT_READ_TURNS`, `MAX_READ_TURNS` | 20, 200 |

Rate limits and their values belong to the mailbox (unit M1); a refusal is
`rate_limited`.

## 18. Schemas

- `common.json` holds every shared definition under `$defs`. Every other file
  references them as `#/$defs/<name>`. `protocol.load_schema(name)` returns the
  file with common's `$defs` merged in, which is self-contained; a consumer in
  another language composes the same way. A file may not redefine a common
  name.
- Every schema is written in the JSON Schema subset `SCHEMA_SUBSET`
  (`llm-scripting-kit.json-schema-subset/v1`), so `llm_kit.schema.json_schema`
  can check and validate it with the standard library alone. The subset has no
  `pattern`, so the address, identity and id grammars are enforced by
  `protocol.parse_target`, `parse_identity` and `is_valid_id`, not by the
  schemas.
- Request schemas refuse unknown fields; response and object schemas allow
  them. The A proof (`{"type": "none"}`) refuses extra fields; the reserved B
  proof allows them.
- Every file carries `examples`. The test validates each example, rejects
  mutated ones, and checks that the TypedDicts in `protocol.py` name the same
  fields as the schemas.

Files: `common`, `envelope.request`, `envelope.response`, the objects `error`,
`signed`, `origin_proof`, `grant`, `audit_row`, `message_record`,
`delivered_message`, `reply_record`, `session`, `machine`, `pause_state`,
`question`, and `<verb>.request` / `<verb>.response` for every verb.

## 19. Not in relay-protocol/1

- Ports, bind addresses, state and config paths, token provisioning, machine
  labels: relay configuration (C0).
- Relay-to-relay forwarding and its peer authentication: a later additive
  revision (origin kind `relay` is reserved).
- A grant producer for the hub origin, and verification of `signed` or of any
  `origin_proof` other than `none`: alternative B or the OAuth phase.
- `continue_session`, `start_session`, `stop_session` and remote approval of
  permission prompts: later, separately gated phases.

## 20. Relay server decisions (unit S3)

The relay server is `llm_kit/relay/service.py` (the checks and verbs,
independent of the transport, so the hub can reuse them),
`llm_kit/relay/server.py` (HTTP), `llm_kit/relay/client.py`
(`HttpRelayClient`, the `RelayClient` over HTTP) and the CLI group
`llm-kit relay`. Where sections 2 to 17 leave room, it decides as follows.

### Transport and lifecycle

- The relay binds only a literal loopback address, `RelayConfig.bind("relay")`
  (`127.0.0.1:4710` by default). The server checks the host before it creates
  any state, and again on the address the socket bound. A non-loopback host is
  a configuration error and nothing is bound.
- Start order: prepare the state (directory, relay token, hub bearer, loopback
  registry), take the relay role lock, bind. A second relay on the same state
  directory is refused `already_running` before it binds.
- A method other than POST, a `Content-Type` other than `application/json`,
  or a path verb that differs from the envelope's `verb` is `bad_request`. A
  path that is not `/v1/<verb>`, or names no verb, is `unknown_verb`.
- `X-Relay-Token` is checked, in constant time, before any part of the body
  is read; a missing or wrong token is `unauthenticated` (HTTP 401) with a
  null `id` and no audit row (nothing of the envelope has been read). The
  Content-Type and declared size checks follow, then a request takes one of
  at most 64 in-flight slots (`DEFAULT_MAX_IN_FLIGHT`, held until the worker
  finishes, long polls and `ask_llm` included); a request past the bound is
  `rate_limited` (HTTP 429, retryable) with `details.what` `in_flight`. The
  body must arrive in full within 10 seconds (`DEFAULT_BODY_DEADLINE_S`);
  otherwise it is `bad_request`.
- A request body over `MAX_REQUEST_BYTES` is `too_large` (HTTP 413) with
  `details.what` `request`. The JSON constants `NaN` and `Infinity` are
  `bad_request`. A malformed `origin` identity is `bad_request` at the envelope
  stage.
- Every result is validated against its `<verb>.response` schema before it is
  returned; a result that does not conform is `internal`.

### Authentication

- Every relay request needs a valid `X-Relay-Token`, whatever its origin.
- A hub-origin request (`claude.ai/<user>`) also needs
  `Authorization: Bearer <hub bearer>`; without it, or with a wrong one, it is
  `unauthenticated`. Both comparisons are constant time. The bearer alone
  never authorizes a relay call. Requiring it on hub origins stops a relay-token
  holder from claiming the hub origin to skip the `/direct` read grants. A hub
  therefore sends both headers. Under alternative A this is a guardrail too: a
  same-user process can read both files.

### Grants

- `grant_mint` and `grant_check` accept only the origin
  `direct:<this machine>/claude/<S>` with `session` equal to `<S>`. Any other
  origin, and a `session` other than the caller's own, is
  `origin_not_allowed`. `origin_proof` must be `{"type": "none"}`;
  `enclave-signature` is `proof_unsupported`.
- Plainly: under alternative A, a same-user process that holds the relay token
  can mint a grant for any session by claiming that session's `direct`
  origin. The grant check stops accidental model-initiated calls; it is not a
  security boundary. `llm-kit relay client` has no mint verb. The `/direct`
  UserPromptSubmit hook is the mint path.
- A selector in a granted call's target param (`read_session` `address`,
  `send_message` `to`, `get_replies` `address`) is resolved to an address at
  the grant stage, before coverage is checked. A selector that matches no live
  session, or several, is refused there (`target_not_found`,
  `target_ambiguous`). Selector targets of `grant_mint` and `grant_check`
  resolve the same way.
- Consumption: `send_message` consumes its use in the mailbox transaction that
  inserts the message (a refused or replayed send consumes nothing). Every
  other granted verb is checked at the grant stage and then RESERVES one use
  in a single immediate transaction (`consume_grant`) before its handler runs;
  if a concurrent call took the last use, the call is refused `grant_used_up`
  with no effect. This is the atomic-effect requirement of section 14: no
  effect runs without a reserved use.
- Refund rule: a reserved use is given back (`refund_grant`, never above the
  minted `uses`) only when the handler failed before any effect. That is a
  `RelayError` other than `too_large` and `internal` (the verbs that reach a
  handler's refusal before acting: an unresolvable target, `endpoint_refused`,
  `consent_refused`, `bad_request`), and `upstream_unavailable` only when its
  cause shows the connection was never made (connection refused, name
  resolution failed). A timeout, a dropped connection, a completion that is
  `too_large`, an `internal` error and any other exception keep the use
  spent, because the endpoint may have processed the request. A call that
  returns ok never refunds.
- A hub-origin `send_message` or `answer_question` is `grant_required` at the
  grant stage, whatever `X-Relay-Grant` names.

### Channel verbs

- A `register` origin must be `channel:<this machine>/claude/<session_id>`. The
  other channel verbs must name the `session_id` of their params (`heartbeat`:
  `session_id` or `previous_session_id`). Anything else is
  `origin_not_allowed`.
- `ack` and `reply` check the registration and nonce but do not renew the lease
  (only `register`, `heartbeat` and `poll` do, section 9).
- When a registration ends (`deregister`, a lapsed lease, a session-id change,
  or a `register` from a pid that holds another session), the mailbox ends the
  old address's unfinished messages with `state_reason` `deregistered`,
  `lease_lapsed` or `session_changed`. The server also sweeps lapsed leases and
  expired messages every 5 seconds.
- `poll` long-polls up to `wait_s`. A `send_message` to this relay wakes it at
  once; a write by another process is seen within the poll interval (1 second).
  While the relay is paused, `poll` returns at once with no messages, and a
  waiting `poll` returns when it sees the pause.

### Messaging

- `send_message` resolves `to` with session discovery and derives the refusal
  from the session's state: `idle` or `ended`, or a registration that ended
  (also when no transcript shows the session), is `session_ended`;
  `live-accepting` with the kind in `accepts` is queued; anything else
  (`live-read-only`, a Codex session, a kind not accepted) is `not_opted_in`.
  Both refusals are stored `refused`. A target that no store shows is
  `target_not_found` and creates no row.
- `get_replies` with `message_id` returns that message; with `address`, the
  newest `limit` (default 50) messages to that address, oldest first, whoever
  sent them (unit M9 kept it so: the grant the owner typed `/direct replies` for names the address, and hub reads need no grant). With `wait_s` it returns as soon as the
  state or `updated_at` of a listed message changes.

### Reads and control

- `list_machines` lists this machine only: `status` `up` or `paused`, and the
  endpoints of the relay's loopback registry.
- `list_sessions` for another machine returns no sessions and lists that
  machine `unreachable` with `machine_unreachable`: relay-protocol/1 defines no
  forwarding.
- `status` adds `registrations` (live registrations), and `library_version`
  when the package metadata is installed.
- `list_questions` and `answer_question` are served through the normal verb
  dispatch by `llm_kit.relay.questions.QuestionService`, built over the
  ledgers of `RelayConfig.ledgers` with the relay's clock; a paused relay
  lists itself `paused` in `machines`. `answer_question` is a granted write
  verb: it takes the reserve-and-refund path above like every other verb
  except `send_message`, with the question `id` as its grant target.
- Every verb of relay-protocol/1 is served, so the not-served set
  (`NOT_SERVED`) is empty. A verb listed there would be refused
  `unknown_verb` at the verb stage with
  `details: {"verb": ..., "not_implemented": true}`.

### Audit

- Every request whose envelope names a known verb and a well-formed origin gets
  one `call` row, refused requests included, with `request_id`, `target`, the
  `grant_id` of the use consumed, and the refusal `code`. The exception is
  `unauthenticated`, whose rows are bounded (below). `outcome` is `error` for
  `internal`, `upstream_unavailable` and `machine_unreachable`, and `refused`
  for every other code.
- A request whose body does not parse, that names an unknown verb, or that
  carries a malformed origin gets no row: the audit table admits only known
  verbs and identities. A request refused at the transport (no relay token, a
  body that did not arrive, the in-flight bound) was never parsed and gets no
  row either.
- `unauthenticated` rows (a hub-origin request without the hub bearer, or a
  service called in process) are bounded, because audit rows are never
  deleted: one row per origin per 60-second window (`AUTH_AUDIT_WINDOW_S`),
  at most 60 such rows per window over all origins
  (`AUTH_AUDIT_MAX_ROWS_PER_WINDOW`), and at most 10,000 in the table
  (`AUTH_AUDIT_MAX_TOTAL_ROWS`, counted from the table when the relay starts).
  The requests a window did not record are counted in one closing row for the
  origin (`detail` names the count; for origins past the per-window limit,
  one closing row with origin `relay:<machine>`), written when the next failed
  request arrives after the window or when the maintenance sweep runs. At the
  total cap the relay stops recording and prints one line to stderr; the
  refusals themselves are unchanged.

### Client and CLI

- `HttpRelayClient` accepts only a loopback `http` URL (the relay token never
  leaves this machine) and ignores proxy environment variables. A transport
  failure is `machine_unreachable`. `poll` and `get_replies` timeouts grow by
  `wait_s`; `ask_llm` waits up to 660 seconds.
- `llm-kit relay serve` runs the relay in the foreground until SIGINT or
  SIGTERM. `pause`, `resume` and `status` call the relay as
  `operator:<machine>`; with `--offline` they read or write the pause file
  directly (no relay, no audit row). `llm-kit relay hub [--host H] [--port N]`
  runs the hub in the foreground the same way (loopback only, until SIGINT or
  SIGTERM) and prints one JSON object `{"serving": <MCP URL>, "machine": ...,
  "protocol": ...}` once it listens. `llm-kit relay client <verb> --session <S>`
  calls one of the eight client verbs as `direct:<machine>/claude/<S>`, with
  `--grant` as `X-Relay-Grant`. Output is JSON; exit 0 ok, 1 refused or
  unreachable, 2 usage or configuration.

## 21. Session discovery decisions (unit D2)

Session discovery (`llm_kit/relay/discovery.py`, `transcript.py`,
`redact.py`) decides the following points sections 7 and 13 leave open.

- `idle` means the process is gone and the transcript was written within the
  last 7 days; otherwise the session is `ended`. The old session id after a
  `/clear` or resume (a session-id change) is `ended`.
- Selectors (`latest`, `task:`, `cwd:`) resolve against live sessions only
  (`live-accepting`, `live-read-only`). Any other session is addressed by its
  full address. `task:` matches the task recorded on a registration.
- A `register` from a pid that holds a registration for a different session id
  ends that registration as `session_changed`: one process, one registration.
- A tool result whose call is outside the scanned window is withheld (replaced
  by a short notice and counted as a redaction), as is the result of a call
  that touched a secrets location.
- Codex sessions are only `idle` or `ended`: their processes are shared
  app-servers that hold no rollout file open, so liveness is not detectable.
- `read_session` may add `skipped_lines`, the count of transcript lines in the
  scanned window that are not JSON.
