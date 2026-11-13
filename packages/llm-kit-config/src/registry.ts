/**
 * The project model file (`llm-kit.project-models/v1`) and its overlays
 * (`llm-kit.project-models-local/v1`): read, validate, layer and select.
 *
 * The rules are the portable validation of the Python reader
 * (`llm_kit.registry.project_models.validate_portable_document`); the shared
 * cases in `tests/formats/project-models/cases.json` fix them. Binding (the
 * machine's endpoint registry, the library effort menu, reserved extras) is
 * not part of this package.
 *
 * @module
 */

import { LlmKitConfigError, invalid, withSource } from "./errors.js";
import { Float, Foreign, PyMap, loadYaml } from "./pyyaml.js";
import type { PyValue } from "./pyyaml.js";
import { compareCodePoints, decodeText, deepFreeze, define, hasOuterSpace, pythonRepr, readBytes } from "./text.js";

/** Any JSON value: what a row's `request_defaults.extras` holds. */
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

/** One catalog row of the project model file. The keys are the file's keys. */
export interface CatalogRow {
  /** The row id that selections name; unique in the catalog. */
  readonly id: string;
  /** Presentation text. */
  readonly label: string;
  /** The endpoint name; the consumer resolves it. */
  readonly endpoint: string;
  /** The exact wire model for that endpoint. */
  readonly model: string;
  /** Omitted: the row does not narrow the effort menu. `[]`: the row has no effort. */
  readonly efforts?: readonly string[];
  /** The output cap for calls to this model, from 1 to 2**53 - 1. */
  readonly max_output_tokens?: number;
  /** Request defaults; only `extras`, a mapping of JSON values. */
  readonly request_defaults?: { readonly extras?: { readonly [key: string]: JsonValue } };
}

/** A selection: a catalog id and an effort (`null` for no effort). */
export interface Selection {
  /** A catalog row id. */
  readonly model: string;
  /** One of the row's efforts, or `null`. */
  readonly effort: string | null;
}

/** The normalized tracked file: what portable validation returns. */
export interface ProjectModelsDocument {
  /** Always 1. */
  readonly version: 1;
  /** The catalog rows in file order. */
  readonly catalog: readonly CatalogRow[];
  /** Selection key to selection, sorted by key. */
  readonly selections: { readonly [key: string]: Selection };
}

/** The normalized overlay file (the personal-layer format). */
export interface OverlayDocument {
  /** Always 1. */
  readonly version: 1;
  /** Selection key to selection, sorted by key. */
  readonly selections: { readonly [key: string]: Selection };
}

/** Where the effective selection of a key comes from. */
export type SelectionSource = "default" | "override";

/** One key after layering; the shape of Python's `resolve_selections` entries. */
export interface ResolvedSelection {
  /** The selection that applies. */
  readonly effective: Selection;
  /** The tracked file's selection, or `null` for a key only an overlay names. */
  readonly default: Selection | null;
  /** `override` when the effective selection differs from the default, else `default`. */
  readonly source: SelectionSource;
}

/** A validated project model file with zero or more overlays applied. */
export interface Registry {
  /** The path or name the tracked file was read from, when known. */
  readonly source?: string;
  /** The normalized tracked file. */
  readonly document: ProjectModelsDocument;
  /** Every selection key of every layer, sorted by key. */
  readonly resolved: { readonly [key: string]: ResolvedSelection };
}

/** A validated overlay. Its catalog references are checked when it is applied. */
export interface Overlay {
  /** The path or name the overlay was read from, when known. */
  readonly source?: string;
  /** The normalized overlay file. */
  readonly document: OverlayDocument;
}

/** The answer of {@link select}: the row and effort that apply to a key. */
export interface Resolved {
  /** The selection key. */
  readonly key: string;
  /** The catalog row the effective selection names. */
  readonly row: CatalogRow;
  /** The effective effort, or `null` for no effort. */
  readonly effort: string | null;
  /** Whether an overlay changed the tracked selection. */
  readonly source: SelectionSource;
}

/** Options of {@link parseRegistry}, {@link parseOverlay} and {@link parsePrompt}. */
export interface ParseOptions {
  /** The path or name of the text; it appears in errors and in the result. */
  readonly source?: string;
}

const MAX_INTEGER = 2n ** 53n - 1n;
const TOP_KEYS = ["version", "catalog", "selections"] as const;
const LOCAL_TOP_KEYS = ["version", "selections"] as const;
const ROW_KEYS = ["id", "label", "endpoint", "model", "efforts", "max_output_tokens", "request_defaults"] as const;
const ROW_REQUIRED = ["id", "label", "endpoint", "model"] as const;
const SELECTION_KEYS = ["model", "effort"] as const;
const DEFAULTS_KEYS = ["extras"] as const;

