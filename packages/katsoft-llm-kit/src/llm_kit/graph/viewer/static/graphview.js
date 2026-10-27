// The graph picture: a layered left-to-right layout drawn as plain SVG.
//
// A node is a box. A node that runs a graph (graph, map, repeat) has a + / -
// control that opens its body inside the box. An open map shows one chip per
// item and an open repeat one chip per round (with its score); the chip you
// pick decides which item or round the body shows.
//
// Run paths: the body of node P is read at P/<child> for a graph node and at
// P[<key>]/<child> for a map (item key) or repeat (round number) node, where
// the record P[<key>] is the item or round itself.

import { svg, formatSeconds, formatCost, shorten, el } from "./dom.js";
import { attachPopover, closePopover } from "./popover.js";

const NODE_W = 200;
const NODE_H = 66;
const GAP_X = 54;
const GAP_Y = 18;
const PAD = 14;
const TITLE_H = 34;
const CHIP_W = 62;
const CHIP_H = 22;
const CHIP_GAP = 6;
const CHIP_ROW = CHIP_H + CHIP_GAP;
const CHAR_W = 6.4;

export function join(prefix, id) {
  return prefix ? `${prefix}/${id}` : id;
}

function computeLevels(nodes) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const memo = new Map();
  const visit = (node, stack) => {
    if (memo.has(node.id)) return memo.get(node.id);
    if (stack.has(node.id)) throw new Error(`the graph has a cycle at node ${node.id}`);
    stack.add(node.id);
    let level = 0;
    for (const dep of node.deps) {
      const other = byId.get(dep);
      if (other) level = Math.max(level, visit(other, stack) + 1);
    }
    stack.delete(node.id);
    memo.set(node.id, level);
    return level;
  };
  nodes.forEach((n) => visit(n, new Set()));
  return memo;
}

export function childKeys(run, concrete, kind) {
  const base = `${concrete}[`;
  const seen = new Set();
  const keys = [];
  for (const path of Object.keys(run.nodes)) {
    if (!path.startsWith(base)) continue;
    const key = path.slice(base.length).split("]", 1)[0];
    if (!seen.has(key)) {
      seen.add(key);
      keys.push(key);
    }
  }
  const position = (key) => {
    const entry = run.nodes[`${concrete}[${key}]`];
    return entry && Number.isInteger(entry.index) ? entry.index : Number.MAX_SAFE_INTEGER;
  };
  if (kind === "repeat") keys.sort((a, b) => Number(a) - Number(b));
  else keys.sort((a, b) => position(a) - position(b)); // items in the order of the list (the record file is sorted by path)
  return keys;
}

function readableKey(key) {
  try {
    return decodeURIComponent(key);
  } catch (error) {
    return key; // not a percent-encoded key; show it as recorded
  }
}

function statusOf(run, path) {
  const summary = run.nodes[path];
  return summary ? summary.status : "pending";
}

function pickChoice(ctx, concrete, kind, keys) {
  const chosen = ctx.state.choice.get(concrete);
  if (chosen !== undefined && keys.includes(chosen)) return chosen;
  if (keys.length === 0) return null;
  if (kind === "repeat") return keys[keys.length - 1];
  const interesting = keys.find((k) => ["failed", "stopped", "running", "waiting", "halted"].includes(statusOf(ctx.run, `${concrete}[${k}]`)));
  return interesting ?? keys[0];
}

// ---------------------------------------------------------------- layout

