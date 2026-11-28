// submitToolPass: the forced tool call, the check, one resubmit, the conversation bytes, failures, the deadline and abort.
import assert from "node:assert/strict";
import { test } from "node:test";

import { submitToolPass } from "../dist/index.js";
import { answer, deferred, scripted, sleep, untilAborted } from "./support.mjs";

const TOOL = { name: "submit", description: "Submit the answer for validation.", schema: { type: "object", properties: { items: { type: "array" } } } };
const START = [{ role: "system", content: "Rules." }, { role: "user", content: "{\"items\":[\"a\",\"b\"]}" }];

/** A check that wants every item to have `ok: true`; the state is the list of accepted ids. */
const check = (args, state) => {
  const items = Array.isArray(args?.items) ? args.items : null;
  if (!items) return { state, problems: [{ id: "*", line: "items: missing" }] };
  const problems = items.filter((item) => !item.ok).map((item) => ({ id: item.id, line: `${item.id}: not ok` }));
  return { state: [...state, ...items.filter((item) => item.ok).map((item) => item.id)], problems };
};

const run = (call, extra = {}) => submitToolPass({ messages: START, tool: TOOL, call, check, state: [], deadlineMs: 5_000, ...extra });

test("an answer the check accepts ends the pass after one call", async () => {
  const transport = scripted(answer({ items: [{ id: "a", ok: true }] }));
  const result = await run(transport.call);
  assert.equal(result.status, "accepted");
  assert.equal(result.attempts, 1);
  assert.deepEqual(result.state, ["a"]);
  assert.deepEqual(result.problems, []);
  assert.equal(transport.calls.length, 1);
  const [first] = transport.calls;
  assert.equal(first.attempt, 1);
  assert.equal(first.tool, TOOL);
  assert.equal(first.toolChoice, "submit");
  assert.ok(first.signal instanceof AbortSignal && !first.signal.aborted);
  assert.equal(first.sent, JSON.stringify(START));
  assert.equal(result.messages.at(-1).content, "accepted");
  assert.deepEqual(START.length, 2, "the caller's messages are not changed");
});

test("problems resubmit once with the assistant call and the tool answer in the exact wire shape", async () => {
  const transport = scripted(
    answer({ items: [{ id: "a", ok: true }, { id: "b", ok: false }] }, { id: "c-1" }),
    answer({ items: [{ id: "b", ok: true }] }, { id: "c-2", text: "Fixed." }),
  );
  const seen = [];
  const result = await run(transport.call, { check: (args, state, attempt) => { seen.push([attempt, state]); return check(args, state); } });
  assert.equal(result.status, "accepted");
  assert.equal(result.attempts, 2);
  assert.deepEqual(result.state, ["a", "b"]);
  assert.deepEqual(seen, [[1, []], [2, ["a"]]], "the check gets the attempt and the state of the attempt before");
  assert.deepEqual(transport.calls.map((call) => call.attempt), [1, 2]);
  // The bytes of the resubmitted conversation: an assistant message with one function tool call (content null for an
  // empty text), then the tool message with the feedback. Key order is part of the shape.
  const arguments1 = JSON.stringify({ items: [{ id: "a", ok: true }, { id: "b", ok: false }] });
  assert.equal(transport.calls[1].sent, JSON.stringify([
    ...START,
    { role: "assistant", content: null, tool_calls: [{ id: "c-1", type: "function", function: { name: "submit", arguments: arguments1 } }] },
    { role: "tool", tool_call_id: "c-1", content: "{\"errors\":[\"b: not ok\"]}" },
  ]));
  const [, , , , assistant2, tool2] = result.messages;
  assert.equal(JSON.stringify(assistant2), JSON.stringify({ role: "assistant", content: "Fixed.", tool_calls: [{ id: "c-2", type: "function", function: { name: "submit", arguments: JSON.stringify({ items: [{ id: "b", ok: true }] }) } }] }));
  assert.equal(JSON.stringify(tool2), JSON.stringify({ role: "tool", tool_call_id: "c-2", content: "accepted" }));
});

test("the feedback function writes the tool answer from the state and the problems", async () => {
  const transport = scripted(answer({ items: [{ id: "a", ok: true }, { id: "b" }] }), answer({ items: [{ id: "b", ok: true }] }));
  await run(transport.call, { feedback: (state, problems) => JSON.stringify({ kept: state, errors: problems.map((problem) => problem.line) }) });
  assert.equal(JSON.parse(transport.calls[1].sent).at(-1).content, "{\"kept\":[\"a\"],\"errors\":[\"b: not ok\"]}");
});

test("arguments that are not JSON reach the check as null and are resubmitted", async () => {
  const transport = scripted(answer("{not json"), answer({ items: [] }));
  const args = [];
  const result = await run(transport.call, { check: (value, state) => { args.push(value); return check(value, state); } });
  assert.deepEqual(args, [null, { items: [] }]);
  assert.equal(result.status, "accepted");
  assert.equal(result.attempts, 2);
});

test("problems after the last resubmit reject the pass; maxResubmits sets how many resubmits run", async () => {
  const bad = answer({ items: [{ id: "x" }] });
  const once = scripted(bad, bad, bad);
  const rejected = await run(once.call);
  assert.equal(rejected.status, "rejected");
  assert.equal(rejected.attempts, 2, "the default is one resubmit");
  assert.deepEqual(rejected.problems, [{ id: "x", line: "x: not ok" }]);
  assert.equal(rejected.messages.at(-1).content, "{\"errors\":[\"x: not ok\"]}");

  const none = scripted(bad);
  assert.equal((await run(none.call, { maxResubmits: 0 })).attempts, 1);
  const two = scripted(bad, bad, bad);
  assert.equal((await run(two.call, { maxResubmits: 2 })).attempts, 3);
});

