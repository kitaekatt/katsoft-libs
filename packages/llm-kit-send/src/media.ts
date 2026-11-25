// Picture, voice and file transports: one request to one media host each, sent as given. Each returns what the caller
// needs for its own record (status, delivery, reported cost) or the raw response, so the caller decides what an answer
// means. No retry and no fallback here: a chain of hosts is `runChain` (chain.ts).
import { usageCost } from "./cost.js";
import { dispatch, recordReplay } from "./ledger.js";
import type { CallHandle, CallLabels, CallSpec } from "./ledger.js";
import { capturedResponse, checkCached, commitStep, entryBytes, modelOf, openStep, parseJson, replayedResponse } from "./reply-cache.js";
import type { CachedResponse, ParsedStepCache, StepCache } from "./reply-cache.js";
import { tapResponse } from "./tap.js";

/** The base URL of Venice's API; {@link postVenice} sends to `<base>/image/<endpoint>`. */
export const VENICE_URL = "https://api.venice.ai/api/v1";
/** The base URL of Ideogram's API; {@link ideogramEdit} sends to `<base>/v2/image/precise-edit/<model>`. */
export const IDEOGRAM_URL = "https://api.ideogram.ai";
/** The base URL of fal's synchronous runs; {@link postFal} sends to `<base>/<endpoint>`. */
export const FAL_RUN_URL = "https://fal.run";
/** The base URL of fal's queue; `falQueue` submits to `<base>/<model>`. */
export const FAL_QUEUE_URL = "https://queue.fal.run";
/** The base URL of Sunra's queue; `sunraQueue` submits to `<base>/<endpoint>`. */
export const SUNRA_QUEUE_URL = "https://api.sunra.ai/v1/queue";
/** The base URL of ElevenLabs' API; {@link elevenLabsRequest} sends to `<base><path>`. */
export const ELEVENLABS_URL = "https://api.elevenlabs.io";

/** The options of every media transport. */
export interface MediaOptions {
  /** The transport; the global `fetch` at call time when not given. */
  readonly fetch?: typeof fetch;
  /** The signal of the request (the caller's abort or deadline). */
  readonly signal?: AbortSignal;
  /** The labels of the call for the call ledger (see {@link setCallLedger}); the package never reads them. A download
   * ({@link fetchFile}) is not recorded and ignores them. */
  readonly labels?: CallLabels | undefined;
}

/** Sends `init` as built: its key order and the given signal stay as they are (`signal` may be undefined). Not recorded. */
const send = (options: MediaOptions, url: string, init: Record<string, unknown>): Promise<Response> =>
  (options.fetch ?? globalThis.fetch)(url, init as RequestInit);

const specOf = (options: MediaOptions, transport: string, method: string, url: string, model: string): CallSpec =>
  ({ transport, method, url, model, stream: false, labels: options.labels });

/** Sends `init` as `send` does, as a recorded call: begun before the transport is called, ended by the caller (or by
 * {@link tapResponse}) once the answer is read. The response promise rejects with the transport's own error. */
const sendPaid = (options: MediaOptions, transport: string, model: string, url: string, init: Record<string, unknown>) =>
  dispatch(specOf(options, transport, String(init.method ?? "POST"), url, model), options.fetch ?? globalThis.fetch, url, init, options.signal);

/** Tells the ledger the end of a parsed answer: `parsed` is false when a 2xx body was not JSON. */
const endParsed = (rec: CallHandle, status: number, parsed: boolean, usage: unknown = null) => {
  const ok = status >= 200 && status <= 299;
  rec.end({ ok: ok && parsed, status, errorKind: !ok ? "http" : parsed ? null : "body", usage: ok ? usage : null });
};
const replayInfo = (status: number) => ({ ok: status >= 200 && status <= 299, status, errorKind: status >= 200 && status <= 299 ? null : "http" });

const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

