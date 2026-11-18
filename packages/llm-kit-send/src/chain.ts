// Fallback chains as data (the format llm-kit.fallback-chain/v1): which hosts serve a request in which order, which
// failures move on to the next host, and the same model's endpoint and input on each host. The reader checks a parsed
// document; `mapInput` turns one host's input into another's; `runChain` runs the steps with the caller's attempts.

/** The format id of a fallback chain file: the value of its `format` field. */
export const FALLBACK_CHAIN_FORMAT = "llm-kit.fallback-chain/v1";

/** One step of a chain: a host, and the failure classes that move on to the next step. */
export interface ChainStep {
  /** The host id (`elevenlabs`, `fal`, ...). */
  readonly host: string;
  /** The failure classes that move on: `no_key` (the step has no key, so it is skipped) and the classes the caller's
   * attempts report (for example `account`: the host refused the account for credits, quota or the key). */
  readonly next_on: readonly string[];
}

/** One input rule: an output field from a constant or from an input field. */
export interface InputRule {
  /** The output field. */
  readonly key: string;
  /** The input field to read; `key` when not given. Not with `value`. */
  readonly from?: string;
  /** A constant (string, number, boolean or null) to write. Not with `from`, `number`, `max`, `id_key`. */
  readonly value?: string | number | boolean | null;
  /** Copy the field only when it is a number (never a boolean). */
  readonly number?: true;
  /** With `number`: write the smaller of the field and `max`. */
  readonly max?: number;
  /** With `id_match`: the output field for a value that matches. */
  readonly id_key?: string;
  /** A string of exactly `length` ASCII letters and digits matches. */
  readonly id_match?: { readonly charset: "alnum"; readonly length: number };
}

/** One host's copy of a model: its endpoint and input. */
export interface ModelHost {
  /** The model's endpoint or id on this host. */
  readonly endpoint: string;
  /** The host whose input the caller gives; `input` maps it to this host's input. */
  readonly from?: string;
  /** The rules, in output order. With `from` only; without it the input is the `from` host's input unchanged. */
  readonly input?: readonly InputRule[];
}

/** A checked fallback chain file ({@link readFallbackChains}). */
export interface FallbackChains {
  readonly format: typeof FALLBACK_CHAIN_FORMAT;
  /** Each chain by name. */
  readonly chains: Readonly<Record<string, { readonly steps: readonly ChainStep[] }>>;
  /** Each model by id: its copy on each host. */
  readonly models: Readonly<Record<string, { readonly hosts: Readonly<Record<string, ModelHost>> }>>;
}

/**
 * A fallback chain document that breaks a rule of `llm-kit.fallback-chain/v1`; {@link readFallbackChains} throws it.
 *
 * @param path - Where the document breaks the rule (`models.m.hosts.h.input[1].max`, `$` for the whole document).
 * @param message - What the rule is.
 * @returns The error; its message starts with `path`.
 * @throws Never.
 */
export class ChainFormatError extends Error {
  /** Where the document breaks the rule. */
  readonly path: string;

  /** Builds the error for `path` and `message`. */
  constructor(path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = "ChainFormatError";
    this.path = path;
  }
}

const HOST_ID = /^[a-z0-9][a-z0-9_.-]*$/;
const CLASS_ID = /^[a-z][a-z0-9_]*$/;
const ALNUM = /^[A-Za-z0-9]+$/;
const RULE_KEYS = new Set(["key", "from", "value", "number", "max", "id_key", "id_match"]);

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const name = (value: unknown) => typeof value === "string" && value.length > 0;

function only(value: Record<string, unknown>, keys: readonly string[], path: string) {
  for (const key of Object.keys(value)) if (!keys.includes(key)) throw new ChainFormatError(path, `unknown field "${key}"`);
}

function checkRule(rule: unknown, path: string): void {
  if (!isObject(rule)) throw new ChainFormatError(path, "a rule is an object");
  for (const key of Object.keys(rule)) if (!RULE_KEYS.has(key)) throw new ChainFormatError(path, `unknown field "${key}"`);
  if (!name(rule.key)) throw new ChainFormatError(`${path}.key`, "a non-empty string is required");
  if ("value" in rule) {
    const value = rule.value;
    if (!(value === null || typeof value === "string" || typeof value === "boolean" || isNumber(value))) {
      throw new ChainFormatError(`${path}.value`, "a string, finite number, boolean or null");
    }
    for (const other of ["from", "number", "max", "id_key", "id_match"]) {
      if (other in rule) throw new ChainFormatError(`${path}.${other}`, "not allowed with value");
    }
    return;
  }
  if ("from" in rule && !name(rule.from)) throw new ChainFormatError(`${path}.from`, "a non-empty string");
  if ("number" in rule && rule.number !== true) throw new ChainFormatError(`${path}.number`, "only true");
  if ("max" in rule) {
    if (!isNumber(rule.max)) throw new ChainFormatError(`${path}.max`, "a finite number");
    if (rule.number !== true) throw new ChainFormatError(`${path}.max`, "needs number: true");
  }
  if (("id_key" in rule) !== ("id_match" in rule)) throw new ChainFormatError(`${path}.id_key`, "id_key and id_match go together");
  if ("id_key" in rule) {
    if (!name(rule.id_key)) throw new ChainFormatError(`${path}.id_key`, "a non-empty string");
    if ("number" in rule) throw new ChainFormatError(`${path}.id_key`, "not allowed with number");
    const match = rule.id_match;
    if (!isObject(match)) throw new ChainFormatError(`${path}.id_match`, "an object");
    only(match, ["charset", "length"], `${path}.id_match`);
    if (match.charset !== "alnum") throw new ChainFormatError(`${path}.id_match.charset`, 'only "alnum"');
    if (!Number.isInteger(match.length) || (match.length as number) < 1) throw new ChainFormatError(`${path}.id_match.length`, "an integer of 1 or more");
  }
}

