"""``llm-kit runs compare``: the composition of :mod:`llm_kit.runs.compare` with the project model file.

The comparison logic is :mod:`llm_kit.runs.compare`; this module only composes
it (the CLI is the composition root, :mod:`llm_kit.cli._compose`):

- each ``--select`` key resolves through the project model file and its
  personal layer (:func:`llm_kit.registry.project_models.read_resolved_models`)
  to a :class:`~llm_kit.runs.compare.Candidate`: the catalogue row, endpoint,
  wire model, effort, output cap and request extras that
  ``llm_kit.dispatch.invoke`` will send;
- its price: 0 for an endpoint the registry classifies ``local``
  (:func:`llm_kit.registry.cost.classify`), else the entry for its wire model
  in the ``--pricing`` table (the pricing table format of
  ``llm_kit.pipeline.runtime.platform.load_pricing``), else none (the plan
  refuses it);
- the caller sends one protocol 2 request through
  :func:`llm_kit.dispatch.invoke.invoke` with ``max_retries: 0``, so a
  transport call is sent once.
"""
from __future__ import annotations

import argparse
import json
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Optional

from llm_kit.runs.compare import (
    CallResult,
    Candidate,
    CompareError,
    Price,
    command_scorer,
    plan_compare,
    read_cases,
    run_compare,
)
from llm_kit.runs.record import CONTENT_CLASSES
from llm_kit.runs.store import RunRecordStore

TOOL_NAME = "llm-kit runs compare"

# The registry, dispatch and pricing modules are imported where they are used, so the other
# ``llm-kit runs`` verbs, which build this verb's parser, do not load them.


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``compare`` verb's arguments (``--scorer`` takes every word after it)."""
    parser.add_argument("--prompt", required=True, help="the prompt file (llm-kit.prompt-file/v1) under the project")
    parser.add_argument("--cases", required=True, help="the case file (JSON Lines, llm-kit.compare-case/v1)")
    parser.add_argument("--select", action="append", required=True, metavar="KEYS",
                        help="selection keys of the project model file; repeat or comma-separate (two or more)")
    parser.add_argument("--flow", required=True, help="the flow name of every record (part of the reuse key)")
    parser.add_argument("--content", required=True, choices=CONTENT_CLASSES,
                        help="the content class of every record")
    parser.add_argument("--max-usd", required=True, type=float,
                        help="the budget of the comparison group in USD, failed and pending attempts included")
    parser.add_argument("--pricing", help="a pricing table (YAML or JSON): wire model -> "
                        "{input, output, cache_hit, context_window}, USD per 1M tokens")
    parser.add_argument("--max-output-tokens", type=int, help="the output cap of every transport call")
    parser.add_argument("--timeout-s", type=float, help="the wall-clock cap of each call in seconds")
    parser.add_argument("--group", help="the group id (default: derived from the flow, prompt, cases and selections)")
    parser.add_argument("--label", help="the group label")
    parser.add_argument("--project-root", help="the project root (default: the current directory)")
    parser.add_argument("--models-file", help="the project model file under the root (default .llm-kit/models.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and the cost bound; call nothing")
    parser.add_argument("--scorer-name", help="the scorer name (default: its first file, else its program)")
    parser.add_argument("--scorer-version", help="the scorer version text, recorded in provenance.scorer")
    parser.add_argument("--scorer", nargs=argparse.REMAINDER, metavar="COMMAND",
                        help="the scorer command: every word after --scorer (it reads and writes one "
                             "llm-kit.run-import/v1 line); give it last")


def _package_version() -> Optional[str]:
    try:
        return metadata.version("katsoft-llm-kit")
    except metadata.PackageNotFoundError:  # a source tree on PYTHONPATH has no distribution metadata
        return None


def _pricing(path: Optional[str]) -> dict[str, Price]:
    if path is None:
        return {}
    from llm_kit.pipeline.runtime.platform import load_pricing  # noqa: PLC0415

    table = load_pricing(path)
    if not isinstance(table, dict):
        raise CompareError("invalid", f"the pricing table {path} is not a mapping of model -> price")
    return {str(model): Price.from_entry(entry) for model, entry in table.items()}


def candidates(
    root: Path,
    *,
    file: Optional[str],
    selections: list[str],
    pricing: dict[str, Price],
    max_output_tokens: Optional[int],
) -> list[Candidate]:
    """The :class:`~llm_kit.runs.compare.Candidate` of each selection key, as ``invoke`` will send it.

    Raises :class:`~llm_kit.runs.compare.CompareError` ``invalid`` for an
    unreadable model file or a key that it does not name.
    """
    from llm_kit.registry import project_models  # noqa: PLC0415
    from llm_kit.registry.cost import LOCAL, classify  # noqa: PLC0415
    from llm_kit.registry.model_endpoints import HARNESS_KIND  # noqa: PLC0415
    from llm_kit.registry.models import discover_model_entries  # noqa: PLC0415

    try:
        resolved = project_models.read_resolved_models(project_root=str(root), file=file)
    except project_models.ProjectModelsError as exc:
        raise CompareError("invalid", f"the project model file: {exc.code}: {exc}") from None
    rows = {row["id"]: row for row in resolved.project.document["catalog"]}
    entries = discover_model_entries(project_root=str(root))
    out = []
    for name in selections:
        chosen = (resolved.selections.get(name) or {}).get("effective")
        if chosen is None:
            raise CompareError("invalid", f"selection {name!r} is not in the project model file "
                               f"(known: {', '.join(resolved.selections) or '<none>'})")
        row = rows[chosen["model"]]
        entry = entries.get(row["endpoint"])
        harness = entry is not None and entry.kind == HARNESS_KIND
        extras = dict((row.get("request_defaults") or {}).get("extras") or {})
        cap = None
        if not harness:
            cap = max_output_tokens if max_output_tokens is not None else row.get("max_output_tokens",
                                                                                  extras.get("max_tokens"))
        if classify(entry, entries=entries).cost_class == LOCAL:
            price: Optional[Price] = Price(0, 0)
        else:
            price = pricing.get(row["model"])
        out.append(Candidate(selection=name, row=row["id"], model=row["model"], endpoint=row["endpoint"],
                             effort=chosen.get("effort"), max_output_tokens=cap, extras=extras, price=price,
                             registry_sha256=resolved.project.sha256, prompt_only=not harness))
    return out