/** Registries and overlays this module made; applyOverlay and select accept only these. */
const REGISTRIES = new WeakSet<object>();
const OVERLAYS = new WeakMap<object, readonly string[]>();

// ---------------------------------------------------------------------------
// The portable validation, in the order of the Python reader
// ---------------------------------------------------------------------------

function typeName(value: PyValue): string {
  if (value === null) return "NoneType";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "bigint") return "int";
  if (typeof value === "string") return "str";
  if (value instanceof Float) return "float";
  if (Array.isArray(value)) return "list";
  if (value instanceof PyMap) return "dict";
  return value.kind;
}

/** A mapping's own entries as a string-keyed map, or a refusal (Python `_mapping`). */
function mapping(value: PyValue, where: string, allowed: readonly string[]): Map<string, PyValue> {
  if (!(value instanceof PyMap)) throw invalid(where, `must be a mapping, not ${typeName(value)}`);
  const out = new Map<string, PyValue>();
  for (const [key, item] of value.entries) {
    if (typeof key !== "string") throw invalid(where, `a key must be a string, not ${typeName(key)}`);
    out.set(key, item);
  }
  const unknown = [...out.keys()].filter((key) => !allowed.includes(key)).sort(compareCodePoints);
  if (unknown.length > 0) throw invalid(where, `unknown key(s) ${unknown.join(", ")}; allowed: ${allowed.join(", ")}`);
  return out;
}

/** Non-empty text with no outer whitespace (Python `_text`). */
function text(value: PyValue, where: string): string {
  if (typeof value !== "string") throw invalid(where, `must be a string, not ${typeName(value)}`);
  if (value === "" || hasOuterSpace(value)) throw invalid(where, "must be non-empty with no leading or trailing whitespace");
  return value;
}

/** A deep, key-sorted JSON copy; anything else is refused (Python `_json_value`). */
function jsonValue(value: PyValue, where: string, open: ReadonlySet<object> = new Set()): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "bigint") {
    if (value > MAX_INTEGER || value < -MAX_INTEGER) {
      throw invalid(where, `must be an integer from -${MAX_INTEGER} to ${MAX_INTEGER}, not ${value}`);
    }
    return Number(value);
  }
  if (value instanceof Float) {
    if (!Number.isFinite(value.value)) throw invalid(where, "must be a finite number");
    return value.value;
  }
  if (Array.isArray(value) || value instanceof PyMap) {
    if (open.has(value)) throw invalid(where, "holds itself (a YAML alias cycle)");
    const inner = new Set(open).add(value);
    if (Array.isArray(value)) return value.map((item, index) => jsonValue(item, `${where}[${index}]`, inner));
    const keyed = value.entries.filter(([key]) => typeof key === "string") as Array<[string, PyValue]>;
    keyed.sort(([a], [b]) => compareCodePoints(a, b));
    const out: { [key: string]: JsonValue } = {};
    for (const [key, item] of keyed) define(out, key, jsonValue(item, `${where}.${key}`, inner));
    if (keyed.length !== value.entries.length) throw invalid(where, "a key must be a string");
    return out;
  }
  throw invalid(where, `must be JSON data, not ${value.kind}`);
}

function requestDefaults(value: PyValue, where: string): NonNullable<CatalogRow["request_defaults"]> {
  const defaults = mapping(value, where, DEFAULTS_KEYS);
  const out: { extras?: { [key: string]: JsonValue } } = {};
  if (defaults.has("extras")) {
    const extras = defaults.get("extras")!;
    if (!(extras instanceof PyMap)) throw invalid(`${where}.extras`, "must be a mapping");
    out.extras = jsonValue(extras, `${where}.extras`) as { [key: string]: JsonValue };
  }
  return out;
}

function maxOutputTokens(value: PyValue, where: string): number {
  if (typeof value !== "bigint" || value < 1n || value > MAX_INTEGER) {
    throw invalid(where, `must be a positive integer up to ${MAX_INTEGER}`);
  }
  return Number(value);
}

function efforts(value: PyValue, where: string): string[] {
  if (!Array.isArray(value)) throw invalid(where, "must be a list (an empty list declares no effort)");
  const out: string[] = [];
  value.forEach((item, index) => {
    const at = `${where}[${index}]`;
    if (typeof item !== "string" || item === "") {
      throw invalid(at, "must be a non-empty string (YAML reads on/off/yes/no as booleans; quote the word)");
    }
    if (out.includes(item)) throw invalid(at, `${JSON.stringify(item)} repeats`);
    out.push(item);
  });
  return out;
}

