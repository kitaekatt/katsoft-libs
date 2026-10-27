# llm_kit.graph viewer: `llm-kit graph serve`

The viewer shows the graphs of a project and the runs on disk. It is read-only: it starts no run, writes no file and calls no model. It needs no dependency beyond the `graph` extra: the server is `http.server`, the page is plain HTML, CSS and JavaScript, and the picture is SVG drawn by the page. Modules: `llm_kit/graph/viewer/` (`server.py`, `runs.py`, `structure.py`, `static/`) and `llm_kit/graph/commands/serve.py`.

The file format is in [llm-kit-graph.md](llm-kit-graph.md). The run records the viewer reads are in [llm-kit-graph-runtime.md](llm-kit-graph-runtime.md).

## Start it

```text
llm-kit graph serve [--project DIR] [--port N]
```

`--project` is the project root (default: the current directory). `--port 0` (the default) picks a free port. The command prints `llm-kit graph viewer: http://127.0.0.1:<port>/ ...` and runs until Ctrl-C. It exits 1 with one line when the config is missing, a `viewer` file does not exist or the port is busy. Open the address in a browser.

The page links to one view with a URL fragment: `#run=<item>/<run id>&node=<node path>` or `#graph=<name>`.

## What the page shows

- **Graphs and runs** (left). A graph shows its structure only. A run shows what happened.
- **The picture** (centre). A box is a node. Edges run left to right from the node that is read to the node that reads it. A box has a status dot, a status color, its id, its op or model, and time and cost.
  - A `graph`, `map` or `repeat` box has a + control. + opens the body inside the box.
  - An open `repeat` shows one chip per round with its score (`R2 8.5`). An open `map` shows one chip per item. The chip you pick decides which round or item the body shows. A closed `repeat` lists its scores; a closed `map` counts its items.
  - `Fit width` or `Actual size`, `Expand all` and `Collapse all` are above the picture.
- **The inspector** (right). A click on a box fills it. It shows: status, times, model, tokens and cost; the panels of the project; the exact prompt that was sent; the raw answer; the output; each input; the parameters as run; the values read; media (an image thumbnail, an audio or video player); the receipt; the node definition. A large value is cut to a preview with a link to the whole text.
- **Notices**. A run that stopped shows why. A run that says `running` and has not changed for 10 minutes is marked as possibly dead. Records whose paths match no node of the run's graph are listed.

Status colors: `pending`, `running`, `done`, `reused`, `seeded`, `skipped`, `failed`, `gated`, `stopped`. The runtime records a node when it ends. So in a run with status `running`, the viewer shows a node with no record as `running` when everything it reads is complete, and a box with records below it but none of its own as `running`.

### The hover popover

Every extra detail opens in one shared popover, never in a page jump or a `title` tooltip.

- Hover or keyboard focus on a trigger opens it. A click (a tap on a phone), or Enter or Space on a box, pins it.
- Esc or the x button closes it and returns focus to the trigger.
- The mouse leaving the trigger and the popover closes it unless it is pinned. A pinned popover stays when the mouse passes over another trigger.
- It has a maximum size, stays inside the window and scrolls.

Panels get the same behavior through `api.popover(trigger, build)`.

## Routes

All routes are `GET` (and `HEAD`).

| Route | Answer |
|---|---|
| `/` | the page |
| `/static/<file>` | one of the page's files (a fixed list, `server.STATIC_FILES`) |
| `/theme.css` | the project theme (`viewer.theme`), or empty CSS |
| `/panels/<n>.mjs` | the n-th panel module (`viewer.panels`) |
| `/project/<path>` | a `.mjs`, `.js` or `.css` file inside the project root; no dot-prefixed part, no link out of the root |
| `/api/session` | project, graph names, panel and theme URLs, `writable`, and the token only when a write route exists |
| `/api/graph/<name>` | the structure of a configured graph (the file as it is now) |
| `/api/runs` | every run and seed: item, run id, kind, status, graph, times, counts, cost; an unreadable record is listed with its error |
| `/api/run/<item>/<run id>` | one run: header, `structure` (the graph as it was when the run ran), `nodes` (a summary per node path), `unmapped` |
| `/api/run/<item>/<run id>/node?path=` | one node's record with every reference resolved |
| `/api/ref/<item>/<ref>` | the whole text of a `value:` or `blob:` reference, as plain text |
| `/media/<item>/<name>` | a media file of the item (`<16 hex><ext>`) |

A run's node summaries carry `status`, `synthetic` (no record of its own), `derived` (shown as running by inference), `kind`, `index`, times, `model`, `effort`, `cost_usd`, `error`, `media` (count) and `score`. A round's `score` is the number `score` in that round's entry of the repeat node's `rounds` attribute.

An error answers JSON `{"error": "..."}`: 400 for a bad query, 403 for a foreign host, 404 for an unknown run, node, file or route, 405 for a write, 422 for a graph or record that cannot be read.

## What the viewer assumes about records

`runs.py` is the only code that reads record fields. It reads `llm-kit.graph.run/v1` through `llm_kit.graph.records.RunStore`: `status`, `kind`, `graph` and `graphs` (the structure table), `stop`, `counts`, and per node `kind`, `op`, `status`, `attributes`, `inputs`, `params`, `reads`, `receipt`, `media`, `error`, `reused_from`, `seed`, `index`, `until`, `carry`, `seconds`, `started_at`, `ended_at`. A node path joins ids with `/`; `id[key]` names one map item or repeat round (`plot/loop[2]/write`, `poses[p-3]/render`).

From a receipt it reads, where present: `resolved.reported_model` or `resolved.requested_model` (else `model`), `resolved.effort.sent` (else `effort`), `usage`, and `cost.usd` (else `cost.estimated_usd`, else `cost_usd`). This is the protocol 2 result document that `llm.model` stores; another op may return its own receipt with `model`, `effort` and `cost_usd`.

Limits:

- The run file is sorted by key, so the viewer orders map items by their `index` and repeat rounds by number. Nodes in one column of a graph follow the file's key order.
- The viewer does not compare a run's graph hash with the graph file as it is now. The run view always shows the graph the run recorded. The graph-only view shows the file as it is now.
- A reused composite has no inner records in the new run; the box shows no children. Open the run named in `reused from`.
- Node statuses the viewer does not know are shown as they are, in the default color.

## Extension points for a project

Both are set in `.llm-kit/graphs.yaml` (section `viewer`, see [llm-kit-graph.md](llm-kit-graph.md)):

```yaml
viewer:
  panels: [pipeline/moments/beta/graph/viewer/panels.mjs]
  theme: pipeline/moments/beta/graph/viewer/theme.css
```

### Theme

The theme is a stylesheet that loads after the viewer's own. It sets CSS variables on `:root`, or overrides any class. Variables: surface (`--bg`, `--panel`, `--panel2`, `--line`, `--ink`, `--muted`, `--soft`, `--accent`, `--link`, `--shadow`, `--canvas-bg`), node kind (`--kind-op`, `--kind-graph`, `--kind-map`, `--kind-repeat`, `--kind-human`, `--kind-commit`), status (`--st-pending`, `--st-running`, `--st-waiting`, `--st-halted`, `--st-failed`, `--st-done`, `--st-reused`, `--st-seeded`, `--st-skipped`, `--st-gated`, `--st-stopped`, `--st-refused`, `--st-partial`) and fonts (`--mono`, `--sans`). The viewer follows the system light or dark setting; the light values are set under `prefers-color-scheme: light`, so a theme that must hold in both redefines the variables in both places.

### Panels

A panel module is an ES module. Its default export (or `panels`) is a panel or a list of panels:

```js
export default [{
  id: "verdict",                                  // required, unique
  title: "Verdict",                               // heading (default: id)
  applies: (node) => node.out && typeof node.out.score === "number",
  render: (node, api) => api.el("p", {}, `Score ${node.out.score}`),   // a DOM node, or text
}];
```

The inspector calls `applies` and `render` for the selected node, after the facts and before the recorded values. A panel that throws shows its error in its own section and in the console; the rest of the page still works.

The content security policy blocks the `style` attribute and `el.style.cssText`. A panel sets a style only with `el.style.setProperty("name", "value")`, or with a class that a theme stylesheet defines.

`node` has: `path`, `id`, `kind`, `op`, `status`, `summary` (the picture's summary), `detail` (the whole node answer), `run` (`{item, run_id, graph, status}`), `structure`, and the recorded values `inputs`, `params`, `reads`, `out`, `attributes`, `receipt`, `facts` (`model`, `effort`, `cost_usd`, `usage`), `media` (each with `url`, `mime`, `name`).

`api` has: `el(tag, props, ...children)`, `text(value)`, `pre(string)`, `json(value)`, `popover(trigger, build, {id, keyActivate})`, `formatSeconds`, `formatCost`. Build the output with `api.el` or `document.createElement`. Values are text; do not build markup from them.

A panel file inside the project is served at `/project/<path>`, so it can import its neighbours with a relative import (`import { render } from "../../render/brief.mjs"`). `/project/<path>` serves `.mjs`, `.js`, `.css` and `.json` (as `application/json`), so a panel can import dictionary JSON with an ES module JSON import (`import words from "../dict/words.json" with { type: "json" }`). Dot-leading parts, traversal and links out of the project are refused. A panel file outside the project (a path in `graphs.local.yaml`) is served at `/panels/<n>.mjs` and cannot import relative files.

## Protection

- The server binds `127.0.0.1` only. There is no host option.
- A request whose `Host` header is not `127.0.0.1`, `localhost` or `[::1]` with the server's port is refused (403), which stops DNS rebinding.
- Every response has `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Cross-Origin-Resource-Policy: same-origin`, `X-Frame-Options: DENY` and a content security policy of the own origin only: no inline script, no inline style, no `eval`, no frame.
- JSON is sent with ASCII escapes and with `<`, `>` and `&` escaped. Stored text is sent as `text/plain`. A media file is shown only under an image, audio or video type chosen from its extension; any other file is a download.
- The page never builds markup from text: it uses `createElement`, `createTextNode` and `setAttribute`. `tests/llm-kit/graph/test_viewer.py` fails if a page file contains `innerHTML` or a similar call.
- `/project/` serves only `.mjs`, `.js` and `.css` files, never a path part that starts with a dot, and never a file whose real path is outside the project root.

## Write hook (answer forms, unit L05)

The viewer has no write route. `ViewerApp.add_post(path, handler)` is the hook for the answer forms of human nodes. A route must be new and under `/api/`. A `POST` to a registered route needs all of these: an `Origin` equal to the viewer's own address, the per-process token in `X-Viewer-Token` (shown by `/api/session` only when a route exists), `Content-Type: application/json`, a body of at most 1 MB that is a JSON object. `handler(body)` returns a JSON value. A `POST` to any other path answers 405. `serve()` does not register routes; the answer verb will.

## Check it in a browser

The fixture project `tests/llm-kit/graph/fixtures/viewer/project` has one item with four runs (a seed, a failed run with a failed map item, a run stopped at its human node, a run still running), a sample panel and a sample theme.

```text
uv run --extra dev llm-kit graph serve --project tests/llm-kit/graph/fixtures/viewer/project --port 7391
```

Open `http://127.0.0.1:7391/#run=night-petition/20261026T100000000000Z-a001`. Then: Expand all; click the `p3` chip of `poses`; hover a box (popover opens); click it (popover pins, inspector fills, a `Verdict` panel shows on an `evaluate` node); press Esc (popover closes, focus returns to the box); open `poses[p2]/render` (a picture thumbnail and an audio player); check at 390 px width.
