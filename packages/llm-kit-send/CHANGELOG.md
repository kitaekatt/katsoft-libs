# Changelog: @katsoft/llm-kit-send

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

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
