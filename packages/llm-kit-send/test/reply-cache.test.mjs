// The reply cache: the shared key and entry cases (the same files the Python reader runs), and each cached transport
// with a counting fetch stub: a hit sends nothing, only allowed answers are stored, and store failures are their own
// error. No network.
import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import {
  REPLY_ENTRY_FORMAT, REPLY_KEY_FORMAT, ReplyCacheError, ReplyEntryError, SendError, ideogramEdit, isVeniceViolation, postChat,
  postImages, postVenice, readEntry, requestKey, sendChat, sendWithRetries, stepKey, withBackupKey, withFallback,
} from "../dist/index.js";
import { PACKAGE, URL_, json, readJson, reply } from "./support.mjs";

const FORMATS = join(PACKAGE, "..", "..", "tests", "formats");
const KEY_CASES = readJson(join(FORMATS, "reply-key", "cases.json"));
const ENTRY_CASES = readJson(join(FORMATS, "reply-entry", "cases.json"));
const ENTRY_SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "reply-entry.v1.schema.json"));

/** True when `value` is valid against `schema`, for the keywords the reply-entry schema uses (an unknown keyword fails). */
function valid(schema, value) {
  const known = new Set(["$schema", "$id", "title", "description", "type", "properties", "required", "additionalProperties", "anyOf", "const", "enum", "minLength", "minimum", "maximum"]);
  for (const keyword of Object.keys(schema)) assert.ok(known.has(keyword), `the checker knows ${keyword}`);
  if (schema.anyOf && !schema.anyOf.some((branch) => valid(branch, value))) return false;
  if ("const" in schema && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  if (schema.type) {
    const kind = value === null ? "null" : Array.isArray(value) ? "array" : Number.isInteger(value) ? "integer" : typeof value;
    if (![schema.type].flat().some((type) => type === kind || (type === "number" && kind === "integer"))) return false;
  }
  if (typeof value === "string" && schema.minLength !== undefined && value.length < schema.minLength) return false;
  if (typeof value === "number" && ((schema.minimum !== undefined && value < schema.minimum) || (schema.maximum !== undefined && value > schema.maximum))) return false;
  if (schema.properties || schema.required || schema.additionalProperties !== undefined) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
    for (const name of schema.required ?? []) if (!(name in value)) return false;
    for (const [name, item] of Object.entries(value)) {
      const rule = schema.properties?.[name] ?? schema.additionalProperties;
      if (rule === false || (rule && rule !== true && !valid(rule, item))) return false;
    }
  }
  return true;
}

const fromBase64 = (value) => new Uint8Array(Buffer.from(value, "base64"));
const partOf = (part) => ("text" in part ? part.text : fromBase64(part.base64));
function wireOf(request) {
  let body = null;
  if (request.body?.text !== undefined) body = request.body.text;
  else if (request.body?.base64 !== undefined) body = fromBase64(request.body.base64);
  else if (request.body?.form) {
    body = new FormData();
    for (const part of request.body.form) {
      if ("text" in part) body.append(part.name, part.text);
      else body.append(part.name, new Blob([fromBase64(part.base64)], { type: part.type }), part.filename);
    }
  }
  return { method: request.method, url: request.url, headers: request.headers, body };
}

test("requestKey and stepKey give every shared key; the same and different groups hold", async () => {
  assert.equal(KEY_CASES.format, REPLY_KEY_FORMAT);
  for (const item of KEY_CASES.parts) assert.equal(requestKey(item.parts.map(partOf)), item.key, item.name);
  const keys = Object.fromEntries(KEY_CASES.parts.map((item) => [item.name, item.key]));
  for (const item of KEY_CASES.requests) {
    keys[item.name] = await stepKey(item.account, wireOf(item.request), item.scope ?? []);
    assert.equal(keys[item.name], item.key, item.name);
  }
  for (const group of KEY_CASES.same) assert.equal(new Set(group.map((name) => keys[name])).size, 1, group.join());
  for (const [first, second] of KEY_CASES.different) assert.notEqual(keys[first], keys[second], `${first} ${second}`);
});

