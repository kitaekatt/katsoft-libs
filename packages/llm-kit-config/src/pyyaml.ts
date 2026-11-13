/**
 * One YAML 1.1 document, read as the PyYAML safe loader of the Python reader
 * reads it (`llm_kit.registry.project_models.parse_yaml_document`).
 *
 * The `yaml` package parses the text with its failsafe schema, so every
 * scalar arrives as text with its style and explicit tag. This module then
 * resolves and constructs each value with PyYAML's rules: its implicit
 * resolvers (`on` is true, `y` is text, `0100` is octal, `1.0e3` is text, `1:30`
 * is base 60), its constructors, its composer checks (duplicate anchor,
 * undefined alias) and the Python reader's own refusals (duplicate key, merge
 * key, `!!omap`, `!!pairs`). The result keeps the Python types the
 * validation needs: `bigint` for an integer, {@link Float} for a float,
 * {@link PyMap} for a mapping with keys of any type, and {@link Foreign} for a
 * value that is not JSON data (a set, binary data, a date, a timestamp).
 *
 * Internal module; not part of the public surface.
 *
 * @module
 */

import { Parser, isAlias, isMap, isPair, isScalar, isSeq, parseAllDocuments } from "yaml";
import type { Alias, Node as YamlNode, Pair, Scalar, YAMLMap, YAMLSeq } from "yaml";

import { invalid } from "./errors.js";

/** A YAML float (a Python `float`); kept apart from integers, which are `bigint`. */
export class Float {
  constructor(readonly value: number) {}
}

/** PyYAML builds every `.nan` as one shared object, so two `.nan` keys are the same key. */
const SHARED_NAN = new Float(NaN);

/** A YAML mapping: its pairs in document order, with keys of any type. */
export class PyMap {
  readonly entries: Array<[PyValue, PyValue]> = [];
}

/** A value that is not JSON data. `identity` is its equality key (Python `==`). */
export class Foreign {
  constructor(readonly kind: "set" | "binary" | "date" | "timestamp", readonly identity: string) {}
}

/** A constructed YAML value, with the Python types the validation distinguishes. */
export type PyValue = null | boolean | string | bigint | Float | PyValue[] | PyMap | Foreign;

const TAG = "tag:yaml.org,2002:";

/** PyYAML's printable set (yaml.reader.Reader.NON_PRINTABLE is its complement). */
const NON_PRINTABLE = /[^\t\n\r\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\u{10000}-\u{10ffff}]/u;

/** PyYAML's implicit resolvers, in registration order, with the first characters each is tried for. */
const RESOLVERS: ReadonlyArray<readonly [string, RegExp, string]> = [
  ["bool", /^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/, "yYnNtTfFoO"],
  ["float",
    /^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/,
    "-+0123456789."],
  ["int",
    /^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/,
    "-+0123456789"],
  ["merge", /^(?:<<)$/, "<"],
  ["null", /^(?:~|null|Null|NULL|)$/, "~nN"],
  ["timestamp",
    /^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$/,
    "0123456789"],
  ["value", /^(?:=)$/, "="],
  ["yaml", /^(?:!|&|\*)$/, "!&*"],
];

/** The tag PyYAML resolves a plain scalar to (its `Resolver.resolve` for a scalar node). */
function resolveImplicit(value: string): string {
  const first = value === "" ? "" : value[0]!;
  for (const [tag, pattern, firsts] of RESOLVERS) {
    const tried = first === "" ? tag === "null" : firsts.includes(first);
    if (tried && pattern.test(value)) return TAG + tag;
  }
  return `${TAG}str`;
}

/** A YAML problem: the Python reader reports every one of them at the path `""`. */
class YamlProblem extends Error {}

function fail(message: string): never {
  throw new YamlProblem(message);
}

const TIMESTAMP =
  /^([0-9][0-9][0-9][0-9])-([0-9][0-9]?)-([0-9][0-9]?)(?:(?:[Tt]|[ \t]+)([0-9][0-9]?):([0-9][0-9]):([0-9][0-9])(?:\.([0-9]*))?(?:[ \t]*(Z|([-+])([0-9][0-9]?)(?::([0-9][0-9]))?))?)?$/;

