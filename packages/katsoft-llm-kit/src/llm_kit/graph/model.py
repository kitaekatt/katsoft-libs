"""The data model of a loaded graph and the operation interfaces.

A graph file (``*.graph.yaml``) is loaded by :mod:`llm_kit.graph.loader` into
the frozen values below. Nothing here reads a file or runs anything.

Node kinds (exactly one kind key per node; :data:`NODE_KINDS`):

``op``      call one registered operation (:class:`Op`) with bound inputs.
``graph``   run a subgraph file with bound inputs.
``map``     run a subgraph (a file or an inline body) once per item of a list.
``repeat``  run a subgraph up to ``max`` times with carried values and a stop rule.
``human``   wait for a human answer that matches a schema (durable wait).
``commit``  check a bundle against the consumer contract, then call a commit
            adapter. Only the top level of an outer graph may hold one.

Scope names an expression may read (:data:`SCOPE_NAMES`): ``inputs`` (the
graph's inputs), ``carry`` and ``round`` (repeat), ``item`` and ``index``
(map item and position; in a repeat, ``index`` is the round number from 0),
and the ids of sibling nodes, each with the attributes its kind exposes
(:data:`KIND_ATTRIBUTES`; an op adds the attributes it declares).

The operation interfaces (:class:`Op`, :class:`OpContext`, :class:`OpResult`,
:class:`OpRegistry`) are what the runner and the generic and consumer
operations agree on. The registry that imports consumer namespaces by path is
``llm_kit.graph.ops.registry.load_registry(config) -> OpRegistry``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional, Union

__all__ = [
    "ATTRIBUTE_CARRY",
    "ATTRIBUTE_OUT",
    "ATTRIBUTE_ROUNDS",
    "Binding",
    "BUILTIN_OP_NAMESPACE",
    "Commit",
    "Graph",
    "GraphError",
    "Human",
    "KIND_ATTRIBUTES",
    "Literal",
    "MapSpec",
    "NODE_KINDS",
    "Node",
    "Op",
    "OpContext",
    "OpRegistry",
    "OpResult",
    "PARAM_KINDS",
    "ParamSpec",
    "Port",
    "RESERVED_NAMES",
    "RepeatSpec",
    "SCOPE_NAMES",
    "Carry",
    "UnknownOpError",
]

#: The node kinds, in the order the documentation lists them.
NODE_KINDS = ("op", "graph", "map", "repeat", "human", "commit")

#: Names an expression reads besides sibling node ids.
SCOPE_NAMES = frozenset({"inputs", "carry", "round", "item", "index"})

#: Names a node id, input, output or carry name may not take: the scope names,
#: the expression keywords and the expression functions.
RESERVED_NAMES = SCOPE_NAMES | frozenset({
    "and", "or", "not", "true", "false", "null",
    "len", "max", "min", "any", "all", "exists", "count",
})

ATTRIBUTE_OUT = "out"
ATTRIBUTE_ROUNDS = "rounds"
ATTRIBUTE_CARRY = "carry"

#: Attributes ``<node>.<attribute>`` each node kind exposes. An ``op`` node
#: exposes the attributes its :class:`Op` declares (always including ``out``).
KIND_ATTRIBUTES: Mapping[str, frozenset[str]] = MappingProxyType({
    "op": frozenset({ATTRIBUTE_OUT}),
    "graph": frozenset({ATTRIBUTE_OUT}),
    "map": frozenset({ATTRIBUTE_OUT}),
    "repeat": frozenset({ATTRIBUTE_ROUNDS, ATTRIBUTE_CARRY}),
    "human": frozenset({ATTRIBUTE_OUT}),
    "commit": frozenset({ATTRIBUTE_OUT}),
})

#: Kinds of an operation parameter (a node key other than ``op``, ``in`` and
#: ``when``). See :class:`ParamSpec`.
PARAM_KINDS = frozenset({"literal", "expr", "exprs", "schema", "file", "template", "selection"})

#: The op namespace llm-kit owns (``llm.model``, ``llm.validate``, ``llm.subprocess``).
BUILTIN_OP_NAMESPACE = "llm"

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
OP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)+$")


class GraphError(ValueError):
    """A graph file, project config or operation declaration is invalid.

    ``where`` names the file and the location in it (``file: nodes.x.in.y``)
    when known.
    """

    def __init__(self, message: str, where: str = "") -> None:
        self.message = message
        self.where = where
        super().__init__(f"{where}: {message}" if where else message)


class UnknownOpError(GraphError, KeyError):
    """An operation name is not in the registry."""

    def __str__(self) -> str:  # KeyError would quote the message
        return ValueError.__str__(self)


def _frozen(mapping: Mapping) -> Mapping:
    return MappingProxyType(dict(mapping))


# ---------------------------------------------------------------------------
# bindings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    """A JSON scalar written where an expression is allowed (``max: 3``)."""

    value: Union[None, bool, int, float]


#: A value position: an expression text (a YAML string) or a literal scalar.
Binding = Union[str, Literal]


# ---------------------------------------------------------------------------
# ports and nodes
# ---------------------------------------------------------------------------

_NO_DEFAULT = object()


@dataclass(frozen=True)
class Port:
    """One input or output: a resolved JSON Schema and an optional default.

    ``schema`` is self-contained: relative file ``$ref`` values are already
    bundled into local ``#/$defs`` references by the loader.
    """

    schema: Mapping[str, Any]
    default: Any = _NO_DEFAULT
    description: str = ""

    @property
    def has_default(self) -> bool:
        return self.default is not _NO_DEFAULT

    def to_json(self) -> dict:
        out: dict[str, Any] = {"schema": self.schema}
        if self.has_default:
            out["default"] = self.default
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True)
class Carry:
    """One carried value of a repeat: its first value and its next value."""

    init: Binding
    next: Binding


@dataclass(frozen=True)
class MapSpec:
    """``map``: run ``body`` once per element of ``over``, identified by ``key``."""

    over: Binding
    key: Binding
    body: "Graph"
    bind: Mapping[str, Binding]   # ``in`` for a file body; empty for an inline body
    inline: bool


@dataclass(frozen=True)
class RepeatSpec:
    """``repeat``: run ``body`` up to ``max`` times; stop when ``until`` is true."""

    max: Binding
    until: Optional[Binding]
    carry: Mapping[str, Carry]
    body: "Graph"
    bind: Mapping[str, Binding]
    inline: bool


@dataclass(frozen=True)
class Human:
    """``human``: a durable wait for an answer of ``kind`` that matches ``schema``."""

    kind: str
    schema: Mapping[str, Any]
    payload: Mapping[str, Binding]


@dataclass(frozen=True)
class Commit:
    """``commit``: check ``bundle`` against ``contract``, then call ``adapter``."""

    contract: Mapping[str, Any]
    bundle: Union[Binding, Mapping[str, Binding]]
    adapter: str


@dataclass(frozen=True)
class Node:
    """One node of a graph.

    ``raw`` is the node mapping as written (JSON values only); hashing uses it
    together with the referenced files. ``params`` holds an op node's keys other
    than ``op``, ``in`` and ``when``, unresolved: their meaning comes from the
    operation's :class:`ParamSpec` declarations.
    """

    id: str
    kind: str
    raw: Mapping[str, Any]
    when: Optional[Binding] = None
    op: Optional[str] = None
    bind: Mapping[str, Binding] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    subgraph: Optional["Graph"] = None
    map: Optional[MapSpec] = None
    repeat: Optional[RepeatSpec] = None
    human: Optional[Human] = None
    commit: Optional[Commit] = None

    def bodies(self) -> Iterator["Graph"]:
        """The graphs this node runs: a subgraph, a map body or a repeat body."""
        if self.subgraph is not None:
            yield self.subgraph
        if self.map is not None:
            yield self.map.body
        if self.repeat is not None:
            yield self.repeat.body


@dataclass(frozen=True)
class Graph:
    """A loaded graph: a graph file, or the inline body of a map or repeat.

    ``path`` is the file the graph was read from (for an inline body, the file
    that holds it) and ``base_dir`` the directory relative paths resolve
    against. ``location`` names an inline body inside its file
    (``nodes.loop.repeat.body``); it is empty for a file. An inline body has no
    ``inputs`` of its own: it reads the enclosing graph's scope, and its
    results are ``out`` (name -> binding). A file graph names its results in
    ``outputs`` and binds them with ``outputs_from``.
    """

    id: str
    path: Path
    base_dir: Path
    raw: Mapping[str, Any]
    nodes: Mapping[str, Node]
    inputs: Mapping[str, Port] = field(default_factory=dict)
    outputs: Mapping[str, Port] = field(default_factory=dict)
    outputs_from: Mapping[str, Binding] = field(default_factory=dict)
    outer: bool = False
    description: str = ""
    location: str = ""
    #: For an inline body: its out bindings (name -> binding).
    out: Mapping[str, Binding] = field(default_factory=dict)

    @property
    def inline(self) -> bool:
        return bool(self.location)

    def where(self, *parts: str) -> str:
        """``<file>: <location>.<parts>`` for messages."""
        dotted = ".".join(p for p in (self.location, *parts) if p)
        return f"{self.path}: {dotted}" if dotted else str(self.path)


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One node parameter an operation accepts.

    Kinds (:data:`PARAM_KINDS`):

    ``literal``    any JSON value, used as written.
    ``expr``       an expression (or a literal scalar).
    ``exprs``      a mapping of name -> expression.
    ``schema``     a JSON Schema; relative file ``$ref`` values resolve against
                   the graph file's directory.
    ``file``       a path relative to the graph file; the file must exist and its
                   bytes count toward the definition hash.
    ``template``   a prompt template path relative to the graph file; its slots
                   (:func:`llm_kit.graph.loader.prompt_slots`) must equal the keys
                   of the ``exprs`` parameter named by ``slots``.
    ``selection``  a model selection key that must exist in the project's
                   ``.llm-kit/models.yaml`` (with its personal layer).
    """

    kind: str
    required: bool = False
    slots: Optional[str] = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PARAM_KINDS:
            raise GraphError(f"unknown parameter kind {self.kind!r}; kinds: {', '.join(sorted(PARAM_KINDS))}")
        if (self.kind == "template") != (self.slots is not None):
            raise GraphError("a template parameter, and only a template parameter, names its slots parameter")


