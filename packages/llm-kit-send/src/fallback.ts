// One fallback before the first token.
import { ReplyCacheError } from "./reply-cache.js";

/** The options of {@link withFallback}. */
export interface FallbackOptions {
  /** True once any output reached the caller's reader; a fallback after that would repeat or mix text. */
  readonly emitted: () => boolean;
  /** Whether a failure of the primary call may go to the fallback; `error.fallback === true` when not given. */
  readonly eligible?: (error: any) => boolean;
  /** Reports the primary failure, and whether the fallback runs next. The fallback function reports its own failure. */
  readonly onFailure?: (error: any, willFallback: boolean) => void;
}

/**
 * Runs `primary`; when it fails before any output was emitted, the failure is not an abort, and it is eligible, runs
 * `fallback` once with that failure. The fallback's own failure propagates; there is no second fallback. A
 * {@link ReplyCacheError} is a store failure, not a provider failure: it propagates at once, before `eligible` or
 * `onFailure` is asked.
 *
 * @param primary - The first call.
 * @param fallback - The one fallback call, given the primary failure; `null` for none.
 * @param options - When output was emitted, which failures are eligible, and the failure report.
 * @returns The result of the primary call, or of the fallback.
 * @throws The primary failure when no fallback runs; else the fallback's failure.
 */
export async function withFallback<T>(primary: () => Promise<T>, fallback: ((error: any) => Promise<T>) | null, options: FallbackOptions): Promise<T> {
  try {
    return await primary();
  } catch (error: any) {
    if (error instanceof ReplyCacheError) throw error;
    const aborted = error?.name === "AbortError" || error?.kind === "aborted";
    const eligible = options.eligible ? options.eligible(error) : error?.fallback === true;
    const next = Boolean(fallback) && !options.emitted() && !aborted && eligible;
    options.onFailure?.(error, next);
    if (!next || !fallback) throw error;
    return fallback(error);
  }
}
