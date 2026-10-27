"""Runtime services of a graph run: concurrency, retries, spend, waits and halts (decision Q30).

Services are set by the consumer's project config (``.llm-kit/graphs.yaml``,
section ``services``), never by graph files::

    services:
      concurrency: {model-text: 4, model-image: 3, heavy: 1, sound: 2, tags: 1}
      retries: none
      spend:
        ledger_env: CONTENT_PIPELINE_SPEND_LEDGER   # the env var naming the campaign ledger file
        guards: [afterplay.beta.moment_caps]        # consumer cap guards, applied in order after the ledger
        models: {gpt-6-luna: 0, deepseek-v4.1-flash: 0.01}   # llm.model estimate per call, by catalogue row
      pause_on_halt: true

:func:`check_services` checks the section when the config loads.
:func:`build_services` turns it into the mapping a
:class:`~llm_kit.graph.run.Runner` takes (keys :data:`SERVICE_NAMES`), which
every op also sees as ``OpContext.services``. :func:`call_op` is how the
runner calls an op: it holds a concurrency slot of the op's kind, admits a
paid op's spend before the call and settles it after.

The rules are in ``docs/reference/llm-kit-graph-services.md``.
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

from .model import GraphError, Op, OpContext, OpResult

__all__ = [
    "RETRY_POLICIES",
    "SERVICE_KEYS",
    "SERVICE_NAMES",
    "SPEND_KEYS",
    "Limits",
    "build_services",
    "call_op",
    "check_services",
    "op_limit",
    "plan_estimate",
]

#: Keys of the ``services`` config section.
SERVICE_KEYS = ("concurrency", "retries", "spend", "pause_on_halt")

#: Keys of ``services.spend``.
SPEND_KEYS = ("ledger_env", "guards", "models")

#: The retry policies this build has. ``none``: a failed op fails its node; rerun the graph.
RETRY_POLICIES = ("none",)

#: Keys of the services mapping the runner reads.
SERVICE_NAMES = ("concurrency", "spend", "waits", "pause_on_halt")

_KIND_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _is_kind(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value[0].isalpha() and set(value) <= _KIND_RE_CHARS


def _usd(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and value >= 0)


def check_services(value: Any, where: str) -> dict:
    """Check the ``services`` section (one config layer); return it as a dict. Raise GraphError."""
    if not isinstance(value, Mapping):
        raise GraphError("services must be a mapping", where)
    out = dict(value)
    unknown = sorted(set(out) - set(SERVICE_KEYS))
    if unknown:
        raise GraphError(f"unknown services keys {unknown}; allowed: {list(SERVICE_KEYS)}", where)
    if "concurrency" in out:
        limits = out["concurrency"]
        if not isinstance(limits, Mapping):
            raise GraphError("concurrency must be a mapping of kind -> slots", f"{where}.concurrency")
        for kind, size in limits.items():
            if not _is_kind(kind):
                raise GraphError(f"concurrency kind {kind!r} must be a name (letters, digits, - _ .)",
                                 f"{where}.concurrency")
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise GraphError(f"concurrency {kind}: {size!r} is not an integer of at least 1",
                                 f"{where}.concurrency.{kind}")
    if "retries" in out and out["retries"] not in RETRY_POLICIES:
        raise GraphError(f"retries {out['retries']!r} is not supported; this build has {list(RETRY_POLICIES)} "
                         "(a failed op fails its node; rerun the graph)", f"{where}.retries")
    if "pause_on_halt" in out and not isinstance(out["pause_on_halt"], bool):
        raise GraphError("pause_on_halt must be true or false", f"{where}.pause_on_halt")
    if "spend" in out:
        spend = out["spend"]
        sw = f"{where}.spend"
        if not isinstance(spend, Mapping):
            raise GraphError("spend must be a mapping", sw)
        unknown = sorted(set(spend) - set(SPEND_KEYS))
        if unknown:
            raise GraphError(f"unknown spend keys {unknown}; allowed: {list(SPEND_KEYS)}", sw)
        env = spend.get("ledger_env")
        if env is not None and (not isinstance(env, str) or not env or not env.replace("_", "").isalnum()):
            raise GraphError(f"ledger_env {env!r} must be an environment variable name", f"{sw}.ledger_env")
        guards = spend.get("guards", [])
        if not isinstance(guards, list) or not all(isinstance(g, str) and "." in g for g in guards):
            raise GraphError("guards must be a list of dotted guard names (<namespace>.<name>)", f"{sw}.guards")
        if len(set(guards)) != len(guards):
            raise GraphError("guards names a guard twice", f"{sw}.guards")
        models = spend.get("models", {})
        if not isinstance(models, Mapping):
            raise GraphError("models must be a mapping of catalogue row id -> USD per call", f"{sw}.models")
        for row, usd in models.items():
            if not isinstance(row, str) or not row:
                raise GraphError(f"model row {row!r} must be a catalogue row id", f"{sw}.models")
            if not _usd(usd):
                raise GraphError(f"model {row}: {usd!r} is not a USD amount of at least 0", f"{sw}.models.{row}")
    return out


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


class Limits:
    """Per-kind concurrency slots for one process: ``kind -> slots``.

    An op holds one slot of its kind (:func:`op_limit`) for the whole call. A
    kind that is not configured is an error: the configured kinds are the
    complete set.
    """

    def __init__(self, sizes: Mapping[str, int]) -> None:
        check_services({"concurrency": dict(sizes)}, "Limits")
        self.sizes = dict(sizes)
        self._slots = {kind: threading.BoundedSemaphore(size) for kind, size in self.sizes.items()}

    @contextmanager
    def slot(self, kind: str) -> Iterator[None]:
        semaphore = self._slots.get(kind)
        if semaphore is None:
            raise GraphError(f"concurrency kind {kind!r} is not configured (services.concurrency has "
                             f"{sorted(self.sizes)})")
        with semaphore:
            yield


def op_limit(op: Op, params: Mapping[str, Any]) -> Optional[str]:
    """The concurrency kind of one call: the node's ``limit`` parameter when the op declares
    one and the node sets it (``llm.model`` image calls), else the op's ``limit``."""
    kind = params.get("limit") if "limit" in op.params else None
    if kind is None:
        return op.limit
    if not _is_kind(kind):
        raise GraphError(f"op {op.name}: limit {kind!r} is not a concurrency kind name")
    return kind


