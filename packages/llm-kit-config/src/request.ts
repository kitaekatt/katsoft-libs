/**
 * The request a selection resolves to, and its OpenAI-compatible body fields.
 *
 * The settings come in three layers, lowest first: the catalog row (its `request_defaults` and
 * its own `max_output_tokens`; in its extras, `temperature`, `top_p` and `max_tokens` are the older
 * spellings of those defaults), the selection (its `effort` and the request profile its `request`
 * names) and the call. Per setting the highest layer that sets it wins; `extras` merge per
 * top-level key. Reasoning is one control with two forms, `effort` and `reasoning_budget`: the
 * highest layer that sets either wins, and a call that names `effort` (null included) decides it.
 * The Python reader (`llm_kit.registry.request_settings`) has the same rules; the shared cases in
 * `tests/formats/project-models/requests.json` fix both.
 *
 * @module
 */

import { LlmKitConfigError, invalid, withSource } from "./errors.js";
import type { CatalogRow, EffortStyle, JsonValue, ProjectModelsDocument, Registry, RequestProfile, Selection } from "./registry.js";
import { isRegistry } from "./registry.js";
import { LEGACY_ROW_EXTRAS, PROFILE_KEYS, REQUEST_SETTINGS, checkProfileExtras, checkSetting, jsNumber } from "./settings.js";
import type { SettingName } from "./settings.js";
import { compareCodePoints, deepFreeze, define, pythonRepr } from "./text.js";

/** The call layer of {@link resolveRequest}: the keys of a request profile plus an effort; the top layer. */
export interface RequestCall {
  /** Sampling temperature, 0 or more. */
  readonly temperature?: number;
  /** Nucleus sampling share, 0 to 1. */
  readonly top_p?: number;
  /** The output cap, an integer 1 or more. */
  readonly max_output_tokens?: number;
  /** A reasoning token budget, an integer 1 or more; not beside `effort`. */
  readonly reasoning_budget?: number;
  /** Wire fields; no name a typed setting or the effort owns. */
  readonly extras?: { readonly [key: string]: JsonValue };
  /** The effort of this call; `null` sends no reasoning control at all. */
  readonly effort?: string | null;
}

/** The resolved request: every key present, `null` when unset. The Python reader returns the same JSON. */
export interface ResolvedRequest {
  /** The catalog row id. */
  readonly row: string;
  /** The row's endpoint name. */
  readonly endpoint: string;
  /** The row's model (the consumer's name for it). */
  readonly model: string;
  /** The model id the endpoint is sent: the row's `wire_model`, else its `model`. A sender puts this in the body. */
  readonly wire_model: string;
  /** The effort, or `null`. */
  readonly effort: string | null;
  /** The effort style the file's `endpoints` declares for the endpoint, or `null` (the consumer decides). */
  readonly effort_style: EffortStyle | null;
  /** Sampling temperature, or `null`. */
  readonly temperature: number | null;
  /** Nucleus sampling share, or `null`. */
  readonly top_p: number | null;
  /** The output cap, or `null` (the provider's own limit). */
  readonly max_output_tokens: number | null;
  /** A reasoning token budget, or `null`. */
  readonly reasoning_budget: number | null;
  /** The merged wire extras. */
  readonly extras: { readonly [key: string]: JsonValue };
  /** The row's consumer labels; never sent. */
  readonly traits: readonly string[];
}

/** Options of {@link chatFields}: the effort style to use when the project file declares none. */
export interface ChatFieldsOptions {
  /** The consumer's effort style for an endpoint the file declares none for. */
  readonly effortStyle?: EffortStyle;
}

const STYLES: readonly EffortStyle[] = ["top-level", "ninfer", "chat_template_kwargs", "reasoning-object", "unsupported"];
const OBJECT_MENU = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
const MAX_SAFE = Number.MAX_SAFE_INTEGER;

type Mutable = { [key: string]: JsonValue };

