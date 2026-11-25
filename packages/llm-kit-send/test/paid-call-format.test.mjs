// The shared cases of llm-kit.paid-call/v1 (tests/formats/paid-call/cases.json, the file the Python tests run too)
// against the schema file in katsoft-llm-kit.
import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import { PAID_CALL_FORMAT } from "../dist/index.js";
import { check } from "./schema-check.mjs";
import { PACKAGE, readJson } from "./support.mjs";

const SCHEMA = readJson(join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats", "paid-call.v1.schema.json"));
const CASES = readJson(join(PACKAGE, "..", "..", "tests", "formats", "paid-call", "cases.json"));

test("the shared cases name the format the package writes", () => {
  assert.equal(SCHEMA.$id, CASES.format);
  assert.equal(PAID_CALL_FORMAT, CASES.format);
});

test("each shared paid-call case agrees with the schema", () => {
  assert.ok(CASES.records.length >= 20);
  for (const item of CASES.records) assert.equal(check(item.record, SCHEMA).length === 0, item.expect === "valid", item.name);
});
