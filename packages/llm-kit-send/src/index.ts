/**
 * Sends OpenAI-compatible chat-completions calls: plain or streamed, with stream limits (first output, silence, pace),
 * one fallback before the first token, the reasoning-off retry, retries with jitter and Retry-After, a backup key, call
 * records and costs. The caller builds the request body (for example from `@katsoft/llm-kit-config` `chatFields`);
 * this package sends it as given.
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
