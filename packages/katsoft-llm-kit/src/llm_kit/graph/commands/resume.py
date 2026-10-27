"""``llm-kit graph resume GRAPH --item ID``: continue a stopped run.

::

    llm-kit graph resume GRAPH --item ID [--run RUN] [--project DIR] [--json]

A run stops when a human node waits for an answer, an op halts (a usage
limit; ``services.pause_on_halt``) or the spend service refuses a call.
``resume`` runs the graph again with the stopped run's inputs and target
(the newest stopped run of GRAPH, or ``--run``). Every node that finished is
reused from its record; a human node with a recorded answer takes it. A
forced path of the stopped run stays forced until a run finishes it. The new
record names the run it continues (``resumed_from``).

A run whose human node still waits is refused: answer it first
(``llm-kit graph answer``).

Output and exit codes are those of ``llm-kit graph run``: 0 done, 1 failed or
refused, 2 usage, 3 stopped again.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from llm_kit.graph.commands.answer import graph_id
from llm_kit.graph.commands.run import EXIT_FAILED, exit_code, print_result
from llm_kit.graph.config import load_config
from llm_kit.graph.model import GraphError
from llm_kit.graph.run import Runner

__all__ = ["resume_plan", "run"]


def resume_plan(store, gid: str, run_id: Optional[str]) -> tuple[dict, list]:
    """The stopped run to continue (``run_id`` or the newest stopped run of ``gid``) and its
    remaining forced paths."""
    if run_id is not None:
        record = store.load_run(run_id)
        if record["graph"]["id"] != gid:
            raise GraphError(f"run {run_id} is a run of {record['graph']['id']}, not {gid}")
    else:
        record = next((r for r in store.runs(newest_first=True)
                       if r["kind"] == "run" and r["graph"]["id"] == gid), None)
        if record is None:
            raise GraphError(f"no run of {gid} to resume")
    if record["status"] != "stopped":
        raise GraphError(f"run {record['run_id']} is {record['status']}; only a stopped run resumes")
    force = [p for p in record.get("force") or [] if (record["nodes"].get(p) or {}).get("status") != "done"]
    return record, force


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph resume", description="Continue a stopped run.")
    parser.add_argument("graph", metavar="GRAPH", help="a graph name from .llm-kit/graphs.yaml or a graph file path")
    parser.add_argument("--item", required=True, help="the work item id")
    parser.add_argument("--run", help="the stopped run to continue (default: the newest run of GRAPH)")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        config = load_config(args.project)
        runner = Runner.for_project(args.project, args.graph, args.item, config=config)
        gid = graph_id(config, args.graph)
        record, force = resume_plan(runner.store, gid, args.run)
        stop = record.get("stop") or {}
        waits = runner.services.get("waits")
        if stop.get("reason") == "waiting" and waits is not None:
            still = [w for w in waits.open_waits(args.item, graph_id=gid) if w.node == stop.get("node")]
            if still:
                raise GraphError(f"node {stop['node']} still waits for an answer; answer it first: "
                                 f"llm-kit graph answer {args.graph} --item {args.item} --node {stop['node']} "
                                 "--value JSON")
        inputs = runner.store.get_value(record["inputs"])
        result = runner.run(inputs, target=record.get("target") or (), force=force,
                            resumed_from=record["run_id"])
    except GraphError as exc:
        print(f"llm-kit graph resume: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print_result(result, as_json=args.json)
    return exit_code(result)