/** One call to an OpenAI-compatible images endpoint. */
export interface ImagesCall {
  /** The images URL (for example `https://openrouter.ai/api/v1/images`). */
  readonly url: string;
  /** Sent as `Authorization: Bearer <key>`. */
  readonly key: string;
  /** Extra headers after `Authorization` and `Content-Type`, such as an app label. */
  readonly headers?: Readonly<Record<string, string>>;
  /** The request object, sent as `JSON.stringify(body)`. */
  readonly body: unknown;
}

/** The answer of {@link postImages}. */
export type ImagesAnswer =
  | {
    readonly ok: true;
    readonly status: number;
    /** The answer JSON, or `{}` when the body was not JSON. */
    readonly payload: any;
    /** The first picture as base64 (`data[0].b64_json`), or `null`. */
    readonly image: string | null;
    /** The first picture's link (`data[0].url`), or `null`. */
    readonly link: string | null;
    /** The answer's `usage`, or `null`. */
    readonly usage: unknown;
    /** The cost the host reported ({@link usageCost}), or `null`; 0 for a replay. */
    readonly cost: number | null;
    /** True for a stored answer of the reply cache: nothing was sent; `usage` is then `null`. */
    readonly replayed: boolean;
  }
  | {
    readonly ok: false;
    /** The HTTP status, or `null` when there was no answer. */
    readonly status: number | null;
    /** The parsed error body, or `{}`. */
    readonly failure: any;
    /** True for a stored answer of the reply cache: nothing was sent. */
    readonly replayed: boolean;
  };

/**
 * One POST to an OpenAI-compatible images endpoint (`/images`, `/images/generations`). The body goes out as given.
 * A 2xx answer always counts as answered: the caller decides whether a picture came (`image`, `link`).
 *
 * With `cache` ({@link ParsedStepCache}), the key is looked up before the send. Only a stored refusal replays (a
 * picture is never deterministic, so a success is never stored): nothing is sent, the stored answer is read by the same
 * code, `replayed` is true, `usage` null and `cost` 0. On a miss, `classify` reads the result of a complete answer
 * that is not busy and whose body parsed as JSON (a body read as `{}` is never stored); a `refusal` is stored.
 *
 * @param call - The URL, key, extra headers and body.
 * @param options - The transport, signal and cache.
 * @returns The parsed answer, or the failure with its status (`null` with no answer). A body that is not JSON, or one
 *   whose read fails, reads as `{}`.
 * @throws The transport's error when the caller's `signal` was aborted. With `cache`: {@link ReplyCacheError} when the
 *   store fails, and `TypeError` before any send for a URL outside the cache's hosts.
 */
export async function postImages(call: ImagesCall, options: MediaOptions & { readonly cache?: ParsedStepCache<ImagesAnswer> | undefined } = {}): Promise<ImagesAnswer> {
  const { signal, cache } = options;
  const headers = { Authorization: `Bearer ${call.key}`, "Content-Type": "application/json", ...call.headers };
  const bytes = JSON.stringify(call.body);
  let opened: Awaited<ReturnType<typeof openStep>> | null = null;
  if (cache) {
    checkCached(cache, call.url, options.fetch);
    opened = await openStep(cache, "postImages", { method: "POST", url: call.url, headers, body: bytes }, modelOf(bytes));
    if (opened.entry) {
      recordReplay(specOf(options, "postImages", "POST", call.url, modelOf(bytes)), replayInfo(opened.entry.status));
      return imagesAnswer(opened.entry.status, parseJson(entryBytes(opened.entry)), true);
    }
  }
  const flight = sendPaid(options, "postImages", modelOf(bytes), call.url, { method: "POST", signal, headers, body: bytes });
  let response: Response;
  try {
    response = await flight.response;
  } catch (error) {
    if (signal?.aborted) throw error;
    return { ok: false, status: null, failure: {}, replayed: false };
  }
  // Without a cache the body is read as in 0.2.0 (`json()`), so a transport that gives a minimal response still works.
  if (!opened) {
    const value = await response.json().then((json: unknown) => ({ ok: true as const, value: json }), () => ({ ok: false as const }));
    const answer = imagesAnswer(response.status, value, false);
    endParsed(flight.rec, response.status, value.ok, answer.ok ? answer.usage : null);
    return answer;
  }
  const read = await response.arrayBuffer().then((buffer) => new Uint8Array(buffer), () => null);
  const parsed = read ? parseJson(read) : { ok: false as const };
  const answer = imagesAnswer(response.status, parsed, false);
  endParsed(flight.rec, response.status, parsed.ok, answer.ok ? answer.usage : null);
  if (read && parsed.ok) {
    const context = { url: call.url, model: opened.step.model, transport: "postImages" };
    await commitStep(opened.step, cache!.classify(answer, context), { status: response.status, headers: response.headers, bytes: read });
  }
  return answer;
}

