"""The one API over a model declaration: ``describe`` and ``run``.

A model declaration is a list of registry ids naming which model(s) may do a
unit of work (the format is specified in bootstrap's plugin-dev skill,
``references/model-declaration.md``, and validated structurally by
``bootstrap_lib.model_declaration``). This module answers the runtime
question the validator deliberately does not: on THIS machine, for THIS
caller, which declared entries can run, in what order, and what happens when
none can.

Three surfaces, kept apart:

- **RENDER** -- :func:`describe` returns the rendered subset: usable entries,
  out-of-quota entries (with their reset time) and unreachable entries, which
  are all real on this machine. Unresolved, unroutable-here,
  requirements-mismatch, excluded and shadowed-core entries are HIDDEN.
  Entries switched off by the cost-class ``allow`` filter (or a consent
  policy) are SHOWN, never hidden: :attr:`Ranking.switched_off` lists them, and :meth:`Ranking.render`
  and :meth:`Ranking.to_json` name them (and only when there are any).
- **SKIP** -- selection passes over every non-usable entry silently: no
  notice, no warning, no log line.
- **FLOOR** -- when no usable rendered entry remains, :func:`describe` raises
  :class:`NoUsableRoutingTarget`, which itemises EVERY declared entry and its
  disposition in declaration order. That is where a typo surfaces.

Ordering is one rule for every caller (:func:`order_by_pace`): entries with a
pace reading are re-sorted by pace, highest first, among their own working
positions; entries without one keep their places; ties keep declaration
order. The first usable entry of the ordered list is the default. An
unattended caller takes it; an in-session caller may choose any usable entry
and announces the choice.

A ``frontdoor: true`` entry stands for its routing group (R39): when the front
door is not usable, its group members follow it in ``routing.order``, each
classified like a declared entry and marked ``via`` the front door, and pace
never reorders a member across its group's boundary. A failed call moves its
unit on (R40, R41) only for the reasons :func:`fallthrough_reason` names; the
callers that own their call share :class:`Reselector` and
:func:`call_with_fallthrough` for it.

``pace = remaining / window_remaining``: 100% is exactly on pace. The number
comes from an UNPINNED read at render time, while the status, ``usable`` and
the default come from the session-pinned verdict
(:func:`~.usage_budget.pinned_evaluate`).
"""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from llm_kit import _runtime
from llm_kit.registry import cost as _cost
from llm_kit.registry.adapter_names import (
    ADAPTER_CLAUDE_CLI,
    ADAPTER_CODEX_CLI,
    ADAPTER_OPENAI_COMPAT,
    ADAPTER_OPENCODE_CLI,
    canonical_adapter_name_or_none,
    lookup_by_adapter,
)
from llm_kit.registry.constants import cli_command
from llm_kit.completion.contract import OutputContractViolation, contract_requirements, merge_requirements
from llm_kit.completion.halt import (
    ENDPOINT_TIMEOUT,
    ENDPOINT_UNREACHABLE,
    HALT_BACKPRESSURE,
    HALT_INSUFFICIENT_CREDIT,
    HALT_QUOTA,
    classify_backpressure,
    classify_endpoint_failure,
    is_frontdoor_group_exhausted,
)
from llm_kit.completion.skill_context import skill_context_requirements
from llm_kit.completion.types import BackendOptions
from llm_kit.dispatch.observer import ExecutionObserver
from llm_kit.registry.model_endpoints import (
    HARNESS_KIND,
    TRANSPORT_KIND,
    EndpointEntry,
    EndpointRegistryError,
    frontdoor_group_members,
)
from llm_kit.registry.models import EndpointResolveError, claude_cli_alias
from llm_kit.dispatch.reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    Reachability,
    check_many,
)
from llm_kit.dispatch.usage_budget import (
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    POOL_SEVEN_DAY,
    Budget,
    ConserveSpec,
    evaluate,
    pinned_evaluate,
    quota_pool_key,
    record_observed_halt,
)
from llm_kit.registry.declaration_syntax import (
    _MODEL_DECLARATION_BOOTSTRAP,
    DeclarationSupportError,
    _model_declaration,
)


CALLER_SESSION = "session"
CALLER_PROCESS = "process"
_CALLERS = (CALLER_SESSION, CALLER_PROCESS)

DISPOSITION_USABLE = "usable"
DISPOSITION_OUT_OF_QUOTA = "out-of-quota"
DISPOSITION_UNREACHABLE = "unreachable"
DISPOSITION_UNRESOLVED = "unresolved"
DISPOSITION_UNROUTABLE = "unroutable"
DISPOSITION_REQUIREMENTS_MISMATCH = "requirements-mismatch"
DISPOSITION_EXCLUDED = "excluded"
DISPOSITION_SHADOWED_CORE = "shadowed-core"
DISPOSITION_SWITCHED_OFF = "switched-off"
#: Dispositions that stay in the render. Everything else is hidden.
_RENDERED = (DISPOSITION_USABLE, DISPOSITION_OUT_OF_QUOTA, DISPOSITION_UNREACHABLE)

#: Harnesses a process caller drives through the completion seam, and the
#: adapter family name each one's backend carries.
_HARNESS_ADAPTERS = {
    "claude": ADAPTER_CLAUDE_CLI, "codex": ADAPTER_CODEX_CLI, "opencode": ADAPTER_OPENCODE_CLI,
}

#: How an in-session caller drives each harness (D1's caller table).
_SESSION_DRIVE = {"claude": "agent", "codex": "codex exec", "opencode": "opencode run"}

#: Entry kinds a session caller may declare it can dispatch beyond the harness
#: drives above. A transport has no agent loop, so a session caller hides it by
#: default; a caller that reaches transports through a runner of its own (the
#: code-review lane runner) passes ``dispatchable=("transport",)``.
DISPATCHABLE_TRANSPORT = TRANSPORT_KIND
_DISPATCHABLE_KINDS = (DISPATCHABLE_TRANSPORT,)

#: Below this fraction of the window left, a pace is not meaningful.
_ABOUT_TO_RESET = 0.01


#: The bootstrap release that shipped ``bootstrap_lib.execution_event``.
EXECUTION_EVENT_BOOTSTRAP = "0.135.0"

#: The envelope schema ``run`` emits; the probe requires the linked module to
#: support it.
_REQUIRED_EXECUTION_SCHEMA = "plugins-kit.execution-event/v1"

#: The exact keywords ``run`` passes to ``usage_payload``.
_USAGE_KEYWORDS = ("input_tokens", "output_tokens", "cache_hit_tokens", "total_tokens")

RULE_CHOICE_SESSION = (
    "Rule: any usable entry may be chosen; the default applies only when you "
    "have no preference. Announce every choice from a multi-entry declaration: "
    '"route: <unit> -> <entry>; <reason>", reason one of default, higher pace, '
    "independence, <prior entry> failed: <kind>, or your own clause."
)
RULE_CHOICE_PROCESS = (
    "Rule: take the first usable entry of the ordered list (the default); a run "
    "is explainable from the declared list plus the logged pace readings."
)


def rule_trigger_session() -> str:
    """The session re-select rule, spelled with the CLI command valid right now.

    A function because the record-halt command differs under a redirected data
    root (:func:`constants.cli_command`); the rendered rule uses this, and
    :data:`RULE_TRIGGER_SESSION` is its value for the process's own environment.
    """
    return (
        "Re-select: any dispatch failure the launch-correction rule does not "
        "explain -- a non-zero exit, an Agent-tool error, a launch that produced no "
        "output -- moves to another usable entry; announce it with the failure kind "
        '(a classified marker, else "unexplained exit"). A schema-invalid or wrong '
        "result from a run that exited 0 is a task failure, not a trigger. Before "
        "re-selecting a unit that may have written, reset its workspace to its "
        "launch state or re-run it in a fresh worktree; read-only review lanes skip "
        "this step. A quota or credit halt (a usage-limit or out-of-credits error) "
        f"is written back first: run `{cli_command()} models record-halt <entry>` before "
        "re-selecting or re-running describe with --exclude, so every later render "
        "in this session shows that entry out of quota."
    )


RULE_TRIGGER_SESSION = rule_trigger_session()
RULE_TRIGGER_PROCESS = (
    "Re-select: only a classified halt (quota, rate limit, auth, insufficient "
    "credit, launch/transport) moves to the next usable entry; a quota or "
    "credit halt records the entry out of quota until its reset. A task error, "
    "schema-invalid output, or an unclassified timeout stays a failed attempt."
)
RULE_INDEPENDENCE = (
    "Independence: prefer a non-author entry; the author may review when no "
    "other usable entry exists, and says so."
)