function layoutGraph(structure, ctx, prefix, skey) {
  const levels = computeLevels(structure.nodes);
  const items = structure.nodes.map((node) => {
    const concrete = join(prefix, node.id);
    const sk = join(skey, node.id);
    const item = { node, concrete, sk, w: NODE_W, h: NODE_H, expanded: false, inner: null, keys: null, choice: null,
      perRow: 0, headH: TITLE_H, x: 0, y: 0, level: levels.get(node.id) };
    if (node.body && ctx.state.expanded.has(sk)) {
      item.expanded = true;
      let bodyPrefix = concrete;
      if (node.kind === "map" || node.kind === "repeat") {
        item.keys = childKeys(ctx.run, concrete, node.kind);
        item.choice = pickChoice(ctx, concrete, node.kind, item.keys);
        bodyPrefix = `${concrete}[${item.choice ?? "-"}]`;
      }
      item.inner = layoutGraph(node.body, ctx, bodyPrefix, sk);
      let width = Math.max(item.inner.w + 2 * PAD, NODE_W);
      if (item.keys && item.keys.length) {
        width = Math.max(width, 3 * (CHIP_W + CHIP_GAP) + 2 * PAD);
        item.perRow = Math.max(1, Math.floor((width - 2 * PAD + CHIP_GAP) / (CHIP_W + CHIP_GAP)));
        item.headH = TITLE_H + 2 + Math.ceil(item.keys.length / item.perRow) * CHIP_ROW;
      }
      item.w = width;
      item.h = item.headH + item.inner.h + 2 * PAD;
    }
    return item;
  });
  const columns = [];
  for (const item of items) (columns[item.level] ||= []).push(item);
  let x = 0;
  let total = 0;
  const heights = columns.map((col) => (col ? col.reduce((s, i) => s + i.h, 0) + GAP_Y * (col.length - 1) : 0));
  const height = Math.max(0, ...heights);
  columns.forEach((col, index) => {
    if (!col) return;
    const colW = Math.max(...col.map((i) => i.w));
    let y = (height - heights[index]) / 2;
    for (const item of col) {
      item.x = x + (colW - item.w) / 2;
      item.y = y;
      y += item.h + GAP_Y;
    }
    x += colW + GAP_X;
    total = x - GAP_X;
  });
  return { items, w: total, h: height };
}

// ---------------------------------------------------------------- drawing

function metaLine(item, ctx) {
  const summary = ctx.run.nodes[item.concrete];
  const node = item.node;
  if (node.kind === "map" || node.kind === "repeat") {
    const keys = childKeys(ctx.run, item.concrete, node.kind);
    if (!keys.length) return node.kind === "map" ? "no items yet" : "no rounds yet";
    if (node.kind === "map") {
      const done = keys.filter((k) => ["done", "reused", "seeded", "skipped"].includes(statusOf(ctx.run, `${item.concrete}[${k}]`))).length;
      return `${keys.length} items, ${done} done`;
    }
    const scores = keys.map((k) => ctx.run.nodes[`${item.concrete}[${k}]`]).map((s) => (s && s.score !== null && s.score !== undefined ? String(s.score) : "-"));
    return `${keys.length} rounds: ${scores.slice(-5).join(" > ")}`;
  }
  if (!summary) return "";
  const bits = [formatSeconds(summary.duration_s), formatCost(summary.cost_usd)].filter(Boolean);
  if (["reused", "seeded", "skipped", "gated", "stopped", "refused"].includes(summary.status)) bits.unshift(summary.status);
  return bits.join("  ");
}

function popoverContent(item, ctx) {
  const summary = ctx.run.nodes[item.concrete];
  const node = item.node;
  const rows = [["path", item.concrete], ["kind", `${node.kind}${node.subtitle ? ` - ${node.subtitle}` : ""}`],
    ["status", summary ? summary.status : "pending"]];
  if (summary) {
    if (summary.duration_s !== null && summary.duration_s !== undefined) rows.push(["time", formatSeconds(summary.duration_s)]);
    if (summary.model) rows.push(["model", `${summary.model}${summary.effort ? ` (${summary.effort})` : ""}`]);
    if (summary.cost_usd !== null && summary.cost_usd !== undefined) rows.push(["cost", formatCost(summary.cost_usd)]);
    if (summary.score !== null && summary.score !== undefined) rows.push(["score", String(summary.score)]);
    if (summary.media) rows.push(["media", String(summary.media)]);
    if (summary.error) rows.push(["error", summary.error]);
  }
  if (node.when) rows.push(["when", node.when]);
  if (node.deps.length) rows.push(["reads", node.deps.join(", ")]);
  return el("div", { class: "pop-rows" },
    rows.map(([k, v]) => el("div", { class: "pop-row" }, el("span", { class: "pop-k", text: k }), el("span", { class: "pop-v", text: v }))),
    el("p", { class: "pop-hint", text: "Click pins this box. Esc closes it." }));
}

