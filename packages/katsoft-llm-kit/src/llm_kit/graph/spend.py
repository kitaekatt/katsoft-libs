"""Spend admission and settlement for paid ops: the campaign ledger and cap guards.

Every op with ``paid=True`` is admitted before its call and settled after it
(:func:`llm_kit.graph.services.call_op`):

1. Estimate. The op's ``estimate(ctx, **inputs) -> USD``; for ``llm.model``,
   the ``services.spend.models`` entry of the catalogue row its selection key
   resolves to (``0`` declares the row unmetered, such as a subscription
   harness). A paid call with no estimate is refused before any call.
2. Admit, when the estimate is above 0: the campaign ledger first
   (:class:`~llm_kit.pipeline.runtime.spend_ledger.SpendLedger`, the file
   named by the ``ledger_env`` environment variable; its cap is fixed when the
   file is created), then each consumer :class:`CapGuard` in config order. A
   refusal releases what was admitted, marks the node ``refused`` and stops
   the run (reason ``spend_cap``): no new node starts.
3. Settle. A returned call settles with the receipt's ``cost.usd`` (a
   ``local`` or ``subscription`` cost class is 0); a receipt without a cost
   holds the reservation as ``unknown`` at the estimate. A call that raised
   settles the same way from the error's receipt, except an error that sent
   nothing (an ``OpError`` with no receipt and a ``refused...`` code, such as
   ``llm.model``'s pre-dispatch refusals), which releases. A cost above the
   reservation is ``overbilled``; a cost on a call estimated at 0 is
   ``unreserved`` and halts the ledger. Both stop the run.

A consumer guard is declared in a configured op namespace::

    from llm_kit.graph.spend import cap_guard, SpendRefused

    @cap_guard("afterplay.beta.moment_caps")
    def moment_caps(setup):            # a GuardSetup; return an object with admit, settle, release
        return MomentCaps(setup)

``admit(charge) -> token`` raises :class:`SpendRefused` to refuse;
``settle(token, charge, cost_usd)`` (``cost_usd`` is None when unknown) and
``release(token, charge)`` close it. The node record's ``spend`` (see
:meth:`Admission.document`) is what a guard reads to count earlier spend of
the item (``setup.store``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from llm_kit.pipeline.runtime.spend_ledger import (
    LEDGER_PATH_ENV,
    Reservation,
    SpendCapExceeded,
    SpendLedger,
    SpendLedgerHalted,
    open_ledger,
)

from .model import GraphError, Op, OpContext
from .ops.registry import OpError
from .run import NodeStopped

__all__ = [
    "GRAPH_GUARD_ATTRIBUTE",
    "MODEL_OP",
    "Admission",
    "CapGuard",
    "Charge",
    "GuardSetup",
    "SpendRefused",
    "SpendService",
    "cap_guard",
    "cost_from_receipt",
    "load_guard",
]

#: The attribute :func:`cap_guard` sets on the decorated factory.
GRAPH_GUARD_ATTRIBUTE = "__graph_guard__"

#: The op whose estimate comes from ``services.spend.models``.
MODEL_OP = "llm.model"


class SpendRefused(NodeStopped):
    """A paid call was refused before it was made: the node is ``refused`` and the run stops.

    ``reason``: ``spend_cap`` (the ledger cap, the ledger's halt or a guard),
    ``no_estimate`` (a paid call without an estimate) or ``no_ledger`` (a
    reservation with no ledger configured).
    """

    def __init__(self, message: str, *, reason: str = "spend_cap",
                 detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(reason, message, status="refused", detail=detail)


@dataclass(frozen=True)
class Charge:
    """One paid call as the ledger and the guards see it."""

    run_id: str
    item: Optional[str]
    graph_id: str
    node_path: str
    op: str
    amount_usd: float
    model: str = ""

    @property
    def identifier(self) -> str:
        return f"{self.run_id}/{self.node_path}"

    @property
    def scope(self) -> str:
        return f"{self.item}/{self.graph_id}"


@dataclass(frozen=True)
class GuardSetup:
    """What a guard factory gets: the project, the item and its run store."""

    project_root: Path
    item: str
    config: Any
    store: Any


class CapGuard:
    """The interface of a consumer cap guard (duck-typed; subclassing is optional)."""

    name: str = ""

    def admit(self, charge: Charge) -> Any:  # pragma: no cover - interface
        """Return a token, or raise :class:`SpendRefused`."""
        raise NotImplementedError

    def settle(self, token: Any, charge: Charge, cost_usd: Optional[float]) -> None:  # pragma: no cover
        raise NotImplementedError

    def release(self, token: Any, charge: Charge) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def cap_guard(name: str) -> Callable[[Callable[[GuardSetup], Any]], Callable[[GuardSetup], Any]]:
    """Declare ``factory(setup: GuardSetup) -> guard`` as the cap guard ``name`` (``<namespace>.<name>``)."""
    if not isinstance(name, str) or name.count(".") < 1 or not all(name.split(".")):
        raise GraphError(f"guard name {name!r} must be dotted: <namespace>.<name>")

    def decorate(factory: Callable[[GuardSetup], Any]) -> Callable[[GuardSetup], Any]:
        if not callable(factory):
            raise GraphError(f"guard {name}: {factory!r} is not callable")
        setattr(factory, GRAPH_GUARD_ATTRIBUTE, name)
        return factory

    return decorate


def load_guard(config: Any, name: str, setup: GuardSetup) -> Any:
    """Build the guard ``name`` from the namespace the op registry imported for it."""
    import sys  # noqa: PLC0415

    from .ops.registry import _module_name  # noqa: PLC0415 -- the namespace's synthetic module name

    namespace = config.namespace_of(name)
    if namespace is None:
        raise GraphError(f"guard {name!r} is not in a configured namespace ({sorted(config.namespaces)})")
    prefix = _module_name(namespace, Path(config.namespaces[namespace]).resolve())
    if prefix not in sys.modules:
        raise GraphError(f"guard {name!r}: namespace {namespace!r} is not imported; load the op registry first")
    found = []
    for module_name, module in sorted(sys.modules.items()):
        if module_name != prefix and not module_name.startswith(prefix + "."):
            continue
        for value in vars(module).values():
            if callable(value) and getattr(value, GRAPH_GUARD_ATTRIBUTE, None) == name \
                    and str(getattr(value, "__module__", "")).startswith(prefix) and value not in found:
                found.append(value)
    if len(found) != 1:
        raise GraphError(f"guard {name!r}: {'no' if not found else len(found)} factories declared with "
                         "llm_kit.graph.spend.cap_guard in its namespace")
    guard = found[0](setup)
    for method in ("admit", "settle", "release"):
        if not callable(getattr(guard, method, None)):
            raise GraphError(f"guard {name!r}: the factory returned {type(guard).__name__}, which has no {method}()")
    return guard


def cost_from_receipt(receipt: Optional[Mapping[str, Any]]) -> Optional[float]:
    """The USD a call cost by its receipt: ``cost.usd``; 0 for a ``local`` or
    ``subscription`` cost class; else None (unknown)."""
    if not isinstance(receipt, Mapping):
        return None
    cost = receipt.get("cost")
    if not isinstance(cost, Mapping):
        return None
    usd = cost.get("usd")
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd < 0:
            raise GraphError(f"receipt cost.usd {usd!r} is not a USD amount")
        return float(usd)
    if cost.get("class") in ("local", "subscription"):
        return 0.0
    return None


@dataclass
class Admission:
    """One admitted paid call; :meth:`document` is the node record's ``spend``."""

    charge: Charge
    source: str
    reservation: Optional[Reservation] = None
    guards: list = field(default_factory=list)   # [(name, guard, token)]
    state: str = "open"
    cost_usd: Optional[float] = None

    def document(self) -> dict:
        return {
            "estimate_usd": self.charge.amount_usd,
            "estimate_source": self.source,
            "model": self.charge.model or None,
            "reservation": self.reservation.id if self.reservation is not None else None,
            "guards": [name for name, _, _ in self.guards],
            "state": self.state,
            "cost_usd": self.cost_usd,
        }


class SpendService:
    """Spend admission and settlement for the paid ops of one runner.

    ``ledger`` is the campaign ledger (None: no reservation can be made, so a
    call estimated above 0 is refused). ``guards`` are ``(name, guard)`` pairs
    applied in order after the ledger. ``models`` maps a catalogue row id to
    the USD estimate of one ``llm.model`` call on it.
    """

    def __init__(self, *, ledger: Optional[SpendLedger] = None, guards: Sequence[tuple[str, Any]] = (),
                 models: Optional[Mapping[str, float]] = None, ledger_env: str = LEDGER_PATH_ENV) -> None:
        self.ledger = ledger
        self.guards = list(guards)
        self.models = dict(models or {})
        self.ledger_env = ledger_env
        self._rows: dict[tuple[str, str], str] = {}

    @classmethod
    def from_config(cls, section: Mapping[str, Any], *, config: Any, item: str, store: Any,
                    env: Mapping[str, str]) -> "SpendService":
        """From ``services.spend``: the ledger named by ``ledger_env`` in ``env`` (absent when unset)."""
        ledger_env = section.get("ledger_env") or LEDGER_PATH_ENV
        raw = (env.get(ledger_env) or "").strip()
        ledger = open_ledger(raw) if raw else None
        setup = GuardSetup(project_root=Path(config.root), item=item, config=config, store=store)
        guards = [(name, load_guard(config, name, setup)) for name in section.get("guards", [])]
        return cls(ledger=ledger, guards=guards, models=section.get("models", {}), ledger_env=ledger_env)

    # -- estimates ------------------------------------------------------------

    def _model_row(self, project_root: Path, selection: str) -> str:
        key = (str(project_root), selection)
        if key not in self._rows:
            from llm_kit.registry import project_models  # noqa: PLC0415

            resolved = project_models.read_resolved_models(project_root=str(project_root))
            chosen = (resolved.selections.get(selection) or {}).get("effective")
            if chosen is None:
                raise GraphError(f"selection {selection!r} is not in the project's model file")
            self._rows[key] = chosen["model"]
        return self._rows[key]

    def estimate(self, op: Op, ctx: OpContext, inputs: Mapping[str, Any]) -> tuple[float, str, str]:
        """``(usd, source, model row)`` of one call; raise :class:`SpendRefused` (``no_estimate``)."""
        model = ""
        if op.estimate is not None:
            usd = op.estimate(ctx, **dict(inputs))
            source = f"op {op.name}"
        elif op.name == MODEL_OP:
            model = self._model_row(ctx.project_root, ctx.params["model"])
            if model not in self.models:
                raise SpendRefused(f"llm.model {ctx.node_path}: no estimate for model row {model!r} "
                                   f"(selection {ctx.params['model']!r}); add it to services.spend.models "
                                   "(0 declares it unmetered)", reason="no_estimate", detail={"model": model})
            usd = self.models[model]
            source = f"services.spend.models.{model}"
        else:
            raise SpendRefused(f"op {op.name} at {ctx.node_path} is paid and declares no estimate",
                               reason="no_estimate")
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd < 0:
            raise GraphError(f"{source}: estimate {usd!r} is not a USD amount of at least 0")
        return float(usd), source, model

    def plan_estimate(self, op: Op, ctx: OpContext, inputs: Mapping[str, Any]) -> tuple[Optional[float], str]:
        try:
            usd, _, _ = self.estimate(op, ctx, inputs)
        except SpendRefused as exc:
            return None, exc.message
        return usd, ""

    # -- admission --------------------------------------------------------------

    def admit(self, op: Op, ctx: OpContext, inputs: Mapping[str, Any]) -> Admission:
        """Estimate and admit one call, or raise :class:`SpendRefused` having admitted nothing."""
        usd, source, model = self.estimate(op, ctx, inputs)
        charge = Charge(run_id=ctx.run_id, item=ctx.item, graph_id=ctx.graph_id, node_path=ctx.node_path,
                        op=op.name, amount_usd=usd, model=model)
        admission = Admission(charge=charge, source=source)
        if usd == 0:
            admission.state = "free"
            return admission
        if self.ledger is None:
            raise SpendRefused(f"{op.name} at {ctx.node_path} is estimated at ${usd:.6f} and needs the spend "
                               f"ledger named by ${self.ledger_env}, which is unset", reason="no_ledger",
                               detail={"estimate_usd": usd})
        try:
            admission.reservation = self.ledger.reserve(usd, scope=charge.scope, identifier=charge.identifier,
                                                        model=model or op.name)
        except SpendLedgerHalted as exc:
            raise SpendRefused(f"{op.name} at {ctx.node_path}: the spend ledger is halted "
                               f"({exc.reason}: {exc.detail})", detail=self._detail(usd, "ledger")) from None
        except SpendCapExceeded as exc:
            raise SpendRefused(f"{op.name} at {ctx.node_path}: ${usd:.6f} does not fit the ledger cap "
                               f"(${exc.measured:.6f} of ${exc.budget:.2f})",
                               detail=self._detail(usd, "ledger")) from None
        for name, guard in self.guards:
            try:
                token = guard.admit(charge)
            except SpendRefused as exc:
                self._release(admission)
                detail = {**self._detail(usd, name), **exc.detail}
                raise SpendRefused(f"{op.name} at {ctx.node_path}: guard {name} refused: {exc.message}",
                                   reason=exc.reason, detail=detail) from None
            except BaseException:
                self._release(admission)
                raise
            admission.guards.append((name, guard, token))
        return admission

    def _detail(self, usd: float, by: str) -> dict:
        detail: dict[str, Any] = {"estimate_usd": usd, "refused_by": by}
        if self.ledger is not None:
            status = self.ledger.status()
            detail["ledger"] = {"cap_usd": status.cap_usd, "outstanding_usd": status.outstanding_usd,
                                "remaining_usd": status.remaining_usd}
        return detail

    def _release(self, admission: Admission) -> None:
        for name, guard, token in reversed(admission.guards):
            guard.release(token, admission.charge)
        if admission.reservation is not None:
            self.ledger.release(admission.reservation)
        admission.state = "released"
        admission.cost_usd = 0.0

    # -- settlement -------------------------------------------------------------

    def settle(self, admission: Admission, receipt: Optional[Mapping[str, Any]]) -> tuple[dict, Optional[dict]]:
        """Settle a call that returned; return ``(spend document, run stop or None)``."""
        return self._settle(admission, cost_from_receipt(receipt))

    def settle_error(self, admission: Admission, exc: BaseException) -> dict:
        """Settle a call that raised; return the spend document. Never raises for the money itself."""
        if isinstance(exc, OpError) and exc.receipt is None and str(exc.code).startswith("refused"):
            if admission.state != "free":
                self._release(admission)
            return admission.document()
        receipt = exc.receipt if isinstance(exc, OpError) else None
        document, _ = self._settle(admission, cost_from_receipt(receipt))
        return document

    def _settle(self, admission: Admission, cost: Optional[float]) -> tuple[dict, Optional[dict]]:
        charge = admission.charge
        if admission.state == "free":
            if cost is not None and cost > 0:
                admission.state = "unreserved"
                admission.cost_usd = cost
                message = (f"{charge.op} at {charge.node_path} cost ${cost:.6f} on a call estimated at $0 "
                           f"({admission.source})")
                if self.ledger is not None:
                    self.ledger.halt("unreserved", message)
                return admission.document(), {"reason": "spend_unreserved", "message": message,
                                              "cost_usd": cost}
            admission.cost_usd = 0.0
            return admission.document(), None
        stop = None
        try:
            self.ledger.settle(admission.reservation, cost)
            admission.state = "unknown" if cost is None else "settled"
        except SpendCapExceeded:
            admission.state = "overbilled"
            stop = {"reason": "spend_overbilled", "cost_usd": cost,
                    "message": f"{charge.op} at {charge.node_path} cost ${cost:.6f}, above its reservation of "
                               f"${charge.amount_usd:.6f}; the ledger is halted"}
        admission.cost_usd = cost
        for name, guard, token in admission.guards:
            guard.settle(token, charge, cost)
        return admission.document(), stop