/**
 * Checks a parsed fallback chain document against the format `llm-kit.fallback-chain/v1` (its JSON Schema and the
 * reader rules: unique hosts in a chain, unique output fields in a rule list, and a `from` that names another host of
 * the same model which has no `from` itself).
 *
 * @param value - The parsed JSON document.
 * @returns The same document, typed.
 * @throws {@link ChainFormatError} with the path of the first broken rule.
 */
export function readFallbackChains(value: unknown): FallbackChains {
  if (!isObject(value)) throw new ChainFormatError("$", "the document is an object");
  only(value, ["format", "chains", "models"], "$");
  if (value.format !== FALLBACK_CHAIN_FORMAT) throw new ChainFormatError("format", `must be "${FALLBACK_CHAIN_FORMAT}"`);
  if (!isObject(value.chains)) throw new ChainFormatError("chains", "an object");
  if (!isObject(value.models)) throw new ChainFormatError("models", "an object");
  for (const [chainName, chain] of Object.entries(value.chains)) {
    const path = `chains.${chainName}`;
    if (!isObject(chain)) throw new ChainFormatError(path, "an object");
    only(chain, ["steps"], path);
    if (!Array.isArray(chain.steps) || chain.steps.length === 0) throw new ChainFormatError(`${path}.steps`, "a non-empty array");
    const hosts = new Set<string>();
    chain.steps.forEach((step: unknown, index: number) => {
      const at = `${path}.steps[${index}]`;
      if (!isObject(step)) throw new ChainFormatError(at, "an object");
      only(step, ["host", "next_on"], at);
      if (typeof step.host !== "string" || !HOST_ID.test(step.host)) throw new ChainFormatError(`${at}.host`, "a host id (a-z, 0-9, _ . -)");
      if (hosts.has(step.host)) throw new ChainFormatError(`${at}.host`, `"${step.host}" is already a step of this chain`);
      hosts.add(step.host);
      if (!Array.isArray(step.next_on)) throw new ChainFormatError(`${at}.next_on`, "an array");
      const classes = new Set<string>();
      step.next_on.forEach((item: unknown, place: number) => {
        if (typeof item !== "string" || !CLASS_ID.test(item)) throw new ChainFormatError(`${at}.next_on[${place}]`, "a class id (a-z, 0-9, _)");
        if (classes.has(item)) throw new ChainFormatError(`${at}.next_on[${place}]`, `"${item}" twice`);
        classes.add(item);
      });
    });
  }
  for (const [modelId, model] of Object.entries(value.models)) {
    const path = `models.${modelId}`;
    if (!isObject(model)) throw new ChainFormatError(path, "an object");
    only(model, ["hosts"], path);
    if (!isObject(model.hosts) || Object.keys(model.hosts).length === 0) throw new ChainFormatError(`${path}.hosts`, "a non-empty object");
    const hosts = model.hosts;
    for (const [host, entry] of Object.entries(hosts)) {
      const at = `${path}.hosts.${host}`;
      if (!HOST_ID.test(host)) throw new ChainFormatError(at, "a host id (a-z, 0-9, _ . -)");
      if (!isObject(entry)) throw new ChainFormatError(at, "an object");
      only(entry, ["endpoint", "from", "input"], at);
      if (!name(entry.endpoint)) throw new ChainFormatError(`${at}.endpoint`, "a non-empty string");
      if ("from" in entry) {
        const source = entry.from;
        if (typeof source !== "string" || source === host || !isObject(hosts[source])) throw new ChainFormatError(`${at}.from`, "another host of this model");
        if ("from" in (hosts[source] as Record<string, unknown>)) throw new ChainFormatError(`${at}.from`, `"${source}" maps from a host itself; mappings do not compose`);
      }
      if ("input" in entry) {
        if (!("from" in entry)) throw new ChainFormatError(`${at}.input`, "needs from");
        if (!Array.isArray(entry.input)) throw new ChainFormatError(`${at}.input`, "an array");
        const outputs = new Set<string>();
        entry.input.forEach((rule: unknown, index: number) => {
          checkRule(rule, `${at}.input[${index}]`);
          for (const output of [(rule as InputRule).key, (rule as InputRule).id_key]) {
            if (output === undefined) continue;
            if (outputs.has(output)) throw new ChainFormatError(`${at}.input[${index}]`, `the output field "${output}" twice`);
            outputs.add(output);
          }
        });
      }
    }
  }
  return value as unknown as FallbackChains;
}

