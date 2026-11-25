// The queue jobs in the call ledger: one pair for each job, from the submit to the result; the polls are not recorded
// but run inside the job's active call.
import assert from "node:assert/strict";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import { CallLedgerError, activeCall, falQueue, requireCallLedger, setCallLedger, sunraQueue } from "../dist/index.js";
import { check } from "./schema-check.mjs";
import { PACKAGE, json, ledgerSink, readJson } from "./support.mjs";

const SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "paid-call.v1.schema.json"));

let sink;
beforeEach(() => { sink = ledgerSink(); setCallLedger(sink); });
afterEach(() => {
  for (const record of sink.records) assert.deepEqual(check(record, SCHEMA), [], JSON.stringify(record));
  setCallLedger(null);
  requireCallLedger(false);
});

/** A queue host that logs each request with the active call id. */
function queue({ submit, states, result, abortAt = -1 }) {
  const calls = [];
  const controller = new AbortController();
  const left = [...states];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), method: init.method ?? "GET", active: activeCall(), events: sink.events.length });
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

test("falQueue: one pair for the job; the polls and the result read run inside the call and are not recorded", async () => {
  const host = queue({ submit: falSubmit, states: [state("IN_QUEUE"), state("IN_PROGRESS"), state("COMPLETED")], result: () => json({ video: { url: "https://v.test/a.mp4" } }) });
  const result = await falQueue({ model: "vendor/video", key: "f", input: { prompt: "SECRET PROMPT" }, pollMs: 0 }, { fetch: host.fetchImpl, signal: host.signal, labels: { unit: 7 } });
  assert.equal(result.ok, true);
  assert.equal(host.calls.length, 5);
  assert.equal(sink.begins().length, 1);
  assert.equal(sink.ends().length, 1);
  const [begin, end] = sink.records;
  assert.deepEqual([begin.transport, begin.method, begin.host, begin.path, begin.model, begin.labels], ["falQueue", "POST", "queue.fal.run", "/vendor/video", "vendor/video", { unit: 7 }]);
  assert.deepEqual([end.ok, end.status, end.errorKind, end.aborted, end.cost], [true, 200, null, false, null]);
  assert.ok(host.calls.every((call) => call.active === begin.id), "every request, polls included, ran inside the call");
  assert.ok(host.calls.every((call) => call.events === 1), "the begin was told before the first request and the end after the last");
  assert.equal(activeCall(), null);
  assert.ok(!JSON.stringify(sink.records).includes("SECRET PROMPT"));
});

test("falQueue failures end the one pair with the stage and the reason: submit http, refusal, the result read, a lost connection, an abort", async () => {
  await falQueue({ model: "m", key: "f", input: {}, pollMs: 0 }, { fetch: queue({ submit: () => json({ detail: "no" }, 500), states: [], result: () => json({}) }).fetchImpl });
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].status, sink.ends()[0].errorKind], [false, 500, "submit:http"]);
  const refusal = (status) => status === 422;
  await falQueue({ model: "m", key: "f", input: {}, pollMs: 0, refusal }, { fetch: queue({ submit: falSubmit, states: [state("COMPLETED")], result: () => json({ detail: "policy" }, 422) }).fetchImpl });
  assert.deepEqual([sink.ends()[1].ok, sink.ends()[1].status, sink.ends()[1].errorKind], [false, 422, "result:refused"]);
  await falQueue({ model: "m", key: "f", input: {}, pollMs: 0 }, { fetch: async () => { throw new TypeError("fetch failed"); } });
  assert.deepEqual([sink.ends()[2].status, sink.ends()[2].errorKind, sink.ends()[2].aborted], [null, "submit:network", false]);
  const host = queue({ submit: falSubmit, states: [state("IN_QUEUE"), state("IN_QUEUE")], result: () => json({}), abortAt: 2 });
  await falQueue({ model: "m", key: "f", input: {}, pollMs: 0 }, { fetch: host.fetchImpl, signal: host.signal });
  assert.deepEqual([sink.ends()[3].errorKind, sink.ends()[3].aborted], ["status:aborted", true]);
  assert.equal(sink.begins().length, 4);
});

test("sunraQueue: one pair for the job; the polls run inside it; a failed job and an unsafe refusal end it with stage and code", async () => {
  const sunra = (states, result) => {
    const calls = [];
    const left = [...states];
    return { calls, fetchImpl: async (url, init) => {
      calls.push({ url: String(url), active: activeCall() });
      if (init.method === "POST") return json({ request_id: "q-1" });
      if (String(url).endsWith("/status")) return left.shift()();
      return result();
    } };
  };
  const ok = sunra([state("IN_PROGRESS"), state("COMPLETED")], () => json({ audio: "https://a" }));
  const result = await sunraQueue({ endpoint: "elevenlabs/turbo-v2.5/text-to-speech", key: "s", input: { text: "SECRET" }, pollMs: 0 }, { fetch: ok.fetchImpl, labels: { chars: 6 } });
  assert.equal(result.ok, true);
  assert.deepEqual([sink.begins()[0].transport, sink.begins()[0].host, sink.begins()[0].path, sink.begins()[0].model, sink.begins()[0].labels],
    ["sunraQueue", "api.sunra.ai", "/v1/queue/elevenlabs/turbo-v2.5/text-to-speech", "elevenlabs/turbo-v2.5/text-to-speech", { chars: 6 }]);
  assert.equal(ok.calls.length, 4);
  assert.ok(ok.calls.every((call) => call.active === sink.begins()[0].id));
  assert.deepEqual([sink.ends()[0].ok, sink.ends()[0].status, sink.ends()[0].errorKind], [true, 200, null]);
  const unsafe = sunra([state("COMPLETED", { success: false, error: { code: "unsafe_content" } })], () => json({}));
  await sunraQueue({ endpoint: "e", key: "s", input: {}, pollMs: 0 }, { fetch: unsafe.fetchImpl });
  assert.deepEqual([sink.ends()[1].ok, sink.ends()[1].errorKind, sink.ends()[1].status], [false, "status:unsafe_content", 200]);
  await sunraQueue({ endpoint: "e", key: "s", input: {}, pollMs: 0 }, { fetch: async () => json({}, 503) });
  assert.deepEqual([sink.ends()[2].errorKind, sink.ends()[2].status], ["submit:http", 503]);
  const controller = new AbortController();
  const hang = async (_url, init) => { controller.abort(new Error("left")); throw init.signal.reason; };
  await sunraQueue({ endpoint: "e", key: "s", input: {}, pollMs: 0 }, { fetch: hang, signal: controller.signal });
  assert.deepEqual([sink.ends()[3].errorKind, sink.ends()[3].aborted], ["submit:aborted", true]);
});

test("a queue job with the ledger required and no sink throws CallLedgerError before the submit; a bad label is a TypeError", async () => {
  setCallLedger(null);
  requireCallLedger(true);
  let sent = 0;
  const fetch = async () => { sent += 1; return json({}); };
  await assert.rejects(falQueue({ model: "m", key: "f", input: {}, pollMs: 0 }, { fetch }), CallLedgerError);
  await assert.rejects(sunraQueue({ endpoint: "e", key: "s", input: {}, pollMs: 0 }, { fetch }), CallLedgerError);
  requireCallLedger(false);
  setCallLedger(sink);
  await assert.rejects(falQueue({ model: "m", key: "f", input: {}, pollMs: 0 }, { fetch, labels: { n: {} } }), TypeError);
  await assert.rejects(sunraQueue({ endpoint: "e", key: "s", input: {}, pollMs: 0 }, { fetch, labels: { n: {} } }), TypeError);
  assert.equal(sent, 0);
});
