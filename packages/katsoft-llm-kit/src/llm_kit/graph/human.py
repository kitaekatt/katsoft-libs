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

A wait is identified by graph, node path and the digest of definition hash and
input hash (the unit id). When a run opens or reaches a wait for a node path,
every other open wait of that graph, item and node path (another digest: an
older build or other inputs) is superseded: the store records it as resolved
with the reason ``superseded by <unit>``, so only the current wait stays open.
A person may also dismiss an open wait (reason ``dismissed``). Both close the
unit ``operator_rejected`` and stay in the store; a later run that asks the same
question again opens the next unit number.
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

__all__ = ["DISMISSED", "SUPERSEDED", "WAITS_FILE", "OpenWait", "WaitService", "answer_wait", "dismiss_wait",
           "open_waits", "resolution_kind"]

#: The wait store's file name inside ``runs.state``.
WAITS_FILE = "waits.sqlite"

#: The reason words of a closed wait that nobody answered (stored as a rejection with this reason prefix).
SUPERSEDED = "superseded"
DISMISSED = "dismissed"

_DRIVER = "llm-kit.graph"
_LEASE_SECONDS = 300.0


def _key(definition_hash: str, input_hash: str) -> str:
    return hashlib.sha256(f"{definition_hash}\n{input_hash}".encode("ascii")).hexdigest()[:16]


def _number(unit_id: str) -> int:
    return int(unit_id.rsplit("#", 1)[1])


def resolution_kind(outcome: str, reason: Optional[str]) -> str:
    """``answered``, ``superseded``, ``dismissed`` or the store's own outcome word for a resolved wait."""
    if outcome == "rejected" and reason:
        for kind in (SUPERSEDED, DISMISSED):
            if reason == kind or reason.startswith(kind + " ") or reason.startswith(kind + ":"):
                return kind
    return outcome


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

    def describe(self, limit: int = 160) -> str:
        """One line: unit, run and a short payload summary."""
        text = json.dumps(self.payload, sort_keys=True, ensure_ascii=True)
        if len(text) > limit:
            text = text[:limit] + "..."
        return f"{self.unit} (run {self.run}, payload {text})"