function daysIn(year: number, month: number): number {
  if (month === 2) return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

/** PyYAML's `construct_yaml_timestamp`: a `datetime.date` or a `datetime.datetime`. */
function constructTimestamp(value: string): Foreign {
  const match = TIMESTAMP.exec(value) ?? fail(`${JSON.stringify(value)} is not a timestamp`);
  const [, y, mo, d, h, mi, s, fraction, tz, sign, tzHour, tzMinute] = match;
  const year = Number(y);
  const month = Number(mo);
  const day = Number(d);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysIn(year, month)) fail(`${value} is not a date`);
  if (h === undefined) return new Foreign("date", `D${year}-${month}-${day}`);
  const hour = Number(h);
  const minute = Number(mi);
  const second = Number(s);
  if (hour > 23 || minute > 59 || second > 59) fail(`${value} is not a time`);
  const micro = fraction ? Number(fraction.slice(0, 6).padEnd(6, "0")) : 0;
  if (tz === undefined) return new Foreign("timestamp", `N${year},${month},${day},${hour},${minute},${second},${micro}`);
  let offset = 0;
  if (sign !== undefined) {
    offset = (Number(tzHour) * 60 + Number(tzMinute ?? 0)) * (sign === "-" ? -1 : 1);
    if (Math.abs(offset) >= 24 * 60) fail(`${value} has an offset of a day or more`);
  }
  const instant = new Date(0);
  instant.setUTCFullYear(year, month - 1, day);
  instant.setUTCHours(hour, minute - offset, second, 0);
  const utc = BigInt(instant.getTime()) * 1000n + BigInt(micro);
  return new Foreign("timestamp", `A${utc}`);
}

const INT_DIGITS: Readonly<Record<number, RegExp>> = { 2: /^[01]+$/, 8: /^[0-7]+$/, 10: /^[0-9]+$/, 16: /^[0-9a-f]+$/i };
const INT_PREFIX: Readonly<Record<number, string>> = { 2: "0b", 8: "0o", 10: "", 16: "0x" };

/** Python's `int(text, base)` for the ASCII forms (sign, base prefix, surrounding space). */
function pythonInt(text: string, base: 2 | 8 | 10 | 16): bigint {
  const match = /^\s*([-+]?)(.*?)\s*$/s.exec(text)!;
  let body = match[2]!;
  const prefix = INT_PREFIX[base]!;
  if (prefix && body.toLowerCase().startsWith(prefix)) body = body.slice(2);
  if (!INT_DIGITS[base]!.test(body)) fail(`${JSON.stringify(text)} is not an integer in base ${base}`);
  const value = BigInt(`${prefix || ""}${body}`);
  return match[1] === "-" ? -value : value;
}

/** PyYAML's `construct_yaml_int`. */
function constructInt(text: string): bigint {
  let value = text.replaceAll("_", "");
  if (value === "") fail("an empty integer");
  let sign = 1n;
  if (value.startsWith("-")) sign = -1n;
  if (value.startsWith("-") || value.startsWith("+")) value = value.slice(1);
  if (value === "0") return 0n;
  if (value.startsWith("0b")) return sign * pythonInt(value.slice(2), 2);
  if (value.startsWith("0x")) return sign * pythonInt(value.slice(2), 16);
  if (value.startsWith("0")) return sign * pythonInt(value, 8);
  if (value.includes(":")) {
    let total = 0n;
    let base = 1n;
    for (const part of value.split(":").reverse()) {
      total += pythonInt(part, 10) * base;
      base *= 60n;
    }
    return sign * total;
  }
  return sign * pythonInt(value, 10);
}

