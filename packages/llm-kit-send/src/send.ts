// One OpenAI-compatible chat-completions call: the raw POST (postChat) and the parsed, plain or streamed call
// (sendChat) with the stream limits and the reasoning-off retry.
import { SendError, httpError, httpErrorOf } from "./errors.js";
import { capturedResponse, checkCached, commitStep, deterministic, modelOf, openStep, parseJson, replayResponse, replayedResponse } from "./reply-cache.js";
import type { CachedResponse, ParsedStepCache, StepCache } from "./reply-cache.js";
import { inTime, limitError, readStream } from "./stream.js";
import type { StreamLimits, ToolCall } from "./stream.js";

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
 * @throws {SendError} Kind `network` when the transport gave no response (`cause` is its error), or `aborted` when the
 *   signal ended it. An exception of `beforeSend` or of `encode` propagates unchanged. {@link ReplyCacheError} when the
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
  const bytes = (call.encode ?? JSON.stringify)(call.body);
  options.beforeSend?.(call.url, bytes);
  if (!cache) return postBytes(call, bytes, options);
  const { step, entry } = await openStep(cache, "postChat", wireOf(call, bytes), modelOf(bytes));
  if (entry) return replayedResponse(entry);
  return capturedResponse(step, await postBytes(call, bytes, options));
}

const headersOf = (call: ChatCall): Record<string, string> =>
  ({ ...(call.key == null ? {} : { Authorization: `Bearer ${call.key}` }), "Content-Type": "application/json", ...call.headers });

const wireOf = (call: ChatCall, bytes: string) => ({ method: "POST", url: call.url, headers: headersOf(call), body: bytes });

/** The POST of bytes already encoded and checked. */
async function postBytes(call: ChatCall, bytes: string, options: PostOptions): Promise<Response> {
  const transport = options.fetch ?? globalThis.fetch;
  const { signal } = options;
  try {
    return await transport(call.url, {
      ...(signal ? { signal } : {}),
      method: "POST",
      headers: headersOf(call),
      body: bytes,
    });
  } catch (error) {
    throw new SendError(signal?.aborted ? "aborted" : "network", "The provider gave no response.", { cause: error });
  }
}

const reasoningOff = (body: Record<string, unknown>) => {
  const reasoning = body.reasoning as { enabled?: unknown; effort?: unknown } | undefined;
  return reasoning?.enabled === false || reasoning?.effort === "none";
};

/** The wait for the response headers counts against the first output: a stalled provider may send nothing at all. */
async function headersInTime(request: Promise<Response>, ms: number, stop: AbortController): Promise<Response> {
  const response = await inTime(request, ms);
  if (response) return response;
  // Stop the stalled call before the caller moves on, so two models never run and bill at once.
  stop.abort();
  void request.then((late) => late.body?.cancel()).catch(() => {});
  throw limitError("no_first_token", null);
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
 * @throws {SendError} On every failure: `http`, `network`, `body`, `no_first_token`, `stalled`, `slow`,
 *   `stream_error`, `incomplete` or `aborted`. An exception of `beforeSend` propagates unchanged. With `cache`:
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
  const postOptions = {
    ...(options.fetch ? { fetch: options.fetch } : {}),
    ...(signal ? { signal } : {}),
    ...(options.beforeSend ? { beforeSend: options.beforeSend } : {}),
  };
  const post = (checked?: string) => (checked === undefined ? postChat(call, postOptions) : postBytes(call, checked, postOptions)).catch((error: unknown) => {
    // A limit abort is ours, not the caller's: report it as no response.
    if (error instanceof SendError && error.kind === "aborted" && !options.signal?.aborted) {
      throw new SendError("network", error.message, { cause: error.cause });
    }
    throw error;
  });
  const send = (checked?: string) => (limits && stop ? headersInTime(post(checked), limits.firstMs, stop) : post(checked));
  const model = String(call.body.model ?? "");

  let opened: Awaited<ReturnType<typeof openStep>> | null = null;
  let firstBytes: string | undefined;
  if (cache) {
    firstBytes = (call.encode ?? JSON.stringify)(call.body);
    options.beforeSend?.(call.url, firstBytes);
    opened = await openStep(cache, "sendChat", wireOf(call, firstBytes), modelOf(firstBytes, model));
    if (opened.entry) {
      const stored = replayResponse(opened.entry);
      const parsed = parseJson(new Uint8Array(await stored.arrayBuffer()));
      if (!stored.ok) throw httpErrorOf(stored.status, parsed.ok ? parsed.value : null, true);
      if (!parsed.ok) throw new SendError("body", "The stored answer is not JSON.", { status: stored.status, replayed: true });
      return answerOf(parsed.value, model, started, true);
    }
  }

  let response = await send(firstBytes);
  let retried = false;
  const retry = options.reasoningRetry;
  if (retry && response.status === 400 && reasoningOff(call.body) && /reasoning is mandatory/i.test(await response.clone().text().catch(() => ""))) {
    retry.onRequired?.(model);
    call.body.reasoning = { max_tokens: retry.budget };
    if (call.body.max_tokens != null) call.body.max_tokens = (call.body.max_tokens as number) + retry.budget;
    retried = true;
    response = await send();
  }
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
      if (parsed.ok) await commitStep(step, cache!.classify(error, context), answered);
      throw error;
    }
    if (!parsed.ok) throw new SendError("body", "The answer is not JSON.", { status: response.status });
    const answer = answerOf(parsed.value, model, started, false);
    await commitStep(step, cache!.classify(answer, context), answered);
    return answer;
  }
  if (!response.ok) throw await httpError(response);

  const asStream = Boolean(options.onDelta) || (call.body.stream === true && !/application\/json/i.test(response.headers.get("content-type") ?? ""));
  if (asStream) {
    const read = await readStream(response, {
      model, limits, started, stop,
      onDelta: options.onDelta, onToolArguments: options.onToolArguments,
      requireFinish: options.requireFinish ?? false, stopOnErrorFinish: options.stopOnErrorFinish ?? true,
      signal: options.signal,
    });
    return { ...read, streamed: true, ms: Date.now() - started, replayed: false };
  }
  let payload: any;
  try {
    payload = await response.json();
  } catch (error) {
    if (options.signal?.aborted) throw new SendError("aborted", "The caller aborted the call.", { status: response.status, cause: error });
    throw new SendError("body", "The answer is not JSON.", { status: response.status, cause: error });
  }
  return answerOf(payload, model, started, false);
}

/** The answer of a parsed plain body. A replay reports no usage: the stored body's usage was billed once already. */
function answerOf(payload: any, model: string, started: number, replayed: boolean): ChatAnswer {
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
  };
}