/** The catalog and, per row id, its declared efforts (`undefined` when it declares none). */
function catalog(value: PyValue, where = "catalog"): { rows: CatalogRow[]; efforts: Map<string, string[] | undefined> } {
  if (!Array.isArray(value)) throw invalid(where, `must be a list, not ${typeName(value)}`);
  const rows: CatalogRow[] = [];
  const declared = new Map<string, string[] | undefined>();
  value.forEach((raw, index) => {
    const at = `catalog[${index}]`;
    const row = mapping(raw, at, ROW_KEYS);
    for (const key of ROW_REQUIRED) if (!row.has(key)) throw invalid(at, `'${key}' is required`);
    const id = text(row.get("id")!, `${at}.id`);
    if (declared.has(id)) throw invalid(`${at}.id`, `duplicate id ${JSON.stringify(id)}`);
    const out: {
      id: string; label: string; endpoint: string; model: string; efforts?: string[];
      max_output_tokens?: number; request_defaults?: NonNullable<CatalogRow["request_defaults"]>;
    } = {
      id,
      label: text(row.get("label")!, `${at}.label`),
      endpoint: text(row.get("endpoint")!, `${at}.endpoint`),
      model: text(row.get("model")!, `${at}.model`),
    };
    let rowEfforts: string[] | undefined;
    if (row.has("efforts")) out.efforts = rowEfforts = efforts(row.get("efforts")!, `${at}.efforts`);
    if (row.has("max_output_tokens")) out.max_output_tokens = maxOutputTokens(row.get("max_output_tokens")!, `${at}.max_output_tokens`);
    if (row.has("request_defaults")) out.request_defaults = requestDefaults(row.get("request_defaults")!, `${at}.request_defaults`);
    declared.set(id, rowEfforts);
    rows.push(out);
  });
  return { rows, efforts: declared };
}

/** The reference checks of one selection: its model is a catalog id and its effort is accepted. */
function checkReference(selection: Selection, where: string, declared: Map<string, string[] | undefined>): void {
  if (!declared.has(selection.model)) {
    throw invalid(`${where}.model`, `${JSON.stringify(selection.model)} is not a catalog id`);
  }
  const allowed = declared.get(selection.model);
  if (selection.effort !== null && allowed !== undefined && !allowed.includes(selection.effort)) {
    throw invalid(`${where}.effort`, `${JSON.stringify(selection.effort)} is not accepted for ${JSON.stringify(selection.model)} (accepted: ${allowed.join(", ") || "none"})`);
  }
}

/**
 * The selections in file order (Python `_validate_selections`). With
 * `declared`, each selection's references are checked where Python checks
 * them; without it, only the structure is checked.
 */
function selections(value: PyValue, declared?: Map<string, string[] | undefined>): Array<[string, Selection]> {
  if (!(value instanceof PyMap)) throw invalid("selections", `must be a mapping, not ${typeName(value)}`);
  const out: Array<[string, Selection]> = [];
  for (const [key, raw] of value.entries) {
    if (typeof key !== "string") throw invalid("selections", `a key must be a string, not ${typeName(key)}`);
    const where = `selections[${pythonRepr(key)}]`;
    if (key === "" || hasOuterSpace(key)) throw invalid(where, "the key must be non-empty with no leading or trailing whitespace");
    const picked = mapping(raw, where, SELECTION_KEYS);
    if (!picked.has("model")) throw invalid(where, "'model' is required");
    const model = text(picked.get("model")!, `${where}.model`);
    if (declared && !declared.has(model)) throw invalid(`${where}.model`, `${JSON.stringify(model)} is not a catalog id`);
    const effort = picked.get("effort") ?? null;
    if (effort !== null && (typeof effort !== "string" || effort === "")) {
      throw invalid(`${where}.effort`, "must be a non-empty string or null (YAML reads on/off/yes/no as booleans; quote the word)");
    }
    const selection: Selection = { model, effort };
    if (declared) checkReference(selection, where, declared);
    out.push([key, selection]);
  }
  return out;
}

function checkVersion(top: Map<string, PyValue>): void {
  if (!top.has("version")) throw invalid("version", "is required");
  if (top.get("version") !== 1n) throw invalid("version", "must be 1");
}

function sortedSelections(entries: Array<[string, Selection]>): { [key: string]: Selection } {
  const out: { [key: string]: Selection } = {};
  for (const [key, selection] of [...entries].sort(([a], [b]) => compareCodePoints(a, b))) define(out, key, selection);
  return out;
}

