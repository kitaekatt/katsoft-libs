// A small checker for the JSON Schema keywords the llm-kit format schemas use (llm-scripting-kit.json-schema-subset/v1,
// the subset the tests need): type, properties, required, additionalProperties, items, enum, const, minLength,
// minimum, maximum, anyOf. A keyword it does not know fails the test, so a schema can never pass by being ignored.
import assert from "node:assert/strict";

const KNOWN = new Set(["type", "properties", "required", "additionalProperties", "items", "enum", "const", "minLength", "maxLength",
  "minimum", "maximum", "anyOf", "description", "title", "$schema", "$id"]);

const typeOf = (value) => value === null ? "null" : Array.isArray(value) ? "array" : Number.isInteger(value) ? "integer" : typeof value;

/** The errors of `value` against `schema`: [pointer, keyword] pairs; empty when it conforms. */
export function check(value, schema, pointer = "") {
  for (const keyword of Object.keys(schema)) assert.ok(KNOWN.has(keyword), `the checker knows ${keyword}`);
  const found = [];
  if (schema.anyOf) {
    if (!schema.anyOf.some((branch) => check(value, branch, pointer).length === 0)) found.push([pointer, "anyOf"]);
    return found;
  }
  if ("const" in schema && value !== schema.const) return [[pointer, "const"]];
  if (schema.enum && !schema.enum.includes(value)) return [[pointer, "enum"]];
  if (schema.type) {
    const types = [schema.type].flat();
    const kind = typeOf(value);
    if (!types.includes(kind) && !(kind === "integer" && types.includes("number"))) return [[pointer, "type"]];
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) found.push([pointer, "minimum"]);
    if (schema.maximum !== undefined && value > schema.maximum) found.push([pointer, "maximum"]);
  }
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) found.push([pointer, "minLength"]);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) found.push([pointer, "maxLength"]);
  }
  if (Array.isArray(value) && schema.items) value.forEach((item, index) => found.push(...check(item, schema.items, `${pointer}/${index}`)));
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const name of schema.required ?? []) if (!(name in value)) found.push([`${pointer}/${name}`, "required"]);
    for (const [name, item] of Object.entries(value)) {
      if (schema.properties && name in schema.properties) found.push(...check(item, schema.properties[name], `${pointer}/${name}`));
      else if (schema.additionalProperties === false) found.push([`${pointer}/${name}`, "additionalProperties"]);
      else if (schema.additionalProperties && typeof schema.additionalProperties === "object") found.push(...check(item, schema.additionalProperties, `${pointer}/${name}`));
    }
  }
  return found;
}
