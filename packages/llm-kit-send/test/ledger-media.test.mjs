// The media transports in the call ledger: one pair for each paid request; a parsed answer ends when it is read, a raw
// response when its body is read or cancelled; GET, DELETE and file downloads are not recorded; a replay is recorded.
import assert from "node:assert/strict";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import {
  CallLedgerError, activeCall, elevenLabsRequest, fetchFile, ideogramEdit, isVeniceViolation, postFal, postImages, postVenice,
  requireCallLedger, setCallLedger,
} from "../dist/index.js";
import { check } from "./schema-check.mjs";
import { PACKAGE, json, ledgerSink, readJson } from "./support.mjs";

const SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "paid-call.v1.schema.json"));
const IMAGES = "https://images.example/v1/images";

let sink;
beforeEach(() => { sink = ledgerSink(); setCallLedger(sink); });
afterEach(() => {
  for (const record of sink.records) assert.deepEqual(check(record, SCHEMA), [], JSON.stringify(record));
  setCallLedger(null);
  requireCallLedger(false);
});
function memoryStore() {
  const rows = new Map();
  return { rows, async get(layer, key) { return rows.get(`${layer}:${key}`) ?? null; }, async put(layer, key, entry) { rows.set(`${layer}:${key}`, entry); } };
}

test("postImages: one pair with the reported cost and usage, the model of the body, the labels; the begin comes before the send", async () => {
  const events = [];
  const events2 = sink.events;
  const answer = await postImages({ url: `${IMAGES}?key=QUERY`, key: "k", body: { prompt: "A lake.", model: "vendor/image" } }, {
    labels: { unit: 2 },
    fetch: async () => { events.push(events2.length); return json({ data: [{ b64_json: "aW1n" }], usage: { cost: 0.04, prompt_tokens: 3 } }); },
  });
  assert.equal(answer.ok, true);
  assert.equal(events[0], 1, "the begin was told before the send");
  const [begin, end] = sink.records;
  assert.deepEqual([begin.transport, begin.method, begin.host, begin.path, begin.model, begin.stream, begin.labels], ["postImages", "POST", "images.example", "/v1/images", "vendor/image", false, { unit: 2 }]);
  assert.deepEqual([end.ok, end.status, end.errorKind, end.cost, end.inputTokens, end.replayed], [true, 200, null, 0.04, 3, false]);
  assert.ok(!JSON.stringify(sink.records).includes("QUERY"));
});

test("postImages: an HTTP error, no answer, a body that is not JSON and an abort each end the call; an abort still throws", async () => {
  await postImages({ url: "u", key: "k", body: { model: "m" } }, { fetch: async () => json({ error: "no" }, 400) });
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].status, sink.ends()[0].errorKind], [false, 400, "http"]);
  await postImages({ url: "u", key: "k", body: {} }, { fetch: async () => { throw new TypeError("fetch failed"); } });
  assert.deepEqual([sink.ends()[1].ok, sink.ends()[1].status, sink.ends()[1].errorKind, sink.ends()[1].aborted], [false, null, "network", false]);
  await postImages({ url: "u", key: "k", body: {} }, { fetch: async () => new Response("<html>") });
  assert.deepEqual([sink.ends()[2].ok, sink.ends()[2].status, sink.ends()[2].errorKind], [false, 200, "body"]);
  const controller = new AbortController();
  controller.abort(new Error("left"));
  await assert.rejects(postImages({ url: "u", key: "k", body: {} }, { signal: controller.signal, fetch: async () => { throw controller.signal.reason; } }), /left/);
  assert.deepEqual([sink.ends()[3].errorKind, sink.ends()[3].aborted], ["aborted", true]);
  assert.equal(sink.begins()[0].host, "", "an unparsable url gives an empty host");
});

test("postImages with the reply cache: a stored refusal replays as a pair with replayed true and nothing sent", async () => {
  const cache = { store: memoryStore(), account: "a", hosts: ["https://images.example/"], classify: (answer) => (answer.ok === false ? "refusal" : null) };
  let sent = 0;
  const fetch = async () => { sent += 1; return json({ error: "unsafe" }, 400); };
  await postImages({ url: IMAGES, key: "k", body: { model: "m", prompt: "x" } }, { fetch, cache });
  const again = await postImages({ url: IMAGES, key: "k", body: { model: "m", prompt: "x" } }, { fetch, cache });
  assert.equal(again.replayed, true);
  assert.equal(sent, 1);
  assert.equal(sink.begins().length, 2);
  assert.deepEqual([sink.ends()[1].replayed, sink.ends()[1].cost, sink.ends()[1].status, sink.ends()[1].ok], [true, 0, 400, false]);
});