def invoke_caller(
    root: Path, *, file: Optional[str], max_output_tokens: Optional[int], timeout_s: Optional[float],
) -> Callable[[Candidate, str], CallResult]:
    """A caller that sends one protocol 2 request for a candidate, with no retry.

    It raises :class:`~llm_kit.runs.compare.CompareError` ``refused`` when the
    request is refused before dispatch, and ``conflict`` when the call used
    another model or effort than the plan (the model file changed).
    """
    from llm_kit.cli import _compose  # noqa: PLC0415
    from llm_kit.completion.invoke_protocol import InvokeRefusal, parse_invoke_request  # noqa: PLC0415
    from llm_kit.dispatch.invoke import OUTCOME_COMPLETED, OUTCOME_HALTED, invoke  # noqa: PLC0415

    def call(candidate: Candidate, prompt: str) -> CallResult:
        payload: dict[str, Any] = {"protocol": 2, "project_root": str(root), "selection": candidate.selection,
                                   "prompt": prompt, "max_retries": 0}
        for key, value in (("file", file), ("max_tokens", max_output_tokens), ("timeout_s", timeout_s)):
            if value is not None:
                payload[key] = value
        try:
            outcome = invoke(parse_invoke_request(payload), default_project_root=str(root),
                             backend_factory=_compose.create_backend)
        except InvokeRefusal as exc:
            raise CompareError("refused", f"{candidate.selection}: refused ({exc.kind}): {exc}") from None
        document = outcome.document
        resolved = document["resolved"]
        if resolved["requested_model"] != candidate.model or resolved["effort"]["requested"] != candidate.effort:
            raise CompareError("conflict", f"{candidate.selection} was sent to {resolved['requested_model']} at "
                               f"effort {resolved['effort']['requested']!r}, not as planned; the model file changed")
        usage = document["usage"]
        output = document.get("output") or {}
        error = document.get("error") or {}
        status = ("completed" if outcome.outcome == OUTCOME_COMPLETED
                  else "halted" if outcome.outcome == OUTCOME_HALTED else "failed")
        return CallResult(
            status=status, text=output.get("text"),
            error=None if status == "completed" else {"code": error.get("code") or "execution",
                                                      "message": error.get("message")},
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            cached_tokens=usage.get("cache_hit_tokens"), reasoning_tokens=usage.get("reasoning_tokens"),
            reported_usd=document["cost"].get("usd"), estimated_usd=document["cost"].get("estimated_usd"),
            total_ms=usage.get("wall_ms"),
        )

    return call


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=True))


def _error(code: str, message: str) -> None:
    print(json.dumps({"error": {"kind": "runs", "code": code, "message": message}}, sort_keys=True),
          file=sys.stderr)


def compare(args: argparse.Namespace, store: RunRecordStore) -> int:
    """Run the ``compare`` verb; return 0 when every attempt ran, 1 when refused or stopped."""
    root = Path(args.project_root) if args.project_root else Path.cwd()
    selections = [key.strip() for value in args.select for key in value.split(",") if key.strip()]
    if args.scorer is not None and not args.scorer:
        raise CompareError("invalid", "--scorer needs a command after it")
    scorer = None if not args.scorer else command_scorer(args.scorer, name=args.scorer_name,
                                                         version=args.scorer_version)
    plan = plan_compare(
        store, project_root=root, prompt=args.prompt, cases=read_cases(args.cases),
        candidates=candidates(root, file=args.models_file, selections=selections, pricing=_pricing(args.pricing),
                              max_output_tokens=args.max_output_tokens),
        flow=args.flow, content=args.content, max_usd=args.max_usd, scorer=scorer, group_id=args.group,
        label=args.label, tool={"name": TOOL_NAME, "version": _package_version()},
    )
    if args.dry_run:
        _print({"dry_run": True, **plan.to_dict()})
        return 0
    try:
        result = run_compare(plan, invoke_caller(root, file=args.models_file, max_output_tokens=args.max_output_tokens,
                                                 timeout_s=args.timeout_s))
    except CompareError as exc:
        if exc.result is not None:
            _print(exc.result.to_dict())
        raise
    _print(result.to_dict())
    if result.stopped is not None:
        _error(result.stopped, result.message or result.stopped)
        return 1
    return 0
