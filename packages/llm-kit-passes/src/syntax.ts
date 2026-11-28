// A syntax registry, its check, and an audience filter that strips the spans an audience does not see. The format, its
// scanner and its kinds belong to the caller; this module knows only spans, kinds, audiences and line separators.

/** One form of a format: a unique id, the operation it writes, and the kind that decides who sees it. */
export interface SyntaxForm<Op extends string = string, Kind extends string = string> {
  readonly id: string;
  readonly op: Op;
  readonly kind: Kind;
}

/**
 * A format's registry: every kind, every audience, and every form. Several forms can share one `op`; each `id` is
 * unique, and each form declares one of `kinds`. `checkSyntaxRegistry` checks these rules.
 */
export interface SyntaxRegistry<Op extends string = string, Kind extends string = string, Audience extends string = string> {
  readonly kinds: readonly Kind[];
  readonly audiences: readonly Audience[];
  readonly forms: readonly SyntaxForm<Op, Kind>[];
}

/** Which kinds each audience does not see: one entry for every audience, an empty list when it sees everything. */
export type SyntaxPolicy<Kind extends string = string, Audience extends string = string> = { readonly [A in Audience]: readonly Kind[] };

/** What `checkSyntaxRegistry` can report. */
export type SyntaxProblemCode =
  | "duplicate-kind" | "duplicate-audience" | "bad-form" | "unknown-kind" | "duplicate-id"
  | "missing-audience" | "bad-policy" | "unknown-hidden-kind" | "unknown-audience";

/**
 * One problem `checkSyntaxRegistry` found: its code, what it is about (a form id, a form index, a kind or an audience),
 * and one line for a person.
 */
export interface SyntaxProblem {
  readonly code: SyntaxProblemCode;
  readonly subject: string;
  readonly line: string;
}

/**
 * One recognized form in a stream, as UTF-16 offsets: `stream.slice(start, end)` is the form's whole text, from its
 * opening delimiter to its closing one (or to the end of the stream when it is not closed). `start < end`.
 */
export interface SyntaxSpan<Kind extends string = string> {
  readonly start: number;
  readonly end: number;
  readonly kind: Kind;
}

/**
 * The caller's scanner: every form it recognizes in `stream`, in stream order, with no two spans overlapping. It gives
 * the forms every audience sees too, so that the text inside them is never scanned again. Text that is not a span is
 * copied as it is.
 */
export type SyntaxScan<Kind extends string = string> = (stream: string) => Iterable<SyntaxSpan<Kind>>;

/** The options of `audienceFilter`: the caller's scanner and the policy. */
export interface AudienceFilterOptions<Kind extends string = string, Audience extends string = string> {
  readonly scan: SyntaxScan<Kind>;
  readonly policy: SyntaxPolicy<Kind, Audience>;
}

/** A filter from `audienceFilter`: the stream with every span the audience does not see removed. */
export type AudienceFilter<Audience extends string = string> = (stream: string, audience: Audience) => string;

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const isName = (value: unknown): value is string => typeof value === "string" && value.length > 0;

/** Each value listed more than once, with its count, in first-seen order. */
function repeats(values: readonly unknown[]): Map<unknown, number> {
  const counts = new Map<unknown, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return new Map([...counts].filter(([, count]) => count > 1));
}

/**
 * Check a registry and its policy. The rules: no kind or audience is listed twice; each form has a non-empty string
 * `id`, `op` and `kind`; each form declares a listed kind; no two forms share an id; the policy has an entry for every
 * listed audience and for no other, and each entry is a list of listed kinds.
 *
 * @param registry The registry: `kinds`, `audiences` and `forms`, each a list.
 * @param policy The kinds each audience does not see, as an object with one entry for each audience.
 * @returns The problems in a fixed order (kinds, audiences, forms by index, repeated ids, then the policy by audience);
 *   an empty list when the registry and policy are sound. Problems in the data are returned, never thrown.
 * @throws {TypeError} When `registry` is not an object with the three lists, or `policy` is not an object.
 */