test("ideogramEdit: one pair with no cost; a refusal and a body that is not JSON end the call; a replay is recorded", async () => {
  const form = () => { const f = new FormData(); f.append("prompt", "short hair"); return f; };
  await ideogramEdit({ key: "i", model: "edit-4", form: form() }, { fetch: async () => json({ data: [{ url: "https://i.test/out.png" }] }) });
  const [begin, end] = sink.records;
  assert.deepEqual([begin.transport, begin.host, begin.path, begin.model, end.ok, end.status, end.cost], ["ideogramEdit", "api.ideogram.ai", "/v2/image/precise-edit/edit-4", "edit-4", true, 200, null]);
  await ideogramEdit({ key: "i", model: "edit-4", form: form() }, { fetch: async () => json({ error: "x", reject_reason: "unsafe" }, 422) });
  assert.deepEqual([sink.ends()[1].ok, sink.ends()[1].errorKind, sink.ends()[1].status], [false, "http", 422]);
  await assert.rejects(ideogramEdit({ key: "i", model: "edit-4", form: form() }, { fetch: async () => new Response("<html>") }), SyntaxError);
  assert.deepEqual([sink.ends()[2].ok, sink.ends()[2].errorKind], [false, "body"]);
  await assert.rejects(ideogramEdit({ key: "i", model: "edit-4", form: form() }, { fetch: async () => { throw new TypeError("fetch failed"); } }), /fetch failed/);
  assert.deepEqual([sink.ends()[3].errorKind, sink.ends()[3].status], ["network", null]);
  const cache = { store: memoryStore(), account: "i", hosts: ["https://api.ideogram.ai/"], classify: (answer) => (answer.ok === false ? "refusal" : null) };
  const refuse = async () => json({ reject_reason: "unsafe" }, 422);
  await ideogramEdit({ key: "i", model: "edit-4", form: form() }, { fetch: refuse, cache });
  const replay = await ideogramEdit({ key: "i", model: "edit-4", form: form() }, { fetch: refuse, cache });
  assert.equal(replay.replayed, true);
  assert.deepEqual([sink.ends()[5].replayed, sink.ends()[5].cost], [true, 0]);
});

test("postVenice: the pair ends when the picture bytes are read; the violation header is still readable; the model is the body's", async () => {
  const response = await postVenice({ endpoint: "generate", key: "v", body: { model: "venice/x", prompt: "p" } }, {
    fetch: async () => new Response(new Uint8Array([1, 2, 3]), { headers: { "content-type": "image/webp", "x-venice-is-content-violation": "true" } }),
  });
  assert.equal(isVeniceViolation(response), true);
  assert.equal(sink.ends().length, 0);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), new Uint8Array([1, 2, 3]));
  assert.deepEqual([sink.begins()[0].transport, sink.begins()[0].host, sink.begins()[0].path, sink.begins()[0].model], ["postVenice", "api.venice.ai", "/api/v1/image/generate", "venice/x"]);
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].status, sink.ends()[0].usage], [true, 200, null]);
  await postVenice({ endpoint: "multi-edit", key: "v", body: { prompt: "p" } }, { fetch: async () => new Response("x") });
  assert.equal(sink.begins()[1].model, "multi-edit", "no model in the body: the endpoint");
  assert.equal(sink.ends().length, 1, "a body never read leaves the record open");
  await assert.rejects(postVenice({ endpoint: "generate", key: "v", body: {} }, { fetch: async () => { throw new TypeError("fetch failed"); } }), /fetch failed/);
  assert.equal(sink.ends()[1].errorKind, "network");
});

test("postVenice with the reply cache: a miss ends when read and still settles; a stored refusal replays as a recorded pair", async () => {
  const cache = { store: memoryStore(), account: "venice", hosts: ["https://api.venice.ai/"] };
  let sent = 0;
  const fetch = async () => { sent += 1; return new Response("refused", { status: 422, headers: { "content-type": "application/json" } }); };
  const first = await postVenice({ endpoint: "generate", key: "v", body: { model: "m" } }, { fetch, cache });
  assert.equal(await first.response.text(), "refused");
  await first.settle("refusal");
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].status, sink.ends()[0].errorKind], [false, 422, "http"]);
  assert.equal(cache.store.rows.size, 1, "the body passed through the bounded copy and was stored");
  const again = await postVenice({ endpoint: "generate", key: "v", body: { model: "m" } }, { fetch, cache });
  assert.equal(again.replayed, true);
  assert.equal(sent, 1);
  assert.deepEqual([sink.begins()[1].transport, sink.ends()[1].replayed, sink.ends()[1].status], ["postVenice", true, 422]);
});