def _execution_event() -> Any:
    """Return ``bootstrap_lib.execution_event``, probed for what ``run`` calls.

    Reached only when a caller passed an observer, which is REFUSE: emitting
    usage without ``usage_payload`` would state an unknown count as zero, and
    emitting nothing would drop the record the caller asked for. Absent and
    too-old are diagnosed apart because their remedies differ; the version in
    a message is this plugin's own constant, never read from the module.
    """
    if _runtime.mode() == "package":
        # Package mode: the routed contract, unprobed.
        return _runtime.load("execution_event", error=DeclarationSupportError)
    try:
        import bootstrap_lib  # noqa: F401, PLC0415
    except ModuleNotFoundError as exc:
        raise DeclarationSupportError(
            "run(observer=...) needs bootstrap_lib.execution_event, but bootstrap_lib is "
            "not importable here; run `claude plugin install bootstrap@plugins-kit` "
            f"(needs bootstrap >= {EXECUTION_EVENT_BOOTSTRAP})"
        ) from exc
    too_old = (
        "the linked bootstrap_lib does not support the execution-event calls "
        f"run(observer=...) makes; it needs bootstrap >= {EXECUTION_EVENT_BOOTSTRAP} -- "
        "run `claude plugin update bootstrap@plugins-kit`, then restart the session"
    )
    try:
        module = importlib.import_module("bootstrap_lib.execution_event")
    except ImportError as exc:
        raise DeclarationSupportError(too_old) from exc
    supported = getattr(module, "SUPPORTED_SCHEMAS", None)
    usage_payload = getattr(module, "usage_payload", None)
    if (
        not isinstance(supported, (set, frozenset))
        or _REQUIRED_EXECUTION_SCHEMA not in supported
    ):
        raise DeclarationSupportError(too_old)
    try:
        inspect.signature(usage_payload).bind(**{name: 0 for name in _USAGE_KEYWORDS})
    except (TypeError, ValueError) as exc:
        raise DeclarationSupportError(too_old) from exc
    return module


def check_registry_entry(entry_id: str, merged: Any) -> Optional[str]:
    """Classify a reserved core id whose MERGED entry is not a Claude harness.

    ``fable``, ``opus``, ``sonnet`` and ``haiku`` are reserved for
    ``harness: claude``. A config layer or registry entry of the same id
    shadows the shipped one, so a merged entry carrying another harness or a
    ``base_url`` is shadowed and cannot route as the core id. Returns the
    reason, or None when the entry is fine, absent, or not a core id.

    It classifies; it never raises. The finding reaches the user through the
    floor diagnostic only. The check reads the MERGED entry because a partial
    layer is legitimate: an owner entry carrying only ``conserve_usage``
    merges over the shipped harness entry and is still a Claude harness.
    """
    if merged is None or entry_id not in _model_declaration().CORE_IDS:
        return None
    base_url = getattr(merged, "base_url", None)
    harness = (getattr(merged, "harness", None) or "").strip().lower()
    if base_url:
        return f"reserved core id shadowed by a transport entry (base_url {base_url})"
    if harness != "claude":
        return f"reserved core id shadowed by harness {harness or '<none>'!r} (must be claude)"
    return None


def order_by_pace(
    items: Sequence[Any], *, pace: Optional[Callable[[Any], Optional[float]]] = None
) -> List[Any]:
    """Apply D5's one ordering rule. Pure; returns a new list.

    Items with a pace are re-sorted by pace, highest first, among the
    positions paced items occupy; items without a pace keep their positions;
    ties keep the input (declaration) order.
    """
    read = pace or (lambda item: getattr(item, "pace", None))
    ordered = list(items)
    paced_slots = [index for index, item in enumerate(ordered) if read(item) is not None]
    paced = sorted(
        (ordered[index] for index in paced_slots),
        key=lambda item: -float(read(item)),  # sorted() is stable: ties keep order
    )
    for slot, item in zip(paced_slots, paced):
        ordered[slot] = item
    return ordered


def _order_blocks(states: Sequence[Any]) -> List[Any]:
    """:func:`order_by_pace` over declaration blocks, never across one.

    A block is one declared entry plus the front-door group members expanded
    after it (all share its ``declared_index``). Blocks move by the pace of
    their declared entry; inside a block the members keep their walk order
    (``routing.order``), so a paced member can never overtake its front door
    or a later declared entry. ``states`` arrive in slot order. Without a
    front door every block is one entry and this is :func:`order_by_pace`.
    """
    blocks: Dict[int, List[Any]] = {}
    for state in states:
        blocks.setdefault(state.declared_index, []).append(state)
    ordered_blocks = order_by_pace(
        [blocks[index] for index in sorted(blocks)],
        pace=lambda block: block[0].pace if block[0].via is None else None,
    )
    return [state for block in ordered_blocks for state in block]


def _format_reset(resets_at: Optional[int]) -> str:
    if resets_at is None:
        return "an unknown time"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(resets_at))


@dataclass(frozen=True)
class Disposition:
    """What happened to one declared entry. Read only by floor handling.

    ``via`` is set on a front-door group member that :func:`describe` added
    after its front-door entry (R39); ``declared_index`` is then that front
    door's index. None for every declared entry.
    """

    id: str
    declared_index: int
    disposition: str
    detail: str = ""
    resets_at: Optional[int] = None
    cost_class: Optional[str] = None
    via: Optional[str] = None

    def describe_line(self) -> str:
        text = self.disposition
        if self.disposition == DISPOSITION_OUT_OF_QUOTA:
            text += f" until {_format_reset(self.resets_at)}"
        line = f"{self.id}: {text}" + (f" ({self.detail})" if self.detail else "")
        return line + (f" [via {self.via}]" if self.via else "")

    def to_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "declared_index": self.declared_index,
            "disposition": self.disposition,
            "detail": self.detail,
            "resets_at": self.resets_at,
        }
        if self.cost_class is not None:
            data["cost_class"] = self.cost_class
        if self.via is not None:
            data["via"] = self.via
        return data


@dataclass(frozen=True)
class EntryState:
    """One rendered entry. ``usable`` gates selection; the rest is information.

    ``via`` names the front-door entry a group member was expanded from (R39);
    None for a declared entry.
    """

    id: str
    declared_index: int
    resolved: bool
    kind: str
    harness: Optional[str]
    model: Optional[str]
    family: Optional[str]
    tier: Optional[int]
    drive: str
    reachability: str
    usability: Optional[str]
    pace: Optional[float]
    shares_quota_with: tuple = ()
    usable: bool = False
    default: bool = False
    is_self: bool = False
    pace_note: str = ""
    remaining: Optional[float] = None
    window_remaining: Optional[float] = None
    resets_at: Optional[int] = None
    pool: Optional[str] = None
    reachability_detail: str = ""
    via: Optional[str] = None

    @property
    def status_text(self) -> str:
        if self.usability == STATUS_OUT_OF_QUOTA:
            return f"out of quota until {_format_reset(self.resets_at)}"
        if self.reachability == STATUS_UNREACHABLE:
            return f"unreachable ({self.reachability_detail})" if self.reachability_detail else "unreachable"
        if self.usability is None:
            return "n/a (unpaced)"
        label = {
            STATUS_AVAILABLE: "available",
            STATUS_UNDER_QUOTA: "under quota",
            STATUS_NO_DATA: "n/a",
        }.get(self.usability, self.usability)
        if self.pace is not None and self.remaining is not None and self.window_remaining is not None:
            return (
                f"{label}  {self.remaining * 100:.0f}% left, "
                f"{self.window_remaining * 100:.0f}% of window   pace {self.pace * 100:.0f}%"
            )
        if self.pace_note:
            return f"{label}  {self.pace_note}"
        return label

    def to_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "declared_index": self.declared_index,
            "resolved": self.resolved,
            "kind": self.kind,
            "harness": self.harness,
            "model": self.model,
            "family": self.family,
            "tier": self.tier,
            "drive": self.drive,
            "reachability": self.reachability,
            "usability": self.usability,
            "pace": self.pace,
            "pace_note": self.pace_note,
            "resets_at": self.resets_at,
            "shares_quota_with": list(self.shares_quota_with),
            "usable": self.usable,
            "default": self.default,
            "is_self": self.is_self,
        }
        if self.via is not None:
            data["via"] = self.via
        return data


@dataclass(frozen=True)
class CostedEntryState(EntryState):
    """An :class:`EntryState` that also reports its registry cost class and the reason for it.

    :func:`describe` builds these only for ``discovery=True``; the classification
    is information and is independent of any consent. A separate class keeps the
    base state, and everything built without ``discovery``, unchanged.
    """

    cost_class: Optional[str] = None
    cost_reason: str = ""

    def to_json(self) -> Dict[str, Any]:
        data = super().to_json()
        data["cost"] = {"class": self.cost_class, "reason": self.cost_reason}
        return data


class NoUsableRoutingTarget(Exception):
    """The floor: no usable rendered entry remains.

    Carries EVERY declared entry and its disposition in declaration order,
    including the hidden ones. It is the only selection error; every caller
    propagates it. An entry the cost-class ``allow`` filter switched off
    appears here like any other, with its cost class and the reason; the ids
    of those entries are :attr:`switched_off_ids`.
    """

    def __init__(self, names: Sequence[str], dispositions: Sequence[Disposition], caller: str) -> None:
        self.names = tuple(names)
        self.dispositions = tuple(dispositions)
        self.caller = caller
        lines = "\n".join("  " + d.describe_line() for d in self.dispositions)
        super().__init__(
            f"no usable routing target in [{', '.join(self.names)}] "
            f"for a {caller} caller:\n{lines}"
        )

    @property
    def switched_off_ids(self) -> tuple:
        """Ids of the entries the cost-class ``allow`` filter switched off, in declaration order."""
        return tuple(
            d.id for d in self.dispositions if d.disposition == DISPOSITION_SWITCHED_OFF
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "kind": "no-usable-routing-target",
            "message": str(self),
            "caller": self.caller,
            "dispositions": [d.to_json() for d in self.dispositions],
        }


