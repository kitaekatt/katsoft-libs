// The media transports: each request's URL, init keys, headers and body exactly; each answer, failure and abort.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ELEVENLABS_URL, FAL_RUN_URL, IDEOGRAM_URL, VENICE_URL, elevenLabsError, elevenLabsRequest, fetchFile, ideogramEdit,
  isVeniceViolation, postFal, postImages, postVenice,
} from "../dist/index.js";
import { json } from "./support.mjs";

/** A transport that records each request (url, init) and answers with `answer(url, init, index)`. */
function host(answer) {
  const calls = [];
  const fetchImpl = async (url, init) => { calls.push({ url: String(url), init }); return answer(String(url), init, calls.length - 1); };
  return { calls, fetchImpl };
}
const broken = () => new Response(new ReadableStream({ start(controller) { controller.error(new TypeError("terminated")); } }));
const aborted = () => { const controller = new AbortController(); controller.abort(new Error("caller left")); return controller.signal; };

test("postImages sends the body as given with Authorization, Content-Type, then the caller's headers", async () => {
  const { calls, fetchImpl } = host(() => json({ data: [{ b64_json: "aW1n", url: "https://cdn.example/x.png" }], usage: { cost: 0.04 } }));
  const signal = new AbortController().signal;
  const body = { prompt: "A lake.", model: "vendor/image", n: 1 };
  const answer = await postImages({ url: "https://images.example/v1/images", key: "k", headers: { "X-Label": "art" }, body }, { fetch: fetchImpl, signal });
  assert.deepEqual(answer, { ok: true, status: 200, payload: { data: [{ b64_json: "aW1n", url: "https://cdn.example/x.png" }], usage: { cost: 0.04 } },
    image: "aW1n", link: "https://cdn.example/x.png", usage: { cost: 0.04 }, cost: 0.04 });
  assert.equal(calls[0].url, "https://images.example/v1/images");
  assert.deepEqual(Object.keys(calls[0].init), ["method", "signal", "headers", "body"]);
  assert.deepEqual(Object.entries(calls[0].init.headers), [["Authorization", "Bearer k"], ["Content-Type", "application/json"], ["X-Label", "art"]]);
  assert.equal(calls[0].init.body, JSON.stringify(body));
  assert.equal(calls[0].init.signal, signal);
});

test("postImages: an error status, a lost connection and bodies that are not JSON; an abort throws", async () => {
  assert.deepEqual(await postImages({ url: "u", key: "k", body: {} }, { fetch: async () => json({ error: "no" }, 400) }), { ok: false, status: 400, failure: { error: "no" } });
  assert.deepEqual(await postImages({ url: "u", key: "k", body: {} }, { fetch: async () => new Response("bad", { status: 502 }) }), { ok: false, status: 502, failure: {} });
  assert.deepEqual(await postImages({ url: "u", key: "k", body: {} }, { fetch: async () => { throw new TypeError("fetch failed"); } }), { ok: false, status: null, failure: {} });
  const empty = await postImages({ url: "u", key: "k", body: {} }, { fetch: async () => new Response("<html>") });
  assert.deepEqual(empty, { ok: true, status: 200, payload: {}, image: null, link: null, usage: null, cost: null });
  const signal = aborted();
  await assert.rejects(postImages({ url: "u", key: "k", body: {} }, { fetch: async () => { throw signal.reason; }, signal }), /caller left/);
});

test("postVenice returns the raw response of POST <base>/image/<endpoint>; isVeniceViolation reads the header", async () => {
  const { calls, fetchImpl } = host(() => new Response("webp", { headers: { "content-type": "image/webp", "x-venice-is-content-violation": "true" } }));
  const response = await postVenice({ endpoint: "multi-edit", key: "v", body: { prompt: "x" } }, { fetch: fetchImpl });
  assert.equal(calls[0].url, `${VENICE_URL}/image/multi-edit`);
  assert.deepEqual(Object.keys(calls[0].init), ["method", "headers", "signal", "body"]);
  assert.deepEqual(calls[0].init.headers, { Authorization: "Bearer v", "Content-Type": "application/json" });
  assert.equal(calls[0].init.body, '{"prompt":"x"}');
  assert.equal(isVeniceViolation(response), true);
  assert.equal(isVeniceViolation(new Response("")), false);
  await assert.rejects(postVenice({ endpoint: "generate", key: "v", body: {}, baseUrl: "https://v.test" }, { fetch: async () => { throw new TypeError("fetch failed"); } }), /fetch failed/);
});

test("ideogramEdit posts the caller's form with only Api-Key and reads the first result or the reject reason", async () => {
  const form = new FormData();
  form.append("prompt", "short hair");
  const { calls, fetchImpl } = host(() => json({ data: [{ url: "https://i.test/out.png", is_image_safe: false }] }));
  assert.deepEqual(await ideogramEdit({ key: "i", model: "edit-4", form }, { fetch: fetchImpl }), { ok: true, status: 200, url: "https://i.test/out.png", safe: false });
  assert.equal(calls[0].url, `${IDEOGRAM_URL}/v2/image/precise-edit/edit-4`);
  assert.deepEqual(Object.keys(calls[0].init), ["method", "headers", "body", "signal"]);
  assert.deepEqual(calls[0].init.headers, { "Api-Key": "i" });
  assert.equal(calls[0].init.body, form);
  assert.deepEqual(await ideogramEdit({ key: "i", model: "m", form }, { fetch: async () => json({ reject_reason: "unsafe" }, 422) }), { ok: false, status: 422, reason: "unsafe" });
  assert.deepEqual(await ideogramEdit({ key: "i", model: "m", form }, { fetch: async () => new Response("x", { status: 500 }) }), { ok: false, status: 500, reason: null });
  assert.deepEqual(await ideogramEdit({ key: "i", model: "m", form }, { fetch: async () => json({ data: [] }) }), { ok: true, status: 200, url: null, safe: true });
  await assert.rejects(ideogramEdit({ key: "i", model: "m", form }, { fetch: async () => new Response("<html>") }), SyntaxError);
  await assert.rejects(ideogramEdit({ key: "i", model: "m", form }, { fetch: async () => { throw new TypeError("fetch failed"); } }), /fetch failed/);
});