export function checkSyntaxRegistry<Op extends string, Kind extends string, Audience extends string>(
  registry: SyntaxRegistry<Op, Kind, Audience>,
  policy: SyntaxPolicy<Kind, Audience>,
): SyntaxProblem[] {
  if (!isObject(registry)) throw new TypeError("registry must be an object");
  for (const field of ["kinds", "audiences", "forms"] as const) {
    if (!Array.isArray(registry[field])) throw new TypeError(`registry.${field} must be an array`);
  }
  if (!isObject(policy)) throw new TypeError("policy must be an object");
  const problems: SyntaxProblem[] = [];
  const report = (code: SyntaxProblemCode, subject: string, line: string): void => void problems.push({ code, subject, line });

  for (const [kind, count] of repeats(registry.kinds)) report("duplicate-kind", String(kind), `the kind ${String(kind)} is listed ${count} times`);
  for (const [audience, count] of repeats(registry.audiences)) {
    report("duplicate-audience", String(audience), `the audience ${String(audience)} is listed ${count} times`);
  }

  const kinds = new Set<unknown>(registry.kinds);
  const ids: string[] = [];
  registry.forms.forEach((form: unknown, index) => {
    if (!isObject(form) || !isName(form.id) || !isName(form.op) || !isName(form.kind)) {
      report("bad-form", String(index), `form ${index} needs a non-empty string id, op and kind`);
      return;
    }
    ids.push(form.id);
    if (!kinds.has(form.kind)) report("unknown-kind", form.id, `form ${form.id} declares the unknown kind ${form.kind}`);
  });
  for (const [id, count] of repeats(ids)) report("duplicate-id", String(id), `form id ${String(id)} is used by ${count} forms`);

  const entries: Record<string, unknown> = policy;
  for (const audience of new Set(registry.audiences)) {
    if (!Object.hasOwn(entries, audience)) {
      report("missing-audience", audience, `the policy has no entry for the audience ${audience}`);
      continue;
    }
    const hidden = entries[audience];
    if (!Array.isArray(hidden)) {
      report("bad-policy", audience, `the policy for ${audience} must be a list of kinds`);
      continue;
    }
    for (const kind of hidden) {
      if (!kinds.has(kind)) report("unknown-hidden-kind", audience, `the policy for ${audience} hides the unknown kind ${String(kind)}`);
    }
  }
  const audiences = new Set<string>(registry.audiences);
  for (const audience of Object.keys(entries)) {
    if (!audiences.has(audience)) report("unknown-audience", audience, `the policy names the unknown audience ${audience}`);
  }
  return problems;
}

/**
 * Make a filter that removes, for one audience, every span whose kind the policy hides from it. The caller's scanner
 * finds the spans; the text outside hidden spans is kept byte for byte.
 *
 * The whole-line rule: when a removed span fills a whole line (it starts at the start of the stream or right after a
 * `"\n"`, and ends at the end of the stream or right before a `"\n"`), one line separator goes with it, so that no empty
 * line is left. That is the `"\n"` after the span; for a span on the last line, it is the `"\n"` that ends the output so
 * far. A span that shares its line with other text removes no separator. For an audience that hides no kind, the
 * filter returns the stream as is and does not call the scanner.
 *
 * @param options `scan`, the caller's scanner, and `policy`, the kinds each audience does not see.
 * @returns A function `(stream, audience) => string`. It throws a TypeError when `stream` is not a string, when the
 *   policy has no entry for `audience`, or when the scanner gives a span that is not an object with integer offsets
 *   `0 <= start < end <= stream.length` and a string kind, or that starts before the end of the span before it.
 * @throws {TypeError} When `scan` is not a function, or `policy` is not an object whose entries are lists of strings.
 */
export function audienceFilter<Kind extends string, Audience extends string>(
  options: AudienceFilterOptions<Kind, Audience>,
): AudienceFilter<Audience> {
  if (!isObject(options)) throw new TypeError("options must be an object");
  const { scan, policy } = options;
  if (typeof scan !== "function") throw new TypeError("options.scan must be a function");
  if (!isObject(policy)) throw new TypeError("options.policy must be an object");
  const hidden = new Map<string, ReadonlySet<string>>();
  for (const [audience, kinds] of Object.entries(policy as Record<string, unknown>)) {
    if (!Array.isArray(kinds) || !kinds.every((kind) => typeof kind === "string")) {
      throw new TypeError(`options.policy.${audience} must be a list of kinds`);
    }
    hidden.set(audience, new Set(kinds));
  }

  return (stream: string, audience: Audience): string => {
    if (typeof stream !== "string") throw new TypeError("stream must be a string");
    const hide = hidden.get(audience);
    if (!hide) throw new TypeError(`the policy has no entry for the audience ${String(audience)}`);
    if (hide.size === 0) return stream;
    let result = "";
    let cursor = 0; // The first stream offset not yet copied or removed.
    let previousEnd = 0;
    for (const span of scan(stream)) {
      if (!isObject(span) || !Number.isInteger(span.start) || !Number.isInteger(span.end) || typeof span.kind !== "string"
        || (span.start as number) < previousEnd || (span.start as number) >= (span.end as number) || (span.end as number) > stream.length) {
        throw new TypeError(`scan gave a bad span: ${JSON.stringify(span)}`);
      }
      const { start, end, kind } = span as unknown as SyntaxSpan;
      previousEnd = end;
      if (!hide.has(kind)) continue; // A kept span is copied with the text around it.
      result += stream.slice(cursor, start);
      cursor = end;
      const wholeLine = (start === 0 || stream[start - 1] === "\n") && (end === stream.length || stream[end] === "\n");
      if (!wholeLine) continue;
      if (stream[end] === "\n") cursor = end + 1;
      else if (result.endsWith("\n")) result = result.slice(0, -1);
    }
    return result + stream.slice(cursor);
  };
}
