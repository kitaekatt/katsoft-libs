// The public surface: public-surface.json against the built exports and the .d.ts
// exports, and a JSDoc block (purpose, parameters, return value, errors) on each name.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import ts from "typescript";

import * as api from "../dist/index.js";
import { PACKAGE, readJson } from "./support.mjs";

const SURFACE = readJson(join(PACKAGE, "public-surface.json"));
const PUBLIC = SURFACE.entries.filter((entry) => entry.status === "public");
const VALUES = PUBLIC.filter((entry) => entry.kind !== "type").map((entry) => entry.name).sort();
const ALL = PUBLIC.map((entry) => entry.name).sort();

test("the runtime exports are the listed values", () => {
  assert.deepEqual(Object.keys(api).sort(), VALUES);
  for (const entry of PUBLIC.filter((item) => item.kind !== "type")) {
    const value = api[entry.name];
    if (entry.kind === "function") assert.equal(typeof value, "function", entry.name);
    if (entry.kind === "class") assert.equal(typeof value, "function", entry.name);
    if (entry.kind === "constant") assert.equal(typeof value, "object", entry.name);
  }
});

test("the .d.ts exports are the listed names, types included", () => {
  const program = ts.createProgram([join(PACKAGE, "dist", "index.d.ts")], { module: ts.ModuleKind.NodeNext, moduleResolution: ts.ModuleResolutionKind.NodeNext, noEmit: true });
  const checker = program.getTypeChecker();
  const module = checker.getSymbolAtLocation(program.getSourceFile(join(PACKAGE, "dist", "index.d.ts")));
  assert.deepEqual(checker.getExportsOfModule(module).map((symbol) => symbol.name).sort(), ALL);
});

test("the package exports only the entry point, the schemas and package.json", () => {
  const manifest = readJson(join(PACKAGE, "package.json"));
  assert.deepEqual(Object.keys(manifest.exports).sort(), [".", "./package.json", "./schemas/*.json"]);
  assert.deepEqual(Object.keys(manifest.dependencies), ["yaml"]);
  assert.match(manifest.dependencies.yaml, /^\d+\.\d+\.\d+$/, "the runtime dependency is pinned exactly");
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  const changelog = readFileSync(join(PACKAGE, "CHANGELOG.md"), "utf8");
  assert.ok(changelog.includes(`## ${manifest.version}`), "CHANGELOG.md has an entry for the version");
});

/** The JSDoc block right before `export ... <name>` in the sources, or undefined. */
function docOf(name) {
  for (const file of readdirSync(join(PACKAGE, "src")).filter((item) => item.endsWith(".ts"))) {
    const text = readFileSync(join(PACKAGE, "src", file), "utf8");
    const match = new RegExp(`/\\*\\*((?:(?!\\*/)[\\s\\S])*)\\*/\\s*export (?:declare )?(?:function|class|interface|type|const) ${name}\\b`).exec(text);
    if (match) return match[1];
  }
  return undefined;
}

test("each public name has a JSDoc block; each function documents parameters, return value and errors", () => {
  for (const entry of PUBLIC) {
    const doc = docOf(entry.name);
    assert.ok(doc && doc.replace(/[\s*]+/g, " ").trim().length > 40, `${entry.name} has a JSDoc block`);
    if (entry.kind === "function") {
      for (const tag of ["@param", "@returns", "@throws"]) assert.ok(doc.includes(tag), `${entry.name} documents ${tag}`);
    }
  }
});
