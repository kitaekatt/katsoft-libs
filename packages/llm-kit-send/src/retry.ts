// Retries while the host is busy: the waits with jitter and Retry-After, an abortable pause, and the one-shot call
// with retries, one deadline and one call record per try.
import { usageCost } from "./cost.js";
import { SendError, clip } from "./errors.js";
import { ReplyCacheError, checkCached, commitStep, entryBytes, modelOf, openStep, parseJson } from "./reply-cache.js";
import type { ParsedStepCache } from "./reply-cache.js";
import { postChat } from "./send.js";
import type { ChatCall } from "./send.js";

/**
 * True for a status worth another try on the same host: 429 or any 5xx.
 *
 * @param status - The HTTP status.
 * @returns Whether the host was busy or failed on its side.
 * @throws Never.
 */
export function isTransient(status: number): boolean {
  return status === 429 || status >= 500;
}

/**
 * Waits `ms` milliseconds, or less when `signal` aborts.
 *
 * @param ms - The wait.
 * @param signal - Ends the wait early.
 * @returns A promise that resolves after the wait.
 * @throws The signal's abort reason (the promise rejects) when it aborts first.
 */
export function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((done, stop) => {
    if (signal?.aborted) return stop(signal.reason);
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", () => { clearTimeout(timer); stop(signal.reason); }, { once: true });
  });
}

/** The options of {@link retryWait}. */
export interface RetryWaitOptions {
  /** The most the extra waits of one call may add up to, in milliseconds. */
  readonly budget: number;
  /** A number from 0 to 1 for the jitter; `Math.random` when not given. */
  readonly random?: () => number;
}

/**
 * The wait before the next try: the base wait with +/-30% jitter, or the host's Retry-After (seconds) when that is
 * longer, never past what is left of the budget.
 *
 * @param base - The base wait, in milliseconds.
 * @param response - The last response, whose `retry-after` header counts; `null` when there was none.
 * @param spent - The milliseconds of waiting already spent.
 * @param options - The budget and the random source.
 * @returns The wait in whole milliseconds, 0 or more.
 * @throws Never.
 */
export function retryWait(base: number, response: Response | null, spent: number, options: RetryWaitOptions): number {
  const { budget, random = Math.random } = options;
  const asked = Number(response?.headers?.get("retry-after")) * 1000;
  const wait = Math.max(base * (0.7 + 0.6 * random()), Number.isFinite(asked) && asked > 0 ? asked : 0);
  return Math.round(Math.max(0, Math.min(wait, budget - spent)));
}

/** One sent try of {@link sendWithRetries}, or one replayed answer: tokens, cost and time; never prompt or reply text.
 * Format `llm-kit.call-record/v2` (v1 plus `replayed`). */
export interface CallRecord {
  /** The try number, from 1. */
  readonly attempt: number;
  /** The model the provider reported, else the body's model. */
  readonly model: string;
  /** The host that served the try, when the provider named it, or `null`. */
  readonly provider: string | null;
  /** True when the try gave a parsed 2xx answer. */
  readonly ok: boolean;
  /** The HTTP status, or `null` when there was no response. */
  readonly status: number | null;
  /** True when the caller's signal or the deadline cut the try after it was sent. */
  readonly aborted: boolean;
  /** The try's time, in milliseconds. */
  readonly ms: number;
  readonly inputTokens: number | null;
  readonly cachedTokens: number | null;
  readonly outputTokens: number | null;
  /** The cost the host reported ({@link usageCost}), or `null`. */
  readonly cost: number | null;
  /** The provider's error message, clipped to 200 characters, or `null`. */
  readonly providerMessage: string | null;
  /** True for a stored answer of the reply cache: nothing was sent, tokens are `null` and the cost is 0. */
  readonly replayed: boolean;
}

