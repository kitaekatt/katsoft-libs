"""Graph structures for the viewer.

Two sources give the same JSON-able tree:

* :func:`graph_structure` turns a loaded :class:`llm_kit.graph.model.Graph` (the
  graph file as it is now) into nested dictionaries. No op registry is needed.
* :func:`embedded_structure` turns the ``graphs`` table of a run record (the
  graph as it was when the run ran) into the same tree.

Each tree node has ``id``, ``kind``, ``subtitle``, ``deps`` (the sibling nodes
it reads: the edges of the picture), ``detail``, ``definition`` and ``body`` (the
tree of the graph it runs, or ``None``).

For a graph file, ``deps`` come from a scan of every string in the node: a
string that parses as an expression and reads ``<sibling>.<attribute>`` adds an
edge. Strings that are not expressions (a prompt path, a model key) fail to
parse and add nothing; the keys that hold such text are skipped. The edges are
for the picture only; ``llm-kit graph validate`` is the check.

Run record node paths (``docs/reference/llm-kit-graph-runtime.md``) join
segments with ``/``. A segment is a node id, with ``[key]`` after the id of a
``map`` (item key) or ``repeat`` (round number) node when it names one item or
round: ``plot/loop[2]/write``. :func:`unmapped_paths` lists the record paths
that match no node of a tree.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from llm_kit.graph.expr import ExprError, parse, references
from llm_kit.graph.model import Graph, GraphError, Literal, Node

__all__ = [
    "PATH_SEPARATOR",
    "embedded_structure",
    "graph_structure",
    "map_path",
    "split_segment",
    "unmapped_paths",
]

#: Separates the segments of a run record node path.
PATH_SEPARATOR = "/"

_SEGMENT = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)(?:\[(.*)\])?$", re.DOTALL)

#: Node keys whose strings are names, paths or schemas, never expressions.
_TEXT_KEYS = frozenset({
    "op", "graph", "model", "prompt", "template", "file", "adapter", "kind",
    "contract", "schema", "output_schema", "mode", "effort", "description",
})


def _binding_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Literal):
        return str(value.value).lower() if isinstance(value.value, bool) else repr(value.value)
    return str(value)


def _expression_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, inner in value.items():
            if key not in _TEXT_KEYS:
                yield from _expression_strings(inner)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for inner in value:
            yield from _expression_strings(inner)


def _dependencies(raw: Mapping[str, Any], siblings: frozenset[str], own_id: str) -> list[str]:
    found: set[str] = set()
    for text in _expression_strings(raw):
        try:
            refs = references(parse(text))
        except ExprError:
            continue  # not an expression (free text); the scan is a heuristic by design
        for ref in refs:
            parts = ref.split(".")
            if len(parts) >= 2 and parts[0] in siblings and parts[0] != own_id:
                found.add(parts[0])
    return sorted(found)


def _subtitle(node: Node) -> str:
    if node.kind == "op":
        bits = [str(node.op)]
        for key in ("model", "effort"):
            value = node.raw.get(key)
            if isinstance(value, str) and value:
                bits.append(value)
        return " - ".join(bits)
    if node.kind == "human":
        return f"human - {node.human.kind}"
    if node.kind == "commit":
        return f"commit - {node.commit.adapter}"
    return node.kind


def _kind_detail(node: Node) -> dict[str, Any]:
    if node.kind == "map":
        return {"over": _binding_text(node.map.over), "key": _binding_text(node.map.key)}
    if node.kind == "repeat":
        spec = node.repeat
        return {
            "max": _binding_text(spec.max),
            "until": _binding_text(spec.until),
            "carry": {name: {"init": _binding_text(c.init), "next": _binding_text(c.next)}
                      for name, c in spec.carry.items()},
        }
    if node.kind == "human":
        return {"kind": node.human.kind}
    if node.kind == "commit":
        return {"adapter": node.commit.adapter}
    if node.kind == "op":
        return {"op": node.op}
    return {}


def _relative(path: Path, root: Optional[Path]) -> str:
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    return str(path)


def graph_structure(graph: Graph, *, root: Optional[Path] = None) -> dict[str, Any]:
    """``graph`` and every body it runs as a JSON-able dictionary.

    ``root`` (the project root) shortens file paths in the output.
    """
    siblings = frozenset(graph.nodes)
    nodes = []
    for node_id, node in graph.nodes.items():
        body = None
        for inner in node.bodies():
            body = graph_structure(inner, root=root)
        nodes.append({
            "id": node_id,
            "kind": node.kind,
            "subtitle": _subtitle(node),
            "when": _binding_text(node.when),
            "deps": _dependencies(node.raw, siblings, node_id),
            "detail": _kind_detail(node),
            "definition": dict(node.raw),
            "body": body,
        })
    return {
        "id": graph.id,
        "file": _relative(graph.path, root),
        "inline": graph.inline,
        "outer": graph.outer,
        "description": graph.description,
        "inputs": sorted(graph.inputs),
        "outputs": sorted(graph.outputs),
        "nodes": nodes,
    }


def embedded_structure(graphs: Mapping[str, Any], graph_hash: str) -> dict[str, Any]:
    """The tree of the graph with hash ``graph_hash`` from a run record's ``graphs`` table."""
    def build(key: str, stack: tuple[str, ...]) -> dict[str, Any]:
        if key in stack:
            raise GraphError(f"the run record's graph table has a cycle at {key[:12]}")
        entry = graphs.get(key)
        if not isinstance(entry, Mapping) or not isinstance(entry.get("nodes"), Mapping):
            raise GraphError(f"the run record has no structure for graph {str(key)[:12]}")
        nodes = []
        for node_id, node in entry["nodes"].items():
            body = node.get("body")
            nodes.append({
                "id": node_id,
                "kind": node.get("kind"),
                "subtitle": str(node.get("op") or node.get("kind")),
                "when": None,
                "deps": list(node.get("deps") or ()),
                "detail": {"op": node.get("op")} if node.get("op") else {},
                "definition": {k: v for k, v in node.items() if k != "body"},
                "body": build(body, (*stack, key)) if body else None,
            })
        return {
            "id": entry.get("id"), "file": entry.get("file"), "inline": bool(entry.get("location")),
            "outer": False, "description": "", "inputs": list(entry.get("inputs") or ()),
            "outputs": list(entry.get("outputs") or ()), "nodes": nodes,
        }
    return build(graph_hash, ())


