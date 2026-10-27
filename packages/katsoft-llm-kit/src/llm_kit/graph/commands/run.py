"""``llm-kit graph run GRAPH --item ID [options]``: run a graph for one work item.

::

    llm-kit graph run GRAPH --item ID [--project DIR] [--target NODE]... [--input NAME=JSON|@FILE]...
                            [--force PATH]... [--dry-run] [--json]

GRAPH is a graph name from ``.llm-kit/graphs.yaml`` or a graph file path.
``--input`` takes a JSON value (``max_rounds=3``, ``guidance='"short"'``) or
``@path`` to a JSON file. ``--target`` limits the run to top-level nodes and
the nodes they read. ``--force`` reruns a node path (and everything above and
below it) instead of reusing its record. ``--dry-run`` prints the plan and
calls nothing; a paid op due to run shows its spend estimate
(``[paid $0.005000]``, or why it has none).

The runtime services (concurrency, spend, waits) come from the project config
(``docs/reference/llm-kit-graph-services.md``).

Prints one line per top-level node (``<status> <path>``), every failed node
with its message, then ``run <id>: <status>`` and the record path. A stopped
run also prints its stop, the top-level nodes done and the nodes remaining.

Exit codes: 0 the run is done (or a dry run printed), 1 a node failed or the
graph, config or inputs are invalid, 2 usage, 3 the run stopped (a node
stopped it cleanly; see the record's ``stop``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from llm_kit.graph.model import GraphError
from llm_kit.graph.run import PlanEntry, Runner, RunResult

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_STOPPED = 3

__all__ = ["exit_code", "format_plan", "parse_inputs", "print_result", "run"]


def parse_inputs(items: Sequence[str]) -> dict:
    """``NAME=JSON`` or ``NAME=@FILE`` arguments as a mapping; raise GraphError on a bad one."""
    out: dict = {}
    for text in items:
        name, sep, value = text.partition("=")
        if not sep or not name:
            raise GraphError(f"--input {text!r}: write NAME=JSON or NAME=@FILE")
        if name in out:
            raise GraphError(f"--input {name!r} is given twice")
        if value.startswith("@"):
            path = Path(value[1:])
            try:
                value = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise GraphError(f"--input {name}: cannot read {path}: {exc}") from None
        try:
            out[name] = json.loads(value)
        except ValueError as exc:
            raise GraphError(f"--input {name}: not JSON ({exc}); quote a string as JSON: "
                             f"{name}='\"text\"'") from None
    return out


def format_plan(entries: Sequence[PlanEntry], *, top_only: bool = False) -> list[str]:
    lines = []
    for entry in entries:
        if top_only and ("/" in entry.path or "[" in entry.path):
            continue
        parts = [f"{entry.action:<7} {entry.path}"]
        if entry.reason:
            parts.append(f"({entry.reason})")
        if entry.run:
            parts.append(f"[run {entry.run}]")
        if entry.paid and entry.estimate_usd is not None:
            parts.append(f"[paid ${entry.estimate_usd:.6f}]")
        elif entry.paid and entry.action == "run" and entry.estimate_note:
            parts.append(f"[paid, no estimate: {entry.estimate_note}]")
        elif entry.paid:
            parts.append("[paid]")
        lines.append(" ".join(parts))
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph run", description="Run a graph for one work item.")
    parser.add_argument("graph", metavar="GRAPH", help="a graph name from .llm-kit/graphs.yaml or a graph file path")
    parser.add_argument("--item", required=True, help="the work item id")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--target", action="append", default=[], metavar="NODE",
                        help="run only this top-level node and the nodes it reads (repeatable)")
    parser.add_argument("--input", action="append", default=[], metavar="NAME=JSON",
                        help="a graph input as JSON, or NAME=@FILE (repeatable)")
    parser.add_argument("--force", action="append", default=[], metavar="PATH",
                        help="rerun this node path instead of reusing its record (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="print what would run; call nothing")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        inputs = parse_inputs(args.input)
        runner = Runner.for_project(args.project, args.graph, args.item)
        if args.dry_run:
            entries = runner.plan(inputs, target=args.target, force=args.force)
            if args.json:
                print(json.dumps([e.__dict__ for e in entries], indent=1, sort_keys=True))
            else:
                for line in format_plan(entries):
                    print(line)
            return EXIT_OK
        result = runner.run(inputs, target=args.target, force=args.force)
    except GraphError as exc:
        print(f"llm-kit graph run: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print_result(result, as_json=args.json)
    return exit_code(result)


def _top(path: str) -> bool:
    return "/" not in path and "[" not in path


def print_result(result: RunResult, *, as_json: bool = False) -> None:
    """Print a run result as the ``run`` and ``resume`` verbs do."""
    if as_json:
        print(json.dumps({"run_id": result.run_id, "status": result.status, "record": str(result.record_path),
                          "stop": result.stop, "nodes": dict(result.nodes), "errors": dict(result.errors),
                          "outputs": result.outputs}, indent=1, sort_keys=True))
        return
    for path, status in result.nodes.items():
        if _top(path):
            print(f"{status:<8} {path}")
    for path, message in result.errors.items():
        if result.nodes[path] == "failed" and message not in ("a node inside it failed", "the item failed",
                                                               "the round failed"):
            print(f"failed   {path}: {message}")
    if result.stop:
        print(f"stopped  {result.stop['node']}: {result.stop['reason']}: {result.stop['message']}")
        done = [p for p, s in result.nodes.items() if _top(p) and s in ("done", "reused", "seeded", "skipped")]
        remaining = [p for p, s in result.nodes.items() if _top(p) and s not in ("done", "reused", "seeded",
                                                                                  "skipped")]
        print(f"done: {', '.join(done) or '-'}")
        print(f"remaining: {', '.join(remaining) or '-'}")
        ledger = result.stop.get("ledger")
        if ledger:
            print(f"ledger: ${ledger['outstanding_usd']:.6f} of ${ledger['cap_usd']:.2f}; "
                  f"${ledger['remaining_usd']:.6f} remaining")
    print(f"run {result.run_id}: {result.status}")
    print(f"record: {result.record_path}")


def exit_code(result: RunResult) -> int:
    if result.status == "done":
        return EXIT_OK
    return EXIT_STOPPED if result.status == "stopped" else EXIT_FAILED
