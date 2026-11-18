// The cost of a call: the one the host reported, or one from a per-token price.

/** A per-token price in US dollars. Hosts that report no cost are priced with it. */
export interface TokenPrice {
  /** The price of one uncached input token. */
  readonly input: number;
  /** The price of one input token read from the host's cache. */
  readonly cached: number;
  /** The price of one input token written to the host's cache. */
  readonly write: number;
  /** The price of one output token. */
  readonly output: number;
}

/**
 * The cost the host reported in the answer's `usage.cost`.
 *
 * @param usage - The answer's usage object, or anything else.
 * @returns The cost, or `null` when the host reported none or it is not a finite number.
 * @throws Never.
 */
export function usageCost(usage: unknown): number | null {
  const cost = (usage as { cost?: unknown } | null | undefined)?.cost;
  return typeof cost === "number" && Number.isFinite(cost) ? cost : null;
}

/**
 * The cost of a call from a per-token price: uncached input, cache reads (`prompt_tokens_details.cached_tokens`), cache
 * writes (`prompt_tokens_details.cache_write_tokens`) and output.
 *
 * @param price - The price, or `undefined` when the model has none.
 * @param usage - The answer's usage object.
 * @returns The cost, or `null` without a price or without finite `prompt_tokens` and `completion_tokens`.
 * @throws Never.
 */
export function tokenCost(price: TokenPrice | undefined, usage: unknown): number | null {
  const counts = usage as any;
  const input = counts?.prompt_tokens, output = counts?.completion_tokens;
  if (!price || !Number.isFinite(input) || !Number.isFinite(output)) return null;
  const cached = Number(counts?.prompt_tokens_details?.cached_tokens) || 0;
  const write = Number(counts?.prompt_tokens_details?.cache_write_tokens) || 0;
  return Math.max(0, input - cached - write) * price.input + cached * price.cached + write * price.write + output * price.output;
}
