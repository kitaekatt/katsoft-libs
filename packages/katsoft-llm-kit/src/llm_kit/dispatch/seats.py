"""Discover reachable harness seats around the current model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from llm_kit.registry.model_endpoints import (
    HARNESS_KIND,
    EndpointEntry,
    EndpointRegistry,
    EndpointRegistryError,
)
from llm_kit.registry.models import claude_cli_alias, discover_model_entries, load_model_config
from llm_kit.dispatch.reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    Reachability,
    check_many,
)
from llm_kit.dispatch.usage_budget import Budget, pinned_evaluate
from llm_kit.registry.model_overrides import ModelOverride, load_model_overrides

_BANDS = {1: "small", 2: "workhorse", 3: "strong", 4: "frontier"}


class SeatResolutionError(EndpointRegistryError):
    """The requested self seat is unknown, ambiguous, or not classifiable."""


@dataclass(frozen=True)
class SeatSelf:
    """The classified harness entry used as the discovery reference."""

    endpoint: str
    model: str
    tier: int
    band: str
    family: str
    harness: Optional[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "tier": self.tier,
            "band": self.band,
            "family": self.family,
            "harness": self.harness,
        }


@dataclass(frozen=True)
class Seat:
    """A candidate with relation, endpoint metadata, probe and optional budget.

    The boolean policy inputs ``disabled`` and ``reset_mode`` are effective
    switches. Budget remains the actual reading even when reset permits the
    seat. to_json returns JSON data; construction performs no I/O or checks.
    """

    relation: str
    endpoint: str
    model: str
    tier: int
    band: str
    family: str
    harness: Optional[str]
    reachability: Reachability
    budget: Optional[Budget] = None
    disabled: bool = False
    reset_mode: bool = False

    def to_json(self) -> dict[str, Any]:
        result = {
            "relation": self.relation,
            "endpoint": self.endpoint,
            "model": self.model,
            "tier": self.tier,
            "band": self.band,
            "family": self.family,
            "harness": self.harness,
            "reachability": self.reachability.to_json(),
            "disabled": self.disabled,
            "reset_mode": self.reset_mode,
        }
        if self.budget is not None:
            result["budget"] = self.budget.to_json()
        return result


@dataclass(frozen=True)
class UnclassifiedEntry:
    """A harness entry missing tier or family metadata."""

    endpoint: str
    model: str
    tier: Optional[int]
    band: Optional[str]
    family: Optional[str]
    harness: Optional[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "tier": self.tier,
            "band": self.band,
            "family": self.family,
            "harness": self.harness,
        }


@dataclass(frozen=True)
class SeatsResult:
    """Discovery records, returned by discover_seats().

    Inputs are the self record and tuples of confirmed, unclassified, unknown,
    exhausted and disabled candidates. Disabled seats are never offered;
    reset_endpoints names usable resets. to_json returns all categories.
    Construction performs no I/O or checks and raises no discovery errors.
    """

    self: SeatSelf
    seats: tuple[Seat, ...]
    unclassified: tuple[UnclassifiedEntry, ...]
    probe_unknown: tuple[Seat, ...]
    out_of_quota: tuple[Seat, ...] = ()
    disabled: tuple[Seat, ...] = ()

    @property
    def reset_endpoints(self) -> frozenset[str]:
        """Usable reset seat ids; unknown probes and observed halts are excluded."""
        return frozenset(seat.endpoint for seat in self.seats if seat.reset_mode)

    def to_json(self) -> dict[str, Any]:
        return {
            "self": self.self.to_json(),
            "seats": [seat.to_json() for seat in self.seats],
            "unclassified": [entry.to_json() for entry in self.unclassified],
            "probe_unknown": [seat.to_json() for seat in self.probe_unknown],
            "out_of_quota": [seat.to_json() for seat in self.out_of_quota],
            "disabled": [seat.to_json() for seat in self.disabled],
            "reset_endpoints": sorted(self.reset_endpoints),
        }


def _classified(entry: EndpointEntry) -> bool:
    return (
        isinstance(entry.tier, int)
        and not isinstance(entry.tier, bool)
        and entry.tier in _BANDS
        and isinstance(entry.family, str)
        and bool(entry.family.strip())
    )


def _resolve_self(self_ref: str, entries: dict[str, EndpointEntry]) -> EndpointEntry:
    direct = entries.get(self_ref)
    if direct is not None:
        if direct.kind != HARNESS_KIND:
            raise SeatResolutionError(
                f"self '{self_ref}' resolves to a non-harness endpoint"
            )
        return direct

    matches = [
        entry for entry in entries.values()
        if entry.kind == HARNESS_KIND and entry.model == self_ref
    ]
    alias = claude_cli_alias(self_ref)
    if not matches and alias is not None:
        matches = [
            entry for entry in entries.values()
            if entry.kind == HARNESS_KIND and entry.model == alias
        ]
    if not matches:
        raise SeatResolutionError(f"unknown self endpoint or model '{self_ref}'")
    if len(matches) > 1:
        names = ", ".join(sorted(entry.id for entry in matches))
        raise SeatResolutionError(
            f"ambiguous self model '{self_ref}' (matching endpoints: {names})"
        )
    return matches[0]


def _self_record(entry: EndpointEntry) -> SeatSelf:
    if not _classified(entry):
        raise SeatResolutionError(
            f"self endpoint '{entry.id}' is unclassified; tier and family are required"
        )
    assert entry.tier is not None
    assert entry.family is not None
    return SeatSelf(
        endpoint=entry.id,
        model=entry.model,
        tier=entry.tier,
        band=_BANDS[entry.tier],
        family=entry.family,
        harness=entry.harness,
    )


def _candidate_record(
    entry: EndpointEntry,
    relation: str,
    reachability: Reachability,
    budget: Optional[Budget] = None,
    policy: ModelOverride = ModelOverride(),
) -> Seat:
    assert entry.tier is not None
    assert entry.family is not None
    return Seat(
        relation=relation,
        endpoint=entry.id,
        model=entry.model,
        tier=entry.tier,
        band=_BANDS[entry.tier],
        family=entry.family,
        harness=entry.harness,
        reachability=reachability,
        budget=budget,
        disabled=policy.disabled,
        reset_mode=policy.reset_mode,
    )


def _candidate_sort_key(entry: EndpointEntry, relation: str) -> tuple[int, int, str]:
    assert entry.tier is not None
    if relation == "UP":
        return (0, -entry.tier, entry.id)
    return (1, 0, entry.id)


def _seat_sort_key(seat: Seat) -> tuple[int, int, int, int, int, str]:
    """Order confirmed seats by relation, effective quota, tier and reset.

    Quota rank sits BELOW relation and ABOVE tier: a seat's relation says
    whether it can do the job at all, which outranks how much of its budget is
    left, while two seats that can both do the job are separated by budget
    before anything else. An under-quota seat therefore stays selectable and
    simply loses to an equally-suitable peer -- which is the whole difference
    between de-prioritizing and disabling.
    """
    relation_rank = 0 if seat.relation == "UP" else 1
    quota_rank = 1 if (not seat.reset_mode and seat.budget is not None and seat.budget.deprioritized) else 0
    tier_rank = -seat.tier if seat.relation == "UP" else 0
    # Keep the pre-override order on a tie: available, behind pace, then seats
    # restored from exhaustion. This is a stable policy application to the
    # ordinary discovery result, whose exhausted seats follow confirmed ones.
    previous_rank = 0
    if seat.budget is not None:
        previous_rank = 2 if not seat.budget.usable else int(seat.budget.deprioritized)
    return (relation_rank, quota_rank, tier_rank, 0 if seat.reset_mode else 1, previous_rank, seat.endpoint)


def discover_seats(
    self_ref: str,
    *,
    project_root: Optional[str | Path] = None,
    timeout: Optional[float] = None,
    registry: Optional[EndpointRegistry] = None,
) -> SeatsResult:
    """Discover reachable UP and BESIDE harness seats.

    This callable first shipped in ``llm-scripting-kit 0.28.0``. It resolves
    ``self_ref`` by endpoint id, then by a unique model id. Only classified
    harness entries are candidates, and every eligible candidate is probed on
    every call through the concurrent reachability checker. Unreachable
    candidates are omitted; indeterminate probes are retained in
    ``probe_unknown``.

    A reachable candidate that declares ``conserve_usage`` is additionally
    checked against its subscription-usage pool
    (:mod:`llm_kit.dispatch.usage_budget`), and its verdict has two different
    effects. OUT OF QUOTA disables the seat: it is moved to ``out_of_quota``
    instead of ``seats`` -- reported rather than dropped, so a caller can tell
    "no seat above me" from "the seat above me is spent". UNDER QUOTA merely
    de-prioritizes it: the seat stays in ``seats`` and sorts after any peer of
    the same relation that is not behind pace. Both verdicts are pinned for the
    session, so a seat cannot disappear mid-session.

    The user model-overrides file applies on every call. Disabled candidates
    receive no probe and are reported in ``disabled``. Reset restores measured
    exhaustion and removes the behind-pace penalty, then sorts first within
    its relation and tier. UP still precedes BESIDE. Observed dispatch halts
    still block reset seats, and budget retains the actual reading.
    Invalid policy raises ValueError; other read failures raise OSError.
    An invalid self reference raises SeatResolutionError. No policy is written.

    ``registry`` is an injectable parsed registry for callers and tests that
    already own a fabricated registry. When omitted, the normal layered
    configuration and user registry are loaded.
    """
    root = str(project_root) if project_root is not None else None
    config = load_model_config(project_root=root)
    discovery = discover_model_entries(
        config=config, project_root=root, registry=registry
    )
    entries = dict(discovery.entries)
    overrides = load_model_overrides()
    policies = {name: overrides.resolve(name, entry.harness) for name, entry in entries.items()}
    self_entry = _resolve_self(self_ref, entries)
    self_record = _self_record(self_entry)

    unclassified = tuple(
        UnclassifiedEntry(
            endpoint=entry.id,
            model=entry.model,
            tier=entry.tier,
            band=_BANDS.get(entry.tier),
            family=entry.family,
            harness=entry.harness,
        )
        for entry in sorted(entries.values(), key=lambda item: item.id)
        if entry.kind == HARNESS_KIND
        and entry.id != self_entry.id
        and not _classified(entry)
    )

    candidates: list[tuple[EndpointEntry, str]] = []
    for entry in entries.values():
        if entry.id == self_entry.id or entry.kind != HARNESS_KIND or not _classified(entry):
            continue
        assert entry.tier is not None
        assert entry.family is not None
        if entry.tier > self_record.tier:
            candidates.append((entry, "UP"))
        elif entry.tier == self_record.tier and entry.family != self_record.family:
            candidates.append((entry, "BESIDE"))
    candidates.sort(key=lambda item: _candidate_sort_key(item[0], item[1]))

    probe_input = {
        entry.id: {"kind": entry.kind, "harness": entry.harness}
        for entry, _relation in candidates
        if not policies[entry.id].disabled
    }
    checks = check_many(
        probe_input,
        timeout=DEFAULT_VERIFY_TIMEOUT_S if timeout is None else timeout,
        project_root=root,
    )

    confirmed: list[Seat] = []
    probe_unknown: list[Seat] = []
    out_of_quota: list[Seat] = []
    disabled: list[Seat] = []
    for entry, relation in candidates:
        policy = policies[entry.id]
        if policy.disabled:
            disabled.append(_candidate_record(
                entry, relation, Reachability(STATUS_UNKNOWN, "config", "disabled by config"),
                policy=policy,
            ))
            continue
        reachability = checks.get(
            entry.id,
            Reachability(
                status=STATUS_UNKNOWN,
                checked="unknown",
                detail="reachability checker returned no result",
            ),
        )
        # The pacing verdict is computed only for a candidate that is actually
        # reachable: an endpoint already excluded by its probe gains nothing
        # from a second reason, and the read would pin a verdict for the
        # session against a seat this session never had.
        budget = None
        if reachability.status == STATUS_REACHABLE and entry.conserve_usage is not None:
            budget = pinned_evaluate(entry.id, entry.conserve_usage, entry.harness)
        candidate = _candidate_record(entry, relation, reachability, budget, policy)
        if reachability.status == STATUS_REACHABLE:
            # A spent pool DISABLES the seat; being merely behind pace does
            # not. The excluded ones are reported rather than dropped, so a
            # caller reporting "no seat above me" can tell a fleet with no
            # frontier model from one whose frontier model is out of quota --
            # only the second answer changes when the window resets.
            if not policy.budget_usable(budget):
                out_of_quota.append(candidate)
            else:
                confirmed.append(candidate)
        elif reachability.status == STATUS_UNKNOWN:
            probe_unknown.append(candidate)

    confirmed.sort(key=_seat_sort_key)
    return SeatsResult(
        self=self_record,
        seats=tuple(confirmed),
        unclassified=unclassified,
        probe_unknown=tuple(probe_unknown),
        out_of_quota=tuple(out_of_quota),
        disabled=tuple(disabled),
    )


__all__ = [
    "Budget",
    "Seat",
    "SeatResolutionError",
    "SeatSelf",
    "SeatsResult",
    "UnclassifiedEntry",
    "discover_seats",
]
