# Changelog: @katsoft/llm-kit-send

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

## 0.4.0

The call ledger: every call that can be billed is told to one sink per process, before it is sent and when it ends (no 0.3.0 name, signature or request byte changes; results gain fields, below).

Added:

- `setCallLedger(sink | null)`: one sink per process, kept at `globalThis[Symbol.for("katsoft.llm-kit-send.callLedger")]`. The sink is `{begin(record), end(record)}`; `begin` runs synchronously, after `encode`, `beforeSend` and a cache miss, in the caller's async context, before the first byte leaves. The records are `llm-kit.paid-call/v1` (`PAID_CALL_FORMAT`, types `PaidCallBegin` and `PaidCallEnd`): transport, method, host, path with no query, model, `stream`, `attempt`, labels; then `ok`, `status`, `errorKind`, `aborted`, `replayed`, served model and host, `generationId`, the raw `usage`, the token counts and the reported `cost`. No prompt or reply text, no query string, no key.
- `activeCall()`: the id of the running call (a package `AsyncLocalStorage`, shared by every copy of the package in the process), set also with no sink, for a fetch guard. `requireCallLedger(on)` and the environment variable `LLM_KIT_CALL_LEDGER=required`: a call with no sink throws `CallLedgerError` (code `LLMKIT_NO_CALL_LEDGER`) before anything is sent. With no sink and the mode off, the first call gives one process warning `LLMKIT_NO_CALL_LEDGER` and every call goes on. A sink that throws, or returns a rejected promise, is a process warning `LLMKIT_CALL_LEDGER_SINK_ERROR` and the call goes on.
- The `labels` option (`Record<string, string | number | null>`) on every transport (`PostOptions`, `SendOptions`, `RetryOptions`, `MediaOptions`); a bad label is a `TypeError` before any send.
- One call for each HTTP send: a retry of `sendWithRetries` and the reasoning-off retry of `sendChat` are each a call (`attempt`). `sendChat` and `sendWithRetries` end the call when the answer is read; a stream ends in a `finally` with the chunk `id` as `generationId`, also when it stalled or was cut. `postImages` and `ideogramEdit` end after the answer is parsed. `postChat`, `postVenice`, `postFal` and `elevenLabsRequest` return the raw response and end the call when its body is read to its end, fails or is cancelled; the body passes through a bounded tap that gives the caller the same status, status text, headers, `url` and chunks, read only as the caller reads, and reads the usage, generation id, model and host from the bytes that pass (JSON up to 1 MiB; an event stream line by line). A body never read leaves the call open. `falQueue` and `sunraQueue` are one call for each job from submit to result (`errorKind` `<stage>:<reason>`); the polls run inside the call and are not recorded. A stored answer of the reply cache is a call with `replayed: true`, cost 0 and no usage. A GET, HEAD or DELETE of `elevenLabsRequest` and `fetchFile` are not recorded and run outside any call.

Changed:

- `ChatAnswer` gains `callId` and `generationId`; `SendError` gains `callId` and `generationId` (both `null` when the error came before any call). `postFal` and `elevenLabsRequest` are `async` functions: a bad label or a required ledger with no sink is a rejected promise, not a synchronous throw.
- `falQueue` and `sunraQueue` can throw (`CallLedgerError`, `TypeError`) before the submit; they still never throw for an abort or a lost connection.
- The package test script passes `--disable-warning=LLMKIT_NO_CALL_LEDGER`, so the 0.3.0 tests (which install no sink) do not print the warning; the process event still fires and `test/ledger.test.mjs` asserts it.

Quality bar for the pieces above:

