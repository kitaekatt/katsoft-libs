// A backup key for a host that refuses the main key for a spend limit or for credits.

/**
 * True when a host refused a key for its limit: 402 (no credits), or a 403 whose body names a limit or credit.
 *
 * @param status - The HTTP status.
 * @param body - The response body text.
 * @returns Whether the key hit its limit.
 * @throws Never.
 */
export function isKeyLimit(status: number, body: string): boolean {
  return status === 402 || (status === 403 && /limit|credit/i.test(body));
}

/** The options of {@link withBackupKey}. */
export interface BackupKeyOptions {
  /** The main key; requests that carry it as `Authorization: Bearer <key>` are covered. */
  readonly primary?: string | null;
  /** The backup key. */
  readonly backup?: string | null;
  /** URL prefixes of the hosts the keys belong to, such as `https://openrouter.ai/`. */
  readonly hosts: readonly string[];
  /** How long the backup key is used after a key limit, in milliseconds. */
  readonly windowMs: number;
  /** The clock; `Date.now` when not given. */
  readonly now?: () => number;
  /** Called with the refused status when the transport switches to the backup key. */
  readonly onSwitch?: (status: number) => void;
}

const urlOf = (input: unknown): string =>
  typeof input === "string" ? input : input instanceof URL ? input.href : String((input as { url?: unknown } | null)?.url ?? "");
const replayable = (body: unknown) => body == null || typeof body === "string" || body instanceof URLSearchParams;

/**
 * Wraps a fetch so that a request to one of `hosts` with the primary key that the host refuses for a key limit
 * ({@link isKeyLimit}) goes again once with the backup key, and the backup key is used for the next `windowMs`. Other
 * hosts, other keys and other errors pass through unchanged. A body that cannot be sent twice (a stream) is not resent.
 * The resend is part of one request for the caller.
 *
 * @param fetchImpl - The transport to wrap.
 * @param options - The keys, the hosts, the window, the clock and the switch report.
 * @returns The wrapped fetch; `fetchImpl` itself when there is no backup key or it equals the primary key.
 * @throws Never; the wrapped fetch rejects as `fetchImpl` does.
 */
export function withBackupKey(fetchImpl: typeof fetch, options: BackupKeyOptions): typeof fetch {
  const { primary, backup, hosts, windowMs, now = Date.now, onSwitch } = options;
  if (!primary || !backup || primary === backup) return fetchImpl;
  let backupUntil = 0;
  const withKey = (init: RequestInit | undefined, key: string): RequestInit => {
    const headers = new Headers(init?.headers);
    headers.set("Authorization", `Bearer ${key}`);
    return { ...init, headers };
  };
  const usesPrimary = (init: RequestInit | undefined) => new Headers(init?.headers).get("Authorization") === `Bearer ${primary}`;
  return (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = urlOf(input);
    if (!hosts.some((host) => url.startsWith(host)) || !usesPrimary(init)) return fetchImpl(input, init);
    if (now() < backupUntil) return fetchImpl(input, withKey(init, backup));
    const response = await fetchImpl(input, init);
    if (response.ok || !replayable(init?.body)) return response;
    const body = await response.clone().text().catch(() => "");
    if (!isKeyLimit(response.status, body)) return response;
    backupUntil = now() + windowMs;
    onSwitch?.(response.status);
    return fetchImpl(input, withKey(init, backup));
  }) as typeof fetch;
}
