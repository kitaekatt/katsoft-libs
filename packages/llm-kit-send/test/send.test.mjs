// postChat and sendChat: the request on the wire, plain and streamed answers, errors, the stream limits and the
// reasoning-off retry. Every transport is scripted; no network.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SendError, postChat, sendChat } from "../dist/index.js";
import { URL_, data, json, recorder, reply, streamOf, toolChunks } from "./support.mjs";

const call = (body = {}, extra = {}) => ({ url: URL_, key: "k", body: { model: "a/model", messages: [{ role: "user", content: "hi" }], ...body }, ...extra });
const LIMITS = { firstMs: 150, gapMs: 150 };

test("postChat sends one POST: the key, the content type, extra headers and the encoded bytes, with no signal unless given", async () => {
  const { calls, fetchImpl } = recorder(() => reply("x"));
  const response = await postChat(call({}, { headers: { "X-App": "demo" }, encode: (body) => JSON.stringify({ ...body, extra: 1 }) }), { fetch: fetchImpl });
  assert.equal(response.status, 200);
  assert.equal(calls[0].url, URL_);
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(calls[0].init.headers, { Authorization: "Bearer k", "Content-Type": "application/json", "X-App": "demo" });
  assert.equal(calls[0].init.signal, undefined);
  assert.equal(calls[0].init.body, '{"model":"a/model","messages":[{"role":"user","content":"hi"}],"extra":1}');
  await postChat({ ...call(), key: null }, { fetch: fetchImpl });
  assert.equal(calls[1].init.headers.Authorization, undefined, "a keyless endpoint gets no Authorization");
});

test("postChat: a beforeSend refusal propagates unchanged and nothing is sent; a lost request is a network error", async () => {
  const { calls, fetchImpl } = recorder(() => reply("x"));
  const refusal = new Error("not this host");
  await assert.rejects(postChat(call(), { fetch: fetchImpl, beforeSend: () => { throw refusal; } }), (error) => error === refusal);
  assert.equal(calls.length, 0);
  const reset = new Error("reset");
  await assert.rejects(postChat(call(), { fetch: async () => { throw reset; } }), (error) => error instanceof SendError && error.kind === "network" && error.cause === reset && error.fallback);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(postChat(call(), { signal: controller.signal, fetch: async (_url, init) => { throw init.signal.reason; } }), (error) => error.kind === "aborted" && !error.fallback);
});

test("sendChat reads a plain answer: text, tool calls, usage, served model and host", async () => {
  const { fetchImpl } = recorder(() => reply("hello", { provider: "Host A", usage: { prompt_tokens: 3, completion_tokens: 2, cost: 0.1 } }));
  const answer = await sendChat(call(), { fetch: fetchImpl });
  assert.equal(answer.text, "hello");
  assert.equal(answer.model, "served/m");
  assert.equal(answer.provider, "Host A");
  assert.equal(answer.usage.cost, 0.1);
  assert.equal(answer.finishReason, "stop");
  assert.equal(answer.streamed, false);
  assert.deepEqual(answer.toolCalls, []);
  const bad = await sendChat(call(), { fetch: async () => json({ choices: [{ message: { content: ["parts"] } }] }) });
  assert.equal(bad.text, "", "non-string content is no text; the payload keeps it");
  assert.deepEqual(bad.payload.choices[0].message.content, ["parts"]);
  assert.equal(bad.model, "a/model", "no reported model: the body's model");
});

test("sendChat errors: the status, the provider's code and message, the host's reason, and whether a fallback is worth it", async () => {
  const cases = [
    [429, { error: { message: "busy" } }, true],
    [502, { error: { message: "down" } }, true],
    [404, {}, true],
    [408, {}, true],
    [400, { error: { message: "Provider returned error", metadata: { provider_name: "Host B", raw: '{"error":{"type":"content_filter","message":"blocked"}}' } } }, true],
    [400, { error: { code: 400, message: "bad field" } }, false],
    [402, { error: { message: "no credits" } }, false],
  ];
  for (const [status, body, fallback] of cases) {
    await assert.rejects(sendChat(call(), { fetch: async () => json(body, status) }), (error) => {
      assert.ok(error instanceof SendError);
      assert.equal(error.kind, "http");
      assert.equal(error.status, status);
      assert.equal(error.fallback, fallback, String(status));
      assert.deepEqual(error.payload, body);
      return true;
    });
  }
  await assert.rejects(sendChat(call(), { fetch: async () => json(cases[4][1], 400) }), (error) =>
    error.provider === "Host B" && error.reason === "content_filter: blocked" && error.providerMessage === "Provider returned error");
  await assert.rejects(sendChat(call(), { fetch: async () => new Response("not json", { status: 200 }) }), (error) =>
    error.kind === "body" && error.status === 200 && error.cause instanceof SyntaxError && error.fallback);
});

