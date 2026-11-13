/**
 * Prompt files (`llm-kit.prompt-file/v1`): read and render.
 *
 * A prompt file is an optional header for people, one line that is exactly
 * `---PROMPT---`, and the prompt body; a file with no marker line is all body.
 * The shared cases in `tests/formats/prompt-file/cases.json` fix the rules.
 *
 * @module
 */

import { createHash } from "node:crypto";

import { LlmKitConfigError, invalid } from "./errors.js";
import { compareCodePoints, decodeText, readBytes } from "./text.js";
import type { ParseOptions } from "./registry.js";

/** One parsed prompt file; the keys match the format's JSON Schema. */
export interface PromptFile {
  /** The path or name the text was read from, when known. Not part of the file. */
  readonly source?: string;
  /** The text before the marker line, or `null` when the file has no marker line. */
  readonly header: string | null;
  /** The prompt text. */
  readonly body: string;
  /** The body's slot names in order of first use, each once. */
  readonly slots: readonly string[];
  /** The lowercase SHA-256 hex digest of the body encoded as UTF-8. */
  readonly sha256: string;
}

/** Options of {@link renderPrompt}: the optional suffix appended after the filled body. */
export interface RenderOptions {
  /** Text appended after the filled body, as written. */
  readonly suffix?: string;
}

const MARKER = "---PROMPT---";
const SLOT = /\{\{([a-z][a-z0-9_]*)\}\}/g;

/** The slot names of `body` in order of first use, each once. */
function slotsOf(body: string): string[] {
  return [...new Set(Array.from(body.matchAll(SLOT), (match) => match[1]!))];
}

/**
 * Parse a prompt file under `llm-kit.prompt-file/v1`.
 *
 * Bytes are decoded as strict UTF-8; a string is checked to be well formed.
 * One leading U+FEFF is removed, then each CRLF and each remaining CR becomes
 * LF. A line that is exactly `---PROMPT---` splits the header from the body;
 * the LF on each side of it belongs to neither part. Slots are
 * `{{name}}` with `name` matching `[a-z][a-z0-9_]*`, found in the body only.
 *
 * @param input - The file's bytes, or its text.
 * @param options - `source`: the path or name of the text, for errors and the result.
 * @returns A frozen {@link PromptFile}.
 * @throws {LlmKitConfigError} `invalid` (`where` `""`) for bytes that are not UTF-8
 *   and for more than one marker line.
 * @throws {TypeError} When `input` is not a string or a `Uint8Array`.
 */
export function parsePrompt(input: string | Uint8Array, options?: ParseOptions): PromptFile {
  const source = options?.source;
  if (source !== undefined && typeof source !== "string") throw new TypeError("options.source must be a string");
  const text = decodeText(input, source).replace(/^\ufeff/, "").replace(/\r\n?/g, "\n");
  const lines = text.split("\n");
  const at = lines.flatMap((line, index) => (line === MARKER ? [index] : []));
  if (at.length > 1) throw invalid("", `the file has ${at.length} ${MARKER} lines; it may have one`, source);
  const header = at.length === 1 ? lines.slice(0, at[0]).join("\n") : null;
  const body = at.length === 1 ? lines.slice(at[0]! + 1).join("\n") : text;
  const slots = slotsOf(body);
  const sha256 = createHash("sha256").update(body, "utf8").digest("hex");
  const parsed: PromptFile = source === undefined ? { header, body, slots, sha256 } : { source, header, body, slots, sha256 };
  Object.freeze(slots);
  return Object.freeze(parsed);
}

/**
 * Read the prompt file at `path` with {@link parsePrompt}. Synchronous.
 *
 * @param path - The file path; it becomes the result's `source`.
 * @returns A frozen {@link PromptFile}.
 * @throws {LlmKitConfigError} `missing` when the file does not exist, `io` when it
 *   cannot be read, `invalid` for its content.
 */
export function loadPrompt(path: string): PromptFile {
  return parsePrompt(readBytes(path), { source: path });
}

/**
 * Fill a parsed prompt's slots and append the suffix. The slots are read
 * from `prompt.body`.
 *
 * The values must name exactly the prompt's slots, and each value must be a
 * string. Each slot is replaced in one pass, so a value that holds `{{x}}`
 * is inserted as written. Values and the suffix keep their characters; no
 * newline rule applies to them.
 *
 * @param prompt - A parsed prompt from {@link parsePrompt} or {@link loadPrompt}.
 * @param values - Slot name to text.
 * @param options - `suffix`: text appended after the filled body.
 * @returns The filled body, then the suffix.
 * @throws {LlmKitConfigError} `slot` for a slot with no value (`where` is the first
 *   such slot), a value with no slot (`where` is the first such name in code
 *   point order), a value that is not a string (`where` is its slot), or a
 *   suffix that is not a string (`where` is `suffix`).
 */
export function renderPrompt(prompt: PromptFile, values: Readonly<Record<string, string>>, options?: RenderOptions): string {
  const source = prompt.source;
  if (values === null || typeof values !== "object") throw new LlmKitConfigError("slot", "the values must be an object", "", source);
  const slots = slotsOf(prompt.body);
  const missing = slots.filter((name) => !Object.hasOwn(values, name));
  const unused = Object.keys(values).filter((name) => !slots.includes(name)).sort(compareCodePoints);
  if (missing.length > 0 || unused.length > 0) {
    const parts = [];
    if (missing.length > 0) parts.push(`slots with no value: ${missing.join(", ")}`);
    if (unused.length > 0) parts.push(`values with no slot: ${unused.join(", ")}`);
    throw new LlmKitConfigError("slot", parts.join("; "), missing[0] ?? unused[0]!, source);
  }
  for (const name of slots) {
    if (typeof values[name] !== "string") throw new LlmKitConfigError("slot", "the value must be a string", name, source);
  }
  const suffix = options?.suffix;
  if (suffix !== undefined && typeof suffix !== "string") throw new LlmKitConfigError("slot", "the suffix must be a string", "suffix", source);
  const filled = prompt.body.replace(SLOT, (_match, name: string) => values[name]!);
  return suffix === undefined ? filled : filled + suffix;
}
