// Every shared case of the two formats, against the built package (dist/).
// A case passes when the code, the path and every expected value match, and the
// parsed data and the result conform to the format's JSON Schema (ajv).
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, test } from "node:test";

import {
  LlmKitConfigError, SCHEMA_IDS, applyOverlay, loadOverlay, loadPrompt, loadRegistry, parseOverlay, parsePrompt,
  parseRegistry, renderPrompt,
} from "../dist/index.js";
import { Float, Foreign, PyMap, loadYaml } from "../dist/pyyaml.js";
import { decodeText } from "../dist/text.js";
import { PROMPT_CASES, REGISTRY_CASES, SCHEMA_SOURCE, inputBytes, validators } from "./support.mjs";

const SCHEMAS = validators(SCHEMA_SOURCE);
const TRACKED = SCHEMAS[SCHEMA_IDS.projectModels];
const LOCAL = SCHEMAS[SCHEMA_IDS.projectModelsLocal];
const PROMPT = SCHEMAS[SCHEMA_IDS.promptFile];
const scratch = mkdtempSync(join(tmpdir(), "llm-kit-config-cases-"));
after(() => rmSync(scratch, { recursive: true, force: true }));

/** The parsed YAML data as JSON, or `undefined` when it is not JSON data (Python's `_is_json`). */
function rawJson(value, open = new Set()) {
  if (value === undefined) return undefined;
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "bigint") return Number(value);
  if (value instanceof Float) return Number.isFinite(value.value) ? value.value : undefined;
  if (value instanceof Foreign) return undefined;
  if (open.has(value)) return undefined;
  const inner = new Set(open).add(value);
  if (Array.isArray(value)) {
    const items = value.map((item) => rawJson(item, inner));
    return items.includes(undefined) ? undefined : items;
  }
  assert.ok(value instanceof PyMap);
  const out = {};
  for (const [key, item] of value.entries) {
    if (typeof key !== "string") return undefined;
    const json = rawJson(item, inner);
    if (json === undefined) return undefined;
    Object.defineProperty(out, key, { value: json, enumerable: true });
  }
  return out;
}

/** The parsed data of a layer's bytes, or `undefined` when the bytes are not one YAML document. */
function parsedData(bytes) {
  try {
    return loadYaml(decodeText(bytes, undefined));
  } catch (error) {
    assert.ok(error instanceof LlmKitConfigError);
    return undefined;
  }
}

/** `["error", layer, error, bytes]` for the first refusal, or `["ok", registry, overlay, applied]`. */
function portable(case_, read) {
  const tracked = inputBytes(case_.tracked);
  let registry;
  try {
    registry = read.registry(tracked, case_.name);
  } catch (error) {
    return ["error", "tracked", error, tracked];
  }
  if (!("local" in case_)) return ["ok", registry, null, null];
  const local = inputBytes(case_.local);
  try {
    const overlay = read.overlay(local, case_.name);
    return ["ok", registry, overlay, applyOverlay(registry, overlay)];
  } catch (error) {
    return ["error", "local", error, local];
  }
}

function checkRegistryCase(case_, read) {
  const expect = case_.expect;
  const outcome = portable(case_, read);
  if ("error" in expect) {
    assert.equal(outcome[0], "error", "the case must be refused");
    const [, layer, error, bytes] = outcome;
    assert.ok(error instanceof LlmKitConfigError, String(error));
    assert.deepEqual({ layer, code: error.code, path: error.where }, expect.error, error.message);
    const data = rawJson(parsedData(bytes));
    if ("schema" in expect) {
      assert.notEqual(data, undefined, "a case with a schema verdict has JSON data");
      assert.equal((layer === "local" ? LOCAL : TRACKED)(data) ? "accepts" : "refuses", expect.schema);
    } else {
      assert.equal(data, undefined, "a case with JSON data states the schema verdict");
    }
    return;
  }
  assert.equal(outcome[0], "ok", outcome[2]?.message);
  const [, registry, overlay, applied] = outcome;
  const trackedData = rawJson(parsedData(inputBytes(case_.tracked)));
  assert.ok(TRACKED(trackedData), "the parsed tracked data conforms");
  assert.ok(TRACKED(registry.document), "the normalized document conforms");
  if ("document" in expect) assert.deepEqual(registry.document, expect.document);
  if (overlay) {
    assert.ok(LOCAL(rawJson(parsedData(inputBytes(case_.local)))), "the parsed local data conforms");
    assert.ok(LOCAL(overlay.document), "the normalized overlay conforms");
    assert.deepEqual(overlay.document, expect.local);
    assert.deepEqual(applied.resolved, expect.resolved);
  }
}