test("stepKey: two forms of the same parts give one key whatever their boundaries; a stream body is refused", async () => {
  const form = () => { const f = new FormData(); f.append("prompt", "x"); f.append("image", new Blob([new Uint8Array([1, 2])], { type: "image/png" }), "a.png"); return f; };
  const request = (body) => ({ method: "POST", url: "https://h.example/edit", headers: {}, body });
  const [a, b] = [form(), form()];
  const boundaries = await Promise.all([a, b].map(async (f) => new Response(f).headers.get("content-type")));
  assert.notEqual(boundaries[0], boundaries[1], "each form has its own boundary");
  assert.equal(await stepKey("m", request(a)), await stepKey("m", request(b)));
  await assert.rejects(stepKey("m", request(new ReadableStream())), TypeError);
});

test("readEntry accepts and refuses each shared entry with its path, as the schema does", () => {
  assert.equal(ENTRY_CASES.format, REPLY_ENTRY_FORMAT);
  assert.equal(ENTRY_SCHEMA.$id, REPLY_ENTRY_FORMAT);
  for (const item of ENTRY_CASES.entries) assert.equal(valid(ENTRY_SCHEMA, item.entry), item.expect === "valid", `schema: ${item.name}`);
  for (const item of ENTRY_CASES.reader_rules) assert.ok(valid(ENTRY_SCHEMA, item.entry), `schema accepts the reader rule case ${item.name}`);
  for (const item of ENTRY_CASES.entries) {
    if (item.expect === "valid") assert.deepEqual(readEntry(item.entry), item.entry, item.name);
    else assert.throws(() => readEntry(item.entry), (error) => error instanceof ReplyEntryError && error.path === item.path && error.message.startsWith(item.path), item.name);
  }
  for (const item of ENTRY_CASES.reader_rules) {
    assert.throws(() => readEntry(item.entry), (error) => error instanceof ReplyEntryError && error.path === item.path, item.name);
  }
});

/** A store in memory with the documented precedence; `puts` lists every write. */
function memoryStore({ failGet = null, failPut = null } = {}) {
  const rows = new Map();
  const puts = [];
  return {
    rows, puts,
    async get(layer, key) { if (failGet) throw failGet; return rows.get(`${layer}:${key}`) ?? null; },
    async put(layer, key, entry) {
      if (failPut) throw failPut;
      readEntry(entry);
      assert.ok(valid(ENTRY_SCHEMA, entry), "every written entry is a valid llm-kit.reply-entry/v1");
      puts.push({ layer, key, entry });
      const live = rows.get(`${layer}:${key}`);
      if (!live || (live.outcome === "success" && entry.outcome === "refusal")) rows.set(`${layer}:${key}`, entry);
    },
  };
}

/** A transport that counts requests and answers with `answer(index, init)`. */
function counting(answer) {
  const calls = [];
  const fetchImpl = async (url, init) => { calls.push({ url: String(url), init }); return answer(calls.length - 1, init); };
  return { calls, fetchImpl };
}

const HOSTS = ["https://llm.example/v1/"];
const IMAGES = "https://llm.example/v1/images";
const refusalOn = (test) => (result) => (test(result) ? "refusal" : null);
const chatBody = (temperature) => ({ model: "asked/m", messages: [{ role: "user", content: "Is this a cat?" }], ...(temperature === undefined ? {} : { temperature }) });

