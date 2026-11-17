// A plain call with retries while the host is busy, one record per try, against a local stub server.
// Run after installing the package: `node retries.mjs`.
import { createServer } from "node:http";
import { sendWithRetries, tokenCost } from "@katsoft/llm-kit-send";

let tries = 0;
const server = createServer((req, res) => {
  req.resume();
  req.on("end", () => {
    tries += 1;
    if (tries === 1) {
      res.writeHead(429, { "content-type": "application/json", "retry-after": "0" });
      return res.end(JSON.stringify({ error: { message: "busy" } }));
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ model: "demo/model", choices: [{ message: { content: "{\"ok\":true}" } }], usage: { prompt_tokens: 12, completion_tokens: 4 } }));
  });
});
await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
const url = `http://127.0.0.1:${server.address().port}/v1/chat/completions`;
const price = { input: 1e-6, cached: 0.1e-6, write: 1e-6, output: 4e-6 };

const result = await sendWithRetries(
  { url, key: "demo-key", body: { model: "demo/model", messages: [{ role: "user", content: "Answer in JSON." }] } },
  { waits: [10, 50], budgetMs: 1000, deadlineMs: 5000, onCall: (record) => console.log(`try ${record.attempt}: status ${record.status}, ok ${record.ok}`) },
);
if (!result.ok) throw new Error(`the call failed with status ${result.status}`);
console.log(`answer ${result.text}; priced cost ${tokenCost(price, result.payload.usage)}`);
server.close();
