# @katsoft/llm-kit-send

Sends OpenAI-compatible chat-completions calls (OpenRouter, Moonshot, a local llama.cpp or vLLM server, any host with
the same wire): plain or streamed, with stream limits, one fallback before the first output, a reasoning-off retry,
retries while the host is busy, a backup key, call records with costs and a call ledger that records every paid call
before it is sent. Sends picture, voice and video requests to
media hosts (OpenAI-compatible images, Venice, Ideogram, fal, Sunra, ElevenLabs), downloads their result links, and
runs a fallback chain of hosts read from a `llm-kit.fallback-chain/v1` file. Keeps an optional reply cache on the
non-stream calls: a stored refusal, or a stored success of a temperature-0 chat call, answers the same wire request
again with no send.

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

// Your application owns the host and the key. The model file does not.
const url = "https://llm.example.com/v1/chat/completions";
const key = process.env.LLM_API_KEY;

const registry = loadRegistry(".llm-kit/models.yaml");
const request = resolveRequest(registry, "summary.write");
const messages = [{ role: "user", content: "Summarize this text." }];
const body = { model: request.wire_model, messages, ...chatFields(request) };
const answer = await sendChat({ url, key, body });
```

The model file is untrusted repository content. It can name any host in `base_url`, any environment variable in
`key_env` and any wire field in `extras`. Do not send a credential to a `base_url` read from it, or read a key from a
`key_env` named in it, unless your own approval rule allows that exact host and variable. The rules are in
[The file is untrusted](../llm-kit-config/README.md#the-file-is-untrusted) in the `@katsoft/llm-kit-config` README (on
npm the link may not resolve; find that section in the package's own README).

## Errors

Every failure is a `SendError` with a `kind`: `http`, `network`, `body`, `no_first_token`, `stalled`, `slow`,
`stream_error`, `incomplete` or `aborted`. It carries the HTTP `status`, the provider's `code` and `providerMessage`
(clipped), the serving host (`provider`), the upstream host's short `reason` and the parsed error `payload`. It never
holds prompt or reply text. `fallback` is true when one try on another model is worth it (a 5xx, 404, 408 or 429, a
400 "Provider returned error", and every stream, network and body failure).

## Media hosts

Each transport sends one request (or one queue job) with the body you give it and returns what you need to record the
call: the status, whether the host delivered, the cost it reported. You decide what an answer means.

| Name | Purpose |
|---|---|
| `postImages` | POST to an OpenAI-compatible images endpoint: `{ok, status, image, link, usage, cost}` or the failure. |
| `postVenice`, `isVeniceViolation` | The raw response of a Venice image request, and its content-violation header. |
| `ideogramEdit` | POST your form to Ideogram's precise edit: the first result or the reject reason. |
| `postFal` | The raw response of a synchronous fal run. |
| `falQueue` | A fal queue job (submit, poll, result) with your refusal rule; a failure keeps its stage and `completed`. |
| `sunraQueue` | A Sunra queue job; a failure keeps its stage, code and `completed`. |
| `elevenLabsRequest`, `elevenLabsError` | The raw response of an ElevenLabs request, and the code and detail of its error body. |
| `fetchFile` | Download a result link with no key, with an optional size limit. |

Base URLs: `VENICE_URL`, `IDEOGRAM_URL`, `FAL_RUN_URL`, `FAL_QUEUE_URL`, `SUNRA_QUEUE_URL`, `ELEVENLABS_URL`. Each
host transport takes an optional `baseUrl`.

```js
import { fetchFile, postImages } from "@katsoft/llm-kit-send";

