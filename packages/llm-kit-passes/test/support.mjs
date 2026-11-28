// Shared helpers of the tests: the package paths, scripted transports and timing helpers. No network.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

/** The package name, from package.json: the one place it is written for the tests. */
export const NAME = readJson(resolve(PACKAGE, "package.json")).name;

/** A promise that resolves after `ms` milliseconds. */
export const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

/** A promise that rejects with the signal's reason when `signal` aborts, and never settles otherwise. */
export const untilAborted = (signal) => new Promise((_, reject) => {
  if (signal.aborted) reject(signal.reason);
  else signal.addEventListener("abort", () => reject(signal.reason), { once: true });
});

/** A promise with its resolve and reject functions. */
export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/** One forced-tool answer: `{text, toolCalls}` with one call of `name` whose arguments are `args` (a string is sent as is). */
export const answer = (args, { id = "call-1", name = "submit", text = "" } = {}) => ({
  text,
  toolCalls: [{ id, type: "function", function: { name, arguments: typeof args === "string" ? args : JSON.stringify(args) } }],
});

/**
 * A scripted transport: each call is recorded (its input, with a JSON copy of the messages it was given) and answered
 * by the next step. A step is a value, a function of the input, or an Error to throw.
 */
export function scripted(...steps) {
  const calls = [];
  const call = async (input) => {
    calls.push({ ...input, sent: JSON.stringify(input.messages) });
    const step = steps[calls.length - 1];
    if (step === undefined) throw new Error(`no step for call ${calls.length}`);
    if (step instanceof Error) throw step;
    return typeof step === "function" ? step(input) : step;
  };
  return { call, calls };
}
