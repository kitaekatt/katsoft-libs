"""Inferred labels for a graph catalog: one model call per graph file, cached by what the model sees.

:func:`label_catalog` takes a catalog (``llm-kit.graph-catalog/v1``) and a
:class:`~llm_kit.graph.render.label_types.Labeller`, and returns a catalog of
format ``llm-kit.graph-catalog/v2``: the same graphs plus ``labels``, a title and
description for each node, a text for each edge, a gloss for each input and
output, and the graph's own title and purpose. The library makes no call itself;
whoever passes a labeller makes them, and
:func:`llm_kit.graph.render.label_model.model_labeller` is the one that asks a
model.

How a render is labelled:

* each graph file is one call (``nodes_per_call`` splits a file that is too big
  for one); calls run concurrently, at most ``jobs`` at a time;
* an answer is parsed strictly (no repair), checked against the call's answer
  schema and against the keys the call asked for. A call that left keys out gets
  ONE follow-up for the missing keys, pinned to the model entry that served the
  first call; a key that was not asked for, or one still missing, is an error;
* each call's merged answer is cached under the SHA-256 of everything the model
  sees (``llm-kit.graph-labels/v1`` entries), so a warm render makes no call and
  a changed graph file relabels only its own calls. ``refresh`` skips cache reads;
* the first failure stops new calls, lets running ones finish and keeps their
  results cached, then raises the first failure in plan order;
* progress goes to a callback and nowhere else; the result does not depend on the
  order the calls finish in.

The cache lives in ``$XDG_CACHE_HOME/llm-kit/graph-labels/`` (fallback
``~/.cache/llm-kit/graph-labels/``); nothing is written in the project.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from llm_kit.graph.render._label_evidence import check_answer, followup_call, op_descriptions, plan_calls
from llm_kit.graph.render.catalog import CATALOG_FORMAT
from llm_kit.graph.render.label_types import (
    LABELLED_CATALOG_FORMAT,
    LABELS_PROMPT_ID,
    LabelCall,
    LabelError,
    LabelFailed,
    LabelReply,
    Labeller,
)
from llm_kit.registry import user_paths
from llm_kit.schema.formats import GRAPH_LABELS_V1, load_schema
from llm_kit.schema.json_schema import validate

__all__ = ["DEFAULT_JOBS", "CACHE_DIRNAME", "LabelCache", "default_cache_dir", "label_catalog"]

#: How many label calls run at the same time unless the caller says otherwise.
DEFAULT_JOBS = 4

#: The directory name of the label cache under the neutral cache directory.
CACHE_DIRNAME = "graph-labels"

_KEY = re.compile(r"[0-9a-f]{64}")


def default_cache_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The default label cache directory: ``<cache home>/llm-kit/graph-labels``.

    The cache home is ``$XDG_CACHE_HOME`` when that is an absolute path, else
    ``~/.cache`` (see :mod:`llm_kit.registry.user_paths`). ``environ`` replaces
    the process environment. The directory is not created.
    """
    return user_paths.neutral_cache_dir(environ) / CACHE_DIRNAME


@functools.lru_cache(maxsize=1)
def _entry_schema_text() -> str:
    return json.dumps(load_schema(GRAPH_LABELS_V1))


def _entry_errors(entry: Any) -> tuple:
    return validate(json.loads(_entry_schema_text()), entry)


def _first(errors: tuple) -> str:
    return "; ".join(f"{pointer or '/'}: {keyword}" for pointer, keyword in errors[:3])


