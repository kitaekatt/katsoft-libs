// A forced tool call checked and resubmitted once, for three questions at once under one deadline. The transport is a
// local stub (no network, no key): its first answer to each question misses a field, its second answer is complete.
// Run after installing the package: `node check-and-gather.mjs`.
import { spreadGather, submitToolPass } from "@katsoft/llm-kit-passes";

const tool = {
  name: "submit",
  description: "Submit the answer for validation. Correct any returned problems once.",
  schema: { type: "object", properties: { city: { type: "string" }, country: { type: "string" } }, required: ["city", "country"] },
};

/** A stub transport: answer 1 leaves out `country`; the resubmit (which sees the tool message) adds it. */
let ids = 0;
async function call({ messages, toolChoice, signal, attempt }) {
  signal.throwIfAborted();
  const question = JSON.parse(messages[1].content);
  const args = attempt === 1 ? { city: question.city } : { city: question.city, country: question.country };
  ids += 1;
  return { text: "", toolCalls: [{ id: `call-${ids}`, type: "function", function: { name: toolChoice, arguments: JSON.stringify(args) } }] };
}

/** The check: both fields must be non-empty strings. The state is the accepted answer. */
function check(args, state) {
  const problems = ["city", "country"]
    .filter((field) => typeof args?.[field] !== "string" || !args[field])
    .map((field) => ({ id: field, line: `${field}: required` }));
  return { state: problems.length ? state : args, problems };
}

const questions = [
  { city: "Lisbon", country: "Portugal" },
  { city: "Osaka", country: "Japan" },
  { city: "Quito", country: "Ecuador" },
];
const { results, timedOut } = await spreadGather(questions, (question, { signal }) => submitToolPass({
  messages: [{ role: "system", content: "Answer with the submit tool." }, { role: "user", content: JSON.stringify(question) }],
  tool, call, check, state: null, deadlineMs: 2_000, signal,
}), { deadlineMs: 5_000 });

if (timedOut) throw new Error("the gather timed out");
for (const entry of results) {
  if (entry.status !== "fulfilled" || entry.value.status !== "accepted") throw new Error(`a pass failed: ${JSON.stringify(entry)}`);
  const pass = entry.value;
  console.log(`${pass.state.city}, ${pass.state.country}: ${pass.status} after ${pass.attempts} attempts; told ${pass.messages[3].content}`);
}
