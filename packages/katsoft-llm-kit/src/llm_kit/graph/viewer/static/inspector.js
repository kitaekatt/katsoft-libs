// The node inspector: facts, panels from the project, and the recorded values.

import { el, formatSeconds, formatCost, formatTime, jsonText, shorten } from "./dom.js";
import { attachPopover } from "./popover.js";

export function panelApi() {
  return {
    el,
    text: (value) => document.createTextNode(String(value)),
    pre: (value) => el("pre", { class: "code text", text: String(value) }),
    json: (value) => el("pre", { class: "code json", text: jsonText(value) }),
    popover: attachPopover,
    formatSeconds,
    formatCost,
  };
}

function pill(text, status) {
  return el("span", { class: `pill st-${status || "pending"}`, text });
}

function fullView(trigger, text) {
  attachPopover(trigger, () => el("pre", { class: "code big", text }), { keyActivate: false });
}

let currentItem = null;

function block(title, value, kind) {
  const text = kind === "text" ? String(value) : jsonText(value);
  const full = el("button", { class: "link", type: "button", text: "full view" });
  fullView(full, text);
  const head = el("header", {}, el("h4", { text: title }), el("span", { class: "muted", text: `${text.length} chars` }), full);
  const section = el("section", { class: "block" }, head);
  if (value && typeof value === "object" && value.$error) {
    section.append(el("p", { class: "error-box", role: "alert", text: `${value.$error}` }));
  }
  if (value && typeof value === "object" && value.$truncated && value.ref && currentItem) {
    head.insertBefore(el("a", { class: "link", href: `/api/ref/${encodeURIComponent(currentItem)}/${encodeURIComponent(value.ref)}`,
      target: "_blank", rel: "noopener", text: "open whole value" }), full);
  }
  section.append(el("pre", { class: `code ${kind}`, text }));
  return section;
}

function emptyValue(value) {
  return value === null || value === undefined || (typeof value === "object" && Object.keys(value).length === 0);
}

function facts(rows) {
  const dl = el("dl", { class: "facts" });
  for (const [key, value] of rows) {
    if (value === null || value === undefined || value === "") continue;
    dl.append(el("dt", { text: key }), el("dd", {}, value instanceof Node ? value : String(value)));
  }
  return dl;
}

function hashCell(hash) {
  if (!hash) return null;
  const code = el("code", { class: "hash", tabindex: "0", text: shorten(hash, 14) });
  attachPopover(code, () => el("code", { class: "hash-full", text: hash }), { keyActivate: false });
  return code;
}

function usageText(usage) {
  if (!usage || typeof usage !== "object") return null;
  const inTok = usage.input_tokens ?? usage.prompt_tokens;
  const outTok = usage.output_tokens ?? usage.completion_tokens;
  if (inTok !== undefined || outTok !== undefined) return `${inTok ?? "?"} in, ${outTok ?? "?"} out`;
  return JSON.stringify(usage);
}

function errorText(error) {
  if (!error) return null;
  if (typeof error === "string") return error;
  return `${error.type || "error"}: ${error.message || ""}`;
}

function sourceText(seed) {
  if (!seed) return null;
  return `${seed.valid === false ? "invalid seed" : "seed"}: ${JSON.stringify(seed.source)}`;
}

function mediaItem(entry) {
  const label = entry.name || entry.ref;
  if (!entry.url) return el("figure", { class: "media" }, el("figcaption", { class: "error-text", text: `${label}: not a media file name` }));
  const mime = entry.mime || "";
  let thumb;
  if (mime.startsWith("image/")) {
    thumb = el("img", { class: "thumb", src: entry.url, alt: label, loading: "lazy" });
    const wrap = el("button", { class: "thumb-btn", type: "button", "aria-label": `View ${label}` }, thumb);
    attachPopover(wrap, () => el("figure", { class: "big-media" }, el("img", { src: entry.url, alt: label }),
      el("figcaption", { text: entry.name || entry.sha256 })), { keyActivate: false });
    return el("figure", { class: "media" }, wrap, el("figcaption", { text: label }));
  }
  if (mime.startsWith("audio/")) {
    return el("figure", { class: "media" }, el("audio", { controls: true, preload: "none", src: entry.url }), el("figcaption", { text: label }));
  }
  if (mime.startsWith("video/")) {
    return el("figure", { class: "media" }, el("video", { controls: true, preload: "none", src: entry.url, class: "thumb" }), el("figcaption", { text: label }));
  }
  return el("figure", { class: "media" }, el("a", { href: entry.url, download: entry.name || entry.sha256, text: "download" }), el("figcaption", { text: label }));
}

function panelSection(panel, nodeContext, api) {
  const body = el("div", { class: "panel-body" });
  try {
    const out = panel.render(nodeContext, api);
    if (out instanceof Node) body.append(out);
    else if (out !== null && out !== undefined) body.append(document.createTextNode(String(out)));
  } catch (error) {
    console.error(`panel ${panel.id}`, error);
    body.append(el("p", { class: "error-text", role: "alert", text: `panel ${panel.id} failed: ${error.message}` }));
  }
  return el("section", { class: "block panel", dataset: { panel: panel.id } },
    el("header", {}, el("h4", { text: panel.title || panel.id })), body);
}

