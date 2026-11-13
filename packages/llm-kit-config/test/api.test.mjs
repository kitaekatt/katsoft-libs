// Each public name, beyond the shared cases: layering, selection, errors, I/O and rendering.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, test } from "node:test";

import {
  LlmKitConfigError, SCHEMA_IDS, applyOverlay, findSelection, loadOverlay, loadPrompt, loadRegistry, parseOverlay,
  parsePrompt, parseRegistry, renderPrompt, select,
} from "../dist/index.js";

const scratch = mkdtempSync(join(tmpdir(), "llm-kit-config-api-"));
after(() => rmSync(scratch, { recursive: true, force: true }));

const TRACKED = [
  "version: 1",
  "catalog:",
  "  - id: fast",
  "    label: Fast",
  "    endpoint: api",
  "    model: vendor/fast-1",
  "    efforts: [low, high]",
  "    max_output_tokens: 4096",
  "  - id: plain",
  "    label: Plain",
  "    endpoint: api",
  "    model: vendor/plain-2",
  "    efforts: []",
  "  - id: open",
  "    label: Open",
  "    endpoint: other",
  "    model: vendor/open-3",
  "selections:",
  "  draft.write: {model: fast, effort: high}",
  "  draft.check: {model: plain}",
  "",
].join("\n");

function refusal(code, where, extra = {}) {
  return (error) => {
    assert.ok(error instanceof LlmKitConfigError, String(error));
    assert.equal(error.name, "LlmKitConfigError");
    assert.equal(error.code, code, error.message);
    assert.equal(error.where, where, error.message);
    for (const [key, value] of Object.entries(extra)) assert.equal(error[key], value);
    return true;
  };
}

function write(name, content) {
  const path = join(scratch, name);
  writeFileSync(path, content);
  return path;
}

describe("parseRegistry and loadRegistry", () => {
  test("a registry holds the normalized document and every key as a default", () => {
    const registry = parseRegistry(TRACKED, { source: "models.yaml" });
    assert.equal(registry.source, "models.yaml");
    assert.deepEqual(registry.document.selections, {
      "draft.check": { model: "plain", effort: null },
      "draft.write": { model: "fast", effort: "high" },
    });
    assert.deepEqual(Object.keys(registry.document.selections), ["draft.check", "draft.write"]);
    assert.equal(registry.document.catalog[0].max_output_tokens, 4096);
    assert.equal("efforts" in registry.document.catalog[2], false);
    assert.deepEqual(registry.resolved["draft.write"], {
      effective: { model: "fast", effort: "high" }, default: { model: "fast", effort: "high" }, source: "default",
    });
    assert.ok(Object.isFrozen(registry) && Object.isFrozen(registry.document.catalog[0].efforts));
  });

  test("bytes, text and a file read the same; a source names errors", () => {
    const path = write("models.yaml", TRACKED);
    const fromFile = loadRegistry(path);
    assert.equal(fromFile.source, path);
    assert.deepEqual(fromFile.document, parseRegistry(new TextEncoder().encode(TRACKED)).document);
    assert.throws(() => parseRegistry("version: 2\n", { source: "x.yaml" }), refusal("invalid", "version", { source: "x.yaml" }));
    assert.throws(() => loadRegistry(write("bad.yaml", "version: 1\ncatalog: {}\n")), refusal("invalid", "catalog", { source: join(scratch, "bad.yaml") }));
  });

  test("a missing file is `missing` and an unreadable path is `io`; neither is `invalid`", () => {
    assert.throws(() => loadRegistry(join(scratch, "nothing.yaml")), refusal("missing", ""));
    const directory = join(scratch, "a-directory");
    mkdirSync(directory);
    assert.throws(() => loadRegistry(directory), (error) => {
      refusal("io", "")(error);
      assert.ok(error.cause instanceof Error);
      return true;
    });
    if (process.platform !== "win32" && process.getuid?.() !== 0) {
      const locked = write("locked.yaml", TRACKED);
      chmodSync(locked, 0o000);
      assert.throws(() => loadRegistry(locked), refusal("io", ""));
      chmodSync(locked, 0o600);
    }
  });

  test("a lone surrogate in text input is not UTF-8", () => {
    assert.throws(() => parseRegistry("version: 1\nx: \ud800\n"), refusal("invalid", ""));
  });

  test("a key such as __proto__ stays an own key of the result", () => {
    const text = `${TRACKED}  __proto__: {model: open, effort: anything}\n`;
    const registry = parseRegistry(text);
    assert.ok(Object.hasOwn(registry.document.selections, "__proto__"));
    assert.equal(Object.getPrototypeOf(registry.document.selections), Object.prototype);
    assert.deepEqual(select(registry, "__proto__").effort, "anything");
  });

  test("wrong argument types are TypeErrors", () => {
    assert.throws(() => parseRegistry(42), TypeError);
    assert.throws(() => parseRegistry("version: 1\n", { source: 3 }), TypeError);
    assert.throws(() => loadRegistry(""), TypeError);
  });
});