test("reasoning-off retry: a host that refuses it gets one more request with a bounded budget, added to max_tokens", async () => {
  const learned = [];
  const { calls, fetchImpl } = recorder((_body, _init, index) => index === 0 ? json({ error: { message: "Reasoning is mandatory for this endpoint" } }, 400) : reply("ok"));
  const request = call({ reasoning: { enabled: false }, max_tokens: 100 });
  const answer = await sendChat(request, { fetch: fetchImpl, reasoningRetry: { budget: 256, onRequired: (model) => learned.push(model) } });
  assert.equal(answer.text, "ok");
  assert.deepEqual(calls.map((c) => [c.body.reasoning, c.body.max_tokens]), [[{ enabled: false }, 100], [{ max_tokens: 256 }, 356]]);
  assert.deepEqual(learned, ["a/model"]);
  const keys = Object.keys(calls[1].body);
  assert.deepEqual(keys, ["model", "messages", "reasoning", "max_tokens"], "the body keeps its key order");
  const once = recorder(() => json({ error: { message: "Reasoning is mandatory" } }, 400));
  await assert.rejects(sendChat(call({ reasoning: { effort: "none" } }), { fetch: once.fetchImpl }), (error) => error.status === 400);
  assert.equal(once.calls.length, 1, "no retry unless asked");
  const effort = recorder((_b, _i, index) => index === 0 ? json({ error: { message: "reasoning is mandatory" } }, 400) : reply("ok"));
  await sendChat(call({ reasoning: { effort: "none" } }), { fetch: effort.fetchImpl, reasoningRetry: { budget: 64 } });
  assert.deepEqual(effort.calls[1].body.reasoning, { max_tokens: 64 });
  const on = recorder(() => json({ error: { message: "reasoning is mandatory" } }, 400));
  await assert.rejects(sendChat(call({ reasoning: { effort: "low" } }), { fetch: on.fetchImpl, reasoningRetry: { budget: 64 } }));
  assert.equal(on.calls.length, 1, "reasoning that is on is never retried");
});

test("a streamed call gives each content delta, then the text, usage, reported model and host", async () => {
  const seen = [];
  const answer = await sendChat(call({ stream: true }), {
    onDelta: (text) => seen.push(text),
    fetch: async () => streamOf(": PROCESSING\n\n", data({ model: "served/m", provider: "Host A", choices: [{ delta: { role: "assistant" } }] }),
      data({ choices: [{ delta: { content: "Hel" } }] }), data({ choices: [{ delta: { content: "lo" } }] }),
      data({ choices: [{ finish_reason: "stop", delta: {} }], usage: { prompt_tokens: 4, completion_tokens: 2 } }), "data: [DONE]\n\n"),
  });
  assert.deepEqual(seen, ["Hel", "lo"]);
  assert.equal(answer.text, "Hello");
  assert.equal(answer.model, "served/m");
  assert.equal(answer.provider, "Host A");
  assert.deepEqual(answer.usage, { prompt_tokens: 4, completion_tokens: 2 });
  assert.equal(answer.streamed, true);
  assert.equal(answer.payload.choices[0].message.content, "Hello");
});

test("a streamed tool round is put together in the shape of a plain answer; arguments are reported as they grow", async () => {
  const seen = [];
  const part = (index, fn, id) => data({ choices: [{ delta: { tool_calls: [{ index, ...(id ? { id, type: "function" } : {}), function: fn }] } }] });
  const answer = await sendChat(call({ stream: true }), {
    limits: LIMITS, requireFinish: true, onToolArguments: (name, raw) => seen.push([name, raw]),
    fetch: async () => streamOf(part(0, { name: "save_", arguments: "" }, "a"), part(1, { name: "reply", arguments: '{"mes' }, "b"),
      part(0, { name: "note", arguments: '{"patch"' }), part(1, { name: "reply", arguments: 'sage":"hi"}' }), part(0, { arguments: ":{}}" }),
      data({ choices: [{ finish_reason: "tool_calls", delta: {} }], usage: { prompt_tokens: 1, completion_tokens: 2 } })),
  });
  assert.deepEqual(answer.toolCalls, [
    { type: "function", id: "a", function: { name: "save_note", arguments: '{"patch":{}}' } },
    { type: "function", id: "b", function: { name: "reply", arguments: '{"message":"hi"}' } },
  ]);
  assert.deepEqual(answer.payload.choices[0].message.tool_calls, answer.toolCalls);
  assert.equal(answer.finishReason, "tool_calls");
  assert.deepEqual(seen, [["reply", '{"mes'], ["save_note", '{"patch"'], ["reply", '{"message":"hi"}'], ["save_note", '{"patch":{}}']]);
});

test("a JSON answer to a streamed request is read as a plain answer without onDelta", async () => {
  const answer = await sendChat(call({ stream: true }), { limits: LIMITS, fetch: async () => reply("plain") });
  assert.equal(answer.text, "plain");
  assert.equal(answer.streamed, false);
});