class WaitService:
    """The durable-wait store of one project (``runs.state``)."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.store = ExecutionStore(self.path)

    @classmethod
    def for_item(cls, config: Any, item: str) -> "WaitService":
        from .records import _placeholder_values, check_item  # noqa: PLC0415 -- the run-path placeholders of the item

        if "state" not in config.runs:
            raise GraphError(f"{config.path}: runs.state is not configured; a human node needs the wait store")
        template = config.runs["state"]
        check_item(item)
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
                self._supersede(item, graph_id, path, latest.unit_id)
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
            if state not in (UnitState.ACCEPTED, UnitState.PENDING, UnitState.OPERATOR_REJECTED):
                raise GraphError(f"human node {path}: wait unit {latest.unit_id} is {state.value}; "
                                 "this build only answers waits")
        if latest is None or latest.state in (UnitState.ACCEPTED, UnitState.OPERATOR_REJECTED):
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
        self._supersede(item, graph_id, path, unit)
        raise self._waiting(path, kind, unit, record.id, claim.fencing_token)

    def _supersede(self, item: str, graph_id: str, path: str, keep_unit: str) -> list[str]:
        """Close every other open wait of ``graph_id``'s node ``path`` as superseded by ``keep_unit``."""
        closed = []
        for wait in self.open_waits(item, graph_id=graph_id):
            if wait.node == path and wait.unit != keep_unit:
                self.store.resolve_interrupt(item, wait.interrupt, decision="reject",
                                             reason=f"{SUPERSEDED}: by {keep_unit}")
                closed.append(wait.unit)
        return closed

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

    def _select(self, item: str, node: Optional[str], graph_id: Optional[str], wait: Optional[str],
                run: Optional[str], store: Any = None) -> OpenWait:
        """The one open wait named by ``node``, ``wait`` (unit id) and ``run``; GraphError when none or several."""
        everything = self.open_waits(item, graph_id=graph_id, store=store)
        waits = [w for w in everything
                 if (node is None or w.node == node) and (wait is None or w.unit == wait)
                 and (run is None or w.run == run)]
        if not waits:
            what = ", ".join(f"{name} {value!r}" for name, value in (("node", node), ("wait", wait), ("run", run))
                             if value is not None)
            raise GraphError(f"no open wait for {what} of item {item!r}; open: "
                             f"{[w.unit for w in everything] or 'none'}")
        if len(waits) > 1:
            lines = "".join(f"\n  {w.describe()}" for w in waits)
            raise GraphError(f"item {item!r} has {len(waits)} open waits for this selection; pick one with "
                             f"--wait UNIT (or --run RUN), or close a stale one with --dismiss UNIT:{lines}")
        return waits[0]

    def answer(self, item: str, node: Optional[str], value: Any, *, graph_id: Optional[str] = None,
               wait: Optional[str] = None, run: Optional[str] = None, store: Any = None) -> OpenWait:
        """Validate ``value`` against the chosen wait's schema and resolve its interrupt (only that one).

        The wait is chosen by ``node``, by ``wait`` (its unit id) or by ``run`` (the run that opened it);
        several open waits that match need ``wait`` or ``run`` to tell them apart.
        ``store`` (the item's RunStore) lets the error for several waits show each payload.
        """
        if node is None and wait is None:
            raise GraphError("name the wait to answer: a node path or a wait unit id")
        chosen = self._select(item, node, graph_id, wait, run, store)
        self._check(chosen.schema, value, f"the answer for {chosen.node}")
        self.store.resolve_interrupt(item, chosen.interrupt, decision="answer", input=value)
        return chosen

    def dismiss(self, item: str, unit: str, *, graph_id: Optional[str] = None,
                reason: Optional[str] = None, store: Any = None) -> OpenWait:
        """Close the open wait ``unit`` without an answer; the store records it as dismissed."""
        chosen = self._select(item, None, graph_id, unit, None, store)
        self.store.resolve_interrupt(item, chosen.interrupt, decision="reject",
                                     reason=f"{DISMISSED}: {reason}" if reason else DISMISSED)
        return chosen

    def closed_waits(self, item: str, *, graph_id: Optional[str] = None) -> list[dict]:
        """The resolved waits of ``item``, oldest first: unit, node, run, ``resolution`` (answered, superseded,
        dismissed) and the stored reason."""
        if self.store.get_run(item) is None:
            return []
        out = []
        for record in self.store.list_interrupts(item):
            res = record.resolution
            meta = record.payload
            if res is None or (graph_id is not None and meta.get("graph") != graph_id):
                continue
            out.append({"unit": record.unit_id, "node": meta["node"], "run": meta["run"], "graph": meta["graph"],
                        "resolution": resolution_kind(res.outcome, res.reason), "reason": res.reason})
        return out


def open_waits(config: Any, item: str, *, graph_id: Optional[str] = None) -> list[OpenWait]:
    """The open waits of ``item`` in the project of ``config``, with payloads resolved."""
    from .records import RunStore  # noqa: PLC0415

    return WaitService.for_item(config, item).open_waits(item, graph_id=graph_id,
                                                         store=RunStore.for_item(config, item))


def answer_wait(config: Any, item: str, node: Optional[str], value: Any, *, graph_id: Optional[str] = None,
                wait: Optional[str] = None, run: Optional[str] = None) -> OpenWait:
    """Answer the open wait of ``node`` (or unit ``wait``, or opened by ``run``) for ``item``.

    Raises GraphError for a bad value, no wait, or several matching waits.
    """
    from .records import RunStore  # noqa: PLC0415

    return WaitService.for_item(config, item).answer(item, node, value, graph_id=graph_id, wait=wait, run=run,
                                                     store=RunStore.for_item(config, item))


def dismiss_wait(config: Any, item: str, unit: str, *, graph_id: Optional[str] = None,
                 reason: Optional[str] = None) -> OpenWait:
    """Dismiss the open wait ``unit`` of ``item`` (recorded, not deleted)."""
    return WaitService.for_item(config, item).dismiss(item, unit, graph_id=graph_id, reason=reason)
