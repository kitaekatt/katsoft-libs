"""``llm-kit graph render [--project DIR] [GRAPH ...] [-o FILE]``.

Writes one self-contained HTML page that draws graphs
(:mod:`llm_kit.graph.render`). The page needs no server and makes no network
requests.

* No ``GRAPH``: every graph in the project config (``.llm-kit/graphs.yaml``),
  with a graph browser listing them and every graph file they run. The default
  output is ``graphs.html`` in the current directory.
* One or more ``GRAPH``: each is a config graph name or a path to a graph file
  (a path needs no project config). The page holds those graphs and the files
  they run; with one graph that runs no other file, it opens straight on that
  graph. The default output is ``<name>.html``.

Prints the path it wrote. Exit codes: 0 written, 1 a graph that does not load
or a file that cannot be written, 2 usage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from llm_kit.graph.config import CONFIG_FILE, load_config
from llm_kit.graph.model import GraphError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

__all__ = ["run"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit graph render",
        description="Write one self-contained HTML page that draws graphs.")
    parser.add_argument("graphs", nargs="*", metavar="GRAPH",
                        help="a config graph name or a graph file path (default: every graph in the config)")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("-o", "--output", help="the HTML file to write")
    parser.add_argument("--title", help="the page title (default: '<project> graphs')")
    return parser


def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in text) or "graph"


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    from llm_kit.graph.render import build_catalog, project_catalog, write_page  # noqa: PLC0415

    root = Path(args.project).resolve()
    try:
        has_config = (root / CONFIG_FILE).is_file()
        config = load_config(root) if has_config else None
        if not args.graphs:
            if config is None:
                print(f"llm-kit graph render: {root / CONFIG_FILE} does not exist; name a graph file to render",
                      file=sys.stderr)
                return EXIT_USAGE
            catalog = project_catalog(config)
            default_name = "graphs.html"
        else:
            chosen = {}
            for item in args.graphs:
                if config is not None and item in config.graphs:
                    chosen[item] = config.graph_path(item)
                    continue
                path = Path(item)
                if not path.is_file():
                    known = f"; config graphs: {sorted(config.graphs)}" if config is not None else ""
                    print(f"llm-kit graph render: {item!r} is neither a config graph nor a file{known}",
                          file=sys.stderr)
                    return EXIT_USAGE
                chosen[item] = path
            catalog = build_catalog(chosen, root=root)
            for entry, item in zip(catalog["entries"], chosen):
                if entry["name"] == item and Path(item).is_file():
                    entry["name"] = catalog["files"][entry["file"]]["id"]
            default_name = _safe_name(catalog["entries"][0]["name"]) + ".html" if len(chosen) == 1 else "graphs.html"
        written = write_page(catalog, Path(args.output or default_name), title=args.title)
    except (GraphError, OSError, ValueError) as exc:
        print(f"llm-kit graph render: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(written)
    return EXIT_OK