/** The options of {@link sendWithRetries}. */
export interface RetryOptions {
  /** The waits between tries, in milliseconds; `[]` is exactly one try. */
  readonly waits: readonly number[];
  /** The most the waits of one call may add up to (see {@link retryWait}). */
  readonly budgetMs: number;
  /** One deadline for all tries, waits and body reads, from the call; never restarted per try. */
  readonly deadlineMs?: number;
  /** The caller's signal; its abort rejects the call with its reason and is never retried. */
  readonly signal?: AbortSignal;
  /** The transport; the global `fetch` at call time when not given. */
  readonly fetch?: typeof fetch;
  /** A check before sending (see {@link postChat}). */
  readonly beforeSend?: (url: string, bytes: string) => void;
  /** One record per try that was sent. It must not throw: the package does not catch it. */
  readonly onCall?: (record: CallRecord) => void;
  /** A number from 0 to 1 for the jitter. */
  readonly random?: () => number;
  /** The reply cache: one lookup before the first try; see {@link sendWithRetries}. */
  readonly cache?: ParsedStepCache<RetryResult> | undefined;
}

/** The result of {@link sendWithRetries}. `replayed` is true for a stored answer of the reply cache. */
export type RetryResult =
  | { readonly ok: true; readonly payload: any; readonly text: string; readonly model: string; readonly ms: number; readonly replayed: boolean }
  /** `status` is `null` when no answer came (network, or the deadline: `body` is then `{deadline: true}`). */
  | { readonly ok: false; readonly status: number | null; readonly body: any; readonly replayed: boolean };

const DEADLINE = Symbol("deadline");

const textOf = (payload: any): string => {
  const content = payload?.choices?.[0]?.message?.content;
  return typeof content === "string" ? content : Array.isArray(content) ? content.map((part: any) => (typeof part?.text === "string" ? part.text : "")).join("") : "";
};

/** `work`, or the signal's abort reason when the signal fires first. */
const within = <T>(work: Promise<T>, signal: AbortSignal) => new Promise<T>((done, fail) => {
  if (signal.aborted) return fail(signal.reason);
  const onAbort = () => fail(signal.reason);
  signal.addEventListener("abort", onAbort, { once: true });
  work.then(done, fail).finally(() => signal.removeEventListener("abort", onAbort));
});

/**
 * One plain chat call with retries. A busy answer (429, 5xx) or no answer at all is tried again after each of `waits`
 * (with jitter and the host's Retry-After, {@link retryWait}); any other status ends it at once. It never throws on an
 * HTTP status. Each try that was sent gives one {@link CallRecord} to `onCall`, also one cut by an abort or the deadline;
 * a call that `beforeSend` refuses gives none.
 *
 * With `cache` ({@link ParsedStepCache}), the key is looked up once, after `beforeSend` and before the first try. A
 * stored refusal, or a stored success of a deterministic request (`temperature` 0), returns at once with
 * `replayed: true` and one record (`replayed: true`, cost 0, tokens `null`). On a miss, only the try that ended the loop
 * can be stored: a complete answer that is not busy (408, 429, 5xx) and whose body parsed as JSON; `classify` reads the
 * result and its verdict is stored.
 *
 * @param call - The URL, key, headers and body (a plain request; the answer is read as JSON).
 * @param options - The waits, budget, deadline, signal, transport, check, record sink and cache.
 * @returns `{ok: true, payload, text, model, ms, replayed}` for the first parsed 2xx answer; else
 *   `{ok: false, status, body, replayed}` with the last status and parsed error body, or
 *   `{ok: false, status: null, body: {deadline: true}, replayed: false}` at the deadline.
 * @throws The caller signal's abort reason when it aborts; an exception of `beforeSend` unchanged. With `cache`:
 *   {@link ReplyCacheError} when the store fails (never retried, never the deadline result), and `TypeError` before any
 *   send for a URL outside the cache's hosts or a {@link withBackupKey} fetch.
 */
