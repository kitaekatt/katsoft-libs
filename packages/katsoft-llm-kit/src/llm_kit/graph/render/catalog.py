"""The graph catalog: every graph file a set of entry graphs reaches, as JSON.

The catalog is the data a rendered page draws. It holds each graph file once,
keyed by its path, so a subgraph file that several graphs run appears one time
and the page links to it. Its JSON Schema is ``graph-catalog.v1.schema.json``
in :mod:`llm_kit.schema.formats`; its format id is
to this module's format id
(:data:`llm_kit.schema.formats.GRAPH_CATALOG_V1`).

* ``entries`` -- the graphs the catalog was built from, in the order given:
  ``{"name", "file"}``. ``name`` is the project config name, or the graph id
  for a graph file given by path.
* ``files`` -- graph file path (relative to the root) -> graph.
* ``used_by`` -- graph file path -> the places that run it:
  ``{"file", "node"}``, where ``node`` is the node path inside that file
  (``loop/evaluate`` for a node inside the inline body of ``loop``).
* ``unused`` -- optional; present only when it is not empty. The graph files
  under the project root that no entry graph reaches or that failed to load,
  as ``{"file"}`` or ``{"file", "error"}``, sorted by ``file``. A graph file
  that loads is also a key of ``files``, with its ``used_by`` entries (a file
  an unused file runs is itself unused); one that does not load has an
  ``error`` text (the root's path taken out) and is not a key of ``files``.
  Only :func:`project_catalog` with no graph names looks for them.

A graph is ``{id, file, outer, description, inputs, outputs, output_sources,
nodes}``. ``inputs`` is a list of ``{name, description}``. ``output_sources``
maps each output to the sibling nodes its binding reads.

A node is ``{id, kind, subtitle, when, deps, reads_inputs, detail, definition}``
plus, for a node that runs a graph, either ``ref`` (the path of the graph file
it runs; the file is in ``files``) or ``body`` (an inline map or repeat body:
``{location, outputs, output_sources, nodes}``). ``deps`` are the sibling nodes
the node reads; an inline body's nodes may also read the enclosing nodes, and
those reads appear in the body's ``outer_deps``. The edges come from a scan of
the node's expressions, as in :mod:`llm_kit.graph.viewer.structure`: they are
for the picture only, and ``llm-kit graph validate`` is the check.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from llm_kit.graph.config import GraphConfig
from llm_kit.graph.expr import ExprError, parse, references
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import Graph, GraphError, Node
from llm_kit.graph.viewer.structure import (
    _binding_text,
    _dependencies,
    _expression_strings,
    _kind_detail,
    _subtitle,
)

__all__ = [
    "CATALOG_FORMAT",
    "build_catalog",
    "project_catalog",
]

#: The ``format`` value of every catalog this module writes.
CATALOG_FORMAT = "llm-kit.graph-catalog/v1"

_INLINE_BODY = "(inline body)"

#: Directories the walk for unused graph files does not enter, besides any whose name starts with ``.``.
_SKIPPED_DIRECTORIES = frozenset({"node_modules", "__pycache__"})


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _refs(texts: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        try:
            found |= set(references(parse(text)))
        except ExprError:
            continue  # free text (a path, a model key): not an expression
    return found


def _input_reads(raw: Mapping[str, Any]) -> list[str]:
    reads = set()
    for ref in _refs(list(_expression_strings(raw))):
        parts = ref.split(".")
        if len(parts) >= 2 and parts[0] == "inputs":
            reads.add(parts[1])
    return sorted(reads)


def _outer_reads(raw: Mapping[str, Any], outer: frozenset[str]) -> list[str]:
    reads = set()
    for ref in _refs(list(_expression_strings(raw))):
        head = ref.split(".")[0]
        if head in outer:
            reads.add(head)
    return sorted(reads)


def _sources(bindings: Mapping[str, Any], siblings: frozenset[str]) -> dict[str, list[str]]:
    result = {}
    for name, binding in bindings.items():
        text = _binding_text(binding)
        heads = {ref.split(".")[0] for ref in _refs([text])} if text else set()
        result[name] = sorted(heads & siblings)
    return result


def _definition(node: Node) -> dict[str, Any]:
    """The node as written, without the nodes of an inline body (they are drawn)."""
    raw = dict(node.raw)
    for kind in ("map", "repeat"):
        spec = raw.get(kind)
        if isinstance(spec, Mapping) and "body" in spec:
            raw[kind] = {**spec, "body": _INLINE_BODY}
    return raw


class _Builder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, dict[str, Any]] = {}
        self.used_by: dict[str, list[dict[str, str]]] = {}

    def file(self, graph: Graph) -> str:
        key = _relative(graph.path, self.root)
        if key in self.files:
            return key
        self.files[key] = {}  # placeholder: a file that includes itself is refused by the loader
        siblings = frozenset(graph.nodes)
        self.files[key] = {
            "id": graph.id,
            "file": key,
            "outer": graph.outer,
            "description": graph.description,
            "inputs": [{"name": name, "description": port.description or ""}
                       for name, port in graph.inputs.items()],
            "outputs": list(graph.outputs),
            "output_sources": _sources(graph.outputs_from, siblings),
            "nodes": self.nodes(graph, key, "", frozenset()),
        }
        return key

    def nodes(self, graph: Graph, file_key: str, prefix: str, outer: frozenset[str]) -> list[dict[str, Any]]:
        siblings = frozenset(graph.nodes)
        result = []
        for node_id, node in graph.nodes.items():
            path = f"{prefix}/{node_id}" if prefix else node_id
            entry: dict[str, Any] = {
                "id": node_id,
                "kind": node.kind,
                "subtitle": _subtitle(node),
                "when": _binding_text(node.when),
                "deps": _dependencies(node.raw, siblings, node_id),
                "reads_inputs": _input_reads(node.raw),
                "detail": _kind_detail(node),
                "definition": _definition(node),
            }
            for body in node.bodies():
                if body.inline:
                    inner = frozenset(body.nodes)
                    visible = (outer | siblings) - inner
                    entry["body"] = {
                        "location": body.location,
                        "outputs": list(body.out),
                        "output_sources": _sources(body.out, inner),
                        "outer_deps": sorted(
                            {d for n in body.nodes.values() for d in _outer_reads(n.raw, visible)}),
                        "nodes": self.nodes(body, file_key, path, visible),
                    }
                else:
                    ref = self.file(body)
                    entry["ref"] = ref
                    self.used_by.setdefault(ref, []).append({"file": file_key, "node": path})
            result.append(entry)
        return result


def _is_directory_link(path: Path, root: Path) -> bool:
    """Whether ``path`` is a directory link of any kind, or resolves outside ``root``.

    ``os.path.islink`` is false for a Windows junction, so ``os.path.isjunction``
    (Python 3.12 and later) is asked as well.
    """
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return True
    return False


def _graph_files_under(root: Path | str) -> list[Path]:
    """Every ``*.graph.yaml`` file under ``root``, sorted by path.

    The walk does not enter a directory whose name starts with ``.`` (``.git``,
    a virtual environment, tool caches), ``node_modules`` or ``__pycache__``,
    nor a directory that is a link of any kind (a symbolic link, or a Windows
    junction, which ``os.walk`` does not count as a link) or whose resolved
    path is outside ``root``. A file link is kept only when it resolves inside
    ``root``, so the result never leaves the root and lists no file twice by
    way of a directory.
    """
    root_path = Path(root).resolve()
    found = []
    for folder, names, files in os.walk(root_path):
        names[:] = sorted(n for n in names if not n.startswith(".") and n not in _SKIPPED_DIRECTORIES
                          and not _is_directory_link(Path(folder) / n, root_path))
        for name in files:
            if not name.endswith(".graph.yaml"):
                continue
            path = Path(folder) / name
            try:
                path.resolve().relative_to(root_path)
            except ValueError:
                continue
            found.append(path)
    return sorted(found)


def _load_error(exc: GraphError, root: Path) -> str:
    """The text of a load error, with the root's path taken out so a page carries no machine path."""
    text = str(exc)
    for spelling in {str(root) + os.sep, root.as_posix() + "/"}:
        text = text.replace(spelling, "")
    return text


