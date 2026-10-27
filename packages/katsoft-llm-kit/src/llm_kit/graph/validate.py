"""Static checks of a loaded graph (``llm-kit graph validate``).

:func:`validate_graph` returns every problem it finds, one :class:`Problem`
per fault, in file order. It checks:

- every expression parses (:mod:`llm_kit.graph.expr`) and every name it reads
  resolves: ``inputs.<input>``, ``carry.<name>`` and ``round.<out>`` inside a
  repeat, ``item`` and ``index`` inside a map (``index`` also in a repeat), and
  ``<node>.<attribute>`` for a sibling node (an inline body also sees the
  nodes around it) with an attribute its kind exposes;
- no cycles among sibling nodes (a reference from inside an inline body counts
  for the node that holds the body);
- every op is in the registry; ``in`` binds exactly the op's inputs (an input
  with a default may be left out); parameters match the op's declarations;
  each prompt template's slots equal the bound slot names; each model
  selection key exists in the project's ``.llm-kit/models.yaml``;
- a subgraph's ``in`` binds exactly its inputs (defaults may be left out);
- ``commit`` nodes only at the top level of an outer graph, and commit adapter
  ops only in ``commit`` nodes; an adapter takes exactly one input, ``bundle``;
- a repeat's literal ``max`` is an integer of at least 1;
- port schemas are compatible where a binding is a bare reference
  (``plan.out.plot``) whose schema is known: the consumer accepts anything
  (``{}``), or the two schemas are equal after ``$ref`` resolution with
  annotations ignored, or one ``anyOf`` branch of the consumer equals the
  producer, or one ``anyOf`` branch of the producer equals the consumer (the
  author narrows, for example with ``when: x != null``; the runner checks the
  value). A literal is checked against the consumer schema. Any other
  expression is not type-checked.

Structural faults (unknown keys, invalid names, schemas outside the
``llm_kit.schema`` subset) are refused earlier by the loader.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from llm_kit.registry.project_models import ProjectModelsError, read_resolved_models
from llm_kit.schema.json_schema import ANNOTATION_KEYWORDS, resolve_local_ref, validate as validate_value

from .loader import resolve_params
from .model import (
    KIND_ATTRIBUTES,
    SCOPE_NAMES,
    Binding,
    Graph,
    GraphError,
    Literal,
    Node,
    Op,
    OpRegistry,
    Port,
    UnknownOpError,
)

__all__ = ["Problem", "validate_graph"]

_BARE_REF = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)((?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*$")
_KEYWORDS = frozenset({"and", "or", "not", "true", "false", "null"})
_MAX_EXPAND = 32


@dataclass(frozen=True)
class Problem:
    """One fault: where it is and what is wrong."""

    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


# ---------------------------------------------------------------------------
# inferred types of bare references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Schema:
    root: Mapping[str, Any]
    node: Mapping[str, Any]


@dataclass(frozen=True)
class _Object:
    props: Mapping[str, Any]   # name -> type info or None


@dataclass(frozen=True)
class _Array:
    items: Any                 # type info or None


TypeInfo = Union[_Schema, _Object, _Array, None]


def _schema(schema: Mapping[str, Any]) -> _Schema:
    return _Schema(schema, schema)


def _deref(info: _Schema) -> Optional[_Schema]:
    node = info.node
    for _ in range(_MAX_EXPAND):
        if "$ref" not in node:
            return _Schema(info.root, node)
        if set(node) - ANNOTATION_KEYWORDS - {"$ref"}:
            return None
        node = resolve_local_ref(info.root, node["$ref"])
    return None


def _navigate(info: TypeInfo, fields: list[str]) -> TypeInfo:
    for name in fields:
        if isinstance(info, _Object):
            info = info.props.get(name)
        elif isinstance(info, _Schema):
            info = _deref(info)
            props = info.node.get("properties") if info is not None else None
            info = _Schema(info.root, props[name]) if isinstance(props, Mapping) and name in props else None
        else:
            return None
    return info


def _expand(root: Mapping, node: Any, depth: int = 0) -> Any:
    if not isinstance(node, Mapping):
        return node
    if depth > _MAX_EXPAND:
        return {"$recursive": True}
    out: dict[str, Any] = {}
    target = None
    for key, value in node.items():
        if key in ANNOTATION_KEYWORDS:
            continue
        if key == "$ref":
            target = _expand(root, resolve_local_ref(root, value), depth + 1)
        elif key in ("properties",) and isinstance(value, Mapping):
            out[key] = {k: _expand(root, v, depth + 1) for k, v in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = _expand(root, value, depth + 1)
        elif key == "anyOf" and isinstance(value, list):
            out[key] = [_expand(root, v, depth + 1) for v in value]
        else:
            out[key] = value
    if target is not None:
        if not out:
            return target
        out["$ref"] = target
    return out


def _compatible(producer: TypeInfo, consumer: Mapping[str, Any]) -> Optional[bool]:
    """True or False when decidable; None when the producer's schema is not known."""
    want = _expand(consumer, consumer)
    if want == {}:
        return True
    if not isinstance(producer, _Schema):
        return None
    have = _expand(producer.root, producer.node)
    if have == want:
        return True
    wanted = want.get("anyOf") if set(want) == {"anyOf"} else None
    if wanted and any(branch == have for branch in wanted):
        return True
    offered = have.get("anyOf") if isinstance(have, Mapping) and set(have) == {"anyOf"} else None
    return bool(offered) and any(branch == want for branch in offered)


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------