@dataclass(frozen=True)
class Ranking:
    """The result of :func:`describe`.

    ``names`` and ``dispositions`` cover EVERY declared id, hidden ones
    included. They stay in memory for callers that build their own floor
    diagnostic and are never emitted: ``repr``, :meth:`render` and
    :meth:`to_json` name rendered entries only. Only
    :class:`NoUsableRoutingTarget` may name a hidden id (RENDER, SKIP and
    FLOOR are separate surfaces).

    ``switched_off`` holds the entries the cost-class ``allow`` filter
    switched off. They are SHOWN, not hidden: when it is non-empty,
    :meth:`render` adds one ``Switched off:`` line and :meth:`to_json` adds a
    ``"switched_off"`` key; when it is empty (always, without ``allow``) the
    output is byte-identical to a build without the filter.

    ``expanded_names`` is the declaration with every front-door entry followed
    by its group members (R39), deduplicated: the full ordered set of ids a
    unit may be dispatched to, whatever is usable now. It is never emitted
    (it may carry hidden ids); :func:`run` sizes its default attempt limit
    from it.
    """

    names: tuple = field(repr=False)
    caller: str = ""
    rendered_entries: tuple = ()
    dispositions: tuple = field(default=(), repr=False)
    rule: str = ""
    # Entries switched off by the cost-class allow filter. They are SHOWN (render
    # and to_json name them when non-empty), never hidden; empty leaves output unchanged.
    switched_off: tuple = field(default=(), repr=False)
    expanded_names: tuple = field(default=(), repr=False)

    @property
    def default(self) -> Optional[EntryState]:
        return next((entry for entry in self.rendered_entries if entry.default), None)

    def render(self) -> str:
        width = max(len(entry.id) for entry in self.rendered_entries)
        # The header names no ids: a hidden entry must not appear anywhere in
        # the render (direction 17); the floor is where hidden ids surface.
        lines = ["Declared models (ordered by pace): choose one; default is marked."]
        for entry in self.rendered_entries:
            if entry.kind == TRANSPORT_KIND:
                how = f"transport/{entry.drive}"
            elif (entry.harness or "") == "claude":
                how = f"claude/{entry.drive}"
            else:
                how = entry.harness or entry.kind
            marks = " ".join(
                mark for mark, on in (("[default]", entry.default), ("[author]", entry.is_self)) if on
            )
            line = f"  {entry.id:<{width}}  {how:<14} {entry.status_text}"
            if marks:
                line += f"   {marks}"
            if entry.shares_quota_with:
                line += f"  shares {entry.pool} with {', '.join(entry.shares_quota_with)}"
            if entry.via:
                line += f"  via {entry.via}"
            lines.append(line)
        if self.switched_off:
            lines.append(
                "Switched off: "
                + ", ".join(f"{d.id} ({d.cost_class})" for d in self.switched_off)
            )
        lines.extend(self._discovery_lines())
        lines.append(self.rule)
        return "\n".join(lines)

    def _discovery_lines(self) -> List[str]:
        return []

    def _discovery_json(self) -> Dict[str, Any]:
        return {}

    def to_json(self) -> Dict[str, Any]:
        default = self.default
        # No `names` and no `dispositions`: both carry hidden ids.
        data: Dict[str, Any] = {
            "caller": self.caller,
            "default": default.id if default is not None else None,
            "rendered_entries": [entry.to_json() for entry in self.rendered_entries],
            "rule": self.rule,
        }
        if self.switched_off:
            data["switched_off"] = [d.to_json() for d in self.switched_off]
        data.update(self._discovery_json())
        return data


@dataclass(frozen=True)
class DiscoveryRanking(Ranking):
    """A :class:`Ranking` that also carries the structured consent-layer note.

    ``consent`` is :func:`llm_kit.registry.consent.discovery_note`: the layer is
    optional, how a caller opts in, and (when a policy was applied) that policy.
    :func:`describe` returns this class only for ``discovery=True`` or a consent
    policy; every other ranking is a plain :class:`Ranking`, byte-for-byte as
    before. :meth:`render` adds one line and :meth:`to_json` one ``consent`` key.
    """

    consent: Optional[Mapping[str, Any]] = field(default=None, repr=False)

    def _discovery_lines(self) -> List[str]:
        if self.consent is None:
            return []
        from llm_kit.registry.consent import discovery_line  # noqa: PLC0415

        return [discovery_line(self.consent)]

    def _discovery_json(self) -> Dict[str, Any]:
        return {"consent": dict(self.consent)} if self.consent is not None else {}


def _fresh_reading(entry_id: str, spec: ConserveSpec, harness: Optional[str]) -> Budget:
    """Unpinned read for the pace NUMBER. ``entry_id`` names it for tests."""
    return evaluate(spec, harness)


def _pace(fresh: Optional[Budget]) -> "tuple[Optional[float], str]":
    """D5's "has a pace" table, for an entry whose pinned verdict is usable."""
    if fresh is None:
        return None, "n/a (unpaced)"
    if fresh.remaining is None or fresh.window_remaining is None:
        return None, "n/a (no reading)"
    if fresh.window_remaining < _ABOUT_TO_RESET:
        return None, "about to reset"
    if fresh.remaining <= 0.0:
        return 0.0, ""
    return fresh.remaining / fresh.window_remaining, ""


def _call_factory(factory: Callable[..., Any], name: str, project_root: Optional[str]) -> Any:
    if project_root is None:
        return factory(name)
    return factory(name, project_root=project_root)


def _armed_call(
    backend: Any, options: Any, requirements: Any, capabilities: Optional[Mapping[str, Any]]
) -> Any:
    """The call armed for the requirement that admitted it (``arm_call``).

    The record is looked up exactly as :func:`describe` looked it up (the
    injected ``capabilities`` or the shipped advertisement, keyed by the
    backend's name), so the record that admitted the entry is the record that
    arms it. A guarantee the record cannot arm raises ``ValueError`` before
    the call.
    """
    from llm_kit.completion.requirements import ArmedCall, arm_call  # noqa: PLC0415

    if not requirements:
        return ArmedCall(backend, options)
    from llm_kit.adapters.capabilities import adapter_capabilities  # noqa: PLC0415

    advertised = capabilities if capabilities is not None else adapter_capabilities()
    name = getattr(backend, "name", None)
    record = lookup_by_adapter(advertised, name)
    return arm_call(backend, options, requirements, record)


def _adapter_name(kind: str, harness: Optional[str]) -> Optional[str]:
    if kind == TRANSPORT_KIND:
        return ADAPTER_OPENAI_COMPAT
    return _HARNESS_ADAPTERS.get((harness or "").lower())


def _drive_name(backend_name: Optional[str], kind: str, harness: Optional[str]) -> str:
    """The adapter that drives an entry, by its current name (a retired name is mapped)."""
    name = canonical_adapter_name_or_none(backend_name) or _adapter_name(kind, harness)
    return name or "unroutable"


def _drive(caller: str, kind: str, harness: Optional[str], backend_name: Optional[str]) -> str:
    if caller == CALLER_SESSION and kind == TRANSPORT_KIND:
        # Reached only when the session caller declared transports dispatchable:
        # it drives the entry through the same adapter a process caller would.
        return _drive_name(backend_name, kind, harness)
    if caller == CALLER_SESSION:
        return _SESSION_DRIVE.get((harness or "").lower(), "unroutable")
    return _drive_name(backend_name, kind, harness)


def _rule(caller: str, has_author: bool) -> str:
    if caller == CALLER_SESSION:
        parts = [RULE_CHOICE_SESSION, rule_trigger_session()]
    else:
        parts = [RULE_CHOICE_PROCESS, RULE_TRIGGER_PROCESS]
    if has_author:
        parts.append(RULE_INDEPENDENCE)
    return "\n".join(parts)


_AUTH_HARNESSES = ("claude", "codex")


class _LazyHarnessAuth(Mapping):
    """Harness authentication probed on first need, once per harness.

    ``describe`` and ``run`` build one only when an ``allow`` filter is in
    force and the caller supplied no ``harness_auth``. Nothing is probed (and
    ``llm_kit.adapters.harness_auth`` is not imported) until a claude or codex
    harness entry is actually classified; ``cwd`` is the working directory the
    probe runs in. Any other harness name is absent from the mapping.
    """

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._auth: Dict[str, Any] = {}
        self._probed: set = set()

    def ensure(self, harness: str) -> None:
        if harness not in _AUTH_HARNESSES or harness in self._probed:
            return
        self._probed.add(harness)
        from llm_kit.adapters import harness_auth as _harness_auth  # noqa: PLC0415

        self._auth.update(_harness_auth.probe_harness_auth([harness], cwd=self._cwd))

    def __getitem__(self, harness: str) -> Any:
        self.ensure(harness)
        return self._auth[harness]

    def __iter__(self):
        return iter(self._auth)

    def __len__(self) -> int:
        return len(self._auth)


