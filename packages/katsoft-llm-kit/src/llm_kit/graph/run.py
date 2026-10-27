"""Run a graph for one work item, reuse fresh records, seed accepted outputs.

:class:`Runner` runs a validated graph: sibling nodes as a DAG on
``pipeline.execution.scheduler`` (a node starts when the nodes it reads are
complete; a failed node gates only its dependents), ``when`` skips, subgraph
files, ``map`` items in parallel with stable keys, and ``repeat`` rounds with
carried values and every round kept. Before a node runs, an earlier record
with the same definition hash and input hash is reused instead
(:mod:`llm_kit.graph.freshness`). Every node execution is written to the run
record (:mod:`llm_kit.graph.records`), values by reference to the
content-addressed stores (:mod:`llm_kit.graph.media`).

``human`` and ``commit`` nodes stop the run cleanly in this build (status
``stopped``, reason ``unsupported``); the services units replace
:meth:`Runner._run_human` and :meth:`Runner._run_commit`. A ``paid`` op is
refused unless the runner was given the spend service (``services["spend"]``).

The behaviour and the record format are specified in
``docs/reference/llm-kit-graph-runtime.md``.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from llm_kit.pipeline.execution.scheduler import NodeFailed, Scheduler, SchedulerNode, run_graph
from llm_kit.schema.json_schema import validate as validate_value

from .expr import ExprError, evaluate, references
from .config import GraphConfig, load_config
from .freshness import ReadsAnalyzer, ReuseIndex, forced, input_hash, parse_binding, reads_document
from .hashing import GraphHashes, hash_graph
from .loader import load_graph, resolve_params
from .media import StoreError, check_json, ref_kind
from .model import (
    KIND_ATTRIBUTES,
    Binding,
    Graph,
    GraphError,
    Literal,
    Node,
    Op,
    OpContext,
    OpRegistry,
    OpResult,
    Port,
)
from .ops import OpError, OpHalted
from .records import RUN_FORMAT, RunStore, new_run_id, utc_now
from .validate import validate_graph

__all__ = ["NodeStopped", "PlanEntry", "RunResult", "Runner"]


class NodeStopped(Exception):
    """Raised by an op or a service to stop the run cleanly.

    The node is recorded ``stopped``; no new node starts; nodes already
    running finish; the rest are ``pending``. ``reason`` is a short word
    (``unsupported``, ``halted``, ``waiting``, ``spend_cap``).
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


@dataclass(frozen=True)
class PlanEntry:
    """One node of a dry run.

    ``action``: ``reuse`` (``run`` names the record's run), ``skip``, ``run``
    (``reason`` says why) or ``unknown`` (it reads a value a node due to run
    would make, or lies inside such a node).
    """

    path: str
    kind: str
    op: Optional[str]
    paid: bool
    action: str
    reason: str = ""
    run: Optional[str] = None


@dataclass(frozen=True)
class RunResult:
    """What :meth:`Runner.run` returns."""

    run_id: str
    status: str
    record_path: Path
    outputs: Optional[dict]
    stop: Optional[dict]
    nodes: Mapping[str, str] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)


class _Unknown:
    """A value a dry run cannot know (a node due to run would make it)."""

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()


