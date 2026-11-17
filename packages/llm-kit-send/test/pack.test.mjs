// The packed tarball: its file list, and its dist/ against the local build. The clean install and the examples run in
// scripts/check-pack.mjs.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { pack, readTarball } from "../scripts/pack-tools.mjs";
import { PACKAGE } from "./support.mjs";

test("npm pack ships dist and the package documents, and nothing else", () => {
  const work = mkdtempSync(join(tmpdir(), "llm-kit-send-packtest-"));
  try {
    const files = readTarball(pack(work).path);
    const names = [...files.keys()].sort();
    const dist = names.filter((name) => name.startsWith("dist/"));
    assert.ok(dist.includes("dist/index.js") && dist.includes("dist/index.d.ts"));
    assert.deepEqual(dist.filter((name) => !/\.(d\.ts|js)$/.test(name)), []);
    assert.deepEqual(names.filter((name) => !name.startsWith("dist/")), ["CHANGELOG.md", "LICENSE", "README.md", "package.json"]);
    for (const name of dist) assert.ok(files.get(name).equals(readFileSync(join(PACKAGE, name))), `${name} matches the build`);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});
