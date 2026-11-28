// The packed tarball: its file list, and its dist/ against the local build. The clean install and the examples run in
// scripts/check-pack.mjs.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { pack, readTarball } from "../scripts/pack-tools.mjs";
import { PACKAGE } from "./support.mjs";

test("npm pack ships dist and the package documents, and nothing else", () => {
  const work = mkdtempSync(join(tmpdir(), "llm-kit-passes-packtest-"));
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

test("the release pack prints only TARBALL and INTEGRITY lines for GITHUB_ENV, though prepack prints to stdout", () => {
  const work = mkdtempSync(join(tmpdir(), "llm-kit-passes-releasepack-"));
  try {
    const run = spawnSync(process.execPath, [join(PACKAGE, "scripts", "pack-release.mjs"), work], { cwd: PACKAGE, encoding: "utf8" });
    assert.equal(run.status, 0, run.stderr);
    const env = run.stdout.trimEnd().split("\n").map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]);
    assert.deepEqual(env.map(([name]) => name), ["TARBALL", "INTEGRITY"]);
    const { TARBALL, INTEGRITY } = Object.fromEntries(env);
    assert.ok(statSync(TARBALL).isFile(), `${TARBALL} is a file`);
    assert.equal(INTEGRITY, `sha512-${createHash("sha512").update(readFileSync(TARBALL)).digest("base64")}`);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});

test("compare-tarballs: a tarball equals itself; a missing tarball fails", () => {
  const work = mkdtempSync(join(tmpdir(), "llm-kit-passes-compare-"));
  try {
    const { path } = pack(work);
    const script = join(PACKAGE, "scripts", "compare-tarballs.mjs");
    const same = spawnSync(process.execPath, [script, path, path], { encoding: "utf8" });
    assert.equal(same.status, 0, same.stderr);
    const missing = spawnSync(process.execPath, [script, path, join(work, "missing.tgz")], { encoding: "utf8" });
    assert.notEqual(missing.status, 0);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});
