/** The ways a chat call can fail. See {@link SendError}. */
export type SendErrorKind =
  | "http"
  | "network"
  | "body"
  | "no_first_token"
  | "stalled"
  | "slow"
  | "stream_error"
  | "incomplete"
  | "aborted";

/** The fields a {@link SendError} carries besides its kind and message. */
export interface SendErrorFields {
  /** The HTTP status of the response, or `null` when there was none. */
  readonly status?: number | null;
  /** The provider's error code, or a stream code such as `finish_reason_error`. */
  readonly code?: unknown;
  /** The provider's own error message, clipped to 200 characters, or `null`. */
  readonly providerMessage?: string | null;
  /** The host that served the call, when the provider named it, or `null`. */
  readonly provider?: string | null;
  /** The upstream host's short reason (see {@link upstreamReason}), or `null`. */
  readonly reason?: string | null;
  /** The parsed error body of an HTTP failure, or `null`. */
  readonly payload?: unknown;
  /** The original error: the transport error, the body read error, or the callback error. */
  readonly cause?: unknown;
  /** True when the error is a stored answer of the reply cache: nothing was sent. */
  readonly replayed?: boolean;
  /** The id of the call in the call ledger (`callId` of {@link SendError}), or `null`. */
  readonly callId?: string | null;
  /** The provider's id of the generation, when a chunk or the answer named it, or `null`. */
  readonly generationId?: string | null;
}

const LIMIT_KINDS: ReadonlySet<SendErrorKind> = new Set(["no_first_token", "stalled", "slow"]);

/** A status that is the provider's fault, not the request's: worth one try on another model. A 400 with
 * OpenRouter's wrapper message "Provider returned error" is the upstream host's rejection, not the router's own check. */
function providerSide(status: number, message: string | null): boolean {
  return status >= 500 || status === 404 || status === 408 || status === 429 || (status === 400 && message === "Provider returned error");
}

/**
 * The error of a failed chat call. It never holds prompt or reply text.
 *
 * `kind` says what failed: `http` (a non-2xx status), `network` (no response; `cause` is the transport error), `body`
 * (the response came, but reading it failed: bad JSON, a dropped stream, no body, or an error thrown by `onDelta` or
 * `onToolArguments`; `cause` is the original error), `no_first_token`, `stalled` and `slow` (a stream limit; `name` is
 * `TimeoutError`), `stream_error` (an `error` chunk, or the finish reason `error`), `incomplete` (a stream that ended with
 * no finish reason) and `aborted` (the caller's signal). `fallback` is true when the failure is the provider's side and
 * one try on another model is worth it: a 5xx, 404, 408 or 429, a 400 "Provider returned error", and every kind but
 * `http` and `aborted`.
 *
 * @param kind - What failed.
 * @param message - A short description for logs; never prompt text.
 * @param fields - The status, provider code and message, host, upstream reason, error body, cause, `replayed`, `callId`
 *   and `generationId`.
 * @returns A new error.
 * @throws Never.
 */
export class SendError extends Error {
  /** What failed. */
  readonly kind: SendErrorKind;
  /** The HTTP status of the response, or `null`. */
  readonly status: number | null;
  /** The provider's error code, a stream code, or `null`. */
  readonly code: unknown;
  /** The provider's own message, clipped to 200 characters, or `null`. */
  readonly providerMessage: string | null;
  /** The host that served the call, or `null`. */
  readonly provider: string | null;
  /** The upstream host's short reason, or `null`. */
  readonly reason: string | null;
  /** The parsed error body of an HTTP failure, or `null`. */
  readonly payload: unknown;
  /** True when one try on another model is worth it. */
  readonly fallback: boolean;
  /** True when the error is a stored answer of the reply cache: nothing was sent. */
  readonly replayed: boolean;
  /** The id of the call in the call ledger (the `id` of its `begin` and `end` records), or `null` when the error came
   * before any call (for example a refused `beforeSend`). */
  readonly callId: string | null;
  /** The provider's id of the generation (the `id` of the stream chunks or of the answer), or `null`: the key to look
   * up what a stalled or cut call cost. */
  readonly generationId: string | null;

  constructor(kind: SendErrorKind, message: string, fields: SendErrorFields = {}) {
    super(message, fields.cause === undefined ? undefined : { cause: fields.cause });
    this.name = LIMIT_KINDS.has(kind) ? "TimeoutError" : "SendError";
    this.kind = kind;
    this.status = fields.status ?? null;
    this.code = fields.code ?? null;
    this.providerMessage = fields.providerMessage ?? null;
    this.provider = fields.provider ?? null;
    this.reason = fields.reason ?? null;
    this.payload = fields.payload ?? null;
    this.fallback = kind === "http" ? providerSide(this.status ?? 0, this.providerMessage) : kind !== "aborted";
    this.replayed = fields.replayed ?? false;
    this.callId = fields.callId ?? null;
    this.generationId = fields.generationId ?? null;
  }
}

/** Sets the call id and the generation id of an error that is raised before the transport knows them; a value already
 * there stays. Returns the same error. */
export function stamp<E extends Error>(error: E, ids: { readonly callId?: string | null; readonly generationId?: string | null }): E {
  if (!(error instanceof SendError)) return error;
  const own = error as { callId: string | null; generationId: string | null };
  if (own.callId === null && ids.callId) own.callId = ids.callId;
  if (own.generationId === null && ids.generationId) own.generationId = ids.generationId;
  return error;
}

/** A string clipped to 200 characters, or `null` for anything else. */
export const clip = (value: unknown): string | null => (typeof value === "string" ? value.slice(0, 200) : null);

/**
 * The upstream host's own reason for a failure, from OpenRouter's `error.metadata.raw` (a filter verdict, a length
 * limit): its error type or code, and its message only when that is one short line, so an echoed prompt never reaches a
 * log.
 *
 * @param raw - The raw upstream error: a JSON string or an object.
 * @returns `"<type or code>: <message>"`, one of the two, or `null` when there is neither.
 * @throws Never; a string that is not JSON gives `null`.
 */
export function upstreamReason(raw: unknown): string | null {
  let body: any = raw;
  if (typeof raw === "string") {
    try { body = JSON.parse(raw); } catch { return null; }
  }
  const error = body?.error ?? body;
  if (!error || typeof error !== "object") return null;
  const kind = [error.type, error.code].find((value) => typeof value === "string" && value.length <= 60);
  const message = typeof error.message === "string" && error.message.length <= 120 && !/\n/.test(error.message) ? error.message : null;
  return kind || message ? [kind, message].filter(Boolean).join(": ") : null;
}

/** The {@link SendError} of a non-2xx response: its status, the provider's code, message, host and reason. */
export async function httpError(response: Response): Promise<SendError> {
  let payload: any = null;
  try { payload = JSON.parse(await response.text()); } catch { /* not JSON */ }
  return httpErrorOf(response.status, payload);
}

/** The {@link SendError} of a non-2xx status and its parsed error body (`null` when it was not JSON). */
export function httpErrorOf(status: number, payload: any, replayed = false): SendError {
  const error = payload?.error;
  return new SendError("http", `The provider answered HTTP ${status}.`, {
    status,
    code: error?.code ?? null,
    providerMessage: clip(error?.message),
    provider: clip(error?.metadata?.provider_name),
    reason: upstreamReason(error?.metadata?.raw),
    payload,
    ...(replayed ? { replayed } : {}),
  });
}
