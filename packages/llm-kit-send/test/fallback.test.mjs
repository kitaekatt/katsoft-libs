// withFallback: one fallback before the first token, never after output, never on an abort.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SendError, withFallback } from "../dist/index.js";

const failing = (error) => async () => { throw error; };

test("the primary answer is returned; its failure goes to the fallback once, and is reported", async () => {
  assert.equal(await withFallback(async () => "first", async () => "second", { emitted: () => false }), "first");
  const reports = [];
  const down = new SendError("http", "down", { status: 502 });
  let given;
  const result = await withFallback(failing(down), async (error) => { given = error; return "second"; }, { emitted: () => false, onFailure: (error, next) => reports.push([error, next]) });
  assert.equal(result, "second");
  assert.equal(given, down);
  assert.deepEqual(reports, [[down, true]]);
});

test("no fallback after output, on an abort, on an ineligible failure, or without a fallback; the fallback failure propagates", async () => {
  const down = new SendError("http", "down", { status: 502 });
  const reports = [];
  const onFailure = (error, next) => reports.push(next);
  await assert.rejects(withFallback(failing(down), async () => "x", { emitted: () => true, onFailure }), (e) => e === down);
  await assert.rejects(withFallback(failing(new DOMException("stop", "AbortError")), async () => "x", { emitted: () => false, onFailure }), (e) => e.name === "AbortError");
  await assert.rejects(withFallback(failing(new SendError("aborted", "stop")), async () => "x", { emitted: () => false, onFailure }), (e) => e.kind === "aborted");
  await assert.rejects(withFallback(failing(new SendError("http", "bad", { status: 400 })), async () => "x", { emitted: () => false, onFailure }), (e) => e.status === 400);
  await assert.rejects(withFallback(failing(down), null, { emitted: () => false, onFailure }), (e) => e === down);
  assert.deepEqual(reports, [false, false, false, false, false]);
  const second = new Error("fallback down");
  await assert.rejects(withFallback(failing(down), failing(second), { emitted: () => false }), (e) => e === second);
  assert.equal(await withFallback(failing(new SendError("http", "bad", { status: 400 })), async () => "y", { emitted: () => false, eligible: (e) => e.status === 400 }), "y", "the consumer's own rule");
});
