// The viewer page: loads the session, the graph structure and one run, and
// keeps the picture, the run list and the inspector in step.

import { el, getJson, formatCost, formatTime, formatSeconds } from "./dom.js";
import { renderGraph, markSelected } from "./graphview.js";
import { renderInspector } from "./inspector.js";
import { renderRunList } from "./runlist.js";
import { attachPopover, closePopover, popoverTrigger } from "./popover.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 2500;
const LIVE = new Set(["running", "waiting"]);
const STATUSES = ["pending", "running", "done", "reused", "seeded", "skipped", "failed", "gated", "stopped"];

const state = { expanded: new Set(), choice: new Map(), selectedPath: null, fit: false };
const structures = new Map();
let session = null;
let panels = [];
let runsInfo = { configured: false, runs: [], note: null };
let selection = null; // { kind: "graph", name } | { kind: "run", item, runId }
let structure = null;
let run = null;
let detail = null;
let detailError = null;
let loadingDetail = false;
let detailToken = 0;
let timer = null;
let signature = "";

// ------------------------------------------------------------- messages

function banner(message, error) {
  console.error(message, error || "");
  const box = $("banner");
  const row = el("p", { class: "banner-row" }, el("span", { text: message }),
    el("button", { type: "button", class: "link", text: "dismiss", on: { click: () => row.remove() } }));
  box.append(row);
}

async function guarded(label, fn) {
  try {
    return await fn();
  } catch (error) {
    banner(`${label}: ${error.message}`, error);
    return undefined;
  }
}

// ------------------------------------------------------------- panels

function normalizePanels(module, url) {
  const raw = module.default ?? module.panels ?? [];
  const list = Array.isArray(raw) ? raw : [raw];
  const out = [];
  for (const panel of list) {
    if (!panel || typeof panel.id !== "string" || typeof panel.render !== "function") {
      banner(`${url}: a panel needs an id (string) and a render(node, api) function`);
      continue;
    }
    out.push({ title: panel.id, applies: () => true, ...panel });
  }
  return out;
}

async function loadPanels() {
  for (const url of session.panels) {
    await guarded(`panel module ${url}`, async () => {
      const module = await import(url);
      panels.push(...normalizePanels(module, url));
    });
  }
}

// ------------------------------------------------------------- structure helpers

function locate(root, segments) {
  let graph = root;
  for (let i = 0; i < segments.length; i += 1) {
    const id = segments[i].replace(/\[[^\]]*\]$/, "");
    const node = graph.nodes.find((n) => n.id === id) || null;
    if (!node) return null;
    if (i === segments.length - 1) return node;
    if (!node.body) return null;
    graph = node.body;
  }
  return null;
}

function containerKeys(graph, prefix, out) {
  for (const node of graph.nodes) {
    if (!node.body) continue;
    const key = prefix ? `${prefix}/${node.id}` : node.id;
    out.push(key);
    containerKeys(node.body, key, out);
  }
  return out;
}

// ------------------------------------------------------------- hash

