// checkSyntaxRegistry and audienceFilter over a small neutral markup: `{op:body}`, where an unclosed form runs to the end
// of the stream. The registry, the policy and the scanner here are test data; a caller supplies its own.
import assert from "node:assert/strict";
import { test } from "node:test";

import { audienceFilter, checkSyntaxRegistry } from "../dist/index.js";

const REGISTRY = {
  kinds: ["shown", "internal", "secret"],
  audiences: ["reader", "editor", "author"],
  forms: [
    { id: "note", op: "note", kind: "shown" },
    { id: "todo", op: "todo", kind: "internal" },
    { id: "key", op: "key", kind: "secret" },
  ],
};
/** The kinds each audience does not see. */
const POLICY = { reader: ["internal", "secret"], editor: ["secret"], author: [] };

/** The test scanner: one span for each `{op:...}` form of a registered op; text it does not know is not a span. */
function scan(stream) {
  const kinds = new Map(REGISTRY.forms.map((form) => [form.op, form.kind]));
  const spans = [];
  for (const match of stream.matchAll(/\{([a-z]+):[^}]*(?:\}|$)/g)) {
    const kind = kinds.get(match[1]);
    if (kind) spans.push({ start: match.index, end: match.index + match[0].length, kind });
  }
  return spans;
}
const filter = audienceFilter({ scan, policy: POLICY });

test("checkSyntaxRegistry: a sound registry and policy have no problems", () => {
  assert.deepEqual(checkSyntaxRegistry(REGISTRY, POLICY), []);
});

test("checkSyntaxRegistry reports a repeated id, an unknown kind and a bad form, and does not throw", () => {
  const registry = {
    ...REGISTRY,
    forms: [...REGISTRY.forms, { id: "note", op: "aside", kind: "shown" }, { id: "draft", op: "draft", kind: "unknown" }, { id: "", op: "x", kind: "shown" }, null],
  };
  assert.deepEqual(checkSyntaxRegistry(registry, POLICY), [
    { code: "unknown-kind", subject: "draft", line: "form draft declares the unknown kind unknown" },
    { code: "bad-form", subject: "5", line: "form 5 needs a non-empty string id, op and kind" },
    { code: "bad-form", subject: "6", line: "form 6 needs a non-empty string id, op and kind" },
    { code: "duplicate-id", subject: "note", line: "form id note is used by 2 forms" },
  ]);
});

test("checkSyntaxRegistry reports a policy that misses an audience, names an unknown one or hides an unknown kind", () => {
  const policy = { reader: ["internal", "hidden"], editor: "secret", guest: [] };
  assert.deepEqual(checkSyntaxRegistry(REGISTRY, policy), [
    { code: "unknown-hidden-kind", subject: "reader", line: "the policy for reader hides the unknown kind hidden" },
    { code: "bad-policy", subject: "editor", line: "the policy for editor must be a list of kinds" },
    { code: "missing-audience", subject: "author", line: "the policy has no entry for the audience author" },
    { code: "unknown-audience", subject: "guest", line: "the policy names the unknown audience guest" },
  ]);
});

test("checkSyntaxRegistry reports a kind or an audience listed twice", () => {
  const registry = { ...REGISTRY, kinds: [...REGISTRY.kinds, "shown"], audiences: [...REGISTRY.audiences, "reader"] };
  assert.deepEqual(checkSyntaxRegistry(registry, POLICY), [
    { code: "duplicate-kind", subject: "shown", line: "the kind shown is listed 2 times" },
    { code: "duplicate-audience", subject: "reader", line: "the audience reader is listed 2 times" },
  ]);
});

test("checkSyntaxRegistry throws a TypeError only for arguments that are not a registry and a policy", () => {
  for (const [registry, policy] of [
    [null, POLICY], [{ ...REGISTRY, forms: "note" }, POLICY], [{ ...REGISTRY, kinds: undefined }, POLICY],
    [{ ...REGISTRY, audiences: {} }, POLICY], [REGISTRY, null], [REGISTRY, []],
  ]) {
    assert.throws(() => checkSyntaxRegistry(registry, policy), TypeError);
  }
});

test("audienceFilter strips the hidden spans inside a line and keeps the text around them", () => {
  assert.equal(filter("before {todo:x} {key:y} {note:z} after", "reader"), "before   {note:z} after");
  assert.equal(filter("before {todo:x} {key:y} {note:z} after", "editor"), "before {todo:x}  {note:z} after");
  assert.equal(filter("a {key:unclosed secret", "reader"), "a ");
  assert.equal(filter("plain {words} and {other:x}", "reader"), "plain {words} and {other:x}");
});

test("audienceFilter: a hidden span that fills a whole line also removes one separator, first, middle or last", () => {
  const hidden = "{key:k}", kept = "{note:n}";
  assert.equal(filter(`${hidden}\n${kept}`, "editor"), kept);
  assert.equal(filter(`${kept}\n${hidden}\n${kept}`, "editor"), `${kept}\n${kept}`);
  assert.equal(filter(`${kept}\n${hidden}`, "editor"), kept);
  assert.equal(filter(`${kept}\n${hidden}\n${hidden}`, "editor"), kept);
  assert.equal(filter(`${hidden}\n${hidden}`, "editor"), "");
  assert.equal(filter(hidden, "editor"), "");
});

test("audienceFilter: a hidden span that shares its line removes no separator", () => {
  assert.equal(filter("x {key:k}\ny", "editor"), "x \ny");
  assert.equal(filter("x\n{key:k} y", "editor"), "x\n y");
  assert.equal(filter("{key:k}{key:j}\ny", "editor"), "\ny");
});

test("audienceFilter: an audience that hides nothing gets the stream as is, without a scan", () => {
  let scans = 0;
  const counting = audienceFilter({ scan: (stream) => { scans += 1; return scan(stream); }, policy: POLICY });
  const stream = "{note:n}\n{todo:t}\n{key:k}";
  assert.equal(counting(stream, "author"), stream);
  assert.equal(scans, 0);
  assert.equal(counting(stream, "reader"), "{note:n}");
  assert.equal(scans, 1);
});

test("audienceFilter takes any iterable of spans from the scanner", () => {
  const generated = audienceFilter({ scan: function* (stream) { yield* scan(stream); }, policy: POLICY });
  assert.equal(generated("a\n{key:k}\nb", "editor"), "a\nb");
});

test("audienceFilter throws a TypeError for bad options, an unknown audience, a stream that is not text and bad spans", () => {
  for (const options of [undefined, {}, { scan }, { scan: "x", policy: POLICY }, { scan, policy: null }, { scan, policy: { reader: "secret" } }]) {
    assert.throws(() => audienceFilter(options), TypeError);
  }
  assert.throws(() => filter("x", "guest"), TypeError);
  assert.throws(() => filter("x", "toString"), TypeError);
  assert.throws(() => filter(5, "reader"), TypeError);
  for (const spans of [
    [{ start: 0, end: 0, kind: "secret" }], [{ start: 0, end: 9, kind: "secret" }], [{ start: -1, end: 1, kind: "secret" }],
    [{ start: 0.5, end: 1, kind: "secret" }], [{ start: 0, end: 1, kind: 7 }], [null],
    [{ start: 2, end: 3, kind: "secret" }, { start: 0, end: 1, kind: "secret" }],
    [{ start: 0, end: 2, kind: "shown" }, { start: 1, end: 3, kind: "secret" }],
  ]) {
    assert.throws(() => audienceFilter({ scan: () => spans, policy: POLICY })("abcd", "reader"), TypeError, JSON.stringify(spans));
  }
});
