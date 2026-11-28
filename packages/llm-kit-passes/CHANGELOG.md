# Changelog: @katsoft/llm-kit-passes

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

## 0.2.0

Adds a generic syntax registry, its check, and an audience filter over a scanner the caller supplies. 0.1.0 is unchanged.

Added:

- `checkSyntaxRegistry(registry, policy)`: checks a `SyntaxRegistry` (`kinds`, `audiences`, `forms` of `{id, op, kind}`) and its `SyntaxPolicy` (the kinds each audience does not see). It returns the problems as `{code, subject, line}` in a fixed order, an empty list when sound: a kind or audience listed twice, a form without a non-empty string `id`, `op` and `kind`, a form of an unknown kind, an id used by two forms, and a policy that misses an audience, names an unknown audience, has an entry that is not a list, or hides an unknown kind. It throws a `TypeError` only when `registry` is not an object with the three lists or `policy` is not an object.
- `audienceFilter({scan, policy})`: gives `(stream, audience) => string`. The caller's `scan` returns every recognized form as a `{start, end, kind}` span, in order and not overlapping. The filter removes each span whose kind the policy hides from the audience and copies all other text byte for byte. The whole-line rule: a removed span that fills a whole line (it starts at the stream start or after a `"\n"`, and ends at the stream end or before a `"\n"`) also removes one separator, the `"\n"` after it or, on the last line, the `"\n"` that ends the output so far. An audience that hides nothing gets the stream as is, and the scanner is not called. A `TypeError` for bad options, an audience with no policy entry, a stream that is not a string, or a bad span.
- Types: `SyntaxForm`, `SyntaxRegistry`, `SyntaxPolicy`, `SyntaxProblemCode`, `SyntaxProblem`, `SyntaxSpan`, `SyntaxScan`, `AudienceFilterOptions`, `AudienceFilter`.

Quality bar:

1. Public API documented: pass. Each new export has a JSDoc block; the two functions document `@param`, `@returns` and `@throws`. `public-surface.json` lists the 31 names and `test/surface.test.mjs` checks the list against the runtime and `.d.ts` exports.
2. Semver release with a changelog: pass. Version 0.2.0 (a minor version: new exports only) in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/syntax.test.mjs` calls `checkSyntaxRegistry` (a sound registry; repeated ids, unknown kinds and bad forms reported without a throw; policy problems; repeated kinds and audiences; `TypeError` for bad arguments) and `audienceFilter` (spans inside a line; an unclosed span; the whole-line rule for the first, middle and last line and for two lines in a row; spans that share a line; an audience that hides nothing, with no scan; an iterable scanner; `TypeError` for bad options, audiences, streams and spans). The 0.1.0 tests are unchanged. `npm test` passes.
4. No client names, prompts or data: pass. The registry, the policy, the kinds, the audiences and the scanner are caller data; the tests and the example use a neutral markup. A reviewer read each public name, the README, the tests and the examples for client names, prompts and data.
5. Examples run without a client app: pass. `npm run check-pack` installs the packed tarball in a new directory, runs `examples/check-and-gather.mjs` and `examples/filter-by-audience.mjs` with plain `node`, and compiles `scripts/consumer.ts`, which now uses every new name, against the shipped types.
6. JSON Schema for each file format: pass (none). The package reads and writes no file; the registry and the policy are in-memory values.
7. Gate before adoption: pass. Lines 1 to 6 and 8 pass.
8. Security properties are tested: pass (none claimed). The filter is a text transform the caller's policy drives; the package claims no security property for it.

## 0.1.0

The first version: two building blocks for LLM passes over an injected transport.

Added:

- `submitToolPass(options)`: one forced tool call, the caller's synchronous `check(args, state, attempt)`, and a resubmit with the check's problems, up to `maxResubmits` (default 1). The pass owns one conversation; after each checked attempt it appends `{role: "assistant", content: text || null, tool_calls: [{id, type: "function", function: {name, arguments}}]}` and `{role: "tool", tool_call_id: id, content}`, with content `"accepted"` or `feedback(state, problems)` (default `JSON.stringify({errors})` of the problem lines). It resubmits only for problems: never after a transport failure, an answer that is not one call of the tool, or the deadline. One `deadlineMs` covers every attempt; at the deadline the attempt's signal aborts (`TimeoutError`) and a later answer is dropped. The result is `{status: "accepted" | "rejected" | "timeout" | "unavailable", state, problems, attempts, messages, error?}`. The caller's `signal` rejects the pass with its reason.
- `spreadGather(items, run, {deadlineMs, startedAt?, signal?})`: every item at once under one AbortController and one deadline; `{results, timedOut}` with one entry for each item in input order (`fulfilled`, `rejected` or `timeout`). Output after the deadline is dropped. With no time left at the start, no item runs.
- Types: `PassMessage`, `PassTool`, `PassToolCall`, `PassCallInput`, `PassCallOutput`, `PassCall`, `PassProblem`, `PassChecked`, `PassCheck`, `ToolPassAttempt`, `ToolPassOptions`, `ToolPassStatus`, `ToolPassResult`, `SpreadContext`, `SpreadRun`, `SpreadOptions`, `SpreadEntry`, `SpreadResult`.

Quality bar:

1. Public API documented: pass. Each export has a JSDoc block; the two functions document `@param`, `@returns` and `@throws`. `public-surface.json` lists the 20 names and `test/surface.test.mjs` checks the list against the runtime and `.d.ts` exports.
2. Semver release with a changelog: pass. Version 0.1.0 in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/tool-pass.test.mjs` (accept on attempt 1; one resubmit with the exact bytes of the assistant and tool messages; custom feedback; arguments that are not JSON; rejection and `maxResubmits`; no resubmit after a transport failure or a wrong answer; one deadline over both attempts with the late answer dropped; a transport the deadline aborted; the caller's abort; `onAttempt`; bad options). `test/spread.test.mjs` (all items at once with one signal, input order; a failed item in place; the deadline with late output dropped; `startedAt`; the caller's abort; no items and bad options). `npm test` passes.
4. No client names, prompts or data: pass. The tool, the check, the feedback, the state and the transport are caller data; a reviewer read each public name, the README and the example for client names, prompts and data.
5. Examples run without a client app: pass. `examples/check-and-gather.mjs` runs against a local stub transport; `npm run check-pack` installs the packed tarball in a new directory, runs it with plain `node`, and compiles `scripts/consumer.ts` against the shipped types.
6. JSON Schema for each file format: pass (none). The package reads and writes no file; the messages it appends are the transport's wire messages.
7. Gate before adoption: pass. Lines 1 to 6 and 8 pass.
8. Security properties are tested: pass (none claimed). The package sends nothing, reads no key and stores nothing.