test("postImages: a stored refusal answers the same bytes with no request; one added space sends again", async () => {
  const store = memoryStore();
  const declined = { error: { message: "rejected by the safety system" } };
  const host = counting(() => json(declined, 400));
  const cache = { store, account: "router:primary", hosts: HOSTS, classify: refusalOn((answer) => answer.ok === false && answer.status === 400) };
  const call = { url: IMAGES, key: "SECRET", body: { model: "img/m", prompt: "A lake." } };
  const first = await postImages(call, { fetch: host.fetchImpl, cache });
  assert.deepEqual(first, { ok: false, status: 400, failure: declined, replayed: false });
  assert.equal(store.puts.length, 1);
  assert.equal(store.puts[0].entry.outcome, "refusal");
  assert.ok(!JSON.stringify(store.puts).includes("SECRET"), "no key in the store");
  const again = await postImages(call, { fetch: host.fetchImpl, cache });
  assert.deepEqual(again, { ok: false, status: 400, failure: declined, replayed: true });
  assert.equal(host.calls.length, 1, "the repeat sent nothing");
  await postImages({ ...call, body: { model: "img/m", prompt: "A lake. " } }, { fetch: host.fetchImpl, cache });
  assert.equal(host.calls.length, 2, "one added space is a new request");
  await postImages(call, { fetch: host.fetchImpl, cache: { ...cache, scope: ["another operation"] } });
  assert.equal(host.calls.length, 3, "another scope is a new key");
  await postImages(call, { fetch: host.fetchImpl, cache: { ...cache, account: "router:backup" } });
  assert.equal(host.calls.length, 4, "another account is a new key");
});

test("postImages: a cached miss sends the same request bytes, header order and init keys as an uncached call", async () => {
  const plain = counting(() => json({ data: [{ b64_json: "aW1n" }] }));
  const cached = counting(() => json({ data: [{ b64_json: "aW1n" }] }));
  const call = { url: IMAGES, key: "k", headers: { "X-Label": "art" }, body: { prompt: "A lake.", model: "img/m" } };
  const signal = new AbortController().signal;
  await postImages(call, { fetch: plain.fetchImpl, signal });
  await postImages(call, { fetch: cached.fetchImpl, signal, cache: { store: memoryStore(), account: "a", hosts: HOSTS, classify: () => null } });
  assert.deepEqual(Object.keys(cached.calls[0].init), Object.keys(plain.calls[0].init));
  assert.deepEqual(Object.entries(cached.calls[0].init.headers), Object.entries(plain.calls[0].init.headers));
  assert.equal(cached.calls[0].init.body, plain.calls[0].init.body);
  assert.equal(cached.calls[0].init.signal, signal);
});

test("postImages: a success, a null verdict, a body that is not JSON, a busy answer and an oversize body store nothing", async () => {
  const store = memoryStore();
  const always = { store, account: "a", hosts: HOSTS, classify: () => "refusal" };
  await postImages({ url: IMAGES, key: "k", body: { p: 1 } }, { fetch: async () => json({ data: [{ b64_json: "aW1n" }] }), cache: { ...always, classify: () => "success" } });
  await postImages({ url: IMAGES, key: "k", body: { p: 2 } }, { fetch: async () => json({ error: "no" }, 400), cache: { ...always, classify: () => null } });
  const notJson = await postImages({ url: IMAGES, key: "k", body: { p: 3 } }, { fetch: async () => new Response("<html>", { status: 400 }), cache: always });
  assert.deepEqual(notJson, { ok: false, status: 400, failure: {}, replayed: false });
  for (const status of [408, 429, 500, 503]) await postImages({ url: IMAGES, key: "k", body: { p: status } }, { fetch: async () => json({ error: "busy" }, status), cache: always });
  await postImages({ url: IMAGES, key: "k", body: { p: 4 } }, { fetch: async () => json({ error: "x".repeat(100) }, 400), cache: { ...always, maxBytes: 50 } });
  await postImages({ url: IMAGES, key: "k", body: { p: 5 } }, { fetch: async () => { throw new TypeError("fetch failed"); }, cache: always });
  assert.deepEqual(store.puts, []);
});

test("a cache on a URL outside its hosts, or with a withBackupKey fetch, throws before any request", async () => {
  const host = counting(() => json({}));
  const cache = { store: memoryStore(), account: "a", hosts: ["https://llm.example/v1"], classify: () => null };
  await assert.rejects(postImages({ url: "https://llm.example/v1x/images", key: "k", body: {} }, { fetch: host.fetchImpl, cache }), TypeError);
  await assert.rejects(postImages({ url: "https://other.example/v1/images", key: "k", body: {} }, { fetch: host.fetchImpl, cache }), TypeError);
  await postImages({ url: "https://llm.example/v1/images", key: "k", body: {} }, { fetch: host.fetchImpl, cache });
  const backup = withBackupKey(host.fetchImpl, { primary: "p", backup: "b", hosts: ["https://llm.example/"], windowMs: 1 });
  await assert.rejects(sendWithRetries({ url: "https://llm.example/v1/chat", key: "p", body: chatBody(0) }, { waits: [], budgetMs: 0, fetch: backup, cache }), TypeError);
  assert.equal(host.calls.length, 1);
});

