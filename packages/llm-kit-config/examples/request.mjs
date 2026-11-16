// Resolve the whole request of a selection (row defaults < request profile < call) and render
// its OpenAI-compatible body fields. Runs with plain `node` after `npm install @katsoft/llm-kit-config`.
import { LlmKitConfigError, chatFields, parseRegistry, resolveRequest } from "@katsoft/llm-kit-config";

const registry = parseRegistry([
  "version: 1",
  "endpoints:",
  "  api: {effort_style: reasoning-object}",
  "catalog:",
  "  - id: fast",
  "    label: Fast",
  "    endpoint: api",
  "    model: vendor/fast-1",
  "    max_output_tokens: 4096",
  "    traits: [short_answers]",
  "    request_defaults: {temperature: 0.7}",
  "requests:",
  "  classify: {temperature: 0, max_output_tokens: 60}",
  "selections:",
  "  label.pick: {model: fast, effort: none, request: classify}",
  "  summary.write: {model: fast}",
  "",
].join("\n"));

for (const key of ["label.pick", "summary.write"]) {
  const request = resolveRequest(registry, key);
  console.log(`${key}: ${JSON.stringify(chatFields(request))} traits=${request.traits.join(",")}`);
}
console.log(`call: ${JSON.stringify(chatFields(resolveRequest(registry, "summary.write", { max_output_tokens: 300, effort: "low" })))}`);

try {
  resolveRequest(registry, "summary.write", { temperature: -1 });
} catch (error) {
  if (!(error instanceof LlmKitConfigError)) throw error;
  console.log(`refused: code=${error.code} where=${error.where}`);
}
