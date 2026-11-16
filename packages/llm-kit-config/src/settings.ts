/**
 * The typed request settings shared by the file reader and the call layer: their names, their
 * rules and the extras names they own. The Python reader has the same rules
 * (`llm_kit.registry.project_models.check_setting` and `validate_profile`).
 *
 * @module
 */

import { invalid } from "./errors.js";

/** The typed request settings, in canonical order. */
export const REQUEST_SETTINGS = ["temperature", "top_p", "max_output_tokens", "reasoning_budget"] as const;

/** One typed request setting. */
export type SettingName = (typeof REQUEST_SETTINGS)[number];

/** The keys of a request profile. */
export const PROFILE_KEYS = [...REQUEST_SETTINGS, "extras"] as const;

/** Extras names a request profile or a call may not hold: a typed setting, the effort or the call owns them. */
export const PROFILE_RESERVED_EXTRAS: ReadonlySet<string> = new Set([
  "model", "messages", "stream", "api_key", "reasoning_effort", "reasoning",
  "temperature", "top_p", "max_tokens", "max_completion_tokens",
]);

/** In a row's extras, the older spellings of the row's typed defaults: `[wire name, setting]`. */
export const LEGACY_ROW_EXTRAS: ReadonlyArray<readonly [string, SettingName]> = [
  ["temperature", "temperature"], ["top_p", "top_p"], ["max_tokens", "max_output_tokens"],
];

const MAX_SAFE = Number.MAX_SAFE_INTEGER;

/**
 * A number as the Python reader sees it: `integer` is false for a decimal (`60.0` in YAML),
 * `inRange` is false for an integer outside -(2**53 - 1) to 2**53 - 1.
 */
export interface Num {
  readonly integer: boolean;
  readonly value: number;
  readonly inRange: boolean;
}

/** A JavaScript value as a {@link Num}, or `undefined` when it is not a number. */
export function jsNumber(value: unknown): Num | undefined {
  if (typeof value !== "number") return undefined;
  const integer = Number.isInteger(value);
  return { integer, value, inRange: !integer || Math.abs(value) <= MAX_SAFE };
}

/** Check one typed setting (Python `check_setting`); returns its value. */
export function checkSetting(name: SettingName, num: Num | undefined, where: string): number {
  if (name === "max_output_tokens" || name === "reasoning_budget") {
    if (num === undefined || !num.integer || !num.inRange || num.value < 1) {
      throw invalid(where, `must be an integer from 1 to ${MAX_SAFE}`);
    }
    return num.value;
  }
  if (num === undefined) throw invalid(where, "must be a number");
  if (!num.integer && !Number.isFinite(num.value)) throw invalid(where, "must be a finite number");
  if (num.integer && !num.inRange) throw invalid(where, `must be a number from -${MAX_SAFE} to ${MAX_SAFE}`);
  if (name === "temperature" && num.value < 0) throw invalid(where, "must be 0 or more");
  if (name === "top_p" && !(num.value >= 0 && num.value <= 1)) throw invalid(where, "must be from 0 to 1");
  return num.value;
}

/** Refuse a reserved extras name of a profile or call (keys in code point order, then the nested effort). */
export function checkProfileExtras(extras: { readonly [key: string]: unknown }, where: string): void {
  for (const key of Object.keys(extras)) {
    if (PROFILE_RESERVED_EXTRAS.has(key)) {
      throw invalid(where, `'${key}' is reserved; set it as a typed setting, through the effort, or in the call`);
    }
  }
  const nested = Object.hasOwn(extras, "chat_template_kwargs") ? extras.chat_template_kwargs : undefined;
  if (nested !== null && typeof nested === "object" && !Array.isArray(nested) && Object.hasOwn(nested, "reasoning_effort")) {
    throw invalid(`${where}.chat_template_kwargs`, "'reasoning_effort' is reserved; set the effort instead");
  }
}
