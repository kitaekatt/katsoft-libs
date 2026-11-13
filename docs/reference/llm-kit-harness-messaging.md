# llm-kit harness messaging: architecture, authority and operation

This document records the design of harness messaging: how a remote Claude (the
claude.ai app) and a local Claude Code session list, read and message the
owner's running Claude Code and Codex sessions and the local LLM across a
fleet of machines. It holds the topology, the authority model, the accepted
risks, the decisions with their reasons, and the operating procedures.

The wire contract is not repeated here. It lives in
[llm-kit-relay-protocol.md](llm-kit-relay-protocol.md) (`relay-protocol/1`,
with amendments 1 to 3); this document cites it by section. The hub's OAuth
server is in [llm-kit-relay-hub-oauth.md](llm-kit-relay-hub-oauth.md). The
library source is `packages/katsoft-llm-kit/src/llm_kit/relay/`. The Claude Code
channel server, the `/direct` skill and its grant hooks, and the `clauder
--relay` launcher option ship in the plugins-kit repository (the llm-kit and
relaunch-kit plugins), not here.

## 1. Purpose and scope

A remote Claude can define work (task folders in a git repository, outside this
design), and with this feature it can also steer it: list sessions across
machines, read one, send a message, read the reply, answer a pending operator
question, and send a prompt to the local LLM. A Claude Code session on any
machine has the same reach through the user-invocable-only `/direct` skill.

Messaging carries how work is going (questions, nudges, course changes). Anything
that must survive belongs in the task record, not in a message.

Out of scope, and not planned: starting or stopping sessions remotely, shell
access, file transfer, and answering a session's permission prompt. Resuming an
idle session is supported only as an opt-in (section 6).

## 2. Topology

```
 claude.ai --HTTPS (MCP, OAuth 2.1)--> HUB (one machine, behind Tailscale Funnel)
                                        |
                 loopback / tailnet only |
        +-------------------------------+------------------------------+
        v                                                              v
 RELAY on the hub machine                              RELAY on each other machine
 127.0.0.1:4710                                        127.0.0.1:4710 (tailscale serve
   |- Claude channel server per session (stdio)          terminates HTTPS on the tailnet)
   |- Codex channel (`llm-kit relay codex`)
   `- local LLM through the relay's own endpoint registry
