"""Reading run records, values, blobs and media for the viewer.

The record format is ``llm-kit.graph.run/v1``, written by the runtime
(``docs/reference/llm-kit-graph-runtime.md``) and read through
:class:`llm_kit.graph.records.RunStore`. This module is the only place in the
viewer that knows the fields of a run or node record: if the format changes,
change this file. It returns plain dictionaries; the page escapes them.

What the viewer adds to the record:

* ``synthetic`` entries: a path that only appears as the parent of other
  records (a map node whose items are recorded, a composite that is still
  running) gets a status rolled up from what is below it.
* ``running`` nodes of a run whose status is ``running``: the runtime records a
  node when it finishes, so a node with no record whose sibling dependencies are
  all complete is shown as running. A run that died keeps status ``running``;
  the run header shows when its file last changed.
* ``score`` of a round: the number ``score`` in that round's entry of the
  repeat node's ``rounds`` attribute.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from llm_kit.graph.config import GraphConfig
from llm_kit.graph.media import StoreError
from llm_kit.graph.model import GraphError
from llm_kit.graph.records import RunStore as RecordStore
from llm_kit.graph.records import list_items
from llm_kit.graph.viewer.structure import (
    PATH_SEPARATOR,
    embedded_structure,
    unmapped_paths,
)

__all__ = ["DONE_LIKE", "INLINE_LIMIT", "NotFound", "RunReader", "receipt_facts", "rollup"]

#: A value inlined into a response is cut to a preview above this many characters.
INLINE_LIMIT = 200_000
#: All inlined characters of one response.
RESPONSE_LIMIT = 4_000_000
PREVIEW_CHARS = 20_000

DONE_LIKE = frozenset({"done", "reused", "seeded", "skipped"})
_REF = re.compile(r"^(value|blob):[0-9a-f]{64}$")
_MEDIA_NAME = re.compile(r"^[0-9a-f]{16}\.[a-z0-9]{1,8}$")
_BASE = re.compile(r"\[[^/]*\]$")

#: media file extension -> MIME type the page may show inline.
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
    ".mp4": "video/mp4", ".webm": "video/webm",
}


class NotFound(LookupError):
    """A run, node, value or media file does not exist."""


def rollup(statuses: list[str]) -> str:
    """One status for a container from the statuses of everything below it."""
    present = set(statuses)
    if not present:
        return "pending"
    for status in ("failed", "stopped", "waiting", "halted", "running"):
        if status in present:
            return status
    if present <= DONE_LIKE:
        return "done"
    if present <= {"pending", "gated"}:
        return "gated" if "gated" in present else "pending"
    return "partial"


# ---------------------------------------------------------------------------
# fields of a node record
# ---------------------------------------------------------------------------


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def receipt_facts(receipt: Any) -> dict[str, Any]:
    """Model, effort, token counts and cost from an op receipt.

    An ``llm.model`` receipt is the protocol 2 result document: ``resolved``
    (``reported_model``, ``requested_model``, ``effort.sent``), ``usage`` and
    ``cost.usd`` (``cost.estimated_usd`` when the price is unknown). Another op
    may return ``model``, ``effort`` and ``cost_usd`` directly.
    """
    out: dict[str, Any] = {"model": None, "effort": None, "cost_usd": None, "usage": None}
    if not isinstance(receipt, Mapping):
        return out
    resolved = receipt.get("resolved") if isinstance(receipt.get("resolved"), Mapping) else {}
    model = resolved.get("reported_model") or resolved.get("requested_model") or receipt.get("model")
    effort = resolved.get("effort")
    if isinstance(effort, Mapping):
        effort = effort.get("sent") or effort.get("requested")
    effort = effort or receipt.get("effort")
    cost = receipt.get("cost")
    usd = None
    if isinstance(cost, Mapping):
        usd = _number(cost.get("usd"))
        if usd is None:
            usd = _number(cost.get("estimated_usd"))
    if usd is None:
        usd = _number(receipt.get("cost_usd"))
    out["model"] = model if isinstance(model, str) else None
    out["effort"] = effort if isinstance(effort, str) else None
    out["cost_usd"] = usd
    out["usage"] = receipt.get("usage") if isinstance(receipt.get("usage"), Mapping) else None
    return out


def _error_text(error: Any) -> Optional[str]:
    if isinstance(error, Mapping):
        return f"{error.get('type', 'error')}: {error.get('message', '')}".strip()
    return str(error) if error else None


def node_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """The small per-node dictionary the graph picture needs."""
    facts = receipt_facts(record.get("receipt"))
    error = _error_text(record.get("error"))
    return {
        "status": str(record.get("status")),
        "synthetic": False,
        "kind": record.get("kind"),
        "index": record.get("index") if isinstance(record.get("index"), int) else None,
        "started": record.get("started_at"),
        "finished": record.get("ended_at"),
        "duration_s": _number(record.get("seconds")),
        "model": facts["model"],
        "effort": facts["effort"],
        "cost_usd": facts["cost_usd"],
        "error": error[:300] if error else None,
        "media": len(record.get("media") or ()),
        "score": None,
    }


def _synthetic(status: str, *, derived: bool = False) -> dict[str, Any]:
    return {"status": status, "synthetic": True, "derived": derived, "kind": None, "index": None, "started": None, "finished": None,
            "duration_s": None, "model": None, "effort": None, "cost_usd": None, "error": None, "media": 0,
            "score": None}


def _join(prefix: str, node_id: str) -> str:
    return f"{prefix}{PATH_SEPARATOR}{node_id}" if prefix else node_id


def _keys_below(nodes: Mapping[str, Any], path: str) -> list[str]:
    """The raw ``key`` of every ``path[key]`` that has an entry, in first-seen order."""
    seen: dict[str, None] = {}
    head = path + "["
    for other in nodes:
        if other.startswith(head):
            seen.setdefault(other[len(head):].split("]", 1)[0], None)
    return list(seen)


def _derive_running(structure: Mapping[str, Any], nodes: dict[str, dict]) -> None:
    """Mark nodes with no record whose sibling dependencies are complete as running."""
    def walk(graph: Mapping[str, Any], prefix: str) -> None:
        for node in graph["nodes"]:
            path = _join(prefix, node["id"])
            if path not in nodes:
                if all(nodes.get(_join(prefix, dep), {}).get("status") in DONE_LIKE for dep in node["deps"]):
                    nodes[path] = _synthetic("running", derived=True)
                continue
            body = node["body"]
            if body is None:
                continue
            if node["kind"] in ("map", "repeat"):
                for key in _keys_below(nodes, path):
                    walk(body, f"{path}[{key}]")
            else:
                walk(body, path)
    walk(structure, "")


class RunReader:
    """The runs of one project, read from disk on every request."""

    def __init__(self, config: GraphConfig) -> None:
        self.config = config
        self._cache: dict[str, tuple[tuple[int, int], dict]] = {}

    @property
    def configured(self) -> bool:
        return "dir" in self.config.runs

    # -- items and records -------------------------------------------------

    def items(self) -> list[str]:
        return list_items(self.config) if self.configured else []

    def store(self, item: str) -> RecordStore:
        if item not in self.items():
            raise NotFound(f"no run directory for item {item!r}")
        return RecordStore.for_item(self.config, item)

    def _load(self, store: RecordStore, run_id: str) -> dict:
        path = store.run_path(run_id)
        try:
            stat = path.stat()
        except FileNotFoundError:
            raise NotFound(f"no run {run_id!r}") from None
        key = str(path)
        cached = self._cache.get(key)
        if cached and cached[0] == (stat.st_mtime_ns, stat.st_size):
            return cached[1]
        record = store.load_run(run_id)
        for field in ("status", "nodes", "graph"):
            if field not in record:
                raise StoreError(f"run record {path} has no {field}")
        if not isinstance(record["nodes"], Mapping) or not isinstance(record["graph"], Mapping):
            raise StoreError(f"run record {path}: nodes and graph must be objects")
        self._cache[key] = ((stat.st_mtime_ns, stat.st_size), record)
        return record

    def _record(self, item: str, run_id: str) -> tuple[RecordStore, dict]:
        store = self.store(item)
        if run_id not in store.run_ids():
            raise NotFound(f"no run {run_id!r} for item {item!r}")
        return store, self._load(store, run_id)

    # -- listing -----------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        """One summary per run or seed, newest first; an unreadable record carries its error."""
        out = []
        for item in self.items():
            store = RecordStore.for_item(self.config, item)
            for run_id in store.run_ids():
                try:
                    record = self._load(store, run_id)
                except GraphError as exc:
                    out.append({"item": item, "run_id": run_id, "status": "unreadable", "error": str(exc),
                                "kind": None, "graph": None})
                    continue
                cost = 0.0
                for node in record["nodes"].values():
                    cost += receipt_facts(node.get("receipt"))["cost_usd"] or 0.0
                out.append({
                    "item": item, "run_id": run_id, "kind": record.get("kind"), "status": record["status"],
                    "graph": record["graph"].get("id"), "started": record.get("started_at"),
                    "finished": record.get("ended_at"), "nodes": len(record["nodes"]),
                    "counts": record.get("counts") or {}, "cost_usd": round(cost, 6), "error": None,
                })
        out.sort(key=lambda r: r["run_id"], reverse=True)
        return out

    # -- one run -----------------------------------------------------------

    def run_view(self, item: str, run_id: str) -> dict[str, Any]:
        """The run header, its graph structure and one summary per node path."""
        store, record = self._record(item, run_id)
        records = record["nodes"]
        nodes = {path: node_summary(node) for path, node in records.items()}
        below: dict[str, list[str]] = {}
        for path, node in records.items():
            parts = path.split(PATH_SEPARATOR)
            for i in range(1, len(parts) + 1):
                prefix = PATH_SEPARATOR.join(parts[:i])
                for candidate in {prefix, _BASE.sub("", prefix)}:
                    if candidate != path:
                        below.setdefault(candidate, []).append(str(node.get("status")))
        live = record["status"] == "running"
        for prefix, statuses in below.items():
            if prefix not in nodes:
                status = rollup(statuses)
                nodes[prefix] = _synthetic("running" if live and status not in ("failed", "stopped") else status)
        for path, node in records.items():
            ref = (node.get("attributes") or {}).get("rounds")
            if ref is None:
                continue
            rounds = self._value(store, ref, _Budget())
            if isinstance(rounds, list):
                for number, entry in enumerate(rounds):
                    target = nodes.get(f"{path}[{number}]")
                    if target is not None and isinstance(entry, Mapping):
                        target["score"] = _number(entry.get("score"))
        graph = record["graph"]
        structure = embedded_structure(record.get("graphs") or {}, graph.get("hash"))
        if live:
            _derive_running(structure, nodes)
        cost = sum(n["cost_usd"] or 0.0 for n in nodes.values())
        updated = datetime.fromtimestamp(store.run_path(run_id).stat().st_mtime, timezone.utc)
        return {
            "run_id": run_id, "item": item, "kind": record.get("kind"), "status": record["status"],
            "started": record.get("started_at"), "finished": record.get("ended_at"),
            "updated": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "graph": graph.get("id"), "graph_file": graph.get("file"), "graph_hash": graph.get("hash"),
            "target": record.get("target"), "force": record.get("force") or [], "stop": record.get("stop"),
            "cost_usd": round(cost, 6), "structure": structure, "nodes": nodes,
            "unmapped": unmapped_paths(structure, list(records)),
        }

    # -- one node ----------------------------------------------------------

    def node_detail(self, item: str, run_id: str, path: str) -> dict[str, Any]:
        """Everything recorded for one node, references resolved."""
        store, record = self._record(item, run_id)
        node = record["nodes"].get(path)
        if node is None:
            view = self.run_view(item, run_id)["nodes"]
            if path not in view:
                raise NotFound(f"no node {path!r} in run {run_id!r}")
            children = [p for p in view if p.startswith(path + PATH_SEPARATOR) and PATH_SEPARATOR not in p[len(path) + 1:]]
            children += [p for p in view if p.startswith(path + "[") and PATH_SEPARATOR not in p[len(path):]]
            return {"path": path, "synthetic": True, "status": view[path]["status"], "children": sorted(set(children))}
        budget = _Budget()
        attributes = {}
        for name, ref in (node.get("attributes") or {}).items():
            attributes[name] = None if ref is None else self._value(store, ref, budget)
        out = attributes.pop("out", None)
        extras = {}
        for key in ("inputs", "params", "reads", "carry"):
            ref = node.get(key)
            if ref is not None:
                extras[key] = self._value(store, ref, budget)
        media = []
        for ref in node.get("media") or ():
            name = str(ref).removeprefix("media:")
            media.append({"ref": ref, "name": name, "mime": MEDIA_TYPES.get(Path(name).suffix.lower()),
                          "url": f"/media/{item}/{name}" if _MEDIA_NAME.match(name) else None})
        receipt = node.get("receipt")
        return {
            "path": path, "synthetic": False, "status": str(node.get("status")), "kind": node.get("kind"),
            "node": node.get("node"), "op": node.get("op"),
            "started": node.get("started_at"), "finished": node.get("ended_at"),
            "duration_s": _number(node.get("seconds")),
            "definition_hash": node.get("definition_hash"), "input_hash": node.get("input_hash"),
            "reused_from": node.get("reused_from"), "seed": node.get("seed"),
            "key": node.get("key"), "index": node.get("index"), "until": node.get("until"),
            "inputs": extras.get("inputs"), "params": extras.get("params"), "reads": extras.get("reads"),
            "carry": extras.get("carry"), "out": out, "attributes": attributes,
            "receipt": receipt, "facts": receipt_facts(receipt),
            "media": media, "error": node.get("error"),
        }

    # -- stored values -----------------------------------------------------

    def _value(self, store: RecordStore, ref: Any, budget: "_Budget") -> Any:
        """A ``value:`` or ``blob:`` reference resolved; a failure is returned as ``{"$error": ...}``."""
        if not isinstance(ref, str) or not _REF.match(ref):
            return {"$error": f"{ref!r} is not a value or blob reference"}
        try:
            value = store.get_attribute(ref)
        except (GraphError, OSError, ValueError) as exc:
            return {"$error": str(exc), "ref": ref}
        size = len(value) if isinstance(value, str) else len(json.dumps(value))
        if size > INLINE_LIMIT or size > budget.left:
            text = value if isinstance(value, str) else json.dumps(value)
            return {"$truncated": True, "ref": ref, "size": size, "preview": text[:PREVIEW_CHARS]}
        budget.left -= size
        return value

    def stored_text(self, item: str, ref: str) -> str:
        """The whole text of a ``value:`` or ``blob:`` reference of ``item``."""
        if not _REF.match(ref):
            raise NotFound("not a value or blob reference")
        store = self.store(item)
        try:
            value = store.get_attribute(ref)
        except StoreError as exc:
            raise NotFound(str(exc)) from None
        return value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=True)

    def media_file(self, item: str, name: str) -> tuple[Path, Optional[str]]:
        """The media file ``name`` (``<16 hex><ext>``) of ``item`` and its MIME type, if known."""
        if not _MEDIA_NAME.match(name):
            raise NotFound("not a media file name")
        store = self.store(item)
        path = store.media_dir / name
        if not path.is_file() or path.is_symlink():
            raise NotFound(f"media {name} is not on disk")
        return path, MEDIA_TYPES.get(path.suffix.lower())


class _Budget:
    def __init__(self) -> None:
        self.left = RESPONSE_LIMIT