def build_catalog(graphs: Mapping[str, Path | str], *, root: Path | str,
                  project: Optional[str] = None,
                  unused: Sequence[Path | str] = ()) -> dict[str, Any]:
    """The catalog of ``graphs`` (entry name -> graph file) and every graph file they run.

    ``root`` is the directory file paths in the catalog are relative to (files
    outside it keep their absolute path). ``project`` is a display name for the
    page; it defaults to the name of ``root``. ``unused`` lists graph files to
    add when no entry graph reaches them (a file an entry reaches is left as it
    is): each that loads is added to ``files`` and to the catalog's ``unused``
    list, with the graph files it runs; each that does not load is listed with
    its error and does not stop the catalog. Raises
    :class:`llm_kit.graph.model.GraphError` when an entry graph does not load,
    and ``ValueError`` when ``graphs`` is empty.
    """
    if not graphs:
        raise ValueError("build_catalog needs at least one graph")
    root_path = Path(root).resolve()
    builder = _Builder(root_path)
    entries = []
    for name, path in graphs.items():
        entries.append({"name": str(name), "file": builder.file(load_graph(path))})
    reached = set(builder.files)
    failed: dict[str, str] = {}
    for path in unused:
        key = _relative(Path(path), root_path)
        if key in builder.files or key in failed:
            continue
        try:
            builder.file(load_graph(path))
        except GraphError as exc:
            failed[key] = _load_error(exc, root_path)
    items = [{"file": key} for key in builder.files if key not in reached]
    items += [{"file": key, "error": error} for key, error in failed.items()]
    catalog: dict[str, Any] = {
        "format": CATALOG_FORMAT,
        "project": project or root_path.name,
        "entries": entries,
        "files": dict(sorted(builder.files.items())),
        "used_by": {key: builder.used_by[key] for key in sorted(builder.used_by)},
    }
    if items:
        catalog["unused"] = sorted(items, key=lambda item: item["file"])
    return catalog


def project_catalog(config: GraphConfig, names: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """The catalog of a project's graphs (``.llm-kit/graphs.yaml``).

    ``names`` selects graphs by config name; ``None`` takes every graph in the
    config, in config order, and also lists the unused graph files: every
    ``*.graph.yaml`` under the project root that no config graph reaches
    (the catalog's ``unused`` list). Raises
    :class:`llm_kit.graph.model.GraphError` for a name the config does not have
    or a config graph file that does not load; an unused file that does not
    load is listed with its error instead.
    """
    chosen = list(config.graphs) if names is None else list(names)
    graphs = {}
    for name in chosen:
        if name not in config.graphs:
            raise GraphError(f"the project config has no graph named {name!r}")
        graphs[name] = config.graph_path(name)
    return build_catalog(graphs, root=config.root,
                         unused=_graph_files_under(config.root) if names is None else ())
