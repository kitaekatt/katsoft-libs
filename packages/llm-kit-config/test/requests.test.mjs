// The resolved-request shared cases (tests/formats/project-models/requests.json) against the built package:
// resolveRequest gives the expected request, and chatFields the expected OpenAI-compatible body fields.
// The Python reader runs the same cases, and its protocol 2 call sends the same body (test_request_settings.py).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { LlmKitConfigError, applyOverlay, chatFields, parseOverlay, parseRegistry, resolveRequest } from "../dist/index.js";
import { REQUEST_CASES, inputBytes } from "./support.mjs";

function registryOf(case_) {
  const registry = parseRegistry(inputBytes(case_.tracked));
  return "local" in case_ ? applyOverlay(registry, parseOverlay(inputBytes(case_.local))) : registry;
}

function refusal(code, where) {
  return (error) => {
    assert.ok(error instanceof LlmKitConfigError, String(error));
    assert.deepEqual({ code: error.code, where: error.where }, { code, where }, error.message);
    return true;
  };
}

describe("llm-kit.project-models/v1 request cases", () => {
  test("the cases file names this format", () => {
    assert.equal(REQUEST_CASES.format, "llm-kit.project-models/v1");
    assert.ok(REQUEST_CASES.cases.length >= 25, `${REQUEST_CASES.cases.length} cases`);
  });
  for (const case_ of REQUEST_CASES.cases) {
    test(case_.name, () => {
      const registry = registryOf(case_);
      const expect = case_.expect;
      if ("error" in expect) {
        assert.throws(() => resolveRequest(registry, case_.key, case_.call), refusal(expect.error.code, expect.error.path));
        assert.equal(case_.wire, undefined, "a refused request has no wire");
        return;
      }
      const request = resolveRequest(registry, case_.key, case_.call);
      assert.deepEqual(request, expect.request);
      if (!case_.wire) return;
      const options = case_.wire.endpoint_style === null ? undefined : { effortStyle: case_.wire.endpoint_style };
      const wire = case_.wire.expect;
      if ("error" in wire) {
        assert.throws(() => chatFields(request, options), refusal("wire", wire.error.path));
      } else {
        assert.deepEqual(chatFields(request, options), wire.fields);
      }
    });
  }
});
