# Changelog: @katsoft/llm-kit-send

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

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
