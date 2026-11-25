// The chat transports in the call ledger: one record pair for each HTTP send (a retry, the reasoning-off retry and each
// try of sendWithRetries are their own), streams ended with the generation id, SendError and ChatAnswer with the ids, a
// replay with replayed true, and the raw postChat response: its passthrough changes nothing the caller gets.
import assert from "node:assert/strict";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import { postChat, requireCallLedger, sendChat, sendWithRetries, setCallLedger, withBackupKey } from "../dist/index.js";
import { check } from "./schema-check.mjs";
import { PACKAGE, URL_, data, json, ledgerSink, readJson, reply, streamOf } from "./support.mjs";

const SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "paid-call.v1.schema.json"));
const call = (body = {}, extra = {}) => ({ url: URL_, key: "k", body: { model: "a/model", messages: [{ role: "user", content: "hi" }], ...body }, ...extra });
const LIMITS = { firstMs: 150, gapMs: 150 };
const HOSTS = ["https://llm.example/v1/"];

let sink;
beforeEach(() => { sink = ledgerSink(); setCallLedger(sink); });
afterEach(() => {
  // Every record this test file's sink got is a valid llm-kit.paid-call/v1.
  for (const record of sink.records) assert.deepEqual(check(record, SCHEMA), [], JSON.stringify(record));
  setCallLedger(null);
  requireCallLedger(false);
});

const pairs = () => sink.begins().map((begin) => ({ begin, end: sink.ends().find((end) => end.id === begin.id) }));
const gen = (extra = {}) => reply("hello", { id: "gen-77", provider: "Host A", usage: { prompt_tokens: 7, completion_tokens: 3, prompt_tokens_details: { cached_tokens: 2 }, cost: 0.0004 }, ...extra });
function memoryStore() {
  const rows = new Map();
  return { rows, async get(layer, key) { return rows.get(`${layer}:${key}`) ?? null; }, async put(layer, key, entry) { rows.set(`${layer}:${key}`, entry); } };
}

test("sendChat: one pair with the usage, tokens, cost, served model, host and generation id; the answer names the call", async () => {
  const answer = await sendChat(call(), { fetch: async () => gen() });
  const [{ begin, end }] = pairs();
  assert.equal(pairs().length, 1);
  assert.equal(answer.callId, begin.id);
  assert.equal(answer.generationId, "gen-77");
  assert.deepEqual([end.ok, end.status, end.errorKind, end.aborted, end.replayed], [true, 200, null, false, false]);
  assert.deepEqual([end.servedModel, end.provider, end.generationId, end.inputTokens, end.cachedTokens, end.outputTokens, end.cost],
    ["served/m", "Host A", "gen-77", 7, 2, 3, 0.0004]);
  assert.deepEqual(end.usage, { prompt_tokens: 7, completion_tokens: 3, prompt_tokens_details: { cached_tokens: 2 }, cost: 0.0004 });
});

test("sendChat failures: an HTTP error, no response and a caller abort each end the call and give the error its call id", async () => {
  await assert.rejects(sendChat(call(), { fetch: async () => json({ error: { message: "busy" } }, 503) }), (error) => error.kind === "http" && error.callId === pairs()[0].begin.id);
  assert.deepEqual([pairs()[0].end.ok, pairs()[0].end.status, pairs()[0].end.errorKind], [false, 503, "http"]);
  await assert.rejects(sendChat(call(), { fetch: async () => { throw new TypeError("fetch failed"); } }), (error) => error.kind === "network" && error.callId === pairs()[1].begin.id);
  assert.deepEqual([pairs()[1].end.ok, pairs()[1].end.status, pairs()[1].end.errorKind, pairs()[1].end.aborted], [false, null, "network", false]);
  const controller = new AbortController();
  controller.abort(new Error("caller left"));
  await assert.rejects(sendChat(call(), { signal: controller.signal, fetch: async (_url, init) => { throw init.signal.reason; } }), (error) => error.kind === "aborted" && error.callId === pairs()[2].begin.id);
  assert.deepEqual([pairs()[2].end.errorKind, pairs()[2].end.aborted], ["aborted", true]);
  await assert.rejects(sendChat(call(), { fetch: async () => new Response("not json", { status: 200 }) }), (error) => error.kind === "body" && error.callId === pairs()[3].begin.id);
  assert.deepEqual([pairs()[3].end.ok, pairs()[3].end.status, pairs()[3].end.errorKind], [false, 200, "body"]);
});