@dataclass
class _Level:
    graph: Graph
    names: Mapping[str, Any]                    # scope names introduced at this level
    owner: Optional[str] = None                 # node being checked at this level
    deps: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Scope:
    levels: tuple[_Level, ...]
    inputs: Mapping[str, Port]
    names: Mapping[str, Any]                    # every scope name visible here

    def enter(self, graph: Graph, names: Mapping[str, Any]) -> "_Scope":
        return _Scope(self.levels + (_Level(graph, dict(names)),), self.inputs, {**self.names, **names})

    def overlay(self, names: Mapping[str, Any]) -> "_Scope":
        return _Scope(self.levels, self.inputs, {**self.names, **names})

    def at(self, index: int) -> "_Scope":
        names: dict[str, Any] = {}
        for level in self.levels[: index + 1]:
            names.update(level.names)
        return _Scope(self.levels[: index + 1], self.inputs, names)


# ---------------------------------------------------------------------------
# the checker
# ---------------------------------------------------------------------------


class _Checker:
    def __init__(self, registry: OpRegistry, project_root: Path) -> None:
        self.registry = registry
        self.project_root = Path(project_root)
        self.problems: list[Problem] = []
        self.done_files: set[Path] = set()
        self.params: dict[int, dict] = {}
        self.types: dict[tuple[int, str], TypeInfo] = {}
        self.inferring: set[tuple[int, str]] = set()
        self._selections: Optional[frozenset[str]] = None
        self._selections_error: Optional[str] = None
        from llm_kit.graph import expr  # noqa: PLC0415 -- lazy: the expression module is a separate unit
        self.expr = expr

    def add(self, where: str, message: str) -> None:
        self.problems.append(Problem(where, message))

    # -- files -------------------------------------------------------------

    def file(self, graph: Graph) -> None:
        if graph.path in self.done_files:
            return
        self.done_files.add(graph.path)
        scope = _Scope((), graph.inputs, {})
        self.level(graph, scope, top=True)

    def level(self, graph: Graph, parent: _Scope, *, top: bool = False, names: Mapping[str, Any] = {}) -> _Scope:
        scope = parent.enter(graph, names)
        level = scope.levels[-1]
        for node_id, node in graph.nodes.items():
            level.owner = node_id
            level.deps.setdefault(node_id, set())
            self.node(graph, node, scope, top=top)
        level.owner = None
        if graph.inline:
            for name, binding in graph.out.items():
                self.binding(binding, scope, graph.where("out", name))
        else:
            for name, binding in graph.outputs_from.items():
                self.binding(binding, scope, graph.where("outputs_from", name), graph.outputs[name].schema)
        self.cycles(graph, level.deps)
        return scope

    def cycles(self, graph: Graph, deps: Mapping[str, set[str]]) -> None:
        state: dict[str, int] = {}
        reported: set[frozenset[str]] = set()

        def visit(node: str, trail: list[str]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                cycle = trail[trail.index(node):] + [node]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    self.add(graph.where("nodes"), "cycle: " + " -> ".join(cycle))
                return
            state[node] = 1
            for nxt in sorted(deps.get(node, ())):
                visit(nxt, trail + [node])
            state[node] = 2

        for node in graph.nodes:
            visit(node, [])

    # -- expressions -------------------------------------------------------

    def binding(self, binding: Binding, scope: _Scope, where: str,
                consumer: Optional[Mapping[str, Any]] = None) -> None:
        if isinstance(binding, Literal):
            if consumer is not None:
                errors = validate_value(consumer, binding.value)
                if errors:
                    self.add(where, f"literal {binding.value!r} does not match the port schema: {list(errors)}")
            return
        try:
            parsed = self.expr.parse(binding)
        except self.expr.ExprError as exc:
            self.add(where, f"expression {binding!r}: {exc}")
            return
        ok = True
        for ref in sorted(self.expr.references(parsed)):
            ok = self.reference(ref, scope, where) and ok
        if ok and consumer is not None:
            verdict = _compatible(self.infer(binding, scope), consumer)
            if verdict is False:
                self.add(where, f"the schema of {binding.strip()!r} does not match the port schema")

    def reference(self, ref: str, scope: _Scope, where: str) -> bool:
        parts = ref.split(".")
        root, rest = parts[0], parts[1:]
        if root in SCOPE_NAMES and root != "inputs":
            if root not in scope.names:
                self.add(where, f"{root!r} is not available here")
                return False
            known = scope.names[root]
            if root == "index" and rest:
                self.add(where, f"{ref!r}: index is a number")
                return False
            if root in ("carry", "round") and rest and rest[0] not in known:
                self.add(where, f"{ref!r}: {root} has no {rest[0]!r}; it has {sorted(known)}")
                return False
            return True
        if root == "inputs":
            if rest and rest[0] not in scope.inputs:
                self.add(where, f"{ref!r}: no input {rest[0]!r}; inputs: {sorted(scope.inputs)}")
                return False
            return True
        for index in range(len(scope.levels) - 1, -1, -1):
            level = scope.levels[index]
            node = level.graph.nodes.get(root)
            if node is None:
                continue
            if not rest:
                self.add(where, f"{ref!r}: name an attribute of node {root!r}, such as {root}.out")
                return False
            attributes = self.attributes(node)
            if attributes is not None and rest[0] not in attributes:
                self.add(where, f"{ref!r}: node {root!r} has no attribute {rest[0]!r}; it has {sorted(attributes)}")
                return False
            if level.owner == root:
                self.add(where, f"{ref!r}: node {root!r} reads its own result")
                return False
            if level.owner is not None:
                level.deps.setdefault(level.owner, set()).add(root)
            return True
        self.add(where, f"{ref!r}: unknown name {root!r}")
        return False

    def attributes(self, node: Node) -> Optional[frozenset[str]]:
        if node.kind == "op":
            op = self.op(node.op)
            return op.attributes if op is not None else None
        return KIND_ATTRIBUTES[node.kind]

    def op(self, name: str) -> Optional[Op]:
        return self.registry.get(name) if name in self.registry else None

    # -- inference ---------------------------------------------------------

    def infer(self, text: str, scope: _Scope) -> TypeInfo:
        match = _BARE_REF.match(text)
        if not match or match.group(1) in _KEYWORDS:
            return None
        root = match.group(1)
        rest = [p.strip() for p in match.group(2).split(".") if p.strip()]
        if root == "inputs":
            if not rest or rest[0] not in scope.inputs:
                return None
            return _navigate(_schema(scope.inputs[rest[0]].schema), rest[1:])
        if root == "index":
            return _schema({"type": "integer"})
        if root in ("item", "round"):
            return _navigate(scope.names.get(root), rest)
        if root in SCOPE_NAMES:
            return None
        for index in range(len(scope.levels) - 1, -1, -1):
            node = scope.levels[index].graph.nodes.get(root)
            if node is not None:
                if not rest:
                    return None
                return _navigate(self.node_type(node, rest[0], scope.at(index)), rest[1:])
        return None

    def node_type(self, node: Node, attribute: str, scope: _Scope) -> TypeInfo:
        key = (id(node), attribute)
        if key in self.types:
            return self.types[key]
        if key in self.inferring:
            return None
        self.inferring.add(key)
        try:
            info = self._node_type(node, attribute, scope)
        finally:
            self.inferring.discard(key)
        self.types[key] = info
        return info

    def _node_type(self, node: Node, attribute: str, scope: _Scope) -> TypeInfo:
        graph = scope.levels[-1].graph
        if node.kind == "op":
            op = self.op(node.op)
            if op is None or attribute != "out":
                return None
            if op.outputs is not None:
                return _schema(op.outputs)
            if op.output_schema_param is not None:
                params = self.node_params(graph, node, op)
                param = params.get(op.output_schema_param) if params is not None else None
                return _schema(param.schema) if param is not None else None
            return None
        if node.kind == "graph" and attribute == "out":
            return _Object({k: _schema(p.schema) for k, p in node.subgraph.outputs.items()})
        if node.kind == "map" and attribute == "out":
            return _Array(self.body_type(node, scope))
        if node.kind == "repeat" and attribute == "rounds":
            return _Array(self.body_type(node, scope))
        if node.kind == "human" and attribute == "out":
            return _schema(node.human.schema)
        if node.kind == "commit" and attribute == "out":
            op = self.op(node.commit.adapter)
            return _schema(op.outputs) if op is not None and op.outputs is not None else None
        return None

    def body_names(self, node: Node, scope: _Scope) -> dict[str, Any]:
        if node.map is not None:
            over = self.infer(node.map.over, scope) if isinstance(node.map.over, str) else None
            item: TypeInfo = None
            if isinstance(over, _Array):
                item = over.items
            elif isinstance(over, _Schema):
                resolved = _deref(over)
                items = resolved.node.get("items") if resolved is not None else None
                item = _Schema(resolved.root, items) if isinstance(items, Mapping) else None
            return {"item": item, "index": None}
        return {"carry": {name: None for name in node.repeat.carry}, "index": None}

    def body_type(self, node: Node, scope: _Scope) -> TypeInfo:
        spec = node.map if node.map is not None else node.repeat
        body = spec.body
        if not spec.inline:
            return _Object({k: _schema(p.schema) for k, p in body.outputs.items()})
        inner = scope.enter(body, self.body_names(node, scope))
        return _Object({name: (self.infer(b, inner) if isinstance(b, str) else None) for name, b in body.out.items()})

    def round_names(self, node: Node, scope: _Scope) -> dict[str, TypeInfo]:
        body_type = self.body_type(node, scope)
        assert isinstance(body_type, _Object)
        return dict(body_type.props)

    # -- nodes -------------------------------------------------------------

    def node_params(self, graph: Graph, node: Node, op: Op) -> Optional[dict]:
        key = id(node)
        if key not in self.params:
            try:
                self.params[key] = resolve_params(node, op, graph)
            except GraphError as exc:
                self.params[key] = None
                self.add(graph.where(f"nodes.{node.id}"), exc.message)
        return self.params[key]

    def node(self, graph: Graph, node: Node, scope: _Scope, *, top: bool) -> None:
        where = graph.where(f"nodes.{node.id}")
        if node.when is not None:
            self.binding(node.when, scope, f"{where}.when")
        if node.kind == "op":
            self.op_node(graph, node, scope, where)
        elif node.kind == "graph":
            self.bind_inputs(node.bind, node.subgraph, scope, f"{where}.in")
            self.file(node.subgraph)
        elif node.kind == "map":
            self.map_node(node, scope, where)
        elif node.kind == "repeat":
            self.repeat_node(node, scope, where)
        elif node.kind == "human":
            for name, binding in node.human.payload.items():
                self.binding(binding, scope, f"{where}.human.payload.{name}")
        else:
            self.commit_node(graph, node, scope, where, top=top)

    def bind_inputs(self, bind: Mapping[str, Binding], target: Graph, scope: _Scope, where: str) -> None:
        self.bind_ports(bind, target.inputs, scope, where, f"graph {target.id}")

    def bind_ports(self, bind: Mapping[str, Binding], ports: Mapping[str, Port], scope: _Scope, where: str,
                   what: str) -> None:
        for name in sorted(set(bind) - set(ports)):
            self.add(f"{where}.{name}", f"{what} has no input {name!r}; inputs: {sorted(ports)}")
        missing = sorted(n for n, p in ports.items() if n not in bind and not p.has_default)
        if missing:
            self.add(where, f"{what} needs inputs {missing}")
        for name, binding in bind.items():
            port = ports.get(name)
            self.binding(binding, scope, f"{where}.{name}", port.schema if port is not None else None)

    def op_node(self, graph: Graph, node: Node, scope: _Scope, where: str) -> None:
        try:
            op = self.registry.get(node.op)
        except UnknownOpError:
            self.add(f"{where}.op", f"unknown op {node.op!r}")
            for name, binding in node.bind.items():
                self.binding(binding, scope, f"{where}.in.{name}")
            return
        if op.commit:
            self.add(f"{where}.op", f"{op.name} is a commit adapter; only a commit node may call it")
        self.bind_ports(node.bind, op.inputs, scope, f"{where}.in", f"op {op.name}")
        params = self.node_params(graph, node, op)
        if params is None:
            return
        for name, param in params.items():
            pw = f"{where}.{name}"
            if param.kind in ("expr", "exprs"):
                for key, binding in param.bindings.items():
                    self.binding(binding, scope, f"{pw}.{key}" if key else pw)
            elif param.kind == "template":
                bound = params.get(op.params[name].slots)
                names = set(bound.bindings) if bound is not None else set()
                slots = set(param.slots)
                if slots != names:
                    parts = []
                    if slots - names:
                        parts.append(f"slots with no binding: {sorted(slots - names)}")
                    if names - slots:
                        parts.append(f"bindings with no slot: {sorted(names - slots)}")
                    self.add(pw, f"template {param.value!r}: " + "; ".join(parts))
            elif param.kind == "selection":
                self.selection(param.value, pw)

    def selection(self, key: str, where: str) -> None:
        if self._selections is None and self._selections_error is None:
            try:
                resolved = read_resolved_models(project_root=str(self.project_root))
                self._selections = frozenset(resolved.selections)
            except ProjectModelsError as exc:
                self._selections_error = f"cannot read the project model file: {exc}"
        if self._selections_error is not None:
            self.add(where, f"selection {key!r}: {self._selections_error}")
            return
        if key not in self._selections:
            self.add(where, f"selection {key!r} is not in the project model file")

    def map_node(self, node: Node, scope: _Scope, where: str) -> None:
        spec = node.map
        self.binding(spec.over, scope, f"{where}.map.over")
        names = self.body_names(node, scope)
        self.binding(spec.key, scope.overlay(names), f"{where}.map.key")
        if spec.inline:
            self.level(spec.body, scope, names=names)
        else:
            self.bind_inputs(spec.bind, spec.body, scope.overlay(names), f"{where}.map.in")
            self.file(spec.body)

    def repeat_node(self, node: Node, scope: _Scope, where: str) -> None:
        spec = node.repeat
        if isinstance(spec.max, Literal):
            value = spec.max.value
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                self.add(f"{where}.repeat.max", f"max must be an integer of at least 1, not {value!r}")
        else:
            self.binding(spec.max, scope, f"{where}.repeat.max")
        names = self.body_names(node, scope)
        for name, carry in spec.carry.items():
            self.binding(carry.init, scope, f"{where}.repeat.carry.{name}.init")
        after = scope.overlay({**names, "round": self.round_names(node, scope)})
        for name, carry in spec.carry.items():
            self.binding(carry.next, after, f"{where}.repeat.carry.{name}.next")
        if spec.until is not None:
            self.binding(spec.until, after, f"{where}.repeat.until")
        if spec.inline:
            self.level(spec.body, scope, names=names)
        else:
            self.bind_inputs(spec.bind, spec.body, scope.overlay(names), f"{where}.repeat.in")
            self.file(spec.body)

    def commit_node(self, graph: Graph, node: Node, scope: _Scope, where: str, *, top: bool) -> None:
        spec = node.commit
        if not (top and graph.outer):
            self.add(where, "a commit node may only be at the top level of an outer graph (outer: true)")
        op = self.op(spec.adapter)
        if op is None:
            self.add(f"{where}.commit.adapter", f"unknown op {spec.adapter!r}")
        elif not op.commit:
            self.add(f"{where}.commit.adapter", f"{op.name} is not a commit adapter (commit=True)")
        elif set(op.inputs) != {"bundle"}:
            self.add(f"{where}.commit.adapter",
                     f"commit adapter {op.name} must take exactly one input, bundle; it takes {sorted(op.inputs)}")
        if isinstance(spec.bundle, Mapping):
            for name, binding in spec.bundle.items():
                self.binding(binding, scope, f"{where}.commit.bundle.{name}")
        else:
            self.binding(spec.bundle, scope, f"{where}.commit.bundle", spec.contract)


def validate_graph(graph: Graph, *, registry: OpRegistry, project_root: Path | str) -> list[Problem]:
    """Every static problem of ``graph`` and the subgraph files it runs.

    ``project_root`` locates ``.llm-kit/models.yaml`` for selection keys; the
    file is read only when a node names one.
    """
    checker = _Checker(registry, Path(project_root))
    checker.file(graph)
    return checker.problems