/** The answer of a status and a parsed body (`{}` when the body was not JSON). */
function imagesAnswer(status: number, parsed: { ok: true; value: any } | { ok: false }, replayed: boolean): ImagesAnswer {
  const value: any = parsed.ok ? parsed.value : {};
  if (status < 200 || status > 299) return { ok: false, status, failure: value, replayed };
  const first = value?.data?.[0];
  return {
    ok: true, status, payload: value, image: text(first?.b64_json), link: text(first?.url),
    usage: replayed ? null : value?.usage ?? null, cost: replayed ? 0 : usageCost(value?.usage), replayed,
  };
}

/** One Venice picture request for {@link postVenice}. */
export interface VeniceCall {
  /** The image endpoint under `<base>/image/`: `generate`, `multi-edit`, ... */
  readonly endpoint: string;
  /** Sent as `Authorization: Bearer <key>`. */
  readonly key: string;
  /** The request object, sent as `JSON.stringify(body)`. */
  readonly body: unknown;
  /** The API base; {@link VENICE_URL} when not given. */
  readonly baseUrl?: string;
}

/**
 * One POST to a Venice image endpoint. The raw response comes back, so the caller can record delivery before it reads
 * the picture bytes (`response.arrayBuffer()`).
 *
 * With `cache` ({@link StepCache}), the key is looked up before the send and the result is a {@link CachedResponse}:
 * a stored refusal comes back rebuilt (`replayed: true`, nothing sent), else the response with its body passing through
 * a bounded copy. The kept headers are `content-type` and `x-venice-is-content-violation`, so a refusal Venice states in
 * its header can be stored with `settle("refusal", {headersOnly: true})`. A success is never stored.
 *
 * @param call - The endpoint, key, body and base URL.
 * @param options - The transport, signal and cache.
 * @returns The untouched response of any status; with `cache`, the {@link CachedResponse}.
 * @throws The transport's error (no answer, or the caller's abort), unchanged. With `cache`: {@link ReplyCacheError}
 *   when the store cannot be read, and `TypeError` before any send for a URL outside the cache's hosts.
 */
export function postVenice(call: VeniceCall, options: MediaOptions & { readonly cache: StepCache }): Promise<CachedResponse>;
export function postVenice(call: VeniceCall, options?: MediaOptions & { readonly cache?: undefined }): Promise<Response>;
export async function postVenice(call: VeniceCall, options: MediaOptions & { readonly cache?: StepCache | undefined } = {}): Promise<Response | CachedResponse> {
  const url = `${call.baseUrl ?? VENICE_URL}/image/${call.endpoint}`;
  const headers = { Authorization: `Bearer ${call.key}`, "Content-Type": "application/json" };
  const body = JSON.stringify(call.body);
  const { cache } = options;
  const model = modelOf(body, call.endpoint);
  const raw = async () => {
    const flight = sendPaid(options, "postVenice", model, url, { method: "POST", headers, signal: options.signal, body });
    return tapResponse(await flight.response, flight.rec, options.signal);
  };
  if (!cache) return raw();
  checkCached(cache, url, options.fetch);
  const { step, entry } = await openStep(cache, "postVenice", { method: "POST", url, headers, body }, modelOf(body), [VENICE_VIOLATION]);
  if (entry) {
    recordReplay(specOf(options, "postVenice", "POST", url, model), replayInfo(entry.status));
    return replayedResponse(entry);
  }
  return capturedResponse(step, await raw());
}

