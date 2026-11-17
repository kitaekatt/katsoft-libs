// The reply cache against a local stub server: a refused picture request is answered from the store the second time
// with no request, one added space is a new request, and a temperature-0 chat answer replays. The store is a Map.
// Run after installing the package: `node reply-cache.mjs`.
import { createServer } from "node:http";
import { postImages, sendWithRetries } from "@katsoft/llm-kit-send";

let requests = 0;
const server = createServer((req, res) => {
  let text = "";
  req.on("data", (chunk) => { text += chunk; });
  req.on("end", () => {
    requests += 1;
    res.writeHead(req.url.endsWith("/images") ? 400 : 200, { "content-type": "application/json" });
    res.end(JSON.stringify(req.url.endsWith("/images")
      ? { error: { message: "refused by the host's content rule" } }
      : { model: "demo/model", choices: [{ message: { content: "yes" } }] }));
  });
});
await new Promise((ready) => server.listen(0, "127.0.0.1", ready));
const base = `http://127.0.0.1:${server.address().port}/v1`;

// One owner's store: a refusal replaces a success, nothing else replaces a live entry. Retention is the store's rule.
const rows = new Map();
const store = {
  async get(layer, key) { return rows.get(`${layer}:${key}`) ?? null; },
  async put(layer, key, entry) {
    const live = rows.get(`${layer}:${key}`);
    if (!live || (live.outcome === "success" && entry.outcome === "refusal")) rows.set(`${layer}:${key}`, entry);
  },
};
const images = { store, account: "demo:main", hosts: [base], classify: (answer) => (answer.ok === false && answer.status === 400 ? "refusal" : null) };
const picture = (prompt) => postImages({ url: `${base}/images`, key: "demo-key", body: { model: "demo/image", prompt } }, { cache: images });

for (const prompt of ["A red door.", "A red door.", "A red door. "]) {
  const answer = await picture(prompt);
  console.log(`picture ${JSON.stringify(prompt)}: status ${answer.status}, replayed ${answer.replayed}, requests so far ${requests}`);
}

const chat = { store, account: "demo:main", hosts: [base], classify: (result) => (result.ok ? "success" : null) };
for (let i = 0; i < 2; i++) {
  const result = await sendWithRetries(
    { url: `${base}/chat/completions`, key: "demo-key", body: { model: "demo/model", temperature: 0, messages: [{ role: "user", content: "Is it red?" }] } },
    { waits: [], budgetMs: 0, cache: chat },
  );
  console.log(`chat: ${result.ok ? result.text : result.status}, replayed ${result.replayed}, requests so far ${requests}`);
}
if (requests !== 3) throw new Error(`expected 3 requests, sent ${requests}`);
server.close();
