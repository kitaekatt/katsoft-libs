// Queue jobs on fal and Sunra: submit, read the status until the job ends, read the result. A job never throws: it
// returns the stage it reached and whether the host reported the job complete, so the caller can record what may have
// been billed.
import { FAL_QUEUE_URL, SUNRA_QUEUE_URL } from "./media.js";
import type { MediaOptions } from "./media.js";
import { pause } from "./retry.js";

/** A step of a queue job: the submit, a status read or the result read. */
export type QueueStage = "submit" | "status" | "result";

/** One answered queue read: its JSON body and status. */
type Answer = { readonly body: any; readonly status: number };
const isAnswer = (value: Answer | FalQueueResult): value is Answer => !("ok" in value);

/** One fal queue job for {@link falQueue}. */
export interface FalQueueCall {
  /** The fal model id (`fal-ai/...`); the job is submitted to `<base>/<model>`. */
  readonly model: string;
  /** Sent as `Authorization: Key <key>` on every queue request. */
  readonly key: string;
  /** The model input, sent as `JSON.stringify(input)`. */
  readonly input: unknown;
  /** The wait between two status reads, in milliseconds; 0 reads again at once. */
  readonly pollMs: number;
  /** The caller's refusal rule, checked on every answer before anything else: true ends the job as `refused`. */
  readonly refusal?: (status: number, body: unknown) => boolean;
  /** The queue base; {@link FAL_QUEUE_URL} when not given. */
  readonly baseUrl?: string;
}

/** The result of {@link falQueue}. */
export type FalQueueResult =
  | { readonly ok: true; readonly output: any }
  | {
    readonly ok: false;
    readonly stage: QueueStage;
    /** `network` (no answer), `aborted` (the caller's signal), `refused` (the caller's `refusal`), `http` (not 2xx),
     * `body` (2xx with no JSON body), `shape` (no `status_url` or `response_url`), `state` (an unknown job status). */
    readonly reason: "network" | "aborted" | "refused" | "http" | "body" | "shape" | "state";
    /** The HTTP status of the last answer, or `null` with no answer. */
    readonly status: number | null;
    /** The parsed body of the last answer, or `null`. */
    readonly body: unknown;
    /** True once fal reported the job `COMPLETED`. */
    readonly completed: boolean;
  };

/**
 * One fal queue job: POST the input to `<base>/<model>`, GET its `status_url` until `COMPLETED` (waiting `pollMs`
 * between reads while it is `IN_QUEUE` or `IN_PROGRESS`), then GET its `response_url`.
 *
 * @param call - The model, key, input, poll wait, refusal rule and base URL.
 * @param options - The transport and signal (the job's deadline).
 * @returns The output JSON, or the stage, reason, last status and body, and whether fal completed the job.
 * @throws Never: an abort or a lost connection is a result.
 */
export async function falQueue(call: FalQueueCall, options: MediaOptions = {}): Promise<FalQueueResult> {
  const { signal } = options;
  const headers = { Authorization: `Key ${call.key}`, "Content-Type": "application/json" };
  let completed = false;
  const fail = (stage: QueueStage, reason: Extract<FalQueueResult, { ok: false }>["reason"], status: number | null, body: unknown): FalQueueResult =>
    ({ ok: false, stage, reason, status, body, completed });
  const read = async (stage: QueueStage, url: string, init: Record<string, unknown> = {}): Promise<Answer | FalQueueResult> => {
    let response: Response;
    try {
      response = await (options.fetch ?? globalThis.fetch)(url, { ...init, headers, signal } as RequestInit);
    } catch {
      return fail(stage, signal?.aborted ? "aborted" : "network", null, null);
    }
    const body: any = await response.json().catch(() => null);
    if (call.refusal?.(response.status, body)) return fail(stage, "refused", response.status, body);
    if (!response.ok) return fail(stage, "http", response.status, body);
    if (!body) return fail(stage, "body", response.status, body);
    return { body, status: response.status };
  };
  const queued = await read("submit", `${call.baseUrl ?? FAL_QUEUE_URL}/${call.model}`, { method: "POST", body: JSON.stringify(call.input) });
  if (!isAnswer(queued)) return queued;
  const statusUrl = typeof queued.body.status_url === "string" ? queued.body.status_url : null;
  const responseUrl = typeof queued.body.response_url === "string" ? queued.body.response_url : null;
  if (!statusUrl || !responseUrl) return fail("submit", "shape", queued.status, queued.body);
  for (;;) {
    const state = await read("status", statusUrl);
    if (!isAnswer(state)) return state;
    if (state.body.status === "COMPLETED") { completed = true; break; }
    if (state.body.status !== "IN_QUEUE" && state.body.status !== "IN_PROGRESS") return fail("status", "state", state.status, state.body);
    if (call.pollMs) {
      try { await pause(call.pollMs, signal); } catch { return fail("status", "aborted", null, null); }
    }
  }
  const done = await read("result", responseUrl);
  if (!isAnswer(done)) return done;
  return { ok: true, output: done.body };
}