const VENICE_VIOLATION = "x-venice-is-content-violation";

/**
 * Whether Venice flagged the answer as a content violation (its `x-venice-is-content-violation: true` header).
 *
 * @param response - A response of {@link postVenice}.
 * @returns True when the header says so.
 * @throws Never.
 */
export function isVeniceViolation(response: Response): boolean {
  return response.headers.get(VENICE_VIOLATION) === "true";
}

/** One Ideogram precise edit for {@link ideogramEdit}. */
export interface IdeogramCall {
  /** Sent as the `Api-Key` header, the only header. */
  readonly key: string;
  /** The edit model, the last part of the path (`ideogram-4-5`, ...). */
  readonly model: string;
  /** The multipart form the caller built (Ideogram's fields: `prompt`, `image`, ...). */
  readonly form: FormData;
  /** The API base; {@link IDEOGRAM_URL} when not given. */
  readonly baseUrl?: string;
}

/** The answer of {@link ideogramEdit}. */
export type IdeogramAnswer =
  | {
    readonly ok: true;
    readonly status: number;
    /** The first result's `url` as the host sent it, or `null`. */
    readonly url: unknown;
    /** False only when the host marked the result `is_image_safe: false`. */
    readonly safe: boolean;
    /** True for a stored answer of the reply cache: nothing was sent. */
    readonly replayed: boolean;
  }
  | {
    readonly ok: false;
    readonly status: number;
    /** The host's `reject_reason`, or `null`. */
    readonly reason: string | null;
    /** True for a stored answer of the reply cache: nothing was sent. */
    readonly replayed: boolean;
  };

/**
 * One POST of the caller's form to `<base>/v2/image/precise-edit/<model>`.
 *
 * With `cache` ({@link ParsedStepCache}), the key (over the form's fields and file bytes, never the boundary) is looked
 * up before the send. Only a stored refusal replays (`replayed: true`, nothing sent). On a miss, `classify` reads the
 * result of a complete answer that is not busy and whose body parsed as JSON; a `refusal` is stored.
 *
 * @param call - The key, model, form and base URL.
 * @param options - The transport, signal and cache.
 * @returns The first result, or the refusal with its status and reject reason. An error body that is not JSON gives
 *   reason `null`.
 * @throws The transport's error (no answer, or the caller's abort), and the parse error of a 2xx body that is not JSON.
 *   With `cache`: {@link ReplyCacheError} when the store fails, and `TypeError` before any send for a URL outside the
 *   cache's hosts.
 */
export async function ideogramEdit(call: IdeogramCall, options: MediaOptions & { readonly cache?: ParsedStepCache<IdeogramAnswer> | undefined } = {}): Promise<IdeogramAnswer> {
  const url = `${call.baseUrl ?? IDEOGRAM_URL}/v2/image/precise-edit/${call.model}`;
  const headers = { "Api-Key": call.key };
  const { cache } = options;
  let opened: Awaited<ReturnType<typeof openStep>> | null = null;
  if (cache) {
    checkCached(cache, url, options.fetch);
    opened = await openStep(cache, "ideogramEdit", { method: "POST", url, headers, body: call.form }, call.model);
    if (opened.entry) {
      recordReplay(specOf(options, "ideogramEdit", "POST", url, call.model), replayInfo(opened.entry.status));
      const parsed = parseJson(entryBytes(opened.entry));
      if (!parsed.ok && opened.entry.status >= 200 && opened.entry.status < 300) throw new SyntaxError("The stored answer is not JSON.");
      return ideogramAnswer(opened.entry.status, parsed.ok ? parsed.value : {}, true);
    }
  }
  const flight = sendPaid(options, "ideogramEdit", call.model, url, { method: "POST", headers, body: call.form, signal: options.signal });
  const response = await flight.response;
  if (!opened) {
    if (!response.ok) {
      const answer = ideogramAnswer(response.status, await response.json().catch(() => ({})), false);
      endParsed(flight.rec, response.status, true);
      return answer;
    }
    const answer = ideogramAnswer(response.status, await response.json().catch((error: unknown) => { endParsed(flight.rec, response.status, false); throw error; }), false);
    endParsed(flight.rec, response.status, true);
    return answer;
  }
  const read = new Uint8Array(await response.arrayBuffer());
  const parsed = parseJson(read);
  if (!parsed.ok && response.ok) {
    endParsed(flight.rec, response.status, false);
    throw new SyntaxError("The answer is not JSON.");
  }
  const answer = ideogramAnswer(response.status, parsed.ok ? parsed.value : {}, false);
  endParsed(flight.rec, response.status, true);
  if (parsed.ok) {
    const context = { url, model: opened.step.model, transport: "ideogramEdit" };
    await commitStep(opened.step, cache!.classify(answer, context), { status: response.status, headers: response.headers, bytes: read });
  }
  return answer;
}