def _consent_note(policy: Any) -> Dict[str, Any]:
    from llm_kit.registry.consent import discovery_note  # noqa: PLC0415

    return discovery_note(policy_in_use=policy)


def describe(
    names: Any,
    *,
    project_root: Optional[str | Path] = None,
    caller: str,
    self_ref: Optional[str] = None,
    requirements: Any = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    backend_factory: Optional[Callable[..., Any]] = None,
    exclude: Iterable[str] = (),
    reachability_cache: Optional[MutableMapping[str, Reachability]] = None,
    entries: Optional[Mapping[str, Any]] = None,
    dispatchable: Iterable[str] = (),
    allow: Optional[Iterable[str]] = None,
    consent_policy: Optional[Any] = None,
    discovery: bool = False,
    harness_auth: Optional[Mapping[str, Any]] = None,
    exclude_groups: Iterable[str] = (),
) -> Ranking:
    """Classify, filter, pace-order and default a declaration.

    ``caller`` is ``"session"`` (an agent that drives the harness itself:
    Agent tool, rendered ``codex exec``/``opencode run``; a transport entry
    has no agent loop and is unroutable here) or ``"process"`` (the completion
    seam: every resolvable entry routes). ``requirements`` is matched with
    ``completion.match_capabilities`` against ``capabilities`` (default: the
    shipped advertisement) keyed by the RESOLVED backend's name, the same
    lookup job-kit's execution makes; a ``Capabilities`` record is first
    specialized to a transport entry's own profile (``completion
    .endpoint_capabilities``), so ``params: ["effort"]`` matches exactly the
    transport entries that deliver an effort. ``backend_factory`` resolves an id
    (a raised ``EndpointResolveError`` means it does not); without one an id
    resolves through the merged entry map, falling back to ``create_backend``
    for ids the map does not carry (the legacy ``openrouter`` wrapper).
    ``reachability_cache`` is read first and receives every probe made, so a
    caller-scoped mapping probes each entry once; without it every call
    probes live. ``entries`` injects the merged entry map.
    ``dispatchable`` names entry kinds a SESSION caller can drive beyond the
    harness drives; the only kind is ``"transport"``, for a session caller
    that reaches transports through a runner of its own. Default: none, so a
    transport is unroutable for a session caller. A process caller already
    routes every resolvable kind, so the argument changes nothing there.

    ``allow`` is the cost-class filter, owned by the caller: an iterable of
    ``"local"``, ``"subscription"`` and ``"metered"`` (normalized once through
    :func:`~llm_kit.registry.cost.normalize_allow`; ``ValueError`` on an
    unknown class). An entry whose class (:func:`~llm_kit.registry.cost.classify`)
    is not allowed gets the disposition ``switched-off``, with its cost class
    and the detail ``"<class> not allowed: <reason>"``. The check follows the
    unroutable checks and precedes the requirements match and every probe, so a
    switched-off entry is never probed. Switched-off entries are SHOWN, not
    hidden: they are listed in :attr:`Ranking.switched_off`, named by
    ``render``/``to_json``, and itemised by the floor. With ``allow=None``
    (the default) nothing is classified or authenticated and the result is
    byte-identical to a call without the argument. Harness authentication
    (claude and codex entries only) is probed lazily, once per harness, in the
    working directory ``Path(project_root or Path.cwd()).resolve()`` -- unless
    the caller supplies ``harness_auth``, a mapping of harness name to
    :class:`~llm_kit.registry.cost.HarnessAuth`, in which case nothing is
    probed.

    ``consent_policy`` is the opt-in alternative to ``allow``: a
    :class:`llm_kit.registry.consent.Policy` the caller obtained from
    ``consent.policy(scope=...)``. It filters exactly as ``allow=policy.allowed``
    would, the switched-off detail names the exact ``llm-kit consent set`` command
    that would allow the class, and the ranking is a :class:`DiscoveryRanking`
    carrying the consent note with the policy applied. Passing both ``allow`` and ``consent_policy`` is a
    ``ValueError``.

    ``discovery=True`` makes the ranking a discovery report: every rendered entry
    carries its cost class and the reason for it (``cost`` in
    :meth:`CostedEntryState.to_json`), independent of any filter, and the ranking is
    a :class:`DiscoveryRanking` carrying the structured consent note. Harness authentication
    is probed lazily, as ``allow`` does. An ``allow`` filter or a consent policy
    classifies to filter and reports a class only on the entries it switches off,
    unless ``discovery=True`` is also passed. With none of the three nothing is
    classified and the result is unchanged.

    Front-door expansion (R39). A declared entry marked ``frontdoor: true``
    (a transport whose ``model`` names a routing group) is followed by that
    group's members (:func:`~llm_kit.registry.model_endpoints.frontdoor_group_members`:
    transport entries whose ``routing.group`` is the group, by
    ``routing.order``). A member the declaration names itself keeps its own
    slot, and a member already added after an earlier front door is not
    repeated; :attr:`Ranking.expanded_names` is the result. The members are
    classified and probed only when the front door is NOT usable -- ruled out
    (``exclude``), switched off, out of quota or unreachable -- each through
    every filter a declared entry passes, against its own registry entry. A
    usable front door is probed exactly as before and its members are neither
    classified, probed, rendered nor itemised. An expanded member carries
    ``via`` (the front door's id) on its :class:`EntryState` and
    :class:`Disposition` and shares the front door's ``declared_index``; the
    render appends ``via <id>`` to its line. Pace ordering moves a front door
    and its members as one block, so the order is the front door, its members
    in ``routing.order``, then the next declared entry.

    ``exclude_groups`` names front-door entries that reported their whole group
    failed (:data:`FALLTHROUGH_GROUP_EXHAUSTED`): every member of each one's
    group -- expanded or declared -- is ruled out like ``exclude``, with the
    detail ``"<front door> reported its group exhausted"``. The front door
    itself is ruled out only by ``exclude``.

    Raises :class:`NoUsableRoutingTarget` when nothing usable remains, and
    ``bootstrap_lib.model_declaration.DeclarationError`` for a structurally
    invalid declaration (empty, duplicate, non-string). Skipping is silent.
    """
    if caller not in _CALLERS:
        raise ValueError(f"caller must be one of {_CALLERS}, got {caller!r}")
    extra_kinds = set(dispatchable or ())
    unknown_kinds = sorted(extra_kinds - set(_DISPATCHABLE_KINDS))
    if unknown_kinds:
        raise ValueError(
            f"dispatchable kinds must be among {_DISPATCHABLE_KINDS}, got {unknown_kinds}"
        )
    session_transport = caller == CALLER_SESSION and DISPATCHABLE_TRANSPORT in extra_kinds
    if allow is not None and consent_policy is not None:
        raise ValueError(
            "allow and consent_policy are alternatives: allow is the caller's own filter, "
            "consent_policy applies the optional consent layer; pass one"
        )
    if consent_policy is not None:
        allow_set: Optional[frozenset] = frozenset(consent_policy.allowed)
    else:
        allow_set = _cost.normalize_allow(allow) if allow is not None else None
    classifying = discovery or allow_set is not None
    auth: Optional[Mapping[str, Any]] = None
    if classifying:
        auth = harness_auth
        if auth is None:
            auth = _LazyHarnessAuth(Path(project_root or Path.cwd()).resolve())
    declared = _model_declaration().parse(names)
    root = str(project_root) if project_root is not None else None
    injected = entries is not None
    if entries is None:
        from llm_kit.registry.models import discover_model_entries  # noqa: PLC0415

        entries = discover_model_entries(project_root=root)
    excluded = set(exclude)
    matcher = None
    if requirements:
        from llm_kit.adapters.capabilities import adapter_capabilities  # noqa: PLC0415
        from llm_kit.adapters.endpoint_profile import (  # noqa: PLC0415
            endpoint_capabilities,
            profile_from_entry,
        )
        from llm_kit.completion.capabilities import Capabilities  # noqa: PLC0415
        from llm_kit.completion.requirements import match_capabilities  # noqa: PLC0415

        advertised = dict(capabilities if capabilities is not None else adapter_capabilities())
        matcher = match_capabilities

    # R39: each declared front-door entry is followed by its group's members, in
    # walk order. An id the caller declared itself keeps its own slot (explicit
    # wins); a member claimed by an earlier front door is not repeated. The
    # expansion is made whatever the front door's own disposition, so a front door
    # ruled out at run time, switched off by ``allow`` or found down still leaves
    # its members selectable.
    explicit = set(declared)
    claimed: set = set()
    slots: List[tuple] = []  # (name, declared index, via)
    for index, name in enumerate(declared):
        slots.append((name, index, None))
        for member in frontdoor_group_members(entries.get(name), entries):
            if member in explicit or member in claimed:
                continue
            claimed.add(member)
            slots.append((member, index, name))
    expanded_names = tuple(slot[0] for slot in slots)
    group_ruled_out: Dict[str, str] = {}
    for door in exclude_groups:
        for member in frontdoor_group_members(entries.get(door), entries):
            group_ruled_out.setdefault(member, f"{door} reported its group exhausted")

    dispositions: Dict[int, Disposition] = {}  # keyed by slot position
    candidates: List[Dict[str, Any]] = []

    def classify_slot(pos: int, name: str, index: int, via: Optional[str]) -> None:
        def settle(
            disposition: str, detail: str = "", resets_at: Optional[int] = None,
            cost_class: Optional[str] = None,
        ) -> None:
            dispositions[pos] = Disposition(
                name, index, disposition, detail, resets_at, cost_class, via=via
            )

        if name in excluded:
            settle(DISPOSITION_EXCLUDED, "ruled out by the caller")
            return
        if name in group_ruled_out:
            settle(DISPOSITION_EXCLUDED, group_ruled_out[name])
            return
        merged = entries.get(name)
        shadow = check_registry_entry(name, merged)
        if shadow is not None:
            settle(DISPOSITION_SHADOWED_CORE, shadow)
            return
        selection = None
        if backend_factory is not None:
            try:
                selection = _call_factory(backend_factory, name, root)
            except (EndpointResolveError, EndpointRegistryError) as exc:
                settle(DISPOSITION_UNROUTABLE if merged is not None else DISPOSITION_UNRESOLVED, str(exc))
                return
        elif merged is None:
            if injected:
                settle(DISPOSITION_UNRESOLVED, "no registry entry")
                return
            from llm_kit.dispatch.factory import create_backend  # noqa: PLC0415

            try:
                selection = _call_factory(create_backend, name, root)
            except (EndpointResolveError, EndpointRegistryError) as exc:
                settle(DISPOSITION_UNRESOLVED, str(exc))
                return
        kind = getattr(merged, "kind", None) or getattr(selection, "kind", None) or TRANSPORT_KIND
        harness = getattr(merged, "harness", None)
        backend_name = getattr(getattr(selection, "backend", None), "name", None)
        if caller == CALLER_SESSION and not (
            (kind == TRANSPORT_KIND and session_transport)
            or (kind == HARNESS_KIND and (harness or "").lower() in _SESSION_DRIVE)
        ):
            settle(
                DISPOSITION_UNROUTABLE,
                "a transport entry has no agent loop" if kind != HARNESS_KIND
                else f"harness {harness!r} has no in-session drive",
            )
            return
        if caller == CALLER_PROCESS and kind == HARNESS_KIND and selection is None and (
            (harness or "").lower() not in _HARNESS_ADAPTERS
        ):
            settle(DISPOSITION_UNROUTABLE, f"harness {harness!r} has no completion backend")
            return
        verdict = None
        if classifying:
            if isinstance(auth, _LazyHarnessAuth) and kind == HARNESS_KIND:
                auth.ensure((harness or "").lower())
            verdict = _cost.classify(merged, entries=entries, harness_auth=auth)
            if allow_set is not None and verdict.cost_class not in allow_set:
                detail = f"{verdict.cost_class} not allowed: {verdict.reason}"
                if consent_policy is not None:
                    detail += f"; {consent_policy.remedy(verdict.cost_class)}"
                settle(
                    DISPOSITION_SWITCHED_OFF,
                    detail,
                    cost_class=verdict.cost_class,
                )
                return
        if matcher is not None:
            adapter = backend_name if isinstance(backend_name, str) else _adapter_name(kind, harness)
            record = lookup_by_adapter(advertised, adapter) if adapter else None
            if (
                isinstance(record, Capabilities)
                and kind == TRANSPORT_KIND
                and isinstance(merged, EndpointEntry)
            ):
                # The family record is the truth for an unknown endpoint; this
                # entry's own profile can enable a conditional param (effort on
                # an entry that resolves a delivering effort style).
                record = endpoint_capabilities(record, profile_from_entry(merged))
            if record is None or not matcher(record, requirements):
                settle(DISPOSITION_REQUIREMENTS_MISMATCH, f"adapter {adapter!r}")
                return
        spec = getattr(merged, "conserve_usage", None)
        pinned = pinned_evaluate(name, spec, harness) if spec is not None else None
        candidate = {
            "pos": pos, "index": index, "via": via, "name": name, "merged": merged,
            "kind": kind, "harness": harness, "backend_name": backend_name, "spec": spec,
            "pinned": pinned, "model": getattr(merged, "model", None) or getattr(selection, "model", None),
            "verdict": verdict,
        }
        if pinned is not None and not pinned.usable:
            settle(DISPOSITION_OUT_OF_QUOTA, pinned.detail, pinned.resets_at)
        candidates.append(candidate)

    known: Dict[str, Reachability] = dict(reachability_cache or {})

    def probe(stage: Iterable[Dict[str, Any]]) -> None:
        # Probe only what can still be dispatched: hidden and out-of-quota
        # entries gain nothing from a reachability verdict.
        to_probe = {
            c["name"]: {"kind": c["kind"], "harness": c["harness"]}
            for c in stage
            if c["pos"] not in dispositions
            and (reachability_cache is None or c["name"] not in reachability_cache)
        }
        probed = check_many(to_probe, timeout=DEFAULT_VERIFY_TIMEOUT_S, project_root=root) if to_probe else {}
        if reachability_cache is not None:
            reachability_cache.update(probed)
        known.update(probed)

    # Stage 1: the declared entries, exactly as without front-door expansion.
    for pos, (name, index, via) in enumerate(slots):
        if via is None:
            classify_slot(pos, name, index, via)
    probe(candidates)

    # Stage 2 (D1): a front door's members are classified and probed only when
    # the front door itself is not usable, so a healthy front door costs no
    # member probe and keeps every unit.
    usable_doors = {
        c["name"] for c in candidates
        if c["pos"] not in dispositions
        and getattr(known.get(c["name"]), "status", STATUS_UNKNOWN) != STATUS_UNREACHABLE
    }
    first_member = len(candidates)
    for pos, (name, index, via) in enumerate(slots):
        if via is not None and via not in usable_doors:
            classify_slot(pos, name, index, via)
    probe(candidates[first_member:])
    candidates.sort(key=lambda c: c["pos"])

    self_model = None
    if self_ref is not None:
        self_entry = entries.get(self_ref)
        self_model = getattr(self_entry, "model", None) or self_ref
        if self_entry is None and not any(
            c["model"] == self_model for c in candidates
        ):
            self_model = claude_cli_alias(self_ref) or self_model

    states: List[EntryState] = []
    for c in candidates:
        pos, index, name, merged = c["pos"], c["index"], c["name"], c["merged"]
        pinned = c["pinned"]
        out_of_quota = pos in dispositions
        reach = None if out_of_quota else known.get(name)
        reach_status = reach.status if reach is not None else STATUS_UNKNOWN
        if not out_of_quota:
            if reach_status == STATUS_UNREACHABLE:
                dispositions[pos] = Disposition(
                    name, index, DISPOSITION_UNREACHABLE, reach.detail, via=c["via"]
                )
            else:
                dispositions[pos] = Disposition(name, index, DISPOSITION_USABLE, via=c["via"])
        fresh = None
        if c["spec"] is not None and not out_of_quota:
            fresh = _fresh_reading(name, c["spec"], c["harness"])
        pace, note = (None, "") if out_of_quota else _pace(fresh)
        costed: Dict[str, Any] = {}
        if discovery and c["verdict"] is not None:
            costed = {"cost_class": c["verdict"].cost_class, "cost_reason": c["verdict"].reason}
        states.append((CostedEntryState if costed else EntryState)(
            id=name,
            declared_index=index,
            resolved=True,
            kind=c["kind"],
            harness=c["harness"],
            model=c["model"],
            family=getattr(merged, "family", None),
            tier=getattr(merged, "tier", None),
            drive=_drive(caller, c["kind"], c["harness"], c["backend_name"]),
            reachability=reach_status,
            usability=pinned.status if pinned is not None else None,
            pace=pace,
            usable=dispositions[pos].disposition == DISPOSITION_USABLE,
            is_self=self_ref is not None and (name == self_ref or c["model"] == self_model),
            pace_note=note,
            remaining=fresh.remaining if fresh is not None else None,
            window_remaining=fresh.window_remaining if fresh is not None else None,
            resets_at=pinned.resets_at if pinned is not None else None,
            pool=c["spec"].pool if c["spec"] is not None else None,
            reachability_detail=reach.detail if reach is not None else "",
            via=c["via"],
            **costed,
        ))

    ordered_dispositions = tuple(dispositions[pos] for pos in sorted(dispositions))
    ordered = _order_blocks(states)
    if not any(state.usable for state in ordered):
        raise NoUsableRoutingTarget(declared, ordered_dispositions, caller)

    # Shared quota (D3): same harness account, same declared pool. The render
    # labels it; an observed halt spends it for all of them (record_observed_halt).
    def pool_key(state: EntryState, spec: Any) -> Any:
        return quota_pool_key(state.harness, spec)

    specs = {c["name"]: c["spec"] for c in candidates}
    first_usable = next(state.id for state in ordered if state.usable)
    finished = []
    for state in ordered:
        key = pool_key(state, specs[state.id])
        shares = tuple(
            other.id for other in ordered
            if other.id != state.id and key is not None and pool_key(other, specs[other.id]) == key
        )
        finished.append(replace(state, shares_quota_with=shares, default=state.id == first_usable))

    ranking_fields: Dict[str, Any] = dict(
        names=tuple(declared),
        caller=caller,
        rendered_entries=tuple(finished),
        dispositions=ordered_dispositions,
        rule=_rule(caller, any(state.is_self for state in finished)),
        switched_off=tuple(
            d for d in ordered_dispositions if d.disposition == DISPOSITION_SWITCHED_OFF
        ),
        expanded_names=expanded_names,
    )
    if discovery or consent_policy is not None:
        return DiscoveryRanking(consent=_consent_note(consent_policy), **ranking_fields)
    return Ranking(**ranking_fields)