const fromBytes = {
  registry: (bytes) => parseRegistry(bytes),
  overlay: (bytes) => parseOverlay(bytes),
};
const fromText = {
  // The text form must read as the bytes do (the cases with bytes that are not UTF-8 stay bytes).
  registry: (bytes) => parseRegistry(textOrBytes(bytes)),
  overlay: (bytes) => parseOverlay(textOrBytes(bytes)),
};
const fromFiles = {
  registry: (bytes, name) => loadRegistry(file(`${name}.tracked.yaml`, bytes)),
  overlay: (bytes, name) => loadOverlay(file(`${name}.local.yaml`, bytes)),
};

function textOrBytes(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    return bytes;
  }
}

function file(name, bytes) {
  const path = join(scratch, name);
  writeFileSync(path, bytes);
  return path;
}

describe("llm-kit.project-models/v1 shared cases", () => {
  test("the cases file names this format and its schemas", () => {
    assert.equal(REGISTRY_CASES.format, SCHEMA_IDS.projectModels);
    assert.ok(REGISTRY_CASES.cases.length >= 80, `${REGISTRY_CASES.cases.length} cases`);
  });
  for (const case_ of REGISTRY_CASES.cases) {
    test(case_.name, () => {
      checkRegistryCase(case_, fromBytes);
      checkRegistryCase(case_, fromText);
      checkRegistryCase(case_, fromFiles);
    });
  }
});

function promptJson(prompt) {
  return { header: prompt.header, body: prompt.body, slots: [...prompt.slots], sha256: prompt.sha256 };
}

describe("llm-kit.prompt-file/v1 shared cases", () => {
  test("the cases file names this format", () => {
    assert.equal(PROMPT_CASES.format, SCHEMA_IDS.promptFile);
    assert.ok(PROMPT_CASES.cases.length >= 30, `${PROMPT_CASES.cases.length} cases`);
  });
  for (const case_ of PROMPT_CASES.cases) {
    test(case_.name, () => {
      const bytes = inputBytes(case_.input);
      const path = file(`${case_.name}.md`, bytes);
      const expect = case_.expect;
      if ("error" in expect) {
        assert.equal(expect.error, "invalid");
        for (const read of [() => parsePrompt(bytes), () => parsePrompt(textOrBytes(bytes)), () => loadPrompt(path)]) {
          assert.throws(read, (error) => error instanceof LlmKitConfigError && error.code === "invalid" && error.where === "");
        }
        assert.equal(case_.render, undefined);
        return;
      }
      const parsed = parsePrompt(bytes);
      assert.deepEqual(promptJson(parsed), expect);
      assert.equal("source" in parsed, false);
      assert.ok(PROMPT({ ...parsed, slots: [...parsed.slots] }), "the parsed form conforms");
      assert.deepEqual(promptJson(parsePrompt(textOrBytes(bytes))), expect);
      const loaded = loadPrompt(path);
      assert.deepEqual({ ...promptJson(loaded), source: loaded.source }, { ...expect, source: path });
      assert.ok(PROMPT({ ...loaded, slots: [...loaded.slots] }), "the loaded form conforms");
      for (const entry of case_.render ?? []) {
        const options = "suffix" in entry ? { suffix: entry.suffix } : undefined;
        if ("error" in entry) {
          assert.equal(entry.error, "slot");
          assert.throws(() => renderPrompt(parsed, entry.slots, options),
            (error) => error instanceof LlmKitConfigError && error.code === "slot");
        } else {
          assert.equal(renderPrompt(parsed, entry.slots, options), entry.output);
          assert.equal(renderPrompt(loaded, entry.slots, options), entry.output);
        }
      }
    });
  }
});
