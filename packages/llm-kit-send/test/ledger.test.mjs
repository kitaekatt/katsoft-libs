// The call ledger core: the sink slot, the required mode, the order of begin and the send, the active call, the labels,
// and what a record may hold. Driven through sendChat; each transport's own records are in ledger-chat, ledger-media
// and ledger-queue.
import assert from "node:assert/strict";
import { AsyncLocalStorage } from "node:async_hooks";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { CallLedgerError, PAID_CALL_FORMAT, activeCall, requireCallLedger, sendChat, setCallLedger } from "../dist/index.js";
import { check } from "./schema-check.mjs";
import { PACKAGE, URL_, ledgerSink, readJson, reply, warningsDuring } from "./support.mjs";

const SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "paid-call.v1.schema.json"));
const SLOT = Symbol.for("katsoft.llm-kit-send.callLedger");
const call = (extra = {}) => ({ url: URL_, key: "SECRET-KEY", body: { model: "asked/m", messages: [{ role: "user", content: "SECRET PROMPT" }], ...extra } });

afterEach(() => { setCallLedger(null); requireCallLedger(false); delete process.env.LLM_KIT_CALL_LEDGER; });

// Declared first: the warning is once for the whole process, and this file's other tests install a sink.
test("with no sink the send goes on and the process gets one LLMKIT_NO_CALL_LEDGER warning, once", async () => {
  let sent = 0;
  const fetch = async () => { sent += 1; return reply("ok"); };
  const warnings = await warningsDuring(async () => {
    await sendChat(call(), { fetch });
    await sendChat(call(), { fetch });
  });
  assert.equal(sent, 2);
  assert.deepEqual(warnings.map((warning) => warning.code), ["LLMKIT_NO_CALL_LEDGER"]);
  assert.equal(activeCall(), null);
});

test("setCallLedger keeps the one sink at the process-wide symbol and null removes it", () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  assert.equal(globalThis[SLOT], sink);
  setCallLedger(null);
  assert.equal(globalThis[SLOT] ?? null, null);
  assert.throws(() => setCallLedger({ begin() {} }), TypeError);
  assert.throws(() => setCallLedger("sink"), TypeError);
});

test("begin runs before the first byte leaves, then end; the order of the sink and the transport is begin, send, end", async () => {
  const events = [];
  const sink = ledgerSink(events);
  setCallLedger(sink);
  await sendChat(call(), { fetch: async () => { events.push("fetch"); return reply("ok"); } });
  assert.deepEqual(events.map((event) => event.split(" ")[0]), ["begin", "fetch", "end"]);
  assert.equal(events[0].split(" ")[1], events[2].split(" ")[1], "one id for the pair");
});

test("begin runs in the caller's async context and after beforeSend and encode", async () => {
  const sink = ledgerSink();
  const seen = [];
  const store = new AsyncLocalStorage();
  setCallLedger({ begin(record) { seen.push(["begin", store.getStore()]); sink.begin(record); }, end(record) { seen.push(["end", store.getStore()]); sink.end(record); } });
  const order = [];
  await store.run({ player: "p-1" }, () => sendChat(
    { ...call(), encode: (body) => { order.push("encode"); return JSON.stringify(body); } },
    { beforeSend: () => order.push("beforeSend"), fetch: async () => { order.push("fetch"); return reply("ok"); } }));
  assert.deepEqual(order, ["encode", "beforeSend", "fetch"]);
  assert.deepEqual(seen[0], ["begin", { player: "p-1" }]);
  assert.equal(sink.begins().length, 1);
});

test("a call that beforeSend refuses, or that fails to encode, writes no record", async () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  await assert.rejects(sendChat(call(), { beforeSend: () => { throw new Error("refused"); }, fetch: async () => reply("never") }), /refused/);
  assert.deepEqual(sink.records, []);
});

test("activeCall is the id of the running call during the send, and null outside", async () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  const inside = [];
  assert.equal(activeCall(), null);
  await sendChat(call(), { fetch: async () => { inside.push(activeCall()); await new Promise((done) => setTimeout(done, 1)); inside.push(activeCall()); return reply("ok"); } });
  assert.equal(inside[0], sink.begins()[0].id);
  assert.equal(inside[1], sink.begins()[0].id, "still the call after an await");
  assert.equal(activeCall(), null);
});