test("a stream: the end carries the usage and the chunk id; the answer has the generation id", async () => {
  const answer = await sendChat(call({ stream: true }), {
    limits: LIMITS, requireFinish: true,
    fetch: async () => streamOf(data({ id: "gen-9", model: "served/m", provider: "Host B", choices: [{ delta: { content: "a" } }] }), data({ id: "gen-9", choices: [{ finish_reason: "stop", delta: {} }], usage: { prompt_tokens: 4, completion_tokens: 2, cost: 0.01 } })),
  });
  const [{ begin, end }] = pairs();
  assert.equal(begin.stream, true);
  assert.equal(answer.generationId, "gen-9");
  assert.equal(answer.callId, begin.id);
  assert.deepEqual([end.ok, end.generationId, end.servedModel, end.provider, end.inputTokens, end.outputTokens, end.cost], [true, "gen-9", "served/m", "Host B", 4, 2, 0.01]);
  assert.equal("reported" in answer, false, "no internal field leaks into the answer");
});

test("a stalled stream: one end with the generation id, the error kind and aborted; SendError has the call and generation ids", async () => {
  let signal;
  await assert.rejects(sendChat(call({ stream: true }), {
    limits: { firstMs: 150, gapMs: 100 },
    fetch: async (_url, init) => { signal = init.signal; return streamOf(data({ id: "gen-5", choices: [{ delta: { content: "a" } }] }), 600, data({ id: "gen-5", choices: [{ finish_reason: "stop", delta: {} }] })); },
  }), (error) => error.kind === "stalled" && error.generationId === "gen-5" && error.callId === sink.begins()[0].id);
  assert.equal(signal.aborted, true);
  assert.equal(pairs().length, 1);
  const { end } = pairs()[0];
  assert.deepEqual([end.ok, end.status, end.errorKind, end.aborted, end.generationId], [false, 200, "stalled", true, "gen-5"]);
});

test("stream errors: an error chunk and a cut body keep the generation id; a finish with no usage ends with null tokens", async () => {
  await assert.rejects(sendChat(call({ stream: true }), { onDelta: () => {}, fetch: async () => streamOf(data({ id: "gen-6", provider: "Host C", choices: [{ delta: { content: "a" } }] }), data({ error: { code: 502, message: "upstream" } })) }),
    (error) => error.kind === "stream_error" && error.generationId === "gen-6" && error.provider === "Host C");
  assert.deepEqual([pairs()[0].end.errorKind, pairs()[0].end.generationId, pairs()[0].end.provider, pairs()[0].end.aborted], ["stream_error", "gen-6", "Host C", false]);
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, requireFinish: true, fetch: async () => streamOf(data({ id: "gen-7", choices: [{ delta: { content: "a" } }] })) }),
    (error) => error.kind === "incomplete" && error.generationId === "gen-7");
  assert.deepEqual([pairs()[1].end.errorKind, pairs()[1].end.generationId, pairs()[1].end.inputTokens], ["incomplete", "gen-7", null]);
});

test("a stream that never answers in time: the call ends as no_first_token, aborted, once; the late response is not recorded twice", async () => {
  const hang = async (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new DOMException("stopped", "AbortError")), { once: true }));
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, fetch: hang }), (error) => error.kind === "no_first_token" && error.callId === sink.begins()[0].id);
  await new Promise((done) => setTimeout(done, 20));
  assert.equal(sink.records.length, 2, "one begin, one end");
  assert.deepEqual([pairs()[0].end.errorKind, pairs()[0].end.aborted, pairs()[0].end.status], ["no_first_token", true, null]);
});

