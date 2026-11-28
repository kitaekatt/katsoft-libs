# Changelog: @katsoft/llm-kit-passes

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

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
