// submitToolPass: one forced tool call, checked by the caller, resubmitted with the check's problems, under one deadline.
import { openScope, requirePositive, started, STOPPED } from "./scope.js";

/** One chat message as the transport sends it (OpenAI chat-completions roles and fields). The pass copies messages and never changes one. */
export type PassMessage = Readonly<Record<string, unknown>>;

/** The tool the model must call: its name, its description, and the JSON Schema of its arguments. The pass gives it to the transport as is. */
export interface PassTool {
  readonly name: string;
  readonly description: string;
  readonly schema: Readonly<Record<string, unknown>>;
}

/** One tool call of an answer, in the OpenAI wire shape (the `toolCalls` of an `@katsoft/llm-kit-send` answer fit as is). */
export interface PassToolCall {
  readonly id?: string | undefined;
  readonly type?: string | undefined;
  readonly function: { readonly name?: string | undefined; readonly arguments: string };
}

/** What the pass gives the transport for one attempt. `messages` is a new array for each attempt. */
export interface PassCallInput {
  readonly messages: readonly PassMessage[];
  readonly tool: PassTool;
  /** The name of the tool the model must call (the tool's own name). */
  readonly toolChoice: string;
  /** Aborts at the deadline (reason `TimeoutError`) or when the caller's signal aborts. */
  readonly signal: AbortSignal;
  /** 1 for the first call, 2 for the first resubmit. */
  readonly attempt: number;
}

/** What the transport answers: the reply text (empty or null when there is none) and the tool calls. */
export interface PassCallOutput {
  readonly text?: string | null | undefined;
  readonly toolCalls: readonly PassToolCall[];
}

/**
 * The injected transport: send `input.messages` with `input.tool` forced and answer the text and tool calls.
 * It throws (or rejects) on a failure; the pass then ends as `unavailable` and does not resubmit.
 */
export type PassCall = (input: PassCallInput) => Promise<PassCallOutput>;

/** One problem the check found: an id the caller chooses and the line the model is told. */
export interface PassProblem {
  readonly id: string;
  readonly line: string;
}

/** What the check gives back: the new state and the problems (an empty list accepts the answer). */
export interface PassChecked<S> {
  readonly state: S;
  readonly problems: readonly PassProblem[];
}

/**
 * The caller's check of one answer: `args` is the parsed JSON of the tool call's arguments, or `null` when they are not
 * JSON; `state` is the state after the attempt before (the initial state on attempt 1). It runs synchronously.
 */
export type PassCheck<S> = (args: unknown, state: S, attempt: number) => PassChecked<S>;

/** What `onAttempt` is told after each attempt. `arguments` is the tool call's raw arguments, or `null` when the call failed. */
export interface ToolPassAttempt {
  readonly attempt: number;
  readonly outcome: "checked" | "unavailable" | "timeout";
  readonly arguments: string | null;
  /** The check's problems for a checked attempt; empty otherwise. */
  readonly problems: readonly PassProblem[];
  /** The transport's error for an unavailable attempt. */
  readonly error?: unknown;
}

/** The inputs of `submitToolPass`: the conversation, the tool, the transport, the check, the state and the limits. */
export interface ToolPassOptions<S> {
  /** The conversation to start from (for example a system and a user message). It is copied, never changed. */
  readonly messages: readonly PassMessage[];
  readonly tool: PassTool;
  readonly call: PassCall;
  readonly check: PassCheck<S>;
  /** The tool message content for an answer with problems. Default: `JSON.stringify({errors: problems.map(p => p.line)})`. */
  readonly feedback?: ((state: S, problems: readonly PassProblem[]) => string) | undefined;
  /** The initial state the first check gets. */
  readonly state: S;
  /** How many times an answer with problems is resubmitted (an integer, 0 or more). Default 1. */
  readonly maxResubmits?: number | undefined;
  /** One deadline in milliseconds over every attempt, counted from the start of the pass. */
  readonly deadlineMs: number;
  /** The caller's signal: its abort stops the pass. */
  readonly signal?: AbortSignal | undefined;
  readonly onAttempt?: ((event: ToolPassAttempt) => void) | undefined;
}

/**
 * How a pass ended: `accepted` (the check found no problems), `rejected` (problems after the last resubmit), `timeout`
 * (the deadline passed) or `unavailable` (the transport failed, or its answer was not one call of the tool).
 */
export type ToolPassStatus = "accepted" | "rejected" | "timeout" | "unavailable";

/** The result of `submitToolPass`. `state` and `problems` are those of the last check (the initial state and no problems before any check). */
export interface ToolPassResult<S> {
  readonly status: ToolPassStatus;
  readonly state: S;
  readonly problems: readonly PassProblem[];
  /** How many calls the pass started. */
  readonly attempts: number;
  /** The whole conversation of the pass: the start, then an assistant and a tool message for each checked attempt. */
  readonly messages: readonly PassMessage[];
  /** The transport's error, for `unavailable`. */
  readonly error?: unknown;
}

const defaultFeedback = (_state: unknown, problems: readonly PassProblem[]): string =>
  JSON.stringify({ errors: problems.map((problem) => problem.line) });

