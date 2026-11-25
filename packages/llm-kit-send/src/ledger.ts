// The call ledger: one sink per process that is told, for each call that can be billed, when it begins (before the
// first byte leaves) and when it ends. The transports call `beginCall`; an app installs the sink with `setCallLedger`.
import { AsyncLocalStorage } from "node:async_hooks";
import { usageCost } from "./cost.js";

/** The format id of the records a {@link CallLedgerSink} gets (`llm-kit.paid-call/v1`). */
export const PAID_CALL_FORMAT = "llm-kit.paid-call/v1";

/** The labels of one call: keys the caller chooses, values a string, a finite number or `null`. */
export type CallLabels = Readonly<Record<string, string | number | null>>;

/** The record a sink gets when a call begins, before the first byte of its request leaves. Format `llm-kit.paid-call/v1`. */
export interface PaidCallBegin {
  readonly format: typeof PAID_CALL_FORMAT;
  readonly phase: "begin";
  /** The call id; the {@link PaidCallEnd} of the call has the same one. */
  readonly id: string;
  /** The package function that sends the call (`sendChat`, `postVenice`, `falQueue`, ...). */
  readonly transport: string;
  /** The HTTP method of the request (of the submit for a queue job). */
  readonly method: string;
  /** The URL host with its port; `""` when the URL cannot be parsed. */
  readonly host: string;
  /** The URL path: no query, no fragment, no user information. */
  readonly path: string;
  /** The model or endpoint the request names; `""` when it names none. */
  readonly model: string;
  /** True when the request asks for a server-sent event stream. */
  readonly stream: boolean;
  /** The try number, from 1: a retry of `sendWithRetries`, or the reasoning-off retry of `sendChat`, is a new call. */
  readonly attempt: number;
  /** When the call began, in milliseconds since the epoch. */
  readonly startedAt: number;
  /** A copy of the caller's `labels` option; `{}` when none was given. */
  readonly labels: CallLabels;
}

/** The record a sink gets when a call ends. Format `llm-kit.paid-call/v1`; every field is present, `null` when unknown. */
export interface PaidCallEnd {
  readonly format: typeof PAID_CALL_FORMAT;
  readonly phase: "end";
  /** The id of the {@link PaidCallBegin}. */
  readonly id: string;
  /** When the call ended, in milliseconds since the epoch. */
  readonly endedAt: number;
  /** The call's time in milliseconds. */
  readonly ms: number;
  /** True when the call gave a complete answer (a queue job: its result). */
  readonly ok: boolean;
  /** The HTTP status of the last answer, or `null` with no response. */
  readonly status: number | null;
  /** What failed, or `null`: a `SendError` kind, `error` (an unexpected exception), `cancelled` (the reader cancelled the
   * body), or `<stage>:<code>` for a queue job. */
  readonly errorKind: string | null;
  /** True when the caller's signal, a deadline or a stream limit cut the call after it was sent. */
  readonly aborted: boolean;
  /** True when the reply cache answered: nothing was sent. */
  readonly replayed: boolean;
  /** The model the provider reported, or `null`. */
  readonly servedModel: string | null;
  /** The host that served the call, when the provider named it, or `null`. */
  readonly provider: string | null;
  /** The provider's id of the generation (the `id` of the answer, or of the stream chunks), or `null`. */
  readonly generationId: string | null;
  /** The usage object the provider reported, as sent, or `null`. */
  readonly usage: Record<string, unknown> | null;
  readonly inputTokens: number | null;
  readonly cachedTokens: number | null;
  readonly outputTokens: number | null;
  /** The cost the host reported ({@link usageCost}), or `null`; 0 for a replay. */
  readonly cost: number | null;
}

/** The sink of the call ledger: told when each call begins and ends. See {@link setCallLedger}. */
export interface CallLedgerSink {
  /** Called synchronously, in the caller's async context, before the first byte of the request leaves. */
  begin(record: PaidCallBegin): void;
  /** Called when the call ends: once for each `begin`, unless the response body is never read or cancelled. */
  end(record: PaidCallEnd): void;
}

const code = "LLMKIT_NO_CALL_LEDGER";

/**
 * The error of a call made while the ledger is required and no sink is installed. It has `code`
 * `LLMKIT_NO_CALL_LEDGER`; nothing was sent.
 *
 * @param message - What to tell the developer.
 * @returns A new error.
 * @throws Never.
 */
export class CallLedgerError extends Error {
  /** Always `LLMKIT_NO_CALL_LEDGER`. */
  readonly code = code;