const PY_FLOAT = /^\s*[-+]?(?:(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|inf(?:inity)?|nan)\s*$/i;

/** Python's `float(text)` for the forms PyYAML passes it. */
function pythonFloat(text: string): number {
  if (!PY_FLOAT.test(text)) fail(`${JSON.stringify(text)} is not a float`);
  const body = text.trim().toLowerCase().replace(/^\+/, "");
  if (/^-?inf(inity)?$/.test(body)) return body.startsWith("-") ? -Infinity : Infinity;
  if (/^-?nan$/.test(body)) return NaN;
  return Number(body);
}

/** PyYAML's `construct_yaml_float`. */
function constructFloat(text: string): Float {
  let value = text.replaceAll("_", "").toLowerCase();
  if (value === "") fail("an empty float");
  let sign = 1;
  if (value.startsWith("-")) sign = -1;
  if (value.startsWith("-") || value.startsWith("+")) value = value.slice(1);
  if (value === ".inf") return new Float(sign * Infinity);
  if (value === ".nan") return SHARED_NAN;
  if (value.includes(":")) {
    let total = 0;
    let base = 1;
    for (const part of value.split(":").reverse()) {
      total += pythonFloat(part) * base;
      base *= 60;
    }
    return new Float(sign * total);
  }
  return new Float(sign * pythonFloat(value));
}

const BOOLS: Readonly<Record<string, boolean>> = { yes: true, no: false, true: true, false: false, on: true, off: false };

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/** Python's `base64.decodebytes` (non-strict `binascii.a2b_base64`); returns the bytes as hex. */
function decodeBase64(text: string): string {
  if (!/^[\x00-\x7f]*$/.test(text)) fail("binary data must be ASCII");
  let quad = 0;
  let pads = 0;
  let bits = 0;
  let acc = 0;
  let out = "";
  for (const char of text) {
    if (char === "=") {
      if (quad >= 2 && quad + ++pads >= 4) return out;
      continue;
    }
    const index = B64.indexOf(char);
    if (index < 0) continue;
    pads = 0;
    acc = (acc << 6) | index;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out += ((acc >> bits) & 0xff).toString(16).padStart(2, "0");
    }
    quad = (quad + 1) % 4;
  }
  if (quad === 1) fail("binary data has one data character more than a multiple of 4");
  if (quad !== 0) fail("binary data has incorrect padding");
  return out;
}

/** The Python equality key of a hashable value, or `undefined` for a value that is not hashable. */
function identity(value: PyValue, unique: () => string): string | undefined {
  if (value === null) return "N";
  if (typeof value === "boolean") return value ? "I1" : "I0";
  if (typeof value === "bigint") return `I${value}`;
  if (typeof value === "string") return `S${value}`;
  if (value instanceof Float) {
    const number = value.value;
    if (Number.isNaN(number)) return value === SHARED_NAN ? "FNaN" : unique();
    if (!Number.isFinite(number)) return `F${number}`;
    return Number.isInteger(number) ? `I${BigInt(number)}` : `F${number}`;
  }
  if (value instanceof Foreign && value.kind !== "set") return value.identity;
  return undefined;
}

type Composed = YamlNode | Pair | null;

/** Composer and constructor for one document; anchors and constructed nodes live here. */
class Builder {
  private readonly anchors = new Map<string, YamlNode>();
  private readonly built = new Map<YamlNode, PyValue>();
  private nan = 0;

  value(node: Composed, isKey = false): PyValue {
    if (node === null) return null;
    if (isAlias(node)) return this.alias(node);
    if (isPair(node)) {
      // A single pair in a flow sequence ([a: 1]) is a one-pair mapping.
      const map = new PyMap();
      this.pairs(map, [node]);
      return map;
    }
    const anchor = (node as { anchor?: string }).anchor;
    if (anchor !== undefined) {
      if (this.anchors.has(anchor)) fail(`duplicate anchor ${anchor}`);
      this.anchors.set(anchor, node);
    }
    if (isScalar(node)) {
      const value = this.scalar(node, isKey);
      this.built.set(node, value);
      return value;
    }
    if (isMap(node)) return this.map(node);
    if (isSeq(node)) return this.seq(node);
    return fail("unknown YAML node");
  }

  private alias(node: Alias): PyValue {
    const target = this.anchors.get(node.source) ?? fail(`undefined alias ${node.source}`);
    if (!this.built.has(target)) fail(`alias ${node.source} refers to a node that cannot be built yet`);
    return this.built.get(target)!;
  }

