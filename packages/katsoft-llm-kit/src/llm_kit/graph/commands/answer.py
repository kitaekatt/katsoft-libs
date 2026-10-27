"""``llm-kit graph answer GRAPH --item ID``: list or answer the open human-input waits of an item.

::

    llm-kit graph answer GRAPH --item ID [--project DIR] [--json]                          # list open waits
    llm-kit graph answer GRAPH --item ID --node PATH --value JSON|@FILE [--project DIR] [--json]
    llm-kit graph answer GRAPH --item ID --wait UNIT --value JSON|@FILE      # choose by wait unit id
    llm-kit graph answer GRAPH --item ID --node PATH --run RUN --value JSON  # choose by the run that asked
    llm-kit graph answer GRAPH --item ID --dismiss UNIT [--reason TEXT]       # close a stale wait, no answer

GRAPH is a graph name from ``.llm-kit/graphs.yaml`` or a graph file path.
Without ``--node`` the verb lists the open waits of the graph for the item
(unit id, node path, kind, run, schema, payload). With ``--node`` (or
``--wait``) it validates the value against the wait's schema and records it
for that one wait; ``llm-kit graph resume`` then continues the run. A value
that fails the schema is refused and nothing is recorded.

A new wait for a node path supersedes the older open waits of that path
(``llm_kit.graph.human``), so normally one wait is open. When several are open
(created before that rule), ``--node`` alone answers none: the error lists each
with its unit id, run and payload. Choose one with ``--wait UNIT`` (or
``--run RUN``) or close a stale one with ``--dismiss UNIT``. A dismissed wait is
kept in the store with the reason ``dismissed``.

Exit codes: 0 listed or answered, 1 a refused value, no open wait or a bad
config, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from llm_kit.graph.config import load_config
from llm_kit.graph.human import WaitService
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import GraphError
from llm_kit.graph.records import RunStore

EXIT_OK = 0
EXIT_FAILED = 1

__all__ = ["graph_id", "run"]


def graph_id(config, graph: str) -> str:
    """The id of GRAPH: a configured graph name or a graph file path."""
    if graph in config.graphs:
        return load_graph(config.graphs[graph]).id
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise GraphError(f"{graph!r} is neither a graph in {config.path} nor a file")
    return load_graph(path).id


def _value(text: str):
    if text.startswith("@"):
        path = Path(text[1:])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GraphError(f"--value: cannot read {path}: {exc}") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise GraphError(f"--value is not JSON ({exc}); quote a string as JSON: '\"text\"'") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph answer",
                                     description="List or answer the open human-input waits of an item.")
    parser.add_argument("graph", metavar="GRAPH", help="a graph name from .llm-kit/graphs.yaml or a graph file path")
    parser.add_argument("--item", required=True, help="the work item id")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--node", help="the node path of the wait to answer")
    parser.add_argument("--wait", metavar="UNIT", help="the unit id of the wait to answer (as listed)")
    parser.add_argument("--run", help="answer the wait that this run opened (with --node)")
    parser.add_argument("--dismiss", metavar="UNIT", help="close the open wait UNIT without an answer")
    parser.add_argument("--reason", help="a note kept with --dismiss")
    parser.add_argument("--value", help="the answer as JSON, or @FILE")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    selecting = args.node is not None or args.wait is not None or args.run is not None
    if args.dismiss is not None:
        if selecting or args.value is not None:
            parser.error("--dismiss takes no --node, --wait, --run or --value")
    else:
        if args.reason is not None:
            parser.error("--reason goes with --dismiss")
        if args.value is not None and args.node is None and args.wait is None:
            parser.error("--value needs --node or --wait")
        if args.run is not None and args.node is None and args.wait is None:
            parser.error("--run goes with --node")
        if selecting and args.value is None:
            parser.error("--node, --wait and --run go with --value")
    try:
        config = load_config(args.project)
        gid = graph_id(config, args.graph)
        waits = WaitService.for_item(config, args.item)
        if args.dismiss is not None:
            wait = waits.dismiss(args.item, args.dismiss, graph_id=gid, reason=args.reason,
                                 store=RunStore.for_item(config, args.item))
            if args.json:
                print(json.dumps({"dismissed": wait.to_json()}, indent=1, sort_keys=True))
            else:
                print(f"dismissed {wait.unit} (node {wait.node}, run {wait.run}); it stays recorded")
            return EXIT_OK
        if args.value is None:
            found = waits.open_waits(args.item, graph_id=gid, store=RunStore.for_item(config, args.item))
            if args.json:
                print(json.dumps([w.to_json() for w in found], indent=1, sort_keys=True))
            else:
                for wait in found:
                    print(f"waiting  {wait.node} (kind {wait.kind}, run {wait.run})")
                    print(f"  unit:    {wait.unit}")
                    print(f"  schema:  {json.dumps(dict(wait.schema), sort_keys=True)}")
                    print(f"  payload: {json.dumps(wait.payload, sort_keys=True)[:2000]}")
                if not found:
                    print(f"no open waits for item {args.item} of {gid}")
            return EXIT_OK
        value = _value(args.value)
        wait = waits.answer(args.item, args.node, value, graph_id=gid, wait=args.wait, run=args.run,
                            store=RunStore.for_item(config, args.item))
    except GraphError as exc:
        print(f"llm-kit graph answer: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"answered": wait.to_json(), "value": value}, indent=1, sort_keys=True))
    else:
        print(f"answered {wait.node} (unit {wait.unit}, run {wait.run}); continue with: llm-kit graph resume "
              f"{args.graph} --item {args.item} --wait {wait.unit}")
    return EXIT_OK