function drawChip(item, key, index, ctx) {
  const concrete = `${item.concrete}[${key}]`;
  const summary = ctx.run.nodes[concrete];
  const status = summary ? summary.status : "pending";
  const col = index % item.perRow;
  const row = Math.floor(index / item.perRow);
  const x = PAD + col * (CHIP_W + CHIP_GAP);
  const y = TITLE_H - 2 + row * CHIP_ROW;
  const isRound = item.node.kind === "repeat";
  const score = summary && summary.score !== null && summary.score !== undefined ? ` ${summary.score}` : "";
  const shown = readableKey(key);
  const label = isRound ? `R${Number(key) + 1}${score}` : shorten(shown, 9);
  const g = svg("g", { class: `gchip st-${status}${key === item.choice ? " sel" : ""}`, transform: `translate(${x},${y})`,
    tabindex: "0", role: "button", "aria-pressed": key === item.choice ? "true" : "false",
    "aria-label": `${isRound ? "round" : "item"} ${key}, ${status}` },
  svg("rect", { width: CHIP_W, height: CHIP_H, rx: 6 }),
  svg("text", { x: CHIP_W / 2, y: 15, "text-anchor": "middle", text: label }));
  const choose = () => ctx.handlers.onChoice(item.concrete, key);
  g.addEventListener("click", (event) => {
    event.stopPropagation();
    choose();
  });
  g.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      choose();
    }
  });
  attachPopover(g, () => el("div", { class: "pop-rows" },
    el("div", { class: "pop-row" }, el("span", { class: "pop-k", text: isRound ? "round" : "item" }), el("span", { class: "pop-v", text: isRound ? String(Number(key) + 1) : shown })),
    el("div", { class: "pop-row" }, el("span", { class: "pop-k", text: "status" }), el("span", { class: "pop-v", text: status })),
    summary && summary.score !== null && summary.score !== undefined
      ? el("div", { class: "pop-row" }, el("span", { class: "pop-k", text: "score" }), el("span", { class: "pop-v", text: String(summary.score) }))
      : null,
    el("p", { class: "pop-hint", text: "Click shows this in the box." })), { id: `chip:${concrete}`, keyActivate: false });
  return g;
}

