"""Freshness of node records (Q17): what a node reads, its input hash, reuse.

A node's input hash covers the values of every reference its own expressions
make to names outside the node, including the expressions inside its inline
bodies, minus the names the node introduces itself (a map's ``item`` and
``index``; a repeat's ``carry``, ``round`` and ``index``; the node ids of its
inline bodies). A reference (``plan.out.plot``) is evaluated as an expression;
a missing one is recorded as missing. Together with the definition hash
(:mod:`llm_kit.graph.hashing`) it decides whether an earlier record can be
reused. The rule is in ``docs/reference/llm-kit-graph-runtime.md``,
"Freshness".
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from llm_kit.pipeline.artifacts.freshness.hashing import FULL_DIGEST_LENGTH, digest, stable_json

from .expr import ExprError, ExprMissingError, evaluate, parse, references
from .model import Binding, Graph, Literal, Node, OpRegistry
from .records import RunStore

__all__ = [
    "INPUT_HASH_VERSION",
    "ReadsAnalyzer",
    "ReuseIndex",
    "binding_refs",
    "forced",
    "input_hash",
    "parse_binding",
    "reads_document",
]

INPUT_HASH_VERSION = b"llm-kit.graph.inputs/v1"

_MAP_NAMES = frozenset({"item", "index"})
_REPEAT_NAMES = frozenset({"carry", "index"})
_REPEAT_AFTER_NAMES = frozenset({"carry", "index", "round"})

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def parse_binding(text: str) -> Any:
    """``expr.parse`` with a process-wide cache (expressions are immutable)."""
    with _cache_lock:
        parsed = _cache.get(text)
    if parsed is None:
        parsed = parse(text)
        with _cache_lock:
            _cache[text] = parsed
    return parsed


def binding_refs(binding: Optional[Binding]) -> frozenset[str]:
    """The reference paths a binding reads (none for a literal)."""
    if binding is None or isinstance(binding, Literal):
        return frozenset()
    return references(parse_binding(binding))


def _root(ref: str) -> str:
    return ref.split(".", 1)[0]


def _without(refs: Iterable[str], names: Iterable[str]) -> set[str]:
    names = frozenset(names)
    return {ref for ref in refs if _root(ref) not in names}


def _all_refs(bindings: Iterable[Binding]) -> set[str]:
    out: set[str] = set()
    for binding in bindings:
        out |= binding_refs(binding)
    return out


class ReadsAnalyzer:
    """The external references of nodes and inline bodies, memoised per node."""

    def __init__(self, registry: OpRegistry) -> None:
        self.registry = registry
        self._memo: dict[int, frozenset[str]] = {}
        self._lock = threading.Lock()

    def node(self, node: Node) -> frozenset[str]:
        """Every reference ``node`` reads from outside itself."""
        key = id(node)
        with self._lock:
            hit = self._memo.get(key)
        if hit is not None:
            return hit
        refs = frozenset(self._node(node))
        with self._lock:
            self._memo[key] = refs
        return refs

    def deps(self, node: Node, siblings: Iterable[str]) -> frozenset[str]:
        """The sibling node ids ``node`` reads."""
        names = frozenset(siblings)
        return frozenset(_root(ref) for ref in self.node(node) if _root(ref) in names and _root(ref) != node.id)

    def body(self, body: Graph) -> set[str]:
        """References an inline body makes to names outside it."""
        refs: set[str] = set()
        for node in body.nodes.values():
            refs |= self.node(node)
        refs |= _all_refs(body.out.values())
        return _without(refs, body.nodes)

    def _node(self, node: Node) -> set[str]:
        refs = set(binding_refs(node.when))
        if node.kind == "op":
            refs |= _all_refs(node.bind.values())
            op = self.registry.get(node.op)
            for name, spec in op.params.items():
                if name not in node.params:
                    continue
                value = node.params[name]
                if spec.kind == "expr" and isinstance(value, str):
                    refs |= binding_refs(value)
                elif spec.kind == "exprs" and isinstance(value, Mapping):
                    refs |= _all_refs(v for v in value.values() if isinstance(v, str))
        elif node.kind == "graph":
            refs |= _all_refs(node.bind.values())
        elif node.kind == "map":
            spec = node.map
            refs |= binding_refs(spec.over)
            inner = set(binding_refs(spec.key))
            inner |= self.body(spec.body) if spec.inline else _all_refs(spec.bind.values())
            refs |= _without(inner, _MAP_NAMES)
        elif node.kind == "repeat":
            spec = node.repeat
            refs |= binding_refs(spec.max)
            refs |= _all_refs(c.init for c in spec.carry.values())
            after = _all_refs(c.next for c in spec.carry.values()) | set(binding_refs(spec.until))
            refs |= _without(after, _REPEAT_AFTER_NAMES)
            inner = self.body(spec.body) if spec.inline else _all_refs(spec.bind.values())
            refs |= _without(inner, _REPEAT_NAMES)
        elif node.kind == "human":
            refs |= _all_refs(node.human.payload.values())
        elif node.kind == "commit":
            bundle = node.commit.bundle
            refs |= _all_refs(bundle.values()) if isinstance(bundle, Mapping) else set(binding_refs(bundle))
        return refs


def reads_document(refs: Iterable[str], scope: Mapping[str, Any]) -> dict[str, dict]:
    """``{reference: {"value": v}}`` or ``{reference: {"missing": true}}`` for each reference.

    A reference whose evaluation fails for another reason (a field of a
    number) is ``{"error": message}``; the node itself fails on it later.
    """
    out: dict[str, dict] = {}
    for ref in sorted(refs):
        try:
            out[ref] = {"value": evaluate(parse_binding(ref), scope)}
        except ExprMissingError:
            out[ref] = {"missing": True}
        except ExprError as exc:
            out[ref] = {"error": str(exc)}
    return out


def input_hash(reads: Mapping[str, Any]) -> str:
    """The 64-hex input hash of a reads document."""
    return digest(INPUT_HASH_VERSION, stable_json(reads), length=FULL_DIGEST_LENGTH)


def forced(path: str, force: Iterable[str]) -> bool:
    """True when ``path`` is a forced path, or lies above or below one."""
    for item in force:
        if path == item:
            return True
        for a, b in ((path, item), (item, path)):
            if b.startswith(a + "/") or b.startswith(a + "["):
                return True
    return False


class ReuseIndex:
    """The records of an item by node path, for the reuse rule."""

    def __init__(self, store: RunStore) -> None:
        self.by_path = store.records_by_path()

    def find(self, path: str, definition_hash: str, input_hash_: str) -> Optional[tuple[str, dict]]:
        """The newest reusable record at ``path`` for these hashes, or None."""
        for run_id, record in self.by_path.get(path, ()):
            if record.get("input_hash") != input_hash_:
                continue
            status = record["status"]
            if status in ("done", "reused") and record.get("definition_hash") == definition_hash:
                return run_id, record
            if status == "seeded" and record["seed"]["valid"]:
                return run_id, record
        return None

    def latest(self, path: str, *, statuses: Optional[Iterable[str]] = None) -> Optional[tuple[str, dict]]:
        """The newest record at ``path`` (optionally only with one of ``statuses``)."""
        wanted = None if statuses is None else frozenset(statuses)
        for run_id, record in self.by_path.get(path, ()):
            if wanted is None or record["status"] in wanted:
                return run_id, record
        return None

    def reason(self, path: str, definition_hash: str, input_hash_: str) -> str:
        """Why no record at ``path`` is reusable (for a plan)."""
        candidates = [r for _, r in self.by_path.get(path, ()) if r["status"] in ("done", "reused", "seeded")]
        if not candidates:
            return "no record"
        record = candidates[0]
        if record["status"] == "seeded" and not record["seed"]["valid"]:
            return "seed invalid"
        if record["status"] != "seeded" and record.get("definition_hash") != definition_hash:
            return "definition changed"
        return "inputs changed"