test("the reasoning-off retry is a second record with attempt 2; the answer names the second", async () => {
  let n = 0;
  const answer = await sendChat(call({ reasoning: { enabled: false }, max_tokens: 100 }), {
    reasoningRetry: { budget: 256 }, labels: { step: "draft" },
    fetch: async () => (n++ === 0 ? json({ error: { message: "Reasoning is mandatory for this endpoint" } }, 400) : gen()),
  });
  assert.equal(pairs().length, 2);
  assert.deepEqual(pairs().map(({ begin }) => begin.attempt), [1, 2]);
  assert.deepEqual(pairs().map(({ begin }) => begin.labels), [{ step: "draft" }, { step: "draft" }]);
  assert.deepEqual([pairs()[0].end.ok, pairs()[0].end.status, pairs()[0].end.errorKind], [false, 400, "http"]);
  assert.equal(pairs()[1].end.ok, true);
  assert.equal(answer.callId, pairs()[1].begin.id);
  assert.notEqual(pairs()[0].begin.id, pairs()[1].begin.id);
});

test("sendWithRetries: one pair for each HTTP try with its attempt number, labels and raw usage; the deadline ends a hung try as aborted", async () => {
  const steps = [() => json({ error: { message: "busy" } }, 429), () => { throw new Error("reset"); }, () => gen()];
  let i = 0;
  const result = await sendWithRetries(call(), { waits: [1, 1], budgetMs: 100, random: () => 0.5, labels: { unit: 4 }, fetch: async () => steps[i++]() });
  assert.equal(result.ok, true);
  assert.deepEqual(pairs().map(({ begin }) => [begin.transport, begin.attempt, begin.labels]), [["sendWithRetries", 1, { unit: 4 }], ["sendWithRetries", 2, { unit: 4 }], ["sendWithRetries", 3, { unit: 4 }]]);
  assert.deepEqual(pairs().map(({ end }) => [end.ok, end.status, end.errorKind]), [[false, 429, "http"], [false, null, "network"], [true, 200, null]]);
  assert.deepEqual([pairs()[2].end.generationId, pairs()[2].end.cost, pairs()[2].end.inputTokens], ["gen-77", 0.0004, 7]);
  const hang = async (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true }));
  const cut = await sendWithRetries(call(), { waits: [], budgetMs: 100, deadlineMs: 5, fetch: hang });
  assert.equal(cut.ok, false);
  assert.deepEqual([pairs()[3].end.errorKind, pairs()[3].end.aborted], ["aborted", true]);
});

test("a reply-cache replay is a begin and an end with replayed true, cost 0 and no usage; nothing is sent", async () => {
  const store = memoryStore();
  const cache = { store, account: "a", hosts: HOSTS, classify: () => "success" };
  const request = () => call({ temperature: 0 });
  let sent = 0;
  const fetch = async () => { sent += 1; return gen(); };
  await sendChat(request(), { fetch, cache });
  const again = await sendChat(request(), { fetch, cache });
  assert.equal(sent, 1);
  assert.equal(again.replayed, true);
  assert.equal(pairs().length, 2);
  const { begin, end } = pairs()[1];
  assert.equal(again.callId, begin.id);
  assert.deepEqual([end.replayed, end.ok, end.cost, end.usage, end.inputTokens, end.generationId, again.generationId], [true, true, 0, null, null, null, null]);
  await sendWithRetries(request(), { waits: [], budgetMs: 1, fetch, cache });
  assert.equal(sent, 1, "the stored answer of sendChat is the same wire request");
  assert.deepEqual([pairs()[2].begin.transport, pairs()[2].end.replayed], ["sendWithRetries", true]);
  const raw = await postChat(request(), { fetch, cache: { store, account: "a", hosts: HOSTS } });
  assert.equal(raw.replayed, true);
  assert.deepEqual([pairs()[3].begin.transport, pairs()[3].end.replayed, pairs()[3].end.cost], ["postChat", true, 0]);
  assert.equal(sent, 1);
});

test("a replayed refusal still throws its error with the call id of the replay record", async () => {
  const store = memoryStore();
  const cache = { store, account: "a", hosts: HOSTS, classify: () => "refusal" };
  const fetch = async () => json({ error: { message: "no" } }, 400);
  await assert.rejects(sendChat(call(), { fetch, cache }), (error) => error.kind === "http" && !error.replayed);
  await assert.rejects(sendChat(call(), { fetch, cache }), (error) => error.replayed === true && error.callId === sink.begins()[1].id);
  assert.deepEqual([pairs()[1].end.replayed, pairs()[1].end.ok, pairs()[1].end.errorKind], [true, false, "http"]);
});

