"""``llm-kit runs``: create a run store, import run records, list and show them, rebuild the index.

Verbs (``llm_kit.runs``; the contract is ``docs/reference/llm-kit-runs.md``):

``init``     create a store: ``--store DIR --accepts synthetic,internal``.
``import``   import a ``llm-kit.run-import/v1`` JSON Lines file as one batch;
             ``--check`` runs every check and writes nothing.
``list``     index rows (one JSON object per line), filtered by ``--flow``,
             ``--group``, ``--model``, ``--status``, ``--case``, ``--since``,
             ``--until``; ``--limit N`` keeps the newest N.
``show``     one record as JSON; ``--with-blobs`` prints it as an import line
             with its blobs, ready for another store's ``import``.
``reindex``  delete the derived index and rebuild it from the events.

The store is ``--store DIR``, else the ``LLM_KIT_RUN_STORE`` variable; with
neither the verb refuses. Exit codes: 0 success, 1 the store refused (a JSON
``error`` object ``{kind: "runs", code, message}`` on stderr; nothing was
written), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from llm_kit.runs.record import CONTENT_CLASSES, RECORD_STATUSES
from llm_kit.runs.store import RunRecordStore, RunStoreError, import_file, resolve_store_path

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

#: verb -> one-line help (also the complete, ordered verb list).
VERBS = {
    "init": "Create a run store with a content-class policy.",
    "import": "Import a run import file (JSON Lines) as one batch.",
    "list": "List index rows that match the filters.",
    "show": "Show one record (optionally with its blobs).",
    "reindex": "Rebuild the derived index from the record events.",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-kit runs", description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="verb", required=True, metavar="verb")
    parsers = {name: sub.add_parser(name, help=text, description=text) for name, text in VERBS.items()}
    for verb_parser in parsers.values():
        verb_parser.add_argument("--store", help="the run store directory (default: $LLM_KIT_RUN_STORE)")
    parsers["init"].add_argument("--accepts", required=True,
                                 help=f"comma-separated content classes from {', '.join(CONTENT_CLASSES)}")
    parsers["import"].add_argument("file", help="the llm-kit.run-import/v1 JSON Lines file")
    parsers["import"].add_argument("--check", action="store_true", help="check only; write nothing")
    lister = parsers["list"]
    for name in ("flow", "group", "model", "case"):
        lister.add_argument(f"--{name}")
    lister.add_argument("--status", choices=RECORD_STATUSES)
    lister.add_argument("--since", help="started at or after this date or time prefix")
    lister.add_argument("--until", help="started before this date or time prefix")
    lister.add_argument("--limit", type=int, help="keep the newest N rows")
    parsers["show"].add_argument("id", help="the record id")
    parsers["show"].add_argument("--with-blobs", action="store_true",
                                 help="print an llm-kit.run-import/v1 line with the record's blobs")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=True))


def _dispatch(args: argparse.Namespace) -> int:
    path = resolve_store_path(args.store)
    if args.verb == "init":
        store = RunRecordStore.init(path, accepts=[c.strip() for c in args.accepts.split(",") if c.strip()])
        _print({"store": str(store.root), "accepts": sorted(store.accepts)})
        return EXIT_OK
    store = RunRecordStore.open(path)
    if args.verb == "import":
        _print({"checked_only": args.check, **import_file(store, args.file, check=args.check).to_dict()})
    elif args.verb == "list":
        for row in store.index().query(flow=args.flow, group=args.group, model=args.model, status=args.status,
                                       case=args.case, since=args.since, until=args.until, limit=args.limit):
            _print(row)
    elif args.verb == "show":
        _print(store.export_line(args.id) if args.with_blobs else store.get(args.id))
    elif args.verb == "reindex":
        _print(store.index().rebuild())
    else:
        raise AssertionError(f"unregistered verb: {args.verb}")
    return EXIT_OK


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Run one ``llm-kit runs`` verb with ``argv`` (default ``sys.argv[1:]``); return the exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(raw)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    try:
        return _dispatch(args)
    except RunStoreError as exc:
        print(json.dumps({"error": {"kind": "runs", "code": exc.code, "message": str(exc)}}, sort_keys=True),
              file=sys.stderr)
        return EXIT_USAGE if exc.code == "no-store" else EXIT_REFUSED


__all__ = ["VERBS", "run"]
