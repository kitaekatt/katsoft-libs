// A call ledger sink that prints one line when a paid call begins and one when it ends, against a local stub server.
// The sink reads its own context (here, a plain variable) when `begin` runs, before the first byte leaves.
// Run after installing the package: `node call-ledger.mjs`.
import { createServer } from "node:http";
import { activeCall, requireCallLedger, sendChat, setCallLedger } from "@katsoft/llm-kit-send";

const server = createServer((req, res) => {
  req.resume();
  req.on("end", () => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ id: "gen-demo-1", model: "demo/model", choices: [{ message: { content: "hi" } }], usage: { prompt_tokens: 12, completion_tokens: 4, cost: 0.0002 } }));
  });
});
await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
const url = `http://127.0.0.1:${server.address().port}/v1/chat/completions`;

setCallLedger({
  begin: (record) => console.log(`begin ${record.transport} ${record.host}${record.path} model ${record.model} labels ${JSON.stringify(record.labels)}`),
  end: (record) => console.log(`end   ok ${record.ok} status ${record.status} tokens ${record.inputTokens}/${record.outputTokens} cost ${record.cost} generation ${record.generationId}`),
});
requireCallLedger(true); // with no sink installed, a call would now throw CallLedgerError before it sends

const answer = await sendChat(
  { url, key: "demo-key", body: { model: "demo/model", messages: [{ role: "user", content: "Say hi." }] } },
  { labels: { workflow: "demo", unit: 1 }, fetch: (input, init) => { console.log(`the request runs inside call ${activeCall()}`); return fetch(input, init); } },
);
console.log(`answer ${answer.text}; callId ${answer.callId}; generationId ${answer.generationId}`);
server.close();