  /** A scalar; as a mapping key, PyYAML's `flatten_mapping` reads the `=` value tag as text. */
  private scalar(node: Scalar, isKey: boolean): PyValue {
    const text = String(node.source ?? node.value ?? "");
    const explicit = node.tag;
    const implicit = explicit === "!" || (explicit === undefined && node.type === "PLAIN");
    let tag = implicit ? resolveImplicit(text) : explicit ?? `${TAG}str`;
    if (isKey && tag === `${TAG}value`) tag = `${TAG}str`;
    switch (tag) {
      case `${TAG}str`: return text;
      case `${TAG}null`: return null;
      case `${TAG}bool`: {
        const word = text.toLowerCase();
        return Object.hasOwn(BOOLS, word) ? BOOLS[word]! : fail(`${JSON.stringify(text)} is not a boolean`);
      }
      case `${TAG}int`: return constructInt(text);
      case `${TAG}float`: return constructFloat(text);
      case `${TAG}binary`: return new Foreign("binary", `B${decodeBase64(text)}`);
      case `${TAG}timestamp`: return constructTimestamp(text);
      case `${TAG}merge`: return fail("merge keys (<<) are not supported; write the keys out");
      default: return fail(`no constructor for the tag ${tag} on a scalar`);
    }
  }

  private map(node: YAMLMap): PyValue {
    const tag = node.tag === undefined || node.tag === "!" ? `${TAG}map` : node.tag;
    if (tag === `${TAG}map`) {
      const map = new PyMap();
      this.built.set(node, map);
      this.pairs(map, node.items as Pair[]);
      return map;
    }
    if (tag === `${TAG}set`) {
      const set = new Foreign("set", "");
      this.built.set(node, set);
      this.pairs(new PyMap(), node.items as Pair[]);
      return set;
    }
    return fail(`no constructor for the tag ${tag} on a mapping`);
  }

  private seq(node: YAMLSeq): PyValue {
    const tag = node.tag === undefined || node.tag === "!" ? `${TAG}seq` : node.tag;
    if (tag !== `${TAG}seq`) fail(`no constructor for the tag ${tag} on a sequence`);
    const list: PyValue[] = [];
    this.built.set(node, list);
    for (const item of node.items) list.push(this.value(item as Composed));
    return list;
  }

  /**
   * Build the pairs into `map`. An empty implicit key (`: v`), an unhashable
   * key and a duplicate key are refused; a merge key (`<<`) is refused when
   * its scalar is built.
   */
  private pairs(map: PyMap, pairs: Pair[]): void {
    const seen = new Set<string>();
    for (const pair of pairs) {
      const keyNode = pair.key as Composed;
      const empty = keyNode === null
        || (isScalar(keyNode) && keyNode.type === "PLAIN" && keyNode.tag === undefined && keyNode.source === "");
      if (empty) fail("a mapping key is empty");
      const key = this.value(keyNode, true);
      const id = identity(key, () => `NaN${this.nan++}`) ?? fail("a mapping key is not hashable");
      if (seen.has(id)) fail("duplicate mapping key");
      seen.add(id);
      map.entries.push([key, this.value(pair.value as Composed)]);
    }
  }
}

interface SourceToken {
  readonly type: string;
  readonly offset: number;
  readonly source: string;
  readonly props?: readonly SourceToken[];
  /** Set by {@link sourceTokens}: the token is inside a flow collection. */
  flow?: boolean;
}

/** Every token of the concrete syntax tree that has a type, an offset and source text. */
function sourceTokens(node: unknown, flow = false, out: SourceToken[] = []): SourceToken[] {
  if (Array.isArray(node)) {
    for (const item of node) sourceTokens(item, flow, out);
  } else if (node !== null && typeof node === "object") {
    const token = node as Partial<SourceToken>;
    const inner = flow || token.type === "flow-collection";
    if (typeof token.type === "string" && typeof token.offset === "number" && typeof token.source === "string") {
      out.push({ ...(token as SourceToken), flow: inner });
    }
    for (const value of Object.values(node)) if (value !== null && typeof value === "object") sourceTokens(value, inner, out);
  }
  return out;
}

const AFTER_ANCHOR = new Set([..."\0 \t\n?:,]}%@`"]);