export async function sendWithRetries(call: ChatCall, options: RetryOptions): Promise<RetryResult> {
  const { waits, signal, deadlineMs, onCall, random, cache } = options;
  if (cache) checkCached(cache, call.url, options.fetch);
  const stop = new AbortController();
  const timer = deadlineMs === undefined ? null : setTimeout(() => stop.abort(DEADLINE), deadlineMs);
  const joined = AbortSignal.any(signal ? [signal, stop.signal] : [stop.signal]);
  const bodyModel = String(call.body.model ?? "");
  const record = (attempt: number, started: number, ok: boolean, payload: any, status: number | null, aborted: boolean, failure: any, replayed = false) => {
    const usage = replayed ? null : payload?.usage;
    onCall?.({
      attempt, model: typeof payload?.model === "string" ? payload.model : bodyModel,
      provider: typeof payload?.provider === "string" ? payload.provider : null, ok, status, aborted, ms: Date.now() - started,
      inputTokens: usage?.prompt_tokens ?? null, cachedTokens: usage?.prompt_tokens_details?.cached_tokens ?? null,
      outputTokens: usage?.completion_tokens ?? null, cost: replayed ? 0 : usageCost(usage), providerMessage: clip(failure?.error?.message), replayed,
    });
  };
  let checked = false;
  const send = async (): Promise<Response | null> => {
    try {
      return await postChat(call, {
        signal: joined,
        ...(options.fetch ? { fetch: options.fetch } : {}),
        // With a cache, `beforeSend` ran before the lookup.
        ...(options.beforeSend && !checked ? { beforeSend: options.beforeSend } : {}),
      });
    } catch (error) {
      if (!(error instanceof SendError)) throw error;
      if (joined.aborted) throw error.cause;
      return null;
    }
  };
  try {
    let opened: Awaited<ReturnType<typeof openStep>> | null = null;
    if (cache) {
      const bytes = (call.encode ?? JSON.stringify)(call.body);
      options.beforeSend?.(call.url, bytes);
      checked = true;
      opened = await openStep(cache, "sendWithRetries", { method: "POST", url: call.url, headers: { "Content-Type": "application/json", ...call.headers }, body: bytes }, modelOf(bytes, bodyModel));
      if (opened.entry) {
        const parsed = parseJson(entryBytes(opened.entry));
        const value = parsed.ok ? parsed.value : null;
        const ok = opened.entry.status >= 200 && opened.entry.status < 300 && Boolean(value);
        record(1, opened.step.started, ok, ok ? value : null, opened.entry.status, false, ok ? null : value, true);
        return ok
          ? { ok: true, payload: value, text: textOf(value), model: typeof value.model === "string" ? value.model : bodyModel, ms: Date.now() - opened.step.started, replayed: true }
          : { ok: false, status: opened.entry.status, body: value, replayed: true };
      }
    }
    let response: Response | null = null, spent = 0, result: RetryResult = { ok: false, status: null, body: null, replayed: false };
    let last: { bytes: Uint8Array; parsed: boolean } | null = null;
    for (let attempt = 0; attempt <= waits.length; attempt++) {
      if (attempt) {
        const ms = retryWait(waits[attempt - 1]!, response, spent, { budget: options.budgetMs, ...(random ? { random } : {}) });
        spent += ms;
        await pause(ms, joined);
      }
      const started = Date.now();
      let payload: any = null, failure: any = null;
      last = null;
      try {
        response = await within(send(), joined);
        if (response) {
          const bytes = await within(response.arrayBuffer().then((buffer) => new Uint8Array(buffer), () => null), joined);
          const parsed = bytes ? parseJson(bytes) : { ok: false as const };
          if (bytes) last = { bytes, parsed: parsed.ok };
          const value = parsed.ok ? parsed.value : null;
          if (response.ok) payload = value;
          else failure = value;
        }
      } catch (error) {
        // A request that was sent and then cut off by an abort or the deadline still counts: the host may bill it.
        if (joined.aborted) record(attempt + 1, started, false, null, null, true, null);
        throw error;
      }
      const ok = Boolean(response?.ok && payload);
      record(attempt + 1, started, ok, payload, response?.status ?? null, false, failure);
      if (ok) {
        result = { ok: true, payload, text: textOf(payload), model: typeof payload.model === "string" ? payload.model : bodyModel, ms: Date.now() - started, replayed: false };
        break;
      }
      result = { ok: false, status: response?.status ?? null, body: response?.ok ? null : failure, replayed: false };
      if (response && !isTransient(response.status)) break;
    }
    // Only the try that ended the loop is stored, when it is a complete answer whose body parsed.
    if (opened && response && last?.parsed) {
      const context = { url: call.url, model: opened.step.model, transport: "sendWithRetries" };
      await commitStep(opened.step, cache!.classify(result, context), { status: response.status, headers: response.headers, bytes: last.bytes });
    }
    return result;
  } catch (error) {
    if (error instanceof ReplyCacheError) throw error;
    if (signal?.aborted) throw error;
    if (stop.signal.aborted) return { ok: false, status: null, body: { deadline: true }, replayed: false };
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
}
