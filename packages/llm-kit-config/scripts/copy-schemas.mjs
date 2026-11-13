// Copy the llm-kit format schemas from the Python package into schemas/ for the npm tarball.
// `npm pack` and `npm publish` run it through `prepack`. The copies are not committed:
// the Python package's schema/formats directory is the one source.
import { copyFileSync, mkdirSync, readdirSync, rmSync } from "node:fs";
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
  const shipped = readdirSync(SCHEMA_SOURCE).filter((name) => name.endsWith(".schema.json")).sort();
  if (JSON.stringify(shipped) !== JSON.stringify([...SCHEMA_FILES].sort())) {
    throw new Error(`the schema files in ${SCHEMA_SOURCE} are ${shipped.join(", ")}; update SCHEMA_FILES and SCHEMA_IDS`);
  }
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });
  for (const name of SCHEMA_FILES) copyFileSync(join(SCHEMA_SOURCE, name), join(target, name));
  return target;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  console.log(`copied ${SCHEMA_FILES.length} schemas to ${copySchemas()}`);
}