test("ideogramEdit: a refused form replays with no request; a changed field sends again", async () => {
  const store = memoryStore();
  const host = counting(() => json({ reject_reason: "unsafe" }, 422));
  const cache = { store, account: "ideo", hosts: ["https://edit.example/"], classify: refusalOn((answer) => answer.ok === false && answer.reason === "unsafe") };
  const form = (prompt) => { const f = new FormData(); f.append("prompt", prompt); f.append("image", new Blob([new Uint8Array([9])], { type: "image/png" }), "image.png"); return f; };
  const call = (prompt) => ({ key: "SECRET", model: "edit-4", form: form(prompt), baseUrl: "https://edit.example" });
  assert.deepEqual(await ideogramEdit(call("short hair"), { fetch: host.fetchImpl, cache }), { ok: false, status: 422, reason: "unsafe", replayed: false });
  assert.deepEqual(await ideogramEdit(call("short hair"), { fetch: host.fetchImpl, cache }), { ok: false, status: 422, reason: "unsafe", replayed: true });
  assert.equal(host.calls.length, 1);
  await ideogramEdit(call("short hair "), { fetch: host.fetchImpl, cache });
  assert.equal(host.calls.length, 2);
});

test("postVenice: the headers arrive before the body is read; a header refusal is stored headers only and replays", async () => {
  const store = memoryStore();
  let release;
  const gate = new Promise((done) => { release = done; });
  const host = counting(() => new Response(new ReadableStream({
    async start(controller) { controller.enqueue(new Uint8Array([1])); await gate; controller.enqueue(new Uint8Array([2])); controller.close(); },
  }), { status: 200, headers: { "content-type": "image/png", "x-venice-is-content-violation": "true", "set-cookie": "s=1" } }));
  const cache = { store, account: "venice", hosts: ["https://venice.example/"] };
  const call = { endpoint: "generate", key: "SECRET", body: { model: "v/m", prompt: "x" }, baseUrl: "https://venice.example/api" };
  const order = [];
  const answer = await postVenice(call, { fetch: host.fetchImpl, cache });
  order.push("headers");
  assert.equal(answer.replayed, false);
  assert.ok(isVeniceViolation(answer.response));
  await answer.settle("refusal", { headersOnly: true });
  order.push("settled");
  release();
  assert.deepEqual(order, ["headers", "settled"], "the caller sees the headers and settles before the body is done");
  assert.deepEqual(store.puts.map(({ entry }) => [entry.status, entry.headers, entry.body]),
    [[200, { "content-type": "image/png", "x-venice-is-content-violation": "true" }, ""]]);
  const again = await postVenice(call, { fetch: host.fetchImpl, cache });
  assert.equal(again.replayed, true);
  assert.ok(isVeniceViolation(again.response));
  assert.equal((await again.response.arrayBuffer()).byteLength, 0);
  await again.settle("refusal");
  assert.equal(host.calls.length, 1);
  assert.equal(store.puts.length, 1);
});

