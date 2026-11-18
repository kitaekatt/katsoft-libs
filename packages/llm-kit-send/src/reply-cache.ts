// The reply cache: the key of one request as it goes on the wire (llm-kit.reply-key/v1), the stored entry
// (llm-kit.reply-entry/v1), and the one lookup, capture and commit that every cached transport shares. The consumer
// gives the store, bound to one owner; this module never decides retention or who owns an entry.
import { createHash } from "node:crypto";

/** The format id of a request key: the first part of every key. */
export const REPLY_KEY_FORMAT = "llm-kit.reply-key/v1";
/** The format id of a stored entry: the value of its `format` field. */
export const REPLY_ENTRY_FORMAT = "llm-kit.reply-entry/v1";

/** What a step answer means for the cache. `null`: do not store. */
export type ReplyVerdict = "refusal" | "success" | null;

/** Which memory an entry belongs to: one provider request (`step`), or the whole operation of the consumer
 * (`operation`). */
export type ReplyLayer = "step" | "operation";

/** One stored entry, format `llm-kit.reply-entry/v1`. */
export type ReplyEntry =
  | {
    readonly format: typeof REPLY_ENTRY_FORMAT;
    readonly layer: "step";
    readonly outcome: "refusal" | "success";
    /** The HTTP status of the stored answer. */
    readonly status: number;
    /** The kept response headers, names lowercased: `content-type` and the transport's named ones. */
    readonly headers: Readonly<Record<string, string>>;
    /** The body bytes, base64 (`""` for a headers-only entry). */
    readonly body: string;
    /** When it was stored, in milliseconds since the epoch. */
    readonly at: number;
  }
  | {
    readonly format: typeof REPLY_ENTRY_FORMAT;
    readonly layer: "operation";
    readonly outcome: "refusal";
    /** The HTTP status the caller got. */
    readonly status: number;
    /** What the caller got: a code and the consumer's fixed text. Never a prompt, never a picture. */
    readonly note: { readonly code: string; readonly message: string };
    readonly at: number;
  };

/** The store the consumer gives. It is bound to one owner before the call. */
export interface ReplyStore {
  /** The live entry for the key, or null. An expired entry is null: the store owns retention. */
  get(layer: ReplyLayer, key: string): Promise<ReplyEntry | null>;
  /** Writes the entry. A refusal replaces a live success; a success never replaces a live refusal; any other live
   * entry stays. */
  put(layer: ReplyLayer, key: string, entry: ReplyEntry): Promise<void>;
}

/** The cache option of one non-stream call. */
export interface StepCache {
  readonly store: ReplyStore;
  /** A non-secret label of the account of the key actually sent, such as `"router:primary"`. Part of the key. A
   * cached call through a {@link withBackupKey} fetch throws `TypeError`: that fetch can change the key after the
   * lookup. */
  readonly account: string;
  /** Extra key parts, put after the account (one part `scope:<requestKey(scope)>`): the caller's own identity of the
   * operation, so a changed caller input is a new key even when the request bytes are the same. */
  readonly scope?: readonly string[];
  /** The URL prefixes that may be cached: the exact origin, then a path prefix on a `/` boundary. */
  readonly hosts: readonly string[];
  /** A deliberate second look: a stored success is not replayed, and a success is not stored. A stored refusal still
   * replays. */
  readonly freshCheck?: boolean;
  /** The largest body to store, in bytes (default 65536). A larger answer is not stored. */
  readonly maxBytes?: number;
  /** Called when `store.put` fails after the answer is known; the call then returns its normal result. Without it, the
   * call throws the {@link ReplyCacheError}. */
  readonly onPutError?: (error: ReplyCacheError) => void;
}

/** What `classify` gets besides the parsed result. */
export interface StepContext {
  readonly url: string;
  /** The `model` (or `modelId`) of the JSON body, else the transport's model argument, else `""`. */
  readonly model: string;
  /** The transport: `"sendChat"`, `"sendWithRetries"`, `"postImages"` or `"ideogramEdit"`. */
  readonly transport: string;
}

/** The cache option of a parsed transport: a {@link StepCache} and the meaning of the parsed result. */
export interface ParsedStepCache<R> extends StepCache {
  /** The meaning of the parsed result. HTTP 200 alone never decides. A `success` is stored only for a deterministic
   * request (a chat transport, `temperature` 0 in the sent bytes, not `freshCheck`). */
  readonly classify: (result: R, context: StepContext) => ReplyVerdict;
}