```

- Relay: one per machine. It owns that machine's sessions: discovery, a durable
  per-session mailbox (SQLite), delivery, replies, the pause switch, the audit
  log and the proxy to the local LLM. Default port 4710, loopback only.
- Hub: one per fleet. It is the single MCP server claude.ai connects to and the
  only process reachable from outside the tailnet. It fans `list_sessions` out
  to every relay and routes every other call to the relay named in the address.
  Default port 4711, loopback only, published by Tailscale Funnel on one HTTPS
  path. The hub is unavailable while its machine sleeps.
- Channel servers: a Claude Code session loads a stdio MCP server that registers
  the session, receives pushed messages while idle at the prompt, and exposes a
  reply tool. A Codex session accepts messages only through `llm-kit relay
  codex --cd DIR`, which owns a `codex app-server` child over stdio.
- Address: `<machine>/<harness>/<session>` (protocol section 7). The machine is
  the fleet label, not a raw hostname.
- Nothing binds a non-loopback address. Remote ingress is Tailscale's: Funnel
  for the hub, `tailscale serve` (tailnet only, never Funnel) for a peer relay.

Why one relay per machine: sessions, transcripts, process tables and the local
model live on the machine, and the split keeps a dead machine from taking the
others down.

### Cross-machine reach

Two paths reach a relay on another machine, and both use the same credential:
a per-peer key that the receiving relay issued to the calling machine.

- Hub path: the hub calls each configured peer relay (`relay.peers` in the
  hub machine's `relay.yaml`).
- `/direct` path (forwarding, protocol section 20 "Forwarding"): the client
  talks only to its own machine's relay on loopback, which forwards to the
  peer. No claude.ai token or Funnel is involved, so `/direct` works while the
  hub or the internet is down, provided the tailnet is up.

A configured machine that cannot be reached is listed as unreachable, never
omitted.

### Session delivery by harness

| Harness | Delivery | Notes |
|---|---|---|
| Claude Code | Channel server, loaded only when the session was launched with `clauder --relay` | Per-session opt-in; a session that never opted in refuses a send with `not_opted_in` |
| Codex | `llm-kit relay codex --cd DIR`: a message becomes `turn/start` on the thread the channel owns | Hand-started Codex TUIs stay read-only |
| Idle session | `continue_session` (opt-in, section 6) | One non-interactive resumed turn |
| Local LLM | The relay proxies to its own endpoint registry (never to a front door that can fail over off the machine) | `ask_llm` |

## 3. Authority model

Decision (owner rulings 2026-11-03, 2026-11-11, 2026-11-12). A message whose
origin is `claude.ai/<user>` (it came through the hub) or `direct:<addr>` (a
Claude Code session on the owner's machine where the owner typed `/direct`) is
the owner's own words, with the authority of a prompt typed in the receiving
session, gated actions included. A message from any other origin is another
session's request and is weighed as an untrusted request. The channel server
states this rule, with the accepted risks below, to the receiving session on
every message. Normative text: protocol section 3.

Reason: the owner steers sessions from a phone. If remote messages were
advisory, every gated action would need the owner at a keyboard, which defeats
the feature. The earlier advisory ruling (2026-11-03) was replaced on
2026-11-11.

Other structural rules:

- Opt-in per session. Only a session launched with `clauder --relay` (or a Codex
  channel the owner started) accepts messages; the dev-channels dialog appears
  on every such launch, which is the consent moment. Everything else is
  read-only.
- The session's own permission mode and allowlists remain the gate for what a
  message makes the model do. Nothing in the hub or relay ever answers a
  session's permission prompt.
- Provenance on every message: source, sender, message id and hop count.
  Relays refuse a hop count above 1, so a session cannot bounce a message
  onward.
- Hub writes need no grant. A hub send is accepted only by an opted-in session;
  a hub answer may answer any pending question.
- `/direct` is unreachable by the model on its own. The skill sets
  `disable-model-invocation: true` and `user-invocable: true`, and a PreToolUse
  gate allows one plain call of the plugin's own `llm-kit` binary and mints
  that call's grant (single use, 120 seconds, bound to session, verb and
  targets). A relay call that skipped the gate is refused `grant_required`.
  Typing `/direct` once enables every `/direct` verb in that session until
  SessionEnd (exit or `/clear`); there is no expiry, use count or prompt.
  A `!` preload never interpolates the skill's arguments (they are substituted
  as raw text before the shell runs, which allows injection); the grant is
  written by the client CLI from the verified arguments. Never weaken the
  frontmatter or the gate to make a test or demo pass.
- Reads are redacted: `read_session` returns a capped tail, strips known
  secret shapes and does not return tool output from secrets paths. Session
  output returned by the hub is data, not instructions, and the hub's tool
  descriptions say so.
- Kill switch, limits and audit: `llm-kit relay pause` stops delivery on a
  machine within one heartbeat; message size caps, per-session and global rate
  limits and message expiry apply; every call and delivery is an append-only
  audit row (protocol sections 15 to 17).

### Alternative A and alternative B

Grants are minted by the hook (alternative A, chosen): a guardrail against
accidental model-initiated calls, not a security boundary against a same-user
model. Alternative B, a separate OS user plus a hardware-backed signature
(Touch ID), is reserved in the protocol (`origin_proof`, `signed`, protocol
section 3 "Extension points for alternative B") and is a later option.

## 4. Accepted risks

Recorded as the owner's decision (2026-11-11), stated in protocol section 3
and repeated to receiving sessions:

- Any process that can read a machine's relay token and the hub bearer can send
  a `claude.ai/` message, which carries the owner's authority.
- Any process that can read the relay token can send a `direct:` message as any
  session on that machine without the owner typing `/direct`.
- The Claude in a `/direct` session can send a message the owner did not write.
- Cross-machine: a machine that holds a peer key for another machine (the hub
  machine and each forwarding source) can send owner-authority messages to
  opted-in sessions there. A process on that machine that reads the key can do
  the same.
- Resume (section 6) widens reach to every once-opted-in idle session on a
  machine that enables it.
- Tailnet policy: with the default allow-all grant, a port-scoped grant
  restricts nothing, so a peer relay's protection is its per-machine peer key,
  not the network. Tagging a machine and granting only the ports it needs is a
  later option; the tailnet policy lives in the Tailscale admin console.

Design deviation, with its reason: peer relays do not check the caller with
`tailscale whois`, because `tailscale serve` hides the source node.
Per-peer credentials replace that check.

## 5. Decisions with reasons

| Decision | Reason |
|---|---|
| Hub exposed with Tailscale Funnel only; relays never exposed | The owner prefers Tailscale to an open port; claude.ai calls the connector from Anthropic's servers, so a tailnet-only address is invisible to it |
| A git mailbox in the tasks repository stays the degraded fallback when the hub is down | Zero exposed surface; minutes of latency |
| Hub OAuth 2.1 for one user (CIMD and dynamic client registration, PKCE); a new connection needs a one-time owner approval code | claude.ai's custom-connector flow expects OAuth; a Funnel URL is not a secret. See llm-kit-relay-hub-oauth.md |
| Opt-in is a launcher option (`clauder --relay`), per session, never default | The choice of command is the consent; it survives `/restart` |
| A hidden development-channels flag loads the channel server | Custom channel servers load only through it (measured on Claude Code 2.1.317); the reply tool is allow-listed so an unattended session does not stall on a prompt |
| Codex: only sessions the owner starts through `llm-kit relay codex` accept messages | Codex has no channel mechanism; the app-server connection that owns the thread is the only inbound path |
| Codex turns run sandboxed: `workspace-write` rooted at the absolute `--cd`, network off, approval `never`, `/tmp` and `$TMPDIR` not writable, hooks off; `danger-full-access` and bypass refused | A remotely triggered turn never runs unsandboxed. Codex dispatch fails silently at exit 0 on misconfiguration, so every path is absolute and the app-server's reported settings are compared with the request before a turn runs |
| An approval or input request from Codex in a relay turn is cancelled and the turn interrupted | Fail-safe reading of "never answer a permission prompt remotely" |
| The local LLM endpoint for `ask_llm` is the machine's own registry entry, not a front door | The front door can fail over off the machine |
| 5090W sessions run natively on Windows, so its relay runs on the Windows host, not in WSL | The WSL guest holds the model server, not sessions; the host reaches the guest's loopback ports |
| Peer credentials are per-peer keys issued by the receiver (`relay.accept_from`, `peer export --for`) | A shared token reused across peers made one machine's secret grant the local principal everywhere |
| `relay.peers` and `relay.accept_from` nest inside the `relay` block; no new top-level `relay.yaml` key | The installed plugin's stdlib opt-in reader refuses unknown top-level keys, which would break running hooks |
| Remote resume off by default (`resume.enabled`), Claude permission mode `default` only | Owner ruling 2026-11-13: resume widens reach to idle sessions, so it is explicit and never permissive |
| Relay and hub off unless `relay.yaml` sets `enabled: true` | Nothing runs or opens a listener without opt-in |
| `llm_kit.relay` is a component at the jobs layer behind the optional `relay` extra | Approved addition to the signed component graph (2026-11-03) |

## 6. Operating procedures

Configuration is `$XDG_CONFIG_HOME/llm-kit/relay.yaml` (`~/.config/llm-kit/`
by default); state, tokens and the mailbox are under
`$XDG_STATE_HOME/llm-kit/relay`. Keys and validation: protocol section 20.

### Start a machine

1. Write `relay.yaml` with `enabled: true` and, for `ask_llm`, an
   `llm_endpoints` entry for the machine's local model server (loopback).
2. `llm-kit relay serve` (foreground; run it under a process supervisor or a
   terminal multiplexer).
3. On the hub machine only: `llm-kit relay hub`, then expose its path with
   Tailscale Funnel (with a TTL when testing) and add the connector URL to
   claude.ai.
4. `llm-kit relay status` shows pause state, forwarding and resume state, and
   peers.

Launch a Claude Code session that accepts messages with `clauder --relay`.
Launch a Codex session that accepts messages with `llm-kit relay codex --cd DIR`
(`--resume THREAD` continues one).

### Pair a machine (example: 5090W reached from m5pro, which runs the hub)

The receiver is the machine being reached (5090W); the source is the machine
that reaches it (m5pro). The library release on each machine must know the keys
before they are written, because older releases refuse unknown `relay` keys.

1. Receiver: install katsoft-llm-kit 1.21 or later. On Windows, run the relay
   natively and publish it on the tailnet with
   `tailscale serve --https=4710 http://127.0.0.1:4710` (tailnet only).
