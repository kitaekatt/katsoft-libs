// Quality bar line 5 for this npm package, end to end: pack the locked, built package,
// install the tarball into a new empty directory (no workspace link), run every example
// with plain `node`, and compile a TypeScript consumer against the shipped .d.ts files
// (TypeScript 5.8, module nodenext, skipLibCheck off). Prints the tarball's integrity.
//
// Run after `npm ci`: `npm run check-pack`. Needs the npm registry for the `yaml` dependency.
import { copyFileSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { PACKAGE, npm, pack } from "./pack-tools.mjs";

const work = mkdtempSync(join(tmpdir(), "llm-kit-config-pack-"));
try {
  const tarball = pack(work);
  console.log(`packed ${tarball.filename} ${tarball.integrity}`);

  const consumer = join(work, "consumer");
  mkdirSync(consumer);
  writeFileSync(join(consumer, "package.json"), `${JSON.stringify({ name: "consumer", private: true, type: "module" }, null, 2)}\n`);
  npm(["install", "--no-audit", "--no-fund", tarball.path], consumer);
  const installed = join(consumer, "node_modules", "@katsoft", "llm-kit-config");
  if (lstatSync(installed).isSymbolicLink()) throw new Error("the package is a link, not an installed tarball");

  const examples = readdirSync(join(PACKAGE, "examples")).filter((name) => name.endsWith(".mjs")).sort();
  if (examples.length === 0) throw new Error("no examples in examples/");
  for (const name of examples) {
    copyFileSync(join(PACKAGE, "examples", name), join(consumer, name));
    const run = spawnSync(process.execPath, [name], { cwd: consumer, encoding: "utf8" });
    if (run.status !== 0 || !run.stdout.trim()) throw new Error(`example ${name} failed (${run.status}):\n${run.stdout}\n${run.stderr}`);
    console.log(`example ${name}: ok (${run.stdout.trim().split("\n").length} lines)`);
  }

  const typescript = createRequire(join(PACKAGE, "package.json")).resolve("typescript/package.json");
  const version = JSON.parse(readFileSync(typescript, "utf8")).version;
  if (!version.startsWith("5.8.")) throw new Error(`the .d.ts check needs TypeScript 5.8, found ${version}`);
  writeFileSync(join(consumer, "tsconfig.json"), `${JSON.stringify({
    compilerOptions: {
      module: "nodenext", moduleResolution: "nodenext", target: "es2022", strict: true,
      exactOptionalPropertyTypes: true, noEmit: true, skipLibCheck: false, types: [],
    },
    files: ["consumer.ts"],
  }, null, 2)}\n`);
  copyFileSync(join(PACKAGE, "scripts", "consumer.ts"), join(consumer, "consumer.ts"));
  const tsc = spawnSync(process.execPath, [join(typescript, "..", "bin", "tsc"), "-p", "tsconfig.json"], { cwd: consumer, encoding: "utf8" });
  if (tsc.status !== 0) throw new Error(`the TypeScript ${version} consumer check failed:\n${tsc.stdout}\n${tsc.stderr}`);
  console.log(`types: ok (TypeScript ${version}, module nodenext)`);
  console.log(`integrity ${tarball.integrity}`);
} finally {
  rmSync(work, { recursive: true, force: true });
}
