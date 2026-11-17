// The call records sendWithRetries emits, validated against llm-kit.call-record/v1 (the schema file itself, read from
// the Python package) with a small checker for the keywords that schema uses.
import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import { sendWithRetries } from "../dist/index.js";
import { PACKAGE, URL_, json, readJson, reply } from "./support.mjs";

const SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "call-record.v1.schema.json"));
const CASES = readJson(join(PACKAGE, "..", "..", "tests", "formats", "call-record", "cases.json"));
const KEYWORDS = new Set(["type", "minimum", "maximum", "maxLength", "description"]);

const typeOf = (value) => value === null ? "null" : Number.isInteger(value) ? "integer" : typeof value;
/** The errors of `record` against the call-record schema: [field, keyword] pairs. */
function errors(record) {
  const found = [];
  for (const name of SCHEMA.required) if (!(name in record)) found.push([name, "required"]);
  for (const name of Object.keys(record)) if (!(name in SCHEMA.properties)) found.push([name, "additionalProperties"]);
  for (const [name, rule] of Object.entries(SCHEMA.properties)) {
    if (!(name in record)) continue;
    for (const keyword of Object.keys(rule)) assert.ok(KEYWORDS.has(keyword), `the checker knows ${keyword}`);
    const value = record[name];
    const types = [rule.type].flat();
    const kind = typeOf(value);
    if (!types.includes(kind) && !(kind === "integer" && types.includes("number"))) { found.push([name, "type"]); continue; }
    if (typeof value === "number" && rule.minimum !== undefined && value < rule.minimum) found.push([name, "minimum"]);
    if (typeof value === "number" && rule.maximum !== undefined && value > rule.maximum) found.push([name, "maximum"]);
    if (typeof value === "string" && rule.maxLength !== undefined && value.length > rule.maxLength) found.push([name, "maxLength"]);
  }
  return found;
}

test("the checker agrees with the shared call-record cases", () => {
  assert.equal(SCHEMA.$id, CASES.format);
  for (const item of CASES.records) assert.equal(errors(item.record).length === 0, item.expect === "valid", item.name);
});

test("every record sendWithRetries emits is a valid llm-kit.call-record/v1", async () => {
  const records = [];
  const controller = new AbortController();
  const steps = [() => json({ error: { message: "busy" } }, 429), () => { throw new Error("reset"); },
    () => reply("done", { provider: "Host A", usage: { prompt_tokens: 7, completion_tokens: 3, prompt_tokens_details: { cached_tokens: 2 }, cost: 0.0004 } })];
  let index = 0;
  await sendWithRetries({ url: URL_, key: "k", body: { model: "asked/m", messages: [] } },
    { waits: [1, 1], budgetMs: 100, random: () => 0.5, fetch: async () => steps[index++](), onCall: (record) => records.push(record) });
  const hang = async (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true }));
  await sendWithRetries({ url: URL_, key: "k", body: { model: "asked/m", messages: [] } },
    { waits: [], budgetMs: 100, deadlineMs: 5, fetch: hang, onCall: (record) => records.push(record) });
  setTimeout(() => controller.abort(new Error("caller stop")), 5);
  await assert.rejects(sendWithRetries({ url: URL_, key: "k", body: { model: "asked/m", messages: [] } },
    { waits: [], budgetMs: 100, signal: controller.signal, fetch: hang, onCall: (record) => records.push(record) }), /caller stop/);
  assert.ok(records.length >= 4, `records: ${records.length}`);
  for (const record of records) assert.deepEqual(errors(record), [], JSON.stringify(record));
});