2. Receiver `relay.yaml`: `relay: {accept_from: [m5pro]}`. Each label must be an
   enabled machine in the fleet registry, other than the receiver itself.
3. Restart the receiver's relay.
4. Source: `ssh <receiver> llm-kit relay peer export --for m5pro | llm-kit relay
   peer import <receiver>`. Standard output must not be a terminal; the key
   never appears in a file you write or a transcript. The source's hub and
   relay use the same stored key.
5. Source `relay.yaml`: `relay: {peers: [{machine: <receiver>, url:
   https://<receiver>.<tailnet>.ts.net:4710}]}`. A non-loopback URL must be
   https; a missing key stops the hub and relay at start.
6. Restart the source's relay and hub (never the owner's Claude Code client).
7. Check: `llm-kit relay status` lists the peer as `stored`, and a hub
   `list_sessions` includes the receiver's sessions.

For the other direction, repeat with the roles swapped.

### Rotate credentials

Run `llm-kit relay rotate-credentials` on a machine whose relay token or hub
bearer may have been exposed, and always on a machine that ran `peer export`
before 1.21 (the old export carried the general token and bearer). The command
prints which consumers to restart (`llm-kit relay serve`, `llm-kit relay hub`,
each Codex channel), whether the claude.ai connector must reconnect (a hub
without `hub.public_url` signs in with the bearer), and which consumers read
the token on every call and need nothing. A running relay and hub accept the old
values until they restart. On a machine that imported old credentials, the relay
and hub refuse to start while `peers/<machine>/relay.token` or `hub.bearer`
exists; delete them as the message says.

