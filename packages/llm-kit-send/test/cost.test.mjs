// usageCost, tokenCost, upstreamReason and SendError.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SendError, tokenCost, upstreamReason, usageCost } from "../dist/index.js";

test("usageCost is the reported cost when it is a finite number", () => {
  assert.equal(usageCost({ cost: 0.25 }), 0.25);
  assert.equal(usageCost({ cost: 0 }), 0);
  for (const usage of [null, undefined, {}, { cost: "1" }, { cost: Number.NaN }, { cost: Infinity }]) assert.equal(usageCost(usage), null);
});

test("tokenCost prices uncached input, cache reads, cache writes and output", () => {
  const price = { input: 2e-6, cached: 0.2e-6, write: 3e-6, output: 10e-6 };
  const usage = { prompt_tokens: 1000, completion_tokens: 100, prompt_tokens_details: { cached_tokens: 600, cache_write_tokens: 100 } };
  assert.equal(tokenCost(price, usage), 300 * 2e-6 + 600 * 0.2e-6 + 100 * 3e-6 + 100 * 10e-6);
  assert.equal(tokenCost(price, { prompt_tokens: 10, completion_tokens: 5 }), 10 * 2e-6 + 5 * 10e-6);
  assert.equal(tokenCost(undefined, usage), null);
  assert.equal(tokenCost(price, { prompt_tokens: 10 }), null);
  assert.equal(tokenCost(price, null), null);
});

test("upstreamReason keeps the host's short verdict and drops anything that could echo a prompt", () => {
  assert.equal(upstreamReason('{"error":{"type":"content_filter","message":"Blocked by policy"}}'), "content_filter: Blocked by policy");
  assert.equal(upstreamReason({ code: "length_exceeded" }), "length_exceeded");
  assert.equal(upstreamReason({ error: { message: "x".repeat(121) } }), null);
  assert.equal(upstreamReason({ error: { message: "line one\nline two", type: "bad" } }), "bad");
  assert.equal(upstreamReason("not json"), null);
  assert.equal(upstreamReason(null), null);
});

test("SendError carries its fields; limit kinds are TimeoutError; fallback follows the kind and status", () => {
  const cause = new Error("x");
  const error = new SendError("http", "m", { status: 503, code: "c", providerMessage: "p", provider: "h", reason: "r", payload: { a: 1 }, cause });
  assert.deepEqual([error.kind, error.status, error.code, error.providerMessage, error.provider, error.reason, error.payload, error.cause, error.fallback, error.name],
    ["http", 503, "c", "p", "h", "r", { a: 1 }, cause, true, "SendError"]);
  assert.equal(new SendError("stalled", "m").name, "TimeoutError");
  assert.equal(new SendError("aborted", "m").fallback, false);
  assert.equal(new SendError("network", "m").fallback, true);
  assert.deepEqual([new SendError("body", "m").status, new SendError("body", "m").code, new SendError("body", "m").payload], [null, null, null]);
});
