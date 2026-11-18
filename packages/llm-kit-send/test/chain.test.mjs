// Fallback chains: the shared cases of llm-kit.fallback-chain/v1 (the same files the Python reader runs), and runChain.
import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import { ChainFormatError, FALLBACK_CHAIN_FORMAT, mapInput, readFallbackChains, runChain } from "../dist/index.js";
import { PACKAGE, readJson } from "./support.mjs";

const CASES = readJson(join(PACKAGE, "..", "..", "tests", "formats", "fallback-chain", "cases.json"));

test("the format id is the shared cases' format", () => {
  assert.equal(FALLBACK_CHAIN_FORMAT, CASES.format);
});

for (const item of CASES.documents) {
  test(`shared document ${item.name}: ${item.expect}`, () => {
    if (item.expect === "valid") assert.equal(readFallbackChains(item.document), item.document);
    else assert.throws(() => readFallbackChains(item.document), (error) => error instanceof ChainFormatError && error.message.startsWith(error.path));
  });
}

for (const item of CASES.mappings) {
  test(`shared mapping ${item.name}`, () => {
    readFallbackChains({ format: FALLBACK_CHAIN_FORMAT, chains: {}, models: { m: { hosts: { a: { endpoint: "e" }, b: { endpoint: "e", from: "a", input: item.rules } } } } });
    assert.equal(JSON.stringify(mapInput(item.rules, item.input)), item.json);
  });
}

const steps = [{ host: "a", next_on: ["no_key", "account"] }, { host: "b", next_on: ["no_key", "account"] }, { host: "c", next_on: [] }];
/** Attempts that answer from `script` (host -> result) and record what each attempt was told. */
function script(results) {
  const told = [];
  const attempt = async (step, context) => { told.push([step.host, context.previous?.host ?? null, [...context.skipped]]); return results[step.host]; };
  return { told, attempt };
}

test("runChain: the first success wins; a listed failure moves on with the previous failure", async () => {
  const { told, attempt } = script({ a: { ok: false, failure: "account", reason: "a_402" }, b: { ok: true, value: "audio" } });
  assert.deepEqual(await runChain(steps, { hasKey: () => true, attempt }), { ok: true, value: "audio", host: "b" });
  assert.deepEqual(told, [["a", null, []], ["b", "a", []]]);
});

test("runChain: an unlisted failure stops the chain at that step", async () => {
  const error = new Error("refused");
  const { attempt } = script({ a: { ok: false, failure: "other", error } });
  assert.deepEqual(await runChain(steps, { hasKey: () => true, attempt }), { ok: false, stop: { host: "a", cause: "failure" }, last: { host: "a", failure: "other", error } });
});

test("runChain: a step with no key is skipped when no_key is listed, else the chain stops there", async () => {
  const keys = new Set(["c"]);
  const { told, attempt } = script({ c: { ok: true, value: 1 } });
  assert.deepEqual(await runChain(steps, { hasKey: (host) => keys.has(host), attempt }), { ok: true, value: 1, host: "c" });
  assert.deepEqual(told, [["c", null, ["a", "b"]]]);
  const none = await runChain(steps, { hasKey: () => false, attempt });
  assert.deepEqual(none, { ok: false, stop: { host: "c", cause: "no_key" }, last: null });
  const { attempt: failing } = script({ a: { ok: false, failure: "account", reason: "a_402" } });
  assert.deepEqual(await runChain(steps, { hasKey: (host) => host === "a", attempt: failing }),
    { ok: false, stop: { host: "c", cause: "no_key" }, last: { host: "a", failure: "account", reason: "a_402" } });
});

test("runChain: every step moves on is the end; an exception stops the chain; an abort stops it before the next step", async () => {
  const { attempt } = script({ a: { ok: false, failure: "account" } });
  assert.deepEqual(await runChain([steps[0]], { hasKey: () => true, attempt }), { ok: false, stop: { host: null, cause: "end" }, last: { host: "a", failure: "account" } });
  let calls = 0;
  await assert.rejects(runChain(steps, { hasKey: () => true, attempt: async () => { calls += 1; throw new Error("boom"); } }), /boom/);
  assert.equal(calls, 1);
  const controller = new AbortController();
  const ran = [];
  await assert.rejects(runChain(steps, { hasKey: () => true, signal: controller.signal, attempt: async (step) => {
    ran.push(step.host);
    controller.abort(new Error("caller left"));
    return { ok: false, failure: "account" };
  } }), /caller left/);
  assert.deepEqual(ran, ["a"]);
});
