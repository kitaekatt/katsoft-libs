// The graph browser: draws one graph file of the embedded catalog at a time.
//
// The catalog (llm_kit.graph.render.catalog) is in the page as JSON. The list
// on the left holds the entry graphs and every graph file they run. The
// picture is laid out by ELK (layered, left to right, orthogonal edges). A node
// that runs a graph (graph, map, repeat) opens in place with its + button and
// closes with -; double-click, or "Open graph file" in the details panel, moves
// into the graph file it runs. All text from the catalog is set as text, never
// parsed as markup.
//
// A catalog of format llm-kit.graph-catalog/v2 also carries inferred labels (titles,
// descriptions, edge labels, input and output glosses written by a small model). The
// Labels button shows or hides them; with them off, or with a v1 catalog, the picture
// is the plain one. Label text is inferred, so it is drawn in purple italic.
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
  // Added while labels are on: an edge label sits at the middle of its edge and takes room in the layer.
  var ELK_LABEL_OPTIONS = {
    "elk.edgeLabels.placement": "CENTER",
    "elk.edgeLabels.inline": "false",
    "elk.spacing.edgeLabel": "4",
    "elk.layered.spacing.nodeNodeBetweenLayers": "64",
  };

  var LABELLED_FORMAT = "llm-kit.graph-catalog/v2";
  var LABELS_KEY = "llm-kit-graph-labels";
  var LABELS = CATALOG.format === LABELLED_FORMAT && CATALOG.labels && typeof CATALOG.labels === "object" ? CATALOG.labels : null;
  var ELLIPSIS = String.fromCharCode(0x2026), DOT = " " + String.fromCharCode(0xb7) + " ";
  var FONT_UI = "system-ui, -apple-system, \"Segoe UI\", Roboto, sans-serif";
  var FONT_TITLE = "700 13px " + FONT_UI, FONT_DESC = "italic 11px " + FONT_UI, FONT_EDGE = "italic 10.5px " + FONT_UI;
  var FONT_GLOSS = "italic 10px " + FONT_UI, FONT_H1 = "700 34px " + FONT_UI, FONT_H2 = "italic 15px " + FONT_UI;
  // Labelled cards are wider and as tall as their text: tag, title, up to DESC_LINES of description, raw line.
  var LAB_W = 252, DESC_LINES = 3, DESC_STEP = 13.5, LAB_IO_W = 200, EDGE_LABEL_W = 150, HEADER_W = 880;
  var LAB_KIND = { op: "CODE", llm: "AI MODEL" };

  var elk = new ELK();
  var state = { trail: [], open: new Set(), sel: null, view: { k: 1, x: 0, y: 0 }, query: "", layoutId: 0, allEdges: false, labels: true };
  var meta = new Map();   // drawn element id -> {node, key, kind, ...}
  var drawn = { edges: [], labels: [], size: { w: 0, h: 0 }, files: [] };
  var edgeText = new Map();   // ELK edge label id -> its wrapped lines

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

  // ------------------------------------------------------------------ labels

  function own(obj, key) { return !!obj && typeof obj === "object" && Object.prototype.hasOwnProperty.call(obj, key); }
  function str(v) { return typeof v === "string" && v ? v : null; }
  function labelsOn() { return !!LABELS && state.labels; }
  function extend(a, b) { Object.keys(b).forEach(function (k) { a[k] = b[k]; }); return a; }

  // The labels of one graph file, or null when labels are off or the file has none.
  function fileLabels(file) { return labelsOn() && own(LABELS, file) && typeof LABELS[file] === "object" && LABELS[file] ? LABELS[file] : null; }

  // The labels of one node: lpath is its path inside its graph file (an inline body adds "id/").
  // A label with no title is ignored; the node is then drawn as today.
  function nodeLabel(file, lpath) {
    var fl = fileLabels(file), n = fl && own(fl.nodes, lpath) ? fl.nodes[lpath] : null;
    if (!n || typeof n !== "object" || !str(n.title)) return null;
    return { title: n.title, description: str(n.description) || "", reads: n.reads && typeof n.reads === "object" ? n.reads : {} };
  }

  // A gloss for a graph input or output of a file, or null.
  function portGloss(file, which, name) {
    var fl = fileLabels(file), g = fl && fl.graph && typeof fl.graph === "object" ? fl.graph : null;
    return g && own(g[which], name) ? str(g[which][name]) : null;
  }

  var measurer = null;
  function textWidth(text, font, perChar) {
    if (measurer === null) {
      try { measurer = document.createElement("canvas").getContext("2d") || false; } catch (err) { measurer = false; }
    }
    if (!measurer) return text.length * perChar;
    measurer.font = font;
    return measurer.measureText(text).width * 1.03;
  }

  // Greedy word wrap to at most maxLines lines of width px; the last line ends in an ellipsis when text is cut.
  function wrap(text, width, font, perChar, maxLines) {
    var words = String(text || "").split(/\s+/).filter(Boolean), lines = [], line = "";
    function w(s) { return textWidth(s, font, perChar); }
    while (words.length && lines.length < maxLines) {
      var word = words[0], trial = line ? line + " " + word : word;
      if (w(trial) <= width) { line = trial; words.shift(); continue; }
      if (line) { lines.push(line); line = ""; continue; }
      var n = word.length;
      while (n > 1 && w(word.slice(0, n)) > width) n--;
      lines.push(word.slice(0, n));
      word = word.slice(n);
      if (word) words[0] = word; else words.shift();
    }
    var cut = words.length > 0;
    if (!cut && line) lines.push(line);
    if (cut && lines.length) {
      var last = lines[lines.length - 1].replace(/[\s.,;:]+$/, "");
      while (last.length > 1 && w(last + ELLIPSIS) > width) last = last.slice(0, -1).replace(/\s+$/, "");
      lines[lines.length - 1] = last + ELLIPSIS;
    }
    return lines;
  }

  function oneLine(text, width, font, perChar) { return wrap(text, width, font, perChar, 1)[0] || ""; }

  function kindTag(kind, labelled) { return labelled && LAB_KIND[kind] ? LAB_KIND[kind] : KIND_LABEL[kind]; }

  // The mono line at the foot of a labelled card: the raw id, the op or model, and the prompt file.
  function rawLine(node, kind) {
    var def = node.definition || {}, parts = [node.id];
    if (kind === "llm") {
      parts.push(typeof def.model === "string" ? def.model : "llm.model");
      if (typeof def.prompt === "string") parts.push(base(def.prompt));
    } else {
      parts.push(cardSub(node));
    }
    return parts.join(DOT);
  }

  // The size of a labelled card and the wrapped text it holds.
  function cardBox(info, lab) {
    var innerW = LAB_W - 26, titleW = LAB_W - (info.canOpen ? 48 : 26);
    var title = oneLine(lab.title, titleW, FONT_TITLE, 7.4);
    var desc = lab.description ? wrap(lab.description, innerW, FONT_DESC, 5.6, DESC_LINES) : [];
    var rawY = (desc.length ? 51 + (desc.length - 1) * DESC_STEP : 34) + 18;
    return { w: LAB_W, h: Math.round(rawY + 11), title: title, desc: desc, rawY: rawY };
  }

  // The ELK label of a dependency edge: its text wrapped to two lines.
  function edgeLabel(text, id) {
    var lines = wrap(text, EDGE_LABEL_W, FONT_EDGE, 5.4, 2), widest = 0;
    lines.forEach(function (l) { widest = Math.max(widest, textWidth(l, FONT_EDGE, 5.4)); });
    edgeText.set(id, lines);
    return { id: id, text: text, width: Math.ceil(widest) + 8, height: lines.length * 12 + 4 };
  }

  function usedByText(file) {
    var uses = CATALOG.used_by[file] || [];
    if (!uses.length) return entryName(file) ? "an entry graph" : "used by nothing in this catalog";
    var names = uses.slice(0, 2).map(function (u) { return shortId(graphAt(u.file).id) + " > " + u.node; });
    return "used by " + names.join(", ") + (uses.length > 2 ? " +" + (uses.length - 2) + " more" : "");
  }

  // The header block above a labelled graph: plain-English title, purpose, and a mono meta line.
  function buildHeader(file, graphW) {
    var fl = fileLabels(file), g = fl && fl.graph && typeof fl.graph === "object" ? fl.graph : null;
    if (!g || !str(g.title)) return null;
    var graph = graphAt(file), width = Math.max(520, Math.min(HEADER_W, graphW - 60));
    var title = wrap(g.title, width, FONT_H1, 18, 2);
    var purpose = str(g.purpose) ? wrap(g.purpose, width, FONT_H2, 7.4, 4) : [];
    var y = 52, group = sv("g", { "class": "hdr" });
    title.forEach(function (line, i) { add(group, sv("text", { "class": "h-title", x: 30, y: y + i * 40, text: line })); });
    y += (title.length - 1) * 40;
    if (purpose.length) {
      y += 28;
      purpose.forEach(function (line, i) { add(group, sv("text", { "class": "h-purpose", x: 30, y: y + i * 21, text: line })); });
      y += (purpose.length - 1) * 21;
    }
    y += 28;
    add(group, sv("text", { "class": "h-meta", x: 30, y: y, text: fit([graph.id, graph.file, usedByText(file)].join(DOT), width, 6.9) }));
    return { g: group, h: y + 26, w: width + 60 };
  }

  // The key at the foot of the stage: what purple italic means, and which model wrote the labels on screen.
  function renderKey() {
    var key = byId("legend-key");
    clear(key);
    var on = labelsOn() && drawn.files.some(function (f) { return !!fileLabels(f); });
    key.hidden = !on;
    if (!on) return;
    var made = [];
    drawn.files.forEach(function (f) {
      var fl = fileLabels(f);
      (fl && Array.isArray(fl.sources) ? fl.sources : []).forEach(function (s) {
        (s && Array.isArray(s.calls) ? s.calls : []).forEach(function (c) {
          var who = c && str(c.entry) ? c.entry + (str(c.model) ? " (" + c.model + ")" : "") : null;
          if (who && made.indexOf(who) < 0) made.push(who);
        });
      });
    });
    add(key, el("span", { "class": "inf", text: "Purple italic" }),
      " means a label a small model inferred (" + (made.length ? made.join(", ") : "model not recorded") +
      ") from the graph files and prompts, and so do the plain-English titles. Everything else comes from the graph file.");
  }

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
    var fl = fileLabels(graph.file);
    if (fl) {
      if (fl.graph && typeof fl.graph === "object") hay.push(fl.graph.title, fl.graph.purpose);
      Object.keys(fl.nodes || {}).forEach(function (path) {
        var n = fl.nodes[path];
        if (n && typeof n === "object") hay.push(n.title, n.description);
      });
    }
    return hay.filter(function (h) { return typeof h === "string"; }).join("\n").toLowerCase().indexOf(q) >= 0;
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
    var fl = fileLabels(file), g = fl && fl.graph && typeof fl.graph === "object" ? fl.graph : null;
    var title = g && str(g.title), purpose = g && str(g.purpose);
    return el("div", {
      "class": "item" + (isEntry ? " entry" : "") + (here ? " on" : ""), title: file,
      onclick: function () { go([{ file: file }]); },
    },
    el("div", { "class": "name", text: title || label }),
    purpose ? el("div", { "class": "desc inf", text: purpose }) : (graph.description ? el("div", { "class": "desc", text: graph.description }) : null),
    el("div", { "class": "meta" },
      title ? el("span", { text: label }) : null,
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

  // ctx says where the labels of this level live: the graph file it belongs to, and the prefix of its
  // node paths inside that file (an inline body continues the path of the node that holds it).
  function buildLevel(graph, scope, depth, files, edges, ctx) {
    var children = [];
    if (drawn.files.indexOf(ctx.file) < 0) drawn.files.push(ctx.file);
    graph.nodes.forEach(function (node) {
      var key = scope ? scope + "/" + node.id : node.id;
      var id = nodeId(key);
      var target = node.ref ? graphAt(node.ref) : node.body;
      var loops = node.ref && files.indexOf(node.ref) >= 0;
      var canOpen = !!target && depth < MAX_DEPTH && !loops;
      var lpath = ctx.prefix + node.id;
      var info = { node: node, key: key, kind: kindOf(node), canOpen: canOpen, compound: false, graph: graph,
        file: ctx.file, lpath: lpath, lab: nodeLabel(ctx.file, lpath) };
      meta.set(id, info);
      if (canOpen && state.open.has(key)) {
        info.compound = true;
        info.target = target;
        children.push({
          id: id,
          layoutOptions: { "elk.padding": "[top=" + HEAD + ",left=14,bottom=14,right=14]" },
          children: buildLevel(target, key, depth + 1, node.ref ? files.concat([node.ref]) : files, edges,
            node.ref ? { file: node.ref, prefix: "" } : { file: ctx.file, prefix: lpath + "/" }),
        });
      } else if (info.lab) {
        info.box = cardBox(info, info.lab);
        children.push({ id: id, width: info.box.w, height: info.box.h });
      } else {
        children.push({ id: id, width: CARD_W, height: CARD_H });
      }
    });
    var shown = state.allEdges ? null : mainDeps(graph.nodes);
    graph.nodes.forEach(function (node) {
      var key = scope ? scope + "/" + node.id : node.id;
      var lab = meta.get(nodeId(key)).lab;
      (shown ? shown[node.id] : node.deps).forEach(function (dep) {
        var from = nodeId(scope ? scope + "/" + dep : dep);
        var edge = { id: "e" + edges.length, sources: [from], targets: [nodeId(key)], kind: "dep" };
        var text = lab && own(lab.reads, dep) ? str(lab.reads[dep]) : null;
        if (text) edge.labels = [edgeLabel(text, edge.id + "l")];
        edges.push(edge);
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
    drawn.files = [];
    edgeText.clear();
    var edges = [];
    var children = buildLevel(graph, "", 0, [file], edges, { file: file, prefix: "" });
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
      var box = ioBox(portGloss(file, "inputs", port.name));
      meta.set(id, { io: "input", name: port.name, port: port, readers: readers, box: box });
      inputs.push({ id: id, width: box.w, height: box.h });
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
      var box = ioBox(portGloss(file, "outputs", name));
      meta.set(id, { io: "output", name: name, box: box });
      outputs.push({ id: id, width: box.w, height: box.h });
      (graph.output_sources[name] || []).forEach(function (src) {
        edges.push({ id: "e" + edges.length, sources: [nodeId(src)], targets: [id], kind: "output" });
      });
    });
    inputs.forEach(function (n) { n.layoutOptions = { "elk.layered.layering.layerConstraint": "FIRST" }; });
    outputs.forEach(function (n) { n.layoutOptions = { "elk.layered.layering.layerConstraint": "LAST" }; });
    var options = labelsOn() ? extend(extend({}, ELK_OPTIONS), ELK_LABEL_OPTIONS) : ELK_OPTIONS;
    return { id: "root", layoutOptions: options, children: inputs.concat(children, outputs), edges: edges };
  }

  // The size of an input or output pill; a gloss adds up to two lines of purple italic under the name.
  function ioBox(gloss) {
    if (!gloss) return { w: IO_W, h: IO_H, gloss: [] };
    var lines = wrap(gloss, LAB_IO_W - 26, FONT_GLOSS, 5.2, 2);
    return { w: LAB_IO_W, h: IO_H + 12 * lines.length, gloss: lines, text: gloss };
  }

  function layout(refit) {
    var step = current(), graph = graphAt(step.file);
    var token = ++state.layoutId;
    byId("status").textContent = "Laying out\u2026";
    elk.layout(buildGraph(graph, step.file)).then(function (laid) {
      if (token !== state.layoutId) return;
      byId("status").textContent = "";
      draw(laid);
      if (refit) fitView(true);
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
    drawn.labels = [];
    var labelsG = sv("g");
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
      (e.labels || []).forEach(function (l) {
        var lines = edgeText.get(l.id);
        if (!lines || l.x === undefined) return;
        var g = sv("g", { "class": "elabel", transform: "translate(" + (l.x + origin.x) + "," + (l.y + origin.y) + ")" },
          sv("title", { text: l.text }));
        lines.forEach(function (line, i) {
          add(g, sv("text", { x: l.width / 2, y: 10 + i * 12, "text-anchor": "middle", text: line }));
        });
        g.dataset.from = e.sources[0];
        g.dataset.to = e.targets[0];
        drawn.labels.push(g);
        add(labelsG, g);
      });
    });
    var header = labelsOn() ? buildHeader(current().file, laid.width || 0) : null;
    var scene = sv("g", { transform: "translate(0," + (header ? header.h : 0) + ")" }, frames, edgesG, labelsG, cards);
    add(vp, defs(), header ? header.g : null, scene);
    drawn.size = { w: Math.max(laid.width || 0, header ? header.w : 0), h: (laid.height || 0) + (header ? header.h : 0) };
    renderKey();
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

  // A card with labels: kind tag, bold title, purple italic description, and the raw id, op or model and prompt in mono.
  function drawLabelledCard(id, info, x, y) {
    var node = info.node, kind = info.kind, lab = info.lab, box = info.box;
    var raw = rawLine(node, kind);
    var g = sv("g", {
      "class": "node lab" + (node.when ? " when" : ""), "data-id": id, transform: "translate(" + x + "," + y + ")",
      style: "--c:" + kindColor(kind),
    },
    sv("title", { text: lab.title + (lab.description ? "\n" + lab.description : "") + "\n" + raw + (node.when ? "\n(only when " + node.when + ")" : "") }),
    sv("rect", { "class": "box", width: box.w, height: box.h, rx: 8 }),
    kind === "llm" ? sv("path", { "class": "topbar", d: "M1.5,9 Q1.5,1.5 9,1.5 L" + (box.w - 9) + ",1.5 Q" + (box.w - 1.5) + ",1.5 " + (box.w - 1.5) + ",9" }) : null,
    sv("rect", { "class": "stripe", width: 4, height: box.h - 20, x: 0, y: 10, rx: 2 }),
    sv("text", { "class": "tag", x: 14, y: 17, text: kindTag(kind, true) }),
    node.when ? sv("text", { "class": "tag cond", x: 14 + kindTag(kind, true).length * 6.6 + 14, y: 17, text: "ONLY IF NEEDED" }) : null,
    sv("text", { "class": "ltitle", x: 14, y: 35, text: box.title }),
    box.desc.map(function (line, i) { return sv("text", { "class": "ldesc", x: 14, y: 51 + i * DESC_STEP, text: line }); }),
    sv("text", { "class": "raw", x: 14, y: box.rawY, text: fit(raw, box.w - 26, 5.7) }));
    if (info.canOpen) add(g, toggleButton(info, box.w - 28, 8, false));
    g.addEventListener("click", function () { select(info.key); });
    g.addEventListener("dblclick", function () { if (node.ref) enter(info.key, node); });
    return g;
  }

  function drawCard(id, info, x, y) {
    if (info.box) return drawLabelledCard(id, info, x, y);
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
    var tag = kindTag(kind, !!info.lab);
    if (info.lab) sub = node.id + "  \u00b7  " + sub;
    var g = sv("g", {
      "class": "compound node", "data-id": id, transform: "translate(" + x + "," + y + ")", style: "--c:" + kindColor(kind),
    },
    info.lab ? sv("title", { text: info.lab.title + (info.lab.description ? "\n" + info.lab.description : "") }) : null,
    sv("rect", { "class": "frame", width: w, height: h, rx: 10 }),
    sv("text", { "class": "tag", x: 14, y: 17, text: tag }),
    sv("text", { "class": "ctitle", x: 14 + tag.length * 7 + 6, y: 17, text: info.lab ? fit(info.lab.title, w - 50 - tag.length * 7, 7.4) : node.id }),
    sv("text", { "class": "csub", x: 14, y: 34, text: fit(sub + (kind === "repeat" ? "  \u00b7  up to " + node.detail.max + " rounds" : ""), w - 50, 6.1) }));
    add(g, toggleButton(info, w - 28, 8, true));
    g.addEventListener("click", function (ev) { if (ev.target.closest(".node") === g) select(info.key); });
    g.addEventListener("dblclick", function (ev) { if (ev.target.closest(".node") === g && node.ref) enter(info.key, node); });
    return g;
  }

  function drawIo(id, info, x, y) {
    var box = info.box, glossed = box.gloss.length > 0;
    var g = sv("g", { "class": "io" + (glossed ? " glossed" : ""), "data-id": id, transform: "translate(" + x + "," + y + ")" },
      sv("title", { text: (info.io === "input" ? "Graph input " : "Graph output ") + info.name + (glossed ? "\n" + box.text : "") }),
      sv("rect", { width: box.w, height: box.h, rx: glossed ? 14 : IO_H / 2 }),
      sv("text", { "class": "iotag", x: 12, y: 19, text: info.io === "input" ? "IN" : "OUT" }),
      sv("text", { x: info.io === "input" ? 32 : 40, y: 19.5, text: fit(info.name, box.w - 46, 7) }),
      box.gloss.map(function (line, i) { return sv("text", { "class": "gloss", x: 12, y: 34 + i * 12, text: line }); }));
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
    drawn.labels.forEach(function (g) {
      g.classList.toggle("hot", !!target && (g.dataset.from === target || g.dataset.to === target));
    });
    svg.classList.toggle("focus", !!target);
    svg.querySelectorAll("[data-id]").forEach(function (g) {
      g.classList.toggle("sel", g.dataset.id === target);
      g.classList.toggle("near", near.has(g.dataset.id));
    });
    if (!quiet) writeHash();
    renderInspector(target);
  }

  // notes (optional) maps a key to a purple italic label shown beside its chip.
  function chipList(keys, scope, notes) {
    if (!keys.length) return el("div", { "class": "file", text: "none" });
    var noted = !!notes && keys.some(function (k) { return own(notes, k) && !!str(notes[k]); });
    return el("div", { "class": "chips" + (noted ? " col" : "") }, keys.map(function (k) {
      var key = scope ? scope + "/" + k : k;
      var chip = el("span", { "class": "chip", text: k, onclick: function () { select(key); } });
      if (!noted) return chip;
      return el("div", { "class": "noted" }, chip, own(notes, k) && str(notes[k]) ? el("span", { "class": "inf", text: str(notes[k]) }) : null);
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
      if (info.box && info.box.text) add(box, el("p", { "class": "inf", text: info.box.text }));
      if (info.port && info.port.description) add(box, el("p", { text: info.port.description }));
      var readers = [];
      if (info.io === "input") meta.forEach(function (m) { if (m.node && m.node.reads_inputs.indexOf(info.name) >= 0) readers.push(m.key); });
      add(box, el("h3", { text: info.io === "input" ? "Read by" : "Comes from" }),
        chipList(info.io === "input" ? readers : (graph.output_sources[info.name] || []), ""));
      return;
    }
    var node = info.node, kind = info.kind, scope = info.key.split("/").slice(0, -1).join("/");
    var readBy = info.graph.nodes.filter(function (n) { return n.deps.indexOf(node.id) >= 0; }).map(function (n) { return n.id; });
    var lab = info.lab;
    add(box,
      el("span", { "class": "pill", style: "--c:" + kindColor(kind), text: kindTag(kind, !!lab) }),
      node.when ? el("span", { "class": "pill", style: "--c:var(--muted)", text: lab ? "ONLY IF NEEDED" : "WHEN" }) : null,
      el("h2", { text: lab ? lab.title : node.id }),
      el("div", { "class": "file", text: lab ? info.key + "  " + DOT.trim() + "  " + KIND_HELP[kind] : (info.key.indexOf("/") >= 0 ? info.key : KIND_HELP[kind]) }),
      lab && lab.description ? el("p", { "class": "inf", text: lab.description }) : null,
      el("h3", { text: "What it is" }),
      kv([["op", node.detail && node.detail.op], ["model", (node.definition || {}).model], ["effort", (node.definition || {}).effort],
        ["human", kind === "human" ? node.detail.kind : null], ["adapter", node.detail && node.detail.adapter],
        ["over", node.detail && node.detail.over], ["key", node.detail && node.detail.key],
        ["max", node.detail && node.detail.max], ["until", node.detail && node.detail.until], ["when", node.when]]),
      el("h3", { text: "Reads" }), chipList(node.deps, scope, lab ? lab.reads : null),
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

  function outputGlosses(graph) {
    return graph.outputs.some(function (name) { return !!portGloss(graph.file, "outputs", name); });
  }

  function renderOverview(box, graph) {
    var counts = {};
    (function walk(nodes) { nodes.forEach(function (n) { var k = kindOf(n); counts[k] = (counts[k] || 0) + 1; if (n.body) walk(n.body.nodes); }); })(graph.nodes);
    var runs = [];
    (function walk(nodes) { nodes.forEach(function (n) { if (n.ref && runs.indexOf(n.ref) < 0) runs.push(n.ref); if (n.body) walk(n.body.nodes); }); })(graph.nodes);
    var fl = fileLabels(graph.file), gl = fl && fl.graph && typeof fl.graph === "object" && str(fl.graph.title) ? fl.graph : null;
    add(box,
      graph.outer ? el("span", { "class": "pill", style: "--c:var(--commit)", text: "OUTER GRAPH" }) : null,
      entryName(graph.file) ? el("span", { "class": "pill", style: "--c:var(--accent)", text: "ENTRY" }) : null,
      el("h2", { text: gl ? gl.title : graph.id }),
      el("div", { "class": "file", text: gl ? graph.id + "  " + DOT.trim() + "  " + graph.file : graph.file }),
      gl && str(gl.purpose) ? el("p", { "class": "inf", text: gl.purpose }) : null,
      graph.description ? el("p", { text: graph.description }) : null,
      el("h3", { text: "Nodes" }),
      el("div", { "class": "chips" }, Object.keys(counts).map(function (k) {
        return el("span", { "class": "pill", style: "--c:" + kindColor(k), text: counts[k] + " " + KIND_LABEL[k].toLowerCase() });
      })),
      el("h3", { text: "Inputs" }),
      graph.inputs.length ? el("ul", null, graph.inputs.map(function (p) {
        var gloss = portGloss(graph.file, "inputs", p.name);
        return el("li", null, el("a", { text: p.name, onclick: function () { select("i:" + p.name); } }),
          gloss ? el("div", { "class": "inf", text: gloss }) : null,
          p.description ? el("div", { "class": "file", text: p.description }) : null);
      })) : el("div", { "class": "file", text: "none" }),
      el("h3", { text: "Outputs" }),
      graph.outputs.length ? el("div", { "class": "chips" + (outputGlosses(graph) ? " col" : "") }, graph.outputs.map(function (name) {
        var chip = el("span", { "class": "chip", text: name, onclick: function () { select("o:" + name); } });
        var gloss = portGloss(graph.file, "outputs", name);
        return outputGlosses(graph) ? el("div", { "class": "noted" }, chip, gloss ? el("span", { "class": "inf", text: gloss }) : null) : chip;
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

  // First show stops fitting at READABLE_K: below about 0.6 the card text is too small to read,
  // so a wide graph keeps this scale, starts at its left (entry) edge, and is panned.
  // The Fit button is a deliberate request and fits the whole graph.
  var READABLE_K = 0.6;

  function fitView(readable) {
    var svg = byId("canvas"), w = svg.clientWidth, h = svg.clientHeight;
    var gw = drawn.size.w || 1, gh = drawn.size.h || 1;
    var fitted = Math.min(1.25, Math.max(0.1, Math.min((w - 40) / gw, (h - 90) / gh)));
    if (readable === true && fitted < READABLE_K) {
      state.view = { k: READABLE_K, x: 20, y: Math.max(20, (h - gh * READABLE_K) / 2) };
    } else {
      state.view = { k: fitted, x: (w - gw * fitted) / 2, y: Math.max(20, (h - gh * fitted) / 2) };
    }
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

  // The Labels button: only a catalog with labels has it. The choice is kept in localStorage like the theme.
  function wireLabels() {
    var button = byId("labels");
    if (!LABELS) { button.hidden = true; state.labels = false; return; }
    try { state.labels = localStorage.getItem(LABELS_KEY) !== "off"; } catch (err) { state.labels = true; }
    function show() {
      button.setAttribute("aria-pressed", state.labels ? "true" : "false");
      button.title = state.labels ? "Hide the inferred labels" : "Show the inferred labels";
    }
    show();
    button.addEventListener("click", function () {
      state.labels = !state.labels;
      try { localStorage.setItem(LABELS_KEY, state.labels ? "on" : "off"); } catch (err) { /* the choice just is not kept */ }
      show();
      renderList();
      layout(true);
    });
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
    wireLabels();
    var shell = byId("shell");
    if (CATALOG.entries.length === 1 && Object.keys(CATALOG.files).length === 1) shell.classList.add("no-side");
    byId("toggle-side").addEventListener("click", function () { shell.classList.toggle("no-side"); });
    byId("search").addEventListener("input", function (ev) { state.query = ev.target.value; renderList(); });
    byId("fit").addEventListener("click", function () { fitView(false); });
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
