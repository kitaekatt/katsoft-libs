# @katsoft/llm-kit-passes

Building blocks for LLM passes over a transport you supply, and an audience filter for a markup format you supply:

- `submitToolPass`: send a conversation with one tool forced, give the tool call's arguments to your check, and
  resubmit with the check's problems once (or `maxResubmits` times). One deadline covers every attempt. The pass owns
  its conversation and appends the OpenAI chat-completions messages for each attempt.
- `spreadGather`: run many calls at once under one AbortController and one deadline, and gather the results in input
  order. Output that comes after the deadline is dropped.
- `checkSyntaxRegistry` and `audienceFilter`: check a format's registry of forms and kinds against its audience policy,
  and remove from a text every form that an audience must not see.

The package sends nothing itself and has no prompts, models or formats of its own. Your `call` sends the request (for
example with `@katsoft/llm-kit-send`), and your `check` decides what a good answer is.

Node 24 or later. No runtime dependency.

```sh
npm install @katsoft/llm-kit-passes
```

## A checked tool pass

```js
import { submitToolPass } from "@katsoft/llm-kit-passes";

const result = await submitToolPass({
  messages: [{ role: "system", content: "Answer with the submit tool." }, { role: "user", content: "Lisbon?" }],
  tool: { name: "submit", description: "Submit the answer.", schema: { type: "object", properties: { country: { type: "string" } } } },
  // Your transport: send `messages` with `tool` forced (`toolChoice` is its name); answer {text, toolCalls}.
  call: async ({ messages, tool, toolChoice, signal, attempt }) => myTransport({ messages, tool, toolChoice, signal }),
  // Your check: the parsed arguments (null when not JSON) and the state; answer the new state and the problems.
  check: (args, state) => ({ state: args, problems: args?.country ? [] : [{ id: "country", line: "country: required" }] }),
  state: null,
  deadlineMs: 20_000,
});
// result.status: "accepted" | "rejected" | "timeout" | "unavailable"
```

The rules:

- The pass resubmits only when the check finds problems. A transport failure, an answer that is not exactly one call of
  the tool (with an id), or the deadline ends the pass at once.
- After each checked attempt the pass appends
  `{role: "assistant", content: text || null, tool_calls: [{id, type: "function", function: {name, arguments}}]}` and
  `{role: "tool", tool_call_id: id, content}`. The content is `"accepted"`, or `feedback(state, problems)` (default
  `JSON.stringify({errors: problems.map(p => p.line)})`).
- One deadline (`deadlineMs`) covers every attempt. At the deadline the attempt's signal aborts with a `TimeoutError`,
  and an answer that comes later is dropped.
- The caller's `signal` stops the pass: the promise rejects with `signal.reason`.

## A spread-gather

```js
import { spreadGather } from "@katsoft/llm-kit-passes";

const { results, timedOut } = await spreadGather(items, (item, { signal, index }) => work(item, signal), { deadlineMs: 30_000 });
// results[i]: {status: "fulfilled", value} | {status: "rejected", reason} | {status: "timeout"}
```

All items start at once and share one signal. `startedAt` (a `Date.now()` value) counts the deadline from an earlier
start; when no time is left, no item runs.

## A syntax registry and an audience filter

```js
import { audienceFilter, checkSyntaxRegistry } from "@katsoft/llm-kit-passes";

const registry = {
  kinds: ["visible", "review", "secret"],
  audiences: ["reader", "reviewer", "owner"],
  forms: [{ id: "em", op: "em", kind: "visible" }, { id: "note", op: "note", kind: "review" }, { id: "key", op: "key", kind: "secret" }],
};
// The kinds each audience does not see.
const policy = { reader: ["review", "secret"], reviewer: ["secret"], owner: [] };
checkSyntaxRegistry(registry, policy); // [] when sound; else [{code, subject, line}, ...]

// Your scanner: every form it recognizes, as {start, end, kind}, in order and not overlapping.
const filter = audienceFilter({ scan: (text) => myScanner(text), policy });
filter("Hi <<note:check>>\n<<key:1234>>", "reader"); // "Hi "
```

The rules:

- `checkSyntaxRegistry` reports, and does not throw for, a kind or audience listed twice, a form without a string
  `id`, `op` and `kind`, a form of an unknown kind, two forms with one id, and a policy that misses an audience, names an
  unknown one or hides an unknown kind. It throws a `TypeError` only when its arguments are not a registry and a policy.
- The filter removes each span whose kind the policy hides from the audience, and copies all other text as it is. Your
  scanner also gives the spans every audience sees, so that the text inside them is not scanned again.
- The whole-line rule: a removed span that fills a whole line also removes one `"\n"`: the one after it, or, on the
  last line, the one before it. A span that shares its line with other text removes no separator.
- An audience that hides nothing gets the text as is, and the scanner is not called. An audience with no policy entry
  is a `TypeError`.

## Examples

With plain `node`, after the package is installed:

- `examples/check-and-gather.mjs` runs three checked passes at once against a local stub transport.
- `examples/filter-by-audience.mjs` checks a small registry and prints one text for three audiences.

## Release

See `docs/reference/package-release.md`, "npm packages", in the repository.