test("a withBackupKey fetch that resends once is one record: the package sees one HTTP send", async () => {
  let n = 0;
  const wrapped = withBackupKey(async () => (n++ === 0 ? json({ error: { message: "limit" } }, 402) : gen()), { primary: "k", backup: "b", hosts: ["https://llm.example/"], windowMs: 1000 });
  await sendChat(call(), { fetch: wrapped });
  assert.equal(n, 2);
  assert.equal(pairs().length, 1);
});

test("postChat: the call ends when the body is read, not before; the end has the usage and the generation id", async () => {
  const response = await postChat(call(), { fetch: async () => gen(), labels: { profile: "story" } });
  assert.equal(sink.ends().length, 0, "the headers are not the end");
  assert.deepEqual(sink.begins()[0].labels, { profile: "story" });
  assert.equal((await response.json()).choices[0].message.content, "hello");
  assert.equal(sink.ends().length, 1);
  const end = sink.ends()[0];
  assert.deepEqual([end.ok, end.status, end.generationId, end.servedModel, end.provider, end.inputTokens, end.cost], [true, 200, "gen-77", "served/m", "Host A", 7, 0.0004]);
});

test("postChat: a body that is cancelled ends as cancelled; an HTTP error body ends not ok; a body never read stays open", async () => {
  const cancelled = await postChat(call({ stream: true }), { fetch: async () => streamOf(data({ id: "gen-2", usage: { prompt_tokens: 1, completion_tokens: 1 }, choices: [{ delta: { content: "a" } }] }), 5000) });
  const reader = cancelled.body.getReader();
  await reader.read();
  await reader.cancel();
  assert.deepEqual([sink.ends()[0].errorKind, sink.ends()[0].ok, sink.ends()[0].generationId], ["cancelled", false, "gen-2"]);
  const failed = await postChat(call(), { fetch: async () => json({ error: { message: "no" } }, 500) });
  await failed.text();
  assert.deepEqual([sink.ends()[1].ok, sink.ends()[1].status, sink.ends()[1].errorKind], [false, 500, "http"]);
  await postChat(call(), { fetch: async () => gen() });
  assert.equal(sink.begins().length, 3);
  assert.equal(sink.ends().length, 2, "the body of the third was never read: its record stays open");
  const none = await postChat(call(), { fetch: async () => new Response(null, { status: 204 }) });
  assert.equal(none.status, 204);
  assert.deepEqual([sink.ends()[2].ok, sink.ends()[2].status], [true, 204], "a response with no body ends at once");
  await assert.rejects(postChat(call(), { fetch: async () => { throw new TypeError("fetch failed"); } }), (error) => error.kind === "network" && error.callId === sink.begins()[4].id);
  assert.equal(sink.ends()[3].errorKind, "network");
});

test("postChat: a body that breaks while it is read ends as body, or aborted when the caller's signal is aborted", async () => {
  const broken = () => new Response(new ReadableStream({ start(controller) { controller.error(new TypeError("terminated")); } }), { status: 200, headers: { "content-type": "application/json" } });
  const response = await postChat(call(), { fetch: async () => broken() });
  await assert.rejects(response.text(), /terminated/);
  assert.deepEqual([sink.ends()[0].errorKind, sink.ends()[0].aborted, sink.ends()[0].ok], ["body", false, false]);
  const controller = new AbortController();
  const again = await postChat(call(), { signal: controller.signal, fetch: async () => broken() });
  controller.abort(new Error("left"));
  await assert.rejects(again.text());
  assert.deepEqual([sink.ends()[1].errorKind, sink.ends()[1].aborted], ["aborted", true]);
});

// --- the hypothesis: the bounded passthrough of a raw response changes nothing the caller gets ---