# ---------------------------------------------------------------------------
# Fall-through: one unit moving to the next endpoint (R40, R41)
# ---------------------------------------------------------------------------

#: The endpoint answered that it is busy (``classify_backpressure``, or a
#: ``kind == "backpressure"`` halt): skipped for this unit, no quota recorded.
FALLTHROUGH_BUSY = "busy"
#: The endpoint could not be connected to (refused, DNS failure, connect
#: timeout): skipped, and remembered as unreachable by the :class:`Reselector`.
FALLTHROUGH_UNREACHABLE = ENDPOINT_UNREACHABLE
#: The request timed out after connecting: skipped for this unit only.
FALLTHROUGH_TIMEOUT = ENDPOINT_TIMEOUT
#: A front door answered that every deployment of its group failed
#: (:func:`~llm_kit.completion.halt.is_frontdoor_group_exhausted`): the unit
#: moves past the front door AND its group's members -- the front door already
#: tried them -- to the next declared entry. Not remembered past the unit.
FALLTHROUGH_GROUP_EXHAUSTED = "group-exhausted"


def fallthrough_reason(exc: BaseException, *, caller_deadline: bool = False) -> Optional[str]:
    """Why a failed call moves its unit to the next endpoint, or None when it must not.

    :data:`FALLTHROUGH_BUSY` for transient overload (an HTTP 429/503 that
    :func:`~llm_kit.completion.halt.classify_backpressure` marks, or an
    exception carrying ``kind == "backpressure"``), else the endpoint
    transport verdict of
    :func:`~llm_kit.completion.halt.classify_endpoint_failure`
    (:data:`FALLTHROUGH_UNREACHABLE` or :data:`FALLTHROUGH_TIMEOUT`). Anything
    else -- a client configuration error, a bad or refused response, a model
    error, a programming error -- is None and must fail the unit where it is.
    The one bad response that does move it is the front door's own
    group-exhausted 502 (:data:`FALLTHROUGH_GROUP_EXHAUSTED`); every other 502
    or 5xx stays a failed call.

    ``caller_deadline`` is True when the call carried the caller's own
    ``timeout_s``. A timeout after the connection was made is then the
    caller's budget expiring, not evidence about the endpoint (the rule "a
    caller that sets the deadline owns the timeout"), so it is None; a
    connect-phase timeout is still :data:`FALLTHROUGH_UNREACHABLE`.
    """
    if getattr(exc, "kind", None) == HALT_BACKPRESSURE or classify_backpressure(exc) is not None:
        return FALLTHROUGH_BUSY
    if is_frontdoor_group_exhausted(exc):
        return FALLTHROUGH_GROUP_EXHAUSTED
    verdict = classify_endpoint_failure(exc)
    if caller_deadline and verdict == FALLTHROUGH_TIMEOUT:
        return None
    return verdict