/** One Sunra queue job for {@link sunraQueue}. */
export interface SunraQueueCall {
  /** The Sunra endpoint id (`elevenlabs/turbo-v2.5/text-to-speech`, ...). */
  readonly endpoint: string;
  /** Sent as `Authorization: Key <key>` on every queue request. */
  readonly key: string;
  /** The endpoint input, sent as `JSON.stringify(input)`. */
  readonly input: unknown;
  /** The wait between two status reads, in milliseconds. */
  readonly pollMs: number;
  /** The queue base; {@link SUNRA_QUEUE_URL} when not given. */
  readonly baseUrl?: string;
}

/** The result of {@link sunraQueue}. */
export type SunraQueueResult =
  | { readonly ok: true; readonly output: any }
  | {
    readonly ok: false;
    readonly stage: QueueStage;
    /** True when Sunra ended the job with its refusal code `unsafe_content`. */
    readonly refused: boolean;
    /** `submit_<status>`, `status_<status>`, `result_<status>`, `no_request_id`, `canceled`, Sunra's error code (or
     * `failed`), `network` (no answer or an unreadable body) or `aborted` (the caller's signal). */
    readonly code: string;
    /** True once Sunra reported the job `COMPLETED` (successful or not). */
    readonly completed: boolean;
  };

/**
 * One Sunra queue job: POST the input to `<base>/<endpoint>`, GET `<base>/requests/<id>/status` until `COMPLETED` or
 * `CANCELED` (waiting `pollMs` between reads), then GET `<base>/requests/<id>`. Sunra documents that a failed job costs
 * nothing.
 *
 * @param call - The endpoint, key, input, poll wait and base URL.
 * @param options - The transport and signal.
 * @returns The output JSON, or the stage, code, refusal flag and whether Sunra completed the job.
 * @throws Never: an abort or a lost connection is a result.
 */
export async function sunraQueue(call: SunraQueueCall, options: MediaOptions = {}): Promise<SunraQueueResult> {
  const { signal } = options;
  const base = call.baseUrl ?? SUNRA_QUEUE_URL;
  const headers = { Authorization: `Key ${call.key}`, "Content-Type": "application/json" };
  const transport = options.fetch ?? globalThis.fetch;
  let stage: QueueStage = "submit";
  let completed = false;
  const fail = (code: string, refused = false): SunraQueueResult => ({ ok: false, stage, refused, code, completed });
  try {
    const submit = await transport(`${base}/${call.endpoint}`, { method: "POST", headers, body: JSON.stringify(call.input), signal } as RequestInit);
    if (!submit.ok) return fail(`submit_${submit.status}`);
    const id = ((await submit.json().catch(() => ({}))) as any)?.request_id;
    if (typeof id !== "string" || !/^[\w-]+$/.test(id)) return fail("no_request_id");
    const request = `${base}/requests/${id}`;
    stage = "status";
    for (;;) {
      const status = await transport(`${request}/status`, { headers, signal } as RequestInit);
      if (!status.ok) return fail(`status_${status.status}`);
      const state: any = await status.json().catch(() => ({}));
      if (state.status === "CANCELED") return fail("canceled");
      if (state.status === "COMPLETED") {
        completed = true;
        if (state.success === false || state.error) {
          const code = typeof state.error?.code === "string" ? state.error.code : "failed";
          return fail(code, code === "unsafe_content");
        }
        break;
      }
      await pause(call.pollMs, signal);
    }
    stage = "result";
    const result = await transport(request, { headers, signal } as RequestInit);
    if (!result.ok) return fail(`result_${result.status}`);
    return { ok: true, output: await result.json() };
  } catch {
    return fail(signal?.aborted ? "aborted" : "network");
  }
}