test("postFal returns the raw response of POST https://fal.run/<endpoint> with Key auth", async () => {
  const { calls, fetchImpl } = host(() => json({ audio: { url: "https://fal.media/a.mp3" } }));
  const response = await postFal({ endpoint: "vendor/tts", key: "f", input: { text: "Hi.", voice: "Sarah" } }, { fetch: fetchImpl });
  assert.equal((await response.json()).audio.url, "https://fal.media/a.mp3");
  assert.equal(calls[0].url, `${FAL_RUN_URL}/vendor/tts`);
  assert.deepEqual(Object.keys(calls[0].init), ["method", "headers", "body", "signal"]);
  assert.deepEqual(calls[0].init.headers, { Authorization: "Key f", "Content-Type": "application/json" });
  assert.equal(calls[0].init.body, '{"text":"Hi.","voice":"Sarah"}');
});

test("elevenLabsRequest: a JSON body with Accept, a form, and a DELETE with no body", async () => {
  const { calls, fetchImpl } = host(() => new Response("ID3"));
  const signal = new AbortController().signal;
  await elevenLabsRequest("/v1/text-to-speech/v?output_format=mp3", { key: "e", json: { text: "Hi." }, accept: "audio/mpeg" }, { fetch: fetchImpl, signal });
  const form = new FormData();
  await elevenLabsRequest("/v1/voices/add", { key: "e", form }, { fetch: fetchImpl });
  await elevenLabsRequest("/v1/voices/v1", { key: "e", method: "DELETE" }, { fetch: fetchImpl });
  assert.deepEqual(calls.map((call) => call.url), [`${ELEVENLABS_URL}/v1/text-to-speech/v?output_format=mp3`, `${ELEVENLABS_URL}/v1/voices/add`, `${ELEVENLABS_URL}/v1/voices/v1`]);
  assert.deepEqual(Object.keys(calls[0].init), ["method", "headers", "body", "signal"]);
  assert.deepEqual(Object.entries(calls[0].init.headers), [["xi-api-key", "e"], ["Content-Type", "application/json"], ["Accept", "audio/mpeg"]]);
  assert.equal(calls[0].init.body, '{"text":"Hi."}');
  assert.equal(calls[0].init.signal, signal);
  assert.deepEqual(calls[1].init.headers, { "xi-api-key": "e" });
  assert.equal(calls[1].init.body, form);
  assert.deepEqual(Object.keys(calls[2].init), ["method", "headers", "signal"]);
  assert.equal(calls[2].init.method, "DELETE");
});

test("elevenLabsError reads the code and detail of each error body shape, never longer than its limit", () => {
  assert.deepEqual(elevenLabsError({ detail: { status: "Quota Exceeded!", message: "You have 0\ncredits." } }), { code: "quota_exceeded_", detail: "You have 0 credits." });
  assert.deepEqual(elevenLabsError({ detail: "bad voice settings" }), { code: null, detail: "bad voice settings" });
  assert.deepEqual(elevenLabsError({ detail: [{ type: "value_error", msg: "Voice not found" }] }), { code: "value_error", detail: "Voice not found" });
  assert.deepEqual(elevenLabsError(null), { code: null, detail: null });
  assert.equal(elevenLabsError({ detail: { code: "x".repeat(99), message: "y".repeat(300) } }).detail.length, 200);
  assert.equal(elevenLabsError({ detail: { code: "x".repeat(99) } }).code.length, 60);
});

test("fetchFile downloads with init {signal} only, and names each failure", async () => {
  const { calls, fetchImpl } = host(() => new Response("bytes"));
  const signal = new AbortController().signal;
  const file = await fetchFile("https://cdn.example/a", { fetch: fetchImpl, signal });
  assert.equal(file.ok, true);
  assert.equal(Buffer.from(file.bytes).toString(), "bytes");
  assert.deepEqual(calls[0].init, { signal });
  assert.deepEqual(await fetchFile("u", { fetch: async () => new Response("", { status: 404 }) }), { ok: false, status: 404, reason: "http" });
  assert.deepEqual(await fetchFile("u", { fetch: async () => { throw new TypeError("fetch failed"); } }), { ok: false, status: null, reason: "network" });
  assert.deepEqual(await fetchFile("u", { fetch: async () => new Response("") }), { ok: false, status: 200, reason: "empty" });
  assert.deepEqual(await fetchFile("u", { maxBytes: 3, fetch: async () => new Response("x", { headers: { "content-length": "9" } }) }), { ok: false, status: 200, reason: "too_large" });
  assert.deepEqual(await fetchFile("u", { maxBytes: 3, fetch: async () => new Response("abcdef") }), { ok: false, status: 200, reason: "over_limit" });
  assert.equal((await fetchFile("u", { fetch: async () => new Response("x", { headers: { "content-length": "9" } }) })).ok, true, "no limit, no header check");
  await assert.rejects(fetchFile("u", { fetch: async () => broken() }), /terminated/);
  const gone = aborted();
  await assert.rejects(fetchFile("u", { fetch: async () => { throw gone.reason; }, signal: gone }), /caller left/);
});
