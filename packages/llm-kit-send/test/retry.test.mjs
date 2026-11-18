// isTransient, retryWait, pause and sendWithRetries. Every transport is scripted; waits are a few milliseconds.
import assert from "node:assert/strict";
import { test } from "node:test";

import { isTransient, pause, retryWait, sendWithRetries } from "../dist/index.js";
import { URL_, json, recorder, reply } from "./support.mjs";

const call = { url: URL_, key: "k", body: { model: "asked/m", messages: [{ role: "user", content: "SECRET PROMPT" }] } };
const base = { waits: [5, 5], budgetMs: 100, random: () => 0.5 };

test("isTransient is 429 and every 5xx, nothing else", () => {
  for (const code of [429, 500, 502, 503, 599]) assert.equal(isTransient(code), true, String(code));
  for (const code of [200, 400, 401, 402, 404, 422]) assert.equal(isTransient(code), false, String(code));
});

test("retryWait: jitter 70% to 130%, Retry-After when longer, never past the budget left", () => {
  assert.equal(retryWait(1000, null, 0, { budget: 5000, random: () => 0 }), 700);
  assert.equal(retryWait(1000, null, 0, { budget: 5000, random: () => 1 }), 1300);
  const asked = new Response("", { headers: { "retry-after": "3" } });
  assert.equal(retryWait(1000, asked, 0, { budget: 5000, random: () => 0.5 }), 3000);
  assert.equal(retryWait(1000, asked, 4000, { budget: 5000, random: () => 0.5 }), 1000);
  assert.equal(retryWait(1000, asked, 9000, { budget: 5000, random: () => 0.5 }), 0);
  assert.equal(retryWait(1000, new Response("", { headers: { "retry-after": "soon" } }), 0, { budget: 5000, random: () => 0.5 }), 1000);
});

test("pause waits, and rejects with the abort reason when the signal is or becomes aborted", async () => {
  await pause(1);
  const early = new AbortController(); early.abort(new Error("early"));
  await assert.rejects(pause(1000, early.signal), /early/);
  const later = new AbortController();
  const waiting = pause(1000, later.signal);
  later.abort(new Error("later"));
  await assert.rejects(waiting, /later/);
});

test("sendWithRetries: busy answers and a lost request are tried again; one record per try, without text", async () => {
  const records = [];
  let index = 0;
  const steps = [() => json({ error: { message: "rate-limited upstream" } }, 429), () => { throw new Error("reset"); },
    () => reply("done", { provider: "Host A", usage: { prompt_tokens: 7, completion_tokens: 3, prompt_tokens_details: { cached_tokens: 2 }, cost: 0.0004 } })];
  const fetchImpl = async () => steps[index++]();
  const result = await sendWithRetries(call, { ...base, fetch: fetchImpl, onCall: (record) => records.push(record) });
  assert.ok(result.ok);
  assert.equal(result.text, "done");
  assert.equal(result.model, "served/m");
  assert.deepEqual(records.map(({ ms, ...rest }) => (assert.equal(typeof ms, "number"), rest)), [
    { attempt: 1, model: "asked/m", provider: null, ok: false, status: 429, aborted: false, inputTokens: null, cachedTokens: null, outputTokens: null, cost: null, providerMessage: "rate-limited upstream", replayed: false },
    { attempt: 2, model: "asked/m", provider: null, ok: false, status: null, aborted: false, inputTokens: null, cachedTokens: null, outputTokens: null, cost: null, providerMessage: null, replayed: false },
    { attempt: 3, model: "served/m", provider: "Host A", ok: true, status: 200, aborted: false, inputTokens: 7, cachedTokens: 2, outputTokens: 3, cost: 0.0004, providerMessage: null, replayed: false },
  ]);
  assert.ok(!JSON.stringify(records).includes("SECRET"));
});

test("sendWithRetries: a request error ends at once; all tries busy gives the last status and body", async () => {
  const bad = recorder(() => json({ error: { message: "bad" } }, 400));
  assert.deepEqual(await sendWithRetries(call, { ...base, fetch: bad.fetchImpl }), { ok: false, status: 400, body: { error: { message: "bad" } }, replayed: false });
  assert.equal(bad.calls.length, 1);
  const busy = recorder(() => json({ error: { message: "busy" } }, 503));
  assert.deepEqual(await sendWithRetries(call, { ...base, fetch: busy.fetchImpl }), { ok: false, status: 503, body: { error: { message: "busy" } }, replayed: false });
  assert.equal(busy.calls.length, 3);
  const once = recorder(() => json({}, 503));
  await sendWithRetries(call, { waits: [], budgetMs: 0, fetch: once.fetchImpl });
  assert.equal(once.calls.length, 1, "no waits is exactly one try");
});

test("sendWithRetries: the deadline gives {deadline: true}; the caller's abort rethrows; both record the cut try", async () => {
  const hang = async (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true }));
  const records = [];
  assert.deepEqual(await sendWithRetries(call, { ...base, deadlineMs: 20, fetch: hang, onCall: (r) => records.push(r) }), { ok: false, status: null, body: { deadline: true }, replayed: false });
  const controller = new AbortController();
  const running = sendWithRetries(call, { ...base, signal: controller.signal, fetch: hang, onCall: (r) => records.push(r) });
  setTimeout(() => controller.abort(new Error("caller stop")), 10);
  await assert.rejects(running, /caller stop/);
  assert.deepEqual(records.map((r) => [r.attempt, r.aborted, r.ok]), [[1, true, false], [1, true, false]]);
});

test("sendWithRetries: a beforeSend refusal propagates, sends nothing and records nothing", async () => {
  const { calls, fetchImpl } = recorder(() => reply("x"));
  const records = [];
  await assert.rejects(sendWithRetries(call, { ...base, fetch: fetchImpl, beforeSend: () => { throw new Error("refused"); }, onCall: (r) => records.push(r) }), /refused/);
  assert.equal(calls.length, 0);
  assert.deepEqual(records, []);
});

test("sendWithRetries joins text parts of an answer", async () => {
  const result = await sendWithRetries(call, { ...base, fetch: async () => json({ choices: [{ message: { content: [{ type: "text", text: "a" }, { type: "image" }, { type: "text", text: "b" }] } }] }) });
  assert.equal(result.ok && result.text, "ab");
});