test("postFal: a pair at the body's end with the endpoint as the model; the answer is read as given", async () => {
  const response = await postFal({ endpoint: "fal-ai/voice", key: "f", input: { text: "x" } }, { fetch: async () => json({ audio: { url: "https://a" } }) });
  assert.equal(sink.ends().length, 0);
  assert.deepEqual(await response.json(), { audio: { url: "https://a" } });
  assert.deepEqual([sink.begins()[0].transport, sink.begins()[0].host, sink.begins()[0].path, sink.begins()[0].model, sink.ends()[0].ok], ["postFal", "fal.run", "/fal-ai/voice", "fal-ai/voice", true]);
  await assert.rejects(postFal({ endpoint: "e", key: "f", input: {} }, { fetch: async () => { throw new TypeError("fetch failed"); } }), /fetch failed/);
  assert.equal(sink.ends()[1].errorKind, "network");
});

test("elevenLabsRequest: a POST is recorded with its path and no query; a GET and a DELETE are not, and run outside any call", async () => {
  let inside = "unset";
  const response = await elevenLabsRequest("/v1/text-to-speech/voice1?output_format=mp3_44100_128", { key: "e", json: { text: "SECRET TEXT", model_id: "eleven_v3" }, accept: "audio/mpeg" },
    { fetch: async () => new Response(new Uint8Array([9]), { headers: { "content-type": "audio/mpeg" } }), labels: { chars: 11 } });
  await response.arrayBuffer();
  assert.deepEqual([sink.begins()[0].transport, sink.begins()[0].path, sink.begins()[0].model, sink.begins()[0].labels, sink.ends()[0].ok], ["elevenLabsRequest", "/v1/text-to-speech/voice1", "eleven_v3", { chars: 11 }, true]);
  assert.ok(!JSON.stringify(sink.records).includes("SECRET TEXT"));
  for (const method of ["GET", "DELETE"]) {
    await elevenLabsRequest("/v1/voices/v1", { key: "e", method }, { fetch: async () => { inside = activeCall(); return json({}); } });
    assert.equal(inside, null);
  }
  assert.equal(sink.begins().length, 1, "no record for a GET or a DELETE");
  await assert.rejects(elevenLabsRequest("/v1/sound-generation", { key: "e", json: {} }, { fetch: async () => { throw new TypeError("fetch failed"); } }), /fetch failed/);
  assert.equal(sink.begins()[1].model, "");
  assert.equal(sink.ends()[1].errorKind, "network");
});

test("fetchFile is not recorded and runs outside any call; with the ledger required and no sink a recorded transport throws before it sends", async () => {
  let inside = "unset";
  await fetchFile("https://cdn.example/a.png", { fetch: async () => { inside = activeCall(); return new Response(new Uint8Array([1])); } });
  assert.equal(inside, null);
  assert.deepEqual(sink.records, []);
  setCallLedger(null);
  requireCallLedger(true);
  let sent = 0;
  const fetch = async () => { sent += 1; return json({}); };
  for (const work of [() => postImages({ url: "u", key: "k", body: {} }, { fetch }), () => postVenice({ endpoint: "g", key: "k", body: {} }, { fetch }),
    () => postFal({ endpoint: "e", key: "k", input: {} }, { fetch }), () => elevenLabsRequest("/v1/x", { key: "k", json: {} }, { fetch }),
    () => ideogramEdit({ key: "k", model: "m", form: new FormData() }, { fetch })]) {
    await assert.rejects(work(), CallLedgerError);
  }
  assert.equal(sent, 0);
  await fetchFile("https://cdn.example/a.png", { fetch });
  assert.equal(sent, 1, "an unrecorded transport is not blocked");
});

test("a bad label is a rejected promise from every media transport, before anything is sent", async () => {
  let sent = 0;
  const fetch = async () => { sent += 1; return json({}); };
  const labels = { unit: [1] };
  for (const work of [() => postImages({ url: "u", key: "k", body: {} }, { fetch, labels }), () => postVenice({ endpoint: "g", key: "k", body: {} }, { fetch, labels }),
    () => postFal({ endpoint: "e", key: "k", input: {} }, { fetch, labels }), () => elevenLabsRequest("/v1/x", { key: "k", json: {} }, { fetch, labels })]) {
    await assert.rejects(work(), TypeError);
  }
  assert.equal(sent, 0);
});
