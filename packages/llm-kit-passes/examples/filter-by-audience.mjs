// A syntax registry, its check, and an audience filter for a small markup: `<<op:body>>` forms in plain text. Readers
// see neither review notes nor keys; reviewers see notes but not keys; owners see everything.
// Run after installing the package: `node filter-by-audience.mjs`.
import { audienceFilter, checkSyntaxRegistry } from "@katsoft/llm-kit-passes";

const registry = {
  kinds: ["visible", "review", "secret"],
  audiences: ["reader", "reviewer", "owner"],
  forms: [
    { id: "em", op: "em", kind: "visible" },
    { id: "note", op: "note", kind: "review" },
    { id: "key", op: "key", kind: "secret" },
  ],
};
const policy = { reader: ["review", "secret"], reviewer: ["secret"], owner: [] };

const problems = checkSyntaxRegistry(registry, policy);
if (problems.length) throw new Error(problems.map((problem) => problem.line).join("\n"));

/** The scanner: one span for each `<<op:...>>` of a registered op; an unclosed form runs to the end of the text. */
const kindOf = new Map(registry.forms.map((form) => [form.op, form.kind]));
function* scan(text) {
  for (const match of text.matchAll(/<<([a-z]+):(?:(?!>>)[\s\S])*(?:>>|$)/g)) {
    const kind = kindOf.get(match[1]);
    if (kind) yield { start: match.index, end: match.index + match[0].length, kind };
  }
}
const filter = audienceFilter({ scan, policy });

const text = [
  "The bridge opens at <<em:nine>>. <<note:check the time>>",
  "<<note:this whole line is for reviewers>>",
  "Tickets are at the gate.",
  "<<key:door code 4417>>",
].join("\n");
for (const audience of registry.audiences) console.log(`${audience}: ${JSON.stringify(filter(text, audience))}`);
if (filter(text, "reader") !== "The bridge opens at <<em:nine>>. \nTickets are at the gate.") throw new Error("the reader view is wrong");
