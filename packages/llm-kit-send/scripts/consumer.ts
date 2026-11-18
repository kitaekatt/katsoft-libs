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

import { fetchFile, falQueue, mapInput, postImages, readFallbackChains, runChain } from "@katsoft/llm-kit-send";
import type { ChainResult, FallbackChains, FileResult, ImagesAnswer } from "@katsoft/llm-kit-send";

export async function media(): Promise<string | null> {
  const picture: ImagesAnswer = await postImages({ url: "https://images.example/v1/images", key: "k", body: { prompt: "x" } });
  const file: FileResult = picture.ok && picture.link ? await fetchFile(picture.link, { maxBytes: 1_000 }) : { ok: false, status: null, reason: "network" };
  const chains: FallbackChains = readFallbackChains(JSON.parse("{}"));
  const result: ChainResult<string> = await runChain(chains.chains["voice"]?.steps ?? [], {
    hasKey: () => true,
    attempt: async () => ({ ok: true, value: JSON.stringify(mapInput([{ key: "text" }], { text: "Hi." })) }),
  });
  const job = await falQueue({ model: "vendor/video", key: "k", input: {}, pollMs: 1_000 });
  return result.ok && file.ok && job.ok ? result.value : null;
}

import { ReplyCacheError, isVeniceViolation, postChat, postVenice, readEntry, stepKey } from "@katsoft/llm-kit-send";
import type { CachedResponse, ParsedStepCache, ReplyEntry, ReplyStore, RetryResult, StepCache } from "@katsoft/llm-kit-send";

export async function cached(store: ReplyStore): Promise<boolean> {
  const step: StepCache = { store, account: "router:main", hosts: ["https://llm.example/v1/"], onPutError: (error: ReplyCacheError) => void error.op };
  const raw: Response = await postChat(call);
  const kept: CachedResponse = await postChat(call, { cache: step });
  await kept.settle(kept.response.ok ? null : "refusal");
  const venice: CachedResponse = await postVenice({ endpoint: "generate", key: "k", body: {} }, { cache: step });
  await venice.settle(isVeniceViolation(venice.response) ? "refusal" : null, { headersOnly: true });
  const retried: ParsedStepCache<RetryResult> = { ...step, classify: (result) => (result.ok ? "success" : null) };
  const answer = await sendWithRetries(call, { waits: [], budgetMs: 0, cache: retried });
  const entry: ReplyEntry = readEntry(JSON.parse("{}"));
  const key: string = await stepKey("router:main", { method: "POST", url: call.url, headers: {}, body: "{}" });
  return raw.ok && answer.replayed && entry.layer === "step" && key.length === 64;
}
