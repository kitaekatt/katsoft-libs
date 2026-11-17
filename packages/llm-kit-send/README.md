# @katsoft/llm-kit-send

Sends OpenAI-compatible chat-completions calls (OpenRouter, Moonshot, a local llama.cpp or vLLM server, any host with
the same wire): plain or streamed, with stream limits, one fallback before the first output, a reasoning-off retry,
retries while the host is busy, a backup key and call records with costs.

The package sends the body you give it, byte for byte (through your `encode`). It does not choose models or build
prompts. To build a body from an llm-kit model file, use `@katsoft/llm-kit-config`.

Node 24 or later. No runtime dependency.

```sh
npm install @katsoft/llm-kit-send
```

## A streamed call with limits and a fallback

```js
import { sendChat, withFallback } from "@katsoft/llm-kit-send";

const url = "https://openrouter.ai/api/v1/chat/completions";
const key = process.env.OPENROUTER_API_KEY;
const messages = [{ role: "user", content: "Say hello." }];
const limits = { firstMs: 20_000, gapMs: 15_000 };
let emitted = false;
const onDelta = (text) => { emitted = true; process.stdout.write(text); };

const answer = await withFallback(
  () => sendChat({ url, key, body: { model: "primary/model", messages, stream: true } }, { limits, onDelta }),
  () => sendChat({ url, key, body: { model: "backup/model", messages, stream: true } }, { limits, onDelta }),
  { emitted: () => emitted },
);
console.log(answer.usage);
```

- `limits.firstMs`: the longest wait for the first real output; the wait for the response headers counts.
- `limits.gapMs`: the longest silence after the first output.
- `limits.pace`: an optional floor, `{windowMs, minCharsPerS}`.
- Real output is content, thinking (`reasoning`, `reasoning_content`, `reasoning_details`) and tool call names or
  arguments. A comment line (`: PROCESSING`) or a role-only chunk is not.
- On a limit the request is aborted before the error is thrown, so two models never run and bill at once.

## A body from an llm-kit model file

```js
import { chatFields, loadRegistry, resolveRequest } from "@katsoft/llm-kit-config";
import { sendChat } from "@katsoft/llm-kit-send";

const registry = loadRegistry(".llm-kit/models.yaml");
const request = resolveRequest(registry, "summary.write");
const endpoint = registry.document.endpoints[request.endpoint];
const messages = [{ role: "user", content: "Summarize this text." }];
const body = { model: request.wire_model, messages, ...chatFields(request) };
const answer = await sendChat({ url: `${endpoint.base_url}/chat/completions`, key: process.env[endpoint.key_env], body });
```

## Errors

Every failure is a `SendError` with a `kind`: `http`, `network`, `body`, `no_first_token`, `stalled`, `slow`,
`stream_error`, `incomplete` or `aborted`. It carries the HTTP `status`, the provider's `code` and `providerMessage`
(clipped), the serving host (`provider`), the upstream host's short `reason` and the parsed error `payload`. It never
holds prompt or reply text. `fallback` is true when one try on another model is worth it (a 5xx, 404, 408 or 429, a
400 "Provider returned error", and every stream, network and body failure).

## Other parts

| Name | Purpose |
|---|---|
| `postChat` | The raw POST: the untouched response of any status. |
| `sendWithRetries` | A plain call with retries while the host is busy (429, 5xx, no answer), one deadline, one `CallRecord` per try. |
| `retryWait`, `pause`, `isTransient` | The wait with jitter and Retry-After within a budget, an abortable pause, the busy statuses. |
| `withBackupKey`, `isKeyLimit` | A fetch wrapper that resends once with a backup key when the host refuses the main key for its limit. |
| `usageCost`, `tokenCost` | The cost the host reported, or one from a per-token price. |
| `upstreamReason` | The upstream host's short reason from OpenRouter's `metadata.raw`. |

Options that hold your rules: `encode` (rewrite the body for an endpoint, for example a privacy field), `beforeSend`
(refuse a call before any byte leaves), `reasoningRetry` (the budget when a host will not turn reasoning off).

Examples that run with plain `node` and no network: `examples/`.

## License

MIT
