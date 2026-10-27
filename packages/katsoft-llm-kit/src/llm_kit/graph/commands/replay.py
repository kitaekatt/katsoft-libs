"""``llm-kit graph replay GRAPH --item ID --node NODE``: run one node again from its saved inputs.

::

    llm-kit graph replay GRAPH --item ID --node NODE [--run RUN] [--project DIR] [--json]

Rebuilds the graph version that run recorded (the newest run of GRAPH, or
``--run``) from the run's file snapshots and runs top-level node NODE of it
again, in a new run. The node reads the values it read in the source run; its
dependencies are reused, so only NODE calls anything (a paid node is metered
as in ``run``). The source run is not changed. The new record has ``replay``.
See ``llm_kit.graph.replay``.

The live graph file does not have to match; the verb refuses when the recorded
version cannot be rebuilt (a missing snapshot, or ops that changed since).

Output and exit codes are those of ``llm-kit graph run``: 0 done, 1 failed,
refused or not replayable, 2 usage, 3 stopped.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from llm_kit.graph.commands.answer import graph_id
from llm_kit.graph.commands.run import EXIT_FAILED, exit_code, print_result
from llm_kit.graph.commands.validate import load_registry
from llm_kit.graph.config import load_config
from llm_kit.graph.model import GraphError
from llm_kit.graph.records import RunStore
from llm_kit.graph.replay import replay_node
from llm_kit.graph.services import build_services

__all__ = ["run"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph replay",
                                     description="Replay one node from its saved inputs against the recorded graph version.")
    parser.add_argument("graph", metavar="GRAPH", help="a graph name from .llm-kit/graphs.yaml or a graph file path")
    parser.add_argument("--item", required=True, help="the work item id")
    parser.add_argument("--node", required=True, help="the top-level node to replay")
    parser.add_argument("--run", help="the run to replay from (default: the newest run of GRAPH)")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        config = load_config(args.project)
        gid = graph_id(config, args.graph)
        store = RunStore.for_item(config, args.item)
        if args.run is not None:
            source = store.load_run(args.run)
        else:
            source = next((r for r in store.runs(newest_first=True) if r["kind"] == "run"
                           and r["graph"]["id"] == gid), None)
            if source is None:
                raise GraphError(f"no run of {gid} to replay")
        if source["graph"]["id"] != gid:
            raise GraphError(f"run {source['run_id']} is a run of {source['graph']['id']}, not {gid}")
        registry = load_registry(config)
        services = build_services(config, item=args.item, store=store)
        result = replay_node(store, source["run_id"], args.node, registry=registry, project_root=config.root,
                             services=services)
    except GraphError as exc:
        print(f"llm-kit graph replay: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print_result(result, as_json=args.json)
    return exit_code(result)
