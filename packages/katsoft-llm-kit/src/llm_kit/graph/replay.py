"""Replay one node of a recorded run against the graph version that run recorded.

``replay_node`` rebuilds the graph a run recorded from the run's file
snapshots (``files`` in the record; the graph hash must come out the same),
then runs just one top-level node of it again in a new run. The node reads the
values it read in the source run: its dependencies are reused from the stored
records, so the replay calls nothing but the replayed node. The source run
record and every other record stay as they are.

A replay stops with a :class:`~llm_kit.graph.model.GraphError` (nothing is
called) when:

* the source run is a seed, or has no record of the node, or the node never
  ran (``pending``, ``gated``, ``skipped``, ``seeded``) or is not a top-level
  node;
* a file of the recorded version is missing from the snapshots or the blob
  store, or the rebuilt graph hash differs from the recorded one (an op
  changed its version, or its signature, since the run);
* a dependency of the node has no reusable record, or the stored record
  that would be reused differs from the one the source run read.

The new record is an ordinary run record, ``target`` the node and ``force``
the node, with ``replay`` naming the source run, the node and the graph hash.
Its snapshot paths sit under a scratch directory (``.llm-kit/replay-*``)
inside the project root, which is removed when the replay ends. Spend,
concurrency and waits come from ``services``, as in any run.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .loader import load_graph
from .model import GraphError, OpRegistry
from .records import RunStore, resolve_record_path
from .run import Runner, RunResult

__all__ = ["replay_node"]

#: Node statuses whose record holds the inputs the node ran with.
REPLAYABLE = frozenset({"done", "reused", "failed", "stopped", "refused"})


def _top(path: str) -> bool:
    return "/" not in path and "[" not in path


def _materialize(store: RunStore, record: Mapping[str, Any], scratch: Path) -> Path:
    """Write the run's recorded files under ``scratch``; return the path of the root graph file."""
    files = record.get("files")
    if not isinstance(files, Mapping) or not files:
        raise GraphError(f"run {record['run_id']} has no file snapshots; its graph version cannot be rebuilt")
    for rel, ref in files.items():
        target = resolve_record_path(scratch, rel)
        try:
            data = store.get_blob(ref)
        except GraphError as exc:
            raise GraphError(f"run {record['run_id']}: the recorded version of {rel} is not available: {exc}") from None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    graph_file = record["graph"]["file"]
    if graph_file not in files:
        raise GraphError(f"run {record['run_id']}: the recorded graph file {graph_file} is not in its file snapshots")
    return resolve_record_path(scratch, graph_file)


def _check_replayable(record: Mapping[str, Any], run_id: str, node: str) -> None:
    if record["kind"] != "run":
        raise GraphError(f"run {run_id} is a {record['kind']}; only a run replays")
    if not _top(node):
        raise GraphError(f"{node!r} is not a top-level node; replay takes a top-level node")
    node_record = record["nodes"].get(node)
    if node_record is None:
        raise GraphError(f"run {run_id} has no record of node {node!r}; nodes: {sorted(p for p in record['nodes'] if _top(p))}")
    if node_record["status"] not in REPLAYABLE:
        raise GraphError(f"node {node!r} was {node_record['status']} in run {run_id}; it has no saved inputs to replay")


def _check_inputs_match(store: RunStore, record: Mapping[str, Any], node: str, plan: list) -> None:
    """Refuse a plan that would run another node or read a different dependency record than the source run did."""
    for entry in plan:
        if not _top(entry.path) or entry.path == node:
            continue
        if entry.action == "skip":
            continue
        if entry.action != "reuse":
            raise GraphError(f"node {entry.path!r}, which {node!r} reads, has no reusable record "
                             f"({entry.action}{': ' + entry.reason if entry.reason else ''}); "
                             "a replay runs only the one node")
        source = record["nodes"].get(entry.path)
        reused = store.load_run(entry.run)["nodes"][entry.path]
        if source is None or source["attributes"] != reused["attributes"]:
            raise GraphError(f"node {entry.path!r}, which {node!r} reads, differs between run {record['run_id']} and "
                             f"the stored record in run {entry.run}; the saved inputs cannot be rebuilt")


def replay_node(store: RunStore, run_id: str, node: str, *, registry: OpRegistry, project_root: Path | str,
                services: Optional[Mapping[str, Any]] = None, max_parallel: int = 8) -> RunResult:
    """Run top-level ``node`` again from run ``run_id``'s saved inputs, in a new run.

    ``registry`` and ``services`` are the live ones (ops run as they are now; their
    ``name@version`` must match the recorded graph, else this raises). Returns the new run's result.
    Raises :class:`GraphError` when the recorded version is not available or the node cannot be
    replayed (see the module doc). Never writes to ``run_id``.
    """
    root = Path(project_root).resolve()
    record = store.load_run(run_id)
    _check_replayable(record, run_id, node)
    parent = root / ".llm-kit"
    parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="replay-", dir=parent))
    try:
        graph = load_graph(_materialize(store, record, scratch))
        if node not in graph.nodes:
            raise GraphError(f"node {node!r} is not a top-level node of the recorded graph {graph.id}")
        runner = Runner(graph, registry, store, item=record["item"], project_root=root, services=services,
                        max_parallel=max_parallel)
        recorded = record["graph"]["hash"]
        if runner.hashes.graph != recorded:
            raise GraphError(f"run {run_id}: the graph rebuilt from its snapshots hashes to {runner.hashes.graph}, "
                             f"not the recorded {recorded}; the ops it calls changed since the run, so that "
                             "version is not available")
        inputs = store.get_value(record["inputs"])
        _check_inputs_match(store, record, node, runner.plan(inputs, target=[node], force=[node]))
        return runner.run(inputs, target=[node], force=[node],
                          replay={"run": run_id, "node": node, "graph": recorded, "graph_file": record["graph"]["file"]})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
