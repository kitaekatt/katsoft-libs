"""``llm-kit graph status GRAPH --item ID [options]``: the run state of one work item.

::

    llm-kit graph status GRAPH --item ID [--project DIR] [--input NAME=JSON|@FILE]... [--runs N] [--all] [--json]

Prints the newest runs (``--runs``, default 5: id, kind, status, target,
counts), then one line per top-level node (every node path with ``--all``):
the newest record (status, run id; ``invalid`` for an invalid seed) and what
a run now would do (the dry-run plan). When the plan cannot be made (for
example a required graph input is neither given nor recorded), the plan line
says why.

Exit codes: 0 printed, 1 the graph or config is invalid, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from llm_kit.graph.commands.run import parse_inputs
from llm_kit.graph.model import GraphError
from llm_kit.graph.run import Runner

EXIT_OK = 0
EXIT_FAILED = 1

__all__ = ["run", "status_report"]


def _top(path: str) -> bool:
    return "/" not in path and "[" not in path


def status_report(runner: Runner, inputs: Optional[dict] = None, *, runs: int = 5, all_paths: bool = False) -> dict:
    """The status of the runner's item as a JSON-ready mapping."""
    store = runner.store
    run_list = []
    for run_id in list(reversed(store.run_ids()))[:runs]:
        record = store.load_run(run_id)
        run_list.append({"run_id": run_id, "kind": record["kind"], "status": record["status"],
                         "graph": record["graph"]["id"], "target": record.get("target"),
                         "counts": record.get("counts", {}), "stop": record.get("stop")})
    plan_error = None
    plan = {}
    try:
        for entry in runner.plan(inputs):
            plan.setdefault(entry.path, entry)
    except GraphError as exc:
        plan_error = str(exc)
    latest = store.latest_records()
    paths = list(dict.fromkeys([*plan, *latest])) if all_paths else list(runner.graph.nodes)
    nodes = []
    for path in paths:
        if not all_paths and not _top(path):
            continue
        record = latest.get(path)
        entry = plan.get(path)
        nodes.append({
            "path": path,
            "status": record[1]["status"] if record else None,
            "run": record[0] if record else None,
            "invalid": bool(record and record[1].get("seed") and not record[1]["seed"]["valid"]),
            "next": entry.action if entry else None,
            "reason": entry.reason if entry else None,
            "paid": entry.paid if entry else False,
        })
    return {"item": runner.item, "graph": runner.graph.id, "runs": run_list, "nodes": nodes,
            "plan_error": plan_error}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph status",
                                     description="Show the run state of a graph for one work item.")
    parser.add_argument("graph", metavar="GRAPH", help="a graph name from .llm-kit/graphs.yaml or a graph file path")
    parser.add_argument("--item", required=True, help="the work item id")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--input", action="append", default=[], metavar="NAME=JSON",
                        help="a graph input for the plan, as JSON or NAME=@FILE (repeatable)")
    parser.add_argument("--runs", type=int, default=5, help="how many recent runs to list (default 5)")
    parser.add_argument("--all", action="store_true", help="list every node path, not only top-level nodes")
    parser.add_argument("--json", action="store_true", help="print the status as JSON")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.runs < 0:
        _parser().error("--runs must not be negative")
    try:
        inputs = parse_inputs(args.input)
        runner = Runner.for_project(args.project, args.graph, args.item)
        report = status_report(runner, inputs, runs=args.runs, all_paths=args.all)
    except GraphError as exc:
        print(f"llm-kit graph status: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return EXIT_OK
    print(f"{report['graph']} item {report['item']}")
    if not report["runs"]:
        print("runs: none")
    for entry in report["runs"]:
        counts = ", ".join(f"{k} {v}" for k, v in entry["counts"].items())
        target = f" target {','.join(entry['target'])}" if entry["target"] else ""
        print(f"run {entry['run_id']} {entry['kind']} {entry['status']}{target} ({counts})")
    if report["plan_error"]:
        print(f"plan: not available: {report['plan_error']}")
    for node in report["nodes"]:
        latest = node["status"] or "no record"
        if node["invalid"]:
            latest += " (invalid)"
        if node["run"]:
            latest += f" [run {node['run']}]"
        nxt = ""
        if node["next"]:
            nxt = f"  next: {node['next']}" + (f" ({node['reason']})" if node["reason"] else "")
            if node["paid"]:
                nxt += " [paid]"
        print(f"{node['path']}: {latest}{nxt}")
    return EXIT_OK
