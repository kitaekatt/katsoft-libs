// One OpenAI-compatible chat-completions call: the raw POST (postChat) and the parsed, plain or streamed call
// (sendChat) with the stream limits and the reasoning-off retry.
import { SendError, httpError, httpErrorOf, stamp } from "./errors.js";
import { dispatch, recordReplay } from "./ledger.js";
import type { CallHandle, CallLabels, CallSpec, EndInfo } from "./ledger.js";
import { capturedResponse, checkCached, commitStep, deterministic, modelOf, openStep, parseJson, replayResponse, replayedResponse } from "./reply-cache.js";
import type { CachedResponse, ParsedStepCache, StepCache } from "./reply-cache.js";
import { inTime, limitError, readStream } from "./stream.js";
import type { StreamLimits, ToolCall } from "./stream.js";
import { tapResponse } from "./tap.js";

/** One request: where it goes, its key, extra headers and the body object. */
export interface ChatCall {
  /** The chat-completions URL of the endpoint. */
  readonly url: string;
  /** Sent as `Authorization: Bearer <key>`; `null` sends no Authorization header. */
  readonly key: string | null;
  /** Extra headers, such as an app label. */
  readonly headers?: Readonly<Record<string, string>>;
  /** The request object. The package reads `stream`, `model`, `reasoning` and `max_tokens`; it changes only the
   * reasoning-off retry fields. */
  readonly body: Record<string, unknown>;
  /** The wire bytes of the body; `JSON.stringify` when not given. A consumer rewrites the body for its endpoint here. */
  readonly encode?: (body: Record<string, unknown>) => string;
}

/** The options of {@link postChat}. */
export interface PostOptions {
  /** The transport; the global `fetch` at call time when not given. */
  readonly fetch?: typeof fetch;
  /** The signal of the request. */
  readonly signal?: AbortSignal;
  /** A check of the URL and the wire bytes before anything is sent; it may throw to refuse the call. */
  readonly beforeSend?: (url: string, bytes: string) => void;
  /** The labels of the call for the call ledger (see {@link setCallLedger}); the package never reads them. */
  readonly labels?: CallLabels | undefined;
}

/** The reasoning-off retry: see {@link SendOptions.reasoningRetry}. */
export interface ReasoningRetry {
  /** The reasoning token budget of the second try; it is added to `max_tokens`. */
  readonly budget: number;
  /** Called with the body's model before the second try, so the consumer can remember the model. */
  readonly onRequired?: (model: string) => void;
}

/** The options of {@link sendChat}. */
export interface SendOptions extends PostOptions {
  /** The stream limits; the call is watched only when they are given (the body should then ask for a stream). */
  readonly limits?: StreamLimits | null;
  /** Each content delta of a streamed call, as it arrives. With it, the body must be a server-sent event stream. */
  readonly onDelta?: (text: string) => void;
  /** The arguments so far of a streamed tool call, each time they grow. */
  readonly onToolArguments?: (name: string, raw: string) => void;
  /** A stream that ends with no finish reason is the kind `incomplete`. */
  readonly requireFinish?: boolean;
  /** A chunk with the finish reason `error` ends the stream as `stream_error` (default true). */
  readonly stopOnErrorFinish?: boolean;
  /** When the body turns reasoning off (`reasoning.enabled === false` or `reasoning.effort === "none"`) and the host
   * answers 400 "reasoning is mandatory", send once more with `reasoning: {max_tokens: budget}`. Off when not given. */
  readonly reasoningRetry?: ReasoningRetry | null;
}

/** The answer of {@link sendChat}, the same shape for a plain and a streamed call. */
export interface ChatAnswer {
  /** The provider JSON; for a stream, the assembled `{model, provider, usage, choices: [{finish_reason,
   * native_finish_reason, message: {content, tool_calls?}}]}`. */
  readonly payload: any;
  /** The reply text, or `""` when the answer has no text. */
  readonly text: string;
  /** The tool calls as the provider sent them; the caller validates them. */
  readonly toolCalls: readonly ToolCall[];
  /** The usage the provider reported, as sent (the caller validates it), or `null`. */
  readonly usage: any;
  /** The model the provider reported, else the body's model. */
  readonly model: string;
  /** The host that served the call, when the provider named it, or `null`. */
  readonly provider: string | null;
  /** The finish reason, or `null`. */
  readonly finishReason: unknown;
  /** The host's own finish reason, or `null`. */
  readonly nativeFinishReason: unknown;
  /** True when the answer was read from an event stream. */
  readonly streamed: boolean;
  /** The time from the call to the end of the answer, in milliseconds. */
  readonly ms: number;
  /** True when the answer is a stored one (the `cache` option): nothing was sent; `usage` is then `null`. */
  readonly replayed: boolean;
  /** The id of the call in the call ledger: the `id` of the records of the send that answered. */
  readonly callId: string;
  /** The provider's id of the generation (the `id` of the answer, or of the stream chunks), or `null`. */
  readonly generationId: string | null;
}