  constructor(message: string) {
    super(message);
    this.name = "CallLedgerError";
  }
}

const SINK = Symbol.for("katsoft.llm-kit-send.callLedger");
const REQUIRED = Symbol.for("katsoft.llm-kit-send.callLedger.required");
const WARNED = Symbol.for("katsoft.llm-kit-send.callLedger.warned");
const ACTIVE = Symbol.for("katsoft.llm-kit-send.activeCall");

// Process-wide slots, so two copies of this package in one process share one sink and one active call.
const slots = globalThis as unknown as Record<symbol, any>;
const storage: AsyncLocalStorage<string> = (slots[ACTIVE] ??= new AsyncLocalStorage<string>());

/**
 * Installs the process's call ledger sink, or removes it with `null`. There is one sink per process, kept at
 * `globalThis[Symbol.for("katsoft.llm-kit-send.callLedger")]`. Every call that can be billed tells it `begin` before
 * the first byte leaves and `end` when the call ends (format `llm-kit.paid-call/v1`). A sink that throws is reported as
 * a process warning (`LLMKIT_CALL_LEDGER_SINK_ERROR`) and the call goes on.
 *
 * @param sink - The sink, or `null` for none.
 * @returns Nothing.
 * @throws {TypeError} When `sink` is neither `null` nor an object with `begin` and `end` functions.
 */
export function setCallLedger(sink: CallLedgerSink | null): void {
  if (sink !== null && (typeof sink?.begin !== "function" || typeof sink?.end !== "function")) {
    throw new TypeError("A call ledger sink has a begin function and an end function.");
  }
  slots[SINK] = sink;
}

/**
 * Turns the required mode on or off. In the required mode a call made with no sink throws {@link CallLedgerError}
 * before anything is sent. The environment variable `LLM_KIT_CALL_LEDGER=required` turns it on too. With the mode off and
 * no sink, the first call gives one process warning `LLMKIT_NO_CALL_LEDGER` and every call goes on.
 *
 * @param on - True to require a sink; false to withdraw this call's requirement (the environment variable still applies).
 * @returns Nothing.
 * @throws Never.
 */
export function requireCallLedger(on: boolean): void {
  slots[REQUIRED] = Boolean(on);
}

/**
 * The id of the call the code runs in: set while a transport of this package sends a request (and during the whole job
 * of a queue transport), also with no sink installed. A fetch guard uses it to tell a covered request from one that
 * bypassed the package.
 *
 * @param - None: it takes no arguments.
 * @returns The id of the running call (the `id` of its records), or `null` outside any call.
 * @throws Never.
 */
export function activeCall(): string | null {
  return storage.getStore() ?? null;
}

const required = (): boolean => slots[REQUIRED] === true || process.env.LLM_KIT_CALL_LEDGER === "required";

function warn(message: string, warningCode: string): void {
  process.emitWarning(message, { code: warningCode });
}

/** What a transport tells the ledger when a call ends; the tokens and the cost come from `usage` unless given. */
export interface EndInfo {
  readonly ok: boolean;
  readonly status?: number | null;
  readonly errorKind?: string | null;
  readonly aborted?: boolean;
  readonly replayed?: boolean;
  readonly servedModel?: string | null;
  readonly provider?: string | null;
  readonly generationId?: string | null;
  readonly usage?: unknown;
  readonly cost?: number | null;
}

/** What a transport tells the ledger when a call begins. */
export interface CallSpec {
  readonly transport: string;
  readonly method: string;
  readonly url: string;
  readonly model: string;
  readonly stream: boolean;
  readonly attempt?: number;
  readonly labels?: CallLabels | undefined;
}

/** One call in the ledger: its id, the way to run the send inside it, and the way to end it (once). */
export interface CallHandle {
  readonly id: string;
  /** Runs `work` with {@link activeCall} set to this call's id (also across its awaits). */
  run<T>(work: () => T): T;
  /** Tells the sink the call ended; the first call counts, later calls do nothing. */
  end(info: EndInfo): void;
}

function checkLabels(labels: unknown): CallLabels {
  if (labels === undefined) return {};
  if (labels === null || typeof labels !== "object" || Array.isArray(labels)) throw new TypeError("The labels of a call are an object.");
  for (const [name, value] of Object.entries(labels)) {
    if (value !== null && typeof value !== "string" && !(typeof value === "number" && Number.isFinite(value))) {
      throw new TypeError(`The label ${JSON.stringify(name)} must be a string, a finite number or null.`);
    }
  }
  return { ...(labels as CallLabels) };
}

