// Parse a prompt file and render it with strict slots.
// Runs with plain `node` after `npm install @katsoft/llm-kit-config`.
import { LlmKitConfigError, parsePrompt, renderPrompt } from "@katsoft/llm-kit-config";

const prompt = parsePrompt([
  "Notes for people: this prompt summarizes one text.",
  "---PROMPT---",
  "Summarize the {{kind}} below in {{length}} sentences.",
  "",
  "{{text}}",
].join("\n"), { source: "summary.prompt.md" });

console.log(`slots: ${prompt.slots.join(", ")}`);
console.log(`sha256: ${prompt.sha256}`);
console.log(renderPrompt(prompt, { kind: "article", length: "two", text: "A {{braced}} value stays as written." }, { suffix: "\n\nAnswer:" }));

try {
  renderPrompt(prompt, { kind: "article", length: "two" });
} catch (error) {
  if (!(error instanceof LlmKitConfigError)) throw error;
  console.log(`refused: code=${error.code} where=${error.where}`);
}
