"""``llm-kit graph``: validate, run and inspect generation graphs (``llm_kit.graph``).

Each verb lives in its own module, ``llm_kit.graph.commands.<verb>``, with
``run(argv) -> int``. :data:`VERBS` is the complete, ordered verb table. A verb
whose module is not in this build exits 2 with a message that names the
missing module; nothing else is tried in its place.

Exit codes: 0 success, 1 the verb found problems (``validate``), 2 usage.
Each verb module documents the codes it adds.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
from typing import Optional, Sequence

EXIT_USAGE = 2

#: verb -> one-line help. The module of a verb is ``llm_kit.graph.commands.<verb>``.
VERBS = {
    "validate": "Statically check graph files (references, ops, slots, schemas, cycles).",
    "run": "Run a graph for one work item.",
    "status": "Show the run state of a graph for one work item.",
    "answer": "Answer an open human-input node.",
    "resume": "Resume a halted or waiting run.",
    "serve": "Serve the read-only run and graph inspection viewer.",
    "replay": "Replay one node from its saved inputs against the recorded graph version.",
}

COMMANDS_PACKAGE = "llm_kit.graph.commands"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph", description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="verb", required=True, metavar="verb")
    for name, text in VERBS.items():
        sub.add_parser(name, help=text, add_help=False)
    return parser


def verb_module(verb: str) -> str:
    """The dotted module of ``verb``."""
    if verb not in VERBS:
        raise KeyError(verb)
    return f"{COMMANDS_PACKAGE}.{verb}"


def run(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in VERBS:
        module = verb_module(raw[0])
        if importlib.util.find_spec(module) is None:
            print(f"llm-kit graph: '{raw[0]}' is not in this build ({module} does not exist)", file=sys.stderr)
            return EXIT_USAGE
        return importlib.import_module(module).run(raw[1:])
    _parser().parse_args(raw)
    return EXIT_USAGE


__all__ = ["COMMANDS_PACKAGE", "VERBS", "run", "verb_module"]
