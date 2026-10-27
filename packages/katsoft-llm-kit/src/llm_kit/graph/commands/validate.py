"""``llm-kit graph validate [--project DIR] [GRAPH ...]``.

Checks each GRAPH (a graph name from ``.llm-kit/graphs.yaml`` or a path to a
graph file); with none, every graph the config names. Prints one line per
problem (``<file>: <location>: <message>``) on stdout, then exits 1; with no
problem it prints ``ok: <n> graph(s)`` and exits 0. A config that cannot be
read is one problem line. Usage errors exit 2. Usable in a pre-push hook.

Op names resolve through ``llm_kit.graph.ops.registry.load_registry(config)``,
which imports the configured namespaces by path and adds llm-kit's own ``llm.*``
operations.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from llm_kit.graph.config import GraphConfig, load_config
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import GraphError, OpRegistry
from llm_kit.graph.validate import validate_graph

EXIT_OK = 0
EXIT_PROBLEMS = 1

__all__ = ["load_registry", "run"]


def load_registry(config: GraphConfig) -> OpRegistry:
    """The op registry for ``config`` (the ops package of llm_kit.graph)."""
    try:
        from llm_kit.graph.ops.registry import load_registry as load  # noqa: PLC0415 -- lazy: a separate unit
    except ModuleNotFoundError as exc:
        if exc.name not in ("llm_kit.graph.ops", "llm_kit.graph.ops.registry"):
            raise
        raise GraphError("the op registry (llm_kit.graph.ops.registry) is not in this build") from None
    return load(config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit graph validate",
                                     description="Statically check graph files.")
    parser.add_argument("graphs", nargs="*", metavar="GRAPH",
                        help="a graph name from .llm-kit/graphs.yaml or a graph file path (default: every graph)")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    return parser


def _targets(config: GraphConfig, names: Sequence[str]) -> list[Path]:
    if not names:
        if not config.graphs:
            raise GraphError(f"{config.path} names no graphs; pass a graph file")
        return list(config.graphs.values())
    out = []
    for name in names:
        if name in config.graphs:
            out.append(config.graphs[name])
        else:
            path = Path(name)
            if not path.is_file():
                raise GraphError(f"{name!r} is neither a graph in {config.path} nor a file")
            out.append(path.resolve())
    return out


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        config = load_config(args.project)
        targets = _targets(config, args.graphs)
        registry = load_registry(config)
    except GraphError as exc:
        print(str(exc))
        return EXIT_PROBLEMS
    lines: list[str] = []
    for path in targets:
        try:
            graph = load_graph(path)
        except GraphError as exc:
            lines.append(str(exc))
            continue
        lines.extend(str(p) for p in validate_graph(graph, registry=registry, project_root=config.root))
    for line in lines:
        print(line)
    if lines:
        return EXIT_PROBLEMS
    print(f"ok: {len(targets)} graph(s)")
    return EXIT_OK