# ---------------------------------------------------------------------------
# building and calling
# ---------------------------------------------------------------------------


def build_services(config: Any, *, item: str, store: Any, env: Optional[Mapping[str, str]] = None) -> dict:
    """The services mapping of a run of ``item`` from the project config.

    ``concurrency`` (a :class:`Limits`) when configured; ``spend`` (a
    :class:`~llm_kit.graph.spend.SpendService`) when ``services.spend`` is
    configured, with the ledger named by ``ledger_env`` in ``env`` (default
    ``os.environ``) and the consumer guards; ``waits`` (a
    :class:`~llm_kit.graph.human.WaitService`) when ``runs.state`` is
    configured; ``pause_on_halt`` (default true). Guards are looked up in the
    namespaces the op registry imported, so load the registry first.
    """
    from .human import WaitService  # noqa: PLC0415 -- human and spend import the runner
    from .spend import SpendService  # noqa: PLC0415

    section = dict(config.services)
    out: dict[str, Any] = {"pause_on_halt": section.get("pause_on_halt", True)}
    if "concurrency" in section:
        out["concurrency"] = Limits(section["concurrency"])
    if "spend" in section:
        out["spend"] = SpendService.from_config(section["spend"], config=config, item=item, store=store,
                                                env=os.environ if env is None else env)
    if "state" in config.runs:
        out["waits"] = WaitService.for_item(config, item)
    return out


def plan_estimate(services: Mapping[str, Any], op: Op, ctx: OpContext, inputs: Mapping[str, Any]
                  ) -> tuple[Optional[float], str]:
    """A dry run's estimate for one paid call: ``(usd, "")`` or ``(None, why)``. Makes no call."""
    from .spend import SpendService  # noqa: PLC0415

    spend = services.get("spend")
    if not isinstance(spend, SpendService):
        return None, "no spend service"
    return spend.plan_estimate(op, ctx, dict(inputs))


def call_op(services: Mapping[str, Any], op: Op, ctx: OpContext, inputs: dict, record: dict
            ) -> tuple[Any, Optional[dict]]:
    """Call ``op`` under the services; return ``(result, stop)``.

    Holds a slot of the op's concurrency kind (when ``services`` has
    ``concurrency``). A paid op is admitted by the spend service before the
    call (a refusal raises :class:`~llm_kit.graph.spend.SpendRefused`, and
    the op is not called) and settled after it, whether it returned or
    raised; ``record["spend"]`` keeps the outcome. ``stop`` is a run stop the
    settlement asks for (``spend_overbilled``, ``spend_unreserved``): the
    node keeps its result and no new node starts.
    """
    from .spend import SpendService  # noqa: PLC0415

    limits = services.get("concurrency")
    if limits is not None and not isinstance(limits, Limits):
        raise GraphError(f"services['concurrency'] must be a Limits, not {type(limits).__name__}")
    kind = op_limit(op, ctx.params)
    spend = None
    if op.paid:
        spend = services.get("spend")
        if spend is None:
            raise GraphError(f"op {op.name} is paid; a paid op needs the spend service "
                             "(services['spend'], llm_kit.graph.services), which this runner was not given")
        if not isinstance(spend, SpendService):
            raise GraphError(f"services['spend'] must be a SpendService, not {type(spend).__name__}")
    with (limits.slot(kind) if limits is not None and kind is not None else nullcontext()):
        if spend is None:
            return op.fn(ctx, **inputs), None
        admission = spend.admit(op, ctx, inputs)
        record["spend"] = admission.document()
        try:
            result = op.fn(ctx, **inputs)
        except BaseException as exc:
            record["spend"] = spend.settle_error(admission, exc)
            raise
        receipt = result.receipt if isinstance(result, OpResult) else None
        record["spend"], stop = spend.settle(admission, receipt)
        return result, stop