test("stream failures: an error chunk, an error finish (unless turned off), and no finish when one is required", async () => {
  await assert.rejects(sendChat(call({ stream: true }), { onDelta: () => {}, fetch: async () => streamOf(data({ provider: "Host C", choices: [{ delta: { content: "a" } }] }), data({ error: { code: 502, message: "upstream" } })) }),
    (error) => error.kind === "stream_error" && error.code === 502 && error.providerMessage === "upstream" && error.provider === "Host C" && error.fallback);
  const errorFinish = () => streamOf(data({ choices: [{ delta: { content: "a" } }] }), data({ choices: [{ finish_reason: "error", delta: {} }] }));
  await assert.rejects(sendChat(call({ stream: true }), { onDelta: () => {}, fetch: async () => errorFinish() }), (error) => error.kind === "stream_error" && error.code === "finish_reason_error");
  const kept = await sendChat(call({ stream: true }), { limits: LIMITS, stopOnErrorFinish: false, fetch: async () => errorFinish() });
  assert.equal(kept.finishReason, "error");
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, requireFinish: true, fetch: async () => streamOf(toolChunks[0], toolChunks[1], "data: [DONE]\n\n") }),
    (error) => error.kind === "incomplete" && error.fallback);
  const open = await sendChat(call({ stream: true }), { onDelta: () => {}, fetch: async () => streamOf(data({ choices: [{ delta: { content: "a" } }] })) });
  assert.equal(open.text, "a", "without requireFinish a stream may end with no finish");
});

test("stream limits: no headers in time, comment lines only, and silence after output end the call; the request is aborted first", async () => {
  const aborted = [];
  const hang = async (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => { aborted.push("headers"); reject(new DOMException("stopped", "AbortError")); }, { once: true });
  });
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, fetch: hang }), (error) => error.kind === "no_first_token" && error.name === "TimeoutError" && error.fallback);
  assert.deepEqual(aborted, ["headers"]);
  const comments = ": PROCESSING\n\n";
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, fetch: async () => streamOf(comments, 60, comments, 60, comments, 60, comments, 300) }), (error) => error.kind === "no_first_token");
  const empty = data({ choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: "" } }] } }] });
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, fetch: async () => streamOf(empty, 60, empty, 60, empty, 300, toolChunks[2]) }), (error) => error.kind === "no_first_token");
  let signal;
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, fetch: async (_u, init) => { signal = init.signal; return streamOf(toolChunks[0], 400, toolChunks[2]); } }),
    (error) => error.kind === "stalled" && error.name === "TimeoutError");
  assert.equal(signal.aborted, true, "the stalled request is aborted");
});

test("stream limits: a slow call that keeps writing runs on; thinking counts as output; 0 turns a limit off", async () => {
  const slow = await sendChat(call({ stream: true }), { limits: { firstMs: 100, gapMs: 100 }, requireFinish: true,
    fetch: async () => streamOf(toolChunks[0], 60, data({ choices: [{ delta: { reasoning: "hmm" } }] }), 60, data({ choices: [{ delta: { reasoning_content: "hm" } }] }), 60, toolChunks[1], 60, toolChunks[2]) });
  assert.equal(slow.toolCalls[0].function.arguments, '{"patch":{}}');
  const off = await sendChat(call({ stream: true }), { limits: { firstMs: 0, gapMs: 0 }, requireFinish: true, fetch: async () => streamOf(150, toolChunks[0], 150, toolChunks[2]) });
  assert.equal(off.toolCalls.length, 1);
});

test("stream pace: a stream that writes under the floor over the window ends as slow", async () => {
  const tick = data({ choices: [{ delta: { content: "a" } }] });
  await assert.rejects(sendChat(call({ stream: true }), { onDelta: () => {}, limits: { firstMs: 1000, gapMs: 1000, pace: { windowMs: 100, minCharsPerS: 100 } },
    fetch: async () => streamOf(tick, 40, tick, 40, tick, 40, tick, 40, tick) }), (error) => error.kind === "slow");
  const fast = await sendChat(call({ stream: true }), { onDelta: () => {}, limits: { firstMs: 1000, gapMs: 1000, pace: { windowMs: 50, minCharsPerS: 1 } },
    fetch: async () => streamOf(tick, 30, tick, 30, tick) });
  assert.equal(fast.text, "aaa");
});

test("a dropped stream or a throwing callback is the kind body with the original cause; the caller's abort is aborted", async () => {
  const drop = new DOMException("stopped", "AbortError");
  await assert.rejects(sendChat(call({ stream: true }), { limits: LIMITS, fetch: async () => new Response(new ReadableStream({ start(c) { setTimeout(() => c.error(drop), 10); } }), { status: 200 }) }),
    (error) => error.kind === "body" && error.cause === drop);
  const boom = new Error("sink failed");
  await assert.rejects(sendChat(call({ stream: true }), { onDelta: () => { throw boom; }, fetch: async () => streamOf(data({ choices: [{ delta: { content: "a" } }] })) }),
    (error) => error.kind === "body" && error.cause === boom);
  const controller = new AbortController();
  const running = sendChat(call({ stream: true }), { signal: controller.signal, onDelta: () => {},
    fetch: async (_u, init) => new Response(new ReadableStream({ start(c) { init.signal.addEventListener("abort", () => c.error(init.signal.reason), { once: true }); } }), { status: 200 }) });
  setTimeout(() => controller.abort(), 10);
  await assert.rejects(running, (error) => error.kind === "aborted" && !error.fallback);
});
