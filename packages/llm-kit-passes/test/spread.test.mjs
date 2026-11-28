// spreadGather: every item at once, one signal and one deadline, results in input order, late output dropped.
import assert from "node:assert/strict";
import { test } from "node:test";

import { spreadGather } from "../dist/index.js";
import { deferred, sleep, untilAborted } from "./support.mjs";

test("every item runs at once and the results keep the input order", async () => {
  let running = 0, most = 0;
  const signals = new Set();
  const delays = [60, 10, 30];
  const result = await spreadGather(delays, async (ms, { signal, index }) => {
    signals.add(signal);
    running += 1; most = Math.max(most, running);
    await sleep(ms);
    running -= 1;
    return `${index}:${ms}`;
  }, { deadlineMs: 5_000 });
  assert.equal(most, 3, "all three ran at the same time");
  assert.equal(signals.size, 1, "one signal for every item");
  assert.equal(result.timedOut, false);
  assert.deepEqual(result.results, [
    { status: "fulfilled", value: "0:60" },
    { status: "fulfilled", value: "1:10" },
    { status: "fulfilled", value: "2:30" },
  ]);
});

test("a failed item keeps its place as rejected; a synchronous throw counts as a failure", async () => {
  const failure = new Error("no");
  const result = await spreadGather(["a", "b", "c"], (item) => {
    if (item === "b") throw failure;
    return Promise.resolve(item.toUpperCase());
  }, { deadlineMs: 5_000 });
  assert.deepEqual(result.results, [
    { status: "fulfilled", value: "A" },
    { status: "rejected", reason: failure },
    { status: "fulfilled", value: "C" },
  ]);
});

test("at the deadline the shared signal aborts, unsettled items are timeouts and their late output is dropped", async () => {
  const slow = deferred();
  let shared;
  const began = Date.now();
  const result = await spreadGather([0, 1, 2], (item, { signal }) => {
    shared = signal;
    if (item === 0) return Promise.resolve("fast");
    if (item === 1) return slow.promise;
    return untilAborted(signal);
  }, { deadlineMs: 80 });
  const elapsed = Date.now() - began;
  assert.ok(elapsed >= 70 && elapsed < 400, `ended after ${elapsed} ms`);
  assert.equal(result.timedOut, true);
  assert.ok(shared.aborted);
  assert.equal(shared.reason.name, "TimeoutError");
  assert.deepEqual(result.results, [{ status: "fulfilled", value: "fast" }, { status: "timeout" }, { status: "timeout" }]);
  slow.resolve("too late");
  await sleep(10);
  assert.deepEqual(result.results[1], { status: "timeout" }, "a late answer does not change the result");
});

test("startedAt counts the deadline from an earlier start; with no time left nothing runs", async () => {
  const began = Date.now();
  const result = await spreadGather([1], (_, { signal }) => untilAborted(signal), { deadlineMs: 1_000, startedAt: Date.now() - 950 });
  assert.ok(Date.now() - began < 400, "the deadline was 50 ms away");
  assert.deepEqual(result, { results: [{ status: "timeout" }], timedOut: true });

  let runs = 0;
  const none = await spreadGather([1, 2], () => { runs += 1; return Promise.resolve(1); }, { deadlineMs: 100, startedAt: Date.now() - 200 });
  assert.equal(runs, 0);
  assert.deepEqual(none, { results: [{ status: "timeout" }, { status: "timeout" }], timedOut: true });
});

test("the caller's abort stops the gather with the signal's reason and aborts the shared signal", async () => {
  const controller = new AbortController();
  const reason = new Error("caller left");
  let shared;
  setTimeout(() => controller.abort(reason), 20);
  await assert.rejects(spreadGather([1, 2], (_, { signal }) => { shared = signal; return new Promise(() => {}); }, { deadlineMs: 5_000, signal: controller.signal }),
    (error) => error === reason);
  assert.ok(shared.aborted);

  let runs = 0;
  await assert.rejects(spreadGather([1], () => { runs += 1; return Promise.resolve(1); }, { deadlineMs: 5_000, signal: AbortSignal.abort(reason) }), (error) => error === reason);
  assert.equal(runs, 0);
});

test("no items give no results at once; bad options throw TypeError", async () => {
  assert.deepEqual(await spreadGather([], () => Promise.resolve(1), { deadlineMs: 10 }), { results: [], timedOut: false });
  for (const bad of [{ deadlineMs: 0 }, { deadlineMs: Infinity }, { deadlineMs: 10, startedAt: "now" }, {}]) {
    await assert.rejects(spreadGather([1], () => Promise.resolve(1), bad), TypeError, JSON.stringify(bad));
  }
  await assert.rejects(spreadGather("ab", () => Promise.resolve(1), { deadlineMs: 10 }), TypeError);
});
