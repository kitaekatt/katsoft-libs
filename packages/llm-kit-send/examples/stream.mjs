// A streamed chat call with stream limits and one fallback, against a local stub server (no network, no key).
// Run after installing the package: `node stream.mjs`.
import { createServer } from "node:http";
import { sendChat, withFallback } from "@katsoft/llm-kit-send";

const chunk = (value) => `data: ${JSON.stringify(value)}\n\n`;
const server = createServer((req, res) => {
  let body = "";
  req.on("data", (part) => { body += part; });
  req.on("end", () => {
    const { model } = JSON.parse(body);
    if (model === "demo/silent") return; // never answers: the first-output limit ends it
    res.writeHead(200, { "content-type": "text/event-stream" });
    for (const word of ["Hello", " from", " the", " fallback."]) res.write(chunk({ model, choices: [{ delta: { content: word } }] }));
    res.end(chunk({ choices: [{ finish_reason: "stop", delta: {} }], usage: { prompt_tokens: 5, completion_tokens: 4, cost: 0.00001 } }));
  });
});
await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
const url = `http://127.0.0.1:${server.address().port}/v1/chat/completions`;
const messages = [{ role: "user", content: "Say hello." }];
const limits = { firstMs: 300, gapMs: 300 };
let emitted = false;
const onDelta = (text) => { emitted = true; process.stdout.write(text); };

const answer = await withFallback(
  () => sendChat({ url, key: null, body: { model: "demo/silent", messages, stream: true } }, { limits, onDelta }),
  () => sendChat({ url, key: null, body: { model: "demo/backup", messages, stream: true } }, { limits, onDelta }),
  { emitted: () => emitted, onFailure: (error, next) => console.log(`primary failed: ${error.kind}; fallback: ${next}`) },
);
console.log(`\nmodel ${answer.model}, ${answer.usage.completion_tokens} output tokens, cost ${answer.usage.cost}`);
server.closeAllConnections();
server.close();