test("postVenice: an abort during the body read stores nothing; a success is never stored; settle never reads the body", async () => {
  const store = memoryStore();
  const cache = { store, account: "venice", hosts: ["https://venice.example/"] };
  const call = { endpoint: "generate", key: "k", body: { prompt: "x" }, baseUrl: "https://venice.example/api" };
  const controller = new AbortController();
  const hanging = async (_url, init) => new Response(new ReadableStream({
    start(stream) { stream.enqueue(new Uint8Array([1])); init.signal.addEventListener("abort", () => stream.error(init.signal.reason), { once: true }); },
  }), { status: 400, headers: { "content-type": "application/json" } });
  const cut = await postVenice(call, { fetch: hanging, signal: controller.signal, cache });
  const reading = cut.response.arrayBuffer();
  controller.abort(new Error("stop"));
  await assert.rejects(reading, /stop/);
  await cut.settle("refusal");
  const unread = await postVenice({ ...call, body: { prompt: "y" } }, { fetch: async () => json({ error: 1 }, 400), cache });
  await unread.settle("refusal");
  assert.equal(unread.response.bodyUsed, false, "settle did not read the body");
  const made = await postVenice({ ...call, body: { prompt: "z" } }, { fetch: async () => new Response(new Uint8Array([1, 2]), { headers: { "content-type": "image/png" } }), cache });
  await made.response.arrayBuffer();
  await made.settle("success");
  assert.deepEqual(store.puts, []);
});

test("postChat: settle stores the read body once; a repeat call returns the same promise; a stream body is refused", async () => {
  const store = memoryStore();
  const host = counting(() => json({ choices: [{ finish_reason: "content_filter", message: { content: "" } }] }));
  const cache = { store, account: "a", hosts: [URL_] };
  const call = { url: URL_, key: "k", body: chatBody(0.7) };
  const answer = await postChat(call, { fetch: host.fetchImpl, cache });
  const parsed = await answer.response.json();
  assert.equal(parsed.choices[0].finish_reason, "content_filter");
  const first = answer.settle("refusal");
  assert.equal(answer.settle("refusal"), first);
  await first;
  assert.equal(store.puts.length, 1);
  const again = await postChat(call, { fetch: host.fetchImpl, cache });
  assert.equal(again.replayed, true);
  assert.deepEqual(await again.response.json(), parsed);
  assert.equal(host.calls.length, 1);
  await assert.rejects(postChat({ ...call, body: { ...call.body, stream: true } }, { fetch: host.fetchImpl, cache }), TypeError);
  const plain = await postChat(call, { fetch: host.fetchImpl });
  assert.ok(plain instanceof Response, "without a cache the result is the raw response");
});

test("sendChat: a temperature-0 success replays with no request and no usage; above 0, unset or a second look sends again", async () => {
  const store = memoryStore();
  const host = counting(() => reply("a cat"));
  const cache = { store, account: "a", hosts: [URL_], classify: (answer) => (answer instanceof SendError ? null : "success") };
  const first = await sendChat({ url: URL_, key: "k", body: chatBody(0) }, { fetch: host.fetchImpl, cache });
  assert.equal(first.replayed, false);
  assert.ok(first.usage);
  const again = await sendChat({ url: URL_, key: "k", body: chatBody(0) }, { fetch: host.fetchImpl, cache });
  assert.equal(host.calls.length, 1);
  assert.deepEqual([again.text, again.model, again.replayed, again.usage], ["a cat", "served/m", true, null]);
  const second = await sendChat({ url: URL_, key: "k", body: chatBody(0) }, { fetch: host.fetchImpl, cache: { ...cache, freshCheck: true } });
  assert.equal(second.replayed, false);
  assert.equal(host.calls.length, 2, "the second look sends again");
  for (const temperature of [0.7, undefined, "0", 0.0001]) {
    await sendChat({ url: URL_, key: "k", body: chatBody(temperature) }, { fetch: host.fetchImpl, cache });
    await sendChat({ url: URL_, key: "k", body: chatBody(temperature) }, { fetch: host.fetchImpl, cache });
  }
  assert.equal(host.calls.length, 10, "a success above 0, unset, a string or near 0 is never replayed");
  assert.equal(store.puts.length, 1);
});