/** The answer of a status and its parsed body. */
function ideogramAnswer(status: number, value: any, replayed: boolean): IdeogramAnswer {
  if (status < 200 || status > 299) return { ok: false, status, reason: typeof value?.reject_reason === "string" ? value.reject_reason : null, replayed };
  const result = value?.data?.[0];
  return { ok: true, status, url: result?.url ?? null, safe: result?.is_image_safe !== false, replayed };
}

/** One synchronous fal run for {@link postFal}. */
export interface FalCall {
  /** The fal endpoint id (`fal-ai/...`). */
  readonly endpoint: string;
  /** Sent as `Authorization: Key <key>`. */
  readonly key: string;
  /** The endpoint input, sent as `JSON.stringify(input)`. */
  readonly input: unknown;
  /** The run base; {@link FAL_RUN_URL} when not given. */
  readonly baseUrl?: string;
}

/**
 * One POST to `<base>/<endpoint>` on fal's synchronous runs. fal's answers differ per endpoint (an audio link, a voice
 * id, an error list), so the raw response comes back.
 *
 * @param call - The endpoint, key, input and base URL.
 * @param options - The transport and signal.
 * @returns The untouched response of any status.
 * @throws The transport's error (no answer, or the caller's abort), unchanged.
 */
export async function postFal(call: FalCall, options: MediaOptions = {}): Promise<Response> {
  const url = `${call.baseUrl ?? FAL_RUN_URL}/${call.endpoint}`;
  const flight = sendPaid(options, "postFal", call.endpoint, url, {
    method: "POST", headers: { Authorization: `Key ${call.key}`, "Content-Type": "application/json" },
    body: JSON.stringify(call.input), signal: options.signal,
  });
  return tapResponse(await flight.response, flight.rec, options.signal);
}

/** One ElevenLabs request for {@link elevenLabsRequest}. */
export interface ElevenLabsCall {
  /** Sent as the `xi-api-key` header. */
  readonly key: string;
  /** The HTTP method; `POST` when not given. */
  readonly method?: string;
  /** A JSON body, sent as `JSON.stringify(json)` with `Content-Type: application/json`. */
  readonly json?: unknown;
  /** A multipart body (used when `json` is not given). */
  readonly form?: FormData;
  /** The `Accept` header (`audio/mpeg`, `application/json`), when given. */
  readonly accept?: string;
  /** The API base; {@link ELEVENLABS_URL} when not given. */
  readonly baseUrl?: string;
}

/**
 * One request to `<base><path>` on ElevenLabs (speech, sound, music, voice design, cloning, deletes). Headers in this
 * order: `xi-api-key`, `Content-Type` (JSON only), `Accept` (when given).
 *
 * @param path - The path and query, from `/` (`/v1/text-to-speech/<voice>?output_format=mp3_44100_128`).
 * @param call - The key, method, body and Accept header.
 * @param options - The transport and signal.
 * @returns The untouched response of any status.
 * @throws The transport's error (no answer, or the caller's abort), unchanged.
 */