/** A raw answer with the cache: the response, whether it is a replay, and the caller's verdict. */
export interface CachedResponse {
  /** The response. On a miss its body passes through a bounded copy; on a replay it is rebuilt from the entry. */
  readonly response: Response;
  readonly replayed: boolean;
  /** The caller's verdict, after it read what it needs. `null`, and a `success` that is not deterministic, store
   * nothing at once. `headersOnly` stores the status and the kept headers with an empty body and never needs the body.
   * Otherwise the caller must first read the body to its end; a body that is not complete, or over `maxBytes`, stores
   * nothing. `settle` never reads the body. A second call returns the first call's promise; a call on a replay does
   * nothing. */
  settle(verdict: ReplyVerdict, options?: { readonly headersOnly?: boolean }): Promise<void>;
}

/** One request as it goes on the wire. */
export interface WireRequest {
  readonly method: string;
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string | Uint8Array | FormData | null;
}

/**
 * A store failure of the reply cache. It is never a {@link SendError}: it is not retried, not a fallback case and
 * never read as no answer.
 *
 * @param op - The store call that failed: `get` (also a stored value that is not a valid entry) or `put`.
 * @param message - A short description; never a key, a body or a note text.
 * @param options - `cause`: the store's error.
 * @returns The error.
 * @throws Never.
 */
export class ReplyCacheError extends Error {
  /** The store call that failed. */
  readonly op: "get" | "put";

  constructor(op: "get" | "put", message: string, options: { cause: unknown }) {
    super(message, { cause: options.cause });
    this.name = "ReplyCacheError";
    this.op = op;
  }
}

/**
 * A stored value that breaks `llm-kit.reply-entry/v1`; {@link readEntry} throws it.
 *
 * @param path - Where the value breaks the rule (`note.code`, `$` for the whole value).
 * @param message - What the rule is.
 * @returns The error; its message starts with `path`.
 * @throws Never.
 */
export class ReplyEntryError extends Error {
  /** Where the value breaks the rule. */
  readonly path: string;

  constructor(path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = "ReplyEntryError";
    this.path = path;
  }
}

const encoder = new TextEncoder();
const bytesOf = (part: string | Uint8Array) => (typeof part === "string" ? encoder.encode(part) : part);

/**
 * The key of a list of parts: the lowercase hex SHA-256 of each part written as `<byte length>:<bytes>\n`, in order.
 * A string part is its UTF-8 bytes. The framing keeps parts apart: `["ab", "c"]` and `["a", "bc"]` differ.
 *
 * @param parts - The parts, in order.
 * @returns 64 lowercase hex characters.
 * @throws Never.
 */
export function requestKey(parts: readonly (string | Uint8Array)[]): string {
  const hash = createHash("sha256");
  for (const part of parts) {
    const bytes = bytesOf(part);
    hash.update(`${bytes.byteLength}:`);
    hash.update(bytes);
    hash.update("\n");
  }
  return hash.digest("hex");
}

/** The request headers that change what a host answers. An allowlist: a secret header never enters a key. */
const KEPT_REQUEST_HEADERS: ReadonlySet<string> = new Set(["accept", "content-type"]);

const keptHeaderLines = (headers: Readonly<Record<string, string>>) => Object.entries(headers)
  .map(([name, value]) => [name.toLowerCase(), value] as const)
  .filter(([name]) => KEPT_REQUEST_HEADERS.has(name))
  .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  .map(([name, value]) => `${name}:${value}`)
  .join("\n");

async function bodyParts(body: WireRequest["body"]): Promise<(string | Uint8Array)[]> {
  if (body === null) return [""];
  if (typeof body === "string" || body instanceof Uint8Array) return [body];
  if (body instanceof FormData) {
    const parts: (string | Uint8Array)[] = [];
    for (const [name, value] of body.entries()) {
      if (typeof value === "string") parts.push(name, "", "", value);
      else parts.push(name, value.name, value.type, new Uint8Array(await value.arrayBuffer()));
    }
    return parts;
  }
  throw new TypeError("A cached request body must be a string, bytes, FormData or null.");
}