class Reselector:
    """Re-selection for one declaration after a fall-through failure: describe again, minus what failed.

    The one mechanism every declaration caller that owns its own call uses --
    :func:`run`, the pipeline's declaration backend and the code-review lane.
    It holds the reachability cache :func:`describe` reads and fills, so an
    entry is probed once for the Reselector's life, and the ids it has
    :meth:`record`-ed as unreachable, which every later selection rules out.
    ``skip`` rules ids out for one selection only (a busy endpoint, or one
    that already failed this unit). ``exclude`` is the caller's own
    exclusion, applied to every selection. Every other keyword is passed to
    :func:`describe` unchanged; the caller defaults to a process caller.

    Thread-safe: selections and records are serialized, so a shared instance
    may serve concurrent calls.
    """

    def __init__(
        self,
        names: Any,
        *,
        exclude: Iterable[str] = (),
        reachability_cache: Optional[MutableMapping[str, Reachability]] = None,
        **describe_kwargs: Any,
    ) -> None:
        if isinstance(names, (list, tuple)):
            names = list(names)
        self._names = names
        self._exclude = frozenset(exclude)
        self._cache: MutableMapping[str, Reachability] = (
            {} if reachability_cache is None else reachability_cache
        )
        describe_kwargs.setdefault("caller", CALLER_PROCESS)
        self._kwargs = describe_kwargs
        self._unreachable: Dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def unreachable(self) -> frozenset:
        """Ids recorded unreachable so far; every selection rules them out."""
        with self._lock:
            return frozenset(self._unreachable)

    def ranking(self, skip: Iterable[str] = (), exclude_groups: Iterable[str] = ()) -> Ranking:
        """:func:`describe` with the caller's exclusion, every recorded failure and ``skip`` ruled out.

        ``exclude_groups`` (front doors that reported their group exhausted)
        is passed to :func:`describe` for this selection only.
        """
        with self._lock:
            exclude = set(self._exclude) | set(self._unreachable) | set(skip)
            return describe(
                self._names, exclude=exclude, reachability_cache=self._cache,
                exclude_groups=tuple(exclude_groups), **self._kwargs
            )

    def select(self, skip: Iterable[str] = (), exclude_groups: Iterable[str] = ()) -> EntryState:
        """The default of :meth:`ranking`; :class:`NoUsableRoutingTarget` when none remains."""
        default = self.ranking(skip, exclude_groups).default
        assert default is not None  # describe() raises the floor otherwise
        return default

    def record(self, entry_id: str, reason: Optional[str], detail: str = "") -> None:
        """Remember a failure. Only :data:`FALLTHROUGH_UNREACHABLE` outlives the unit.

        An unreachable entry is written to the reachability cache (``checked:
        "dispatch"``) and ruled out of every later selection; a busy or
        timed-out entry is not remembered, since both clear on their own.
        """
        if reason != FALLTHROUGH_UNREACHABLE:
            return
        with self._lock:
            self._unreachable[entry_id] = detail
            self._cache[entry_id] = Reachability(
                status=STATUS_UNREACHABLE, checked="dispatch",
                detail=detail or "connect failed",
            )


def call_with_fallthrough(
    reselector: Reselector,
    call: Callable[[Any], Any],
    *,
    first: Any,
    classify: Callable[[BaseException], Optional[str]] = fallthrough_reason,
    on_fallthrough: Optional[Callable[[EntryState, str, BaseException], None]] = None,
) -> Any:
    """Run ``call(first)``; on a fall-through failure run ``call`` on the next endpoint.

    ``classify`` (default :func:`fallthrough_reason`) decides each failure:
    None re-raises it unchanged; any reason records it on ``reselector``
    (:meth:`Reselector.record`), rules the entry out for the rest of this
    unit, reports it to ``on_fallthrough`` and selects again. A
    :data:`FALLTHROUGH_GROUP_EXHAUSTED` failure also rules out that front
    door's whole group for the unit. When no usable entry remains, the LAST
    endpoint's own exception propagates -- not the floor -- so the caller
    classifies it exactly as it would without fall-through. Each entry is
    called at most once per unit. ``first`` is the entry to call first (an
    :class:`EntryState`, or any object with the entry's ``id``).
    """
    entry = first
    skip: set = set()
    exhausted_doors: set = set()
    while True:
        try:
            return call(entry)
        except Exception as exc:  # noqa: BLE001 -- classified below, re-raised when not ours
            reason = classify(exc)
            if reason is None:
                raise
            failure = exc
        reselector.record(entry.id, reason, f"{type(failure).__name__}: {failure}")
        skip.add(entry.id)
        if reason == FALLTHROUGH_GROUP_EXHAUSTED:
            exhausted_doors.add(entry.id)
        if on_fallthrough is not None:
            on_fallthrough(entry, reason, failure)
        try:
            entry = reselector.select(skip, exhausted_doors)
        except NoUsableRoutingTarget:
            exhausted = True
        else:
            exhausted = False
        if exhausted:
            raise failure


# ---------------------------------------------------------------------------
# run(): unattended dispatch for callers with no loop of their own
# ---------------------------------------------------------------------------

RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_ATTEMPT_LIMIT = "attempt-limit"

#: Halts that move an unattended run to the next entry (D6, process set).
_QUOTA_HALTS = (HALT_QUOTA, HALT_INSUFFICIENT_CREDIT)

#: Launch/transport failures carrying no halt marker that still move on: the
#: entry is marked unreachable for the rest of the run.
_LAUNCH_ERRORS = (FileNotFoundError, ConnectionError)


@dataclass(frozen=True)
class RunRequest:
    """One unit of work. ``workspace`` names a git work tree the unit may write."""

    system: str
    prompt: str
    options: Optional[BackendOptions] = None
    workspace: Optional[Path] = None


