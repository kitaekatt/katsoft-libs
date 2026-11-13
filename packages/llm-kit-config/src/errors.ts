/**
 * The one error type of this package.
 *
 * @module
 */

/**
 * The kind of problem an {@link LlmKitConfigError} reports.
 *
 * - `invalid`: the content of a file or text is wrong (not UTF-8, not YAML,
 *   a broken rule of the format, a reference to a missing catalog id).
 * - `missing`: a file to read does not exist.
 * - `io`: a file exists but cannot be read (permissions, a directory, a
 *   read failure). The original error is in `cause`.
 * - `unknown-selection`: {@link select} was asked for a key that no layer names.
 * - `slot`: {@link renderPrompt} was given values that do not match the slots.
 */
export type LlmKitConfigErrorCode = "invalid" | "missing" | "io" | "unknown-selection" | "slot";

/**
 * A refusal from this package. Every function fails on the first problem it
 * finds and throws this error; nothing falls back to a default.
 *
 * `code` says what kind of problem it is (see {@link LlmKitConfigErrorCode}).
 * `where` names the first problem inside the content: for the project model
 * files it is the same path the Python reader reports (`""` for the bytes or
 * the YAML as a whole, `document`, `version`, `catalog[1].efforts[0]`,
 * `selections['draft'].effort`); for a prompt it is `""` for the file, a slot
 * name for a `slot` refusal, or `suffix`. `source` is the path or name of the
 * text, when one is known.
 */
export class LlmKitConfigError extends Error {
  /** The kind of problem. */
  readonly code: LlmKitConfigErrorCode;
  /** The path of the first problem inside the content; `""` for the content as a whole. */
  readonly where: string;
  /** The path or name of the text that was read, when known. */
  readonly source?: string;

  /**
   * Create a refusal.
   *
   * @param code - The kind of problem.
   * @param message - A sentence for people; it is not part of the contract.
   * @param where - The path of the problem inside the content.
   * @param source - The path or name of the text, when known.
   * @param cause - The underlying error of an `io` or `missing` refusal.
   */
  constructor(code: LlmKitConfigErrorCode, message: string, where = "", source?: string, cause?: unknown) {
    const at = [source, where].filter((part) => part).join(": ");
    super(at ? `${at}: ${message}` : message, cause === undefined ? undefined : { cause });
    this.name = "LlmKitConfigError";
    this.code = code;
    this.where = where;
    if (source !== undefined) this.source = source;
  }
}

/** An `invalid` refusal at `where`. */
export function invalid(where: string, message: string, source?: string): LlmKitConfigError {
  return new LlmKitConfigError("invalid", message, where, source);
}

/** The same refusal with `source` set (used when the source is known only to the caller). */
export function withSource(error: LlmKitConfigError, source: string | undefined): LlmKitConfigError {
  if (source === undefined || error.source !== undefined) return error;
  const message = error.where && error.message.startsWith(`${error.where}: `)
    ? error.message.slice(error.where.length + 2)
    : error.message;
  return new LlmKitConfigError(error.code, message, error.where, source, error.cause);
}