/**
 * Sends one chat request and returns the provider's response untouched, of any status. `beforeSend` runs first; its
 * exception propagates unchanged and nothing is sent.
 *
 * With `cache` ({@link StepCache}; a plain request only), the key is computed after `beforeSend` and looked up before
 * the send. A hit sends nothing and gives the stored answer rebuilt as a response; a miss gives the response with its
 * body passing through a bounded copy. Either way the result is a {@link CachedResponse}: the caller reads the response,
 * then calls `settle` with its verdict.
 *
 * @param call - The URL, key, headers and body.
 * @param options - The transport, the signal, the check before sending and the cache.
 * @returns The response; with `cache`, the {@link CachedResponse}.
 * Each send is one call in the call ledger ({@link setCallLedger}), named `postChat`: `begin` before the first byte
 * leaves, `end` when the returned body is read to its end, fails or is cancelled (a body that is never read leaves the
 * call open). The response is a passthrough of the transport's: the same status, headers and chunks, read only as the
 * caller reads; a JSON or event-stream body is also read for its usage and generation id, up to 1 MiB. A replay of the
 * cache is a recorded call with `replayed: true`.
 *
 * @throws {SendError} Kind `network` when the transport gave no response (`cause` is its error), or `aborted` when the
 *   signal ended it. {@link CallLedgerError} before anything is sent when the ledger is required and has no sink; `TypeError`
 *   for a bad label. An exception of `beforeSend` or of `encode` propagates unchanged. {@link ReplyCacheError} when the
 *   store cannot be read. `TypeError` before any send for a cache on a URL outside its hosts, a streamed body
 *   (`stream: true`) or a {@link withBackupKey} fetch.
 */
export function postChat(call: ChatCall, options: PostOptions & { readonly cache: StepCache }): Promise<CachedResponse>;
export function postChat(call: ChatCall, options?: PostOptions & { readonly cache?: undefined }): Promise<Response>;
export async function postChat(call: ChatCall, options: PostOptions & { readonly cache?: StepCache | undefined } = {}): Promise<Response | CachedResponse> {
  const { cache } = options;
  if (cache) {
    checkCached(cache, call.url, options.fetch);
    if (call.body.stream === true) throw new TypeError("A cached chat call cannot be a stream.");
  }
  const bytes = checkedBytes(call, options);
  if (!cache) return postRaw(call, bytes, options, 1);
  const { step, entry } = await openStep(cache, "postChat", wireOf(call, bytes), modelOf(bytes));
  if (entry) {
    recordReplay(specOf(call, options, "postChat", 1), { ok: entry.status >= 200 && entry.status < 300, status: entry.status, errorKind: entry.status >= 400 ? "http" : null });
    return replayedResponse(entry);
  }
  return capturedResponse(step, await postRaw(call, bytes, options, 1));
}

/** The wire bytes of the body, after `beforeSend` accepted them. */
function checkedBytes(call: ChatCall, options: PostOptions): string {
  const bytes = (call.encode ?? JSON.stringify)(call.body);
  options.beforeSend?.(call.url, bytes);
  return bytes;
}

const specOf = (call: ChatCall, options: PostOptions, transport: string, attempt: number): CallSpec => ({
  transport, method: "POST", url: call.url, model: String(call.body.model ?? ""), stream: call.body.stream === true, attempt, labels: options.labels,
});

/**
 * One try of {@link postChat} as sendWithRetries makes it: the same raw response, recorded as try number `attempt`. Not
 * part of the public surface.
 */
export async function postChatTry(call: ChatCall, options: PostOptions, attempt: number, transport: string): Promise<Response> {
  return postRaw(call, checkedBytes(call, options), options, attempt, transport);
}

