// One AbortController under one deadline and the caller's signal; shared by submitToolPass and spreadGather.
// Internal: not exported from the package.

/** Why a scope stopped: the deadline passed first, or the caller's signal aborted first. */
export type StopCause = "deadline" | "caller";

/** A running scope. `stopped` rejects (with no reason the caller sees) when `signal` aborts. */
export interface Scope {
  readonly signal: AbortSignal;
  readonly stopped: Promise<never>;
  cause(): StopCause | null;
  close(): void;
}

/** The marker `stopped` rejects with. */
export const STOPPED: unique symbol = Symbol("stopped");

/**
 * Open a scope whose signal aborts after `remainingMs` (a `TimeoutError` reason, as `AbortSignal.timeout` gives) or when
 * `outer` aborts (with its reason), whichever comes first. The caller must call `close()` when the work ends.
 */
export function openScope(remainingMs: number, outer: AbortSignal | undefined): Scope {
  const controller = new AbortController();
  let cause: StopCause | null = null;
  const stop = (why: StopCause, reason: unknown): void => {
    if (controller.signal.aborted) return;
    cause = why;
    controller.abort(reason);
  };
  const onOuter = (): void => stop("caller", outer?.reason);
  const timer = setTimeout(() => stop("deadline", new DOMException("The deadline passed.", "TimeoutError")), Math.max(0, remainingMs));
  if (outer?.aborted) onOuter();
  else outer?.addEventListener("abort", onOuter, { once: true });
  const stopped = new Promise<never>((_, reject) => {
    if (controller.signal.aborted) reject(STOPPED);
    else controller.signal.addEventListener("abort", () => reject(STOPPED), { once: true });
  });
  stopped.catch(() => undefined);
  return {
    signal: controller.signal,
    stopped,
    cause: () => cause,
    close() {
      clearTimeout(timer);
      outer?.removeEventListener("abort", onOuter);
    },
  };
}

/** Call `work` and return its promise; a synchronous throw becomes a rejected promise. A later rejection is never unhandled. */
export function started<T>(work: () => T | PromiseLike<T>): Promise<T> {
  let promise: Promise<T>;
  try {
    promise = Promise.resolve(work());
  } catch (error) {
    promise = Promise.reject(error);
  }
  promise.catch(() => undefined);
  return promise;
}

/** Throw a TypeError unless `value` is a finite number above 0. */
export function requirePositive(name: string, value: unknown): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) throw new TypeError(`${name} must be a finite number above 0`);
}