test("sendChat: a refused request replays its SendError even on a second look; a 200 error envelope stores nothing", async () => {
  const store = memoryStore();
  const host = counting(() => json({ error: { message: "blocked by policy", code: 403 } }, 403));
  const cache = { store, account: "a", hosts: [URL_], classify: (answer) => (answer instanceof SendError && answer.status === 403 ? "refusal" : null) };
  const call = () => ({ url: URL_, key: "k", body: chatBody(0.5) });
  const first = await sendChat(call(), { fetch: host.fetchImpl, cache }).catch((error) => error);
  assert.ok(first instanceof SendError && !first.replayed);
  const again = await sendChat(call(), { fetch: host.fetchImpl, cache: { ...cache, freshCheck: true } }).catch((error) => error);
  assert.ok(again instanceof SendError);
  assert.deepEqual([again.kind, again.status, again.providerMessage, again.replayed], ["http", 403, "blocked by policy", true]);
  assert.equal(host.calls.length, 1);
  const envelope = counting(() => json({ error: { message: "upstream" } }));
  await sendChat(call(), { fetch: envelope.fetchImpl, cache: { ...cache, store: memoryStore(), classify: () => null } });
  for (const options of [{ onDelta: () => {} }, { limits: { firstMs: 1, gapMs: 1 } }]) {
    await assert.rejects(sendChat(call(), { fetch: host.fetchImpl, cache, ...options }), TypeError);
  }
  await assert.rejects(sendChat({ ...call(), body: { ...chatBody(0), stream: true } }, { fetch: host.fetchImpl, cache }), TypeError);
});

test("sendWithRetries: a hit returns at once with one replayed record; a busy answer is never stored", async () => {
  const store = memoryStore();
  const records = [];
  const host = counting(() => reply("yes", { usage: { prompt_tokens: 5, completion_tokens: 1, cost: 0.01 } }));
  const cache = { store, account: "a", hosts: [URL_], classify: (result) => (result.ok ? "success" : null) };
  const options = { waits: [1], budgetMs: 10, fetch: host.fetchImpl, onCall: (record) => records.push(record), cache };
  const first = await sendWithRetries({ url: URL_, key: "k", body: chatBody(0) }, options);
  const again = await sendWithRetries({ url: URL_, key: "k", body: chatBody(0) }, options);
  assert.equal(host.calls.length, 1);
  assert.deepEqual([again.ok, again.text, again.replayed, first.replayed], [true, "yes", true, false]);
  assert.deepEqual(records.map(({ ms, ...rest }) => rest)[1],
    { attempt: 1, model: "served/m", provider: null, ok: true, status: 200, aborted: false, inputTokens: null, cachedTokens: null, outputTokens: null, cost: 0, providerMessage: null, replayed: true });
  assert.equal(records[0].cost, 0.01);
  const busy = memoryStore();
  await sendWithRetries({ url: URL_, key: "k", body: chatBody(0) }, { waits: [1], budgetMs: 10, fetch: async () => json({ error: { message: "busy" } }, 503), cache: { ...cache, store: busy, classify: () => "refusal" } });
  assert.deepEqual(busy.puts, []);
});

test("sendWithRetries: only the try that ended the loop is stored", async () => {
  const store = memoryStore();
  let index = 0;
  const answers = [() => json({ error: { message: "busy" } }, 429), () => json({ error: { message: "refused" } }, 400)];
  const cache = { store, account: "a", hosts: [URL_], classify: (result) => (result.ok === false ? "refusal" : null) };
  const result = await sendWithRetries({ url: URL_, key: "k", body: chatBody(0) }, { waits: [1], budgetMs: 10, random: () => 0.5, fetch: async () => answers[index++](), cache });
  assert.deepEqual(result, { ok: false, status: 400, body: { error: { message: "refused" } }, replayed: false });
  assert.deepEqual(store.puts.map(({ entry }) => entry.status), [400]);
});