1. Public API documented: pass. Each new export (`setCallLedger`, `requireCallLedger`, `activeCall`, `CallLedgerError`, `PAID_CALL_FORMAT`, `CallLedgerSink`, `PaidCallBegin`, `PaidCallEnd`, `CallLabels`) has a JSDoc block with `@param`, `@returns` and `@throws`; the transports' blocks state the ledger behavior and the new errors; `public-surface.json` lists the names and `test/surface.test.mjs` checks the list against the runtime and `.d.ts` exports.
2. Semver release with a changelog: pass. Version 0.4.0 (minor: additions, and two result fields) in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/ledger.test.mjs` (begin before the send and after `encode` and `beforeSend`, in the caller's context; the sink slot; the single warning; required mode never calls the stub; a throwing sink; `activeCall` during and after, with two calls at once; the labels; no prompt, key or query in any record), `test/ledger-chat.test.mjs` (one pair for each send; the reasoning-off retry; each `sendWithRetries` try; a stalled stream with `generationId` on the end and on `SendError`; a replay; the raw `postChat` end and the passthrough tests: status, headers, `url` and chunks unchanged, no read ahead, clone, a body over the bound, an event stream, bytes), `test/ledger-media.test.mjs` and `test/ledger-queue.test.mjs` (each media transport and each queue failure stage; GET, DELETE and `fetchFile` not recorded; polls inside the call). `npm test` passes.
4. No client names, prompts or data: pass. Label keys, hosts, accounts and the sink are caller data.
5. Examples run without a client app: pass. `examples/call-ledger.mjs` runs against a local stub server; `npm run check-pack` runs every example from the packed tarball and compiles `scripts/consumer.ts` (the ledger types and functions included) against the shipped types.
6. JSON Schema for each file format: pass. `llm-kit.paid-call/v1` (`paid-call.v1.schema.json`, an `anyOf` of the `begin` and `end` records) in `packages/katsoft-llm-kit/src/llm_kit/schema/formats/`, with 22 shared cases (`tests/formats/paid-call/cases.json`) that `test/paid-call-format.test.mjs` and `tests/llm-kit/test_paid_call_format.py` both run; `test/ledger*.test.mjs` validate every record their sink gets against the schema.
7. Gate before adoption: pass. Lines 1 to 6 and 8 pass.
8. Security properties are tested: pass. No prompt text, reply text, key, user information, query string or fragment in a record (`test/ledger.test.mjs`, `test/ledger-media.test.mjs`); the required mode refuses before any send (`test/ledger.test.mjs`, `test/ledger-media.test.mjs`, `test/ledger-queue.test.mjs`); a bad label is refused before any send; a sink failure never fails a call.

## 0.3.0

Added (no 0.2.0 name or request byte changes; results gain one field, below):

- The reply cache, the `cache` option on every non-stream call: `sendChat` (plain only), `sendWithRetries`, `postImages` and `ideogramEdit` with the caller's `classify`; `postChat` and `postVenice` give a `CachedResponse` with `settle(verdict, {headersOnly})`. The key is computed after `beforeSend` and looked up before the send; a hit sends nothing. A stored refusal always replays; a success is stored and replayed only for a chat call whose sent body has `temperature` exactly 0 and that is not a `freshCheck`. Only complete, parsed, non-busy answers up to `maxBytes` (default 65536) are stored. The store is the caller's (`ReplyStore`, bound to one owner). A store failure is `ReplyCacheError` (op `get` or `put`): `sendWithRetries` rethrows it before its deadline result, `withFallback` before it asks `eligible`, and it is never a `SendError`; `onPutError` reports a failed write and keeps the answer. A cached call through a `withBackupKey` fetch, to a URL outside `hosts`, or with a streamed body throws `TypeError` before any send.
- `requestKey`, `stepKey` (format `llm-kit.reply-key/v1`, `REPLY_KEY_FORMAT`), `readEntry` and `ReplyEntryError` (format `llm-kit.reply-entry/v1`, `REPLY_ENTRY_FORMAT`); types `ReplyVerdict`, `ReplyLayer`, `ReplyStore`, `StepCache`, `ParsedStepCache`, `StepContext`, `ReplyEntry`, `WireRequest`, `CachedResponse`.

Changed:

- `replayed: boolean` on `ChatAnswer`, `SendError`, both arms of `RetryResult`, `ImagesAnswer` and `IdeogramAnswer`; `false` without the cache. A replay has cost 0 and no usage.
- `CallRecord` has `replayed` and is now `llm-kit.call-record/v2` (v1 plus the required `replayed`); v1 stays for older records. A hit of `sendWithRetries` gives one record with `replayed: true`, tokens null and cost 0.

Quality bar for the pieces above:

1. Public API documented: pass. Each new export has a JSDoc block with `@param`, `@returns` and `@throws`; `public-surface.json` lists them; `test/surface.test.mjs` checks the list against the runtime and `.d.ts` exports.
2. Semver release with a changelog: pass. Version 0.3.0 (minor: an added option and an added result field) in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/reply-cache.test.mjs` runs the shared key and entry cases (`tests/formats/reply-key/cases.json`, `tests/formats/reply-entry/cases.json`, the same files the Python reader runs) and each cached transport with a counting stub: a hit sends nothing, one added space sends, a success above or without temperature 0 and a second look send again, busy, unparsed, oversize and aborted answers store nothing, the headers arrive before the body, `ReplyCacheError` is not retried, not a fallback and not `network`; `test/call-record.test.mjs` validates every emitted record, a replayed one included, against `call-record.v2.schema.json`. `npm test` passes.
4. No client names, prompts or data: pass. Hosts, accounts, verdicts and the store are caller data.
5. Examples run without a client app: pass. `examples/reply-cache.mjs` runs against a local stub server; `npm run check-pack` runs every example from the packed tarball and compiles `scripts/consumer.ts` (the cache types included) against the shipped types.
6. JSON Schema for each file format: pass. `llm-kit.reply-entry/v1` (`reply-entry.v1.schema.json`) and `llm-kit.call-record/v2` (`call-record.v2.schema.json`) in `packages/katsoft-llm-kit/src/llm_kit/schema/formats/`, with shared cases the TypeScript and Python tests both run against the schemas (`test/reply-cache.test.mjs` also checks every entry its test store is given); the reply key has shared cases (`tests/formats/reply-key/cases.json`) and no schema (a key, not a file).
7. Gate before adoption: pass. Lines 1 to 6 pass.