/** A deep, key-sorted JSON copy of a call value; anything else is refused at `where`. */
function jsJson(value: unknown, where: string, open: ReadonlySet<object> = new Set()): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw invalid(where, "must be a finite number");
    if (Number.isInteger(value) && Math.abs(value) > MAX_SAFE) throw invalid(where, `must be an integer from -${MAX_SAFE} to ${MAX_SAFE}`);
    return value;
  }
  if (typeof value !== "object") throw invalid(where, `must be JSON data, not ${typeof value}`);
  if (open.has(value)) throw invalid(where, "holds itself");
  const inner = new Set(open).add(value);
  if (Array.isArray(value)) return value.map((item, index) => jsJson(item, `${where}[${index}]`, inner));
  if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) {
    throw invalid(where, "must be a plain object");
  }
  const out: Mutable = {};
  for (const key of Object.keys(value).sort(compareCodePoints)) define(out, key, jsJson((value as Mutable)[key], `${where}.${key}`, inner));
  return out;
}

function isPlainObject(value: unknown): value is { [key: string]: unknown } {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** The validated call layer (Python `validate_profile(..., call=True)`). */
function callLayer(call: unknown): { [key: string]: unknown } {
  if (call === undefined) return {};
  if (!isPlainObject(call)) throw invalid("call", "must be a mapping");
  const allowed: readonly string[] = [...PROFILE_KEYS, "effort"];
  const unknown = Object.keys(call).filter((key) => !allowed.includes(key)).sort(compareCodePoints);
  if (unknown.length > 0) throw invalid("call", `unknown key(s) ${unknown.join(", ")}; allowed: ${allowed.join(", ")}`);
  const out: { [key: string]: unknown } = {};
  for (const name of REQUEST_SETTINGS) {
    if (Object.hasOwn(call, name)) out[name] = checkSetting(name, jsNumber(call[name]), `call.${name}`);
  }
  if (Object.hasOwn(call, "extras")) {
    if (!isPlainObject(call.extras)) throw invalid("call.extras", "must be a mapping");
    const extras = jsJson(call.extras, "call.extras") as Mutable;
    checkProfileExtras(extras, "call.extras");
    out.extras = extras;
  }
  if (Object.hasOwn(call, "effort")) {
    const effort = call.effort;
    if (effort !== null && (typeof effort !== "string" || effort === "")) throw invalid("call.effort", "must be a non-empty string or null");
    if (Object.hasOwn(call, "reasoning_budget")) throw invalid("call", "sets both effort and reasoning_budget; one layer sets one reasoning control");
    out.effort = effort;
  }
  return out;
}

/** A row's typed defaults, with the older extras spellings read as them, and its other extras. */
function rowLayer(row: CatalogRow, index: number): { settings: Map<SettingName, number>; extras: Mutable } {
  const defaults = row.request_defaults ?? {};
  const extras = structuredClone((defaults.extras ?? {}) as Mutable);
  const settings = new Map<SettingName, number>();
  for (const [wire, name] of LEGACY_ROW_EXTRAS) {
    if (!Object.hasOwn(extras, wire)) continue;
    const value = extras[wire];
    delete extras[wire];
    settings.set(name, checkSetting(name, jsNumber(value), `catalog[${index}].request_defaults.extras.${wire}`));
  }
  for (const name of ["temperature", "top_p", "reasoning_budget"] as const) {
    if (defaults[name] !== undefined) settings.set(name, defaults[name]);
  }
  if (row.max_output_tokens !== undefined) settings.set("max_output_tokens", row.max_output_tokens);
  return { settings, extras };
}

/** The resolved request of one effective selection (Python `request_for`). */
function requestFor(document: ProjectModelsDocument, selection: Selection, call: unknown): ResolvedRequest {
  const index = document.catalog.findIndex((row) => row.id === selection.model);
  if (index < 0) throw invalid("selection.model", `${JSON.stringify(selection.model)} is not a catalog id`);
  const row = document.catalog[index]!;
  let profile: RequestProfile = {};
  if (selection.request !== undefined) {
    const profiles = document.requests ?? {};
    if (!Object.hasOwn(profiles, selection.request)) throw invalid("selection.request", `${JSON.stringify(selection.request)} is not a key of requests`);
    profile = profiles[selection.request]!;
  }
  const layer = callLayer(call);
  const callEffort = layer.effort as string | null | undefined;
  if (callEffort !== undefined && callEffort !== null && row.efforts !== undefined && !row.efforts.includes(callEffort)) {
    throw invalid("call.effort", `${JSON.stringify(callEffort)} is not accepted for ${JSON.stringify(row.id)}`);
  }
  const { settings, extras } = rowLayer(row, index);

  let effort = selection.effort;
  let budget: number | null;
  if (Object.hasOwn(layer, "effort") || Object.hasOwn(layer, "reasoning_budget")) {
    effort = (layer.effort as string | null | undefined) ?? null;
    budget = (layer.reasoning_budget as number | undefined) ?? null;
  } else if (effort !== null || profile.reasoning_budget !== undefined) {
    if (effort !== null && profile.reasoning_budget !== undefined) {
      throw invalid("selection.effort", "an effort and its request's reasoning_budget are one layer; set one");
    }
    budget = profile.reasoning_budget ?? null;
  } else {
    budget = settings.get("reasoning_budget") ?? null;
  }
  const pick = (name: "temperature" | "top_p" | "max_output_tokens"): number | null => {
    if (Object.hasOwn(layer, name)) return layer[name] as number;
    if (profile[name] !== undefined) return profile[name]!;
    return settings.get(name) ?? null;
  };
  const endpoints = document.endpoints ?? {};
  const merged: Mutable = {};
  for (const source of [extras, profile.extras ?? {}, (layer.extras ?? {}) as Mutable]) {
    for (const key of Object.keys(source)) define(merged, key, structuredClone(source[key]!));
  }
  return deepFreeze({
    row: row.id,
    endpoint: row.endpoint,
    model: row.model,
    wire_model: row.wire_model ?? row.model,
    effort,
    effort_style: Object.hasOwn(endpoints, row.endpoint) ? endpoints[row.endpoint]!.effort_style : null,
    temperature: pick("temperature"),
    top_p: pick("top_p"),
    max_output_tokens: pick("max_output_tokens"),
    reasoning_budget: budget,
    extras: merged,
    traits: [...(row.traits ?? [])],
  });
}

/**
 * The resolved request of a selection key: its effective selection (overlays applied), its
 * request profile, the row's defaults and the call, layered as the module documentation says.
 *
 * @param registry - A registry from this package (overlays already applied, when any).
 * @param key - The selection key.
 * @param call - The call layer: any of `temperature`, `top_p`, `max_output_tokens`,
 *   `reasoning_budget`, `extras` and `effort`. Omitted: an empty call.
 * @returns A frozen {@link ResolvedRequest}; every key is present, `null` when unset.
 * @throws {LlmKitConfigError} `unknown-selection` when no layer names `key`
 *   (`where` `selections['<key>']`); `invalid` at `call` or `call.<key>` for a bad call, and at
 *   `catalog[<i>].request_defaults.extras.<key>` for an older extras spelling that is not a valid
 *   setting.
 * @throws {TypeError} When `registry` does not come from this package or `key` is not a string.
 */
export function resolveRequest(registry: Registry, key: string, call?: RequestCall): ResolvedRequest {
  if (!isRegistry(registry)) throw new TypeError("registry must come from parseRegistry, loadRegistry or applyOverlay");
  if (typeof key !== "string") throw new TypeError("key must be a string");
  if (!Object.hasOwn(registry.resolved, key)) {
    throw new LlmKitConfigError("unknown-selection", "no layer names this selection key", `selections[${pythonRepr(key)}]`, registry.source);
  }
  try {
    return requestFor(registry.document, registry.resolved[key]!.effective, call);
  } catch (error) {
    if (error instanceof LlmKitConfigError) throw withSource(error, registry.source);
    throw error;
  }
}

function wire(where: string, message: string): LlmKitConfigError {
  return new LlmKitConfigError("wire", message, where);
}

/** The effort's own keys in `fields` (from extras): a delivering style refuses to write over them. */
function checkEffortKeys(fields: Mutable, style: EffortStyle): void {
  if (style === "reasoning-object" && Object.hasOwn(fields, "reasoning")) throw wire("extras.reasoning", "extras already hold 'reasoning'");
  if (Object.hasOwn(fields, "reasoning_effort")) throw wire("extras.reasoning_effort", "extras already hold 'reasoning_effort'");
  const nested = Object.hasOwn(fields, "chat_template_kwargs") ? fields.chat_template_kwargs : undefined;
  if (isPlainObject(nested) && Object.hasOwn(nested, "reasoning_effort")) {
    throw wire("extras.chat_template_kwargs.reasoning_effort", "extras already hold the nested 'reasoning_effort'");
  }
}

function placeEffort(fields: Mutable, effort: string, style: EffortStyle): void {
  checkEffortKeys(fields, style);
  if (style === "top-level") fields.reasoning_effort = effort;
  else if (style === "ninfer") fields.reasoning_effort = effort === "high" ? "xhigh" : effort;
  else if (style === "reasoning-object") {
    if (!OBJECT_MENU.includes(effort)) throw wire("effort", `the reasoning-object style takes ${OBJECT_MENU.join(", ")}`);
    fields.reasoning = effort === "none" ? { enabled: false } : { effort };
  } else {
    const nested = fields.chat_template_kwargs;
    fields.chat_template_kwargs = { ...(isPlainObject(nested) ? (nested as Mutable) : {}), reasoning_effort: effort };
  }
}

/**
 * The OpenAI-compatible chat body fields of a resolved request: `max_tokens`, `temperature`,
 * `top_p`, the effort placed in its style, the reasoning budget and the extras. A sender adds
 * `model`, `messages` and its own transport fields. The Python reader's protocol 2 call sends
 * exactly these fields for the same request. The extras come from the tracked file and are passed on
 * as given: this function does not refuse the keys the Python binding refuses (`add_dirs`,
 * `scratch_dir`, `output_schema`, `__llm_kit_` names, `network` other than `false`; see
 * {@link RequestDefaults}`.extras`).
 *
 * Effort placement: `top-level` sends `reasoning_effort`; `ninfer` the same with `high` sent as
 * `xhigh`; `chat_template_kwargs` adds `reasoning_effort` to that object; `reasoning-object` sends
 * `reasoning: {effort}` (`{enabled: false}` for `none`). A reasoning budget needs
 * `reasoning-object` and sends `reasoning: {max_tokens}`.
 *
 * @param request - A request from {@link resolveRequest}.
 * @param options - `effortStyle`: the consumer's style when the file declares none for the endpoint.
 * @returns A new object of the body fields.
 * @throws {LlmKitConfigError} `wire` with `where` `effort` (no style that delivers it, or a value the
 *   style does not take), `reasoning_budget` (a style other than `reasoning-object`), or `extras.<key>`
 *   (an extras key the effort, the budget or a typed setting would also write).
 * @throws {TypeError} When `request` is not an object or `options.effortStyle` is not a style.
 */
export function chatFields(request: ResolvedRequest, options?: ChatFieldsOptions): { [key: string]: JsonValue } {
  if (!isPlainObject(request)) throw new TypeError("request must be a resolved request");
  const fallback = options?.effortStyle;
  if (fallback !== undefined && !STYLES.includes(fallback)) throw new TypeError(`options.effortStyle must be one of ${STYLES.join(", ")}`);
  const style = request.effort_style ?? fallback ?? null;
  const fields: Mutable = {};
  if (request.max_output_tokens !== null) fields.max_tokens = request.max_output_tokens;
  if (request.temperature !== null) fields.temperature = request.temperature;
  if (request.top_p !== null) fields.top_p = request.top_p;
  for (const key of Object.keys(request.extras)) {
    if (Object.hasOwn(fields, key)) throw wire(`extras.${key}`, `extras repeat the typed field '${key}'`);
    define(fields, key, structuredClone(request.extras[key]!));
  }
  if (request.effort !== null) {
    if (style === null || style === "unsupported") throw wire("effort", `no effort style delivers effort ${JSON.stringify(request.effort)}`);
    placeEffort(fields, request.effort, style);
  }
  if (request.reasoning_budget !== null) {
    if (style !== "reasoning-object") throw wire("reasoning_budget", "only the reasoning-object style carries a reasoning budget");
    if (Object.hasOwn(fields, "reasoning")) throw wire("extras.reasoning", "extras already hold 'reasoning'");
    fields.reasoning = { max_tokens: request.reasoning_budget };
  }
  return fields;
}
