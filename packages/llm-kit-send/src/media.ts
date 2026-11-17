// Picture, voice and file transports: one request to one media host each, sent as given. Each returns what the caller
// needs for its own record (status, delivery, reported cost) or the raw response, so the caller decides what an answer
// means. No retry and no fallback here: a chain of hosts is `runChain` (chain.ts).
import { usageCost } from "./cost.js";

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
}

/** Sends `init` as built: its key order and the given signal stay as they are (`signal` may be undefined). */
const send = (options: MediaOptions, url: string, init: Record<string, unknown>): Promise<Response> =>
  (options.fetch ?? globalThis.fetch)(url, init as RequestInit);

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
    /** The cost the host reported ({@link usageCost}), or `null`. */
    readonly cost: number | null;
  }
  | {
    readonly ok: false;
    /** The HTTP status, or `null` when there was no answer. */
    readonly status: number | null;
    /** The parsed error body, or `{}`. */
    readonly failure: any;
  };

/**
 * One POST to an OpenAI-compatible images endpoint (`/images`, `/images/generations`). The body goes out as given.
 * A 2xx answer always counts as answered: the caller decides whether a picture came (`image`, `link`).
 *
 * @param call - The URL, key, extra headers and body.
 * @param options - The transport and signal.
 * @returns The parsed answer, or the failure with its status (`null` with no answer). A body that is not JSON, or one
 *   whose read fails, reads as `{}`.
 * @throws The transport's error when the caller's `signal` was aborted; nothing else.
 */
export async function postImages(call: ImagesCall, options: MediaOptions = {}): Promise<ImagesAnswer> {
  const { signal } = options;
  let response: Response;
  try {
    response = await send(options, call.url, {
      method: "POST", signal,
      headers: { Authorization: `Bearer ${call.key}`, "Content-Type": "application/json", ...call.headers },
      body: JSON.stringify(call.body),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    return { ok: false, status: null, failure: {} };
  }
  if (!response.ok) return { ok: false, status: response.status, failure: await response.json().catch(() => ({})) };
  const payload: any = await response.json().catch(() => ({}));
  const first = payload?.data?.[0];
  return {
    ok: true, status: response.status, payload, image: text(first?.b64_json), link: text(first?.url),
    usage: payload?.usage ?? null, cost: usageCost(payload?.usage),
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
 * @param call - The endpoint, key, body and base URL.
 * @param options - The transport and signal.
 * @returns The untouched response of any status.
 * @throws The transport's error (no answer, or the caller's abort), unchanged.
 */
export function postVenice(call: VeniceCall, options: MediaOptions = {}): Promise<Response> {
  return send(options, `${call.baseUrl ?? VENICE_URL}/image/${call.endpoint}`, {
    method: "POST", headers: { Authorization: `Bearer ${call.key}`, "Content-Type": "application/json" },
    signal: options.signal, body: JSON.stringify(call.body),
  });
}

/**
 * Whether Venice flagged the answer as a content violation (its `x-venice-is-content-violation: true` header).
 *
 * @param response - A response of {@link postVenice}.
 * @returns True when the header says so.
 * @throws Never.
 */
export function isVeniceViolation(response: Response): boolean {
  return response.headers.get("x-venice-is-content-violation") === "true";
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
  }
  | {
    readonly ok: false;
    readonly status: number;
    /** The host's `reject_reason`, or `null`. */
    readonly reason: string | null;
  };

/**
 * One POST of the caller's form to `<base>/v2/image/precise-edit/<model>`.
 *
 * @param call - The key, model, form and base URL.
 * @param options - The transport and signal.
 * @returns The first result, or the refusal with its status and reject reason. An error body that is not JSON gives
 *   reason `null`.
 * @throws The transport's error (no answer, or the caller's abort), and the parse error of a 2xx body that is not JSON.
 */
export async function ideogramEdit(call: IdeogramCall, options: MediaOptions = {}): Promise<IdeogramAnswer> {
  const response = await send(options, `${call.baseUrl ?? IDEOGRAM_URL}/v2/image/precise-edit/${call.model}`, {
    method: "POST", headers: { "Api-Key": call.key }, body: call.form, signal: options.signal,
  });
  if (!response.ok) {
    const failure: any = await response.json().catch(() => ({}));
    return { ok: false, status: response.status, reason: typeof failure?.reject_reason === "string" ? failure.reject_reason : null };
  }
  const payload: any = await response.json();
  const result = payload?.data?.[0];
  return { ok: true, status: response.status, url: result?.url ?? null, safe: result?.is_image_safe !== false };
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
export function postFal(call: FalCall, options: MediaOptions = {}): Promise<Response> {
  return send(options, `${call.baseUrl ?? FAL_RUN_URL}/${call.endpoint}`, {
    method: "POST", headers: { Authorization: `Key ${call.key}`, "Content-Type": "application/json" },
    body: JSON.stringify(call.input), signal: options.signal,
  });
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
export function elevenLabsRequest(path: string, call: ElevenLabsCall, options: MediaOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { "xi-api-key": call.key };
  if (call.json !== undefined) headers["Content-Type"] = "application/json";
  if (call.accept) headers.Accept = call.accept;
  const init: Record<string, unknown> = { method: call.method ?? "POST", headers };
  if (call.json !== undefined) init.body = JSON.stringify(call.json);
  else if (call.form) init.body = call.form;
  init.signal = options.signal;
  return send(options, `${call.baseUrl ?? ELEVENLABS_URL}${path}`, init);
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