test("two calls at once each see their own active call", async () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  const seen = [];
  const slow = (ms) => async () => { await new Promise((done) => setTimeout(done, ms)); seen.push(activeCall()); return reply("ok"); };
  await Promise.all([sendChat(call(), { fetch: slow(5) }), sendChat(call(), { fetch: slow(1) })]);
  assert.equal(new Set(seen).size, 2);
  assert.deepEqual([...seen].sort(), sink.begins().map((record) => record.id).sort());
});

test("the active call is set even with no sink, so a fetch guard sees a covered call", async () => {
  let inside = null;
  await sendChat(call(), { fetch: async () => { inside = activeCall(); return reply("ok"); } });
  assert.equal(typeof inside, "string");
});

test("required mode with no sink throws CallLedgerError before anything is sent, by call and by environment", async () => {
  let sent = 0;
  const fetch = async () => { sent += 1; return reply("never"); };
  requireCallLedger(true);
  await assert.rejects(sendChat(call(), { fetch }), (error) => error instanceof CallLedgerError && error.code === "LLMKIT_NO_CALL_LEDGER");
  requireCallLedger(false);
  process.env.LLM_KIT_CALL_LEDGER = "required";
  await assert.rejects(sendChat(call(), { fetch }), CallLedgerError);
  assert.equal(sent, 0);
  setCallLedger(ledgerSink());
  await sendChat(call(), { fetch });
  assert.equal(sent, 1, "with a sink the required mode lets the call through");
});

test("a sink that throws is logged as a process warning and the call goes on", async () => {
  setCallLedger({ begin() { throw new Error("disk full"); }, end() { return Promise.reject(new Error("db gone")); } });
  const warnings = await warningsDuring(async () => {
    const answer = await sendChat(call(), { fetch: async () => reply("fine") });
    assert.equal(answer.text, "fine");
  });
  const codes = warnings.map((warning) => warning.code);
  assert.deepEqual(codes, ["LLMKIT_CALL_LEDGER_SINK_ERROR", "LLMKIT_CALL_LEDGER_SINK_ERROR"]);
  assert.match(warnings[0].message, /begin.*disk full/);
  assert.match(warnings[1].message, /end.*db gone/);
});

test("the records are valid llm-kit.paid-call/v1, hold no prompt, reply, key or query string, and the path has no query", async () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  const url = "https://user:pw@llm.example:8443/v1/chat/completions?api_key=QUERYSECRET#frag";
  await sendChat({ ...call(), url }, { labels: { profile: "story", unit: 3, note: null }, fetch: async () => reply("SECRET REPLY") });
  const [begin, end] = sink.records;
  assert.deepEqual(check(begin, SCHEMA), []);
  assert.deepEqual(check(end, SCHEMA), []);
  assert.equal(begin.format, PAID_CALL_FORMAT);
  assert.deepEqual([begin.transport, begin.method, begin.host, begin.path, begin.model, begin.stream, begin.attempt],
    ["sendChat", "POST", "llm.example:8443", "/v1/chat/completions", "asked/m", false, 1]);
  assert.deepEqual(begin.labels, { profile: "story", unit: 3, note: null });
  const text = JSON.stringify(sink.records);
  for (const secret of ["SECRET PROMPT", "SECRET REPLY", "SECRET-KEY", "QUERYSECRET", "api_key", "user:pw", "frag"]) assert.ok(!text.includes(secret), secret);
});

test("a label that is not a string, a finite number or null is a TypeError before anything is sent", async () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  let sent = 0;
  const fetch = async () => { sent += 1; return reply("never"); };
  for (const labels of [{ unit: { n: 1 } }, { unit: [1] }, { unit: undefined }, { unit: Number.NaN }, { unit: true }, "story", []]) {
    await assert.rejects(sendChat(call(), { labels, fetch }), TypeError, JSON.stringify(labels));
  }
  assert.equal(sent, 0);
  assert.deepEqual(sink.records, []);
});

test("a url that cannot be parsed still gives a record, with an empty host and the text before any query", async () => {
  const sink = ledgerSink();
  setCallLedger(sink);
  await sendChat({ ...call(), url: "chat?key=K" }, { fetch: async () => reply("ok") });
  assert.deepEqual([sink.begins()[0].host, sink.begins()[0].path], ["", "chat"]);
});
