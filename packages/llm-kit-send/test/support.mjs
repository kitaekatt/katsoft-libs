// Shared helpers of the tests: scripted transports and server-sent event bodies. No network.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const URL_ = "https://llm.example/v1/chat/completions";

export function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

/** One SSE `data:` line for `value`. */
export const data = (value) => `data: ${JSON.stringify(value)}\n\n`;

/** A 200 event-stream response: each string is sent as a chunk, each number is a pause in milliseconds. */
export function streamOf(...steps) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    async start(controller) {
      for (const step of steps) {
        if (typeof step === "number") await new Promise((done) => setTimeout(done, step));
        else controller.enqueue(encoder.encode(step));
      }
      controller.close();
    },
  }), { status: 200, headers: { "content-type": "text/event-stream" } });
}

/** A transport that records each request and answers with `answer(body, init, index)`. */
export function recorder(answer) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    const body = JSON.parse(String(init.body));
    calls.push({ url: String(url), init, body });
    return answer(body, init, calls.length - 1);
  };
  return { calls, fetchImpl };
}

export const json = (value, status = 200, headers = {}) =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json", ...headers } });

export const reply = (content, extra = {}) => json({ model: "served/m", choices: [{ finish_reason: "stop", message: { content } }], usage: { prompt_tokens: 3, completion_tokens: 2 }, ...extra });

/** Three chunks of one tool call: name and the first part of the arguments, the rest, then the finish with usage. */
export const toolChunks = [
  data({ choices: [{ delta: { tool_calls: [{ index: 0, id: "a", type: "function", function: { name: "save_note", arguments: '{"pa' } }] } }] }),
  data({ choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: 'tch":{}}' } }] } }] }),
  data({ choices: [{ finish_reason: "tool_calls", delta: {} }], usage: { prompt_tokens: 5, completion_tokens: 7 } }),
];

/** A call ledger sink that keeps every record, and the order of its calls in `events` (shared with a stub transport). */
export function ledgerSink(events = []) {
  const records = [];
  return {
    records, events,
    begin(record) { records.push(record); events.push(`begin ${record.id}`); },
    end(record) { records.push(record); events.push(`end ${record.id}`); },
    begins: () => records.filter((record) => record.phase === "begin"),
    ends: () => records.filter((record) => record.phase === "end"),
  };
}

/** The warnings the process emits while `work` runs (the event is asynchronous: one turn is awaited after). */
export async function warningsDuring(work) {
  const seen = [];
  const listener = (warning) => seen.push(warning);
  process.on("warning", listener);
  try {
    await work();
    await new Promise((done) => setImmediate(done));
  } finally {
    process.off("warning", listener);
  }
  return seen;
}