function parsed(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function validate<S>(options: ToolPassOptions<S>): void {
  if (!Array.isArray(options.messages)) throw new TypeError("messages must be an array");
  const tool = options.tool;
  if (!tool || typeof tool.name !== "string" || !tool.name || typeof tool.description !== "string" || !tool.schema || typeof tool.schema !== "object") {
    throw new TypeError("tool must be {name, description, schema} with a non-empty name");
  }
  if (typeof options.call !== "function" || typeof options.check !== "function") throw new TypeError("call and check must be functions");
  if (options.feedback !== undefined && typeof options.feedback !== "function") throw new TypeError("feedback must be a function");
  const resubmits = options.maxResubmits ?? 1;
  if (!Number.isInteger(resubmits) || resubmits < 0) throw new TypeError("maxResubmits must be an integer, 0 or more");
  requirePositive("deadlineMs", options.deadlineMs);
}

/**
 * Run one tool pass: send the conversation with the tool forced, give the call's arguments to the caller's check, and
 * resubmit with the check's problems until the check accepts or `maxResubmits` resubmits have run. The pass owns its
 * conversation: after each checked attempt it appends the assistant message
 * `{role: "assistant", content: text || null, tool_calls: [{id, type: "function", function: {name, arguments}}]}` and
 * the tool message `{role: "tool", tool_call_id: id, content}`, where content is `"accepted"` or `feedback(state, problems)`.
 * It resubmits only for problems: never after a transport failure, a wrong answer or the deadline. One deadline covers
 * every attempt; at the deadline the attempt's signal aborts and an answer that comes later is dropped.
 *
 * @param options The conversation, the tool, the transport, the check, the state and the limits (`ToolPassOptions`).
 * @returns The status, the last state and problems, the number of attempts, the conversation and, for `unavailable`, the error.
 * @throws {TypeError} When an option is not valid, before any call, or when the check gives no `problems` array or
 *   `feedback` gives no string. Rejects with the caller's `signal.reason` when the caller's signal aborts. An error
 *   thrown by `check`, `feedback` or `onAttempt` is passed on.
 */
export async function submitToolPass<S>(options: ToolPassOptions<S>): Promise<ToolPassResult<S>> {
  validate(options);
  const { tool, call, check, signal: outer, onAttempt } = options;
  const feedback = options.feedback ?? defaultFeedback;
  const maxResubmits = options.maxResubmits ?? 1;
  outer?.throwIfAborted();
  const messages: PassMessage[] = [...options.messages];
  let state = options.state;
  let problems: readonly PassProblem[] = [];
  const finish = (status: ToolPassStatus, attempts: number, error?: unknown): ToolPassResult<S> => ({
    status, state, problems, attempts, messages: [...messages], ...(status === "unavailable" ? { error } : {}),
  });
  const scope = openScope(options.deadlineMs, outer);
  try {
    for (let attempt = 1; ; attempt += 1) {
      let output: PassCallOutput;
      try {
        if (scope.signal.aborted) throw STOPPED;
        const input: PassCallInput = { messages: [...messages], tool, toolChoice: tool.name, signal: scope.signal, attempt };
        output = await Promise.race([started(() => call(input)), scope.stopped]);
        if (scope.signal.aborted) throw STOPPED; // an answer at or after the deadline is dropped
      } catch (error) {
        const cause = scope.cause();
        if (cause === "caller") throw outer?.reason;
        const outcome = cause === "deadline" ? "timeout" : "unavailable";
        onAttempt?.({ attempt, outcome, arguments: null, problems: [], ...(outcome === "unavailable" ? { error } : {}) });
        return finish(outcome, attempt, error);
      }
      const calls = output?.toolCalls;
      const only = Array.isArray(calls) && calls.length === 1 ? calls[0] : undefined;
      if (!only || only.function?.name !== tool.name || typeof only.id !== "string" || typeof only.function.arguments !== "string") {
        const error = new Error(`expected one call of ${tool.name} with an id and string arguments`);
        onAttempt?.({ attempt, outcome: "unavailable", arguments: null, problems: [], error });
        return finish("unavailable", attempt, error);
      }
      const { id } = only;
      const args = only.function.arguments;
      messages.push({ role: "assistant", content: output.text || null, tool_calls: [{ id, type: "function", function: { name: tool.name, arguments: args } }] });
      const checked = check(parsed(args), state, attempt);
      if (!checked || !Array.isArray(checked.problems)) throw new TypeError("check must return {state, problems} with a problems array");
      state = checked.state;
      problems = checked.problems;
      const content = problems.length ? feedback(state, problems) : "accepted";
      if (typeof content !== "string") throw new TypeError("feedback must return a string");
      messages.push({ role: "tool", tool_call_id: id, content });
      onAttempt?.({ attempt, outcome: "checked", arguments: args, problems });
      if (!problems.length) return finish("accepted", attempt);
      if (attempt > maxResubmits) return finish("rejected", attempt);
    }
  } finally {
    scope.close();
  }
}