To replace one peer key, re-run `peer export --for <M>` on the receiver after
removing the issued key, then re-import on the source and restart both.

### Enable remote resume

Off by default. To allow `continue_session` on a machine, set in `relay.yaml`
`resume: {enabled: true}` (optional `timeout_s`; `claude_permission_mode`
accepts only `default`) alongside the top-level `enabled: true`. The relay reads
the file on every call, so setting it back to false takes effect at once. Only a
session that registered through a channel at least once and is now idle can be
resumed. Eligibility, claims, the lease and the residual race with an
interactive `claude --resume`: protocol section 20 "Remote resume".

### Pause

`llm-kit relay pause` and `llm-kit relay resume` set and clear the relay pause
flag on one machine (`--offline` writes the pause file directly); a hub pause
refuses every hub tool call and does not affect `/direct`. Effects: protocol
section 16.

## 7. Verification state

The feature was live-tested end to end on 2026-11-12: claude.ai chat, Duet and a
`/direct` session each messaged a relay session, which treated the message as
the owner's words, and a session that never typed `/direct` was refused. Codex
relay acceptance passed on codex-cli 0.164.0.

Each acceptance criterion is covered by an automated test in `tests/llm-kit/`
and is shown to fail with its fix reverted:

- `list_sessions` shows every live Claude Code and Codex session with correct
  state and marks unreachable machines.
- A message to an idle opted-in session is acted on and its reply is readable;
  resending a message id is not delivered twice.
- A message to a session that did not opt in is refused with a reason, never
  queued silently.
- `ask_llm` returns a completion through the relay.
- The model cannot invoke `/direct`; the frontmatter is pinned by a test, and a
  send without a live grant is refused by the hook and the client, and a grant
  is consumed by its use.
- A hop count above 1 is refused.
- A job interrupt appears in `list_questions`; answering it produces the same
  validated resolution as `llm-kit jobs resolve`.
- Every hub request without a valid token is refused; no relay port is reachable
  from outside the tailnet.
- `relay pause` stops delivery within one heartbeat; the audit ledger records
  every call with its identity.

## 8. Known limits

- A hand-started Codex TUI is read-only. Attaching a TUI to a relay-launched
  session is optional and unbuilt.
- WSL sessions on 5090W are not reachable (none run there); adding them needs a
  fleet label for the guest and distinct ports.
- A Claude session reopened interactively after a resume claim can race the run
  (documented residual race, protocol section 20).
- The Codex second-SIGTERM test is load-sensitive under `-n 8`.