function validateTracked(data: PyValue): ProjectModelsDocument {
  const top = mapping(data, "document", TOP_KEYS);
  checkVersion(top);
  const { rows, efforts: declared } = catalog(top.has("catalog") ? top.get("catalog")! : []);
  const picked = selections(top.has("selections") ? top.get("selections")! : new PyMap(), declared);
  return { version: 1, catalog: rows, selections: sortedSelections(picked) };
}

function sameSelection(a: Selection | null, b: Selection | null): boolean {
  return a !== null && b !== null && a.model === b.model && a.effort === b.effort;
}

function makeRegistry(
  document: ProjectModelsDocument, layers: ReadonlyArray<OverlayDocument>, source: string | undefined,
): Registry {
  const effective = new Map<string, Selection>(Object.entries(document.selections));
  for (const layer of layers) for (const [key, selection] of Object.entries(layer.selections)) effective.set(key, selection);
  const resolved: { [key: string]: ResolvedSelection } = {};
  for (const key of [...effective.keys()].sort(compareCodePoints)) {
    const fallback = Object.hasOwn(document.selections, key) ? document.selections[key]! : null;
    const picked = effective.get(key)!;
    define(resolved, key, sameSelection(picked, fallback)
      ? { effective: fallback!, default: fallback, source: "default" }
      : { effective: picked, default: fallback, source: "override" });
  }
  const registry: Registry = source === undefined ? { document, resolved } : { source, document, resolved };
  REGISTRIES.add(deepFreeze(registry));
  return registry;
}

function withContext<T>(source: string | undefined, run: () => T): T {
  try {
    return run();
  } catch (error) {
    if (error instanceof LlmKitConfigError) throw withSource(error, source);
    throw error;
  }
}

function sourceOf(options: ParseOptions | undefined): string | undefined {
  const source = options?.source;
  if (source !== undefined && typeof source !== "string") throw new TypeError("options.source must be a string");
  return source;
}

// ---------------------------------------------------------------------------
// The public functions
// ---------------------------------------------------------------------------

/**
 * Read and validate a project model file (`llm-kit.project-models/v1`).
 *
 * The bytes are decoded as strict UTF-8; a string is checked to be well
 * formed. One leading byte order mark is skipped and CRLF, CR and LF are line
 * breaks. The text is one YAML 1.1 document read as the PyYAML safe loader
 * reads it. Portable validation then checks the structure and the
 * references. Binding (endpoints, the library effort menu, reserved extras)
 * is the consumer's.
 *
 * @param input - The file's bytes, or its text.
 * @param options - `source`: the path or name of the text, for errors and the result.
 * @returns A frozen {@link Registry} with no overlay: every key's source is `default`.
 * @throws {LlmKitConfigError} `invalid`, with `where` set to the first problem's path
 *   (the same path the Python reader reports).
 * @throws {TypeError} When `input` is not a string or a `Uint8Array`.
 */
export function parseRegistry(input: string | Uint8Array, options?: ParseOptions): Registry {
  const source = sourceOf(options);
  return withContext(source, () => makeRegistry(validateTracked(loadYaml(decodeText(input, source))), [], source));
}

/**
 * Read the project model file at `path` with {@link parseRegistry}. Synchronous.
 *
 * @param path - The file path; it becomes the registry's `source`.
 * @returns A frozen {@link Registry} with no overlay.
 * @throws {LlmKitConfigError} `missing` when the file does not exist, `io` when it
 *   cannot be read, `invalid` for its content.
 */
export function loadRegistry(path: string): Registry {
  return parseRegistry(readBytes(path), { source: path });
}

/**
 * Read and validate an overlay: a file in the personal-layer format
 * (`llm-kit.project-models-local/v1`), `{version: 1, selections: {...}}`.
 *
 * The bytes and YAML rules are those of {@link parseRegistry}. The structure
 * is checked here; whether each selection names a catalog id and an accepted
 * effort is checked by {@link applyOverlay}, which knows the catalog.
 *
 * @param input - The file's bytes, or its text.
 * @param options - `source`: the path or name of the text, for errors and the result.
 * @returns A frozen {@link Overlay}.
 * @throws {LlmKitConfigError} `invalid`, with `where` set to the first problem's path.
 * @throws {TypeError} When `input` is not a string or a `Uint8Array`.
 */
