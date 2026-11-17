// The fal and Sunra queue jobs: the request sequence, the poll, each failure stage, `completed`, and the abort.
import assert from "node:assert/strict";
import { test } from "node:test";

import { FAL_QUEUE_URL, SUNRA_QUEUE_URL, falQueue, sunraQueue } from "../dist/index.js";
import { json } from "./support.mjs";

/** A queue host: `submit` answers the POST, `states` answer the status reads in turn, `result` the result read. */
function queue({ submit, states, result, abortAt = -1 }) {
  const calls = [];
  const controller = new AbortController();
  const left = [...states];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    if (calls.length - 1 === abortAt) controller.abort(new Error("caller left"));
    if (init.signal?.aborted) throw init.signal.reason;
    if (init.method === "POST") return submit();
    if (String(url).endsWith("/status")) return left.shift()();
    return result();
  };
  return { calls, fetchImpl, signal: controller.signal };
}
const falSubmit = () => json({ request_id: "r1", status_url: "https://q.test/m/requests/r1/status", response_url: "https://q.test/m/requests/r1" });
const state = (status, extra = {}) => () => json({ status, ...extra });

test("falQueue: submit, poll until COMPLETED, read the result; Key auth on each request", async () => {
  const host = queue({ submit: falSubmit, states: [state("IN_QUEUE"), state("IN_PROGRESS"), state("COMPLETED")], result: () => json({ video: { url: "https://v.test/a.mp4" } }) });
  const result = await falQueue({ model: "vendor/video", key: "f", input: { prompt: "x" }, pollMs: 0 }, { fetch: host.fetchImpl, signal: host.signal });
  assert.deepEqual(result, { ok: true, output: { video: { url: "https://v.test/a.mp4" } } });
  assert.deepEqual(host.calls.map((call) => [call.init.method ?? "GET", call.url]), [["POST", `${FAL_QUEUE_URL}/vendor/video`],
    ["GET", "https://q.test/m/requests/r1/status"], ["GET", "https://q.test/m/requests/r1/status"], ["GET", "https://q.test/m/requests/r1/status"], ["GET", "https://q.test/m/requests/r1"]]);
  assert.deepEqual(Object.keys(host.calls[0].init), ["method", "body", "headers", "signal"]);
  assert.deepEqual(Object.keys(host.calls[1].init), ["headers", "signal"]);
  assert.deepEqual(host.calls[0].init.headers, { Authorization: "Key f", "Content-Type": "application/json" });
  assert.equal(host.calls[0].init.body, '{"prompt":"x"}');
  assert.ok(host.calls.every((call) => call.init.signal === host.signal));
});

test("falQueue: the caller's refusal rule is checked first, on every answer", async () => {
  const refusal = (status, body) => status === 422 || /policy/.test(JSON.stringify(body?.detail ?? ""));
  const declined = queue({ submit: () => json({ detail: "x" }, 422), states: [], result: () => json({}) });
  assert.deepEqual(await falQueue({ model: "m", key: "f", input: {}, pollMs: 0, refusal }, { fetch: declined.fetchImpl }),
    { ok: false, stage: "submit", reason: "refused", status: 422, body: { detail: "x" }, completed: false });
  const later = queue({ submit: falSubmit, states: [state("IN_PROGRESS", { detail: "policy" })], result: () => json({}) });
  const result = await falQueue({ model: "m", key: "f", input: {}, pollMs: 0, refusal }, { fetch: later.fetchImpl });
  assert.equal(result.reason, "refused");
  assert.equal(result.stage, "status");
});

test("falQueue: each failure keeps its stage, status and whether fal completed the job", async () => {
  const run = (host, pollMs = 0) => falQueue({ model: "m", key: "f", input: {}, pollMs }, { fetch: host.fetchImpl, signal: host.signal });
  assert.deepEqual(await run(queue({ submit: () => json({ error: 1 }, 500), states: [], result: () => json({}) })),
    { ok: false, stage: "submit", reason: "http", status: 500, body: { error: 1 }, completed: false });
  assert.deepEqual(await run(queue({ submit: () => new Response("<html>"), states: [], result: () => json({}) })),
    { ok: false, stage: "submit", reason: "body", status: 200, body: null, completed: false });
  assert.deepEqual(await run(queue({ submit: () => json({ request_id: "r1" }), states: [], result: () => json({}) })),
    { ok: false, stage: "submit", reason: "shape", status: 200, body: { request_id: "r1" }, completed: false });
  assert.deepEqual(await run(queue({ submit: falSubmit, states: [state("FAILED")], result: () => json({}) })),
    { ok: false, stage: "status", reason: "state", status: 200, body: { status: "FAILED" }, completed: false });
  assert.deepEqual(await run(queue({ submit: falSubmit, states: [state("COMPLETED")], result: () => json({}, 500) })),
    { ok: false, stage: "result", reason: "http", status: 500, body: {}, completed: true });
  assert.deepEqual(await run(queue({ submit: () => { throw new TypeError("fetch failed"); }, states: [], result: () => json({}) })),
    { ok: false, stage: "submit", reason: "network", status: null, body: null, completed: false });
  assert.deepEqual(await run(queue({ submit: falSubmit, states: [state("COMPLETED")], result: () => json({}), abortAt: 2 })),
    { ok: false, stage: "result", reason: "aborted", status: null, body: null, completed: true });
  const waiting = queue({ submit: falSubmit, states: [state("IN_QUEUE"), state("IN_QUEUE")], result: () => json({}), abortAt: 1 });
  const early = await run(waiting, 10_000);
  assert.deepEqual(early, { ok: false, stage: "status", reason: "aborted", status: null, body: null, completed: false });
});

