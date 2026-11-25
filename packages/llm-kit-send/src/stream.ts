// Reads an OpenAI-compatible server-sent event body under the stream limits: the content deltas, thinking, tool
// calls, usage, the reported model and host, and the finish reason. Internal: send.ts calls it.
import { SendError, clip, stamp } from "./errors.js";

/**
 * A pace floor for a stream: after the first output, a stream that writes under `minCharsPerS` characters a second over
 * the last `windowMs` ends with the kind `slow`.
 */
export interface StreamPace {
  /** The window the pace is measured over, in milliseconds. */
  readonly windowMs: number;
  /** The lowest pace, in characters a second. */
  readonly minCharsPerS: number;
}

/**
 * The limits of a watched call: a call that keeps writing may run on; one that goes silent ends. `firstMs` is the
 * longest wait for the first real output (the wait for the response headers counts); `gapMs` the longest silence after
 * it; 0 turns that limit off. Real output is content, thinking (`reasoning`, `reasoning_content`,
 * `reasoning_details`) and tool call names or arguments; a comment line or a role-only chunk is not.
 */
export interface StreamLimits {
  /** The longest wait for the first real output, in milliseconds; 0 is no limit. */
  readonly firstMs: number;
  /** The longest silence after the first output, in milliseconds; 0 is no limit. */
  readonly gapMs: number;
  /** An optional pace floor. */
  readonly pace?: StreamPace;
}

/** One tool call of an answer, as the provider sent it. */
export interface ToolCall {
  readonly id?: string;
  readonly type?: string;
  readonly function: { readonly name?: string; readonly arguments: string };
}

/** What a stream read gives: the assembled answer in the shape of a plain (non-streamed) answer. */
export interface StreamRead {
  readonly payload: Record<string, unknown>;
  readonly text: string;
  readonly toolCalls: ToolCall[];
  readonly usage: unknown;
  readonly model: string;
  readonly provider: string | null;
  readonly finishReason: unknown;
  readonly nativeFinishReason: unknown;
  /** The `id` of the chunks (OpenRouter's generation id), or `null`. */
  readonly generationId: string | null;
  /** The model the chunks named, or `null` when none did (`model` is then the body's). Left out of the answer. */
  readonly reported: string | null;
}

export interface ReadOptions {
  readonly model: string;
  readonly limits: StreamLimits | null;
  /** When the call started: the first-output limit counts from here. */
  readonly started: number;
  /** Aborted before a limit error is thrown, so a stalled call never runs beside the next one. */
  readonly stop: AbortController | null;
  readonly onDelta?: ((text: string) => void) | undefined;
  readonly onToolArguments?: ((name: string, raw: string) => void) | undefined;
  readonly requireFinish: boolean;
  readonly stopOnErrorFinish: boolean;
  /** The caller's signal: a read cut by it is the kind `aborted`. */
  readonly signal?: AbortSignal | undefined;
}

/** Waits for `work` until `limit` ms are spent (`spent` already are); null when the time is up. A limit of 0 has no end.
 * The timer never outlives the wait, also when `work` fails. */
export async function inTime<T>(work: Promise<T>, limit: number, spent = 0): Promise<T | null> {
  if (!(limit > 0)) return work;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([work, new Promise<null>((resolve) => { timer = setTimeout(resolve, Math.max(0, limit - spent), null); })]);
  } finally {
    clearTimeout(timer);
  }
}

/** The error of a stream limit; `name` is `TimeoutError`. */
export const limitError = (kind: "no_first_token" | "stalled" | "slow", provider: string | null) =>
  new SendError(kind, "No new output", { provider });

/** The characters of real output in one stream delta: content, thinking and tool call arguments. */
function outputChars(delta: any): number {
  const text = (value: unknown) => (typeof value === "string" ? value.length : 0);
  let chars = text(delta?.content) + text(delta?.reasoning) + text(delta?.reasoning_content);
  for (const part of Array.isArray(delta?.tool_calls) ? delta.tool_calls : []) chars += text(part?.function?.arguments);
  return chars;
}

/** Judges the pace after the first output over the last `windowMs`; it is judged only when output arrives. */
function paceWatch(pace: StreamPace | undefined) {
  if (!pace) return null;
  const samples: [number, number][] = [];
  let total = 0;
  return {
    slow(chars: number, now = Date.now()): boolean {
      if (!(chars > 0)) return false;
      total += chars;
      samples.push([now, total]);
      if (now - samples[0]![0] < pace.windowMs) return false;
      // The newest sample at or before the window start is the base; older ones are no longer needed.
      while (samples.length > 1 && samples[1]![0] <= now - pace.windowMs) samples.shift();
      const [at, base] = samples[0]!;
      return ((total - base) * 1000) / Math.max(1, now - at) < pace.minCharsPerS;
    },
  };
}