/** A response of three chunks and the number of times its source was pulled. */
function chunked(headers = { "content-type": "application/json" }) {
  const parts = ['{"id":"gen-3","model":"served/m","usage":{"prompt_tokens":5,"comp', 'letion_tokens":2,"cost":0.5},"choices":[{"message":', '{"content":"x"}}]}'];
  const state = { pulls: 0, parts };
  const encoder = new TextEncoder();
  const body = new ReadableStream({ pull(controller) { const part = parts[state.pulls++]; if (part === undefined) controller.close(); else controller.enqueue(encoder.encode(part)); } }, { highWaterMark: 0 });
  const response = new Response(body, { status: 200, statusText: "Fine", headers });
  Object.defineProperty(response, "url", { value: "https://llm.example/v1/chat/completions" });
  return { state, response };
}
const readAll = async (response) => { const out = []; const reader = response.body.getReader(); for (;;) { const next = await reader.read(); if (next.done) return out; out.push(new TextDecoder().decode(next.value)); } };

test("passthrough: the caller gets the same status, status text, headers, url and the same chunks in the same order", async () => {
  const direct = chunked({ "content-type": "application/json", "x-request-id": "r1", "set-cookie": "a=b" });
  const tapped = chunked({ "content-type": "application/json", "x-request-id": "r1", "set-cookie": "a=b" });
  const expected = await readAll(direct.response);
  const response = await postChat(call(), { fetch: async () => tapped.response });
  assert.equal(response.status, 200);
  assert.equal(response.statusText, "Fine");
  assert.equal(response.ok, true);
  assert.equal(response.url, "https://llm.example/v1/chat/completions");
  assert.equal(response.redirected, false);
  assert.deepEqual([...response.headers], [...direct.response.headers]);
  assert.deepEqual(await readAll(response), expected);
  assert.equal(sink.ends()[0].usage.cost, 0.5, "the usage was read from the bytes that passed, split across chunks");
});

test("passthrough: it reads only as the caller reads (no read ahead), keeps the body clonable, and the text and json readers still work", async () => {
  const source = chunked();
  const response = await postChat(call(), { fetch: async () => source.response });
  assert.equal(source.state.pulls, 0, "nothing is read before the caller reads");
  const reader = response.body.getReader();
  await reader.read();
  assert.equal(source.state.pulls, 1, "one chunk read for one read");
  await reader.cancel();
  const second = chunked();
  const again = await postChat(call(), { fetch: async () => second.response });
  const copy = again.clone();
  assert.deepEqual(await again.json(), JSON.parse(second.state.parts.join("")));
  assert.equal(await copy.text(), second.state.parts.join(""));
  assert.equal(again.bodyUsed, true);
  const third = chunked();
  const viaText = await postChat(call(), { fetch: async () => third.response });
  assert.equal(await viaText.text(), third.state.parts.join(""));
});

test("passthrough: a body over the JSON bound still reaches the caller whole, with no usage in the record", async () => {
  const big = JSON.stringify({ id: "gen-big", usage: { prompt_tokens: 1, completion_tokens: 1 }, pad: "x".repeat((1 << 20) + 10) });
  const response = await postChat(call(), { fetch: async () => new Response(big, { headers: { "content-type": "application/json" } }) });
  assert.equal((await response.text()).length, big.length);
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].usage, sink.ends()[0].generationId], [true, null, null]);
});

test("passthrough: an event-stream body gives the generation id and the usage, and the caller reads every event", async () => {
  const events = [data({ id: "gen-4", model: "served/m", provider: "Host D", choices: [{ delta: { content: "a" } }] }), data({ choices: [{ delta: { content: "b" } }], usage: null }),
    data({ choices: [{ finish_reason: "stop", delta: {} }], usage: { prompt_tokens: 9, completion_tokens: 4, cost: 0.2 } }), "data: [DONE]\n\n"];
  const response = await postChat(call({ stream: true }), { fetch: async () => streamOf(...events) });
  assert.equal(await response.text(), events.join(""));
  const end = sink.ends()[0];
  assert.deepEqual([end.generationId, end.servedModel, end.provider, end.inputTokens, end.outputTokens, end.cost], ["gen-4", "served/m", "Host D", 9, 4, 0.2]);
});

test("passthrough: a body that is not JSON or an event stream (bytes) passes untouched and ends with no usage", async () => {
  const bytes = new Uint8Array([0, 255, 1, 2, 3]);
  const response = await postChat(call(), { fetch: async () => new Response(bytes, { headers: { "content-type": "audio/mpeg" } }) });
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes);
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].usage, sink.ends()[0].generationId], [true, null, null]);
});
