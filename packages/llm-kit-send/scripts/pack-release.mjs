// Pack the release tarball for release-npm.yml: `node scripts/pack-release.mjs <destination>`.
// Prints only `TARBALL=<path>` and `INTEGRITY=<sha512-...>` for $GITHUB_ENV. npm's stdout,
// which also holds the prepack output, stays inside pack(), which reads its JSON.
import { resolve } from "node:path";

import { pack } from "./pack-tools.mjs";

const [destination] = process.argv.slice(2);
if (!destination) {
  console.error("usage: node scripts/pack-release.mjs <destination>");
  process.exit(2);
}
const { path, integrity } = pack(resolve(destination));
console.log(`TARBALL=${path}\nINTEGRITY=${integrity}`);
