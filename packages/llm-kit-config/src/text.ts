/**
 * Text helpers shared by the readers: strict UTF-8 decoding, file reading
 * with the I/O error codes, and the few Python string rules the error paths
 * and the whitespace rule depend on.
 *
 * Internal module; not part of the public surface.
 *
 * @module
 */

import { readFileSync } from "node:fs";

import { LlmKitConfigError, invalid } from "./errors.js";

const DECODER = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/**
 * Decode `input` as strict UTF-8, or check that a string is well formed.
 * A byte order mark is kept; each reader applies its own rule to it.
 */
export function decodeText(input: string | Uint8Array, source: string | undefined): string {
  if (typeof input === "string") {
    if (!input.isWellFormed()) throw invalid("", "the text holds a lone surrogate, so it is not UTF-8", source);
    return input;
  }
  if (!(input instanceof Uint8Array)) throw new TypeError("input must be a string or a Uint8Array");
  try {
    return DECODER.decode(input);
  } catch {
    throw invalid("", "the bytes are not UTF-8", source);
  }
}

/** Read the bytes of the file at `path`; `missing` when it does not exist, `io` for other failures. */
export function readBytes(path: string): Uint8Array {
  if (typeof path !== "string" || path === "") throw new TypeError("path must be a non-empty string");
  try {
    return readFileSync(path);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "ENOENT") throw new LlmKitConfigError("missing", "the file does not exist", "", path, error);
    throw new LlmKitConfigError("io", `the file cannot be read (${code ?? String(error)})`, "", path, error);
  }
}

/** The characters Python's `str.isspace` accepts (Unicode 15), which `str.strip()` removes. */
const PY_SPACE = new Set(
  [0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
    0x2028, 0x2029, 0x202f, 0x205f, 0x3000].map((code) => String.fromCodePoint(code)),
);

/** Whether `text` has leading or trailing whitespace as Python's `str.strip()` sees it. */
export function hasOuterSpace(text: string): boolean {
  return text.length > 0 && (PY_SPACE.has(text[0]!) || PY_SPACE.has(text[text.length - 1]!));
}

/** Compare two strings by code point, as Python sorts `str` values. */
export function compareCodePoints(a: string, b: string): number {
  const left = [...a];
  const right = [...b];
  const length = Math.min(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    const diff = left[i]!.codePointAt(0)! - right[i]!.codePointAt(0)!;
    if (diff !== 0) return diff;
  }
  return left.length - right.length;
}

const NOT_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

/** Python's `repr()` of a string, which the selection paths use (`selections['k']`). */
export function pythonRepr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const char of text) {
    const code = char.codePointAt(0)!;
    if (char === quote || char === "\\") out += `\\${char}`;
    else if (char === "\t") out += "\\t";
    else if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (char !== " " && NOT_PRINTABLE.test(char)) {
      if (code < 0x100) out += `\\x${code.toString(16).padStart(2, "0")}`;
      else if (code < 0x10000) out += `\\u${code.toString(16).padStart(4, "0")}`;
      else out += `\\U${code.toString(16).padStart(8, "0")}`;
    } else out += char;
  }
  return out + quote;
}

/** Set an own, enumerable property, also for keys such as `__proto__`. */
export function define<T>(target: Record<string, T>, key: string, value: T): void {
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
}

/** Freeze `value` and everything it holds. */
export function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const key of Object.keys(value)) deepFreeze((value as Record<string, unknown>)[key]);
  }
  return value;
}
