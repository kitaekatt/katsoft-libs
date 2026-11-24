"""``llm-kit graph render [--project DIR] [GRAPH ...] [-o FILE] [--title TEXT] [--no-labels] [--relabel] [--label-model IDS] [--label-jobs N] [--label-batch N] [--label-cache DIR]``.

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

Labels. By default a model reads the graph files (and the first lines of each
prompt file) and writes a title and description for every node, a text for
every edge and a gloss for every input and output; the page draws them marked
as inferred, with a Labels toggle. ``--no-labels`` skips them (no model call, no
cache access) and writes the plain ``llm-kit.graph-catalog/v1`` page. The
labels come from :func:`llm_kit.graph.render.label_model.model_labeller` (asks
``haiku``, or the registry entries of ``--label-model`` in order, under the
user's consent scope ``graph-labels``) through
:func:`llm_kit.graph.render.labels.label_catalog`: one call per graph file
(``--label-batch N`` splits a file into calls of at most N nodes), ``--label-jobs
N`` calls at a time (default 4). Answers are cached by what the model sees, in
``$XDG_CACHE_HOME/llm-kit/graph-labels`` (else ``~/.cache/llm-kit/graph-labels``;
``--label-cache DIR`` names another), so a warm render makes no call and needs no
consent; ``--relabel`` ignores cached answers and writes new ones. Progress lines
go to standard error; standard output holds only the written path.

If the labels cannot be made (consent is off, no model is usable, an answer is
unusable, the cache is damaged) the command writes no page, leaves any existing
output file as it was, and says how to fix it (the consent command, or
``--no-labels``).

Exit codes: 0 written, 1 a graph that does not load or a file that cannot be
written, 2 usage (``--no-labels`` with another label flag included), 3 the
labels could not be made.

:func:`run` takes an optional ``labeller`` (a
:class:`~llm_kit.graph.render.label_types.Labeller`) that replaces the model
labeller; tests and callers that bring their own labeller use it.
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
EXIT_LABELS = 3

_PREFIX = "llm-kit graph render: "

#: The label flags that mean nothing with ``--no-labels``: (attribute, flag).
_LABEL_FLAGS = (("relabel", "--relabel"), ("label_model", "--label-model"), ("label_jobs", "--label-jobs"),
                ("label_batch", "--label-batch"), ("label_cache", "--label-cache"))

__all__ = ["run"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit graph render",
        description="Write one self-contained HTML page that draws graphs, with labels a model infers "
                    "from the graph files (use --no-labels to skip them).")
    parser.add_argument("graphs", nargs="*", metavar="GRAPH",
                        help="a config graph name or a graph file path (default: every graph in the config)")
    parser.add_argument("--project", default=".", help="the project root (default: the current directory)")
    parser.add_argument("-o", "--output", help="the HTML file to write")
    parser.add_argument("--title", help="the page title (default: '<project> graphs')")
    parser.add_argument("--no-labels", action="store_true",
                        help="write the plain page: no model call and no label cache")
    parser.add_argument("--relabel", action="store_true", default=None,
                        help="ignore cached labels and ask the model again (the answers are cached anew)")
    parser.add_argument("--label-model", metavar="IDS",
                        help="registry entry ids that write the labels, comma separated, in preference order "
                             "(default: haiku)")
    parser.add_argument("--label-jobs", metavar="N",
                        help="label calls made at the same time (default: 4)")
    parser.add_argument("--label-batch", metavar="N",
                        help="split a graph file into label calls of at most N nodes (default: one call per file)")
    parser.add_argument("--label-cache", metavar="DIR",
                        help="the label cache directory (default: $XDG_CACHE_HOME/llm-kit/graph-labels, "
                             "else ~/.cache/llm-kit/graph-labels)")
    return parser


def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in text) or "graph"


def _positive(flag: str, text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise ValueError(f"{flag} takes a whole number of at least 1, got {text!r}")
    return number


def _usage(message: str) -> int:
    print(_PREFIX + message, file=sys.stderr)
    return EXIT_USAGE


def _progress(line: str) -> None:
    print(_PREFIX + line, file=sys.stderr)


def run(argv: Optional[Sequence[str]] = None, *, labeller=None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    from llm_kit.graph.render import build_catalog, project_catalog, write_page  # noqa: PLC0415

    jobs = batch = None
    if args.no_labels:
        for attribute, flag in _LABEL_FLAGS:
            if getattr(args, attribute) is not None:
                return _usage(f"{flag} has no meaning with --no-labels")
    else:
        try:
            jobs = _positive("--label-jobs", args.label_jobs) if args.label_jobs is not None else None
            batch = _positive("--label-batch", args.label_batch) if args.label_batch is not None else None
        except ValueError as exc:
            return _usage(str(exc))

    root = Path(args.project).resolve()
    if not args.no_labels and labeller is None:
        from llm_kit.graph.render import label_model  # noqa: PLC0415

        declaration = (tuple(part.strip() for part in args.label_model.split(","))
                       if args.label_model is not None else label_model.DEFAULT_LABEL_MODELS)
        try:
            labeller = label_model.model_labeller(declaration, project_root=root)
        except ValueError as exc:
            return _usage(f"--label-model: {exc}")

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
        if not args.no_labels:
            from llm_kit.graph.render.label_types import LabelError  # noqa: PLC0415
            from llm_kit.graph.render.labels import (  # noqa: PLC0415
                DEFAULT_JOBS,
                LabelCache,
                default_cache_dir,
                label_catalog,
            )

            try:
                catalog = label_catalog(
                    catalog, labeller, root=root,
                    namespaces=config.namespaces if config is not None else None,
                    cache=LabelCache(Path(args.label_cache) if args.label_cache else default_cache_dir()),
                    jobs=jobs or DEFAULT_JOBS, nodes_per_call=batch, refresh=bool(args.relabel),
                    progress=_progress)
            except LabelError as exc:
                message = str(exc)
                if "--no-labels" not in message:
                    message += "; render with --no-labels to skip the labels"
                print(_PREFIX + message, file=sys.stderr)
                return EXIT_LABELS
        written = write_page(catalog, Path(args.output or default_name), title=args.title)
    except (GraphError, OSError, ValueError) as exc:
        print(f"llm-kit graph render: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(written)
    return EXIT_OK