/** The raw POST of checked bytes: the call ends when the response body is read, fails or is cancelled. */
async function postRaw(call: ChatCall, bytes: string, options: PostOptions, attempt: number, transport = "postChat"): Promise<Response> {
  const flight = launch(call, bytes, options, transport, attempt);
  return tapResponse(await flight.response, flight.rec, options.signal);
}

const headersOf = (call: ChatCall): Record<string, string> =>
  ({ ...(call.key == null ? {} : { Authorization: `Bearer ${call.key}` }), "Content-Type": "application/json", ...call.headers });

const wireOf = (call: ChatCall, bytes: string) => ({ method: "POST", url: call.url, headers: headersOf(call), body: bytes });

/** A request on its way: its record in the call ledger, begun before the transport was called, and its response. */
interface Flight {
  readonly rec: CallHandle;
  /** The response; rejects with a {@link SendError} (`network`, or `aborted` with the signal) that has the call id. */
  readonly response: Promise<Response>;
}

/** Begins the call in the ledger, then calls the transport, inside the call (see {@link activeCall}). */
function launch(call: ChatCall, bytes: string, options: PostOptions, transport: string, attempt: number): Flight {
  const { signal } = options;
  const flight = dispatch(specOf(call, options, transport, attempt), options.fetch ?? globalThis.fetch, call.url, {
    ...(signal ? { signal } : {}),
    method: "POST",
    headers: headersOf(call),
    body: bytes,
  }, signal);
  const response = flight.response.catch((error: unknown) => {
    throw stamp(new SendError(signal?.aborted ? "aborted" : "network", "The provider gave no response.", { cause: error }), { callId: flight.rec.id });
  });
  return { rec: flight.rec, response };
}

const reasoningOff = (body: Record<string, unknown>) => {
  const reasoning = body.reasoning as { enabled?: unknown; effort?: unknown } | undefined;
  return reasoning?.enabled === false || reasoning?.effort === "none";
};

/** The wait for the response headers counts against the first output: a stalled provider may send nothing at all. */
async function headersInTime(flight: Flight, ms: number, stop: AbortController): Promise<Response> {
  const response = await inTime(flight.response, ms);
  if (response) return response;
  // Stop the stalled call before the caller moves on, so two models never run and bill at once.
  stop.abort();
  flight.rec.end({ ok: false, errorKind: "no_first_token", aborted: true });
  void flight.response.then((late) => late.body?.cancel()).catch(() => {});
  throw stamp(limitError("no_first_token", null), { callId: flight.rec.id });
}

/**
 * Sends one OpenAI-compatible chat call and reads its answer: a plain JSON answer, or a server-sent event stream with
 * the stream limits, the content deltas and the tool calls put together. A JSON body answered to a streamed request is
 * read as a plain answer when there is no `onDelta`.
 *
 * With `limits`, the wait for the headers counts against the first output, and on a limit the request is aborted
 * before the error is thrown, so two models never run at once. With `reasoningRetry`, a host that refuses to turn
 * reasoning off gets one more request with a bounded budget.
 *
 * With `cache` ({@link ParsedStepCache}; a plain call only: no `stream: true`, `onDelta` or `limits`), the key of the
 * first send is looked up after `beforeSend`. A stored refusal, or a stored success of a deterministic request, is
 * replayed: nothing is sent, the stored answer is read by the same code, `replayed` is true and `usage` null (a stored
 * HTTP failure throws its {@link SendError} with `replayed: true`). On a miss, `classify` reads the answer (or the HTTP
 * error) of a complete answer whose body parsed, and its verdict is stored under the first send's key. The temperature
 * test reads the bytes of the send that answered.
 *
 * @param call - The URL, key, headers and body; see {@link ChatCall}.
 * @param options - The transport, signal, limits, callbacks, retry and cache; see {@link SendOptions}.
 * @returns The answer; see {@link ChatAnswer}.
 * Each HTTP send is one call in the call ledger ({@link setCallLedger}): the reasoning-off retry is a second call
 * (`attempt` 2). The call ends in a `finally` of the read: a stream that stalled or was cut still ends with the chunk id
 * as `generationId`. `ChatAnswer` and `SendError` carry `callId` and `generationId`.
 *
 * @throws {SendError} On every failure: `http`, `network`, `body`, `no_first_token`, `stalled`, `slow`,
 *   `stream_error`, `incomplete` or `aborted`. {@link CallLedgerError} before anything is sent when the ledger is required
 *   and has no sink; `TypeError` for a bad label. An exception of `beforeSend` propagates unchanged. With `cache`:
 *   {@link ReplyCacheError} when the store fails (never a `SendError`), and `TypeError` before any send for a streamed
 *   call, a URL outside the cache's hosts or a {@link withBackupKey} fetch.
 */