function whereOf(url: string): { host: string; path: string } {
  try {
    const parsed = new URL(url);
    return { host: parsed.host, path: parsed.pathname };
  } catch {
    return { host: "", path: url.split(/[?#]/, 1)[0] ?? "" };
  }
}

const count = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null);

function tell(sink: CallLedgerSink, phase: "begin" | "end", record: PaidCallBegin | PaidCallEnd): void {
  const report = (error: unknown) =>
    warn(`The call ledger sink failed on ${phase}: ${error instanceof Error ? error.message : String(error)}`, "LLMKIT_CALL_LEDGER_SINK_ERROR");
  try {
    const result: unknown = phase === "begin" ? sink.begin(record as PaidCallBegin) : sink.end(record as PaidCallEnd);
    if (typeof (result as { then?: unknown } | null)?.then === "function") (result as Promise<unknown>).then(undefined, report);
  } catch (error) {
    report(error);
  }
}

/**
 * Begins a call: checks the labels, finds the sink (the required mode with none throws), tells the sink `begin`
 * synchronously and returns the handle. Nothing has been sent yet.
 */
export function beginCall(spec: CallSpec): CallHandle {
  const labels = checkLabels(spec.labels);
  const sink: CallLedgerSink | null = slots[SINK] ?? null;
  if (!sink) {
    if (required()) throw new CallLedgerError("No call ledger sink is installed and the ledger is required: call setCallLedger before any send.");
    if (!slots[WARNED]) {
      slots[WARNED] = true;
      warn("No call ledger sink is installed: paid calls are not recorded. Call setCallLedger, or requireCallLedger(true) to refuse such calls.", code);
    }
  }
  const id = globalThis.crypto.randomUUID();
  const startedAt = Date.now();
  if (sink) {
    tell(sink, "begin", {
      format: PAID_CALL_FORMAT, phase: "begin", id, transport: spec.transport, method: spec.method.toUpperCase(), ...whereOf(spec.url),
      model: spec.model, stream: spec.stream, attempt: spec.attempt ?? 1, startedAt, labels,
    });
  }
  let ended = false;
  return {
    id,
    run: (work) => storage.run(id, work),
    end(info) {
      if (ended) return;
      ended = true;
      if (!sink) return;
      const endedAt = Date.now();
      const usage = !info.replayed && info.usage !== null && typeof info.usage === "object" && !Array.isArray(info.usage) ? info.usage as Record<string, any> : null;
      tell(sink, "end", {
        format: PAID_CALL_FORMAT, phase: "end", id, endedAt, ms: Math.max(0, endedAt - startedAt), ok: info.ok,
        status: info.status ?? null, errorKind: info.errorKind ?? null, aborted: info.aborted ?? false, replayed: info.replayed ?? false,
        servedModel: info.servedModel ?? null, provider: info.provider ?? null, generationId: info.generationId ?? null, usage,
        inputTokens: count(usage?.prompt_tokens), cachedTokens: count(usage?.prompt_tokens_details?.cached_tokens),
        outputTokens: count(usage?.completion_tokens),
        cost: info.replayed ? 0 : ((c) => (c !== null && c >= 0 ? c : null))(info.cost !== undefined ? info.cost : usageCost(usage)),
      });
    },
  };
}

/** Records an answer of the reply cache: `begin` and `end` at once with `replayed: true`; nothing is sent. Returns the call id. */
export function recordReplay(spec: CallSpec, info: Omit<EndInfo, "replayed">): string {
  const call = beginCall(spec);
  call.end({ ...info, replayed: true });
  return call.id;
}

/**
 * Begins the call, then calls the transport inside it. The returned response promise rejects with the transport's own
 * error, after the call was ended as `network`, or `aborted` when `signal` was aborted. The begin is told synchronously.
 */
export function dispatch(spec: CallSpec, fetchImpl: typeof fetch, url: string, init: Record<string, unknown>, signal?: AbortSignal | undefined): { rec: CallHandle; response: Promise<Response> } {
  const rec = beginCall(spec);
  let pending: Promise<Response>;
  try {
    pending = Promise.resolve(rec.run(() => fetchImpl(url, init as RequestInit)));
  } catch (error) {
    pending = Promise.reject(error);
  }
  const response = pending.catch((error: unknown) => {
    rec.end({ ok: false, errorKind: signal?.aborted ? "aborted" : "network", aborted: Boolean(signal?.aborted) });
    throw error;
  });
  return { rec, response };
}
