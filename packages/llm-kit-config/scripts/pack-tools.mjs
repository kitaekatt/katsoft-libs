// Helpers shared by scripts/check-pack.mjs and test/pack.test.mjs: run npm, pack the
// package, and read a .tgz without a tar binary (Windows tar and absolute paths do not mix).
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";

export const PACKAGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** Run npm with `args` in `cwd`; return stdout. Throws with npm's output on failure. */
export function npm(args, cwd) {
  const npmCli = process.env.npm_execpath;
  const [command, prefix] = npmCli && /\.(c|m)?js$/.test(npmCli)
    ? [process.execPath, [npmCli]]
    : [process.platform === "win32" ? "npm.cmd" : "npm", []];
  const done = spawnSync(command, [...prefix, ...args], {
    cwd, encoding: "utf8", shell: command === "npm.cmd", maxBuffer: 64 * 1024 * 1024,
  });
  if (done.error) throw done.error;
  if (done.status !== 0) throw new Error(`npm ${args.join(" ")} failed (${done.status}):\n${done.stdout}\n${done.stderr}`);
  return done.stdout;
}

/** `npm pack` into `destination`; returns {path, integrity, filename}. */
export function pack(destination) {
  const out = npm(["pack", "--json", "--pack-destination", destination], PACKAGE);
  const [info] = JSON.parse(out.slice(out.indexOf("[")));
  const path = resolve(destination, info.filename);
  const integrity = `sha512-${createHash("sha512").update(readFileSync(path)).digest("base64")}`;
  if (integrity !== info.integrity) throw new Error(`npm reported ${info.integrity}, the file hashes to ${integrity}`);
  return { path, integrity, filename: info.filename };
}

/** The regular files of a .tgz as a Map of path (without the leading `package/`) to bytes. */
export function readTarball(path) {
  const data = gunzipSync(readFileSync(path));
  const files = new Map();
  let longName;
  for (let at = 0; at + 512 <= data.length;) {
    const header = data.subarray(at, at + 512);
    if (header.every((byte) => byte === 0)) break;
    const field = (start, length) => header.subarray(start, start + length).toString("latin1").replace(/\0.*$/s, "");
    const size = Number.parseInt(field(124, 12).trim() || "0", 8);
    const type = field(156, 1) || "0";
    const body = data.subarray(at + 512, at + 512 + size);
    if (type === "x") {
      const match = /\d+ path=([^\n]*)\n/.exec(body.toString("utf8"));
      longName = match ? match[1] : undefined;
    } else if (type === "0") {
      const prefix = field(345, 155);
      const name = longName ?? (prefix ? `${prefix}/${field(0, 100)}` : field(0, 100));
      files.set(name.replace(/^package\//, ""), Buffer.from(body));
      longName = undefined;
    }
    at += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}
