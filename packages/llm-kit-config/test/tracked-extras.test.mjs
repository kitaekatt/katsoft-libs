// The extras a tracked project file may not set (tests/formats/project-models/tracked-extras.json) against the built
// package. The Python reader refuses them at binding (`validate_document`); this package has no binding stage, so
// `resolveRequest` is the point that refuses them: it is where a row's and a profile's extras are resolved for use.
// `parseRegistry` stays portable and accepts every case, as Python's portable validation does.
// Every implementation of the format runs this file; Python runs it in tests/llm-kit/test_codex_tracked_extras.py.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, test } from "node:test";

import {
  LlmKitConfigError, SCHEMA_IDS, applyOverlay, chatFields, loadRegistry, parseOverlay, parseRegistry, resolveRequest,
} from "../dist/index.js";
import { SCHEMA_SOURCE, TRACKED_EXTRAS_CASES, inputBytes, validators } from "./support.mjs";

const TRACKED = validators(SCHEMA_SOURCE)[SCHEMA_IDS.projectModels];

const scratch = mkdtempSync(join(tmpdir(), "llm-kit-config-extras-"));
after(() => rmSync(scratch, { recursive: true, force: true }));

function refusal(path, key) {
  return (error) => {
    assert.ok(error instanceof LlmKitConfigError, String(error));
    assert.deepEqual({ code: error.code, where: error.where }, { code: "invalid", where: path }, error.message);
    assert.ok(error.message.includes(key), `the message names ${key}: ${error.message}`);
    return true;
  };
}

describe("tracked project file extras: shared cases", () => {
  test("the cases file names this format and holds cases", () => {
    assert.equal(TRACKED_EXTRAS_CASES.format, "llm-kit.project-models/v1");
    const cases = TRACKED_EXTRAS_CASES.cases;
    assert.ok(cases.length > 0, "the cases file holds no case");
    assert.equal(new Set(cases.map((case_) => case_.name)).size, cases.length, "case names are unique");
    assert.ok(cases.some((case_) => "error" in case_.expect), "a refused case");
    assert.ok(cases.some((case_) => "document" in case_.expect), "an accepted case");
  });
  for (const case_ of TRACKED_EXTRAS_CASES.cases) {
    test(case_.name, () => {
      const bytes = inputBytes(case_.tracked);
      const registry = parseRegistry(bytes); // portable reading accepts every case
      assert.ok(TRACKED(registry.document), "the JSON Schema states the shape, not this rule: it accepts every case");
      const expect = case_.expect;
      if ("error" in expect) {
        assert.equal(expect.error.layer, "tracked");
        assert.equal(expect.error.code, "invalid");
        const key = expect.error.path.split(".").at(-1);
        assert.throws(() => resolveRequest(registry, case_.key), refusal(expect.error.path, key));
        const path = join(scratch, `${case_.name}.yaml`);
        writeFileSync(path, bytes);
        assert.throws(() => resolveRequest(loadRegistry(path), case_.key), (error) => {
          refusal(expect.error.path, key)(error);
          assert.equal(error.source, path);
          return true;
        });
        return;
      }
      assert.deepEqual(registry.document, expect.document);
      const request = resolveRequest(registry, case_.key);
      assert.equal(request.row, expect.document.selections[case_.key].model);
    });
  }
});

const REFUSED_ROW = ["version: 1", "catalog:", "  - {id: a, label: A, endpoint: api, model: vendor/a, request_defaults: {extras: {add_dirs: [/work/a]}}}", "selections:", "  k: {model: a}"];
const CLEAN = ["version: 1", "catalog:", "  - {id: a, label: A, endpoint: api, model: vendor/a}", "selections:", "  k: {model: a}"];

describe("tracked project file extras: where the rule applies", () => {
  test("the whole file is refused before the selection key is looked up", () => {
    const registry = parseRegistry(inputBytes({ lines: REFUSED_ROW }));
    assert.throws(() => resolveRequest(registry, "no-such-key"), refusal("catalog[0].request_defaults.extras.add_dirs", "add_dirs"));
  });
  test("a refused file is refused after an overlay too", () => {
    const registry = parseRegistry(inputBytes({ lines: REFUSED_ROW }));
    const layered = applyOverlay(registry, parseOverlay(inputBytes({ lines: ["version: 1", "selections:", "  k: {model: a, effort: null}"] })));
    assert.throws(() => resolveRequest(layered, "k"), refusal("catalog[0].request_defaults.extras.add_dirs", "add_dirs"));
  });
  test("the call layer may still set the keys the file may not", () => {
    const registry = parseRegistry(inputBytes({ lines: CLEAN }));
    const extras = { add_dirs: ["/work/a"], scratch_dir: "/work/s", network: true, output_schema: "s.json", __llm_kit_images: ["/work/a.png"] };
    const request = resolveRequest(registry, "k", { extras });
    assert.deepEqual(request.extras, extras);
    assert.deepEqual(chatFields(request), extras);
  });
  test("parseRegistry and loadRegistry still read a file that holds them", () => {
    const bytes = inputBytes({ lines: REFUSED_ROW });
    const path = join(scratch, "portable.yaml");
    writeFileSync(path, bytes);
    assert.deepEqual(parseRegistry(bytes).document.catalog[0].request_defaults.extras, { add_dirs: ["/work/a"] });
    assert.deepEqual(loadRegistry(path).document.catalog[0].request_defaults.extras, { add_dirs: ["/work/a"] });
  });
});
