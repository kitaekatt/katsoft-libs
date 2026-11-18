// A picture from an OpenAI-compatible images endpoint (a link, then its download), and a two-host fallback chain read
// from a llm-kit.fallback-chain/v1 document, against a local stub server. Run after installing the package:
// `node media-chain.mjs`.
import { createServer } from "node:http";
import { fetchFile, mapInput, postImages, readFallbackChains, runChain } from "@katsoft/llm-kit-send";

const server = createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => { body += chunk; });
  req.on("end", () => {
    if (req.url === "/v1/images") {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ data: [{ url: `${base}/files/picture.png` }], usage: { cost: 0.02 } }));
    }
    if (req.url === "/files/picture.png") return res.end(Buffer.from("PNG bytes"));
    if (req.url === "/primary/speech") { res.writeHead(402); return res.end("{}"); }
    if (req.url === "/backup/speech") {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ audio: "ok", input: JSON.parse(body) }));
    }
    res.writeHead(404);
    res.end();
  });
});
await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
const base = `http://127.0.0.1:${server.address().port}`;

const picture = await postImages({ url: `${base}/v1/images`, key: "demo-key", body: { model: "demo/image", prompt: "A lake at dusk." } });
if (!picture.ok || !picture.link) throw new Error("no picture link");
const file = await fetchFile(picture.link, { maxBytes: 1_000_000 });
console.log(`picture: ${file.ok ? file.bytes.length : 0} bytes, reported cost ${picture.cost}`);

const chains = readFallbackChains({
  format: "llm-kit.fallback-chain/v1",
  chains: { speech: { steps: [{ host: "primary", next_on: ["no_key", "account"] }, { host: "backup", next_on: [] }] } },
  models: { "tts-1": { hosts: { primary: { endpoint: "primary/speech" }, backup: { endpoint: "backup/speech", from: "primary",
    input: [{ key: "text" }, { key: "speed", number: true, max: 2 }] } } } },
});
const input = { text: "Hello.", speed: 3, style: "calm" };
const hosts = chains.models["tts-1"].hosts;
const result = await runChain(chains.chains.speech.steps, {
  hasKey: () => true,
  attempt: async (step, { previous }) => {
    const host = hosts[step.host];
    const sent = host.input ? mapInput(host.input, input) : input;
    const response = await fetch(`${base}/${host.endpoint}`, { method: "POST", body: JSON.stringify(sent) });
    if (response.ok) return { ok: true, value: await response.json() };
    console.log(`${step.host} answered ${response.status}${previous ? ` after ${previous.host}` : ""}`);
    return { ok: false, failure: response.status === 402 ? "account" : "other", reason: `${step.host}_${response.status}` };
  },
});
if (!result.ok) throw new Error(`the chain stopped at ${result.stop.host}`);
console.log(`served by ${result.host}: ${JSON.stringify(result.value.input)}`);
server.close();
