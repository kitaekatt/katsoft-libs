// The bounded passthrough of a raw response: the caller gets a response with the same status, headers and body bytes,
// and the ledger gets the call's end when that body is read to its end, fails, or is cancelled. A body that is never
// read leaves the call open. Internal: the raw transports call it.
import { usageCost } from "./cost.js";
import type { CallHandle, EndInfo } from "./ledger.js";

/** The most of a JSON body that is kept to read its usage; a larger answer ends with no usage. */
const JSON_LIMIT = 1 << 20;
/** The longest event-stream line that is kept (a longer line is dropped). */
const LINE_LIMIT = 1 << 20;

interface Sniffed {
  generationId: string | null;
  servedModel: string | null;
  provider: string | null;
  usage: unknown;
}

const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

/** Reads what the ledger needs (ids, model, host, usage) from the body bytes as they pass; it keeps no reply text. */
function sniffer(type: string) {
  const found: Sniffed = { generationId: null, servedModel: null, provider: null, usage: null };
  const take = (value: any) => {
    if (!value || typeof value !== "object") return;
    found.generationId ??= text(value.id);
    found.servedModel = text(value.model) ?? found.servedModel;
    found.provider = text(value.provider) ?? found.provider;
    if (value.usage && typeof value.usage === "object") found.usage = value.usage;
  };
  const decoder = new TextDecoder();
  if (/json/i.test(type)) {
    const chunks: Uint8Array[] = [];
    let size = 0;
    return {
      push(chunk: Uint8Array) { if (size <= JSON_LIMIT) { size += chunk.byteLength; chunks.push(chunk); } },
      done(): Sniffed {
        if (size > JSON_LIMIT) return found;
        const bytes = new Uint8Array(size);
        let at = 0;
        for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.byteLength; }
        try { take(JSON.parse(decoder.decode(bytes))); } catch { /* not JSON: nothing to read */ }
        return found;
      },
    };
  }
  if (/event-stream/i.test(type)) {
    let buffer = "";
    const line = (raw: string) => {
      if (!raw.startsWith("data:")) return;
      const data = raw.slice(5).trim();
      if (!data || data === "[DONE]") return;
      // Parse the first chunk (for its id) and the chunks that carry a usage object; skip the others.
      if (found.generationId !== null && !/"usage"\s*:\s*\{/.test(data)) return;
      try { take(JSON.parse(data)); } catch { /* a chunk that is not JSON */ }
    };
    const lines = (flush: boolean) => {
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) { line(buffer.slice(0, end).replace(/\r$/, "")); buffer = buffer.slice(end + 1); }
      if (buffer.length > LINE_LIMIT) buffer = "";
      if (flush) { line(buffer); buffer = ""; }
    };
    return {
      push(chunk: Uint8Array) { buffer += decoder.decode(chunk, { stream: true }); lines(false); },
      done(): Sniffed { buffer += decoder.decode(); lines(true); return found; },
    };
  }
  return { push() {}, done: () => found };
}

/**
 * The response the caller gets for `response`: the same status, status text, headers, `url`, `redirected` and `type`, and
 * the same body bytes in the same chunks, read only when the caller reads (no buffering ahead, so the stream's
 * backpressure holds). The call `rec` ends when the body is read to its end (`ok` is the response's `ok`), when it
 * fails (`body`, or `aborted` when `signal` was aborted) or when the reader cancels it (`cancelled`). A JSON or
 * event-stream body is also read for its generation id, model, host and usage, up to a bound.
 */
export function tapResponse(response: Response, rec: CallHandle, signal?: AbortSignal | undefined): Response {
  const { status } = response;
  const end = (info: EndInfo) => rec.end({ status, ...info });
  if (!response.body) {
    end({ ok: response.ok, errorKind: response.ok ? null : "http" });
    return response;
  }
  const reader = response.body.getReader();
  const sniff = sniffer(response.headers.get("content-type") ?? "");
  const read = (info: EndInfo) => {
    const seen = sniff.done();
    end({ ...info, generationId: seen.generationId, servedModel: seen.servedModel, provider: seen.provider, usage: seen.usage, cost: usageCost(seen.usage) });
  };
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      let next: Awaited<ReturnType<typeof reader.read>>;
      try {
        next = await reader.read();
      } catch (error) {
        end({ ok: false, errorKind: signal?.aborted ? "aborted" : "body", aborted: Boolean(signal?.aborted) });
        controller.error(error);
        return;
      }
      if (next.done) {
        read({ ok: response.ok, errorKind: response.ok ? null : "http" });
        controller.close();
        return;
      }
      sniff.push(next.value);
      controller.enqueue(next.value);
    },
    cancel(reason) {
      read({ ok: false, errorKind: "cancelled" });
      return reader.cancel(reason);
    },
  }, { highWaterMark: 0 });
  const tapped = new Response(body, { status, statusText: response.statusText, headers: response.headers });
  Object.defineProperties(tapped, {
    url: { value: response.url }, redirected: { value: response.redirected }, type: { value: response.type },
  });
  return tapped;
}
