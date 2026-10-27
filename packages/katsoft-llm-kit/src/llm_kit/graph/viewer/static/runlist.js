// The sidebar: graphs (structure only) and runs found on disk.

import { el, formatCost, formatTime } from "./dom.js";

function pill(status) {
  return el("span", { class: `pill st-${status}`, text: status });
}

export function renderRunList(host, model) {
  host.replaceChildren();
  host.append(el("h2", { text: "Graphs" }));
  if (!model.graphs.length) host.append(el("p", { class: "muted", text: "The project config names no graphs." }));
  const graphList = el("ul", { class: "list" });
  for (const name of model.graphs) {
    const selected = model.selected && model.selected.kind === "graph" && model.selected.name === name;
    graphList.append(el("li", {}, el("button", { type: "button", class: `row${selected ? " active" : ""}`, "aria-current": selected ? "true" : null,
      on: { click: () => model.onGraph(name) } }, el("span", { class: "row-main", text: name }), el("span", { class: "muted", text: "structure" }))));
  }
  host.append(graphList, el("h2", { text: "Runs" }));
  if (model.note) host.append(el("p", { class: "muted", text: model.note }));
  if (model.configured && !model.runs.length) host.append(el("p", { class: "muted", text: "No runs on disk yet." }));
  const runList = el("ul", { class: "list" });
  for (const run of model.runs) {
    if (run.status === "unreadable") {
      runList.append(el("li", {}, el("div", { class: "row bad" },
        el("span", { class: "row-main", text: `${run.item} / ${run.run_id}` }), pill("failed"),
        el("span", { class: "error-text", text: run.error }))));
      continue;
    }
    const selected = model.selected && model.selected.kind === "run" && model.selected.item === run.item && model.selected.runId === run.run_id;
    runList.append(el("li", {}, el("button", { type: "button", class: `row${selected ? " active" : ""}`, "aria-current": selected ? "true" : null,
      on: { click: () => model.onRun(run.item, run.run_id) } },
    el("span", { class: "row-main", text: run.item }),
    pill(run.status),
    el("span", { class: "muted", text: `${run.run_id} - ${run.graph}` }),
    el("span", { class: "muted", text: [formatTime(run.started), formatCost(run.cost_usd)].filter(Boolean).join(" - ") }))));
  }
  host.append(runList);
}