function drawNode(item, ax, ay, ctx) {
  const summary = ctx.run.nodes[item.concrete];
  const status = summary ? summary.status : "pending";
  const node = item.node;
  const g = svg("g", {
    class: `gnode kind-${node.kind} st-${status}${item.expanded ? " open" : ""}${ctx.state.selectedPath === item.concrete ? " selected" : ""}`,
    transform: `translate(${ax},${ay})`, tabindex: "0", role: "button",
    "aria-label": `${node.id}, ${node.kind}, ${status}`, dataset: { path: item.concrete },
  });
  g.append(svg("rect", { class: "gbody", width: item.w, height: item.h, rx: 12 }),
    svg("rect", { class: "gtone", width: item.w - 24, height: 4, x: 12, y: 0, rx: 2 }),
    svg("circle", { class: "gdot", cx: 17, cy: 21, r: 5 }),
    svg("text", { class: "gid", x: 28, y: 25, text: shorten(node.id, Math.floor((item.w - 28 - 60) / 7.2)) }));
  const score = summary && summary.score !== null && summary.score !== undefined ? summary.score : null;
  if (!item.expanded) {
    const subtitle = summary && summary.model ? summary.model : node.subtitle;
    g.append(svg("text", { class: "gsub", x: 14, y: 43, text: shorten(subtitle, Math.floor((item.w - 28) / CHAR_W)) }),
      svg("text", { class: "gmeta", x: 14, y: 59, text: shorten(metaLine(item, ctx), Math.floor((item.w - 28) / CHAR_W)) }));
  } else {
    g.append(svg("text", { class: "gkind", x: item.w - 48, y: 25, "text-anchor": "end", text: node.kind }));
  }
  if (score !== null) g.append(svg("text", { class: "gbadge", x: item.w - 48, y: item.expanded ? 12 : 25, "text-anchor": "end", text: String(score) }));
  if (node.body) {
    const toggle = svg("g", { class: "gtoggle", transform: `translate(${item.w - 32},11)`, tabindex: "0", role: "button",
      "aria-expanded": item.expanded ? "true" : "false", "aria-label": `${item.expanded ? "Collapse" : "Expand"} ${node.id}` },
    svg("rect", { width: 20, height: 20, rx: 6 }),
    svg("text", { x: 10, y: 15, "text-anchor": "middle", text: item.expanded ? "-" : "+" }));
    const flip = () => ctx.handlers.onToggle(item.sk);
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      flip();
    });
    toggle.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        flip();
      }
    });
    g.append(toggle);
  }
  if (item.expanded) {
    (item.keys || []).forEach((key, index) => g.append(drawChip(item, key, index, ctx)));
    g.append(drawLevel(item.inner, PAD, item.headH + PAD, ctx));
  }
  g.addEventListener("click", (event) => {
    event.stopPropagation(); // a body node sits inside its container's box
    ctx.handlers.onSelect(item.concrete, item.node);
  });
  g.addEventListener("keydown", (event) => {
    if (event.target === g && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      ctx.handlers.onSelect(item.concrete, item.node);
    }
  });
  attachPopover(g, () => popoverContent(item, ctx), { id: `node:${item.concrete}`, keyActivate: true });
  return g;
}

function drawLevel(layout, ox, oy, ctx) {
  const group = svg("g", { class: "glevel", transform: `translate(${ox},${oy})` });
  const byId = new Map(layout.items.map((i) => [i.node.id, i]));
  for (const item of layout.items) {
    for (const dep of item.node.deps) {
      const from = byId.get(dep);
      if (!from) continue;
      const x1 = from.x + from.w;
      const y1 = from.y + from.h / 2;
      const x2 = item.x;
      const y2 = item.y + item.h / 2;
      const dx = Math.max(24, (x2 - x1) / 2);
      const fromStatus = statusOf(ctx.run, from.concrete);
      group.append(svg("path", { class: `gedge${["done", "reused", "seeded"].includes(fromStatus) ? " live" : ""}`,
        d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}` }));
    }
  }
  for (const item of layout.items) group.append(drawNode(item, item.x, item.y, ctx));
  return group;
}

/**
 * Draw `structure` for `run` into `host`.
 * state: { expanded:Set, choice:Map, selectedPath, fit }
 * handlers: { onSelect(path, node), onToggle(structuralKey), onChoice(path, key) }
 */
export function renderGraph(host, structure, run, state, handlers) {
  const ctx = { run: run || { nodes: {} }, state, handlers };
  const layout = layoutGraph(structure, ctx, "", "");
  const width = layout.w + 2 * PAD;
  const height = layout.h + 2 * PAD;
  const root = svg("svg", { class: "graph", width, height, viewBox: `0 0 ${width} ${height}`, role: "group",
    "aria-label": `Graph ${structure.id}` }, drawLevel(layout, PAD, PAD, ctx));
  host.classList.toggle("fit", state.fit);
  host.replaceChildren(root);
  return root;
}

export function markSelected(host, path) {
  for (const g of host.querySelectorAll(".gnode")) g.classList.toggle("selected", g.dataset.path === path);
}

export function closeGraphPopovers() {
  closePopover(false);
}