class _Ended(Exception):
    """A node ended without a result; its record is written. ``status`` is failed or stopped."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


class _Exec:
    """The state of one run or one dry run."""

    def __init__(self, runner: "Runner", *, plan: bool, force: Sequence[str]) -> None:
        self.plan = plan
        self.force = tuple(force)
        self.index = ReuseIndex(runner.store)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.stop: Optional[dict] = None
        self.run_id = new_run_id()
        self.record: dict = {}
        self.entries: list[PlanEntry] = []


def _encode_key(key: Union[str, int]) -> str:
    text = str(key)
    for char in "%/[]":
        text = text.replace(char, "%{:02X}".format(ord(char)))
    return text


def _schema_errors(schema: Mapping, value: Any) -> list:
    return [f"{pointer or '/'} {keyword}" for pointer, keyword in validate_value(schema, value)]


def _check_port(port: Port, value: Any, what: str) -> None:
    errors = _schema_errors(port.schema, value)
    if errors:
        raise GraphError(f"{what} does not match its schema: {', '.join(errors)}")


def _with_defaults(ports: Mapping[str, Port], values: dict, what: str) -> dict:
    out = dict(values)
    unknown = sorted(set(out) - set(ports))
    if unknown:
        raise GraphError(f"{what} has no inputs {unknown}; inputs: {sorted(ports)}")
    for name, port in ports.items():
        if name not in out:
            if not port.has_default:
                raise GraphError(f"{what} needs input {name!r}")
            out[name] = copy.deepcopy(port.default)
        _check_port(port, out[name], f"{what} input {name!r}")
    return out


def _error(exc: BaseException) -> dict:
    out = {"type": type(exc).__name__, "message": str(exc)}
    if not isinstance(exc, (GraphError, ExprError, NodeStopped, OpError)):
        out["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-20:])
    return out


class Runner:
    """Runs one graph for one work item.

    ``services`` is the mapping an op sees as ``OpContext.services``; the
    runner reads ``services["spend"]`` only to admit paid ops (unit L05).
    ``max_parallel`` bounds the nodes (or map items) one level runs at once.
    """

    def __init__(self, graph: Graph, registry: OpRegistry, store: RunStore, *, item: str,
                 project_root: Path | str, services: Optional[Mapping[str, Any]] = None,
                 max_parallel: int = 8) -> None:
        if max_parallel < 1:
            raise GraphError("max_parallel must be at least 1")
        self.graph = graph
        self.registry = registry
        self.store = store
        self.item = item
        self.project_root = Path(project_root).resolve()
        self.services = dict(services or {})
        self.max_parallel = max_parallel
        problems = validate_graph(graph, registry=registry, project_root=self.project_root)
        if problems:
            raise GraphError(f"graph {graph.id} is invalid:\n" + "\n".join(str(p) for p in problems))
        self.hashes = hash_graph(graph, registry)
        self.reads = ReadsAnalyzer(registry)
        self._structure: Optional[dict] = None

    @classmethod
    def for_project(cls, project_root: Path | str, graph: str, item: str, *,
                    registry: Optional[OpRegistry] = None, services: Optional[Mapping[str, Any]] = None,
                    max_parallel: int = 8, config: Optional[GraphConfig] = None) -> "Runner":
        """A runner from the project config: GRAPH is a configured name or a graph file path."""
        config = config or load_config(project_root)
        if graph in config.graphs:
            path = config.graphs[graph]
        else:
            path = Path(graph)
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.is_file():
                raise GraphError(f"{graph!r} is neither a graph in {config.path} nor a file")
        if registry is None:
            from .commands import validate as _validate  # noqa: PLC0415 -- the registry loader of the CLI
            registry = _validate.load_registry(config)
        return cls(load_graph(path), registry, RunStore.for_item(config, item), item=item,
                   project_root=config.root, services=services, max_parallel=max_parallel)

    # ------------------------------------------------------------------
    # public verbs
    # ------------------------------------------------------------------

    def run(self, inputs: Optional[Mapping[str, Any]] = None, *, target: Iterable[str] = (),
            force: Iterable[str] = ()) -> RunResult:
        """Run the graph (or the ``target`` nodes and what they read) and write the record."""
        target_ids, force = self._check_selection(target, force)
        values, sources = self.resolve_inputs(inputs)
        ex = _Exec(self, plan=False, force=force)
        ex.record = {
            "format": RUN_FORMAT,
            "run_id": ex.run_id,
            "kind": "run",
            "item": self.item,
            "status": "running",
            "started_at": utc_now(),
            "ended_at": None,
            "graph": self._graph_head(),
            "graphs": self.structure(),
            "files": self._snapshot_files(),
            "target": sorted(target_ids) if target_ids is not None else None,
            "force": list(force),
            "inputs": self.store.put_value(values),
            "inputs_from": sources,
            "outputs": None,
            "stop": None,
            "error": None,
            "counts": {},
            "nodes": {},
        }
        path = self.store.write_run(ex.record)
        outputs = None
        try:
            try:
                outputs = self._file_graph(ex, self.graph, self.hashes, values, "", only=target_ids)
            except _Ended:
                outputs = None
        except BaseException as exc:
            ex.record["status"] = "failed"
            ex.record["error"] = _error(exc)
            self._finish(ex)
            raise
        if target_ids is None and outputs is not None:
            ex.record["outputs"] = self.store.put_value(outputs)
        statuses = [r["status"] for r in ex.record["nodes"].values()]
        if "failed" in statuses:
            ex.record["status"] = "failed"
        elif ex.stop is not None or "stopped" in statuses:
            ex.record["status"] = "stopped"
        else:
            ex.record["status"] = "done"
        ex.record["stop"] = ex.stop
        self._finish(ex)
        return RunResult(
            run_id=ex.run_id,
            status=ex.record["status"],
            record_path=path,
            outputs=outputs if target_ids is None else None,
            stop=ex.stop,
            nodes={p: r["status"] for p, r in ex.record["nodes"].items()},
            errors={p: r["error"]["message"] for p, r in ex.record["nodes"].items() if r.get("error")},
        )

    def plan(self, inputs: Optional[Mapping[str, Any]] = None, *, target: Iterable[str] = (),
             force: Iterable[str] = ()) -> list[PlanEntry]:
        """A dry run: what a run now would reuse, skip and run. Calls no op and writes nothing."""
        target_ids, force = self._check_selection(target, force)
        values, _ = self.resolve_inputs(inputs)
        ex = _Exec(self, plan=True, force=force)
        try:
            self._file_graph(ex, self.graph, self.hashes, values, "", only=target_ids)
        except _Ended:
            pass
        return list(ex.entries)

    def seed(self, node_path: str, output: Mapping[str, Any], source: Mapping[str, Any], *,
             valid: bool = True, inputs: Optional[Mapping[str, Any]] = None) -> dict:
        """Record an accepted external output for a top-level node (Q39); return its node record.

        The seed's reads are computed from the graph inputs and the newest
        record of each node it reads, so seed in dependency order.
        """
        if node_path not in self.graph.nodes:
            raise GraphError(f"seed {node_path!r}: not a top-level node of {self.graph.id}; "
                             f"nodes: {sorted(self.graph.nodes)}")
        node = self.graph.nodes[node_path]
        if not isinstance(output, Mapping):
            raise GraphError(f"seed {node_path}: output must be a mapping of attribute -> value")
        attributes = self._attributes(node)
        required = {"rounds", "carry"} if node.kind == "repeat" else {"out"}
        unknown = sorted(set(output) - attributes)
        missing = sorted(required - set(output))
        if unknown or missing:
            raise GraphError(f"seed {node_path}: attributes {sorted(attributes)}; "
                             f"unknown {unknown}, missing {missing}")
        output = {name: output.get(name) for name in sorted(attributes)}
        check_json(output, f"seed {node_path} output")
        if not isinstance(source, Mapping):
            raise GraphError(f"seed {node_path}: source must be a JSON object")
        source = dict(source)
        check_json(source, f"seed {node_path} source")
        if valid:
            self._check_seed_output(node, output["out"] if "out" in output else None)
        values, sources = self.resolve_inputs(inputs)
        index = ReuseIndex(self.store)
        scope: dict[str, Any] = {"inputs": values}
        for dep in sorted(self.reads.deps(node, self.graph.nodes)):
            hit = index.latest(dep, statuses=("done", "reused", "seeded", "skipped"))
            if hit is None:
                raise GraphError(f"seed {node_path}: it reads node {dep!r}, which has no record; seed {dep!r} first")
            scope[dep] = self._load_attributes(hit[1])
        reads = reads_document(self.reads.node(node), scope)
        run_id = new_run_id()
        now = utc_now()
        record = self._node_base(node, node_path, self.hashes)
        record.update({
            "status": "seeded",
            "input_hash": input_hash(reads),
            "reads": self.store.put_value(reads),
            "attributes": self._store_attributes(node, output),
            "started_at": now,
            "ended_at": now,
            "seconds": 0.0,
            "seed": {"source": source, "valid": bool(valid)},
        })
        run = {
            "format": RUN_FORMAT,
            "run_id": run_id,
            "kind": "seed",
            "item": self.item,
            "status": "done",
            "started_at": now,
            "ended_at": now,
            "graph": self._graph_head(),
            "graphs": self.structure(),
            "files": self._snapshot_files(),
            "target": None,
            "force": [],
            "inputs": self.store.put_value(values),
            "inputs_from": sources,
            "outputs": None,
            "stop": None,
            "error": None,
            "counts": {"seeded": 1},
            "nodes": {node_path: record},
        }
        self.store.write_run(run)
        return record

    def resolve_inputs(self, given: Optional[Mapping[str, Any]] = None) -> tuple[dict, dict[str, str]]:
        """Graph inputs: given, else the newest recorded value for this item, else the port default."""
        given = dict(given or {})
        unknown = sorted(set(given) - set(self.graph.inputs))
        if unknown:
            raise GraphError(f"graph {self.graph.id} has no inputs {unknown}; inputs: {sorted(self.graph.inputs)}")
        check_json(given, "inputs")
        recorded, recorded_from = self.store.latest_inputs(self.graph.id)
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for name, port in self.graph.inputs.items():
            if name in given:
                values[name] = given[name]
            elif name in recorded:
                values[name] = recorded[name]
                sources[name] = recorded_from[name]
            elif port.has_default:
                values[name] = copy.deepcopy(port.default)
            else:
                raise GraphError(f"graph {self.graph.id} needs input {name!r} (none given or recorded for "
                                 f"item {self.item})")
            _check_port(port, values[name], f"graph input {name!r}")
        return values, sources

    # ------------------------------------------------------------------
    # structure and snapshots
    # ------------------------------------------------------------------

    def _rel(self, path: Path) -> str:
        return PurePosixPath(Path(os.path.relpath(Path(path).resolve(), self.project_root))).as_posix()

    def _graph_head(self) -> dict:
        return {"id": self.graph.id, "file": self._rel(self.graph.path), "hash": self.hashes.graph}

    def structure(self) -> dict:
        """graph hash -> the structure of that graph (the record's ``graphs``)."""
        if self._structure is not None:
            return self._structure
        out: dict[str, dict] = {}

        def walk(graph: Graph, hashes: GraphHashes) -> None:
            if hashes.graph in out:
                return
            nodes = {}
            for node_id, node in graph.nodes.items():
                body = hashes.bodies.get(node_id)
                op = self._node_op(node)
                nodes[node_id] = {
                    "kind": node.kind,
                    "op": op.ref if op is not None else None,
                    "paid": bool(op.paid) if op is not None else False,
                    "deps": sorted(self.reads.deps(node, graph.nodes)),
                    "body": body.graph if body is not None else None,
                    "definition_hash": hashes.nodes[node_id],
                }
            out[hashes.graph] = {
                "id": graph.id,
                "file": self._rel(graph.path),
                "location": graph.location,
                "inputs": list(graph.inputs),
                "outputs": list(graph.out) if graph.inline else list(graph.outputs),
                "nodes": nodes,
            }
            for node_id, node in graph.nodes.items():
                for body in node.bodies():
                    walk(body, hashes.bodies[node_id])

        walk(self.graph, self.hashes)
        self._structure = out
        return out

    def _node_op(self, node: Node) -> Optional[Op]:
        if node.kind == "op":
            return self.registry.get(node.op)
        if node.kind == "commit":
            return self.registry.get(node.commit.adapter)
        return None

    def _snapshot_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        seen: set[int] = set()

        def walk(graph: Graph) -> None:
            if id(graph) in seen:
                return
            seen.add(id(graph))
            if not graph.inline:
                files[self._rel(graph.path)] = self.store.put_blob(Path(graph.path).read_bytes())
            for node in graph.nodes.values():
                if node.kind == "op":
                    op = self.registry.get(node.op)
                    for param in resolve_params(node, op, graph).values():
                        if param.kind in ("file", "template"):
                            files[self._rel(param.path)] = self.store.put_blob(Path(param.path).read_bytes())
                for body in node.bodies():
                    walk(body)

        walk(self.graph)
        return dict(sorted(files.items()))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _check_selection(self, target: Iterable[str], force: Iterable[str]) -> tuple[Optional[set], tuple]:
        target = list(target or ())
        unknown = sorted(t for t in target if t not in self.graph.nodes)
        if unknown:
            raise GraphError(f"target {unknown} not top-level nodes of {self.graph.id}; "
                             f"nodes: {sorted(self.graph.nodes)}")
        target_ids = None
        if target:
            target_ids = set()
            stack = list(target)
            while stack:
                node_id = stack.pop()
                if node_id in target_ids:
                    continue
                target_ids.add(node_id)
                stack.extend(self.reads.deps(self.graph.nodes[node_id], self.graph.nodes))
        force = tuple(force or ())
        for path in force:
            if not isinstance(path, str) or not path or path.startswith("/") or path.endswith("/"):
                raise GraphError(f"force path {path!r} is malformed")
            head = path.split("/", 1)[0].split("[", 1)[0]
            if head not in self.graph.nodes:
                raise GraphError(f"force path {path!r} does not start with a top-level node of {self.graph.id}")
        return target_ids, force

    def _attributes(self, node: Node) -> frozenset[str]:
        if node.kind == "op":
            return self.registry.get(node.op).attributes
        return KIND_ATTRIBUTES[node.kind]

    def _check_seed_output(self, node: Node, out: Any) -> None:
        if node.kind == "graph":
            if not isinstance(out, Mapping) or set(out) != set(node.subgraph.outputs):
                raise GraphError(f"seed {node.id}: out must hold exactly the outputs "
                                 f"{sorted(node.subgraph.outputs)} of {node.subgraph.id}")
            for name, port in node.subgraph.outputs.items():
                _check_port(port, out[name], f"seed {node.id} output {name!r}")
        elif node.kind == "op":
            schema = self._op_out_schema(node, self.registry.get(node.op), self.graph)
            if schema is not None:
                errors = _schema_errors(schema, out)
                if errors:
                    raise GraphError(f"seed {node.id}: out does not match the op's output schema: "
                                     f"{', '.join(errors)}")
        elif node.kind == "human":
            errors = _schema_errors(node.human.schema, out)
            if errors:
                raise GraphError(f"seed {node.id}: out does not match the human schema: {', '.join(errors)}")

    def _op_out_schema(self, node: Node, op: Op, graph: Graph) -> Optional[Mapping]:
        if op.outputs is not None:
            return op.outputs
        if op.output_schema_param is not None:
            param = resolve_params(node, op, graph).get(op.output_schema_param)
            return param.schema if param is not None else None
        return None

    def _eval(self, binding: Binding, scope: Mapping[str, Any]) -> Any:
        if isinstance(binding, Literal):
            return copy.deepcopy(binding.value)
        return evaluate(parse_binding(binding), scope)

    def _load_attributes(self, record: Mapping) -> dict:
        return {name: self.store.get_attribute(ref) for name, ref in record["attributes"].items()}

    def _store_attributes(self, node: Node, attributes: Mapping[str, Any]) -> dict[str, str]:
        out = {}
        for name, value in attributes.items():
            if node.kind == "op" and name != "out":
                out[name] = self.store.put_attribute(value)
            else:
                out[name] = self.store.put_value(value)
        return out

    def _node_base(self, node: Node, path: str, hashes: GraphHashes) -> dict:
        op = self._node_op(node)
        return {
            "path": path,
            "node": node.id,
            "kind": node.kind,
            "graph": hashes.graph,
            "op": op.ref if op is not None else None,
            "status": None,
            "definition_hash": hashes.nodes[node.id],
            "input_hash": None,
            "reads": None,
            "inputs": None,
            "params": None,
            "attributes": {},
            "receipt": None,
            "media": [],
            "started_at": None,
            "ended_at": None,
            "seconds": None,
            "error": None,
            "reused_from": None,
            "seed": None,
        }

    def _keep_partial(self, ex: _Exec, node: Node, record: dict, exc: OpError) -> None:
        """Keep the receipt and the attributes an op made before it failed or halted."""
        if ex.plan:
            return
        if exc.receipt is not None:
            receipt = dict(exc.receipt)
            check_json(receipt, f"node {record['path']} receipt")
            record["receipt"] = receipt
        if exc.attributes:
            check_json(dict(exc.attributes), f"node {record['path']} attributes")
            record["attributes"] = {name: self.store.put_attribute(value)
                                    for name, value in sorted(exc.attributes.items())}

    def _put(self, ex: _Exec, record: dict) -> None:
        if ex.plan:
            return
        with ex.lock:
            if record["path"] in ex.record["nodes"]:
                raise GraphError(f"node path {record['path']} ran twice in run {ex.run_id}")
            ex.record["nodes"][record["path"]] = record
            self.store.write_run(ex.record)

    def _finish(self, ex: _Exec) -> None:
        with ex.lock:
            counts: dict[str, int] = {}
            for record in ex.record["nodes"].values():
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            ex.record["counts"] = dict(sorted(counts.items()))
            ex.record["ended_at"] = utc_now()
            self.store.write_run(ex.record)

    def _entry(self, ex: _Exec, node: Node, path: str, action: str, reason: str = "",
               run: Optional[str] = None) -> None:
        op = self._node_op(node)
        with ex.lock:
            ex.entries.append(PlanEntry(path, node.kind, op.ref if op is not None else None,
                                        bool(op.paid) if op is not None else False, action, reason, run))

    def _unknown_inside(self, ex: _Exec, graph: Graph, prefix: str, reason: str) -> None:
        for node_id, node in graph.nodes.items():
            path = prefix + node_id
            self._entry(ex, node, path, "unknown", reason)
            for body in node.bodies():
                inner = f"{path}[*]/" if node.kind in ("map", "repeat") else f"{path}/"
                self._unknown_inside(ex, body, inner, reason)

    @staticmethod
    def _has_unknown(refs: Iterable[str], scope: Mapping[str, Any]) -> bool:
        return any(scope.get(ref.split(".", 1)[0]) is UNKNOWN for ref in refs)

    # ------------------------------------------------------------------
    # levels
    # ------------------------------------------------------------------

    def _file_graph(self, ex: _Exec, graph: Graph, hashes: GraphHashes, inputs: dict, prefix: str,
                    only: Optional[set] = None) -> Any:
        scope = {"inputs": inputs}
        results = self._level(ex, graph, hashes, scope, prefix, only)
        if only is not None:
            return None
        full = {**scope, **results}
        refs = set()
        for binding in graph.outputs_from.values():
            if isinstance(binding, str):
                refs |= references(parse_binding(binding))
        if ex.plan and self._has_unknown(refs, full):
            return UNKNOWN
        outputs = {}
        for name, binding in graph.outputs_from.items():
            outputs[name] = self._eval(binding, full)
            _check_port(graph.outputs[name], outputs[name], f"graph {graph.id} output {name!r}")
        return outputs

    def _body_out(self, ex: _Exec, body: Graph, scope: Mapping[str, Any]) -> Any:
        refs = set()
        for binding in body.out.values():
            if isinstance(binding, str):
                refs |= references(parse_binding(binding))
        if ex.plan and self._has_unknown(refs, scope):
            return UNKNOWN
        return {name: self._eval(binding, scope) for name, binding in body.out.items()}

    def _level(self, ex: _Exec, graph: Graph, hashes: GraphHashes, base: Mapping[str, Any], prefix: str,
               only: Optional[set] = None) -> dict[str, Any]:
        """Run the nodes of one graph level; return node id -> attributes. Raise _Ended on failure."""
        ids = [n for n in graph.nodes if only is None or n in only]
        deps = {n: sorted(self.reads.deps(graph.nodes[n], ids)) for n in ids}
        results: dict[str, Any] = {}
        ended: dict[str, str] = {}
        lock = threading.Lock()

        def execute(snode: SchedulerNode) -> None:
            node_id = snode.node_id
            node = graph.nodes[node_id]
            path = prefix + node_id
            if ex.stopping.is_set():
                self._mark(ex, node, path, hashes, "pending")
                with lock:
                    ended[node_id] = "pending"
                raise NodeFailed(path)
            with lock:
                scope = {**base, **{d: results[d] for d in deps[node_id]}}
            try:
                attributes = self._node(ex, graph, hashes, node, scope, path)
            except _Ended as end:
                with lock:
                    ended[node_id] = end.status
                raise NodeFailed(path) from None
            with lock:
                results[node_id] = attributes

        scheduler = Scheduler([SchedulerNode(n, tuple(deps[n])) for n in ids],
                              capacity=max(1, min(len(ids), self.max_parallel)))
        outcome = run_graph(scheduler, execute)
        gated = list(outcome.state.gated)
        while gated:
            for node_id in list(gated):
                states = [ended.get(d) for d in deps[node_id]]
                if any(s is None and d in gated for s, d in zip(states, deps[node_id])):
                    continue
                status = "gated" if any(s in ("failed", "gated") for s in states) else "pending"
                ended[node_id] = status
                self._mark(ex, graph.nodes[node_id], prefix + node_id, hashes, status)
                gated.remove(node_id)
        if ended:
            raise _Ended("failed" if any(s in ("failed", "gated") for s in ended.values()) else "stopped")
        return results

    def _mark(self, ex: _Exec, node: Node, path: str, hashes: GraphHashes, status: str) -> None:
        if ex.plan:
            return
        record = self._node_base(node, path, hashes)
        record["status"] = status
        self._put(ex, record)

    # ------------------------------------------------------------------
    # one node
    # ------------------------------------------------------------------

    def _node(self, ex: _Exec, graph: Graph, hashes: GraphHashes, node: Node, scope: Mapping[str, Any],
              path: str) -> Any:
        record = self._node_base(node, path, hashes)
        record["started_at"] = utc_now()
        started = time.monotonic()

        def finish(status: str, **fields: Any) -> None:
            record.update(fields)
            record["status"] = status
            record["ended_at"] = utc_now()
            record["seconds"] = round(time.monotonic() - started, 6)
            self._put(ex, record)

        try:
            refs = self.reads.node(node)
            if ex.plan and self._has_unknown(refs, scope):
                self._entry(ex, node, path, "unknown", "reads a value a node due to run would make")
                for body in node.bodies():
                    inner = f"{path}[*]/" if node.kind in ("map", "repeat") else f"{path}/"
                    self._unknown_inside(ex, body, inner, f"inside {path}")
                return UNKNOWN
            reads = reads_document(refs, scope)
            record["input_hash"] = input_hash(reads)
            if not ex.plan:
                record["reads"] = self.store.put_value(reads)
            if node.when is not None:
                when = self._eval(node.when, scope)
                if not isinstance(when, bool):
                    raise GraphError(f"when gave {type(when).__name__}, not a boolean")
                if not when:
                    attributes = {name: None for name in self._attributes(node)}
                    if ex.plan:
                        self._entry(ex, node, path, "skip")
                    else:
                        finish("skipped", attributes=self._store_attributes(node, attributes))
                    return attributes
            if not forced(path, ex.force):
                hit = ex.index.find(path, record["definition_hash"], record["input_hash"])
                if hit is not None:
                    run_id, old = hit
                    attributes = self._load_attributes(old)
                    if ex.plan:
                        self._entry(ex, node, path, "reuse", run=run_id)
                    else:
                        finish("reused", attributes=dict(old["attributes"]), inputs=old.get("inputs"),
                               params=old.get("params"), media=list(old.get("media") or []),
                               reused_from={"run": run_id, "path": path})
                    return attributes
            if ex.plan:
                reason = "forced" if forced(path, ex.force) else ex.index.reason(
                    path, record["definition_hash"], record["input_hash"])
                self._entry(ex, node, path, "run", reason)
                if node.kind in ("op", "human", "commit"):
                    return UNKNOWN
            body_hashes = hashes.bodies.get(node.id)
            if node.kind == "op":
                attributes = self._run_op(ex, graph, node, scope, path, record)
            elif node.kind == "graph":
                attributes = self._run_graph_node(ex, node, body_hashes, scope, path, record)
            elif node.kind == "map":
                attributes = self._run_map(ex, graph, hashes, node, body_hashes, scope, path)
            elif node.kind == "repeat":
                attributes = self._run_repeat(ex, graph, hashes, node, body_hashes, scope, path)
            elif node.kind == "human":
                attributes = self._run_human(ex, graph, node, scope, path, record)
            else:
                attributes = self._run_commit(ex, graph, node, scope, path, record)
        except _Ended as end:
            inner = "stopped" if end.status == "stopped" else "failed"
            message = ("a node inside it stopped the run" if inner == "stopped"
                       else "a node inside it failed")
            finish(inner, error={"type": "Inner", "message": message})
            raise
        except OpHalted as halt:
            self._keep_partial(ex, node, record, halt)
            with ex.lock:
                if ex.stop is None:
                    ex.stop = {"reason": "halted", "node": path, "message": str(halt), "halt": halt.halt}
            ex.stopping.set()
            finish("stopped", error={**_error(halt), "code": halt.code, "halt": halt.halt})
            raise _Ended("stopped") from None
        except OpError as exc:
            if ex.plan:
                self._entry(ex, node, path, "error", str(exc))
                return UNKNOWN
            self._keep_partial(ex, node, record, exc)
            finish("failed", error={**_error(exc), "code": exc.code})
            raise _Ended("failed") from None
        except NodeStopped as stop:
            with ex.lock:
                if ex.stop is None:
                    ex.stop = {"reason": stop.reason, "node": path, "message": stop.message}
            ex.stopping.set()
            finish("stopped", error=_error(stop))
            raise _Ended("stopped") from None
        except Exception as exc:  # noqa: BLE001 -- a node failure is recorded, then gates its dependents
            if ex.plan:
                self._entry(ex, node, path, "error", str(exc))
                return UNKNOWN
            finish("failed", error=_error(exc))
            raise _Ended("failed") from None
        if ex.plan:
            return attributes
        if attributes is UNKNOWN:  # pragma: no cover - only a dry run makes unknown values
            raise GraphError("internal: unknown value in a run")
        if node.kind != "op":
            record["attributes"] = self._store_attributes(node, attributes)
        finish("done")
        return attributes

    # -- kinds -----------------------------------------------------------

    def _run_op(self, ex: _Exec, graph: Graph, node: Node, scope: Mapping[str, Any], path: str,
                record: dict) -> dict:
        op = self.registry.get(node.op)
        if op.commit:
            raise GraphError(f"{op.name} is a commit adapter; only a commit node may call it")
        inputs = {name: self._eval(binding, scope) for name, binding in node.bind.items()}
        inputs = _with_defaults(op.inputs, inputs, f"op {op.name}")
        params: dict[str, Any] = {}
        shown: dict[str, Any] = {}
        for name, param in resolve_params(node, op, graph).items():
            if param.kind == "expr":
                params[name] = shown[name] = self._eval(param.bindings[""], scope)
            elif param.kind == "exprs":
                params[name] = shown[name] = {k: self._eval(b, scope) for k, b in param.bindings.items()}
            elif param.kind == "schema":
                params[name] = shown[name] = param.schema
            elif param.kind in ("file", "template"):
                params[name] = param.path
                shown[name] = self._rel(param.path)
            else:
                params[name] = shown[name] = copy.deepcopy(param.value)
        record["inputs"] = self.store.put_value(inputs)
        record["params"] = self.store.put_value(shown)
        if op.paid and "spend" not in self.services:
            raise GraphError(f"op {op.name} is paid; a paid op needs the spend service "
                             "(services['spend'], llm_kit.graph.services), which this runner was not given")
        ctx = OpContext(run_id=ex.run_id, item=self.item, graph_id=graph.id, node_path=path,
                        project_root=self.project_root, base_dir=graph.base_dir, params=params,
                        store=self.store, services=self.services)
        result = op.fn(ctx, **copy.deepcopy(inputs))
        if not isinstance(result, OpResult):
            result = OpResult(out=result)
        check_json(result.out, f"op {op.name} out")
        schema = self._op_out_schema(node, op, graph)
        if schema is not None:
            errors = _schema_errors(schema, result.out)
            if errors:
                raise GraphError(f"op {op.name}: out does not match its output schema: {', '.join(errors)}")
        extra = dict(result.attributes)
        declared = set(op.attributes) - {"out"}
        if set(extra) != declared:
            raise GraphError(f"op {op.name} must return the attributes {sorted(declared)}; "
                             f"it returned {sorted(extra)}")
        check_json(extra, f"op {op.name} attributes")
        receipt = None
        if result.receipt is not None:
            receipt = dict(result.receipt)
            check_json(receipt, f"op {op.name} receipt")
        media = []
        for ref in result.media:
            if ref_kind(ref) != "media":
                raise StoreError(f"op {op.name} reported {ref!r} as media")
            self.store.media_path(ref)
            media.append(ref)
        attributes = {"out": result.out, **extra}
        record["attributes"] = self._store_attributes(node, attributes)
        record["receipt"] = receipt
        record["media"] = media
        return attributes

    def _run_graph_node(self, ex: _Exec, node: Node, hashes: GraphHashes, scope: Mapping[str, Any], path: str,
                        record: dict) -> dict:
        sub = node.subgraph
        inputs = {name: self._eval(binding, scope) for name, binding in node.bind.items()}
        inputs = _with_defaults(sub.inputs, inputs, f"graph {sub.id}")
        if not ex.plan:
            record["inputs"] = self.store.put_value(inputs)
        out = self._file_graph(ex, sub, hashes, inputs, path + "/")
        return UNKNOWN if out is UNKNOWN else {"out": out}

    def _run_map(self, ex: _Exec, graph: Graph, hashes: GraphHashes, node: Node, body_hashes: GraphHashes,
                 scope: Mapping[str, Any], path: str) -> Any:
        spec = node.map
        over = self._eval(spec.over, scope)
        if not isinstance(over, list):
            raise GraphError(f"map over gave {type(over).__name__}, not a list")
        keys: list[Union[str, int]] = []
        for index, item in enumerate(over):
            key = self._eval(spec.key, {**scope, "item": item, "index": index})
            if isinstance(key, bool) or not isinstance(key, (str, int)) or key == "":
                raise GraphError(f"map key of item {index} is {key!r}; a key is a non-empty string or an integer")
            if key in keys:
                raise GraphError(f"map key {key!r} is not unique (items {keys.index(key)} and {index})")
            keys.append(key)
        outs: dict[int, Any] = {}
        ended: dict[int, str] = {}
        lock = threading.Lock()

        def execute(snode: SchedulerNode) -> None:
            index = snode.node_id
            item_path = f"{path}[{_encode_key(keys[index])}]"
            if ex.stopping.is_set():
                self._item_record(ex, node, item_path, hashes, "pending", key=keys[index], index=index)
                with lock:
                    ended[index] = "pending"
                raise NodeFailed(item_path)
            try:
                out = self._item(ex, node, body_hashes, {**scope, "item": over[index], "index": index},
                                 item_path, key=keys[index], index=index, hashes=hashes)
            except _Ended as end:
                with lock:
                    ended[index] = end.status
                raise NodeFailed(item_path) from None
            with lock:
                outs[index] = out

        if over:
            scheduler = Scheduler([SchedulerNode(i) for i in range(len(over))],
                                  capacity=max(1, min(len(over), self.max_parallel)))
            run_graph(scheduler, execute)
        if ended:
            raise _Ended("failed" if "failed" in ended.values() else "stopped")
        values = [outs[i] for i in range(len(over))]
        if any(v is UNKNOWN for v in values):
            return UNKNOWN
        return {"out": values}

    def _item(self, ex: _Exec, node: Node, body_hashes: GraphHashes, scope: Mapping[str, Any], path: str, *,
              key: Union[str, int], index: int, hashes: GraphHashes) -> Any:
        spec = node.map
        started = time.monotonic()
        started_at = utc_now()
        try:
            if spec.inline:
                results = self._level(ex, spec.body, body_hashes, scope, path + "/")
                out = self._body_out(ex, spec.body, {**scope, **results})
            else:
                inputs = {name: self._eval(binding, scope) for name, binding in spec.bind.items()}
                inputs = _with_defaults(spec.body.inputs, inputs, f"graph {spec.body.id}")
                out = self._file_graph(ex, spec.body, body_hashes, inputs, path + "/")
        except _Ended as end:
            self._item_record(ex, node, path, hashes, end.status, key=key, index=index, started=started,
                              started_at=started_at, error={"type": "Inner", "message": f"the item {end.status}"})
            raise
        except Exception as exc:  # noqa: BLE001 -- recorded on the item, then fails the map
            if ex.plan:
                raise
            self._item_record(ex, node, path, hashes, "failed", key=key, index=index, started=started,
                              started_at=started_at, error=_error(exc))
            raise _Ended("failed") from None
        if out is not UNKNOWN:
            self._item_record(ex, node, path, hashes, "done", key=key, index=index, started=started,
                              started_at=started_at, out=out)
        return out

    def _item_record(self, ex: _Exec, node: Node, path: str, hashes: GraphHashes, status: str, *,
                     key: Any = None, index: Optional[int] = None, started: Optional[float] = None,
                     started_at: Optional[str] = None, error: Optional[dict] = None, out: Any = None,
                     kind: str = "item", extra: Optional[dict] = None) -> None:
        if ex.plan:
            return
        record = self._node_base(node, path, hashes)
        record.update({"kind": kind, "op": None, "definition_hash": None, "status": status, "error": error,
                       "index": index, "started_at": started_at,
                       "ended_at": utc_now() if started is not None else None,
                       "seconds": round(time.monotonic() - started, 6) if started is not None else None})
        if kind == "item":
            record["key"] = key
        if status == "done":
            record["attributes"] = {"out": self.store.put_value(out)}
        record.update(extra or {})
        self._put(ex, record)

    def _run_repeat(self, ex: _Exec, graph: Graph, hashes: GraphHashes, node: Node, body_hashes: GraphHashes,
                    scope: Mapping[str, Any], path: str) -> Any:
        spec = node.repeat
        maximum = self._eval(spec.max, scope)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise GraphError(f"repeat max gave {maximum!r}; it must be an integer of at least 1")
        carry = {name: self._eval(c.init, scope) for name, c in spec.carry.items()}
        rounds: list[Any] = []
        for index in range(maximum):
            round_path = f"{path}[{index}]"
            if ex.stopping.is_set():
                self._item_record(ex, node, round_path, hashes, "pending", index=index, kind="round")
                raise _Ended("stopped")
            round_scope = {**scope, "carry": carry, "index": index}
            started = time.monotonic()
            started_at = utc_now()
            try:
                if spec.inline:
                    results = self._level(ex, spec.body, body_hashes, round_scope, round_path + "/")
                    out = self._body_out(ex, spec.body, {**round_scope, **results})
                else:
                    inputs = {name: self._eval(binding, round_scope) for name, binding in spec.bind.items()}
                    inputs = _with_defaults(spec.body.inputs, inputs, f"graph {spec.body.id}")
                    out = self._file_graph(ex, spec.body, body_hashes, inputs, round_path + "/")
                if out is UNKNOWN:
                    if index + 1 < maximum:
                        self._unknown_inside(ex, spec.body, f"{path}[{index + 1}+]/", f"later rounds of {path}")
                    return UNKNOWN
                after = {**scope, "carry": carry, "index": index, "round": out}
                until = False
                if spec.until is not None:
                    until = self._eval(spec.until, after)
                    if not isinstance(until, bool):
                        raise GraphError(f"until gave {type(until).__name__}, not a boolean")
                carry = {name: self._eval(c.next, after) for name, c in spec.carry.items()}
            except _Ended as end:
                self._item_record(ex, node, round_path, hashes, end.status, index=index, started=started,
                                  started_at=started_at, kind="round",
                                  error={"type": "Inner", "message": f"the round {end.status}"})
                raise
            except Exception as exc:  # noqa: BLE001 -- recorded on the round, then fails the repeat
                if ex.plan:
                    raise
                self._item_record(ex, node, round_path, hashes, "failed", index=index, started=started,
                                  started_at=started_at, kind="round", error=_error(exc))
                raise _Ended("failed") from None
            rounds.append(out)
            self._item_record(ex, node, round_path, hashes, "done", index=index, started=started,
                              started_at=started_at, kind="round", out=out,
                              extra=None if ex.plan else {"until": until, "carry": self.store.put_value(carry)})
            if until:
                break
        return {"rounds": rounds, "carry": carry}

    def _run_human(self, ex: _Exec, graph: Graph, node: Node, scope: Mapping[str, Any], path: str,
                   record: dict) -> Any:
        """The hook for human-input nodes (unit L05 replaces it). This build stops the run."""
        payload = {name: self._eval(binding, scope) for name, binding in node.human.payload.items()}
        record["inputs"] = self.store.put_value(payload)
        raise NodeStopped("unsupported", f"human node {path} (kind {node.human.kind!r}) needs the durable-wait "
                                         "service (llm_kit.graph.human), which is not in this build")

    def _run_commit(self, ex: _Exec, graph: Graph, node: Node, scope: Mapping[str, Any], path: str,
                    record: dict) -> Any:
        """The hook for commit nodes (unit L05 replaces it). This build stops the run."""
        bundle = node.commit.bundle
        value = ({name: self._eval(b, scope) for name, b in bundle.items()} if isinstance(bundle, Mapping)
                 else self._eval(bundle, scope))
        record["inputs"] = self.store.put_value(value)
        raise NodeStopped("unsupported", f"commit node {path} (adapter {node.commit.adapter}) needs the commit "
                                         "service (llm_kit.graph.commit), which is not in this build")
