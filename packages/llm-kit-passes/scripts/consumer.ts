// A TypeScript consumer of the shipped .d.ts files; scripts/check-pack.mjs compiles it with module nodenext.
import { audienceFilter, checkSyntaxRegistry, spreadGather, submitToolPass } from "@katsoft/llm-kit-passes";
import type {
  PassCall, PassCallInput, PassCallOutput, PassCheck, PassChecked, PassMessage, PassProblem, PassTool, PassToolCall,
  SpreadContext, SpreadEntry, SpreadOptions, SpreadResult, SpreadRun, ToolPassAttempt, ToolPassOptions, ToolPassResult, ToolPassStatus,
  AudienceFilter, AudienceFilterOptions, SyntaxForm, SyntaxPolicy, SyntaxProblem, SyntaxProblemCode, SyntaxRegistry, SyntaxScan, SyntaxSpan,
} from "@katsoft/llm-kit-passes";

interface Counted { readonly total: number }

const tool: PassTool = { name: "submit", description: "Submit.", schema: { type: "object" } };
const messages: PassMessage[] = [{ role: "user", content: "Count." }];
const call: PassCall = async (input: PassCallInput): Promise<PassCallOutput> => {
  const toolCall: PassToolCall = { id: `c${input.attempt}`, type: "function", function: { name: input.toolChoice, arguments: "{\"total\":1}" } };
  return { text: null, toolCalls: [toolCall] };
};
const check: PassCheck<Counted> = (args: unknown, state: Counted): PassChecked<Counted> => {
  const problems: PassProblem[] = typeof args === "object" && args !== null && "total" in args ? [] : [{ id: "total", line: "total: missing" }];
  return { state: { total: state.total + 1 }, problems };
};

export async function run(signal: AbortSignal): Promise<number> {
  const seen: ToolPassAttempt[] = [];
  const options: ToolPassOptions<Counted> = { messages, tool, call, check, state: { total: 0 }, deadlineMs: 1_000, signal, onAttempt: (event) => void seen.push(event) };
  const result: ToolPassResult<Counted> = await submitToolPass(options);
  const status: ToolPassStatus = result.status;
  const work: SpreadRun<number, string> = async (item: number, context: SpreadContext) => `${item}@${context.index}:${context.signal.aborted}`;
  const spread: SpreadOptions = { deadlineMs: 1_000, startedAt: Date.now(), signal };
  const gathered: SpreadResult<string> = await spreadGather([1, 2], work, spread);
  const first: SpreadEntry<string> | undefined = gathered.results[0];
  return status === "accepted" && first?.status === "fulfilled" ? result.state.total + first.value.length : seen.length;
}

type Op = "em" | "key";
type Kind = "visible" | "secret";
type Audience = "reader" | "owner";

export function filtered(text: string): string {
  const forms: SyntaxForm<Op, Kind>[] = [{ id: "em", op: "em", kind: "visible" }, { id: "key", op: "key", kind: "secret" }];
  const registry: SyntaxRegistry<Op, Kind, Audience> = { kinds: ["visible", "secret"], audiences: ["reader", "owner"], forms };
  const policy: SyntaxPolicy<Kind, Audience> = { reader: ["secret"], owner: [] };
  const problems: SyntaxProblem[] = checkSyntaxRegistry(registry, policy);
  const codes: SyntaxProblemCode[] = problems.map((problem) => problem.code);
  const scan: SyntaxScan<Kind> = (stream: string): SyntaxSpan<Kind>[] => (stream.startsWith("<<key:") ? [{ start: 0, end: stream.length, kind: "secret" }] : []);
  const options: AudienceFilterOptions<Kind, Audience> = { scan, policy };
  const filter: AudienceFilter<Audience> = audienceFilter(options);
  return `${codes.length}:${filter(text, "reader")}`;
}