/**
 * The step key of one request: {@link requestKey} over the format, the account, the scope (one part
 * `scope:<requestKey(scope)>`, only when it is not empty), the method in upper case, the URL as sent, the kept request
 * headers (`accept` and `content-type`, names lowercased, sorted, one `name:value` line each, as one part) and the body
 * (a string or bytes as one part; `null` as one empty part; for FormData each entry as four parts `name`, `filename`,
 * `type`, bytes, in form order, with `""` filename and type for a text field). Secrets, attribution headers, lengths
 * and the multipart boundary are not inputs.
 *
 * @param account - The non-secret label of the key's account.
 * @param request - The method, URL, headers and body as sent.
 * @param scope - The caller's extra parts (see {@link StepCache.scope}).
 * @returns 64 lowercase hex characters.
 * @throws {TypeError} For a body that is not a string, bytes, FormData or null (a stream).
 */
export async function stepKey(account: string, request: WireRequest, scope: readonly string[] = []): Promise<string> {
  return requestKey([
    REPLY_KEY_FORMAT, account, ...(scope.length ? [`scope:${requestKey(scope)}`] : []),
    request.method.toUpperCase(), request.url, keptHeaderLines(request.headers), ...await bodyParts(request.body),
  ]);
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const STEP_KEYS = ["format", "layer", "outcome", "status", "headers", "body", "at"];
const OPERATION_KEYS = ["format", "layer", "outcome", "status", "note", "at"];
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

/**
 * Checks a stored value against `llm-kit.reply-entry/v1` (the JSON Schema `reply-entry.v1.schema.json` in
 * katsoft-llm-kit, plus the reader rules: `body` is canonical base64, header names are lowercase).
 *
 * @param value - The parsed value.
 * @returns The value, typed.
 * @throws {ReplyEntryError} With the path of the first rule it breaks.
 */
export function readEntry(value: unknown): ReplyEntry {
  if (!isObject(value)) throw new ReplyEntryError("$", "an entry is an object");
  if (value.format !== REPLY_ENTRY_FORMAT) throw new ReplyEntryError("format", `must be "${REPLY_ENTRY_FORMAT}"`);
  if (value.layer !== "step" && value.layer !== "operation") throw new ReplyEntryError("layer", 'must be "step" or "operation"');
  const allowed = value.layer === "step" ? STEP_KEYS : OPERATION_KEYS;
  for (const key of allowed) if (!(key in value)) throw new ReplyEntryError(key, "is required");
  for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new ReplyEntryError(key, "is not a field of this layer");
  const outcomes = value.layer === "step" ? ["refusal", "success"] : ["refusal"];
  if (!outcomes.includes(value.outcome as string)) throw new ReplyEntryError("outcome", `must be one of ${outcomes.join(", ")}`);
  if (!Number.isInteger(value.status) || (value.status as number) < 100 || (value.status as number) > 599) {
    throw new ReplyEntryError("status", "must be an integer from 100 to 599");
  }
  if (!Number.isInteger(value.at) || (value.at as number) < 0) throw new ReplyEntryError("at", "must be an integer of 0 or more");
  if (value.layer === "step") {
    if (!isObject(value.headers)) throw new ReplyEntryError("headers", "must be an object");
    for (const [name, header] of Object.entries(value.headers)) {
      if (typeof header !== "string") throw new ReplyEntryError(`headers.${name}`, "must be a string");
      if (name !== name.toLowerCase() || !name) throw new ReplyEntryError(`headers.${name}`, "a header name is lowercase and not empty");
    }
    if (typeof value.body !== "string" || !BASE64.test(value.body)) throw new ReplyEntryError("body", "must be base64");
  } else {
    const note = value.note;
    if (!isObject(note)) throw new ReplyEntryError("note", "must be an object");
    for (const key of ["code", "message"]) if (!(key in note)) throw new ReplyEntryError(`note.${key}`, "is required");
    for (const key of Object.keys(note)) if (key !== "code" && key !== "message") throw new ReplyEntryError(`note.${key}`, "is not a field of a note");
    if (typeof note.code !== "string" || !note.code) throw new ReplyEntryError("note.code", "must be a string that is not empty");
    if (typeof note.message !== "string") throw new ReplyEntryError("note.message", "must be a string");
  }
  return value as unknown as ReplyEntry;
}

// ---------------------------------------------------------------------------------------------------------------------
// The one internal implementation: lookup, capture and commit. `classify` (parsed transports) and `settle` (raw
// transports) are its two public doors.

/** Marks a fetch made by {@link withBackupKey}: a cached call through it throws, since it can change the key. */
export const BACKUP_KEY_FETCH: unique symbol = Symbol("llm-kit-send.backup-key-fetch");

const DEFAULT_MAX_BYTES = 65_536;
const NULL_BODY = new Set([101, 204, 205, 304]);
/** A busy answer is never stored: the host may answer the same bytes the next time. */
const busy = (status: number) => status === 408 || status === 429 || status >= 500;

function hostAllowed(url: string, hosts: readonly string[]): boolean {
  let target: URL;
  try { target = new URL(url); } catch { return false; }
  return hosts.some((host) => {
    let prefix: URL;
    try { prefix = new URL(host); } catch { return false; }
    if (prefix.origin !== target.origin) return false;
    const path = prefix.pathname.replace(/\/+$/, "");
    return !path || target.pathname === path || target.pathname.startsWith(`${path}/`);
  });
}

const CHAT_TRANSPORTS: ReadonlySet<string> = new Set(["sendChat", "sendWithRetries", "postChat"]);

/** True when a success of this request may be stored and replayed: a chat transport, not a second look, and wire
 * bytes that are a JSON object whose top-level `temperature` is the number 0. */
export function deterministic(transport: string, bytes: unknown, cache: StepCache): boolean {
  if (!CHAT_TRANSPORTS.has(transport) || cache.freshCheck || typeof bytes !== "string") return false;
  let body: unknown;
  try { body = JSON.parse(bytes); } catch { return false; }
  return isObject(body) && typeof body.temperature === "number" && body.temperature === 0;
}

/** The model of a JSON body (`model`, else `modelId`), else `fallback`. */
export function modelOf(bytes: unknown, fallback = ""): string {
  if (typeof bytes !== "string") return fallback;
  try {
    const body = JSON.parse(bytes);
    if (isObject(body)) {
      if (typeof body.model === "string") return body.model;
      if (typeof body.modelId === "string") return body.modelId;
    }
  } catch { /* not JSON */ }
  return fallback;
}

/** One cached step: its key, its options and what the transport sent. */
export interface Step {
  readonly cache: StepCache;
  readonly key: string;
  readonly transport: string;
  readonly url: string;
  readonly model: string;
  /** True when a success of the request may be stored and replayed. */
  readonly deterministic: boolean;
  /** The response headers to keep besides `content-type`. */
  readonly keptHeaders: readonly string[];
  /** When the lookup started. */
  readonly started: number;
}

/** Checks the call before anything is sent. A caller error, found in tests. */
export function checkCached(cache: StepCache, url: string, fetchImpl: unknown): void {
  if (!hostAllowed(url, cache.hosts)) throw new TypeError("A cached call must go to one of the cache's hosts.");
  if (typeof fetchImpl === "function" && (fetchImpl as { [BACKUP_KEY_FETCH]?: unknown })[BACKUP_KEY_FETCH]) {
    throw new TypeError("A cached call cannot use a withBackupKey fetch: it may send another key than the one in the cache key.");
  }
}

/** Computes the key and looks it up. `entry` is a stored answer that may be replayed now, else null. */
export async function openStep(cache: StepCache, transport: string, request: WireRequest, model: string,
  keptHeaders: readonly string[] = []): Promise<{ step: Step; entry: Extract<ReplyEntry, { layer: "step" }> | null }> {
  const started = Date.now();
  const key = await stepKey(cache.account, request, cache.scope ?? []);
  const step: Step = {
    cache, key, transport, url: request.url, model, deterministic: deterministic(transport, request.body, cache), keptHeaders, started,
  };
  let stored: unknown;
  try {
    stored = await cache.store.get("step", key);
  } catch (error) {
    throw new ReplyCacheError("get", "The reply store could not be read.", { cause: error });
  }
  if (stored === null || stored === undefined) return { step, entry: null };
  let entry: ReplyEntry;
  try {
    entry = readEntry(stored);
  } catch (error) {
    throw new ReplyCacheError("get", "The reply store gave a value that is not a reply entry.", { cause: error });
  }
  if (entry.layer !== "step") throw new ReplyCacheError("get", "The reply store gave an operation entry for a step key.", { cause: null });
  // A stored success replays only while the request is deterministic now; a refusal always replays.
  return { step, entry: entry.outcome === "refusal" || step.deterministic ? entry : null };
}

/** The response rebuilt from a stored entry. */
export function replayResponse(entry: Extract<ReplyEntry, { layer: "step" }>): Response {
  const body = NULL_BODY.has(entry.status) ? null : Buffer.from(entry.body, "base64");
  return new Response(body, { status: entry.status, headers: entry.headers });
}

/** The bytes of a stored entry. */
export const entryBytes = (entry: Extract<ReplyEntry, { layer: "step" }>): Uint8Array => new Uint8Array(Buffer.from(entry.body, "base64"));

/** What a step answered, for {@link commitStep}: `bytes` null stores the headers only. */
export interface StepAnswer {
  readonly status: number;
  readonly headers: Headers;
  readonly bytes: Uint8Array | null;
}

/** Stores the answer when the verdict and the request allow it. A put failure goes to `onPutError`, else throws. */
export async function commitStep(step: Step, verdict: ReplyVerdict, answer: StepAnswer): Promise<void> {
  if (verdict !== "refusal" && verdict !== "success") return;
  if (verdict === "success" && !step.deterministic) return;
  if (busy(answer.status)) return;
  const bytes = answer.bytes ?? new Uint8Array(0);
  if (bytes.byteLength > (step.cache.maxBytes ?? DEFAULT_MAX_BYTES)) return;
  const headers: Record<string, string> = {};
  for (const name of ["content-type", ...step.keptHeaders.map((item) => item.toLowerCase())]) {
    const value = answer.headers.get(name);
    if (value !== null) headers[name] = value;
  }
  const entry: ReplyEntry = {
    format: REPLY_ENTRY_FORMAT, layer: "step", outcome: verdict, status: answer.status, headers,
    body: Buffer.from(bytes).toString("base64"), at: Date.now(),
  };
  try {
    await step.cache.store.put("step", step.key, entry);
  } catch (cause) {
    const error = new ReplyCacheError("put", "The reply store could not be written.", { cause });
    if (!step.cache.onPutError) throw error;
    step.cache.onPutError(error);
  }
}

/**
 * The response of a miss with its body passing through a copy of at most `maxBytes`, and the settle handle. The caller
 * reads the response as it would without the cache, and owns its reader.
 */
export function capturedResponse(step: Step, response: Response): CachedResponse {
  const limit = step.cache.maxBytes ?? DEFAULT_MAX_BYTES;
  const kept: { chunks: Uint8Array[]; size: number; done: boolean; over: boolean } = { chunks: [], size: 0, done: !response.body, over: false };
  let given = response;
  if (response.body) {
    const copy = new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        if (!kept.over) {
          kept.size += chunk.byteLength;
          if (kept.size > limit) { kept.over = true; kept.chunks = []; } else kept.chunks.push(chunk.slice());
        }
        controller.enqueue(chunk);
      },
      flush() { kept.done = true; },
    });
    given = new Response(response.body.pipeThrough(copy), { status: response.status, statusText: response.statusText, headers: response.headers });
  }
  let settled: Promise<void> | null = null;
  return {
    response: given,
    replayed: false,
    settle(verdict, options = {}) {
      settled ??= (async () => {
        if (options.headersOnly) return commitStep(step, verdict, { status: response.status, headers: response.headers, bytes: null });
        if (!kept.done || kept.over) return;
        const bytes = new Uint8Array(kept.size);
        let at = 0;
        for (const chunk of kept.chunks) { bytes.set(chunk, at); at += chunk.byteLength; }
        return commitStep(step, verdict, { status: response.status, headers: response.headers, bytes });
      })();
      return settled;
    },
  };
}

/** The handle of a replayed raw answer: `settle` does nothing. */
export function replayedResponse(entry: Extract<ReplyEntry, { layer: "step" }>): CachedResponse {
  return { response: replayResponse(entry), replayed: true, settle: () => Promise.resolve() };
}

const decoder = new TextDecoder();
/** The body as JSON, or `ok: false` when it is not JSON. */
export function parseJson(bytes: Uint8Array): { ok: true; value: any } | { ok: false } {
  try { return { ok: true, value: JSON.parse(decoder.decode(bytes)) }; } catch { return { ok: false }; }
}