@dataclass(frozen=True)
class Attempt:
    """One execution, reported through ``on_attempt`` with its pace reading."""

    entry: str
    number: int
    pace: Optional[float]
    halt: Optional[str] = None
    error: Optional[str] = None
    outcome: str = ""
    workspace_action: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "entry": self.entry, "number": self.number, "pace": self.pace,
            "halt": self.halt, "error": self.error, "outcome": self.outcome,
            "workspace_action": self.workspace_action,
        }


@dataclass(frozen=True)
class RunResult:
    """How an unattended run ended. The floor is raised, never returned."""

    status: str
    entry: Optional[str] = None
    response: Any = None
    attempts: tuple = ()
    detail: str = ""


class _WorkspaceSnapshot:
    """The launch state of a git work tree, restorable without touching history.

    The working tree (tracked plus untracked, non-ignored files) is captured
    as a tree object through a TEMPORARY index, so the user's own index is
    never rewritten to take the snapshot. Restoring puts every snapshot file
    back, removes non-ignored files the unit created, restores the real
    index, and moves HEAD back (``reset --soft``, so a unit's commit stays in
    the reflog) when the unit committed.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.head = self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
        self.index_tree = self._git("write-tree").strip()
        self.tree = self._tree_of_worktree()

    def _git(self, *args: str, env: Optional[Mapping[str, str]] = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, env={**os.environ, **(env or {})},
        )
        if check and proc.returncode != 0:
            raise OSError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _tree_of_worktree(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._git("add", "-A", ".", env=env)
            return self._git("write-tree", env=env).strip()

    def restore(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            now_env = {"GIT_INDEX_FILE": str(Path(tmp) / "now")}
            self._git("add", "-A", ".", env=now_env)
            present = set(self._git("ls-files", "-z", env=now_env).split("\0")) - {""}
            wanted = set(self._git("ls-tree", "-r", "-z", "--name-only", self.tree).split("\0")) - {""}
            for rel in sorted(present - wanted):
                try:
                    (self.root / rel).unlink()
                except OSError:
                    pass
            snap_env = {"GIT_INDEX_FILE": str(Path(tmp) / "snap")}
            self._git("read-tree", self.tree, env=snap_env)
            self._git("checkout-index", "-a", "-f", env=snap_env)
        moved = ""
        current = self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
        if self.head is not None and current != self.head:
            self._git("reset", "-q", "--soft", self.head)
            moved = f"; HEAD moved back to {self.head[:12]}"
        self._git("read-tree", self.index_tree)
        return f"workspace reset to its launch state (tree {self.tree[:12]}){moved}"


def run(
    names: Any,
    request: RunRequest,
    *,
    project_root: Optional[str | Path] = None,
    requirements: Any = None,
    exclude: Iterable[str] = (),
    max_attempts: Optional[int] = None,
    on_attempt: Optional[Callable[[Attempt], None]] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    backend_factory: Optional[Callable[..., Any]] = None,
    reachability_cache: Optional[MutableMapping[str, Reachability]] = None,
    entries: Optional[Mapping[str, Any]] = None,
    observer: Optional[ExecutionObserver] = None,
    allow: Optional[Iterable[str]] = None,
) -> RunResult:
    """Dispatch a unit to the first usable entry, moving on only on a classified halt.

    For callers with NO loop of their own. Each execution is reported through
    ``on_attempt`` and counted against ``max_attempts``, which limits
    executions only: reaching it is ``RUN_ATTEMPT_LIMIT``, never the floor.
    ``max_attempts`` None (the default) allows one execution per id of the
    expanded declaration (:attr:`Ranking.expanded_names` of the first
    selection: the declared ids plus every front door's group members), so
    every endpoint can be tried once; an explicit value is the limit as given.
    Reachability probes are not executions and count against nothing.
    A quota or credit halt writes the entry's verdict back, and every entry
    sharing its quota pool with it
    (:func:`~.usage_budget.record_observed_halt`, when it declares
    ``conserve_usage``); every classified halt and every launch failure
    excludes the entry for the rest of the run, and :func:`describe` is called
    again (through a :class:`Reselector`). A launch failure is a
    ``FileNotFoundError`` or ``ConnectionError``, or an endpoint transport
    failure (:func:`fallthrough_reason`: a refused connection, a DNS failure,
    a timeout -- R40 -- except a post-connect timeout of a request that set
    its own ``timeout_s``, which stays a failed attempt), or a front door's
    group-exhausted 502, after which its group's members are skipped too and
    the next declared entry is tried; one that could not
    connect also marks the entry unreachable in the reachability cache. A busy
    endpoint (the ``backpressure`` halt, R41, also when only
    :func:`fallthrough_reason` recognises it) is excluded like any classified
    halt and records no quota verdict. A task error stays a failed attempt.

    A transport entry's call is made with ``options.max_retries=0`` when the
    request leaves ``max_retries`` unset, so a busy or unreachable endpoint
    hands the unit on at once instead of waiting through SDK retries; an
    explicit ``max_retries`` is sent as given. When ``request.workspace``
    names a git work tree, it is reset to its launch state before any
    re-selection, and a workspace that cannot be reset ends the run rather
    than layering a second model on the first one's partial edits.
    :class:`NoUsableRoutingTarget` propagates.

    A request whose ``options.output_contract`` is set has its selection
    requirement (:func:`~llm_kit.completion.contract.contract_requirements`) merged
    into ``requirements`` before every :func:`describe`, so an entry whose
    adapter cannot satisfy the contract's policy is skipped rather than
    dispatched to and refused. A conflicting caller requirement raises
    ``ValueError`` before anything runs. A call that completed but violated
    its contract is a failed attempt, not a halt, and the violation's full
    response (raw text, controls, report) is returned in ``RunResult.response``.

    A request whose ``options.skill_context`` is set has its selection
    requirement (:func:`~llm_kit.completion.skill_context.skill_context_requirements`)
    merged the same way, so a harness entry, whose adapter refuses skill
    context, is skipped rather than dispatched to; a declaration of harness
    entries only reaches the floor. A conflicting caller requirement raises
    ``ValueError`` before anything runs. The skill context was built before
    this call (``materialize_skill_context``), so every attempt sends the same
    block. Each ``dispatch-selected`` payload then gains
    ``skill_context: {"digest", "skills", "estimated_tokens"}``: counts and the
    digest only, never a name, a path or text.

    A ``guarantees`` requirement (``filesystem-write``, ``shell-exec``,
    ``subagent-spawn``) both admits an entry and arms it: the selected backend
    and options pass through :func:`~llm_kit.completion.requirements.arm_call`, so a
    claude-cli or codex-cli entry runs in text-only mode and an opencode-cli
    entry gets the deny-list names, with no caller flag. A subject the record
    cannot arm raises ``ValueError`` before the call, and a response that does
    not report an armed control raises ``RuntimeError``. Without such a
    requirement the call is dispatched unchanged.

    ``observer`` (keyword-only) receives execution events through its
    ``emit`` method (:class:`~.observer.ExecutionObserver`; a
    ``bootstrap_lib.execution_event.Emitter`` bound to the caller's run and
    unit fits). Per attempt: ``dispatch-selected``, ``call-started``, ``usage``
    (only when the response reports a non-zero count, through
    ``usage_payload``), ``result``; once per call: ``terminal``. An observer
    exception propagates unchanged. When an observer is passed and
    ``bootstrap_lib.execution_event`` is absent or too old, the call raises
    :class:`DeclarationSupportError` before any dispatch; without an observer
    the module is never imported.

    ``allow`` (keyword-only, default ``None``) is the cost-class filter of
    :func:`describe`. It is normalized ONCE here, so a one-shot iterable such as
    a generator survives re-selection, and the same frozenset filters every
    attempt's :func:`describe`; an unknown class raises ``ValueError`` before
    anything runs. Harness authentication is probed lazily, once for the whole
    run, in ``request.workspace or project_root or Path.cwd()`` (resolved) --
    the directory the backend runs in -- and reused by every attempt. Switched-off
    entries are shown by :func:`describe` and itemised by the floor, never
    dispatched to: a declaration whose every entry is switched off raises
    :class:`NoUsableRoutingTarget` before any backend call. Without ``allow``
    nothing is classified or probed.
    """
    if max_attempts is not None and max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    allow_set = _cost.normalize_allow(allow) if allow is not None else None
    allow_kwargs: Dict[str, Any] = {}
    if allow_set is not None:
        allow_kwargs = {
            "allow": allow_set,
            "harness_auth": _LazyHarnessAuth(
                Path(request.workspace or project_root or Path.cwd()).resolve()
            ),
        }
    execution_event = _execution_event() if observer is not None else None
    contract = getattr(request.options, "output_contract", None)
    if contract is not None:
        requirements = merge_requirements(requirements, contract_requirements(contract))
    skill_context = getattr(request.options, "skill_context", None)
    if skill_context is not None:
        requirements = merge_requirements(requirements, skill_context_requirements(skill_context))
    root = str(project_root) if project_root is not None else None
    if backend_factory is None:
        from llm_kit.dispatch.factory import create_backend as backend_factory  # noqa: PLC0415
    reselector = Reselector(
        names, exclude=exclude, reachability_cache=reachability_cache,
        project_root=root, caller=CALLER_PROCESS, requirements=requirements,
        capabilities=capabilities, backend_factory=backend_factory, entries=entries,
        **allow_kwargs,
    )
    excluded: set = set()  # every entry that halted or failed to launch in this run
    exhausted_doors: set = set()  # front doors that reported their whole group failed
    limit = max_attempts
    attempts: List[Attempt] = []
    snapshot: Optional[_WorkspaceSnapshot] = None
    snapshot_error = ""
    if request.workspace is not None:
        try:
            snapshot = _WorkspaceSnapshot(Path(request.workspace))
        except (OSError, subprocess.SubprocessError) as exc:
            snapshot_error = str(exc)

    def report(attempt: Attempt) -> None:
        attempts.append(attempt)
        if on_attempt is not None:
            on_attempt(attempt)

    def emit(event: str, **fields: Any) -> None:
        if observer is not None:
            observer.emit(event, **fields)

    def finish(result: RunResult) -> RunResult:
        emit("terminal", payload={"state": result.status})
        return result

    while True:
        try:
            ranking = reselector.ranking(excluded, exhausted_doors)
        except NoUsableRoutingTarget:
            emit("terminal", payload={"state": "unroutable"})
            raise
        if limit is None:
            # Sized once, from the first selection's stable expansion.
            limit = max(1, len(ranking.expanded_names))
        chosen = ranking.default
        assert chosen is not None  # describe() raises the floor otherwise
        number = len(attempts) + 1
        selection = _call_factory(backend_factory, chosen.id, root)
        options = request.options or BackendOptions()
        if options.effort is None and getattr(selection, "effort", None):
            options = replace(options, effort=selection.effort)
        if chosen.kind == TRANSPORT_KIND and getattr(options, "max_retries", None) is None:
            # D2: no SDK retry loop in front of the next endpoint.
            options = replace(options, max_retries=0)
        armed = _armed_call(selection.backend, options, requirements, capabilities)
        backend, options = armed.backend, armed.options
        attempt_fields: Dict[str, Any] = {"attempt_id": str(number)}
        backend_name = getattr(backend, "name", None)
        if isinstance(backend_name, str) and backend_name:
            attempt_fields["adapter"] = canonical_adapter_name_or_none(backend_name)
        if isinstance(selection.model, str) and selection.model:
            attempt_fields["model"] = selection.model
        if observer is not None:
            picked: Dict[str, Any] = {"entry": chosen.id}
            if chosen.pace is not None:
                picked["pace"] = chosen.pace
            if skill_context is not None:
                picked["skill_context"] = {
                    "digest": skill_context.report.digest,
                    "skills": skill_context.report.skills,
                    "estimated_tokens": skill_context.report.estimated_tokens,
                }
            emit("dispatch-selected", payload=picked, **attempt_fields)
            emit("call-started", **attempt_fields)

        def observe_usage(reported: Any) -> None:
            if observer is None:
                return
            usage = execution_event.usage_payload(
                **{name: getattr(reported, name, None) for name in _USAGE_KEYWORDS}
            )
            if usage is not None:
                emit("usage", payload=usage, **attempt_fields)

        try:
            response = backend.complete(
                request.system, request.prompt, model=selection.model, options=options
            )
        except OutputContractViolation as exc:
            # The call ran and was billed; its answer failed the contract. A
            # task error (the message carries no model text, so it can never
            # read as a halt), and the raw response survives for the caller.
            report(Attempt(chosen.id, number, chosen.pace, error=str(exc), outcome="failed"))
            observe_usage(exc.response)
            emit("result", payload={"status": "failed", "reason": "output-contract"}, **attempt_fields)
            return finish(RunResult(RUN_FAILED, chosen.id, exc.response, tuple(attempts), f"task error: {exc}"))
        except Exception as exc:  # noqa: BLE001 -- transports raise heterogeneous types
            halt = backend.classify_halt(exc)
            endpoint_failure = None
            if halt is None:
                reason = fallthrough_reason(
                    exc, caller_deadline=getattr(options, "timeout_s", None) is not None
                )
                if reason == FALLTHROUGH_BUSY:
                    # Overload the backend's own classifier did not name: the
                    # same backpressure halt (excluded, no quota verdict).
                    halt = HALT_BACKPRESSURE
                elif reason is not None:
                    endpoint_failure = reason
            launch = halt is None and (
                isinstance(exc, _LAUNCH_ERRORS) or endpoint_failure is not None
            )
            halted_payload: Dict[str, Any] = {"status": "halted"}
            halted_payload.update({"halt": halt} if halt else {"reason": "launch"})
            if halt is None and not launch:
                report(Attempt(chosen.id, number, chosen.pace, error=str(exc), outcome="failed"))
                emit("result", payload={"status": "failed", "reason": "task-error"}, **attempt_fields)
                return finish(RunResult(RUN_FAILED, chosen.id, None, tuple(attempts), f"task error: {exc}"))
            if halt in _QUOTA_HALTS:
                registry = entries
                if registry is None:
                    from llm_kit.registry.models import discover_model_entries  # noqa: PLC0415

                    registry = discover_model_entries(project_root=root)
                spec = getattr(registry.get(chosen.id), "conserve_usage", None)
                if spec is not None:
                    # entries: the halt spends every entry sharing this pool.
                    record_observed_halt(
                        chosen.id, spec, entries=registry,
                        resets_at=getattr(exc, "resets_at", None),
                    )
            if endpoint_failure == FALLTHROUGH_GROUP_EXHAUSTED:
                # The front door already walked its group: skip its members too.
                exhausted_doors.add(chosen.id)
            if launch and endpoint_failure in (None, ENDPOINT_UNREACHABLE):
                # It could not be started or connected to: unreachable for the
                # rest of the run. A timeout or an exhausted group only excludes it.
                reselector.record(
                    chosen.id, FALLTHROUGH_UNREACHABLE,
                    f"launch failed: {type(exc).__name__}: {exc}",
                )
            excluded.add(chosen.id)
            action = ""
            if request.workspace is not None:
                try:
                    if snapshot is None:
                        raise OSError(snapshot_error or "no launch snapshot")
                    action = snapshot.restore()
                except (OSError, subprocess.SubprocessError) as reset_exc:
                    action = f"workspace could not be reset: {reset_exc}"
                    report(Attempt(chosen.id, number, chosen.pace, halt=halt or "launch",
                                   error=str(exc), outcome="halted", workspace_action=action))
                    emit("result", payload=halted_payload, **attempt_fields)
                    return finish(RunResult(
                        RUN_FAILED, chosen.id, None, tuple(attempts),
                        f"{chosen.id} halted and the workspace could not be reset to its "
                        f"launch state, so no other entry was dispatched: {reset_exc}",
                    ))
            report(Attempt(chosen.id, number, chosen.pace, halt=halt or "launch",
                           error=str(exc), outcome="halted", workspace_action=action))
            emit("result", payload=halted_payload, **attempt_fields)
            if len(attempts) >= limit:
                return finish(RunResult(
                    RUN_ATTEMPT_LIMIT, chosen.id, None, tuple(attempts),
                    f"attempt limit reached ({limit})",
                ))
            continue
        if armed.controls:
            applied = tuple(getattr(response, "execution_controls_applied", ()) or ())
            missing = [c for c in armed.controls if c not in applied]
            if missing:
                # Advertisement/adapter drift: the run went out without a
                # control the requirement depended on. Never report it as a
                # completed call that met the guarantee.
                raise RuntimeError(
                    f"{chosen.id} completed without the armed control(s) "
                    f"{missing} its guarantees requirement depends on "
                    f"(applied: {list(applied)})"
                )
        report(Attempt(chosen.id, number, chosen.pace, outcome="completed"))
        observe_usage(response)
        emit("result", payload={"status": "completed"}, **attempt_fields)
        return finish(RunResult(RUN_COMPLETED, chosen.id, response, tuple(attempts)))


__all__ = [
    "CALLER_PROCESS",
    "CALLER_SESSION",
    "DISPATCHABLE_TRANSPORT",
    "DISPOSITION_EXCLUDED",
    "DISPOSITION_OUT_OF_QUOTA",
    "DISPOSITION_REQUIREMENTS_MISMATCH",
    "DISPOSITION_SHADOWED_CORE",
    "DISPOSITION_SWITCHED_OFF",
    "DISPOSITION_UNREACHABLE",
    "DISPOSITION_UNRESOLVED",
    "DISPOSITION_UNROUTABLE",
    "DISPOSITION_USABLE",
    "FALLTHROUGH_BUSY",
    "FALLTHROUGH_GROUP_EXHAUSTED",
    "FALLTHROUGH_TIMEOUT",
    "FALLTHROUGH_UNREACHABLE",
    "RUN_ATTEMPT_LIMIT",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "Attempt",
    "DeclarationSupportError",
    "Disposition",
    "EXECUTION_EVENT_BOOTSTRAP",
    "EntryState",
    "ExecutionObserver",
    "NoUsableRoutingTarget",
    "Ranking",
    "Reselector",
    "RunRequest",
    "RunResult",
    "call_with_fallthrough",
    "check_registry_entry",
    "describe",
    "fallthrough_reason",
    "order_by_pace",
    "run",
]