type MutableCall = { id?: string; type?: string; function: { name?: string; arguments: string } };

/** Reads a server-sent event chat body. */
export async function readStream(response: Response, options: ReadOptions): Promise<StreamRead> {
  const { limits, stop } = options;
  if (!response.body) throw new SendError("body", "The response has no body.", { status: response.status });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const calls: MutableCall[] = [];
  let content = "", buffer = "", usage: unknown = null, reported = options.model, provider: string | null = null;
  let generation: string | null = null, named: string | null = null;
  let finish: unknown = null, native: unknown = null, wrote = false, last = options.started, chars = 0;
  const pace = paceWatch(limits?.pace);
  const progress = () => { wrote = true; last = Date.now(); };

  const line = (raw: string) => {
    if (!raw.startsWith("data:")) return;
    const data = raw.slice(5).trim();
    if (!data || data === "[DONE]") return;
    let chunk: any;
    try { chunk = JSON.parse(data); } catch { return; }
    if (generation === null && typeof chunk?.id === "string" && chunk.id) generation = chunk.id;
    if (typeof chunk?.model === "string") { reported = chunk.model; named = chunk.model; }
    if (typeof chunk?.provider === "string") provider = chunk.provider;
    if (chunk?.error) {
      throw new SendError("stream_error", "The provider sent an error in the stream.", {
        code: chunk.error.code ?? null, providerMessage: clip(chunk.error.message), provider,
      });
    }
    if (chunk?.usage) usage = chunk.usage;
    const choice = chunk?.choices?.[0];
    if (choice?.finish_reason) finish = choice.finish_reason;
    if (choice?.native_finish_reason) native = choice.native_finish_reason;
    const delta = choice?.delta;
    // OpenRouter streams thinking as `reasoning`, some hosts as `reasoning_content`. Thinking is progress.
    if ((typeof delta?.content === "string" && delta.content) || delta?.reasoning || delta?.reasoning_content || delta?.reasoning_details?.length) progress();
    chars += outputChars(delta);
    if (typeof delta?.content === "string" && delta.content) {
      content += delta.content;
      options.onDelta?.(delta.content);
    }
    for (const part of Array.isArray(delta?.tool_calls) ? delta.tool_calls : []) {
      const call = (calls[part.index ?? 0] ??= { function: { arguments: "" } });
      if (part.id) call.id = part.id;
      if (part.type) call.type = part.type;
      // A name may arrive in pieces, or again whole on a later delta.
      if (part.function?.name && part.function.name !== call.function.name) call.function.name = (call.function.name ?? "") + part.function.name;
      if (typeof part.function?.arguments === "string") call.function.arguments += part.function.arguments;
      if (part.function?.name || part.function?.arguments) progress();
      if (part.function?.arguments && call.function.name) options.onToolArguments?.(call.function.name, call.function.arguments);
    }
    if (options.stopOnErrorFinish && choice?.finish_reason === "error") {
      throw new SendError("stream_error", "The provider ended the stream with an error.", { code: "finish_reason_error", provider });
    }
  };

  try {
    for (;;) {
      const pending = reader.read();
      const next = limits ? await inTime(pending, wrote ? limits.gapMs : limits.firstMs, Date.now() - last) : await pending;
      if (next === null) {
        void pending.catch(() => {});
        stop?.abort();
        throw limitError(wrote ? "stalled" : "no_first_token", provider);
      }
      if (next.done) break;
      buffer += decoder.decode(next.value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        line(buffer.slice(0, end).replace(/\r$/, ""));
        buffer = buffer.slice(end + 1);
      }
      if (pace?.slow(chars)) {
        stop?.abort();
        throw limitError("slow", provider);
      }
      chars = 0;
    }
    line((buffer + decoder.decode()).replace(/\r$/, ""));
  } catch (error) {
    if (error instanceof SendError) throw stamp(error, { generationId: generation });
    if (options.signal?.aborted) throw new SendError("aborted", "The caller aborted the call.", { status: response.status, provider, generationId: generation, cause: error });
    throw new SendError("body", "The stream could not be read.", { status: response.status, provider, generationId: generation, cause: error });
  } finally {
    void reader.cancel().catch(() => {});
  }
  // A stream that ends with no finish was interrupted. A complete-looking tool call may be only the first of several.
  if (options.requireFinish && !finish) throw new SendError("incomplete", "Stream ended with no finish", { provider, generationId: generation });
  const toolCalls = calls.filter(Boolean).map((call) => ({ type: "function", ...call }));
  return {
    payload: {
      model: reported, provider, usage,
      choices: [{ finish_reason: finish, native_finish_reason: native, message: { content, ...(toolCalls.length ? { tool_calls: toolCalls } : {}) } }],
    },
    text: content, toolCalls, usage, model: reported, provider, finishReason: finish, nativeFinishReason: native,
    generationId: generation, reported: named,
  };
}
