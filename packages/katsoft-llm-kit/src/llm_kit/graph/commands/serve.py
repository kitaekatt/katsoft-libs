"""``llm-kit graph serve [--project DIR] [--port N]``.

Serves the read-only run and graph inspection viewer on ``127.0.0.1`` and
prints its address. The viewer lists the project's graphs and the runs on disk;
it starts nothing and writes nothing. ``--port 0`` (the default) picks a free
port. Ctrl-C stops it. Usage errors exit 2; a project the viewer cannot serve
(no config, a missing panel or theme file, a busy port) prints one line and
exits 1.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from llm_kit.graph.config import load_config
from llm_kit.graph.model import GraphError

EXIT_OK = 0
EXIT_FAILED = 1

__all__ = ["run"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph serve",
                                     description="Serve the read-only run and graph inspection viewer on loopback.")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("--port", type=int, default=0,
                        help="the TCP port on 127.0.0.1 (default: 0, a free port)")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    from llm_kit.graph.viewer.server import serve  # noqa: PLC0415 -- the server loads only when served

    try:
        serve(load_config(args.project), args.port)
    except (GraphError, OSError) as exc:
        print(f"llm-kit graph serve: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK
