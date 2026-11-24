// The graph browser: draws one graph file of the embedded catalog at a time.
//
// The catalog (llm_kit.graph.render.catalog) is in the page as JSON. The list
// on the left holds the entry graphs and every graph file they run. The
// picture is laid out by ELK (layered, left to right, orthogonal edges). A node
// that runs a graph (graph, map, repeat) opens in place with its + button and
// closes with -; double-click, or "Open graph file" in the details panel, moves
// into the graph file it runs. All text from the catalog is set as text, never
// parsed as markup.
(function () {
  "use strict";

  var CATALOG = JSON.parse(document.getElementById("catalog").textContent);
  var SVGNS = "http://www.w3.org/2000/svg";
  var CARD_W = 214, CARD_H = 58, IO_W = 136, IO_H = 30, HEAD = 44, MAX_DEPTH = 6;
  var KIND_LABEL = { op: "OP", llm: "LLM", human: "HUMAN", commit: "COMMIT", graph: "GRAPH", map: "MAP", repeat: "REPEAT" };
  var KIND_HELP = {
    op: "runs an operation", llm: "calls a language model", human: "waits for a person",
    commit: "writes the result out", graph: "runs a graph file", map: "runs a graph once per item",
    repeat: "runs a graph in rounds",
  };
  var ELK_OPTIONS = {
    "elk.algorithm": "layered",
    "elk.direction": "RIGHT",
    "elk.hierarchyHandling": "INCLUDE_CHILDREN",
    "elk.edgeRouting": "ORTHOGONAL",
    "elk.layered.spacing.nodeNodeBetweenLayers": "56",
    "elk.spacing.nodeNode": "22",
    "elk.layered.spacing.edgeNodeBetweenLayers": "16",
    "elk.spacing.edgeEdge": "8",
    "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
    "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
    "elk.padding": "[top=30,left=30,bottom=30,right=30]",
  };

  var elk = new ELK();
  var state = { trail: [], open: new Set(), sel: null, view: { k: 1, x: 0, y: 0 }, query: "", layoutId: 0, allEdges: false };
  var meta = new Map();   // drawn element id -> {node, key, kind, ...}
  var drawn = { edges: [], size: { w: 0, h: 0 } };

  // ------------------------------------------------------------------ helpers

  function byId(id) { return document.getElementById(id); }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    setAll(node, attrs);
    for (var i = 2; i < arguments.length; i++) add(node, arguments[i]);
    return node;
  }

  function sv(tag, attrs) {
    var node = document.createElementNS(SVGNS, tag);
    setAll(node, attrs);
    for (var i = 2; i < arguments.length; i++) add(node, arguments[i]);
    return node;
  }

  function setAll(node, attrs) {
    if (!attrs) return;
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "onclick" || k === "ondblclick") node.addEventListener(k.slice(2), v);
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    });
  }

  function add(node) {
    for (var i = 1; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid === null || kid === undefined || kid === false) continue;
      if (Array.isArray(kid)) { kid.forEach(function (k) { add(node, k); }); continue; }
      node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    }
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function base(path) { var i = path.lastIndexOf("/"); return i < 0 ? path : path.slice(i + 1); }
  function dir(path) { var i = path.lastIndexOf("/"); return i < 0 ? "" : path.slice(0, i); }

  function shortId(id) {
    // The last dotted segment, unless it is too short to read alone.
    var parts = String(id || "").split(".");
    return parts.length > 1 && parts[parts.length - 1].length > 2 ? parts[parts.length - 1] : String(id || "");
  }

  function fit(text, width, charW) {
    text = String(text || "");
    var max = Math.floor(width / charW);
    return text.length > max ? text.slice(0, Math.max(1, max - 1)) + "\u2026" : text;
  }

  function kindOf(node) {
    if (node.kind === "op") return node.detail && node.detail.op === "llm.model" ? "llm" : "op";
    return node.kind;
  }

  function kindColor(kind) { return "var(--" + (kind === "input" || kind === "output" ? "io" : kind) + ")"; }

  function cardSub(node) {
    var kind = kindOf(node), def = node.definition || {};
    if (kind === "llm") return typeof def.model === "string" ? def.model : "llm.model";
    if (kind === "op") return shortId(node.detail && node.detail.op);
    if (kind === "human") return "asks: " + (node.detail.kind || "");
    if (kind === "commit") return shortId(node.detail.adapter);
    var target = node.ref ? CATALOG.files[node.ref] : null;
    if (kind === "map") return "each " + fit(node.detail.over || "item", 140, 6) + (target ? " \u2192 " + shortId(target.id) : "");
    if (kind === "repeat") return "up to " + (node.detail.max || "?") + " rounds" + (target ? " \u2192 " + shortId(target.id) : "");
    return target ? shortId(target.id) : base(node.ref || "");
  }

  function graphAt(file) { return CATALOG.files[file]; }

  function current() { return state.trail[state.trail.length - 1]; }

  // ------------------------------------------------------------------ list

  function fileMatches(graph, q) {
    if (!q) return true;
    var hay = [graph.id, graph.file, graph.description];
    (function walk(nodes) {
      nodes.forEach(function (n) {
        hay.push(n.id, n.subtitle);
        if (n.body) walk(n.body.nodes);
      });
    })(graph.nodes);
    return hay.join("\n").toLowerCase().indexOf(q) >= 0;
  }

  function countNodes(graph) {
    var count = 0;
    (function walk(nodes) { nodes.forEach(function (n) { count++; if (n.body) walk(n.body.nodes); }); })(graph.nodes);
    return count;
  }

  function listItem(file, label, isEntry) {
    var graph = graphAt(file);
    var used = (CATALOG.used_by[file] || []).length;
    var here = current() && current().file === file;
    return el("div", {
      "class": "item" + (isEntry ? " entry" : "") + (here ? " on" : ""), title: file,
      onclick: function () { go([{ file: file }]); },
    },
    el("div", { "class": "name", text: label }),
    graph.description ? el("div", { "class": "desc", text: graph.description }) : null,
    el("div", { "class": "meta" },
      el("span", { text: countNodes(graph) + " nodes" }),
      used ? el("span", { text: "used by " + used }) : null,
      isEntry ? el("span", { text: base(file) }) : null));
  }

  function renderList() {
    var list = byId("list"), q = state.query.trim().toLowerCase();
    clear(list);
    var entries = CATALOG.entries.filter(function (e) { return fileMatches(graphAt(e.file), q); });
    if (entries.length) {
      add(list, el("div", { "class": "group-title", text: "Entry graphs" }));
      entries.forEach(function (e) { add(list, listItem(e.file, e.name, true)); });
    }
    var entryFiles = new Set(CATALOG.entries.map(function (e) { return e.file; }));
    var rest = Object.keys(CATALOG.files).filter(function (f) {
      return !entryFiles.has(f) && fileMatches(graphAt(f), q);
    });
    if (rest.length) {
      add(list, el("div", { "class": "group-title", text: "Graph files they run (" + rest.length + ")" }));
      var folders = {};
      rest.forEach(function (f) { (folders[dir(f)] = folders[dir(f)] || []).push(f); });
      Object.keys(folders).sort().forEach(function (folder) {
        add(list, el("div", { "class": "folder", text: (folder || ".") + "/" }));
        folders[folder].forEach(function (f) { add(list, listItem(f, shortId(graphAt(f).id), false)); });
      });
    }
    if (!entries.length && !rest.length) add(list, el("div", { "class": "empty", text: "Nothing matches \u201c" + state.query + "\u201d." }));
  }

  // ------------------------------------------------------------------ navigation

  function go(trail, selectKey) {
    state.trail = trail;
    state.open = new Set();
    state.sel = null;
    if (selectKey) {
      var parts = selectKey.split("/");
      for (var i = 1; i < parts.length; i++) state.open.add(parts.slice(0, i).join("/"));
      state.sel = selectKey;
    }
    writeHash();
    renderList();
    renderCrumbs();
    layout(true);
  }

  function enter(key, node) {
    if (!node.ref) return;
    var trail = state.trail.concat([{ file: node.ref, via: { file: current().file, node: key } }]);
    go(trail);
  }

  function renderCrumbs() {
    var crumbs = byId("crumbs");
    clear(crumbs);
    state.trail.forEach(function (step, i) {
      if (i) add(crumbs, el("span", { "class": "gt", text: "\u203a" }));
      var graph = graphAt(step.file);
      var label = step.via ? step.via.node.split("/").pop() + " (" + shortId(graph.id) + ")" : (entryName(step.file) || shortId(graph.id));
      var last = i === state.trail.length - 1;
      add(crumbs, el("a", {
        "class": last ? "here" : null, title: step.file,
        onclick: function () { if (!last) go(state.trail.slice(0, i + 1)); },
      }, label));
    });
  }

  function entryName(file) {
    for (var i = 0; i < CATALOG.entries.length; i++) if (CATALOG.entries[i].file === file) return CATALOG.entries[i].name;
    return null;
  }

  function writeHash() {
    var parts = state.trail.map(function (s) { return s.via ? s.via.node : ""; });
    var hash = "f=" + encodeURIComponent(state.trail[0].file);
    if (parts.length > 1) hash += "&p=" + encodeURIComponent(parts.slice(1).join("|"));
    if (state.sel) hash += "&s=" + encodeURIComponent(state.sel);
    history.replaceState(null, "", "#" + hash);
  }

  function readHash() {
    var params = {};
    location.hash.replace(/^#/, "").split("&").forEach(function (pair) {
      var i = pair.indexOf("=");
      if (i > 0) params[pair.slice(0, i)] = decodeURIComponent(pair.slice(i + 1));
    });
    if (!params.f || !graphAt(params.f)) return null;
    var trail = [{ file: params.f }];
    (params.p ? params.p.split("|") : []).forEach(function (nodePath) {
      var from = trail[trail.length - 1].file, node = findNode(graphAt(from), nodePath);
      if (node && node.ref) trail.push({ file: node.ref, via: { file: from, node: nodePath } });
    });
    return { trail: trail, sel: params.s || null };
  }

  function findNode(graph, path) {
    var nodes = graph.nodes, found = null;
    path.split("/").forEach(function (id) {
      found = null;
      for (var i = 0; nodes && i < nodes.length; i++) if (nodes[i].id === id) found = nodes[i];
      nodes = found && found.body ? found.body.nodes : null;
    });
    return found;
  }

  // ------------------------------------------------------------------ layout

  function nodeId(key) { return "n:" + key; }

  function buildLevel(graph, scope, depth, files, edges) {
    var children = [];
    graph.nodes.forEach(function (node) {
      var key = scope ? scope + "/" + node.id : node.id;
      var id = nodeId(key);
      var target = node.ref ? graphAt(node.ref) : node.body;
      var loops = node.ref && files.indexOf(node.ref) >= 0;
      var canOpen = !!target && depth < MAX_DEPTH && !loops;
      var info = { node: node, key: key, kind: kindOf(node), canOpen: canOpen, compound: false, graph: graph };
      meta.set(id, info);
      if (canOpen && state.open.has(key)) {
        info.compound = true;
        info.target = target;
        children.push({
          id: id,
          layoutOptions: { "elk.padding": "[top=" + HEAD + ",left=14,bottom=14,right=14]" },
          children: buildLevel(target, key, depth + 1, node.ref ? files.concat([node.ref]) : files, edges),
        });
      } else {
        children.push({ id: id, width: CARD_W, height: CARD_H });
      }
    });
    var shown = state.allEdges ? null : mainDeps(graph.nodes);
    graph.nodes.forEach(function (node) {
      var key = scope ? scope + "/" + node.id : node.id;
      (shown ? shown[node.id] : node.deps).forEach(function (dep) {
        var from = nodeId(scope ? scope + "/" + dep : dep);
        edges.push({ id: "e" + edges.length, sources: [from], targets: [nodeId(key)], kind: "dep" });
      });
    });
    return children;
  }

  // The reads that are not implied by a longer path: a read of d is left out
  // when another read of the same node already depends on d, directly or not.
  function mainDeps(nodes) {
    var deps = {}, reach = {};
    nodes.forEach(function (n) { deps[n.id] = n.deps; });
    function reachable(id, stack) {
      if (reach[id]) return reach[id];
      var found = new Set();
      if (stack.has(id)) return found;
      stack.add(id);
      (deps[id] || []).forEach(function (d) {
        found.add(d);
        reachable(d, stack).forEach(function (x) { found.add(x); });
      });
      stack.delete(id);
      reach[id] = found;
      return found;
    }
    var result = {};
    nodes.forEach(function (n) {
      result[n.id] = n.deps.filter(function (d) {
        return !n.deps.some(function (other) { return other !== d && reachable(other, new Set()).has(d); });
      });
    });
    return result;
  }

  function buildGraph(graph, file) {
    meta.clear();
    var edges = [];
    var children = buildLevel(graph, "", 0, [file], edges);
    var inputs = [], outputs = [];
    graph.inputs.forEach(function (port) {
      var readers = [];
      (function walk(nodes, scope) {
        nodes.forEach(function (n) {
          var key = scope ? scope + "/" + n.id : n.id;
          var info = meta.get(nodeId(key));
          if (n.reads_inputs.indexOf(port.name) >= 0) readers.push(key);
          if (info && info.compound && n.body) walk(n.body.nodes, key);
        });
      })(graph.nodes, "");
      var id = "i:" + port.name;
      meta.set(id, { io: "input", name: port.name, port: port, readers: readers });
      inputs.push({ id: id, width: IO_W, height: IO_H });
      // Lines go only to the readers that read no sibling node; selecting the
      // input highlights every reader.
      readers.forEach(function (key) {
        var n = findNode(graph, key);
        if (!state.allEdges && n && n.deps.length && key.indexOf("/") < 0) return;
        edges.push({ id: "e" + edges.length, sources: [id], targets: [nodeId(key)], kind: "input" });
      });
    });
    graph.outputs.forEach(function (name) {
      var id = "o:" + name;
      meta.set(id, { io: "output", name: name });
      outputs.push({ id: id, width: IO_W, height: IO_H });
      (graph.output_sources[name] || []).forEach(function (src) {
        edges.push({ id: "e" + edges.length, sources: [nodeId(src)], targets: [id], kind: "output" });
      });
    });
    inputs.forEach(function (n) { n.layoutOptions = { "elk.layered.layering.layerConstraint": "FIRST" }; });
    outputs.forEach(function (n) { n.layoutOptions = { "elk.layered.layering.layerConstraint": "LAST" }; });
    return { id: "root", layoutOptions: ELK_OPTIONS, children: inputs.concat(children, outputs), edges: edges };
  }

  function layout(refit) {
    var step = current(), graph = graphAt(step.file);
    var token = ++state.layoutId;
    byId("status").textContent = "Laying out\u2026";
    elk.layout(buildGraph(graph, step.file)).then(function (laid) {
      if (token !== state.layoutId) return;
      byId("status").textContent = "";
      draw(laid);
      if (refit) fitView();
      select(state.sel, true);
    }).catch(function (err) {
      byId("status").textContent = "The layout failed: " + err.message;
      throw err;
    });
  }

  // ------------------------------------------------------------------ drawing

  function draw(laid) {
    var vp = byId("viewport");
    clear(vp);
    var abs = new Map([["root", { x: 0, y: 0 }]]);
    var frames = sv("g"), edgesG = sv("g"), cards = sv("g");
    (function walk(node, ox, oy) {
      (node.children || []).forEach(function (c) {
        var x = ox + c.x, y = oy + c.y;
        abs.set(c.id, { x: x, y: y, w: c.width, h: c.height });
        var info = meta.get(c.id);
        if (info && info.io) add(cards, drawIo(c.id, info, x, y));
        else if (info && info.compound) { add(frames, drawCompound(c.id, info, x, y, c.width, c.height)); walk(c, x, y); }
        else if (info) add(cards, drawCard(c.id, info, x, y));
      });
    })(laid, 0, 0);
    drawn.edges = [];
    (laid.edges || []).forEach(function (e) {
      var origin = abs.get(e.container || "root") || { x: 0, y: 0 };
      (e.sections || []).forEach(function (sec) {
        var pts = [sec.startPoint].concat(sec.bendPoints || [], [sec.endPoint]).map(function (p) {
          return { x: p.x + origin.x, y: p.y + origin.y };
        });
        var path = sv("path", { "class": "edge" + (e.kind === "input" ? " input" : ""), d: roundedPath(pts), "marker-end": "url(#arrow)" });
        path.dataset.from = e.sources[0];
        path.dataset.to = e.targets[0];
        drawn.edges.push(path);
        add(edgesG, path);
      });
    });
    add(vp, defs(), frames, edgesG, cards);
    drawn.size = { w: laid.width || 0, h: laid.height || 0 };
  }

  function defs() {
    function marker(id, cls) {
      return sv("marker", { id: id, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
        sv("path", { d: "M0,0 L10,5 L0,10 z", "class": cls }));
    }
    return sv("defs", null, marker("arrow", "arrow"), marker("arrow-hot", "arrow-hot"));
  }

  function roundedPath(pts) {
    var r = 6, d = "M" + pts[0].x + "," + pts[0].y;
    for (var i = 1; i < pts.length - 1; i++) {
      var p = pts[i], a = pts[i - 1], b = pts[i + 1];
      var r1 = Math.min(r, dist(a, p) / 2), r2 = Math.min(r, dist(p, b) / 2);
      var s = toward(p, a, r1), e = toward(p, b, r2);
      d += " L" + s.x + "," + s.y + " Q" + p.x + "," + p.y + " " + e.x + "," + e.y;
    }
    var last = pts[pts.length - 1];
    return d + " L" + last.x + "," + last.y;
  }
  function dist(a, b) { return Math.abs(a.x - b.x) + Math.abs(a.y - b.y); }
  function toward(p, q, r) {
    var d = dist(p, q) || 1;
    return { x: p.x + (q.x - p.x) * r / d, y: p.y + (q.y - p.y) * r / d };
  }

  function toggleButton(info, x, y, open) {
    var g = sv("g", { "class": "toggle", transform: "translate(" + x + "," + y + ")" },
      sv("title", { text: open ? "Close this subgraph" : "Open this subgraph in place" }),
      sv("rect", { width: 20, height: 20, rx: 4 }),
      sv("text", { x: 10, y: 14.5, "text-anchor": "middle", text: open ? "\u2212" : "+" }));
    g.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (open) state.open.delete(info.key); else state.open.add(info.key);
      state.sel = info.key;
      layout(false);
    });
    return g;
  }

  function drawCard(id, info, x, y) {
    var node = info.node, kind = info.kind;
    var g = sv("g", {
      "class": "node" + (node.when ? " when" : ""), "data-id": id, transform: "translate(" + x + "," + y + ")",
      style: "--c:" + kindColor(kind),
    },
    sv("title", { text: node.id + " \u2014 " + KIND_HELP[kind] + (node.when ? " (only when " + node.when + ")" : "") }),
    sv("rect", { "class": "box", width: CARD_W, height: CARD_H, rx: 8 }),
    sv("rect", { "class": "stripe", width: 4, height: CARD_H - 16, x: 0, y: 8, rx: 2 }),
    sv("text", { "class": "tag", x: 14, y: 17, text: KIND_LABEL[kind] + (node.when ? " \u00b7 WHEN" : "") }),
    sv("text", { "class": "title", x: 14, y: 34, text: fit(node.id, CARD_W - 44, 7.2) }),
    sv("text", { "class": "sub", x: 14, y: 49, text: fit(cardSub(node), CARD_W - 22, 6.1) }));
    if (info.canOpen) add(g, toggleButton(info, CARD_W - 28, 8, false));
    g.addEventListener("click", function () { select(info.key); });
    g.addEventListener("dblclick", function () { if (node.ref) enter(info.key, node); });
    return g;
  }

  function drawCompound(id, info, x, y, w, h) {
    var node = info.node, kind = info.kind, target = info.target;
    var sub = node.ref ? (target.id + "  \u00b7  " + base(node.ref)) : "inline body";
    var g = sv("g", {
      "class": "compound node", "data-id": id, transform: "translate(" + x + "," + y + ")", style: "--c:" + kindColor(kind),
    },
    sv("rect", { "class": "frame", width: w, height: h, rx: 10 }),
    sv("text", { "class": "tag", x: 14, y: 17, text: KIND_LABEL[kind] }),
    sv("text", { "class": "ctitle", x: 14 + KIND_LABEL[kind].length * 7 + 6, y: 17, text: node.id }),
    sv("text", { "class": "csub", x: 14, y: 34, text: fit(sub + (kind === "repeat" ? "  \u00b7  up to " + node.detail.max + " rounds" : ""), w - 50, 6.1) }));
    add(g, toggleButton(info, w - 28, 8, true));
    g.addEventListener("click", function (ev) { if (ev.target.closest(".node") === g) select(info.key); });
    g.addEventListener("dblclick", function (ev) { if (ev.target.closest(".node") === g && node.ref) enter(info.key, node); });
    return g;
  }

  function drawIo(id, info, x, y) {
    var g = sv("g", { "class": "io", "data-id": id, transform: "translate(" + x + "," + y + ")" },
      sv("title", { text: (info.io === "input" ? "Graph input " : "Graph output ") + info.name }),
      sv("rect", { width: IO_W, height: IO_H, rx: IO_H / 2 }),
      sv("text", { "class": "iotag", x: 12, y: 19, text: info.io === "input" ? "IN" : "OUT" }),
      sv("text", { x: info.io === "input" ? 32 : 40, y: 19.5, text: fit(info.name, IO_W - 46, 7) }));
    g.addEventListener("click", function () { select(id); });
    return g;
  }

  // ------------------------------------------------------------------ selection and details

  function select(key, quiet) {
    state.sel = key || null;
    var svg = byId("canvas");
    var target = key ? (key.indexOf("i:") === 0 || key.indexOf("o:") === 0 ? key : nodeId(key)) : null;
    var near = new Set(target ? [target] : []);
    var picked = target ? meta.get(target) : null;
    if (picked && picked.readers) picked.readers.forEach(function (k) { near.add(nodeId(k)); });
    drawn.edges.forEach(function (path) {
      var hot = target && (path.dataset.from === target || path.dataset.to === target);
      path.classList.toggle("hot", !!hot);
      path.setAttribute("marker-end", hot ? "url(#arrow-hot)" : "url(#arrow)");
      if (hot) { near.add(path.dataset.from); near.add(path.dataset.to); }
    });
    svg.classList.toggle("focus", !!target);
    svg.querySelectorAll("[data-id]").forEach(function (g) {
      g.classList.toggle("sel", g.dataset.id === target);
      g.classList.toggle("near", near.has(g.dataset.id));
    });
    if (!quiet) writeHash();
    renderInspector(target);
  }

  function chipList(keys, scope) {
    if (!keys.length) return el("div", { "class": "file", text: "none" });
    return el("div", { "class": "chips" }, keys.map(function (k) {
      var key = scope ? scope + "/" + k : k;
      return el("span", { "class": "chip", text: k, onclick: function () { select(key); } });
    }));
  }

  function kv(pairs) {
    var dl = el("dl", { "class": "kv" });
    pairs.forEach(function (p) {
      if (p[1] === null || p[1] === undefined || p[1] === "") return;
      add(dl, el("dt", { text: p[0] }), el("dd", { text: typeof p[1] === "string" ? p[1] : JSON.stringify(p[1]) }));
    });
    return dl;
  }

  function usedByList(file) {
    var uses = CATALOG.used_by[file] || [];
    if (!uses.length) return el("div", { "class": "file", text: entryName(file) ? "an entry graph" : "nothing in this catalog" });
    return el("ul", null, uses.map(function (u) {
      return el("li", null, el("a", {
        text: shortId(graphAt(u.file).id) + " \u203a " + u.node,
        onclick: function () { go([{ file: u.file }], u.node); },
      }));
    }));
  }

  function renderInspector(target) {
    var box = byId("inspector");
    clear(box);
    var step = current(), graph = graphAt(step.file);
    var info = target ? meta.get(target) : null;
    if (!info) { renderOverview(box, graph); return; }
    if (info.io) {
      add(box, el("span", { "class": "pill", style: "--c:var(--io)", text: info.io === "input" ? "GRAPH INPUT" : "GRAPH OUTPUT" }),
        el("h2", { text: info.name }));
      if (info.port && info.port.description) add(box, el("p", { text: info.port.description }));
      var readers = [];
      if (info.io === "input") meta.forEach(function (m) { if (m.node && m.node.reads_inputs.indexOf(info.name) >= 0) readers.push(m.key); });
      add(box, el("h3", { text: info.io === "input" ? "Read by" : "Comes from" }),
        chipList(info.io === "input" ? readers : (graph.output_sources[info.name] || []), ""));
      return;
    }
    var node = info.node, kind = info.kind, scope = info.key.split("/").slice(0, -1).join("/");
    var readBy = info.graph.nodes.filter(function (n) { return n.deps.indexOf(node.id) >= 0; }).map(function (n) { return n.id; });
    add(box,
      el("span", { "class": "pill", style: "--c:" + kindColor(kind), text: KIND_LABEL[kind] }),
      node.when ? el("span", { "class": "pill", style: "--c:var(--muted)", text: "WHEN" }) : null,
      el("h2", { text: node.id }),
      el("div", { "class": "file", text: info.key.indexOf("/") >= 0 ? info.key : KIND_HELP[kind] }),
      el("h3", { text: "What it is" }),
      kv([["op", node.detail && node.detail.op], ["model", (node.definition || {}).model], ["effort", (node.definition || {}).effort],
        ["human", kind === "human" ? node.detail.kind : null], ["adapter", node.detail && node.detail.adapter],
        ["over", node.detail && node.detail.over], ["key", node.detail && node.detail.key],
        ["max", node.detail && node.detail.max], ["until", node.detail && node.detail.until], ["when", node.when]]),
      el("h3", { text: "Reads" }), chipList(node.deps, scope),
      node.reads_inputs.length ? el("div", { "class": "file", text: "graph inputs: " + node.reads_inputs.join(", ") }) : null,
      el("h3", { text: "Read by" }), chipList(readBy, scope));
    if (node.ref) {
      var sub = graphAt(node.ref);
      add(box, el("h3", { text: "Runs" }),
        el("div", null, el("strong", { text: sub.id })), el("div", { "class": "file", text: node.ref }),
        sub.description ? el("p", { text: sub.description }) : null,
        el("div", { "class": "actions" },
          el("button", { type: "button", text: "Open graph file", onclick: function () { enter(info.key, node); } }),
          info.canOpen ? el("button", {
            type: "button", text: state.open.has(info.key) ? "Close in place" : "Open in place",
            onclick: function () { if (state.open.has(info.key)) state.open.delete(info.key); else state.open.add(info.key); layout(false); },
          }) : null));
    } else if (node.body) {
      add(box, el("h3", { text: "Runs" }), el("div", { "class": "file", text: "an inline body with " + node.body.nodes.length + " nodes" }),
        info.canOpen ? el("div", { "class": "actions" }, el("button", {
          type: "button", text: state.open.has(info.key) ? "Close in place" : "Open in place",
          onclick: function () { if (state.open.has(info.key)) state.open.delete(info.key); else state.open.add(info.key); layout(false); },
        })) : null);
    }
    add(box, el("h3", { text: "As written" }), el("pre", { text: JSON.stringify(node.definition, null, 2) }));
  }

  function renderOverview(box, graph) {
    var counts = {};
    (function walk(nodes) { nodes.forEach(function (n) { var k = kindOf(n); counts[k] = (counts[k] || 0) + 1; if (n.body) walk(n.body.nodes); }); })(graph.nodes);
    var runs = [];
    (function walk(nodes) { nodes.forEach(function (n) { if (n.ref && runs.indexOf(n.ref) < 0) runs.push(n.ref); if (n.body) walk(n.body.nodes); }); })(graph.nodes);
    add(box,
      graph.outer ? el("span", { "class": "pill", style: "--c:var(--commit)", text: "OUTER GRAPH" }) : null,
      entryName(graph.file) ? el("span", { "class": "pill", style: "--c:var(--accent)", text: "ENTRY" }) : null,
      el("h2", { text: graph.id }),
      el("div", { "class": "file", text: graph.file }),
      graph.description ? el("p", { text: graph.description }) : null,
      el("h3", { text: "Nodes" }),
      el("div", { "class": "chips" }, Object.keys(counts).map(function (k) {
        return el("span", { "class": "pill", style: "--c:" + kindColor(k), text: counts[k] + " " + KIND_LABEL[k].toLowerCase() });
      })),
      el("h3", { text: "Inputs" }),
      graph.inputs.length ? el("ul", null, graph.inputs.map(function (p) {
        return el("li", null, el("a", { text: p.name, onclick: function () { select("i:" + p.name); } }),
          p.description ? el("div", { "class": "file", text: p.description }) : null);
      })) : el("div", { "class": "file", text: "none" }),
      el("h3", { text: "Outputs" }),
      graph.outputs.length ? el("div", { "class": "chips" }, graph.outputs.map(function (name) {
        return el("span", { "class": "chip", text: name, onclick: function () { select("o:" + name); } });
      })) : el("div", { "class": "file", text: "none" }),
      el("h3", { text: "Runs these graph files" }),
      runs.length ? el("ul", null, runs.map(function (f) {
        return el("li", null, el("a", { text: shortId(graphAt(f).id), title: f, onclick: function () { go([{ file: f }]); } }));
      })) : el("div", { "class": "file", text: "none" }),
      el("h3", { text: "Used by" }), usedByList(graph.file),
      el("p", { "class": "file", text: "Click a node for its details. + opens a subgraph in place; double-click moves into it." }));
  }

  // ------------------------------------------------------------------ pan and zoom

  function applyView() {
    var v = state.view;
    byId("viewport").setAttribute("transform", "translate(" + v.x + "," + v.y + ") scale(" + v.k + ")");
  }

  function fitView() {
    var svg = byId("canvas"), w = svg.clientWidth, h = svg.clientHeight;
    var gw = drawn.size.w || 1, gh = drawn.size.h || 1;
    var k = Math.min(1.25, Math.max(0.1, Math.min((w - 40) / gw, (h - 90) / gh)));
    state.view = { k: k, x: (w - gw * k) / 2, y: Math.max(20, (h - gh * k) / 2) };
    applyView();
  }

  function zoomAt(factor, cx, cy) {
    var v = state.view, k = Math.min(3, Math.max(0.08, v.k * factor));
    state.view = { k: k, x: cx - (cx - v.x) * (k / v.k), y: cy - (cy - v.y) * (k / v.k) };
    applyView();
  }

  function wireCanvas() {
    var svg = byId("canvas"), drag = null;
    svg.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var r = svg.getBoundingClientRect();
      zoomAt(Math.exp(-ev.deltaY * 0.0015), ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    svg.addEventListener("mousedown", function (ev) {
      if (ev.target.closest(".node, .io")) return;
      drag = { x: ev.clientX, y: ev.clientY, vx: state.view.x, vy: state.view.y, moved: false };
      svg.classList.add("panning");
    });
    window.addEventListener("mousemove", function (ev) {
      if (!drag) return;
      var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      state.view.x = drag.vx + dx; state.view.y = drag.vy + dy;
      applyView();
    });
    window.addEventListener("mouseup", function (ev) {
      if (drag && !drag.moved && !ev.target.closest(".node, .io")) select(null);
      drag = null;
      svg.classList.remove("panning");
    });
  }

  // ------------------------------------------------------------------ start

  function openAll(open) {
    var graph = graphAt(current().file);
    state.open = new Set();
    if (open) {
      (function walk(g, scope, depth, files) {
        g.nodes.forEach(function (n) {
          var key = scope ? scope + "/" + n.id : n.id;
          var target = n.ref ? graphAt(n.ref) : n.body;
          if (!target || depth >= 2 || (n.ref && files.indexOf(n.ref) >= 0)) return;
          state.open.add(key);
          walk(target, key, depth + 1, n.ref ? files.concat([n.ref]) : files);
        });
      })(graph, "", 0, [current().file]);
    }
    layout(true);
  }

  function start() {
    byId("project").textContent = CATALOG.project;
    document.title = CATALOG.project + " graphs";
    var legend = byId("legend");
    Object.keys(KIND_LABEL).forEach(function (k) { add(legend, el("span", { style: "--c:" + kindColor(k), text: KIND_HELP[k] })); });
    var saved = localStorage.getItem("llm-kit-graph-theme");
    if (saved) document.documentElement.dataset.theme = saved;
    byId("theme").addEventListener("click", function () {
      var light = document.documentElement.dataset.theme !== "light";
      document.documentElement.dataset.theme = light ? "light" : "dark";
      localStorage.setItem("llm-kit-graph-theme", light ? "light" : "dark");
    });
    var shell = byId("shell");
    if (CATALOG.entries.length === 1 && Object.keys(CATALOG.files).length === 1) shell.classList.add("no-side");
    byId("toggle-side").addEventListener("click", function () { shell.classList.toggle("no-side"); });
    byId("search").addEventListener("input", function (ev) { state.query = ev.target.value; renderList(); });
    byId("fit").addEventListener("click", fitView);
    byId("zoom-in").addEventListener("click", function () { var s = byId("canvas"); zoomAt(1.25, s.clientWidth / 2, s.clientHeight / 2); });
    byId("zoom-out").addEventListener("click", function () { var s = byId("canvas"); zoomAt(0.8, s.clientWidth / 2, s.clientHeight / 2); });
    byId("expand-all").addEventListener("click", function () { openAll(true); });
    byId("all-edges").addEventListener("change", function (ev) { state.allEdges = ev.target.checked; layout(false); });
    byId("collapse-all").addEventListener("click", function () { openAll(false); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && document.activeElement !== byId("search")) {
        if (state.sel) select(null); else if (state.trail.length > 1) go(state.trail.slice(0, -1));
      }
    });
    wireCanvas();
    window.addEventListener("hashchange", function () {
      var moved = readHash();
      if (moved) go(moved.trail, moved.sel);
    });
    var fromHash = readHash();
    if (fromHash) go(fromHash.trail, fromHash.sel);
    else go([{ file: CATALOG.entries[0].file }]);
  }

  start();
})();