export async function elevenLabsRequest(path: string, call: ElevenLabsCall, options: MediaOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { "xi-api-key": call.key };
  if (call.json !== undefined) headers["Content-Type"] = "application/json";
  if (call.accept) headers.Accept = call.accept;
  const init: Record<string, unknown> = { method: call.method ?? "POST", headers };
  if (call.json !== undefined) init.body = JSON.stringify(call.json);
  else if (call.form) init.body = call.form;
  init.signal = options.signal;
  const url = `${call.baseUrl ?? ELEVENLABS_URL}${path}`;
  // A read or a delete costs nothing: it is not recorded, and it runs outside any call.
  if (/^(GET|HEAD|DELETE)$/i.test(String(init.method))) return send(options, url, init);
  const model = (call.json as { model_id?: unknown } | null | undefined)?.model_id;
  const flight = sendPaid(options, "elevenLabsRequest", typeof model === "string" ? model : "", url, init);
  return tapResponse(await flight.response, flight.rec, options.signal);
}

/**
 * A short, safe reason from an ElevenLabs error body: `{detail: {status | code | type, message}}`, a string `detail`, or
 * a list of validation errors. The code is cut to 60 characters, lowercased and cleaned for logs; the detail has no
 * newlines and at most 200 characters. It never holds the request text.
 *
 * @param body - The parsed error body, or anything else.
 * @returns The code and the detail, each `null` when the body has none.
 * @throws Never.
 */
export function elevenLabsError(body: unknown): { code: string | null; detail: string | null } {
  const d = (body as { detail?: any } | null | undefined)?.detail;
  const first = Array.isArray(d) ? d[0] : null;
  const raw = [d?.status, d?.code, d?.type, first?.type].find((value) => typeof value === "string" && value) as string | undefined;
  const message = [d?.message, typeof d === "string" ? d : null, first?.msg].find((value) => typeof value === "string" && value) as string | undefined;
  return {
    code: raw ? raw.slice(0, 60).toLowerCase().replace(/[^a-z0-9_.-]/g, "_") : null,
    detail: message ? message.replace(/[\r\n]+/g, " ").slice(0, 200) : null,
  };
}

/** The options of {@link fetchFile}. */
export interface FileOptions extends MediaOptions {
  /** The largest file to accept, in bytes. With it, a declared `content-length` over the limit is refused unread
   * (`too_large`), and a body read over the limit is `over_limit`. No limit when not given. */
  readonly maxBytes?: number;
}

/** The result of {@link fetchFile}. */
export type FileResult =
  | { readonly ok: true; readonly status: number; readonly bytes: Uint8Array }
  | {
    readonly ok: false;
    /** The HTTP status, or `null` when there was no answer. */
    readonly status: number | null;
    readonly reason: "network" | "http" | "too_large" | "over_limit" | "empty";
  };

/**
 * Downloads a result link (a picture, an audio file, a video) with no key: the request's init is `{signal}` only.
 *
 * @param url - The link the host returned.
 * @param options - The transport, signal and size limit.
 * @returns The bytes, or why there are none: `network` (no answer), `http` (not 2xx), `too_large` (declared length over
 *   `maxBytes`), `over_limit` (read length over `maxBytes`), `empty` (no bytes).
 * @throws The transport's error when the caller's `signal` was aborted, and the error of a body read that fails.
 */
export async function fetchFile(url: string, options: FileOptions = {}): Promise<FileResult> {
  const { signal, maxBytes } = options;
  let response: Response;
  try {
    response = await send(options, url, { signal });
  } catch (error) {
    if (signal?.aborted) throw error;
    return { ok: false, status: null, reason: "network" };
  }
  if (!response.ok) return { ok: false, status: response.status, reason: "http" };
  if (maxBytes !== undefined && Number(response.headers.get("content-length") ?? 0) > maxBytes) {
    return { ok: false, status: response.status, reason: "too_large" };
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (!bytes.length) return { ok: false, status: response.status, reason: "empty" };
  if (maxBytes !== undefined && bytes.length > maxBytes) return { ok: false, status: response.status, reason: "over_limit" };
  return { ok: true, status: response.status, bytes };
}