/**
 * view: { path, structNode, summary, detail, loading, error, run, panels }
 */
export function renderInspector(host, view) {
  host.replaceChildren();
  currentItem = view && view.run ? view.run.item : null;
  if (!view || !view.path) {
    host.append(el("p", { class: "empty", text: "Pick a box to see what was recorded." }));
    return;
  }
  const { structNode, summary, detail } = view;
  const status = detail ? detail.status : summary ? summary.status : "pending";
  host.append(el("header", { class: "ins-head" },
    el("p", { class: "kicker", text: structNode ? `${structNode.kind}${structNode.subtitle ? ` - ${structNode.subtitle}` : ""}` : "node" }),
    el("h3", { text: view.path }),
    el("p", {}, pill(status, status))));
  if (view.error) host.append(el("p", { class: "error-text", role: "alert", text: view.error }));
  if (view.loading) host.append(el("p", { class: "muted", text: "Loading the record..." }));
  if (!detail) {
    if (!view.loading && !view.error) host.append(el("p", { class: "muted", text: "No record for this node yet." }));
    return;
  }
  if (detail.synthetic) {
    host.append(el("p", { class: "muted", text: "This box has no record of its own; its status comes from the nodes below it." }),
      el("ul", { class: "plain" }, (detail.children || []).map((c) => el("li", {}, el("code", { text: c })))));
    return;
  }
  const receipt = detail.receipt || {};
  const facts_ = detail.facts || {};
  const failure = errorText(detail.error);
  if (failure) host.append(el("p", { class: "error-box", role: "alert", text: failure }));
  const reused = detail.reused_from ? `${detail.reused_from.run} at ${detail.reused_from.path}` : null;
  host.append(facts([
    ["status", detail.status],
    ["record", detail.kind],
    ["started", formatTime(detail.started)],
    ["finished", formatTime(detail.finished)],
    ["time", formatSeconds(detail.duration_s)],
    ["model", facts_.model ? `${facts_.model}${facts_.effort ? ` (${facts_.effort})` : ""}` : null],
    ["tokens", usageText(facts_.usage)],
    ["cost", typeof facts_.cost_usd === "number" ? formatCost(facts_.cost_usd) : null],
    ["reused from", reused],
    ["seed", sourceText(detail.seed)],
    ["definition", hashCell(detail.definition_hash)],
    ["inputs hash", hashCell(detail.input_hash)],
  ]));

  const api = panelApi();
  const nodeContext = {
    path: detail.path, id: structNode ? structNode.id : detail.path.split("/").pop(), kind: structNode ? structNode.kind : null,
    op: detail.op || null, status: detail.status, summary, detail, run: view.run, structure: structNode,
    inputs: detail.inputs, params: detail.params, reads: detail.reads, out: detail.out, attributes: detail.attributes,
    receipt: detail.receipt, facts: detail.facts, media: detail.media,
  };
  for (const panel of view.panels || []) {
    let applies = false;
    try {
      applies = Boolean(panel.applies(nodeContext));
    } catch (error) {
      console.error(`panel ${panel.id} applies()`, error);
      host.append(el("p", { class: "error-text", role: "alert", text: `panel ${panel.id} failed: ${error.message}` }));
    }
    if (applies) host.append(panelSection(panel, nodeContext, api));
  }

  const attributes = detail.attributes || {};
  if (typeof attributes.sent === "string") host.append(block("Exact prompt sent", attributes.sent, "text"));
  else if (attributes.sent !== undefined) host.append(block("Exact prompt sent", attributes.sent, "json"));
  if (typeof attributes.text === "string") host.append(block("Raw answer", attributes.text, "text"));
  if (!emptyValue(detail.out)) host.append(block("Output", detail.out, "json"));
  if (!emptyValue(detail.inputs)) {
    for (const [port, value] of Object.entries(detail.inputs)) host.append(block(`Input: ${port}`, value, "json"));
  }
  if (!emptyValue(detail.params)) host.append(block("Parameters as run", detail.params, "json"));
  if (!emptyValue(detail.reads)) host.append(block("Values read", detail.reads, "json"));
  if (!emptyValue(detail.carry)) host.append(block("Carried values", detail.carry, "json"));
  if (detail.media && detail.media.length) {
    host.append(el("section", { class: "block" }, el("header", {}, el("h4", { text: "Media" })),
      el("div", { class: "media-row" }, detail.media.map(mediaItem))));
  }
  const other = Object.fromEntries(Object.entries(attributes).filter(([k]) => k !== "sent" && k !== "text"));
  if (!emptyValue(other)) host.append(block("Other attributes", other, "json"));
  if (!emptyValue(receipt)) host.append(block("Receipt", receipt, "json"));
  if (structNode && structNode.definition) host.append(block("Definition", structNode.definition, "json"));
}