const sunraStates = (...states) => states.map((value) => () => json(value));

test("sunraQueue: submit, poll the status URL of the request id, read the result", async () => {
  const host = queue({ submit: () => json({ request_id: "pd_1" }), states: sunraStates({ status: "IN_QUEUE" }, { status: "COMPLETED", success: true }), result: () => json({ audio: { url: "https://s.test/a.mp3" } }) });
  const result = await sunraQueue({ endpoint: "vendor/tts", key: "s", input: { text: "Hi." }, pollMs: 1 }, { fetch: host.fetchImpl, signal: host.signal });
  assert.deepEqual(result, { ok: true, output: { audio: { url: "https://s.test/a.mp3" } } });
  assert.deepEqual(host.calls.map((call) => call.url), [`${SUNRA_QUEUE_URL}/vendor/tts`, `${SUNRA_QUEUE_URL}/requests/pd_1/status`, `${SUNRA_QUEUE_URL}/requests/pd_1/status`, `${SUNRA_QUEUE_URL}/requests/pd_1`]);
  assert.deepEqual(Object.keys(host.calls[0].init), ["method", "headers", "body", "signal"]);
  assert.deepEqual(Object.keys(host.calls[1].init), ["headers", "signal"]);
  assert.deepEqual(host.calls[0].init.headers, { Authorization: "Key s", "Content-Type": "application/json" });
});

test("sunraQueue: each failure code, the refusal code, completed, and the abort", async () => {
  const run = (host) => sunraQueue({ endpoint: "e", key: "s", input: {}, pollMs: 1 }, { fetch: host.fetchImpl, signal: host.signal });
  const submitted = () => json({ request_id: "pd_1" });
  assert.deepEqual(await run(queue({ submit: () => json({}, 500), states: [], result: () => json({}) })), { ok: false, stage: "submit", refused: false, code: "submit_500", completed: false });
  assert.deepEqual(await run(queue({ submit: () => json({ request_id: "../x" }), states: [], result: () => json({}) })), { ok: false, stage: "submit", refused: false, code: "no_request_id", completed: false });
  assert.deepEqual(await run(queue({ submit: submitted, states: [() => json({}, 503)], result: () => json({}) })), { ok: false, stage: "status", refused: false, code: "status_503", completed: false });
  assert.deepEqual(await run(queue({ submit: submitted, states: sunraStates({ status: "CANCELED" }), result: () => json({}) })), { ok: false, stage: "status", refused: false, code: "canceled", completed: false });
  assert.deepEqual(await run(queue({ submit: submitted, states: sunraStates({ status: "COMPLETED", success: false, error: { code: "unsafe_content" } }), result: () => json({}) })),
    { ok: false, stage: "status", refused: true, code: "unsafe_content", completed: true });
  assert.deepEqual(await run(queue({ submit: submitted, states: sunraStates({ status: "COMPLETED", error: {} }), result: () => json({}) })),
    { ok: false, stage: "status", refused: false, code: "failed", completed: true });
  assert.deepEqual(await run(queue({ submit: submitted, states: sunraStates({ status: "COMPLETED", success: true }), result: () => json({}, 500) })),
    { ok: false, stage: "result", refused: false, code: "result_500", completed: true });
  assert.deepEqual(await run(queue({ submit: submitted, states: sunraStates({ status: "COMPLETED", success: true }), result: () => new Response("<html>") })),
    { ok: false, stage: "result", refused: false, code: "network", completed: true });
  assert.deepEqual(await run(queue({ submit: () => { throw new TypeError("fetch failed"); }, states: [], result: () => json({}) })), { ok: false, stage: "submit", refused: false, code: "network", completed: false });
  assert.deepEqual(await run(queue({ submit: submitted, states: sunraStates({ status: "IN_QUEUE" }, { status: "IN_QUEUE" }), result: () => json({}), abortAt: 1 })),
    { ok: false, stage: "status", refused: false, code: "aborted", completed: false });
});