test("a failing get is a ReplyCacheError: nothing is sent, no retry, no deadline result, no fallback, never network", async () => {
  const host = counting(() => reply("x"));
  const failGet = new Error("disk");
  const cache = { store: memoryStore({ failGet }), account: "a", hosts: [URL_], classify: () => null };
  const error = await sendWithRetries({ url: URL_, key: "k", body: chatBody(0) }, { waits: [1, 1], budgetMs: 10, deadlineMs: 1000, fetch: host.fetchImpl, cache }).catch((caught) => caught);
  assert.ok(error instanceof ReplyCacheError);
  assert.equal(error.op, "get");
  assert.equal(error.cause, failGet);
  assert.ok(!(error instanceof SendError));
  await assert.rejects(postImages({ url: IMAGES, key: "k", body: {} }, { fetch: host.fetchImpl, cache: { ...cache, hosts: HOSTS } }), ReplyCacheError);
  await assert.rejects(sendChat({ url: URL_, key: "k", body: chatBody(0) }, { fetch: host.fetchImpl, cache }), ReplyCacheError);
  assert.equal(host.calls.length, 0);
  let asked = false;
  const fallback = await withFallback(() => sendChat({ url: URL_, key: "k", body: chatBody(0) }, { fetch: host.fetchImpl, cache }),
    async () => "fallback", { emitted: () => false, eligible: () => { asked = true; return true; } }).catch((caught) => caught);
  assert.ok(fallback instanceof ReplyCacheError);
  assert.equal(asked, false, "the fallback's eligibility was never asked");
  const bad = { store: { get: async () => ({ format: "other" }), put: async () => {} }, account: "a", hosts: [URL_], classify: () => null };
  await assert.rejects(sendChat({ url: URL_, key: "k", body: chatBody(0) }, { fetch: host.fetchImpl, cache: bad }), (caught) => caught instanceof ReplyCacheError && caught.cause instanceof ReplyEntryError);
});

test("a failing put throws a ReplyCacheError, or with onPutError reports it and gives the normal answer", async () => {
  const failPut = new Error("full");
  const host = counting(() => json({ error: { message: "no" } }, 400));
  const cache = { store: memoryStore({ failPut }), account: "a", hosts: HOSTS, classify: () => "refusal" };
  const thrown = await postImages({ url: IMAGES, key: "k", body: {} }, { fetch: host.fetchImpl, cache }).catch((caught) => caught);
  assert.ok(thrown instanceof ReplyCacheError && thrown.op === "put" && thrown.cause === failPut);
  const reported = [];
  const answer = await postImages({ url: IMAGES, key: "k", body: {} }, { fetch: host.fetchImpl, cache: { ...cache, onPutError: (error) => reported.push(error) } });
  assert.deepEqual(answer, { ok: false, status: 400, failure: { error: { message: "no" } }, replayed: false });
  assert.equal(reported.length, 1);
  assert.equal(reported[0].op, "put");
  const raw = await postVenice({ endpoint: "generate", key: "k", body: {}, baseUrl: "https://llm.example/v1" }, { fetch: host.fetchImpl, cache });
  await raw.response.arrayBuffer();
  await assert.rejects(raw.settle("refusal"), ReplyCacheError);
});

test("a stored entry the store gives back is a valid llm-kit.reply-entry/v1 of the step layer", async () => {
  const store = memoryStore();
  await postImages({ url: IMAGES, key: "k", body: { p: 1 } }, { fetch: async () => json({ error: "no" }, 400, { "set-cookie": "s=1", "x-request-id": "r" }), cache: { store, account: "a", hosts: HOSTS, classify: () => "refusal" } });
  const [{ layer, entry }] = store.puts;
  assert.equal(layer, "step");
  assert.deepEqual(readEntry(entry), entry);
  assert.deepEqual(Object.keys(entry.headers), ["content-type"], "only the kept headers");
  assert.deepEqual(JSON.parse(Buffer.from(entry.body, "base64").toString("utf8")), { error: "no" });
});

test("without a cache, a transport that answers with only json() reads as in 0.2.0", async () => {
  const minimal = (value, status = 200) => ({ ok: status < 300, status, headers: new Headers(), json: async () => value });
  assert.deepEqual(await postImages({ url: IMAGES, key: "k", body: {} }, { fetch: async () => minimal({ data: [{ b64_json: "aW1n" }] }) }),
    { ok: true, status: 200, payload: { data: [{ b64_json: "aW1n" }] }, image: "aW1n", link: null, usage: null, cost: null, replayed: false });
  const retried = await sendWithRetries({ url: URL_, key: "k", body: chatBody(0) }, { waits: [], budgetMs: 0, fetch: async () => minimal({ model: "m", choices: [{ message: { content: "ok" } }] }) });
  assert.deepEqual([retried.ok, retried.text], [true, "ok"]);
});
