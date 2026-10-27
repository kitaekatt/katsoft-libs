"""Load graph files, resolve schema file references and read prompt templates.

The loader checks the STRUCTURE of a graph file: known keys, one kind per
node, valid names, well-formed and self-contained schemas, subgraph files that
exist and do not include themselves. It raises :class:`GraphError` on the first
structural fault. Meaning (references, ops, slots, port compatibility, cycles)
is checked by :mod:`llm_kit.graph.validate`, which collects every problem.

YAML needs PyYAML (the ``graph`` extra). A graph file holds JSON values only:
duplicate keys, non-string keys, dates, binary data and non-finite numbers are
refused.

Schema file references: a ``$ref`` whose value does not start with ``#`` names
a JSON file relative to the file that holds it (``../contracts/plot.schema.json``).
:func:`resolve_schema` bundles each referenced file into the root's ``$defs``
under ``file:<path relative to the root's directory>`` and rewrites the
reference, so the result is self-contained and passes
:func:`llm_kit.schema.json_schema.check_schema`. A file's own ``$defs`` become
``file:<path>#<name>``. A reference with a fragment (``x.json#/$defs/a``) is
refused.

Prompt templates: an optional header for people, then one line that is exactly
:data:`PROMPT_MARKER`; only the text after it is the prompt. A file without the
marker line is all prompt. Slots are ``{{name}}`` (lower-case letters, digits
and ``_``, starting with a letter).
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from llm_kit.schema.json_schema import check_schema, validate as validate_value

from .model import (
    NAME_RE,
    NODE_KINDS,
    OP_NAME_RE,
    RESERVED_NAMES,
    Binding,
    Carry,
    Commit,
    Graph,
    GraphError,
    Human,
    Literal,
    MapSpec,
    Node,
    Op,
    ParamSpec,
    Port,
    RepeatSpec,
)

__all__ = [
    "PROMPT_MARKER",
    "SLOT_RE",
    "ResolvedParam",
    "load_graph",
    "load_yaml",
    "prompt_body",
    "prompt_slots",
    "resolve_params",
    "resolve_schema",
]

PROMPT_MARKER = "---PROMPT---"
SLOT_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
GRAPH_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)*$")
FILE_DEF_PREFIX = "file:"

_GRAPH_KEYS = ("id", "description", "outer", "inputs", "outputs", "nodes", "outputs_from")
_BODY_KEYS = ("nodes", "out")
_PORT_KEYS = ("schema", "default", "description")
_MAP_KEYS = ("over", "key", "graph", "body", "in")
_REPEAT_KEYS = ("max", "until", "carry", "graph", "body", "in")
_CARRY_KEYS = ("init", "next")
_HUMAN_KEYS = ("kind", "schema", "payload")
_COMMIT_KEYS = ("contract", "bundle", "adapter")
_OP_NODE_KEYS = ("op", "in", "when")


# ---------------------------------------------------------------------------
# YAML and JSON documents
# ---------------------------------------------------------------------------


def _import_yaml() -> Any:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        raise GraphError("PyYAML is required to read graph files; install katsoft-llm-kit[graph]") from None
    return yaml


def _loader_class() -> Any:
    yaml = _import_yaml()

    class Loader(yaml.SafeLoader):
        def construct_mapping(self, node: Any, deep: bool = False) -> dict:
            mapping = super().construct_mapping(node, deep=deep)
            if len(mapping) != len(node.value):
                raise yaml.constructor.ConstructorError(None, None, "duplicate mapping key", node.start_mark)
            return mapping

    return Loader


def _require_json(value: Any, where: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphError("non-finite numbers are not JSON", where)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json(item, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise GraphError(f"mapping key {key!r} is not a string", where)
            _require_json(item, f"{where}.{key}" if where else key)
        return
    raise GraphError(f"{type(value).__name__} values are not JSON (quote it as a string)", where)


def load_yaml(path: Path) -> Any:
    """One YAML document of JSON values from ``path``."""
    yaml = _import_yaml()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise GraphError("file does not exist", str(path)) from None
    except UnicodeDecodeError as exc:
        raise GraphError(f"file is not UTF-8: {exc}", str(path)) from None
    try:
        document = yaml.load(text, Loader=_loader_class())  # noqa: S506 -- a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise GraphError(f"not valid YAML: {exc}", str(path)) from None
    if document is None:
        raise GraphError("file is empty", str(path))
    _require_json(document, "")
    return document


def _json_pairs(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _load_json(path: Path, where: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise GraphError(f"schema file {path} does not exist", where) from None
    try:
        return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_refuse_constant)
    except ValueError as exc:
        raise GraphError(f"schema file {path} is not valid JSON: {exc}", where) from None


def _refuse_constant(token: str) -> Any:
    raise ValueError(f"{token} is not JSON")


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_schema(schema: Any, base_dir: Path, *, where: str = "") -> dict:
    """A self-contained copy of ``schema`` with file references bundled.

    Relative file ``$ref`` values resolve against ``base_dir`` (and, inside a
    referenced file, against that file's directory). The result passes
    :func:`check_schema`; any fault raises :class:`GraphError`.
    """
    if not isinstance(schema, Mapping):
        raise GraphError("a schema must be a mapping", where)
    root_dir = Path(base_dir).resolve()
    bundled: dict[str, Any] = {}
    keys_by_path: dict[Path, str] = {}

    def file_key(path: Path) -> str:
        return FILE_DEF_PREFIX + PurePosixPath(Path(os.path.relpath(path, root_dir))).as_posix()

    def include(path: Path) -> str:
        if path in keys_by_path:
            return keys_by_path[path]
        key = file_key(path)
        keys_by_path[path] = key
        data = _load_json(path, where)
        if not isinstance(data, Mapping):
            raise GraphError(f"schema file {path} must hold a JSON object", where)
        own_defs = data.get("$defs", {})
        if not isinstance(own_defs, Mapping):
            raise GraphError(f"schema file {path}: $defs must be an object", where)
        bundled[key] = rewrite({k: v for k, v in data.items() if k != "$defs"}, path.parent, key)
        for name, sub in own_defs.items():
            bundled[f"{key}#{name}"] = rewrite(sub, path.parent, key)
        return key

    def rewrite_ref(ref: Any, cur_dir: Path, prefix: Optional[str]) -> Any:
        if not isinstance(ref, str) or not ref:
            raise GraphError(f"$ref {ref!r} must be a non-empty string", where)
        if ref.startswith("#"):
            if prefix is None:
                return ref
            if not ref.startswith("#/$defs/"):
                raise GraphError(f"$ref {ref!r} in a schema file must name #/$defs/<name>", where)
            name = _unescape(ref[len("#/$defs/"):])
            return "#/$defs/" + _escape(f"{prefix}#{name}")
        if "#" in ref:
            raise GraphError(f"$ref {ref!r}: a file reference names a whole file, without a fragment", where)
        if Path(ref).is_absolute() or PurePosixPath(ref).is_absolute():
            raise GraphError(f"$ref {ref!r} must be a relative path", where)
        return "#/$defs/" + _escape(include((cur_dir / ref).resolve()))

    def rewrite(node: Any, cur_dir: Path, prefix: Optional[str]) -> Any:
        if not isinstance(node, Mapping):
            return node  # check_schema reports it
        out: dict[str, Any] = {}
        for keyword, value in node.items():
            if keyword == "$ref":
                out[keyword] = rewrite_ref(value, cur_dir, prefix)
            elif keyword in ("properties", "$defs") and isinstance(value, Mapping):
                out[keyword] = {k: rewrite(v, cur_dir, prefix) for k, v in value.items()}
            elif keyword in ("items", "additionalProperties"):
                out[keyword] = rewrite(value, cur_dir, prefix)
            elif keyword == "anyOf" and isinstance(value, list):
                out[keyword] = [rewrite(v, cur_dir, prefix) for v in value]
            else:
                out[keyword] = value
        return out

    result = rewrite(schema, root_dir, None)
    if bundled:
        own = dict(result.get("$defs", {})) if isinstance(result.get("$defs"), Mapping) else {}
        clash = sorted(set(own) & set(bundled))
        if clash:
            raise GraphError(f"$defs names {clash} are reserved for bundled schema files", where)
        if "$defs" in result and not isinstance(result["$defs"], Mapping):
            raise GraphError("$defs must be an object", where)
        result["$defs"] = {**own, **bundled}
    try:
        check_schema(result)
    except ValueError as exc:
        raise GraphError(f"invalid schema: {exc}", where) from None
    return result


def _port(value: Any, base_dir: Path, where: str) -> Port:
    if not isinstance(value, Mapping):
        raise GraphError("a port is a JSON Schema mapping or {schema, default, description}", where)
    if "schema" in value:
        unknown = sorted(set(value) - set(_PORT_KEYS))
        if unknown:
            raise GraphError(f"unknown port keys {unknown}; allowed: {list(_PORT_KEYS)}", where)
        schema = resolve_schema(value["schema"], base_dir, where=f"{where}.schema")
        description = value.get("description", "")
        if not isinstance(description, str):
            raise GraphError("description must be a string", where)
        if "default" in value:
            errors = validate_value(schema, value["default"])
            if errors:
                raise GraphError(f"default does not match the schema: {list(errors)}", where)
            return Port(schema, value["default"], description)
        return Port(schema, description=description)
    return Port(resolve_schema(value, base_dir, where=where))


def _ports(value: Any, base_dir: Path, where: str) -> dict[str, Port]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GraphError("must be a mapping of name -> port", where)
    out = {}
    for name, port in value.items():
        _check_name(name, f"{where}.{name}", "port")
        out[name] = _port(port, base_dir, f"{where}.{name}")
    return out


# ---------------------------------------------------------------------------
# prompt templates
# ---------------------------------------------------------------------------


def prompt_body(text: str, *, where: str = "") -> str:
    """The prompt text: after the one marker line, or the whole text without one."""
    lines = text.split("\n")
    at = [i for i, line in enumerate(lines) if line == PROMPT_MARKER]
    if not at:
        return text
    if len(at) > 1:
        raise GraphError(f"template has {len(at)} {PROMPT_MARKER!r} lines; it may have one", where)
    return "\n".join(lines[at[0] + 1:])


def prompt_slots(text: str, *, where: str = "") -> tuple[str, ...]:
    """The slot names of a template's prompt text, in order of first use."""
    seen: dict[str, None] = {}
    for match in SLOT_RE.finditer(prompt_body(text, where=where)):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


# ---------------------------------------------------------------------------
# graph files
# ---------------------------------------------------------------------------


def _check_name(name: Any, where: str, what: str) -> None:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise GraphError(f"{what} name {name!r} must match {NAME_RE.pattern}", where)
    if name in RESERVED_NAMES:
        raise GraphError(f"{what} name {name!r} is reserved", where)


def _keys(value: Any, allowed: tuple[str, ...], where: str, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise GraphError(f"{what} must be a mapping", where)
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise GraphError(f"unknown {what} keys {unknown}; allowed: {list(allowed)}", where)
    return value


def _binding(value: Any, where: str) -> Binding:
    if isinstance(value, str):
        if not value.strip():
            raise GraphError("an expression may not be empty", where)
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return Literal(value)
    raise GraphError("must be an expression (a string) or a literal scalar", where)


def _bindings(value: Any, where: str, *, names: bool = True) -> dict[str, Binding]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GraphError("must be a mapping of name -> expression", where)
    out = {}
    for name, item in value.items():
        if names:
            if not isinstance(name, str) or not NAME_RE.match(name):
                raise GraphError(f"name {name!r} must match {NAME_RE.pattern}", f"{where}.{name}")
        out[name] = _binding(item, f"{where}.{name}")
    return out


class _Loader:
    def __init__(self) -> None:
        self.cache: dict[Path, Graph] = {}

    def file(self, path: Path, stack: tuple[Path, ...], *, as_subgraph: bool) -> Graph:
        path = Path(path).resolve()
        if path in stack:
            chain = " -> ".join(str(p) for p in (*stack, path))
            raise GraphError(f"subgraph cycle: {chain}", str(path))
        graph = self.cache.get(path)
        if graph is None:
            graph = self._file(path, stack + (path,))
            self.cache[path] = graph
        if as_subgraph and graph.outer:
            raise GraphError("an outer graph cannot be a subgraph", str(path))
        return graph

    def _file(self, path: Path, stack: tuple[Path, ...]) -> Graph:
        doc = load_yaml(path)
        where = str(path)
        _keys(doc, _GRAPH_KEYS, where, "graph")
        graph_id = doc.get("id")
        if not isinstance(graph_id, str) or not GRAPH_ID_RE.match(graph_id):
            raise GraphError(f"id {graph_id!r} must be a dotted name", f"{where}: id")
        outer = doc.get("outer", False)
        if not isinstance(outer, bool):
            raise GraphError("outer must be true or false", f"{where}: outer")
        description = doc.get("description", "")
        if not isinstance(description, str):
            raise GraphError("description must be a string", f"{where}: description")
        base_dir = path.parent
        inputs = _ports(doc.get("inputs"), base_dir, f"{where}: inputs")
        outputs = _ports(doc.get("outputs"), base_dir, f"{where}: outputs")
        outputs_from = _bindings(doc.get("outputs_from"), f"{where}: outputs_from")
        if set(outputs_from) != set(outputs):
            raise GraphError(
                f"outputs_from must bind exactly the outputs {sorted(outputs)}; it binds {sorted(outputs_from)}",
                f"{where}: outputs_from")
        nodes = self._nodes(doc.get("nodes"), path, base_dir, "", graph_id, stack)
        return Graph(id=graph_id, path=path, base_dir=base_dir, raw=doc, nodes=nodes, inputs=inputs,
                     outputs=outputs, outputs_from=outputs_from, outer=outer, description=description)

    def _nodes(self, value: Any, path: Path, base_dir: Path, location: str, graph_id: str,
               stack: tuple[Path, ...]) -> dict[str, Node]:
        here = f"{path}: {location + '.' if location else ''}nodes"
        if not isinstance(value, Mapping) or not value:
            raise GraphError("nodes must be a non-empty mapping", here)
        out = {}
        for node_id, raw in value.items():
            _check_name(node_id, f"{here}.{node_id}", "node")
            node_location = f"{location + '.' if location else ''}nodes.{node_id}"
            out[node_id] = self._node(node_id, raw, path, base_dir, node_location, graph_id, stack)
        return out

    def _body(self, spec: Mapping, kind: str, node_id: str, path: Path, base_dir: Path, location: str,
              graph_id: str, stack: tuple[Path, ...]) -> tuple[Graph, dict[str, Binding], bool]:
        where = f"{path}: {location}.{kind}"
        if ("graph" in spec) == ("body" in spec):
            raise GraphError(f"{kind} needs exactly one of graph (a file) or body (inline)", where)
        if "graph" in spec:
            body = self._subgraph(spec["graph"], base_dir, f"{where}.graph", stack)
            return body, _bindings(spec.get("in"), f"{where}.in"), False
        if "in" in spec:
            raise GraphError("in binds a subgraph file's inputs; an inline body reads the scope directly",
                             f"{where}.in")
        body_location = f"{location}.{kind}.body"
        raw = _keys(spec["body"], _BODY_KEYS, f"{path}: {body_location}", "body")
        if "out" not in raw:
            raise GraphError("an inline body needs out (name -> expression)", f"{path}: {body_location}")
        nodes = self._nodes(raw.get("nodes"), path, base_dir, body_location, graph_id, stack)
        out = _bindings(raw["out"], f"{path}: {body_location}.out")
        if not out:
            raise GraphError("out must bind at least one name", f"{path}: {body_location}.out")
        for name in out:
            _check_name(name, f"{path}: {body_location}.out.{name}", "out")
        body = Graph(id=f"{graph_id}#{node_id}", path=path, base_dir=base_dir, raw=raw, nodes=nodes,
                     location=body_location, out=out)
        return body, {}, True

    def _subgraph(self, ref: Any, base_dir: Path, where: str, stack: tuple[Path, ...]) -> Graph:
        if not isinstance(ref, str) or not ref:
            raise GraphError("graph must be a relative path to a graph file", where)
        if Path(ref).is_absolute():
            raise GraphError(f"graph {ref!r} must be a relative path", where)
        target = (base_dir / ref).resolve()
        if not target.is_file():
            raise GraphError(f"subgraph file {ref!r} does not exist ({target})", where)
        return self.file(target, stack, as_subgraph=True)

    def _node(self, node_id: str, raw: Any, path: Path, base_dir: Path, location: str, graph_id: str,
              stack: tuple[Path, ...]) -> Node:
        where = f"{path}: {location}"
        if not isinstance(raw, Mapping):
            raise GraphError("a node must be a mapping", where)
        kinds = [k for k in NODE_KINDS if k in raw]
        if len(kinds) != 1:
            raise GraphError(f"a node needs exactly one kind key of {list(NODE_KINDS)}; found {kinds}", where)
        kind = kinds[0]
        when = _binding(raw["when"], f"{where}.when") if "when" in raw else None
        common = dict(id=node_id, kind=kind, raw=raw, when=when)
        if kind == "op":
            op = raw["op"]
            if not isinstance(op, str) or not OP_NAME_RE.match(op):
                raise GraphError(f"op {op!r} must be a dotted op name", f"{where}.op")
            params = {k: v for k, v in raw.items() if k not in _OP_NODE_KEYS}
            return Node(**common, op=op, bind=_bindings(raw.get("in"), f"{where}.in"), params=params)
        if kind == "graph":
            _keys(raw, ("graph", "in", "when"), where, "graph node")
            sub = self._subgraph(raw["graph"], base_dir, f"{where}.graph", stack)
            return Node(**common, subgraph=sub, bind=_bindings(raw.get("in"), f"{where}.in"))
        if kind == "map":
            _keys(raw, ("map", "when"), where, "map node")
            spec = _keys(raw["map"], _MAP_KEYS, f"{where}.map", "map")
            for required in ("over", "key"):
                if required not in spec:
                    raise GraphError(f"map needs {required}", f"{where}.map")
            body, bind, inline = self._body(spec, "map", node_id, path, base_dir, location, graph_id, stack)
            return Node(**common, map=MapSpec(over=_binding(spec["over"], f"{where}.map.over"),
                                              key=_binding(spec["key"], f"{where}.map.key"),
                                              body=body, bind=bind, inline=inline))
        if kind == "repeat":
            _keys(raw, ("repeat", "when"), where, "repeat node")
            spec = _keys(raw["repeat"], _REPEAT_KEYS, f"{where}.repeat", "repeat")
            if "max" not in spec:
                raise GraphError("repeat needs max", f"{where}.repeat")
            carry = {}
            carry_raw = spec.get("carry", {})
            if not isinstance(carry_raw, Mapping):
                raise GraphError("carry must be a mapping of name -> {init, next}", f"{where}.repeat.carry")
            for name, item in carry_raw.items():
                cw = f"{where}.repeat.carry.{name}"
                _check_name(name, cw, "carry")
                item = _keys(item, _CARRY_KEYS, cw, "carry")
                if set(item) != set(_CARRY_KEYS):
                    raise GraphError("a carried value needs init and next", cw)
                carry[name] = Carry(init=_binding(item["init"], f"{cw}.init"),
                                    next=_binding(item["next"], f"{cw}.next"))
            body, bind, inline = self._body(spec, "repeat", node_id, path, base_dir, location, graph_id, stack)
            until = _binding(spec["until"], f"{where}.repeat.until") if "until" in spec else None
            return Node(**common, repeat=RepeatSpec(max=_binding(spec["max"], f"{where}.repeat.max"),
                                                    until=until, carry=carry, body=body, bind=bind,
                                                    inline=inline))
        if kind == "human":
            _keys(raw, ("human", "when"), where, "human node")
            spec = _keys(raw["human"], _HUMAN_KEYS, f"{where}.human", "human")
            human_kind = spec.get("kind")
            if not isinstance(human_kind, str) or not human_kind:
                raise GraphError("human needs kind (a non-empty string)", f"{where}.human")
            if "schema" not in spec:
                raise GraphError("human needs schema", f"{where}.human")
            schema = resolve_schema(spec["schema"], base_dir, where=f"{where}.human.schema")
            return Node(**common, human=Human(human_kind, schema,
                                              _bindings(spec.get("payload"), f"{where}.human.payload")))
        _keys(raw, ("commit", "when"), where, "commit node")
        spec = _keys(raw["commit"], _COMMIT_KEYS, f"{where}.commit", "commit")
        missing = [k for k in _COMMIT_KEYS if k not in spec]
        if missing:
            raise GraphError(f"commit needs {missing}", f"{where}.commit")
        contract = resolve_schema(spec["contract"] if isinstance(spec["contract"], Mapping)
                                  else {"$ref": spec["contract"]}, base_dir, where=f"{where}.commit.contract")
        adapter = spec["adapter"]
        if not isinstance(adapter, str) or not OP_NAME_RE.match(adapter):
            raise GraphError(f"adapter {adapter!r} must be a dotted op name", f"{where}.commit.adapter")
        bundle_raw = spec["bundle"]
        bundle = (_bindings(bundle_raw, f"{where}.commit.bundle") if isinstance(bundle_raw, Mapping)
                  else _binding(bundle_raw, f"{where}.commit.bundle"))
        return Node(**common, commit=Commit(contract=contract, bundle=bundle, adapter=adapter))


def load_graph(path: Path | str) -> Graph:
    """Load a graph file and every subgraph file it names."""
    return _Loader().file(Path(path), (), as_subgraph=False)


# ---------------------------------------------------------------------------
# op parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedParam:
    """One op parameter of a node, resolved against its :class:`ParamSpec`.

    ``value`` is the value as written. ``schema`` is set for a ``schema``
    parameter; ``path`` and ``data`` for a ``file``; ``path``, ``body`` and
    ``slots`` for a ``template``; ``bindings`` for ``expr`` (one entry, key
    ``""``) and ``exprs``.
    """

    name: str
    kind: str
    value: Any
    schema: Optional[Mapping[str, Any]] = None
    path: Optional[Path] = None
    data: Optional[bytes] = None
    body: Optional[str] = None
    slots: tuple[str, ...] = ()
    bindings: Optional[Mapping[str, Binding]] = None


def _relative_file(value: Any, base_dir: Path, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise GraphError("must be a relative file path", where)
    if Path(value).is_absolute():
        raise GraphError(f"{value!r} must be a relative path", where)
    path = (base_dir / value).resolve()
    if not path.is_file():
        raise GraphError(f"file {value!r} does not exist ({path})", where)
    return path


def resolve_params(node: Node, op: Op, graph: Graph) -> dict[str, ResolvedParam]:
    """Resolve an op node's parameters against the op's declarations.

    Raises :class:`GraphError` for an unknown or missing parameter, a value of
    the wrong shape, a missing file or an invalid schema.
    """
    where = graph.where(f"nodes.{node.id}")
    unknown = sorted(set(node.params) - set(op.params))
    if unknown:
        raise GraphError(f"op {op.name} takes no parameters {unknown}; it takes {sorted(op.params)}", where)
    out: dict[str, ResolvedParam] = {}
    for name, spec in op.params.items():
        pw = f"{where}.{name}"
        if name not in node.params:
            if spec.required:
                raise GraphError(f"op {op.name} needs parameter {name!r}", where)
            continue
        value = node.params[name]
        if spec.kind == "literal":
            out[name] = ResolvedParam(name, spec.kind, value)
        elif spec.kind == "expr":
            out[name] = ResolvedParam(name, spec.kind, value, bindings={"": _binding(value, pw)})
        elif spec.kind == "exprs":
            out[name] = ResolvedParam(name, spec.kind, value, bindings=_bindings(value, pw))
        elif spec.kind == "schema":
            out[name] = ResolvedParam(name, spec.kind, value, schema=resolve_schema(value, graph.base_dir, where=pw))
        elif spec.kind == "file":
            path = _relative_file(value, graph.base_dir, pw)
            out[name] = ResolvedParam(name, spec.kind, value, path=path, data=path.read_bytes())
        elif spec.kind == "template":
            path = _relative_file(value, graph.base_dir, pw)
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise GraphError(f"template is not UTF-8: {exc}", pw) from None
            body = prompt_body(text, where=pw)
            out[name] = ResolvedParam(name, spec.kind, value, path=path, body=body,
                                      slots=prompt_slots(text, where=pw))
        else:  # selection
            if not isinstance(value, str) or not value:
                raise GraphError("must be a model selection key", pw)
            out[name] = ResolvedParam(name, spec.kind, value)
    return out
