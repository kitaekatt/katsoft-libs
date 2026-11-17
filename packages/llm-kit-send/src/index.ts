/**
 * Sends OpenAI-compatible chat-completions calls: plain or streamed, with stream limits (first output, silence, pace),
 * one fallback before the first token, the reasoning-off retry, retries with jitter and Retry-After, a backup key, call
 * records and costs. Sends picture, voice and video requests to media hosts (OpenAI-compatible images, Venice,
 * Ideogram, fal, Sunra, ElevenLabs) and downloads their result links. Reads fallback chain files
 * (`llm-kit.fallback-chain/v1`) and runs a chain of hosts. The caller builds each request body (for example from
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
export { ChainFormatError, FALLBACK_CHAIN_FORMAT, mapInput, readFallbackChains, runChain } from "./chain.js";
export type {
  AttemptResult, ChainContext, ChainFailure, ChainOptions, ChainResult, ChainStep, FallbackChains, InputRule, ModelHost,
} from "./chain.js";