# ---------------------------------------------------------------------------
# run record paths
# ---------------------------------------------------------------------------


def split_segment(segment: str) -> Optional[tuple[str, Optional[str]]]:
    """``(node id, key)`` of a path segment (``loop[2]`` -> ``("loop", "2")``), or None."""
    match = _SEGMENT.match(segment)
    return (match.group(1), match.group(2)) if match else None


def _node(structure: Mapping[str, Any], node_id: str) -> Optional[Mapping[str, Any]]:
    for node in structure["nodes"]:
        if node["id"] == node_id:
            return node
    return None


def map_path(structure: Mapping[str, Any], segments: Sequence[str]) -> bool:
    """True when ``segments`` name a node (or a map item or repeat round) of ``structure``."""
    if not segments:
        return False
    parsed = split_segment(segments[0])
    if parsed is None:
        return False
    node_id, key = parsed
    node = _node(structure, node_id)
    if node is None:
        return False
    keyed = node["kind"] in ("map", "repeat")
    if key is not None and not keyed:
        return False  # [key] on a node that is neither a map nor a repeat
    rest = list(segments[1:])
    if not rest:
        return True
    if node["body"] is None or (keyed and key is None):
        return False
    return map_path(node["body"], rest)


def unmapped_paths(structure: Mapping[str, Any], paths: Sequence[str]) -> list[str]:
    """The record paths that match no node of ``structure``, sorted."""
    return sorted(p for p in paths if not map_path(structure, p.split(PATH_SEPARATOR)))
