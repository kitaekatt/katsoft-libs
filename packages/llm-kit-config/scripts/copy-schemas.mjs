// Copy the llm-kit format schemas from the Python package into schemas/ for the npm tarball.
// `npm pack` and `npm publish` run it through `prepack`. The copies are not committed:
// the Python package's schema/formats directory is the one source. Only the listed formats ship;
// the source folder may hold others (the run formats), which the npm package does not read.
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const SCHEMA_SOURCE = join(PACKAGE, "..", "katsoft-llm-kit", "src", "llm_kit", "schema", "formats");
export const SCHEMA_FILES = [
  "project-models.v1.schema.json",
  "project-models-local.v1.schema.json",
  "prompt-file.v1.schema.json",
];

export function copySchemas(target = join(PACKAGE, "schemas")) {
  const missing = SCHEMA_FILES.filter((name) => !existsSync(join(SCHEMA_SOURCE, name)));
  if (missing.length) {
    throw new Error(`the shipped schemas ${missing.join(", ")} are missing from ${SCHEMA_SOURCE}; update SCHEMA_FILES`);
  }
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });
  for (const name of SCHEMA_FILES) {
    copyFileSync(join(SCHEMA_SOURCE, name), join(target, name));
    if (!readFileSync(join(target, name)).equals(readFileSync(join(SCHEMA_SOURCE, name)))) {
      throw new Error(`${name} differs from its source after the copy`);
    }
  }
  return target;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  console.log(`copied ${SCHEMA_FILES.length} schemas to ${copySchemas()}`);
}
