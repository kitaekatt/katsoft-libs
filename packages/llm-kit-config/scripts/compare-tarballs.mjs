// Compare the files of two npm tarballs: `node scripts/compare-tarballs.mjs <a.tgz> <b.tgz>`.
// Exits 1 and names each difference. release-npm.yml uses it when a version is already on npm.
import { readTarball } from "./pack-tools.mjs";

const [left, right] = process.argv.slice(2);
if (!left || !right) {
  console.error("usage: node scripts/compare-tarballs.mjs <a.tgz> <b.tgz>");
  process.exit(2);
}
const a = readTarball(left);
const b = readTarball(right);
const differences = [...new Set([...a.keys(), ...b.keys()])].sort()
  .filter((name) => !a.has(name) || !b.has(name) || !a.get(name).equals(b.get(name)));
for (const name of differences) console.error(`differs: ${name}`);
if (differences.length > 0) process.exit(1);
console.log(`the ${a.size} files are equal`);