describe("parseOverlay, loadOverlay and applyOverlay", () => {
  const registry = parseRegistry(TRACKED);

  test("an overlay replaces whole selections; equal entries stay defaults; new keys have no default", () => {
    const overlay = parseOverlay([
      "version: 1",
      "selections:",
      "  draft.write: {model: open}",
      "  draft.check: {model: plain, effort: null}",
      "  review.extra: {model: fast, effort: low}",
    ].join("\n"));
    assert.deepEqual(overlay.document.selections["draft.write"], { model: "open", effort: null });
    const applied = applyOverlay(registry, overlay);
    assert.notEqual(applied, registry);
    assert.deepEqual(applied.resolved["draft.write"], {
      effective: { model: "open", effort: null }, default: { model: "fast", effort: "high" }, source: "override",
    });
    assert.equal(applied.resolved["draft.check"].source, "default");
    assert.deepEqual(applied.resolved["review.extra"], {
      effective: { model: "fast", effort: "low" }, default: null, source: "override",
    });
    assert.equal(registry.resolved["draft.write"].source, "default", "the input registry does not change");
  });

  test("the last layer wins, also across separate applyOverlay calls", () => {
    const first = parseOverlay("version: 1\nselections:\n  draft.write: {model: fast, effort: low}\n  only.first: {model: plain}\n");
    const second = parseOverlay("version: 1\nselections:\n  draft.write: {model: fast, effort: high}\n");
    const together = applyOverlay(registry, first, second);
    const stepwise = applyOverlay(applyOverlay(registry, first), second);
    assert.deepEqual(together.resolved, stepwise.resolved);
    assert.equal(together.resolved["draft.write"].source, "default", "the last layer set it back to the default");
    assert.equal(together.resolved["only.first"].source, "override");
    assert.deepEqual(applyOverlay(registry).resolved, registry.resolved);
  });

  test("catalog references are checked when the overlay is applied, in file order", () => {
    const overlay = parseOverlay("version: 1\nselections:\n  z: {model: fast, effort: max}\n  a: {model: ghost}\n", { source: "local.yaml" });
    assert.throws(() => applyOverlay(registry, overlay), refusal("invalid", "selections['z'].effort", { source: "local.yaml" }));
    const empty = parseOverlay("version: 1\nselections:\n  k: {model: plain, effort: low}\n");
    assert.throws(() => applyOverlay(registry, empty), refusal("invalid", "selections['k'].effort"));
    const any = applyOverlay(registry, parseOverlay("version: 1\nselections:\n  k: {model: open, effort: whatever}\n"));
    assert.equal(select(any, "k").effort, "whatever", "a row with no efforts accepts any effort");
  });

  test("structure is checked by parseOverlay", () => {
    assert.throws(() => parseOverlay("version: 1\ncatalog: []\n"), refusal("invalid", "document"));
    assert.throws(() => parseOverlay("version: 1\nselections:\n  k: {model: a, effort: off}\n"), refusal("invalid", "selections['k'].effort"));
    assert.throws(() => parseOverlay("version: 1\nselections:\n  \"it's\": {}\n"), refusal("invalid", "selections[\"it's\"]"));
  });

  test("loadOverlay reads a file and reports a missing one as `missing`", () => {
    const path = write("models.local.yaml", "version: 1\nselections:\n  draft.write: {model: plain}\n");
    const overlay = loadOverlay(path);
    assert.equal(overlay.source, path);
    assert.equal(select(applyOverlay(registry, overlay), "draft.write").row.id, "plain");
    assert.throws(() => loadOverlay(join(scratch, "absent.local.yaml")), refusal("missing", ""));
  });

  test("only values made by this package are accepted", () => {
    const overlay = parseOverlay("version: 1\n");
    assert.throws(() => applyOverlay({ ...registry }, overlay), TypeError);
    assert.throws(() => applyOverlay(registry, { ...overlay }), TypeError);
  });
});

