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

Contract amendments. A request-side enum member or a widened identity grammar
is also allowed within `relay-protocol/1`, as a numbered amendment, when all
four conditions hold:

1. Every request that was valid before stays valid with the same meaning, so
   no existing client changes.
2. A relay without the amendment refuses the new request non-retryably at the
   envelope or params stage (`bad_request`), with no effect and no state: no
   registration, no mailbox row, no lease.
3. A relay that accepts the amendment says so in a response field before any
   client sends the new request, so deployment is relay-first and a client
   checks the field instead of assuming it.
4. The amendment is listed here with its date and the library version that
   ships it.

The amendments:

- Amendment 1, 2026-11-12, katsoft-llm-kit 1.19.0 (Codex channel origin). The
  channel identity is `channel:<machine>/(claude|codex)/<session>`.
  `register.harness` is one of `claude`, `codex`. `status.harnesses` lists the
  channel harnesses the relay serves; a relay that omits the field serves
  `claude` only. The `direct:` identity stays `direct:<machine>/claude/<session>`:
  `/direct` is a Claude Code feature. A relay of 1.18.0 or earlier refuses
  `harness: codex` and a `channel:<machine>/codex/<session>` origin with
  `bad_request` (condition 2) and reports no `harnesses` (condition 3).
- Amendment 2, 2026-11-12, katsoft-llm-kit 1.21.0 (relay-to-relay
  forwarding). The request envelope gains the optional field `on_behalf_of`,
  and the origin kind `relay` (`relay:<machine>`) may originate the read, llm
  and write verbs and `status` for a peer relay that forwards a `/direct`
  session's call (section 20, "Forwarding"). The header `X-Relay-Peer-Key`
  carries the peer key: the one credential another machine's hub and relay
  hold for this relay, which authenticates the principal `peer:<machine>`
  and never travels with the relay token (section 3, "Principals").
  `status.forwarding: {"receive": true}` says a relay
  accepts forwarded calls; `status.harnesses` is not reused for it, because
  serving a harness does not establish forwarding support. A message stored
  from a forwarded send carries `forwarded_by: relay:<machine>` on its
  `message_record` and `delivered_message`, and an audit row may carry
  `on_behalf_of`. Requests without the field are unchanged (condition 1). A
  relay of 1.20.0 or earlier refuses `on_behalf_of` with `bad_request` at the
  envelope stage, because its envelope schema refuses unknown fields
  (condition 2), and reports no `forwarding` (condition 3); a forwarding relay
  checks the field before it sends a forwarded call, so a receiver upgrades
  first. The same release moves the hub's peer calls (katsoft-llm-kit 1.19.0)
  from the peer's general relay token and hub bearer to the peer key: a
  `peer export` object is `llm-kit-relay-peer/2` and carries the key alone,
  and the `llm-kit-relay-peer/1` object is refused with the re-export
  command, and a `peers/<machine>/relay.token` or `hub.bearer` that 1.19.0
  or 1.20.0 wrote stops the hub and relay, peer key or not, naming the
  command that deletes it (section 20, "Client and CLI"). A hub of 1.20.0 or
  earlier cannot reach a 1.21.0 peer, so both machines upgrade together.
  Upgrade step: if you ran `llm-kit relay peer export` on a machine before
  1.21.0, run `llm-kit relay rotate-credentials` on that machine and restart
  the consumers it names. Those exports handed out its general relay token
  and hub bearer, which authenticate as the local principal and stay valid
  until rotated; nothing rotates them implicitly.