export function parseOverlay(input: string | Uint8Array, options?: ParseOptions): Overlay {
  const source = sourceOf(options);
  return withContext(source, () => {
    const top = mapping(loadYaml(decodeText(input, source)), "document", LOCAL_TOP_KEYS);
    checkVersion(top);
    const picked = selections(top.has("selections") ? top.get("selections")! : new PyMap());
    const document: OverlayDocument = { version: 1, selections: sortedSelections(picked) };
    const overlay: Overlay = source === undefined ? { document } : { source, document };
    OVERLAYS.set(deepFreeze(overlay), picked.map(([key]) => key));
    return overlay;
  });
}

/**
 * Read the overlay file at `path` with {@link parseOverlay}. Synchronous. A
 * consumer that treats a missing overlay as "no overlay" catches the
 * `missing` code itself; this function never assumes it.
 *
 * @param path - The file path; it becomes the overlay's `source`.
 * @returns A frozen {@link Overlay}.
 * @throws {LlmKitConfigError} `missing` when the file does not exist, `io` when it
 *   cannot be read, `invalid` for its content.
 */
export function loadOverlay(path: string): Overlay {
  return parseOverlay(readBytes(path), { source: path });
}

/**
 * Apply overlays to a registry, in order. Per key, the whole selection of the
 * last layer that names the key wins. A key only an overlay names is an
 * override with no default (`default` is `null`). An overlay entry equal to
 * the tracked entry is no override (`source` stays `default`).
 *
 * Each overlay's selections are checked against the registry's catalog first,
 * in file order: the model must be a catalog id, and the effort must be one of
 * the row's `efforts` when the row declares them (`null` when it declares `[]`).
 *
 * @param registry - A registry from this package (with or without overlays).
 * @param overlays - Overlays from {@link parseOverlay} or {@link loadOverlay}.
 * @returns A new frozen {@link Registry}; the input is not changed.
 * @throws {LlmKitConfigError} `invalid` for the first selection that breaks a
 *   reference rule; `source` is that overlay's source.
 * @throws {TypeError} When an argument does not come from this package.
 */
export function applyOverlay(registry: Registry, ...overlays: Overlay[]): Registry {
  if (!REGISTRIES.has(registry)) throw new TypeError("registry must come from parseRegistry, loadRegistry or applyOverlay");
  const declared = new Map<string, string[] | undefined>(registry.document.catalog.map((row) => [row.id, row.efforts as string[] | undefined]));
  const layers: OverlayDocument[] = [];
  for (const [key, entry] of Object.entries(registry.resolved)) {
    if (entry.source === "override" || entry.default === null) layers.push({ version: 1, selections: { [key]: entry.effective } });
  }
  for (const overlay of overlays) {
    const order = OVERLAYS.get(overlay);
    if (order === undefined) throw new TypeError("each overlay must come from parseOverlay or loadOverlay");
    withContext(overlay.source, () => {
      for (const key of order) checkReference(overlay.document.selections[key]!, `selections[${pythonRepr(key)}]`, declared);
    });
    layers.push(overlay.document);
  }
  return makeRegistry(registry.document, layers, registry.source);
}

/**
 * The resolved row, effort and provenance of a selection key, or `undefined`
 * when no layer names the key. Absence is the only case that returns
 * `undefined`.
 *
 * @param registry - A registry from this package.
 * @param key - The selection key.
 * @returns The {@link Resolved} answer, or `undefined` when the key is absent.
 * @throws {TypeError} When `registry` does not come from this package or `key` is not a string.
 */
export function findSelection(registry: Registry, key: string): Resolved | undefined {
  if (!REGISTRIES.has(registry)) throw new TypeError("registry must come from parseRegistry, loadRegistry or applyOverlay");
  if (typeof key !== "string") throw new TypeError("key must be a string");
  if (!Object.hasOwn(registry.resolved, key)) return undefined;
  const { effective, source } = registry.resolved[key]!;
  const row = registry.document.catalog.find((candidate) => candidate.id === effective.model)!;
  return Object.freeze({ key, row, effort: effective.effort, source });
}

/**
 * The resolved row, effort and provenance of a selection key.
 *
 * @param registry - A registry from this package.
 * @param key - The selection key.
 * @returns The {@link Resolved} answer.
 * @throws {LlmKitConfigError} `unknown-selection` when no layer names the key
 *   (`where` is `selections['<key>']`).
 * @throws {TypeError} When `registry` does not come from this package or `key` is not a string.
 */
export function select(registry: Registry, key: string): Resolved {
  const found = findSelection(registry, key);
  if (found === undefined) {
    throw new LlmKitConfigError("unknown-selection", "no layer names this selection key", `selections[${pythonRepr(key)}]`, registry.source);
  }
  return found;
}