class LabelCache:
    """A directory of cache entries (``llm-kit.graph-labels/v1``), one file ``<key>.json`` per request.

    ``directory`` is created when the first entry is written; reading creates
    nothing. :meth:`put` writes through a temporary file and an atomic rename, so
    a reader never sees half an entry.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path(self, key: str) -> Path:
        """The file of ``key`` (64 lowercase hex digits); ``ValueError`` for any other text."""
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError(f"a label cache key is 64 lowercase hex digits, got {key!r}")
        return self.directory / f"{key}.json"

    def get(self, key: str) -> Optional[dict]:
        """The entry stored under ``key``, or ``None`` when there is none.

        A file that is there but unreadable, not JSON, not a ``graph-labels/v1``
        entry, or whose ``key`` is not its file name raises
        :class:`~llm_kit.graph.render.label_types.LabelError` naming the file and
        the ``--relabel`` remedy; a damaged entry is never used.
        """
        path = self.path(key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LabelError(f"cannot read the label cache entry {path}: {exc}; run with --relabel to replace it") from None
        try:
            entry = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise LabelError(f"the label cache entry {path} is damaged (not JSON: {exc}); "
                             f"run with --relabel to replace it") from None
        errors = _entry_errors(entry)
        if errors:
            raise LabelError(f"the label cache entry {path} is damaged ({_first(errors)}); "
                             f"run with --relabel to replace it")
        if entry["key"] != key:
            raise LabelError(f"the label cache entry {path} is damaged (it holds the key {entry['key']}); "
                             f"run with --relabel to replace it")
        return entry

    def put(self, entry: Mapping[str, Any]) -> Path:
        """Write ``entry`` to ``<directory>/<entry key>.json`` atomically and return the path.

        ``ValueError`` when the entry's ``key`` is not 64 lowercase hex digits;
        ``OSError`` when the directory or file cannot be written (no partial file
        is left behind).
        """
        path = self.path(entry["key"])
        self.directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".entry-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(entry, indent=1, ensure_ascii=False) + "\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return path


# ---------------------------------------------------------------------------
# one request
# ---------------------------------------------------------------------------


def _checked(call: LabelCall, reply: LabelReply) -> dict:
    """The reply's answer as plain JSON data, checked against the call's answer schema."""
    try:
        answer = json.loads(json.dumps(reply.answer))
    except (TypeError, ValueError) as exc:
        raise LabelFailed(f"the answer for {call.file} is not JSON data: {exc}") from None
    if not isinstance(answer, dict):
        raise LabelFailed(f"the answer for {call.file} is not a JSON object")
    errors = validate(call.schema, answer)
    if errors:
        raise LabelFailed(f"the answer for {call.file} does not match the answer schema ({_first(errors)})")
    return answer


def _merge(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict:
    merged: dict[str, Any] = {"nodes": {**first.get("nodes", {}), **second.get("nodes", {})},
                              "edges": {**first.get("edges", {}), **second.get("edges", {})}}
    if "graph" in first or "graph" in second:
        merged["graph"] = second.get("graph", first.get("graph"))
    for key in ("inputs", "outputs"):
        if key in first or key in second:
            merged[key] = {**first.get(key, {}), **second.get(key, {})}
    return merged


def _describe(missing: Mapping[str, list]) -> str:
    names = [name for group in missing.values() for name in group]
    return ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def _request(call: LabelCall, labeller: Labeller, say: Callable[[str], None]) -> dict:
    """Make one request: the call, then one follow-up for the keys it left out. Returns the cache entry."""
    reply = labeller(call)
    answer = _checked(call, reply)
    missing = check_answer(call, answer)
    served = [{"entry": reply.entry, "model": reply.model}]
    if missing:
        follow = followup_call(call, missing, reply.entry)
        say(f"labels: {call.file}: follow-up for {sum(len(v) for v in missing.values())} missing keys "
            f"with {reply.entry}")
        second = labeller(follow)
        if second.entry != reply.entry:
            raise LabelFailed(f"the follow-up for {call.file} was answered by {second.entry}, "
                              f"not by {reply.entry}, the entry pinned for it")
        extra = _checked(follow, second)
        check_answer(follow, extra)  # a key nobody asked for
        answer = _merge(answer, extra)
        served.append({"entry": second.entry, "model": second.model})
        left = check_answer(call, answer)
        if left:
            raise LabelFailed(f"the answer for {call.file} still lacks {_describe(left)} after a follow-up")
    answer.setdefault("nodes", {})
    answer.setdefault("edges", {})
    entry = {"format": GRAPH_LABELS_V1, "key": call.key, "prompt": LABELS_PROMPT_ID,
             "declaration": list(labeller.declaration), "calls": served, "answer": answer}
    errors = _entry_errors(entry)
    if errors:
        raise LabelFailed(f"the labels for {call.file} do not make a valid cache entry ({_first(errors)})")
    return entry


def _check_cached(call: LabelCall, entry: dict, cache: LabelCache, declaration: tuple) -> None:
    where = cache.path(call.key)
    if entry["prompt"] != LABELS_PROMPT_ID or tuple(entry["declaration"]) != declaration:
        raise LabelError(f"the label cache entry {where} was made for another request; run with --relabel to replace it")
    try:
        missing = check_answer(call, entry["answer"])
    except LabelFailed as exc:
        raise LabelError(f"the label cache entry {where} does not match its request ({exc}); "
                         f"run with --relabel to replace it") from None
    if missing:
        raise LabelError(f"the label cache entry {where} is incomplete (it lacks {_describe(missing)}); "
                         f"run with --relabel to replace it")


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------


def _assemble(catalog: Mapping[str, Any], by_file: Mapping[str, list], entries: Mapping[str, dict]) -> dict:
    labels: dict[str, Any] = {}
    for file in catalog["files"]:
        sources: list = []
        graph: Optional[dict] = None
        nodes: dict[str, Any] = {}
        for index, call in enumerate(by_file[file]):
            entry = entries[call.key]
            answer = entry["answer"]
            sources.append({"prompt": entry["prompt"], "key": entry["key"], "calls": copy.deepcopy(entry["calls"])})
            if call.header:
                graph = {"title": answer["graph"]["title"], "purpose": answer["graph"]["purpose"],
                         "inputs": dict(answer.get("inputs", {})), "outputs": dict(answer.get("outputs", {})),
                         "source": index}
            for path in call.nodes:
                reads = {}
                for edge in call.edges:
                    dep, target = edge.split("->", 1)
                    if target == path:
                        reads[dep.rsplit("/", 1)[-1]] = answer["edges"][edge]
                nodes[path] = {"title": answer["nodes"][path]["title"],
                               "description": answer["nodes"][path]["description"],
                               "reads": reads, "source": index}
        labels[file] = {"sources": sources, "graph": graph, "nodes": nodes}
    result = copy.deepcopy(dict(catalog))
    result["format"] = LABELLED_CATALOG_FORMAT
    result["labels"] = labels
    return result


def label_catalog(catalog: Mapping[str, Any], labeller: Labeller, *, root: Path | str,
                  cache: Optional[LabelCache] = None, jobs: int = DEFAULT_JOBS,
                  nodes_per_call: Optional[int] = None, refresh: bool = False,
                  namespaces: Optional[Mapping[str, Path]] = None,
                  progress: Optional[Callable[[str], None]] = None) -> dict:
    """Label every node, edge, input and output of ``catalog`` and return the v2 catalog.

    ``catalog`` is a ``llm-kit.graph-catalog/v1`` catalog
    (:func:`~llm_kit.graph.render.project_catalog`); it is not changed. The
    result is the same catalog with format ``llm-kit.graph-catalog/v2`` and a
    ``labels`` object that holds complete label blocks for every graph file
    (normalised from the model answers; see ``docs/reference/llm-kit-formats.md``).

    ``labeller`` makes the calls and may be called from several threads;
    its ``declaration`` enters the cache key. ``root`` is the project root: the
    prompt files, op namespace directories and ``.py`` files read as evidence are
    read only when they resolve inside it, and a graph file outside it is
    labelled from the catalog alone. ``namespaces`` maps each op namespace of the
    project config to its directory (``GraphConfig.namespaces``); ``None`` means
    none. ``cache`` stores results (``None`` reads and writes no cache); with
    ``refresh`` true cache entries are not read but are rewritten. ``jobs`` is
    the number of calls that run at once and ``nodes_per_call`` splits a file's
    nodes into calls of at most that many (``None``: one call per file).
    ``progress`` receives one text line at a time (no trailing newline) and is
    called under a lock; nothing is printed.

    A cache hit makes no call, so a render whose labels are all cached never
    calls ``labeller``. Raises ``ValueError`` for a catalog that is not v1 or a bad
    ``jobs`` or ``nodes_per_call``; :class:`~llm_kit.graph.render.label_types.LabelError`
    for a damaged cache entry (before any call) or a cache that cannot be
    written; :class:`~llm_kit.graph.render.label_types.LabelRefused` or
    :class:`~llm_kit.graph.render.label_types.LabelFailed` from planning or the
    calls. When several calls fail, the first in plan order is raised with the
    number of the others; calls that finished before the failure stay cached.
    """
    if catalog.get("format") != CATALOG_FORMAT:
        raise ValueError(f"label_catalog takes a {CATALOG_FORMAT} catalog, got {catalog.get('format')!r}")
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError(f"jobs must be an integer of at least 1, got {jobs!r}")
    if nodes_per_call is not None and (isinstance(nodes_per_call, bool) or not isinstance(nodes_per_call, int)
                                       or nodes_per_call < 1):
        raise ValueError(f"nodes_per_call must be None or an integer of at least 1, got {nodes_per_call!r}")
    root = Path(root)
    declaration = tuple(labeller.declaration)
    lock = threading.Lock()

    def say(text: str) -> None:
        if progress is not None:
            with lock:
                progress(text)

    plan = plan_calls(catalog, root=root, declaration=declaration,
                      op_texts=op_descriptions(namespaces or {}, root=root), nodes_per_call=nodes_per_call)
    by_file: dict[str, list] = {}
    for call in plan:
        by_file.setdefault(call.file, []).append(call)
    unique: dict[str, LabelCall] = {}
    for call in plan:
        unique.setdefault(call.key, call)

    entries: dict[str, dict] = {}
    todo: list[LabelCall] = []
    for call in unique.values():
        entry = cache.get(call.key) if cache is not None and not refresh else None
        if entry is None:
            todo.append(call)
        else:
            _check_cached(call, entry, cache, declaration)
            entries[call.key] = entry
    cached = len(entries)
    files = len(catalog["files"])
    shape = f"{files} graph files" + (f" in {len(unique)} calls" if len(unique) != files else "")
    if todo:
        say(f"labels: {shape}, {cached} cached, {len(todo)} to make with {', '.join(declaration)} "
            f"({jobs} at a time)")
    else:
        say(f"labels: {shape}, {cached} cached, none to make")

    if todo:
        stop = threading.Event()
        done = [0]

        def make(call: LabelCall) -> Optional[dict]:
            if stop.is_set():
                return None
            started = time.monotonic()
            try:
                entry = _request(call, labeller, say)
                if cache is not None:
                    try:
                        cache.put(entry)
                    except OSError as exc:
                        raise LabelError(f"cannot write the label cache under {cache.directory}: {exc}") from None
            except BaseException:
                stop.set()
                raise
            with lock:
                done[0] += 1
                number = done[0]
            part = f" (call {call.index + 1} of {len(by_file[call.file])})" if len(by_file[call.file]) > 1 else ""
            served = entry["calls"][-1]
            say(f"labels: [{number}/{len(todo)}] {call.file}{part} made with {served['entry']} "
                f"({served['model']}) in {time.monotonic() - started:.1f}s")
            return entry

        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="graph-labels") as pool:
            futures = [pool.submit(make, call) for call in todo]
        failures = [future.exception() for future in futures]
        raised = [exc for exc in failures if exc is not None]
        if raised:
            first, others = raised[0], len(raised) - 1
            if isinstance(first, LabelError) and others:
                try:
                    wrapped = type(first)(f"{first} ({others} other labelling call{'s' if others != 1 else ''} "
                                          f"also failed)")
                except TypeError:
                    raise first
                raise wrapped from first
            raise first
        for call, future in zip(todo, futures):
            entries[call.key] = future.result()
        say(f"labels: {len(todo)} made, {cached} cached")
    else:
        say(f"labels: 0 made, {cached} cached")
    return _assemble(catalog, by_file, entries)