- Amendment 3, 2026-11-13, the katsoft-llm-kit release after 1.22.0 (remote
  resume; owner ruling on plan-codex Q2, option (a): sessions that once opted
  in, off unless relay.yaml says so, permission mode `default`). The verb
  `continue_session` is added (a new verb is a plain addition under the first
  list above); it is recorded as an amendment because it widens two
  request-side enums, `verb` and `grantable_verb` in `common.json`, and adds a
  client verb (`continue`) and a hub tool. A relay without it refuses the verb
  `unknown_verb` with no effect (a defined, non-retryable refusal, the shape
  condition 2 asks for), and `status.continue_session: {"enabled": <bool>}`
  says that a relay serves it and whether its relay.yaml turns it on
  (condition 3). No existing request changes meaning (condition 1). The
  mailbox (schema version 3) gains `messages.claimed_by`, the `opt_ins` table
  and the `resume_leases` table; none of them is on the wire. What a consumer
  that vendors the schemas must copy: `common.json` (the two enums and the
  audit row's `verb` enum) and, if it vendors them, `status.response.json`
  and the new `continue_session.request.json` and
  `continue_session.response.json`. The llm-kit plugin vendors
  `common.json` and `register.request.json`; only `common.json` changes for
  it. Behaviour: section 20, "Remote resume".

Policy revisions inside `relay-protocol/1` (no envelope, schema or field
changed; a client of the earlier policy keeps working):

- 2026-11-11, hub writes (owner rulings "opt-in session = grant" and "allow
  answering any question"): a relay accepts a hub-origin `send_message`
  without a grant, for a session that opted in through a channel, and a
  hub-origin `answer_question` without a grant, for any question (section 3).
  Before, both were `grant_required`. (katsoft-llm-kit 1.14.0 refused hub
  answers `not_opted_in`; 1.15.0 accepts them.)
- 2026-11-12, authority of a `/direct` message (owner ruling "Yes, count it as
  me"): a message whose `sender` is a `direct:` origin counts as the owner's
  own words in the receiving session, as a `claude.ai/<user>` message already
  did (section 3). Before, it was another session's request. No relay
  behaviour changed; the receiving session's channel server states the rule
  (llm-kit plugin 0.20.0).

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
| `X-Relay-Token` | every request to a relay from its own machine | The relay token: the local principal. Without a valid one (and without a peer key), the relay refuses `unauthenticated`. |
| `Authorization: Bearer <hub bearer>` | every request to the hub's MCP endpoint; also every hub-origin request to a relay made with the relay token (section 20) | The hub bearer. It authenticates the connector to the hub, and a hub-origin request to a relay. By itself it never authorizes a relay call. |
| `X-Relay-Grant` | optional on a granted verb | A grant id. When present, that grant must cover the call. When absent, the relay picks a covering live grant of the origin's session, and refuses `grant_required` if there is none. |
| `X-Relay-Peer-Key` | every request from another machine's hub or relay (amendment 2) | The peer key the receiving relay issued to exactly one machine (`accept/<machine>/peer.key`), compared in constant time against every issued key: the principal `peer:<machine>` (section 3, "Principals"). It is the whole credential of such a request: one that also carries `X-Relay-Token`, and one whose key the relay did not issue, is `unauthenticated`. |

### Origins

The envelope's `origin` names who the request is for:

| Kind | Form | Who |
|---|---|---|
| channel | `channel:<machine>/(claude\|codex)/<session>` | a session's channel server |
| direct | `direct:<machine>/claude/<session>` | the client CLI acting for a `/direct` session |
| hub | `claude.ai/<user>` | the hub acting for the claude.ai connector |
| operator | `operator:<machine>` | the local operator CLI (pause, resume, status) |
| relay | `relay:<machine>` | the relay itself in audit rows; and (amendment 2) a peer relay forwarding a `/direct` session's call, with `on_behalf_of` naming that session |

A relay accepts a `direct` or `operator` origin only for its own machine, a
`direct` origin only with harness `claude`, and a `channel` origin with harness
`claude` or `codex` (amendment 1, section 1). A channel origin must match its
registration (section 9). A `relay:<M>` origin is accepted only from the
principal `peer:<M>` (the relay token never authenticates it:
`unauthenticated`; another machine's key: `origin_not_allowed`), and, for
every verb but `status`, only with `on_behalf_of` (`origin_not_allowed`
without it).

### Principals

The credential a request presents, never the origin its envelope claims,
decides who is calling:

- The relay token is the local principal. It may claim every origin kind but
  `relay`, under the rules of this section (unchanged: the accepted risks
  below).
- A peer key is the principal `peer:<M>`, where `<M>` is the machine the
  relay issued that key to (`llm-kit relay peer export --for <M>`, stored as
  `accept/<M>/peer.key` under the same private-file policy as the relay
  token: `0700`/`0600` on POSIX, a protected owner-plus-SYSTEM ACL on
  Windows). The relay admits `peer:<M>` only while its `relay.accept_from`
  lists `<M>` (`origin_not_allowed` otherwise, whatever the origin; a key on
  disk binds nothing once `<M>` leaves the list).

A holder of the peer key `<R>` issued to `<M>` can do exactly this at `<R>`,
and nothing else (`origin_not_allowed`):

- claim `relay:<M>`: `status`, and, with `on_behalf_of: direct:<M>/claude/<S>`,
  the read, llm and write verbs (`list_machines`, `list_sessions`,
  `read_session`, `get_replies`, `list_questions`, `ask_llm`, `send_message`,
  `answer_question`) on `<R>`'s own sessions, ledgers and endpoints, with no
  grant stage at `<R>` (the source relay checked the session's grant);
- claim the hub origin `claude.ai/<user>`, with no hub bearer: `status` and
  the same eight verbs with the hub's rules (an opted-in session for
  `send_message`, any question for `answer_question`). This is the hub on
  `<M>` reaching `<R>` (section 13).

It can never claim `direct:`, `operator:` or `channel:` for any machine,
`<R>` included, nor `relay:` for a machine other than `<M>`, nor
`on_behalf_of` a session on another machine; so it mints no grant, pauses
and resumes nothing and registers no channel. A request it authenticates is
never forwarded onward (one hop): neither origin it may claim is ever
forwarded, so `<R>` answers it from its own machine only. A local-principal
request never claims `relay:`, so no local caller can make `<R>` act on
behalf of a peer.

The envelope field `on_behalf_of` (amendment 2) is valid only with a `relay`
origin; on any other origin it is `bad_request`, so a client never supplies
its own delegation. It must be `direct:<M>/claude/<S>`, where `<M>` is the
relay origin's machine; anything else is `origin_not_allowed`.

### Who may call what

Each verb has a tier, the origin kinds that may call it (anything else is
`origin_not_allowed`), and the origin kinds that need a covering grant:

| Tier | Verbs | Origins | Grant needed for |
|---|---|---|---|
| channel | `register`, `heartbeat`, `deregister`, `poll`, `ack`, `reply` | channel | none (bound by the channel nonce) |
| read | `list_machines`, `list_sessions`, `read_session`, `get_replies`, `list_questions` | direct, hub, relay | direct |
| llm | `ask_llm` | direct, hub, relay | direct |
| write | `send_message`, `answer_question` | direct, hub, relay | direct |
| write | `continue_session` (amendment 3) | direct, hub | direct |
| grant | `grant_mint`, `grant_check` | direct | none |
| control | `pause`, `resume` | operator | none |
| control | `status` | channel, direct, hub, operator, relay | none |

Rules that follow from the table:

- A `/direct` write needs a covering grant, checked and consumed by the
  relay, which owns the grant table. Every `/direct` client verb needs a grant,
  reads included (owner ruling 9).
- A relay origin (amendment 2) runs no grant stage at the receiver. That is
  safe only because the forwarding relay checked the `/direct` session's grant
  for the exact remote target and reserved its use before it sent anything,
  because the peer key binds the request to one requester machine, and
  because `on_behalf_of` must name a session on that machine (section 20,
  "Forwarding"). A relay origin never joins the channel, grant or control
  tiers (`status` excepted).
- `continue_session` (amendment 3) is the one write a relay origin may not
  call (`origin_not_allowed`) and that is never forwarded: a `/direct`
  session asks the relay of its own machine, and a target on another machine
  is `target_invalid` (`details.why: not_forwarded`). The hub reaches the
  target's relay directly, with this relay's own credentials or, for a peer,
  the peer key that relay issued (a hub origin under a peer key). A hub
  `continue_session` needs no grant, as a hub `send_message` needs none; the
  target machine's `resume: {enabled: true}` and the session's past opt-in are
  the permission (section 20, "Remote resume").
- A hub write needs no grant. For `send_message` the target session's opt-in
  is the permission (owner ruling 2026-11-11, "opt-in session = grant"): a
  session the owner launched with the channel (`clauder --relay`, which
  registers it, section 9) accepts hub sends, and every other session is
  read-only for the hub. The relay enforces it, not the hub: a hub
  `send_message` to a session that is not `live-accepting` with the kind in
  its `accepts` is `not_opted_in` (`session_ended` when the process is gone),
  the same refusal every origin gets (section 20, "Messaging"). A hub
  `answer_question` answers any question (owner ruling 2026-11-11, "allow
  answering any question"), with the refusals a `/direct` answer meets. No
  write path needs only the relay token: a hub write also needs the hub
  bearer (section 20, "Authentication").
- Authority of a message. A message whose `sender` is `claude.ai/<user>`
  counts as the owner's own words in the receiving session, gated actions
  included (owner ruling 2026-11-11, decision 7, "let remote count as you";
  switched on the same day). So does a message whose `sender` is
  `direct:<machine>/claude/<session>`, the origin the client CLI sends for a
  session where the owner typed `/direct` (owner ruling 2026-11-12, "Yes,
  count it as me": "Any session where you've typed /direct can send messages
  that carry your authority. The risk: that session's Claude could send one
  without you writing it."). A `direct` sender may be on another of the
  owner's machines (amendment 2): its relay forwarded the send, and the
  receiving relay stored it with `forwarded_by: relay:<machine>` naming that
  relay; it carries the same authority. Only `direct` and hub senders are ever
  stored, so every delivered message has one of these two senders; a message
  with any other sender is an untrusted request. The owner accepted these
  risks under alternative A:
  - any process on the machine that can read the relay token and the hub
    bearer (any Claude session running as the owner, for example) can send a
    message as `claude.ai/<user>` and so impersonate the owner;
  - any process that can read the relay token can send a message as the
    `direct` origin of any Claude session on the machine, whether or not the
    owner typed `/direct` there. The relay refuses a `direct` `send_message`
    without a covering grant (`grant_required`), but `grant_mint` checks only
    the relay token and that the origin names the session it mints for
    (section 14, `origin_proof` type `none`); the `/direct` enablement that
    the llm-kit hooks check is a file such a process can also write;
  - the Claude in a `/direct` session can send a message the owner did not
    write;
  - with `resume: {enabled: true}` in a machine's relay.yaml (amendment 3),
    each of the senders above can also resume any idle session on that
    machine that once opted in, and run one turn in it, without the owner
    at the keyboard and with no per-call confirmation (owner ruling on Q2);
    a peer key holder can do so through a hub origin at every peer that
    accepts its key and turned resume on;
  - a machine holds the peer key each peer issued to it
    (`peers/<peer>/peer.key` in its state directory, section 13), so any
    process there that can read it can do at that peer what "Principals"
    above lists: send a message as `claude.ai/<user>` to that peer's
    opted-in sessions, answer its questions, read it and ask its endpoints,
    and do the same as the `direct` origin of any Claude session on the
    holding machine (`relay:<holder>` with `on_behalf_of`). The first two
    risks above are extended from the holding machine to every peer that
    accepts it, and to nothing more: the key claims none of the peer's own
    `direct`, `operator` or `channel` origins and is never forwarded on.

### Peer credentials

A relay on another machine (a peer) is reached with one secret: the peer key
that peer issued to this machine, sent as `X-Relay-Peer-Key` by both this
machine's hub (hub origin) and its relay (`relay:` origin). No relay token or
hub bearer, this machine's or the peer's, is ever sent to a peer or exported:
this machine's go only to loopback URLs, and a non-loopback URL takes the peer
key alone. There is no fallback when a peer's key is missing (the hub and the
relay refuse to start instead), and the key is never sent over plain `http`
or anywhere but that peer's URL. Provisioning is a pull by this machine over a
channel the owner already trusts: `ssh <peer> llm-kit relay peer export --for
<this machine>`, piped into `llm-kit relay peer import <peer>` (section 20,
"Client and CLI"), after the peer's `relay.accept_from` lists this machine; no
credential is written into a configuration file, a task record or a
transcript.

Before 1.21.0, `peer export` printed the general relay token and hub bearer.
If you ran `peer export` on a machine before 1.21.0, run `llm-kit relay
rotate-credentials` on that machine and restart the consumers it names:
until then those credentials still authenticate as its local principal,
with every risk listed above, whatever `relay.accept_from` says. On the
machine that imported them, the hub and relay refuse to start while
`peers/<machine>/relay.token` or `hub.bearer` exists, peer key or not, and
name the command that deletes them; they are never deleted automatically.

  The llm-kit channel server states the rule and these risks to the
  receiving session.
- Hub reads and `ask_llm` need no grant (design section 9, gate 4); `ask_llm`
  still needs the relay's consent policy (`consent_refused`).
- Nothing in this protocol answers a session's permission prompt.

### Alternative A: a guardrail, not a security boundary

Under alternative A (chosen 2026-11-03), grants are minted by the `/direct`
hooks of the llm-kit plugin through `grant_mint` with
`origin_proof: {"type": "none"}`.
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
| `continue_session` | `continue` | `to` (a full address; never forwarded) |
| `grant_mint` | | |
| `grant_check` | | |
| `pause` | | |
| `resume` | | |
| `status` | | |

With forwarding peers (amendment 2), a `/direct` call naming a peer goes to that
peer, and the grant target is the same param under the same coverage rule; the
per-verb routing and refund rules are in section 20, "Forwarding".

The client verbs are the `/direct` and `llm-kit relay client` names; each maps
one to one to its wire verb, so a capability added to the hub gains its
`/direct` form in the same change. The request and response of every verb are
`schemas/<verb>.request.json` and `schemas/<verb>.response.json`.

## 9. Channel handshake

`register`: the channel server sends `session_id`, `harness` (`claude` or
`codex`), `pid` (the harness process it serves), `cwd`, `version`, `accepts`
(message kinds; empty means read-only) and a random `channel_nonce` (16-128
chars), plus optional `title`, `task` and `transcript_path`. The origin must be
`channel:<this machine>/<harness>/<session_id>`. The result gives the
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

A `codex` registration (amendment 1) is the same handshake. Its `pid` is the
`codex app-server` child the channel owns, its `version` is the Codex banner,
and its optional `transcript_path` is the thread's rollout file. It never sends
`previous_session_id`: a Codex thread id does not change in a running channel.
A client checks `status.harnesses` for `codex` before registering one (section
1); a relay without it answers `bad_request`.

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
  session is `registered: false` and read-only. A Codex thread registered
  through a channel (amendment 1) is `registered: true` and `live-accepting`
  like a Claude one; a scanned Codex thread whose writer lock is held is
  `live-read-only` (section 21).
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
  record with `replayed: true` and consumes no grant use (a `/direct` resend
  still needs a covering grant); the same id with other content is
  `idempotency_conflict`.
  `expires_in_s` defaults to 3600 (at most 86400). `hop` defaults to 0; above
  1 is `hop_limit`. A refusal stored as `refused` returns the error and leaves
  the row readable through `get_replies`. A hub send needs no grant and is
  accepted only for an opted-in session (section 3).
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
  silent. A `machine` other than the serving relay's is listed `unreachable`,
  unless the relay forwards a `/direct` call to that peer (section 20,
  "Forwarding").
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
  refused `paused` while the relay is paused. A hub-origin call needs no
  grant (section 3) and otherwise meets the same checks and refusals.

- `continue_session(to, kind, body, id?, expires_in_s?, task?)` (amendment 3)
  -> the message record, exactly as `send_message` returns it, with the
  message already `delivered` and claimed for the run; the run's answer
  arrives as its reply (`get_replies`), `answered` with the final text or
  `declined` with the reason. `to` must be a full address of a session on the
  serving relay's machine. Off unless that machine's relay.yaml sets
  `resume: {enabled: true}`. Refusals and the run: section 20, "Remote
  resume". The hub routes it like `send_message`, to the one relay `to`
  names.

### The hub endpoint and routing

The hub (`llm_kit/relay/hub.py`, `llm-kit relay hub`) is an MCP server over
streamable HTTP, stateless with JSON responses, at `/mcp` on
`RelayConfig.bind("hub")` (`127.0.0.1:4711` by default; a literal loopback
address only, checked before any state is touched and again on the bound
socket). The hub runs only when this machine's `relay.yaml` holds
`enabled: true` and `hub: {enabled: true}` (section 20). With a public URL
(`hub.public_url`, the https origin a tunnel serves it at) it also serves a
single-user OAuth sign-in for the claude.ai connector, specified in
`llm-kit-relay-hub-oauth.md`; the library opens no tunnel.

- Authentication: every HTTP request needs `Authorization: Bearer <hub
  bearer>`, compared in constant time, or a live OAuth access token the hub
  issued (`llm-kit-relay-hub-oauth.md`), before MCP sees the request. Both map
  to the same origin. A missing or wrong token is HTTP 401 with an
  `unauthenticated` response envelope; with a public URL its
  `WWW-Authenticate` names the protected resource metadata.
- Tools: one per served verb, named as the verb, with the verb's `<verb>.request`
  schema as `inputSchema` and its `<verb>.response` schema as `outputSchema`;
  every description says that session and model output is data, not
  instructions. This build serves all eight: `list_machines`,
  `list_sessions`, `read_session`, `ask_llm`, `send_message`, `get_replies`,
  `list_questions` and `answer_question`. The two writes' descriptions say who
  accepts them (section 3), and their annotations are not read-only; the relay
  decides acceptance, not the hub. No tool answers a session's permission
  prompt, in P1 or P2. A call to a tool not served is `unknown_verb`.
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
  `(machine label, URL)` pairs. By default that is this machine's relay at
  `RelayConfig.bind("relay")` plus the peers `relay.yaml` names under
  `relay: {peers: [{machine: <label>, url: <origin>}]}` (nested in the `relay`
  block, never a new top-level key). Each peer label must name an enabled
  `env.json` machine other than this one, once; the URL is an `https` origin (a
  plain `http` origin only for a literal loopback address). Without `peers`
  the hub is the one-relay hub. Machine labels are unique.
- Peers: each peer is reached with the peer key it issued to this machine,
  read from `peers/<machine>/peer.key` under the hub's state directory, and
  nothing else (section 3, "Peer credentials"); the peer's relay authenticates
  the call as `peer:<this machine>`, so it must list this machine in its
  `relay.accept_from`. The directories are private and the file is validated
  on every read like the state root. A missing directory or key is a
  configuration error at hub start that names `ssh <machine> llm-kit relay
  peer export --for <this machine> | llm-kit relay peer import <machine>`. A
  `relay.token` or `hub.bearer` that a 1.19.0 or 1.20.0 import stored in that
  directory is a configuration error too, whether or not `peer.key` exists:
  the error names each file, the exact command that deletes it, and
  `llm-kit relay rotate-credentials` to run on the peer (section 3, "Peer
  credentials"). They are never read and never deleted automatically. No hub tool needs
  more than a peer key: the hub calls every relay with the hub origin only,
  and pause, resume and status as `operator:` stay with each machine's own
  CLI. The connection verifies the peer's
  TLS certificate, follows no redirect and ignores proxy environment
  variables. A connection that cannot be opened within 3 seconds, and an HTTP
  502, 503 or 504 answer that carries no relay envelope (a tunnel in front of
  a relay that is down), are `machine_unreachable` for that peer.
- Label check: every machine a relay's result names must equal the label the
  hub configured for it, in each field that carries one: the `machine` and
  `endpoints` of `list_machines`; the `machine` and `address` of each session
  and the `machine` of each reach entry in `list_sessions`; the `machine` and
  the `q:<machine>/...` id of each question in `list_questions`; the `address`
  of `read_session`; the `endpoint` of `ask_llm`; the `to` of each message and
  the `sender` of its reply in `send_message` and `get_replies`; the id of
  `answer_question`. These destination and ownership fields stay strict.
  A message's `sender` is sender provenance and has its own rule (amendment
  2): it may be the hub (`claude.ai/<user>`, which names no machine), a
  `direct` session on the relay that stored it, or a `direct:<M>/...` session
  on another machine `<M>` only when the same record has
  `forwarded_by: relay:<M>` and `<M>` is one of the hub's configured relays.
  A channel, operator or relay identity as sender, a foreign sender without
  the stamp, a stamp that does not match the sender's machine, and a stamp
  naming an unknown machine are refused. A relay that names another machine,
  or a machine field that is not well formed, is refused `internal`, naming
  both labels and the field, before the hub aggregates, routes or shows any of
  the result; the fan-out verbs list that machine `unreachable` with that
  error. Free text (turns, bodies) is never read. The table of fields, with
  `sender` and `forwarded_by` marked as provenance, is `LABELED_FIELDS` in
  `routing.py` (re-exported by `hub.py`), and a test fails when a response
  schema gains a machine-bearing field that is not in it. A forwarding relay
  applies the same checks to its peers' results (section 20, "Forwarding").
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
- Writes go to exactly one relay and are never fanned out. `send_message`
  needs `to` as an address with a machine (a selector is `target_invalid`) and
  goes to that machine's relay; a machine the hub has no relay for is
  `machine_unreachable`. `answer_question` goes to the relay its question id
  names (`q:<machine>/...`; an id naming no machine is `bad_request`).
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
  but not the loopback registry, which only the relay writes, and not the
  `peers/` tree, which only the peer import command creates. It does not need
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
  `target_ambiguous`). `command` records the `/direct` line the owner typed
  (the one that enabled the session) for audit.
  Under A, `origin_proof` is `{"type": "none"}`.
- `grant_check(session, verb, target?, grant_id?)`: whether a live grant
  covers the call, WITHOUT consuming it. Returns `covered`, the `grant_id` that would be used, and the
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
`grant_id`, `message_id`, `from_state`, `to_state`, `code`, `detail`,
`on_behalf_of`. A `/direct` call is recorded with origin
`direct:<machine>/claude/<session>`, distinct from `claude.ai/<user>`. A
forwarded call is recorded at the receiver with origin `relay:<machine>` and
`on_behalf_of` naming the `/direct` session (amendment 2); the forwarding
relay records the session's own call row, whose `detail` names the peer.
Rows written before katsoft-llm-kit 1.21.0 have a null `on_behalf_of`.

## 16. Pause

`pause(scope, reason?)` and `resume(scope)`, origin `operator`, set and clear
a persisted flag; `scope` is `relay` or `hub`. Both return the pause state
(`relay` and `hub`, each `paused`, `paused_since`, `reason`).

- Relay paused: `send_message`, `answer_question` and `continue_session` are refused `paused`
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
- Forwarding more than one hop: a relay origin is never forwarded or fanned
  out again (amendment 2 is one hop), and channel, operator, hub and relay
  origins are never forwarded at all. A request authenticated by a peer key
  may claim only a relay or hub origin (section 3, "Principals"), so it is
  never forwarded, whatever it claims.
- Verification of `signed` or of any `origin_proof` other than `none`:
  alternative B or the OAuth phase.
- `start_session`, `stop_session` and remote approval of permission prompts:
  later, separately gated phases. (`continue_session` joined the protocol in
  amendment 3; it never approves a prompt: section 20, "Remote resume".)

## 20. Relay server decisions (unit S3)

The relay server is `llm_kit/relay/service.py` (the checks and verbs,
independent of the transport, so the hub can reuse them),
`llm_kit/relay/server.py` (HTTP), `llm_kit/relay/client.py`
(`HttpRelayClient`, the `RelayClient` over HTTP) and the CLI group
`llm-kit relay`. Where sections 2 to 17 leave room, it decides as follows.

### Transport and lifecycle

- Opt-in: the relay runs only when this machine's `relay.yaml` holds
  `enabled: true` (the hub also needs `hub: {enabled: true}`). The server
  checks the file when it is constructed and again at start, before any state
  is created; a missing file or key is off, a malformed one is an error, and
  no flag or environment variable turns it on. The plugin's check is
  `llm_kit.relay.config.relay_enabled()` / `hub_enabled()`
  (`llm-kit-relay-hub-oauth.md` section 1).
- The relay binds only a literal loopback address, `RelayConfig.bind("relay")`
  (`127.0.0.1:4710` by default). The server checks the host before it creates
  any state, and again on the address the socket bound. A non-loopback host is
  a configuration error and nothing is bound. The hub binds the same way. On
  Windows both listeners bind with `SO_EXCLUSIVEADDRUSE`, because there
  `SO_REUSEADDR` would let a second process bind a port that is in use.
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

- Every relay request needs a valid `X-Relay-Token` (the local principal) or
  a valid `X-Relay-Peer-Key` (the principal `peer:<M>`), never both
  (`unauthenticated`); the transport checks one of them before it reads the
  body. A `relay:` origin needs the peer key. Section 3, "Principals", lists
  what each principal may claim.
- A hub-origin request (`claude.ai/<user>`) with the relay token also needs
  `Authorization: Bearer <hub bearer>`; without it, or with a wrong one, it is
  `unauthenticated`. With a peer key it needs no bearer. All comparisons are
  constant time. The bearer alone
  never authorizes a relay call. Requiring it on hub origins stops a relay-token
  holder from claiming the hub origin to skip the `/direct` grants. A hub
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
  hooks of the llm-kit plugin are the mint path: once the owner has typed
  `/direct` in a session, its PreToolUse gate mints one single-use grant for
  each relay client call it lets through (the plugin's `hooks/` sources
  document the session-wide enablement).
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
- A hub-origin call has no grant stage: the verb table names `direct` alone
  in every verb's grant column, and `X-Relay-Grant` on a hub call is ignored.
  A hub `send_message` meets the messaging refusals below; a hub
  `answer_question` meets the questions refusals of section 13.

### Channel verbs

- A `register` origin must be `channel:<this machine>/<harness>/<session_id>`,
  with `harness` one of `claude`, `codex` (amendment 1). The
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
  `live-accepting` with the kind in `accepts` is queued, whatever the
  harness; anything else (`live-read-only`, a scanned Codex thread, a kind not
  accepted) is `not_opted_in`.
  Both refusals are stored `refused`. A target that no store shows is
  `target_not_found` and creates no row. The rule is the same for a hub and a
  `/direct` origin; it is what makes a session's opt-in the hub's write
  permission (section 3).
- `get_replies` with `message_id` returns that message; with `address`, the
  newest `limit` (default 50) messages to that address, oldest first, whoever
  sent them (unit M9 kept it so: a `/direct replies` grant names the address, and hub reads need no grant). With `wait_s` it returns as soon as the
  state or `updated_at` of a listed message changes.

### Reads and control

- `list_machines` lists this machine only: `status` `up` or `paused`, and the
  endpoints of the relay's loopback registry.
- `list_sessions` for another machine returns no sessions and lists that
  machine `unreachable` with `machine_unreachable`, except a `/direct` call for
  a forwarding peer (section 20, "Forwarding").
- `status` adds `registrations` (live registrations), `harnesses` (the channel
  harnesses served, amendment 1 of section 1), `forwarding: {"receive": true}`
  (amendment 2) and `library_version` when the package metadata is installed.
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

- `HttpRelayClient` accepts a loopback `http` URL (this machine's relay), or a
  peer: a non-loopback URL, which must be `https` and comes with that peer's
  own relay token and hub bearer, both given explicitly (this machine's
  credentials never go to a peer). TLS is verified against the system trust
  roots and cannot be turned off. Redirects are never followed and proxy
  environment variables are ignored, for loopback and peers alike. A transport
  failure is `machine_unreachable`, as is an HTTP 502, 503 or 504 answer with no
  relay envelope; a peer's connect timeout is 3 seconds. Every error the
  client makes up itself (no relay error envelope) carries `details.sent`:
  `false` only when the connection was never opened (name resolution failed,
  connection refused, connect timeout, no free pooled connection), `true`
  with `outcome: "unknown"` otherwise (a read timeout or dropped connection
  after sending, a gateway answer, a reply that is not a relay envelope). With
  a peer key (`peer_key=`) it sends `X-Relay-Peer-Key` alone on every call and
  is refused, at construction, together with a relay token or hub bearer; a
  non-loopback URL requires one. `poll` and
  `get_replies` timeouts grow by `wait_s`; `ask_llm` waits up to 660 seconds.
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
- `llm-kit relay peer export --for <M>` mints, or reuses, the peer key this
  relay issues to `<M>` as `accept/<M>/peer.key` (private directories, a
  `0600` file, the protected ACL on Windows) and prints it as one JSON object,
  `{"format": "llm-kit-relay-peer/2", "machine": <label>, "peer_key": ...}`.
  `--for` is required, `<M>` must be listed in `relay.accept_from` and must
  not be this machine, and the command refuses (exit 2) when standard output
  is a terminal. This machine's relay token and hub bearer are never
  exported. `llm-kit relay peer import <machine>` reads that object from
  standard input (never a terminal, at most 4096 bytes), requires its
  `machine` to equal `<machine>`, which must be an enabled `env.json` machine
  other than this one, and writes `peers/<machine>/peer.key` through the
  private-file helpers (private directories, a `0600` file, a protected
  owner-plus-SYSTEM ACL on Windows). It prints the file name and whether the
  peer is configured in `relay.peers`, never the key. An
  `llm-kit-relay-peer/1` object (what 1.19.0 and 1.20.0 exported: the general
  relay token and hub bearer) is refused with the re-export command, and
  nothing is stored. A relay reads its issued keys at start, and the hub and
  relay read the stored ones at start, so both machines restart after
  provisioning. `llm-kit relay status` adds `peers` (each peer's machine, URL
  and whether its peer key is `stored` or `missing`) when `relay.peers` is
  configured, and adds nothing otherwise.
- `llm-kit relay rotate-credentials` replaces this machine's relay token and
  hub bearer with new random ones, each written atomically through the
  private-file helpers after the existing file passed the read checks, and
  prints one JSON object: `rotated` (the two files), `restart` (the local
  consumers that read them once, at start: `llm-kit relay serve`, `llm-kit
  relay hub`, each `llm-kit relay codex` channel), `reconnect` (the claude.ai
  connector, only for a hub without `hub.public_url`, which signs in with the
  bearer), `unchanged` (the Claude channel server, the `/direct` hooks and the
  client CLI, which read the token on every call) and a note that a running
  relay and hub accept the old values until they restart. It never prints a
  credential, refuses (exit 2) when either file is missing, and is never run
  implicitly, at start or otherwise.
- `llm-kit relay client sessions` shows live sessions only by default
  (`live-accepting`, `live-read-only`) and adds `omitted`, the count of the
  others by state (an unfiltered listing was about 158 KB, mostly ended
  sessions). `--state all` lists every session; `--state <state>` sends that
  one state to the relay. The filter is the CLI's: the verb and the hub tool
  are unchanged.

### Forwarding (amendment 2)

Configuration. On the forwarding (source) relay, each `relay.peers` entry is a
forwarding peer, reached at its URL with the peer key it issued to the source
(`peers/<machine>/peer.key`) and nothing else; a missing key stops the relay
at start naming the provisioning command, as it stops the hub. On the
receiving relay, `relay: {accept_from: [<label>, ...]}` (nested in the `relay`
block, never a new top-level key) names the machines whose hub and relay may
call it with a peer key; each must be a non-disabled `env.json` machine other
than itself, once. `peer export --for <M>` issues `<M>`'s key; `ssh <receiver>
llm-kit relay peer export --for <source> | llm-kit relay peer import
<receiver>` on the source provisions it, for the source's hub and relay
alike.

Source-side checks. The source relay sends a peer request only when all of
these hold: the origin is `direct:<self>/claude/<S>` (a channel, operator, hub
or relay origin is never forwarded); the verb is in the read, llm or write
tier; the request carries no `on_behalf_of` (one from a client is
`bad_request`); the grant stage passed for `S`, the verb and the target value
the table below names, compared exactly as granted; and one use is reserved
before the first forwarded request. The order is fixed:

1. the local grant check, with no I/O: a refusal here (`grant_required`, for
   example) sends zero peer requests of any kind, capability probes included;
2. the capability probe: `status` as `relay:<self>`, whose result must carry
   `forwarding.receive: true`; its answer is cached per peer for 60 seconds
   (`PEER_PROBE_TTL_S`), a transport failure is not cached;
3. reserving the use (`consume_grant`), so of two concurrent calls on the last
   use one is `grant_used_up` and sends nothing;
4. forwarding, as `relay:<self>` with `on_behalf_of: direct:<self>/claude/<S>`
   set by the relay itself.

A single-target call to a relay that does not advertise `forwarding.receive`,
or whose probe is refused, is `not_implemented`, with nothing sent and
nothing consumed; a probe that cannot reach the peer is `machine_unreachable`
with `details.sent: false`, also with nothing consumed. In a fan-out, such a
peer is listed `unreachable` with that error and the rest proceed.

Per-verb rules. The grant target is the verb's target param (section 8) under
the coverage rule of section 14: a request that names no target is covered
only by a grant whose `targets` is empty, and a named target must be in
`targets`. One client call reserves exactly one use, whatever the number of
peer requests.

| Verb (target param) | Request form | Routing | Use refunded only when |
|---|---|---|---|
| `list_machines` (none) | always | fan out: this relay plus every forwarding peer; a peer that cannot answer is listed `unreachable` | the call fails before any peer request (an ok fan-out is never refunded) |
| `list_sessions` (`machine`) | no `machine` | fan out as above; each peer request carries `machine: <peer>` | as `list_machines` |
| | `machine: <peer>` (grant target `<peer>`) | that peer only | the outcome rules below |
| | `machine: <self>`, or no peer of that name | local, or `unreachable` with no I/O | the local rule (section 20, "Grants") |
| `list_questions` (`machine`) | as `list_sessions` | as `list_sessions` (ledger entries gain `machine`) | as `list_sessions` |
| `read_session` (`address`) | full address naming a peer | that peer | the outcome rules below |
| | bare selector (`latest`, `cwd:`, `task:`) | local only; never fanned out | the local rule |
| | selector with a peer prefix (`5090w/latest`) | `target_invalid`, also at `grant_mint`, no I/O | always (nothing was reserved) |
| `ask_llm` (`endpoint`) | `<peer>/llm/<entry>` | that peer | the outcome rules below |
| `send_message` (`to`) | full address naming a peer | that peer | the outcome rules below (a refused or replayed send refunds) |
| `get_replies` (`address`) | full address naming a peer | that peer; `wait_s` runs there | the outcome rules below |
| `get_replies` (`message_id`; the grant target is the id) | an id names no machine | ask this relay and every peer WITHOUT `wait_s`; exactly one may hold it; `wait_s` then runs on the holder only; several holders are `target_ambiguous` | only when every relay was asked and answered `message_not_found` |
| `answer_question` (`id`) | `q:<peer>/...` | the peer the id names | the outcome rules below |

Outcome rules for a forwarded call. The use is refunded only on a CERTAIN
pre-effect outcome: the request was provably not sent (`details.sent:
false`); the receiver answered with an error envelope whose code is
pre-effect under the rule of "Grants" above; or a send answered
`replayed: true` or one of the stored refusals (`not_opted_in`,
`session_ended`, `hop_limit`, `too_large`), mirroring the local rule that a
refused or replayed send consumes nothing. Everything else keeps the use spent
and is `machine_unreachable` with `details: {sent: true, outcome: "unknown"}`:
a read timeout or dropped connection after sending, a gateway 502, 503 or 504
without an envelope, and a malformed reply.

Deduplication. A forwarded send always carries an id: the source relay assigns
`msg_<random>` when the client gave none, before reserving, and names it in
every result and in every error's `details.message_id`. The receiver's
idempotency (same id and content is `replayed`) makes a retry after an unknown
outcome deliver once. The retry needs a fresh grant, and its replay refunds
that grant.

Provenance. A receiver that accepts a send from an authenticated
`relay:<M>` stores `sender` as the `on_behalf_of` identity and `forwarded_by`
as `relay:<M>`; no client can set either. The source relay checks every peer
result with the label rules of section 13 (destination and ownership fields
must name the peer, senders follow the provenance rule with this relay and its
peers as the known machines), and a forwarded send's record must have exactly
the `on_behalf_of` it sent as `sender` and `relay:<self>` as `forwarded_by`.
A result that fails is `internal`, and the use stays spent.

Receiver. A relay answers a relay-origin call from its own machine only: a
relay-origin list verb is never fanned out again, and a relay-origin call is
never forwarded.

Mailbox. `schema_version` 2 adds the nullable `messages.forwarded_by` and
`audit.on_behalf_of` columns; a version-1 mailbox is migrated in place at
open, and its rows keep null. A relay of 1.20.0 or earlier refuses a
version-2 mailbox (`internal`, naming both versions).

### Remote resume (amendment 3)

`continue_session` is the one verb after which the relay starts a process
(`llm_kit/relay/resume.py`). A Codex or Claude channel still starts nothing in
the relay.

Configuration. relay.yaml `resume: {enabled: false, timeout_s: 1800,
claude_permission_mode: default}`, every key optional, unknown keys refused.
`enabled` defaults to false, and the relay reads the file on every call, so
turning it off takes effect at once (the top-level `enabled: true` is needed
too). `timeout_s` (1..86400) bounds one run. `claude_permission_mode` accepts
only `default` (owner ruling on Q2); no `bypass*` mode is ever accepted.

Eligibility. The target must be a session address of this machine that
registered through a channel with a non-empty `accepts` at least once (the
durable `opt_ins` table, written at every such `register` and heartbeat), and
that is now `idle`: no live process, no held Codex writer lock, a transcript
to resume, and a recorded working directory that exists. Checked in this
order, each refusal with no effect and no row:

| Condition | Code | `details` |
|---|---|---|
| `to` names another machine | `target_invalid` | `why: not_forwarded` |
| resume off | `not_opted_in` | `why: resume_disabled`, `key: resume.enabled`, `config` |
| never opted in | `not_opted_in` | `why: never_opted_in` |
| a run already holds the address | `rate_limited` (retryable) | `what: resume_running`, `message_id` |
| session live | `invalid_transition` | `why: session_live`, `state`, `use: send_message` |
| session ended or nothing to resume | `session_ended` | `why` |
| no existing working directory | `target_invalid` | `why: no_cwd` |

A resend with an existing `id` and the same content is a replay
(`replayed: true`, no new run); the same id with other content, or an id used
by a `send_message`, is `idempotency_conflict`. A `/direct` call consumes its
grant use in the transaction that stores the message, so a refusal consumes
nothing.

Claim and lease. One IMMEDIATE mailbox transaction re-checks the target's
liveness, stores the message, moves it `queued -> delivered` with
`claimed_by` set to a fresh lease id, and inserts the address's
`resume_leases` row (phase `claimed`, the relay instance id, expiry). A
claimed message is never returned by `poll`, never acked or replied by a
channel (`invalid_transition`, `why: resume_running`), never lapsed and never
moved to `session-ended`. A `register` of an address that holds a lease is
`registration_conflict` (retryable), `details.why: resume_running`, so a
returning channel waits; the register check and the claim run under one lock
in the relay. What stays open is the gap between that check and the spawn
for a process the relay does not see first: the owner can open the session
in a TUI or `claude --resume` meanwhile. A Codex run then fails with "thread
is open in another Codex process"; a Claude run is a documented residual race.

The run. The lease phase becomes `spawning`; the child starts in a new
session (its own process group) with `LLM_KIT_RESUME_LEASE=<lease id>` in its
environment; its pid, create time and group are recorded (phase `running`).
Every phase write, every renewal (every 30 s, lease 90 s, until the run's
deadline) and the final write are fenced by the lease id: a runner that lost
its lease kills its own child's group and writes nothing.

- Codex: `codex app-server --listen stdio://` with the Codex channel's
  defaults (approval `never`, `workspace-write` rooted at the rollout's
  recorded cwd, network off, hooks and notify off), `thread/resume` with
  `/tmp` and `$TMPDIR` excluded and the effective policy compared
  (`config_mismatch` declines), one `turn/start` whose text is the Codex
  channel's two header lines then the body, judged by the turn result only.
  An approval or user-input request is answered `cancel` where the schema
  offers it and the turn is always interrupted; the message is declined.
- Claude: `claude -p --resume <id> --output-format stream-json --verbose
  --permission-mode default`, the prompt (the same two header lines, then
  the body, so the prompt never starts with the body) on stdin, cwd = the
  transcript's cwd, `CLAUDECODE` and `CLAUDE_CODE_*` removed from the
  environment. Answered only when the run's `init` event names the same
  session id, cwd and permission mode (otherwise the child is killed and the
  message declined `config_mismatch`) and a later `result` event of that
  session has `is_error: false` and a non-empty `result`; a non-zero exit, a
  missing event or an empty result declines. In `-p` mode a tool that needs
  permission is refused, never asked: no permission prompt is answered. The
  user's Claude Code hooks run as in any `claude -p` (measured 2026-11-13:
  SessionStart hooks fired); unlike the Codex run there is no flag here that
  turns them off.
- A timeout or a relay stop kills the child's group and declines.

The claim ends only through one transaction that writes the reply (sender
`relay:<machine>`, over 8192 bytes truncated with a marker pointing to
`read_session`), the terminal state, the cleared `claimed_by` and the deleted
lease row together.

Orphan recovery, at relay start and on every maintenance sweep (5 s). A lease
is an orphan when another relay instance holds it, when it expired (a runner
stops renewing past its deadline), or when its recorded child is gone and no
runner of this relay owns it. A recorded child that is alive with the same
pid AND create time is stopped (SIGTERM to its group, a grace period,
SIGKILL) and must be gone before the claim is cleared; a pid whose create
time differs is never signalled. A `spawning` lease with no recorded pid is
matched to this user's processes by the environment tag. A child is never
adopted: its output pipe belonged to the dead relay. The message is declined
with the reason: "relay restarted before the run started", "relay restarted
during the run; it was stopped", "the run's result was lost; for Codex the
turn may have run, read_session shows it", or "the run stopped renewing its
lease past its deadline; it was stopped".

What a `/direct` client (the llm-kit plugin) needs for its `continue` verb:
the client verb `continue` (`client continue <address> --kind K --body B
[--id ID] [--expires-in S] [--task T]`, same `--session` and `--grant` as the
other client verbs), a grant for `continue_session` whose single target is the
full address, and `common.json`'s widened enums vendored.

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
- A scanned Codex thread (one no channel registered) is `idle`, `ended` or,
  while some process holds its `thread-writer-locks/<id>.lock` open,
  `live-read-only`: a live Codex writer is observable through that lock, and
  never `live-accepting` and never queued (`not_opted_in`). A lock file that
  exists but is held by no process does not count. Only a channel registration
  (amendment 1, section 1) makes a Codex session `live-accepting`.
- `read_session` may add `skipped_lines`, the count of transcript lines in the
  scanned window that are not JSON.
