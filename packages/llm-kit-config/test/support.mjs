// Shared helpers of the tests: the shared case files, case inputs and the JSON Schemas.
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import Ajv2020 from "ajv/dist/2020.js";

export const PACKAGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const REPO = resolve(PACKAGE, "..", "..");
export const FORMATS = join(REPO, "tests", "formats");
export const SCHEMA_SOURCE = join(REPO, "packages", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats");

export function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

export const REGISTRY_CASES = readJson(join(FORMATS, "project-models", "cases.json"));
export const PROMPT_CASES = readJson(join(FORMATS, "prompt-file", "cases.json"));

/** The bytes a case input names: lines (LF-joined, final LF), text (UTF-8) or base64. */
export function inputBytes(spec) {
  const [[kind, value], ...rest] = Object.entries(spec);
  if (rest.length > 0) throw new Error(`a case input has one kind: ${JSON.stringify(spec)}`);
  if (kind === "lines") return new TextEncoder().encode(`${value.join("\n")}\n`);
  if (kind === "text") return new TextEncoder().encode(value);
  if (kind === "base64") return new Uint8Array(Buffer.from(value, "base64"));
  throw new Error(`unknown input kind ${kind}`);
}

/** One ajv validator per schema file in `directory`, keyed by `$id`. */
export function validators(directory) {
  const ajv = new Ajv2020({ strict: true, allErrors: false });
  const out = {};
  for (const file of ["project-models.v1.schema.json", "project-models-local.v1.schema.json", "prompt-file.v1.schema.json"]) {
    const schema = readJson(join(directory, file));
    out[schema.$id] = ajv.compile(schema);
  }
  return out;
}