function parseHash() {
  const out = {};
  for (const part of location.hash.replace(/^#/, "").split("&")) {
    const at = part.indexOf("=");
    if (at > 0) out[part.slice(0, at)] = decodeURIComponent(part.slice(at + 1));
  }
  return out;
}

function writeHash() {
  const parts = [];
  if (selection && selection.kind === "graph") parts.push(`graph=${encodeURIComponent(selection.name)}`);
  if (selection && selection.kind === "run") parts.push(`run=${encodeURIComponent(`${selection.item}/${selection.runId}`)}`);
  if (state.selectedPath) parts.push(`node=${encodeURIComponent(state.selectedPath)}`);
  history.replaceState(null, "", `#${parts.join("&")}`);
}

// ------------------------------------------------------------- loading

async function loadStructure(name) {
  if (!structures.has(name)) structures.set(name, (await getJson(`/api/graph/${encodeURIComponent(name)}`)).structure);
  return structures.get(name);
}

async function loadRuns() {
  runsInfo = await getJson("/api/runs");
}

async function fetchRun() {
  return getJson(`/api/run/${encodeURIComponent(selection.item)}/${encodeURIComponent(selection.runId)}`);
}

async function selectGraph(name) {
  selection = { kind: "graph", name };
  run = null;
  state.selectedPath = null;
  detail = null;
  structure = null;
  stopPolling();
  await guarded(`graph ${name}`, async () => {
    structure = await loadStructure(name);
  });
  renderAll();
}

async function selectRun(item, runId) {
  selection = { kind: "run", item, runId };
  state.selectedPath = null;
  detail = null;
  structure = null;
  run = null;
  await guarded(`run ${item}/${runId}`, async () => {
    run = await fetchRun();
    structure = run.structure;
  });
  renderAll();
  poll();
}

async function loadDetail() {
  const path = state.selectedPath;
  const token = ++detailToken;
  detail = null;
  detailError = null;
  if (!run || !path) {
    loadingDetail = false;
    renderInspectorOnly();
    return;
  }
  loadingDetail = true;
  renderInspectorOnly();
  try {
    const response = await fetch(`/api/run/${encodeURIComponent(run.item)}/${encodeURIComponent(run.run_id)}/node?path=${encodeURIComponent(path)}`,
      { headers: { Accept: "application/json" } });
    const body = await response.json();
    if (token !== detailToken) return;
    if (response.status === 404) detail = null;
    else if (!response.ok) detailError = body.error || `HTTP ${response.status}`;
    else detail = body;
  } catch (error) {
    if (token !== detailToken) return;
    detailError = error.message;
    console.error(error);
  }
  loadingDetail = false;
  renderInspectorOnly();
}

// ------------------------------------------------------------- polling

function runSignature(value) {
  return JSON.stringify([value.status, Object.entries(value.nodes).map(([p, n]) => [p, n.status, n.score])]);
}

function stopPolling() {
  clearTimeout(timer);
  timer = null;
}

function poll() {
  stopPolling();
  if (!run || !LIVE.has(run.status)) return;
  timer = setTimeout(async () => {
    const current = selection;
    await guarded("refresh", async () => {
      const [next] = await Promise.all([fetchRun(), loadRuns()]);
      if (selection !== current) return;
      const sig = runSignature(next);
      run = next;
      structure = next.structure;
      if (sig !== signature) {
        renderAll();
        if (state.selectedPath) loadDetail();
      }
    });
    poll();
  }, POLL_MS);
}

// ------------------------------------------------------------- rendering

function renderSide() {
  renderRunList($("side"), {
    graphs: session.graphs, runs: runsInfo.runs, configured: runsInfo.configured, note: runsInfo.note, selected: selection,
    onGraph: (name) => selectGraph(name).then(writeHash),
    onRun: (item, runId) => selectRun(item, runId).then(writeHash),
  });
}

function renderRunBar() {
  const bar = $("runbar");
  bar.replaceChildren();
  if (selection && selection.kind === "graph") {
    bar.append(el("h2", { text: structure ? structure.id : selection.name }), el("span", { class: "muted", text: "structure only - pick a run to see what happened" }));
    return;
  }
  if (!run) {
    bar.append(el("h2", { text: "Pick a graph or a run" }));
    return;
  }
  const seconds = run.started && run.finished ? (new Date(run.finished) - new Date(run.started)) / 1000 : null;
  let hash = null;
  if (run.graph_hash) {
    hash = el("code", { class: "hash", tabindex: "0", text: run.graph_hash.slice(0, 12) });
    attachPopover(hash, () => el("code", { class: "hash-full", text: `graph hash ${run.graph_hash}` }));
  }
  bar.append(el("h2", { text: `${run.item} / ${run.run_id}` }),
    el("span", { class: `pill st-${run.status}`, text: run.status }),
    el("span", { class: "muted", text: [run.graph, formatTime(run.started), formatSeconds(seconds), formatCost(run.cost_usd)].filter(Boolean).join(" - ") }),
    hash);
}

function renderToolbar() {
  const bar = $("toolbar");
  bar.replaceChildren();
  const fit = el("button", { type: "button", class: "tool", "aria-pressed": String(state.fit), text: state.fit ? "Fit width" : "Actual size",
    on: { click: () => {
      state.fit = !state.fit;
      renderGraphOnly();
      renderToolbar();
    } } });
  const expand = el("button", { type: "button", class: "tool", text: "Expand all", on: { click: () => {
    if (!structure) return;
    containerKeys(structure, "", []).forEach((k) => state.expanded.add(k));
    renderGraphOnly();
  } } });
  const collapse = el("button", { type: "button", class: "tool", text: "Collapse all", on: { click: () => {
    state.expanded.clear();
    renderGraphOnly();
  } } });
  const legend = el("ul", { class: "legend", "aria-label": "Status colors" },
    STATUSES.map((s) => el("li", {}, el("span", { class: `dot st-${s}` }), s)));
  bar.append(fit, expand, collapse, legend);
}

function renderNotice() {
  const box = $("notice");
  box.replaceChildren();
  if (run && run.stop) {
    box.append(el("p", { class: "warn", text: `The run stopped at ${run.stop.node}: ${run.stop.reason}${run.stop.message ? ` - ${run.stop.message}` : ""}` }));
  }
  if (run && run.status === "running" && run.updated) {
    const quiet = (Date.now() - new Date(run.updated).getTime()) / 1000;
    if (quiet > 600) {
      box.append(el("p", { class: "warn", text: `This run says running, but its record has not changed for ${formatSeconds(quiet)}. It may have died.` }));
    }
  }
  if (run && run.unmapped && run.unmapped.length) {
    box.append(el("details", { class: "warn" },
      el("summary", { text: `${run.unmapped.length} record path(s) match no node of the graph as it is now` }),
      el("ul", { class: "plain" }, run.unmapped.slice(0, 50).map((p) => el("li", {}, el("code", { text: p }))))));
  }
}

function graphHandlers() {
  return {
    onSelect: (path) => {
      state.selectedPath = path;
      markSelected($("canvas"), path);
      writeHash();
      loadDetail();
    },
    onToggle: (key) => {
      if (state.expanded.has(key)) state.expanded.delete(key);
      else state.expanded.add(key);
      renderGraphOnly();
    },
    onChoice: (path, key) => {
      state.choice.set(path, key);
      renderGraphOnly();
    },
  };
}

function renderGraphOnly() {
  const host = $("canvas");
  const left = host.scrollLeft;
  const top = host.scrollTop;
  if (!structure) {
    host.replaceChildren(el("p", { class: "empty", text: selection ? "No graph to show." : "Choose a graph or a run on the left." }));
    return;
  }
  try {
    renderGraph(host, structure, run, state, graphHandlers());
  } catch (error) {
    banner(`drawing the graph: ${error.message}`, error);
    host.replaceChildren(el("p", { class: "error-box", role: "alert", text: `The graph cannot be drawn: ${error.message}` }));
  }
  host.scrollLeft = left;
  host.scrollTop = top;
  signature = run ? runSignature(run) : "";
}

function renderInspectorOnly() {
  const open = popoverTrigger();
  if (open && $("inspector").contains(open)) closePopover(false); // its trigger is about to be replaced
  const path = state.selectedPath;
  const structNode = path && structure ? locate(structure, path.split("/")) : null;
  renderInspector($("inspector"), {
    path, structNode, summary: run && path ? run.nodes[path] || null : null,
    detail, loading: loadingDetail, error: detailError,
    run: run ? { item: run.item, run_id: run.run_id, graph: run.graph, status: run.status } : null, panels,
  });
}

function renderAll() {
  renderSide();
  renderRunBar();
  renderToolbar();
  renderNotice();
  renderGraphOnly();
  renderInspectorOnly();
}

// ------------------------------------------------------------- start

async function applyHash() {
  const wanted = parseHash();
  if (wanted.run) {
    const at = wanted.run.indexOf("/");
    await selectRun(wanted.run.slice(0, at), wanted.run.slice(at + 1));
  } else if (wanted.graph) {
    await selectGraph(wanted.graph);
  } else if (runsInfo.runs.some((r) => r.status !== "unreadable")) {
    const first = runsInfo.runs.find((r) => r.status !== "unreadable");
    await selectRun(first.item, first.run_id);
  } else if (session.graphs.length) {
    await selectGraph(session.graphs[0]);
  }
  if (wanted.node) {
    state.selectedPath = wanted.node;
    markSelected($("canvas"), wanted.node);
    await loadDetail();
  }
  writeHash();
}

async function start() {
  await guarded("session", async () => {
    session = await getJson("/api/session");
    $("project").textContent = session.project;
    if (session.theme && session.theme !== "/theme.css") $("theme").setAttribute("href", session.theme);
    await loadPanels();
    await loadRuns();
    renderSide();
    renderToolbar();
    await applyHash();
  });
  if (!session) return;
  window.addEventListener("hashchange", () => {
    closePopover(false);
    applyHash();
  });
}

start();
