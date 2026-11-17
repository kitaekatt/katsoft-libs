// A TypeScript consumer of the shipped .d.ts files; scripts/check-pack.mjs compiles it with module nodenext.
import { SendError, sendChat, sendWithRetries, withBackupKey, withFallback } from "@katsoft/llm-kit-send";
import type { CallRecord, ChatAnswer, ChatCall, StreamLimits, TokenPrice } from "@katsoft/llm-kit-send";

const call: ChatCall = { url: "https://llm.example/v1/chat/completions", key: "k", body: { model: "a/b", messages: [], stream: true } };
const limits: StreamLimits = { firstMs: 20_000, gapMs: 15_000, pace: { windowMs: 5_000, minCharsPerS: 10 } };
const price: TokenPrice = { input: 1e-6, cached: 1e-7, write: 1e-6, output: 4e-6 };
const records: CallRecord[] = [];

export async function run(): Promise<number | null> {
  const transport = withBackupKey(fetch, { primary: "a", backup: "b", hosts: ["https://llm.example/"], windowMs: 600_000 });
  const answer: ChatAnswer = await withFallback(() => sendChat(call, { limits, fetch: transport, onDelta: (text: string) => void text }), null, { emitted: () => false });
  const plain = await sendWithRetries({ ...call, body: { ...call.body, stream: false } }, { waits: [500], budgetMs: 5_000, onCall: (record) => records.push(record) });
  try { await sendChat(call); } catch (error) { if (error instanceof SendError && error.kind === "stalled") return null; }
  return plain.ok ? price.input * answer.text.length : null;
}