## 0.2.0

Added (no 0.1.0 name, signature or behavior changes):

- Media transports: `postImages` (OpenAI-compatible images), `postVenice` and `isVeniceViolation`, `ideogramEdit`, `postFal`, `falQueue` (with the caller's `refusal` rule), `sunraQueue`, `elevenLabsRequest` and `elevenLabsError`, and `fetchFile` (a keyless download with an optional size limit). Each sends the body as given and keeps the request's header and init order. The base URLs `VENICE_URL`, `IDEOGRAM_URL`, `FAL_RUN_URL`, `FAL_QUEUE_URL`, `SUNRA_QUEUE_URL`, `ELEVENLABS_URL`.
- Fallback chains as data, format `llm-kit.fallback-chain/v1` (`FALLBACK_CHAIN_FORMAT`): `readFallbackChains` (throws `ChainFormatError` with the path), `mapInput` and `runChain` (stop host and cause, the last failure, attempts told the previous failure and the skipped hosts).
- The `CallRecord` of `sendWithRetries` has a JSON Schema, `llm-kit.call-record/v1`, in katsoft-llm-kit; the tests validate the emitted records against it. The record's shape does not change.

Quality bar for the pieces above:

1. Public API documented: pass. Each new export has a JSDoc block; `public-surface.json` lists them (kind `constant` for the base URLs and the format id); `test/surface.test.mjs` checks the list against the runtime and `.d.ts` exports and the `@param`, `@returns` and `@throws` tags of each function and class.
2. Semver release with a changelog: pass. Version 0.2.0 (minor: additions only) in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/media.test.mjs` (each transport's URL, init keys, headers and body exactly, each failure, abort and body that is not JSON), `test/queue.test.mjs` (each stage, code, `completed` and abort), `test/chain.test.mjs` (the shared cases of `tests/formats/fallback-chain/cases.json`, the same file the Python reader runs, and `runChain`), `test/call-record.test.mjs`; `npm test` passes.
4. No client names, prompts or data: pass. Hosts are named by their public APIs; endpoints, models, keys, rules and chains are caller data.
5. Examples run without a client app: pass. `examples/media-chain.mjs` runs against a local stub server; `npm run check-pack` installs the packed tarball in a new directory, runs every example with plain `node` and compiles `scripts/consumer.ts` against the shipped types.
6. JSON Schema for each file format: pass. `llm-kit.fallback-chain/v1` (`fallback-chain.v1.schema.json`) and `llm-kit.call-record/v1` (`call-record.v1.schema.json`) in `packages/katsoft-llm-kit/src/llm_kit/schema/formats/`, with shared cases that the TypeScript and Python tests both run.
7. Gate before adoption: pass. Lines 1 to 6 pass.

## 0.1.0

The first version, published by hand on 2026-11-17 from katsoft-libs `5527d9c4` (integrity `sha512-0MZBtA8i4L/I1WZP4a6M2b/vyqx9fR3+p96JQMWUH6eG8zkCgOx5oEN4ANQ34oZhxGTAiTr3Mj31kcfVY1q/wg==`). The tarball's copy of this file has the heading "Unreleased". It has no release tag (`docs/reference/package-release.md`, "npm packages").

Added:

- `sendChat`: one OpenAI-compatible chat call, plain or streamed, with stream limits (`firstMs` counts the wait for the headers, `gapMs` the silence after the first output, an optional pace floor), the content deltas (`onDelta`), tool calls put together from a stream (`onToolArguments`), `requireFinish`, `stopOnErrorFinish`, the reasoning-off retry (`reasoningRetry`), a check before sending (`beforeSend`) and the caller's `signal`. On a limit the request is aborted before the error, so two models never run at once.
- `postChat`: the raw POST, the untouched response of any status.
- `SendError`: the error kinds `http`, `network`, `body`, `no_first_token`, `stalled`, `slow`, `stream_error`, `incomplete` and `aborted`, the provider's status, code, message, host and reason, and `fallback`. `upstreamReason`.
- `withFallback`: one fallback before the first output, never on an abort.
- `sendWithRetries`, `retryWait`, `pause`, `isTransient`: a plain call with retries while the host is busy (jitter, Retry-After within a budget, one deadline) and one `CallRecord` per try that was sent.
- `withBackupKey`, `isKeyLimit`: a backup key for a host that refuses the main key for a spend limit or credits.
- `usageCost`, `tokenCost`: the reported cost, or a cost from a per-token price.

Quality bar for the pieces above:

1. Public API documented: pass. Each export has a JSDoc block; `public-surface.json` lists them; `test/surface.test.mjs` checks the list against the runtime and `.d.ts` exports and the `@param`, `@returns` and `@throws` tags of each function.
2. Semver release with a changelog: pass. Version 0.1.0 in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/send.test.mjs`, `fallback.test.mjs`, `retry.test.mjs`, `backup-key.test.mjs` and `cost.test.mjs` call each function; `npm test` passes.
4. No client names, prompts or data: pass. Hosts, keys, prices, budgets and waits are caller data.
5. Examples run without a client app: pass. `examples/stream.mjs` and `examples/retries.mjs` run against a local stub server; `npm run check-pack` installs the packed tarball in a new directory and runs them with plain `node`.
6. JSON Schema for each file format: not applicable. The package reads and writes no file; `CallRecord` is an in-process value. A schema comes when a record or a cache key is stored.
7. Gate before adoption: pass. Lines 1 to 6 pass.
