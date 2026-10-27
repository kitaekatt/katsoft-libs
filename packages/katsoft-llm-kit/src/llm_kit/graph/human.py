"""Human-input nodes as durable waits on the execution store's interrupts (decision Q29).

A ``human`` node asks a person for an answer that matches its schema. The
wait is a unit of an :class:`~llm_kit.pipeline.execution.store.ExecutionStore`
(SQLite) at ``<runs.state>/waits.sqlite``; one store run per work item. The
unit id is ``<graph id>/<node path>@<key>#<n>``, where ``key`` digests the
node's definition hash and input hash: the same question with the same inputs
is the same wait, whichever graph run reaches it.

1. A run reaches the node with no answer: the runner claims the unit (a
   fencing token), records a typed interrupt request (kind = the node's
   ``kind``, schema = the node's schema, payload = graph, node, run, item and
   a reference to the payload value in the run store) and the node stops the
   run (status ``stopped``, reason ``waiting``).
2. ``llm-kit graph answer`` (or :func:`answer_wait`) validates the answer
   against the node schema and resolves the interrupt; the unit is pending.
3. ``llm-kit graph resume`` runs the graph again. Finished nodes are reused;
   the human node claims the unit (the next fencing token), accepts it with
   the answer and its ``out`` is the answer. The run record keeps the answer
   and ``wait`` (unit, interrupt, fencing tokens). Later runs reuse the node
   record like any other.

A forced node (``--force``) whose answer was used asks again (unit ``#n+1``).
A second process that races for the same unit gets the store's claim error.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from llm_kit.pipeline.execution.interrupts import unit_resolutions
from llm_kit.pipeline.execution.model import InterruptRequest, UnitState
from llm_kit.pipeline.execution.store import ExecutionStore
from llm_kit.schema.json_schema import validate as validate_value

from .model import GraphError
from .run import NodeStopped

__all__ = ["WAITS_FILE", "OpenWait", "WaitService", "answer_wait", "open_waits"]

#: The wait store's file name inside ``runs.state``.
WAITS_FILE = "waits.sqlite"

_DRIVER = "llm-kit.graph"
_LEASE_SECONDS = 300.0


def _key(definition_hash: str, input_hash: str) -> str:
    return hashlib.sha256(f"{definition_hash}\n{input_hash}".encode("ascii")).hexdigest()[:16]


def _number(unit_id: str) -> int:
    return int(unit_id.rsplit("#", 1)[1])


@dataclass(frozen=True)
class OpenWait:
    """A human node waiting for an answer."""

    item: str
    graph: str
    node: str
    run: str
    kind: str
    interrupt: str
    unit: str
    schema: Mapping[str, Any]
    payload: Any
    created_at: float

    def to_json(self) -> dict:
        return {"item": self.item, "graph": self.graph, "node": self.node, "run": self.run, "kind": self.kind,
                "interrupt": self.interrupt, "unit": self.unit, "schema": dict(self.schema),
                "payload": self.payload, "created_at": self.created_at}


class WaitService:
    """The durable-wait store of one project (``runs.state``)."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.store = ExecutionStore(self.path)

    @classmethod
    def for_item(cls, config: Any, item: str) -> "WaitService":
        from .records import _placeholder_values  # noqa: PLC0415 -- the run-path placeholders of the item

        if "state" not in config.runs:
            raise GraphError(f"{config.path}: runs.state is not configured; a human node needs the wait store")
        template = config.runs["state"]
        return cls(config.run_path("state", **_placeholder_values(config, template, item)) / WAITS_FILE)

    def _ensure_run(self, item: str) -> None:
        if self.store.get_run(item) is None:
            self.store.create_run(item, driver=_DRIVER, backend="human", model="-", adapter_version="1")

    def _units(self, item: str, prefix: str) -> list:
        if self.store.get_run(item) is None:
            return []
        units = [u for u in self.store.list_units(item) if u.unit_id.startswith(prefix + "#")]
        return sorted(units, key=lambda u: _number(u.unit_id))

    def wait(self, *, item: str, graph_id: str, path: str, run_id: str, kind: str, schema: Mapping[str, Any],
             payload_ref: str, definition_hash: str, input_hash: str, ask_again: bool = False
             ) -> tuple[Any, dict]:
        """Return ``(answer, wait document)``, or raise :class:`NodeStopped` (``waiting``) after asking."""
        prefix = f"{graph_id}/{path}@{_key(definition_hash, input_hash)}"
        worker = f"llm-kit-graph:{run_id}"
        units = self._units(item, prefix)
        latest = units[-1] if units else None
        if latest is not None:
            state = latest.state
            answers = [r for r in unit_resolutions(self.store, item, latest.unit_id) if r["outcome"] == "answered"]
            if state is UnitState.WAITING:
                open_ = self.store.open_interrupt(item, latest.unit_id)
                raise self._waiting(path, kind, latest.unit_id, open_.id if open_ is not None else None)
            if state is UnitState.PENDING and answers:
                answer = answers[-1]["input"]
                self._check(schema, answer, f"human node {path}: the recorded answer")
                claim = self.store.claim_unit(item, latest.unit_id, worker, lease_seconds=_LEASE_SECONDS)
                self.store.accept_unit(item, latest.unit_id, claim.fencing_token,
                                       text=json.dumps(answer, sort_keys=True, ensure_ascii=True))
                return answer, {"unit": latest.unit_id, "interrupt": answers[-1]["interrupt_id"],
                                "fencing_token": claim.fencing_token, "answered": True}
            if state is UnitState.ACCEPTED and not ask_again:
                if not answers:
                    raise GraphError(f"human node {path}: wait unit {latest.unit_id} was accepted without an answer")
                answer = answers[-1]["input"]
                self._check(schema, answer, f"human node {path}: the recorded answer")
                return answer, {"unit": latest.unit_id, "interrupt": answers[-1]["interrupt_id"],
                                "fencing_token": None, "answered": True}
            if state not in (UnitState.ACCEPTED, UnitState.PENDING):
                raise GraphError(f"human node {path}: wait unit {latest.unit_id} is {state.value}; "
                                 "this build only answers waits")
        if latest is None or latest.state is UnitState.ACCEPTED:
            self._ensure_run(item)
            unit = f"{prefix}#{_number(latest.unit_id) + 1 if latest is not None else 1}"
            self.store.register_units(item, [unit])
        else:
            unit = latest.unit_id                       # registered, never asked (an interrupted run)
        claim = self.store.claim_unit(item, unit, worker, lease_seconds=_LEASE_SECONDS)
        request = InterruptRequest(kind=kind, request_schema=dict(schema),
                                   payload={"graph": graph_id, "node": path, "run": run_id, "item": item,
                                            "payload": payload_ref, "input_hash": input_hash})
        record = self.store.request_interrupt(item, unit, claim.fencing_token, request)
        raise self._waiting(path, kind, unit, record.id, claim.fencing_token)

    @staticmethod
    def _waiting(path: str, kind: str, unit: str, interrupt: Optional[str],
                 token: Optional[int] = None) -> NodeStopped:
        return NodeStopped("waiting", f"human node {path} waits for an answer of kind {kind!r} "
                                      f"(llm-kit graph answer --node {path})",
                           detail={"wait": {"unit": unit, "interrupt": interrupt, "kind": kind,
                                            "fencing_token": token}})

    @staticmethod
    def _check(schema: Mapping[str, Any], value: Any, what: str) -> None:
        errors = [f"{pointer or '/'} {keyword}" for pointer, keyword in validate_value(schema, value)]
        if errors:
            raise GraphError(f"{what} does not match the node schema: {', '.join(errors)}")

    # -- reads and answers --------------------------------------------------------

    def open_waits(self, item: str, *, graph_id: Optional[str] = None, store: Any = None) -> list[OpenWait]:
        """The open waits of ``item`` (of ``graph_id`` when given), oldest first.

        ``store`` (the item's :class:`~llm_kit.graph.records.RunStore`) resolves
        each payload reference; without it ``payload`` is the reference.
        """
        if self.store.get_run(item) is None:
            return []
        out = []
        for record in self.store.list_interrupts(item):
            if record.resolution is not None:
                continue
            meta = record.payload
            if graph_id is not None and meta.get("graph") != graph_id:
                continue
            payload = meta.get("payload")
            if store is not None and isinstance(payload, str):
                payload = store.get_value(payload)
            out.append(OpenWait(item=item, graph=meta["graph"], node=meta["node"], run=meta["run"],
                                kind=record.kind, interrupt=record.id, unit=record.unit_id,
                                schema=record.request_schema, payload=payload, created_at=record.created_at))
        return out

    def answer(self, item: str, node: str, value: Any, *, graph_id: Optional[str] = None) -> OpenWait:
        """Validate ``value`` against the waiting node's schema and resolve its interrupt."""
        waits = [w for w in self.open_waits(item, graph_id=graph_id) if w.node == node]
        if not waits:
            open_nodes = sorted({w.node for w in self.open_waits(item, graph_id=graph_id)})
            raise GraphError(f"no open wait for node {node!r} of item {item!r}; open: {open_nodes or 'none'}")
        if len(waits) > 1:
            raise GraphError(f"node {node!r} of item {item!r} has {len(waits)} open waits "
                             f"(graphs {sorted(w.graph for w in waits)}); name the graph")
        wait = waits[0]
        self._check(wait.schema, value, f"the answer for {node}")
        self.store.resolve_interrupt(item, wait.interrupt, decision="answer", input=value)
        return wait


def open_waits(config: Any, item: str, *, graph_id: Optional[str] = None) -> list[OpenWait]:
    """The open waits of ``item`` in the project of ``config``, with payloads resolved."""
    from .records import RunStore  # noqa: PLC0415

    return WaitService.for_item(config, item).open_waits(item, graph_id=graph_id,
                                                         store=RunStore.for_item(config, item))


def answer_wait(config: Any, item: str, node: str, value: Any, *, graph_id: Optional[str] = None) -> OpenWait:
    """Answer the open wait of ``node`` for ``item``; raise GraphError for a bad value or no wait."""
    return WaitService.for_item(config, item).answer(item, node, value, graph_id=graph_id)