export async function sendChat(call: ChatCall, options: SendOptions & { readonly cache?: ParsedStepCache<ChatAnswer | SendError> | undefined } = {}): Promise<ChatAnswer> {
  const started = Date.now();
  const { cache } = options;
  if (cache) {
    if (call.body.stream === true || options.onDelta || options.limits) throw new TypeError("A cached chat call cannot be a stream.");
    checkCached(cache, call.url, options.fetch);
  }
  const limits = options.limits ?? null;
  const stop = limits ? new AbortController() : null;
  const signal = stop ? (options.signal ? AbortSignal.any([stop.signal, options.signal]) : stop.signal) : options.signal;
  const postOptions: PostOptions = {
    ...(options.fetch ? { fetch: options.fetch } : {}),
    ...(signal ? { signal } : {}),
    ...(options.beforeSend ? { beforeSend: options.beforeSend } : {}),
    ...(options.labels ? { labels: options.labels } : {}),
  };
  const post = (checked: string | undefined, attempt: number): Flight => {
    const flight = launch(call, checked ?? checkedBytes(call, postOptions), postOptions, "sendChat", attempt);
    // A limit abort is ours, not the caller's: report it as no response.
    const response = flight.response.catch((error: unknown) => {
      if (error instanceof SendError && error.kind === "aborted" && !options.signal?.aborted) {
        throw stamp(new SendError("network", error.message, { cause: error.cause }), { callId: flight.rec.id });
      }
      throw error;
    });
    return { rec: flight.rec, response };
  };
  const send = async (checked: string | undefined, attempt: number): Promise<{ response: Response; rec: CallHandle }> => {
    const flight = post(checked, attempt);
    const response = limits && stop ? await headersInTime(flight, limits.firstMs, stop) : await flight.response;
    return { response, rec: flight.rec };
  };
  const model = String(call.body.model ?? "");

  let opened: Awaited<ReturnType<typeof openStep>> | null = null;
  let firstBytes: string | undefined;
  if (cache) {
    firstBytes = checkedBytes(call, options);
    opened = await openStep(cache, "sendChat", wireOf(call, firstBytes), modelOf(firstBytes, model));
    if (opened.entry) {
      const stored = replayResponse(opened.entry);
      const parsed = parseJson(new Uint8Array(await stored.arrayBuffer()));
      const callId = recordReplay(specOf(call, options, "sendChat", 1), {
        ok: stored.ok && parsed.ok, status: stored.status, errorKind: !stored.ok ? "http" : parsed.ok ? null : "body",
      });
      if (!stored.ok) throw stamp(httpErrorOf(stored.status, parsed.ok ? parsed.value : null, true), { callId });
      if (!parsed.ok) throw stamp(new SendError("body", "The stored answer is not JSON.", { status: stored.status, replayed: true }), { callId });
      return answerOf(parsed.value, model, started, true, callId);
    }
  }

  let sent = await send(firstBytes, 1);
  let retried = false;
  const retry = options.reasoningRetry;
  try {
    if (retry && sent.response.status === 400 && reasoningOff(call.body) && /reasoning is mandatory/i.test(await sent.response.clone().text().catch(() => ""))) {
      sent.rec.end({ ok: false, status: 400, errorKind: "http" });
      retry.onRequired?.(model);
      call.body.reasoning = { max_tokens: retry.budget };
      if (call.body.max_tokens != null) call.body.max_tokens = (call.body.max_tokens as number) + retry.budget;
      retried = true;
      sent = await send(undefined, 2);
    }
  } catch (error) {
    sent.rec.end({ ok: false, errorKind: "error" });
    throw error;
  }
  const { response, rec } = sent;
  try {
    return await read(response, rec);
  } catch (error) {
    // Ends the call if the read did not (a no-op when it did), with what the error says; stamps the ids.
    rec.end(failedEnd(error, response.status));
    throw stamp(error as Error, { callId: rec.id });
  }

  async function read(response: Response, rec: CallHandle): Promise<ChatAnswer> {
    if (opened) {
      // The step of the send that answered: the first send's key, the answering bytes' temperature.
      const step = retried
        ? { ...opened.step, deterministic: deterministic("sendChat", (call.encode ?? JSON.stringify)(call.body), cache!) }
        : opened.step;
      const context = { url: call.url, model: step.model, transport: "sendChat" };
      let bytes: Uint8Array;
      try {
        bytes = new Uint8Array(await response.arrayBuffer());
      } catch (error) {
        if (!response.ok) throw httpErrorOf(response.status, null);
        if (options.signal?.aborted) throw new SendError("aborted", "The caller aborted the call.", { status: response.status, cause: error });
        throw new SendError("body", "The answer is not JSON.", { status: response.status, cause: error });
      }
      const parsed = parseJson(bytes);
      const answered = { status: response.status, headers: response.headers, bytes };
      if (!response.ok) {
        const error = httpErrorOf(response.status, parsed.ok ? parsed.value : null);
        rec.end(failedEnd(error, response.status));
        if (parsed.ok) await commitStep(step, cache!.classify(stamp(error, { callId: rec.id }), context), answered);
        throw error;
      }
      if (!parsed.ok) throw new SendError("body", "The answer is not JSON.", { status: response.status });
      const answer = answerOf(parsed.value, model, started, false, rec.id);
      rec.end(plainEnd(response.status, parsed.value));
      await commitStep(step, cache!.classify(answer, context), answered);
      return answer;
    }
    if (!response.ok) throw await httpError(response);

    const asStream = Boolean(options.onDelta) || (call.body.stream === true && !/application\/json/i.test(response.headers.get("content-type") ?? ""));
    if (asStream) {
      const { reported, ...streamed } = await readStream(response, {
        model, limits, started, stop,
        onDelta: options.onDelta, onToolArguments: options.onToolArguments,
        requireFinish: options.requireFinish ?? false, stopOnErrorFinish: options.stopOnErrorFinish ?? true,
        signal: options.signal,
      });
      rec.end({ ok: true, status: response.status, servedModel: reported, provider: streamed.provider, generationId: streamed.generationId, usage: streamed.usage });
      return { ...streamed, streamed: true, ms: Date.now() - started, replayed: false, callId: rec.id };
    }
    let payload: any;
    try {
      payload = await response.json();
    } catch (error) {
      if (options.signal?.aborted) throw new SendError("aborted", "The caller aborted the call.", { status: response.status, cause: error });
      throw new SendError("body", "The answer is not JSON.", { status: response.status, cause: error });
    }
    rec.end(plainEnd(response.status, payload));
    return answerOf(payload, model, started, false, rec.id);
  }
}