test("a transport failure ends the pass as unavailable with no resubmit", async () => {
  const failure = new Error("host down");
  const first = scripted(failure, answer({ items: [] }));
  const result = await run(first.call);
  assert.equal(result.status, "unavailable");
  assert.equal(result.attempts, 1);
  assert.equal(result.error, failure);
  assert.equal(first.calls.length, 1);

  // A failure of the resubmit keeps the state and the problems of the attempt before it.
  const second = scripted(answer({ items: [{ id: "a", ok: true }, { id: "b" }] }), failure);
  const late = await run(second.call);
  assert.equal(late.status, "unavailable");
  assert.equal(late.attempts, 2);
  assert.deepEqual(late.state, ["a"]);
  assert.deepEqual(late.problems, [{ id: "b", line: "b: not ok" }]);
});

test("an answer without exactly one call of the tool is unavailable, not a resubmit", async () => {
  for (const wrong of [
    { text: "No tool.", toolCalls: [] },
    { text: "", toolCalls: [answer({}).toolCalls[0], answer({}, { id: "call-2" }).toolCalls[0]] },
    answer({ items: [] }, { name: "other" }),
    { text: "", toolCalls: [{ type: "function", function: { name: "submit", arguments: "{}" } }] },
  ]) {
    const transport = scripted(wrong, answer({ items: [] }));
    const result = await run(transport.call);
    assert.equal(result.status, "unavailable");
    assert.equal(transport.calls.length, 1);
    assert.match(String(result.error), /one call of submit/);
  }
});

test("one deadline covers every attempt; the late answer is dropped", async () => {
  // Attempt 1 takes 200 ms and has problems; attempt 2 never answers and ignores its signal. One 300 ms deadline over
  // both attempts ends the pass near 300 ms; a deadline for each attempt would end it at 500 ms or later.
  const late = deferred();
  let signal2;
  const transport = scripted(
    async () => { await sleep(200); return answer({ items: [{ id: "a", ok: true }, { id: "b" }] }); },
    (input) => { signal2 = input.signal; return late.promise; },
  );
  const began = Date.now();
  const result = await run(transport.call, { deadlineMs: 300 });
  const elapsed = Date.now() - began;
  assert.equal(result.status, "timeout");
  assert.equal(result.attempts, 2);
  assert.ok(elapsed >= 290 && elapsed < 450, `ended after ${elapsed} ms`);
  assert.ok(signal2.aborted, "the attempt's signal is aborted at the deadline");
  assert.equal(signal2.reason.name, "TimeoutError");
  assert.deepEqual(result.state, ["a"]);
  assert.deepEqual(result.problems, [{ id: "b", line: "b: not ok" }]);
  const length = result.messages.length;
  late.resolve(answer({ items: [{ id: "b", ok: true }] }));
  await sleep(10);
  assert.equal(result.messages.length, length);
  assert.equal(transport.calls.length, 2);
});

test("a transport that fails because the deadline aborted it is a timeout, with no resubmit", async () => {
  const transport = scripted((input) => untilAborted(input.signal), answer({ items: [] }));
  const result = await run(transport.call, { deadlineMs: 30 });
  assert.equal(result.status, "timeout");
  assert.equal(result.attempts, 1);
  assert.equal(transport.calls.length, 1);
});

test("the caller's abort stops the pass with the signal's reason", async () => {
  const controller = new AbortController();
  const reason = new Error("caller left");
  let signal;
  const transport = scripted((input) => { signal = input.signal; setTimeout(() => controller.abort(reason), 20); return new Promise(() => {}); });
  await assert.rejects(run(transport.call, { signal: controller.signal }), (error) => error === reason);
  assert.ok(signal.aborted, "the attempt's signal is aborted");

  const before = scripted(answer({ items: [] }));
  await assert.rejects(run(before.call, { signal: AbortSignal.abort(reason) }), (error) => error === reason);
  assert.equal(before.calls.length, 0, "an aborted signal starts no call");
});

test("onAttempt is told the outcome of each attempt", async () => {
  const events = [];
  const transport = scripted(answer({ items: [{ id: "b" }] }), new Error("down"));
  await run(transport.call, { onAttempt: (event) => events.push(event) });
  assert.deepEqual(events.map(({ attempt, outcome, arguments: args, problems }) => ({ attempt, outcome, args, problems })), [
    { attempt: 1, outcome: "checked", args: JSON.stringify({ items: [{ id: "b" }] }), problems: [{ id: "b", line: "b: not ok" }] },
    { attempt: 2, outcome: "unavailable", args: null, problems: [] },
  ]);
  assert.equal(events[1].error.message, "down");
});

test("bad options throw TypeError before any call", async () => {
  const transport = scripted(answer({ items: [] }));
  for (const bad of [{ deadlineMs: 0 }, { deadlineMs: Number.NaN }, { maxResubmits: -1 }, { maxResubmits: 1.5 }, { messages: "x" }, { tool: { name: "" } }]) {
    await assert.rejects(run(transport.call, bad), TypeError, JSON.stringify(bad));
  }
  await assert.rejects(run(transport.call, { check: () => ({ state: [] }) }), TypeError, "a check result without a problems list");
  assert.equal(transport.calls.length, 1, "only the bad check result made a call");
});