/**
 * Builds a host's input from another host's input with the rules, in rule order. A field that is missing (or
 * undefined) is left out; null is copied.
 *
 * @param rules - The checked rules (a `ModelHost.input`).
 * @param input - The input of the `from` host.
 * @returns A new object with the output fields in rule order.
 * @throws Never for checked rules.
 */
export function mapInput(rules: readonly InputRule[], input: Readonly<Record<string, unknown>>): Record<string, unknown> {
  const output: Record<string, unknown> = {};
  for (const rule of rules) {
    if ("value" in rule) { output[rule.key] = rule.value; continue; }
    const field = input[rule.from ?? rule.key];
    if (field === undefined) continue;
    if (rule.number) {
      if (typeof field !== "number") continue;
      output[rule.key] = rule.max === undefined ? field : Math.min(field, rule.max);
      continue;
    }
    if (rule.id_key && rule.id_match && typeof field === "string" && field.length === rule.id_match.length && ALNUM.test(field)) {
      output[rule.id_key] = field;
      continue;
    }
    output[rule.key] = field;
  }
  return output;
}

/** A failed attempt that the chain has seen. */
export interface ChainFailure {
  /** The host of the attempt. */
  readonly host: string;
  /** The failure class (`account`, `other`, ...): it moves on when the step's `next_on` lists it. */
  readonly failure: string;
  /** The caller's short reason, for its logs. */
  readonly reason?: string;
  /** The caller's error, for it to throw when the chain stops. */
  readonly error?: unknown;
}

/** What an attempt is told: the last failure before it, and the hosts skipped for no key so far, in order. */
export interface ChainContext {
  readonly previous: ChainFailure | null;
  readonly skipped: readonly string[];
}

/** The result of one attempt of {@link runChain}. */
export type AttemptResult<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly failure: string; readonly reason?: string; readonly error?: unknown };

/** The options of {@link runChain}. */
export interface ChainOptions<T> {
  /** Whether the caller has a key for the host. */
  readonly hasKey: (host: string) => boolean;
  /** One try on one host. An exception ends the chain (it propagates). */
  readonly attempt: (step: ChainStep, context: ChainContext) => Promise<AttemptResult<T>>;
  /** When given, an abort before a step ends the chain with the signal's reason (thrown). */
  readonly signal?: AbortSignal;
}

/** The result of {@link runChain}. */
export type ChainResult<T> =
  | { readonly ok: true; readonly value: T; readonly host: string }
  | {
    readonly ok: false;
    /** Where and why the chain stopped: `failure` (a class the step does not move on), `no_key` (a step with no key
     * that has no `no_key` in `next_on`), `end` (every step moved on; `host` is null). */
    readonly stop: { readonly host: string | null; readonly cause: "failure" | "no_key" | "end" };
    /** The last attempt that failed, or null when no attempt ran. */
    readonly last: ChainFailure | null;
  };

/**
 * Runs the steps in order. A step with no key is skipped when its `next_on` has `no_key`, else the chain stops. A step
 * with a key runs `attempt`; a failure whose class is in the step's `next_on` moves on, any other stops the chain.
 *
 * @param steps - The chain's steps (`FallbackChains.chains[name].steps`).
 * @param options - The key check, the attempt and an optional signal.
 * @returns The first success with its host, or where the chain stopped and the last failure.
 * @throws What `attempt` throws (no later step runs), and the signal's reason when it aborts before a step.
 */
export async function runChain<T>(steps: readonly ChainStep[], options: ChainOptions<T>): Promise<ChainResult<T>> {
  let previous: ChainFailure | null = null;
  const skipped: string[] = [];
  for (const step of steps) {
    if (options.signal?.aborted) throw options.signal.reason;
    if (!options.hasKey(step.host)) {
      if (!step.next_on.includes("no_key")) return { ok: false, stop: { host: step.host, cause: "no_key" }, last: previous };
      skipped.push(step.host);
      continue;
    }
    const result = await options.attempt(step, { previous, skipped: [...skipped] });
    if (result.ok) return { ok: true, value: result.value, host: step.host };
    previous = { host: step.host, failure: result.failure, ...(result.reason === undefined ? {} : { reason: result.reason }), ...(result.error === undefined ? {} : { error: result.error }) };
    if (!step.next_on.includes(result.failure)) return { ok: false, stop: { host: step.host, cause: "failure" }, last: previous };
  }
  return { ok: false, stop: { host: null, cause: "end" }, last: previous };
}