const LIMIT_KINDS: ReadonlySet<string> = new Set(["no_first_token", "stalled", "slow"]);

/** How the ledger records a failed read: the error's kind, its status (the response's when it has none), its host and
 * generation id. A limit or an abort counts as cut after it was sent. */
function failedEnd(error: unknown, status: number): EndInfo {
  if (!(error instanceof SendError)) return { ok: false, status, errorKind: "error" };
  return {
    ok: false, status: error.status ?? status, errorKind: error.kind, aborted: error.kind === "aborted" || LIMIT_KINDS.has(error.kind),
    provider: error.provider, generationId: error.generationId,
  };
}

const named = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

/** The ledger's end of a parsed plain answer. */
const plainEnd = (status: number, payload: any): EndInfo => ({
  ok: true, status, servedModel: named(payload?.model), provider: named(payload?.provider), generationId: named(payload?.id), usage: payload?.usage ?? null,
});

/** The answer of a parsed plain body. A replay reports no usage: the stored body's usage was billed once already. */
function answerOf(payload: any, model: string, started: number, replayed: boolean, callId: string): ChatAnswer {
  const choice = payload?.choices?.[0];
  const content = choice?.message?.content;
  return {
    payload,
    text: typeof content === "string" ? content : "",
    toolCalls: Array.isArray(choice?.message?.tool_calls) ? choice.message.tool_calls : [],
    usage: replayed ? null : payload?.usage ?? null,
    model: typeof payload?.model === "string" ? payload.model : model,
    provider: typeof payload?.provider === "string" ? payload.provider : null,
    finishReason: choice?.finish_reason ?? null,
    nativeFinishReason: choice?.native_finish_reason ?? null,
    streamed: false,
    ms: Date.now() - started,
    replayed,
    callId,
    generationId: replayed ? null : named(payload?.id),
  };
}