const picture = await postImages({ url: "https://openrouter.ai/api/v1/images", key, body: { model, prompt } });
const bytes = picture.ok && picture.link ? await fetchFile(picture.link, { maxBytes: 20_000_000 }) : null;
```

## Fallback chains as data

A `llm-kit.fallback-chain/v1` file lists the hosts of a chain in order, the failure classes that move on (`no_key`
skips a host with no key), and the same model's endpoint and input on each host. `readFallbackChains` checks a parsed
file (`ChainFormatError` names the place); `mapInput` builds one host's input from another's; `runChain` runs the
steps with your attempt function. The format and its rules: `docs/reference/llm-kit-formats.md` in the source
repository; the Python reader is `llm_kit.registry.fallback_chains` in katsoft-llm-kit.

```js
const chains = readFallbackChains(JSON.parse(readFileSync("fallback-chains.json", "utf8")));
const result = await runChain(chains.chains.speech.steps, {
  hasKey: (host) => Boolean(keys[host]),
  attempt: async (step, { previous }) => trySpeech(step.host, previous),   // {ok: true, value} or {ok: false, failure}
});
```

## The reply cache

Every non-stream call takes a `cache` option: your store (bound to one owner), a non-secret account label, the hosts
that may be cached, and, on the parsed transports (`sendChat`, `sendWithRetries`, `postImages`, `ideogramEdit`),
`classify`, your reading of the parsed result as `"refusal"`, `"success"` or `null`. The key (`stepKey`,
`llm-kit.reply-key/v1`) is the request as sent: one added space is a new request; secrets, attribution headers and the
multipart boundary are not part of it. The rules:

- A stored refusal always replays: nothing is sent, the stored answer is read by the same code, and the result says
  `replayed: true` (cost 0, no usage). `freshCheck: true` (a deliberate second look) still replays a refusal.
- A success is stored and replayed only for a deterministic request: a chat transport whose sent body has
  `temperature` exactly 0, and not `freshCheck`. A picture is never deterministic.
- Only complete answers whose body parsed are stored, never a busy answer (408, 429, 5xx), a network failure, an abort
  or a body over `maxBytes`.
- A store failure is `ReplyCacheError`: never retried, never a fallback, never read as no answer. `onPutError` turns a
  failed write into a report and the normal answer.

The raw transports (`postChat`, `postVenice`) give a `CachedResponse`: read the response as usual, then call
`settle(verdict)` (`{headersOnly: true}` for a refusal stated in the headers). Stored entries have the format
`llm-kit.reply-entry/v1` (`readEntry`); the Python reader is `llm_kit.registry.reply_cache` in katsoft-llm-kit.

```js
const cache = { store, account: "router:main", hosts: ["https://openrouter.ai/api/v1/"],
  classify: (answer) => (answer.ok === false && answer.status === 400 ? "refusal" : null) };
const answer = await postImages({ url, key, body }, { cache });   // the same refused body again sends nothing
```

## The call ledger

Every call that can be billed is told to one sink per process: `begin` before the first byte of the request leaves, `end`
when the call ends. An app installs the sink once (`setCallLedger`) and keeps its own cost table, report or guard on the
records. The records have the format `llm-kit.paid-call/v1` (`PAID_CALL_FORMAT`; the schema and shared cases are in
katsoft-llm-kit). They never hold prompt or reply text, a query string or a key.

```js
import { requireCallLedger, sendChat, setCallLedger } from "@katsoft/llm-kit-send";

setCallLedger({
  begin: (record) => table.insert({ id: record.id, model: record.model, ...myContext.get() }), // runs in your async context
  end: (record) => table.update(record.id, { ok: record.ok, cost: record.cost, generation: record.generationId }),
});
requireCallLedger(true); // or LLM_KIT_CALL_LEDGER=required: a call with no sink throws CallLedgerError before it sends
const answer = await sendChat(call, { labels: { workflow: "story", unit: 3 } }); // answer.callId, answer.generationId
```

- `begin` runs synchronously, after `encode`, `beforeSend` and a cache miss, in the caller's async context, so the sink
  can read your own context (a player, a request). It has the transport, method, host, path (no query), model, `stream`,
  `attempt`, `startedAt` and your `labels` (a `labels` option of every transport: string, finite number or `null`
  values).
- `end` has `ok`, `status`, `errorKind`, `aborted`, `replayed`, the served model and host, the provider's
  `generationId`, the raw `usage`, the token counts and the reported `cost`. It runs once for each `begin`.
- One call for each HTTP send: a retry of `sendWithRetries` and the reasoning-off retry of `sendChat` are each a call
  (`attempt`). A stream ends in a `finally` with the chunk id as `generationId`, also when it stalled or was cut;
  `ChatAnswer` and `SendError` carry `callId` and `generationId`, so a cut call's cost can be looked up later. A queue job
  (`falQueue`, `sunraQueue`) is one call from submit to result; the polls run inside it and are not recorded.
- `postChat`, `postVenice`, `postFal` and `elevenLabsRequest` return the raw response, so their call ends when the body is
  read to its end, fails or is cancelled. The body passes through a bounded tap: the same status, headers and chunks,
  read only as you read, and its usage and generation id are read from the bytes that pass (a JSON body up to 1 MiB). A
  body you never read leaves the call open: count such calls in your report.
- A stored answer of the reply cache is a call with `replayed: true`, cost 0 and no usage; nothing was sent. A GET,
  a DELETE and `fetchFile` are not recorded.
- `activeCall()` is the id of the call the code runs in, also with no sink. A guard on `globalThis.fetch` uses it: a
  request to a paid host with no active call bypassed this package.
- With no sink the call goes on and the process gets one warning, code `LLMKIT_NO_CALL_LEDGER`. A sink that throws, or
  returns a rejected promise, is reported as a warning (`LLMKIT_CALL_LEDGER_SINK_ERROR`) and the call goes on.

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