/**
 * PyYAML's scanner is stricter than YAML 1.2 in a few places; refuse what it refuses:
 * a tab outside a quoted scalar, a block scalar's text or a comment; a plain
 * scalar that starts with `?` or `:` in a flow collection (PyYAML reads those
 * as indicators, and its result differs or it refuses); a tag not
 * followed by a space or a line break; an anchor or alias name that is not ASCII
 * letters, digits, `-` and `_`; a `%YAML` directive that is not 1.x or repeats.
 */
function checkScanner(text: string): void {
  const tokens = sourceTokens([...new Parser().parse(text)]);
  const tabsAllowed: Array<[number, number]> = [];
  let yamlDirectives = 0;
  for (const token of tokens) {
    const end = token.offset + token.source.length;
    switch (token.type) {
      case "comment":
      case "single-quoted-scalar":
      case "double-quoted-scalar":
        tabsAllowed.push([token.offset, end]);
        break;
      case "block-scalar": {
        const header = token.props?.at(-1);
        const start = header ? header.offset + header.source.length : token.offset;
        tabsAllowed.push([start, start + token.source.length]);
        break;
      }
      case "scalar":
        if (token.flow && /^[?:]/.test(token.source)) fail(`${token.source[0]} cannot start a plain scalar in a flow collection`);
        break;
      case "tag":
        if (end < text.length && text[end] !== " " && text[end] !== "\n") fail(`the tag ${token.source} must be followed by a space`);
        break;
      case "anchor":
      case "alias":
        if (!/^[&*][0-9A-Za-z_-]+$/.test(token.source) || (end < text.length && !AFTER_ANCHOR.has(text[end]!))) {
          fail(`the ${token.type} ${token.source} must use only ASCII letters, digits, - and _`);
        }
        break;
      case "document":
        yamlDirectives = 0;
        break;
      case "directive":
        if (token.source.startsWith("%YAML")) {
          yamlDirectives += 1;
          if (yamlDirectives > 1) fail("duplicate %YAML directive");
          if (!/^%YAML +1\.[0-9]+ *(?:#.*)?$/.test(token.source)) fail(`unsupported directive ${token.source} (version 1.* is required)`);
        }
        break;
      default:
        break;
    }
  }
  for (let at = text.indexOf("\t"); at >= 0; at = text.indexOf("\t", at + 1)) {
    if (!tabsAllowed.some(([start, end]) => start <= at && at < end)) fail("a tab cannot start a token (PyYAML reads tabs only inside quoted or block scalars and comments)");
  }
}

/**
 * Read `text` (already decoded) as one YAML document with PyYAML's rules.
 * Throws an `invalid` {@link LlmKitConfigError} at the path `""` for text
 * that is not one non-empty YAML document under those rules.
 */
export function loadYaml(text: string, source?: string): PyValue {
  const bad = NON_PRINTABLE.exec(text);
  if (bad) {
    const code = bad[0].codePointAt(0)!.toString(16).padStart(4, "0");
    throw invalid("", `the file is not valid YAML: special character U+${code} is not allowed`, source);
  }
  if (/[\u2028\u2029]/.test(text)) {
    throw invalid("", "the file is not supported: it holds U+2028 or U+2029, which PyYAML reads as a line break", source);
  }
  // PyYAML reads CRLF, CR and NEL as LF everywhere. Both parsers skip one leading byte order mark.
  const normalized = text.replace(/\r\n?|\x85/g, "\n");
  let value: PyValue;
  try {
    checkScanner(normalized);
    const documents = parseAllDocuments(normalized, {
      version: "1.1", schema: "failsafe", merge: false, uniqueKeys: false, strict: true, prettyErrors: false,
    });
    if (!Array.isArray(documents) || documents.length === 0) throw new YamlProblem("the file is empty");
    if (documents.length > 1) throw new YamlProblem("the file holds more than one YAML document");
    const document = documents[0]!;
    if (document.errors.length > 0) throw new YamlProblem(document.errors[0]!.message);
    value = new Builder().value(document.contents as Composed);
  } catch (error) {
    if (error instanceof YamlProblem) throw invalid("", `the file is not valid YAML: ${error.message}`, source);
    throw error;
  }
  if (value === null) throw invalid("", "the file is empty", source);
  return value;
}
