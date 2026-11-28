// spreadGather: run every item at once under one AbortController and one deadline; gather the results in input order.
import { openScope, requirePositive, started } from "./scope.js";

/** What `run` gets with each item: the one signal all items share, and the item's index in the input. */
export interface SpreadContext {
  /** Aborts at the deadline (reason `TimeoutError`) or when the caller's signal aborts. */
  readonly signal: AbortSignal;
  readonly index: number;
}

/** The work for one item. A throw or a rejection is that item's `rejected` entry. */
export type SpreadRun<I, O> = (item: I, context: SpreadContext) => O | PromiseLike<O>;

/** The options of `spreadGather`: the one deadline, when it starts, and the caller's signal. */
export interface SpreadOptions {
  /** One deadline in milliseconds over every item, counted from `startedAt`. */
  readonly deadlineMs: number;
  /** When the deadline starts, as a `Date.now()` value. Default: the call of `spreadGather`. */
  readonly startedAt?: number | undefined;
  /** The caller's signal: its abort stops the gather. */
  readonly signal?: AbortSignal | undefined;
}

/** One item's entry: its value, its error, or `timeout` when it had not settled at the deadline (its later output is dropped). */
export type SpreadEntry<O> =
  | { readonly status: "fulfilled"; readonly value: O }
  | { readonly status: "rejected"; readonly reason: unknown }
  | { readonly status: "timeout" };

/** The result of `spreadGather`: one entry for each item, in input order, and whether the deadline passed first. */
export interface SpreadResult<O> {
  readonly results: readonly SpreadEntry<O>[];
  readonly timedOut: boolean;
}

/**
 * Run `run` for every item at once, all under one AbortController and one deadline, and gather the settled results in
 * input order. At the deadline the shared signal aborts, each item that has not settled is a `timeout` entry, and any
 * output that comes later is dropped. When no time is left at the start, no item runs. A failure of one item does not
 * stop the others.
 *
 * @param items The inputs, one run each.
 * @param run The work for one item; it gets the item and `{signal, index}`.
 * @param options `deadlineMs`, the optional `startedAt` the deadline counts from, and the caller's `signal`.
 * @returns The entries in input order, and `timedOut`: true when the deadline passed before every item settled.
 * @throws {TypeError} When `items` is not an array, `run` is not a function, `deadlineMs` is not a finite number
 *   above 0, or `startedAt` is not a finite number. Rejects with the caller's `signal.reason` when the caller's signal
 *   aborts (the shared signal aborts too).
 */
export async function spreadGather<I, O>(items: readonly I[], run: SpreadRun<I, O>, options: SpreadOptions): Promise<SpreadResult<O>> {
  if (!Array.isArray(items)) throw new TypeError("items must be an array");
  if (typeof run !== "function") throw new TypeError("run must be a function");
  requirePositive("deadlineMs", options?.deadlineMs);
  const startedAt = options.startedAt ?? Date.now();
  if (typeof startedAt !== "number" || !Number.isFinite(startedAt)) throw new TypeError("startedAt must be a finite number");
  const outer = options.signal;
  outer?.throwIfAborted();
  const remaining = startedAt + options.deadlineMs - Date.now();
  if (items.length === 0) return { results: [], timedOut: false };
  if (remaining <= 0) return { results: items.map(() => ({ status: "timeout" })), timedOut: true };

  const scope = openScope(remaining, outer);
  const entries: (SpreadEntry<O> | undefined)[] = items.map(() => undefined);
  let open = true;
  let left = items.length;
  // Closed in the abort event itself, before any item's own reaction to the abort can settle it.
  scope.signal.addEventListener("abort", () => { open = false; }, { once: true });
  try {
    const all = new Promise<void>((done) => {
      items.forEach((item, index) => {
        started(() => run(item, { signal: scope.signal, index })).then(
          (value) => settle(index, { status: "fulfilled", value }),
          (reason: unknown) => settle(index, { status: "rejected", reason }),
        );
      });
      function settle(index: number, entry: SpreadEntry<O>): void {
        if (!open) return;
        entries[index] = entry;
        left -= 1;
        if (left === 0) done();
      }
    });
    await Promise.race([all, scope.stopped]).catch(() => undefined);
    open = false;
    if (scope.cause() === "caller") throw outer?.reason;
    return { results: entries.map((entry) => entry ?? { status: "timeout" }), timedOut: left > 0 };
  } finally {
    open = false;
    scope.close();
  }
}
