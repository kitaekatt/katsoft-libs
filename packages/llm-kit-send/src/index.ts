/**
 * Sends OpenAI-compatible chat-completions calls: plain or streamed, with stream limits (first output, silence, pace),
 * one fallback before the first token, the reasoning-off retry, retries with jitter and Retry-After, a backup key, call
 * records and costs. Sends picture, voice and video requests to media hosts (OpenAI-compatible images, Venice,
 * Ideogram, fal, Sunra, ElevenLabs) and downloads their result links. Reads fallback chain files
 * (`llm-kit.fallback-chain/v1`) and runs a chain of hosts. Tells every call that can be billed to one call ledger sink
 * (`setCallLedger`, records `llm-kit.paid-call/v1`): `begin` before the first byte leaves, `end` when the call ends. Keeps a reply cache on the non-stream calls (the `cache`
 * option): a stored refusal, or a stored success of a temperature-0 chat call, is answered with no send, under keys of
 * the exact wire request (`llm-kit.reply-key/v1`, entries `llm-kit.reply-entry/v1`). The caller builds each request body (for example from
 * `@katsoft/llm-kit-config` `chatFields`); this package sends it as given.
 *
 * @packageDocumentation
 */

export { SendError, upstreamReason } from "./errors.js";
export type { SendErrorFields, SendErrorKind } from "./errors.js";
export { postChat, sendChat } from "./send.js";
export type { ChatAnswer, ChatCall, PostOptions, ReasoningRetry, SendOptions } from "./send.js";
export type { StreamLimits, StreamPace, ToolCall } from "./stream.js";
export { withFallback } from "./fallback.js";
export type { FallbackOptions } from "./fallback.js";
export { CallLedgerError, PAID_CALL_FORMAT, activeCall, requireCallLedger, setCallLedger } from "./ledger.js";
export type { CallLabels, CallLedgerSink, PaidCallBegin, PaidCallEnd } from "./ledger.js";
export { isTransient, pause, retryWait, sendWithRetries } from "./retry.js";
export type { CallRecord, RetryOptions, RetryResult, RetryWaitOptions } from "./retry.js";
export { isKeyLimit, withBackupKey } from "./backup-key.js";
export type { BackupKeyOptions } from "./backup-key.js";
export { tokenCost, usageCost } from "./cost.js";
export type { TokenPrice } from "./cost.js";
export {
  ELEVENLABS_URL, FAL_QUEUE_URL, FAL_RUN_URL, IDEOGRAM_URL, SUNRA_QUEUE_URL, VENICE_URL,
  elevenLabsError, elevenLabsRequest, fetchFile, ideogramEdit, isVeniceViolation, postFal, postImages, postVenice,
} from "./media.js";
export type {
  ElevenLabsCall, FalCall, FileOptions, FileResult, IdeogramAnswer, IdeogramCall, ImagesAnswer, ImagesCall, MediaOptions,
  VeniceCall,
} from "./media.js";
export { falQueue, sunraQueue } from "./queue.js";
export type { FalQueueCall, FalQueueResult, QueueStage, SunraQueueCall, SunraQueueResult } from "./queue.js";
export { REPLY_ENTRY_FORMAT, REPLY_KEY_FORMAT, ReplyCacheError, ReplyEntryError, readEntry, requestKey, stepKey } from "./reply-cache.js";
export type {
  CachedResponse, ParsedStepCache, ReplyEntry, ReplyLayer, ReplyStore, ReplyVerdict, StepCache, StepContext, WireRequest,
} from "./reply-cache.js";
export { ChainFormatError, FALLBACK_CHAIN_FORMAT, mapInput, readFallbackChains, runChain } from "./chain.js";
export type {
  AttemptResult, ChainContext, ChainFailure, ChainOptions, ChainResult, ChainStep, FallbackChains, InputRule, ModelHost,
} from "./chain.js";