describe("select and findSelection", () => {
  const registry = parseRegistry(TRACKED, { source: "models.yaml" });

  test("select returns the row, the effort and the source", () => {
    const resolved = select(registry, "draft.write");
    assert.deepEqual(resolved, { key: "draft.write", row: registry.document.catalog[0], effort: "high", source: "default" });
    assert.equal(resolved.row.model, "vendor/fast-1");
  });

  test("an absent key: select throws `unknown-selection`, findSelection returns undefined", () => {
    assert.throws(() => select(registry, "nope"), refusal("unknown-selection", "selections['nope']", { source: "models.yaml" }));
    assert.equal(findSelection(registry, "nope"), undefined);
    assert.equal(findSelection(registry, "toString"), undefined, "inherited names are absent");
    assert.equal(findSelection(registry, "draft.check").effort, null);
  });

  test("anything other than absence throws", () => {
    assert.throws(() => findSelection({}, "draft.write"), TypeError);
    assert.throws(() => findSelection(registry, 1), TypeError);
    assert.throws(() => select(registry, undefined), TypeError);
  });
});

describe("parsePrompt, loadPrompt and renderPrompt", () => {
  const text = "For people.\r\n---PROMPT---\r\nWrite {{kind}} about {{topic}}.\r\n";

  test("parsePrompt reads the header, body, slots and hash", () => {
    const prompt = parsePrompt(text, { source: "p.md" });
    assert.deepEqual({ ...prompt, slots: [...prompt.slots] }, {
      source: "p.md", header: "For people.", body: "Write {{kind}} about {{topic}}.\n", slots: ["kind", "topic"],
      sha256: createHash("sha256").update("Write {{kind}} about {{topic}}.\n", "utf8").digest("hex"),
    });
    assert.ok(Object.isFrozen(prompt) && Object.isFrozen(prompt.slots));
  });

  test("loadPrompt reads a file; missing and invalid are told apart", () => {
    const path = write("p.md", text);
    assert.equal(loadPrompt(path).source, path);
    assert.throws(() => loadPrompt(join(scratch, "none.md")), refusal("missing", ""));
    const bad = write("bad.md", "a\n---PROMPT---\nb\n---PROMPT---\n");
    assert.throws(() => loadPrompt(bad), refusal("invalid", "", { source: bad }));
  });

  test("renderPrompt fills every slot once and appends the suffix", () => {
    const prompt = parsePrompt(text);
    assert.equal(renderPrompt(prompt, { kind: "a $& poem", topic: "{{kind}}" }, { suffix: "\n\nEnd" }),
      "Write a $& poem about {{kind}}.\n\n\nEnd");
  });

  test("renderPrompt names the first wrong slot in `where`", () => {
    const prompt = parsePrompt(text, { source: "p.md" });
    assert.throws(() => renderPrompt(prompt, { topic: "t" }), refusal("slot", "kind", { source: "p.md" }));
    assert.throws(() => renderPrompt(prompt, { kind: "k", topic: "t", zeta: "", beta: "" }), refusal("slot", "beta"));
    assert.throws(() => renderPrompt(prompt, { kind: "k", topic: 1 }), refusal("slot", "topic"));
    assert.throws(() => renderPrompt(prompt, { kind: "k", topic: "t" }, { suffix: 5 }), refusal("slot", "suffix"));
    assert.throws(() => renderPrompt(prompt, null), refusal("slot", ""));
  });
});

describe("SCHEMA_IDS", () => {
  test("names the three format ids and is frozen", () => {
    assert.deepEqual({ ...SCHEMA_IDS }, {
      projectModels: "llm-kit.project-models/v1",
      projectModelsLocal: "llm-kit.project-models-local/v1",
      promptFile: "llm-kit.prompt-file/v1",
    });
    assert.ok(Object.isFrozen(SCHEMA_IDS));
  });
});
