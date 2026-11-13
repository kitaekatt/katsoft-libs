// The packed tarball: its file list, its schema bytes against the Python package's
// sources, and its dist/ against the local build. The clean install and the examples
// run in scripts/check-pack.mjs (they need the npm registry).
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { pack, readTarball } from "../scripts/pack-tools.mjs";
import { SCHEMA_FILES, SCHEMA_SOURCE } from "../scripts/copy-schemas.mjs";
import { PACKAGE } from "./support.mjs";

test("npm pack ships dist, the schemas copied byte for byte, and nothing else", () => {
  const work = mkdtempSync(join(tmpdir(), "llm-kit-config-packtest-"));
  try {
    const files = readTarball(pack(work).path);
    const names = [...files.keys()].sort();
    const dist = names.filter((name) => name.startsWith("dist/"));
    assert.ok(dist.includes("dist/index.js") && dist.includes("dist/index.d.ts"));
    assert.deepEqual(dist.filter((name) => !/\.(d\.ts|js)$/.test(name)), []);
    assert.deepEqual(names.filter((name) => !name.startsWith("dist/")),
      ["CHANGELOG.md", "LICENSE", "README.md", "package.json", ...SCHEMA_FILES.map((name) => `schemas/${name}`).sort()].sort());
    for (const name of SCHEMA_FILES) {
      assert.ok(files.get(`schemas/${name}`).equals(readFileSync(join(SCHEMA_SOURCE, name))), `${name} matches its source`);
    }
    for (const name of dist) assert.ok(files.get(name).equals(readFileSync(join(PACKAGE, name))), `${name} matches the build`);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});