@dataclass(frozen=True)
class OpContext:
    """What the runner hands an operation for one call.

    ``params`` are the node's parameters after resolution: an ``expr`` or
    ``exprs`` parameter is evaluated, a ``schema`` is resolved, a ``file`` or
    ``template`` is an absolute path. ``store`` (content-addressed media and
    blobs) and ``services`` (spend, concurrency, halt) are set by the runner;
    an operation that needs one and finds none fails.
    """

    run_id: str
    item: Optional[str]
    graph_id: str
    node_path: str
    project_root: Path
    base_dir: Path
    params: Mapping[str, Any] = field(default_factory=dict)
    store: Any = None
    services: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpResult:
    """An operation's result.

    ``out`` is the node's ``out`` (a JSON value). ``attributes`` holds the
    other attributes the operation declares (``sent``, ``text``). ``receipt``
    is the JSON call receipt (model, usage, cost, timings); ``media`` lists
    media references the run record keeps. An operation function may return a
    bare JSON value instead; the runner treats it as ``OpResult(out=value)``.
    """

    out: Any
    attributes: Mapping[str, Any] = field(default_factory=dict)
    receipt: Optional[Mapping[str, Any]] = None
    media: tuple = ()


@dataclass(frozen=True)
class Op:
    """A registered operation.

    ``fn(ctx, **inputs)`` returns a JSON value or an :class:`OpResult`.
    ``inputs`` are the ports a node binds with ``in``; ``outputs`` is the schema
    of ``out``, or ``None`` when ``output_schema_param`` names the ``schema``
    parameter that gives it per node (``llm.model``'s ``output_schema``) or the
    output is not declared. Every schema is self-contained (the ``@op``
    decorator resolves relative file references with
    :func:`llm_kit.graph.loader.resolve_schema`). ``version`` enters every
    definition hash of a node that uses the operation.

    ``paid``, ``estimate`` and ``limit`` (a concurrency kind) are read by the
    runtime services. ``commit`` marks a commit adapter: only a ``commit`` node
    may call it.
    """

    name: str
    version: str
    fn: Callable[..., Any]
    inputs: Mapping[str, Port] = field(default_factory=dict)
    outputs: Optional[Mapping[str, Any]] = None
    params: Mapping[str, ParamSpec] = field(default_factory=dict)
    attributes: frozenset[str] = frozenset({ATTRIBUTE_OUT})
    output_schema_param: Optional[str] = None
    paid: bool = False
    estimate: Optional[Callable[..., float]] = None
    limit: Optional[str] = None
    commit: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not OP_NAME_RE.match(self.name):
            raise GraphError(f"op name {self.name!r} must be dotted: <namespace>.<name>")
        if not isinstance(self.version, str) or not self.version:
            raise GraphError(f"op {self.name}: version must be a non-empty string")
        if not callable(self.fn):
            raise GraphError(f"op {self.name}: fn is not callable")
        if ATTRIBUTE_OUT not in self.attributes:
            raise GraphError(f"op {self.name}: attributes must include 'out'")
        for name in self.attributes:
            if not NAME_RE.match(name) or name in RESERVED_NAMES:
                raise GraphError(f"op {self.name}: attribute {name!r} is not a valid name")
        for name in self.inputs:
            if not NAME_RE.match(name):
                raise GraphError(f"op {self.name}: input {name!r} is not a valid name")
        for name, spec in self.params.items():
            if name in ("op", "in", "when"):
                raise GraphError(f"op {self.name}: parameter {name!r} is a reserved node key")
            if spec.kind == "template" and self.params.get(spec.slots, ParamSpec("literal")).kind != "exprs":
                raise GraphError(f"op {self.name}: template parameter {name!r} names slots "
                                 f"{spec.slots!r}, which is not an 'exprs' parameter")
        if self.output_schema_param is not None:
            spec = self.params.get(self.output_schema_param)
            if spec is None or spec.kind != "schema":
                raise GraphError(f"op {self.name}: output_schema_param {self.output_schema_param!r} "
                                 "is not a 'schema' parameter")
            if self.outputs is not None:
                raise GraphError(f"op {self.name}: declares both outputs and output_schema_param")
        object.__setattr__(self, "inputs", _frozen(self.inputs))
        object.__setattr__(self, "params", _frozen(self.params))
        object.__setattr__(self, "attributes", frozenset(self.attributes))

    @property
    def ref(self) -> str:
        """``name@version``, the form a definition hash includes."""
        return f"{self.name}@{self.version}"


class OpRegistry:
    """Operations by name. Registering a name twice is an error."""

    def __init__(self, ops: Optional[Mapping[str, Op]] = None) -> None:
        self._ops: dict[str, Op] = {}
        for op in (ops or {}).values():
            self.register(op)

    def register(self, op: Op) -> Op:
        if not isinstance(op, Op):
            raise GraphError(f"cannot register {type(op).__name__}; expected an Op")
        if op.name in self._ops:
            raise GraphError(f"op {op.name} is registered twice")
        self._ops[op.name] = op
        return op

    def get(self, name: str) -> Op:
        try:
            return self._ops[name]
        except KeyError:
            raise UnknownOpError(f"unknown op {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._ops

    def names(self) -> list[str]:
        return sorted(self._ops)

    def __len__(self) -> int:
        return len(self._ops)
