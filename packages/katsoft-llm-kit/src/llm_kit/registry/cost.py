"""Cost classes for declared model entries (pure; registry layer, imports nothing outside registry).

Every declared entry is classified as one of three cost classes:

* ``local``        -- an unmetered transport, or a front door whose every tier is unmetered.
* ``subscription`` -- a harness running on a subscription session pool (claude or codex
  with SUBSCRIPTION auth).
* ``metered``      -- paid OR unverified billing: anything metered, unknown, undeclared,
  or not provably local/subscription. The class states that billing could not be shown
  to be free; it never claims that a charge occurred.

Classification fails closed: an entry that cannot be shown to be local or subscription
is ``metered``. Auth state for harnesses is supplied by the caller (see
``llm_kit.adapters.harness_auth``); this module performs no I/O. A classification is
information and is always reported with its reason; whether a caller may use a class
is the optional consent layer's business (``llm_kit.registry.consent``).

Classification rules, applied in order:

1. ``entry is None`` -> metered ("no registry entry: billing undeclared").
2. ``kind == "harness"`` with ``h = (entry.harness or "").lower()``: claude or codex
   with ``harness_auth[h].state == SUBSCRIPTION`` is subscription; otherwise (including
   a missing key) metered, with the reason taken from the auth record; opencode or any
   other harness is metered.
3. ``kind == "transport"`` with ``frontdoor: true``: tiers are the transport entries in
   ``entries`` whose ``routing.group == entry.model``. Local only if that set is
   non-empty and every tier is unmetered; otherwise metered. The entry's own
   ``billing`` does not count.
4. Any other transport is local if unmetered, metered otherwise.
5. Any other kind is metered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from llm_kit.registry.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry

# The harness auth states SUBSCRIPTION and METERED share their spelling with the
# cost classes of the same name: a claude or codex harness on SUBSCRIPTION auth is
# class ``subscription``, and METERED auth is class ``metered``. UNKNOWN auth is
# class ``metered`` (fail closed).
LOCAL, SUBSCRIPTION, METERED, UNKNOWN = "local", "subscription", "metered", "unknown"
COST_CLASSES = (LOCAL, SUBSCRIPTION, METERED)

# Billing-diverting environment variables: any of these set and non-empty routes
# Claude billing away from the subscription session pool. Must equal
# ``llm_kit.pipeline.execution.drivers.claude_bg.BILLING_DIVERTING_VARS``.
BILLING_DIVERTING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)


@dataclass(frozen=True)
class HarnessAuth:
    """How one harness is authenticated: ``state`` is SUBSCRIPTION, METERED or UNKNOWN.

    ``reason`` names fields and variables, never secret values.
    """

    state: str
    reason: str


@dataclass(frozen=True)
class CostVerdict:
    """A cost class (LOCAL, SUBSCRIPTION or METERED) and the reason for it."""

    cost_class: str
    reason: str


def is_unmetered(entry: EndpointEntry) -> bool:
    """True when the entry's billing mode is ``unmetered`` (the single paid/unpaid rule)."""
    return entry.billing_mode == "unmetered"


def normalize_allow(allow: Optional[Iterable[str]]) -> Optional[frozenset]:
    """Normalize an allow set to a frozenset of cost classes; None stays None.

    Raises ValueError on an unknown cost class. Consumes a generator once.
    """
    if allow is None:
        return None
    if isinstance(allow, str):
        raise ValueError(
            f"allow must be an iterable of cost classes, not the string {allow!r}; "
            f"valid classes: {', '.join(COST_CLASSES)}"
        )
    names = frozenset(allow)
    unknown = sorted(str(n) for n in names if n not in COST_CLASSES)
    if unknown:
        raise ValueError(
            f"unknown cost class {', '.join(repr(u) for u in unknown)}; "
            f"valid classes: {', '.join(COST_CLASSES)}"
        )
    return names


def _classify_harness(
    entry: EndpointEntry, harness_auth: Optional[Mapping[str, HarnessAuth]]
) -> CostVerdict:
    name = (entry.harness or "").lower()
    if name not in ("claude", "codex"):
        return CostVerdict(METERED, f"harness {entry.harness!r} has no subscription rule (billing unverified)")
    auth = (harness_auth or {}).get(name)
    if auth is None:
        return CostVerdict(METERED, f"no auth state for harness {name} (billing unverified)")
    if auth.state == SUBSCRIPTION:
        return CostVerdict(SUBSCRIPTION, f"{name} on subscription auth: {auth.reason}")
    return CostVerdict(METERED, f"{name} auth is {auth.state} (billing unverified): {auth.reason}")


def _classify_frontdoor(
    entry: EndpointEntry, entries: Optional[Mapping[str, EndpointEntry]]
) -> CostVerdict:
    group = entry.model
    tiers = [
        e
        for e in (entries or {}).values()
        if e.kind == TRANSPORT_KIND and e.routing is not None and e.routing.group == group
    ]
    if not tiers:
        return CostVerdict(METERED, f"no tiers declared for group {group} (billing unverified)")
    for tier in sorted(tiers, key=lambda t: t.id):
        if not is_unmetered(tier):
            return CostVerdict(METERED, f"tier {tier.id} is not declared unmetered (billing unverified)")
    return CostVerdict(LOCAL, f"every tier of group {group} is unmetered")


def classify(
    entry: Optional[EndpointEntry],
    *,
    entries: Optional[Mapping[str, EndpointEntry]] = None,
    harness_auth: Optional[Mapping[str, HarnessAuth]] = None,
) -> CostVerdict:
    """Classify one entry per the module rules. Pure; never probes auth."""
    if entry is None:
        return CostVerdict(METERED, "no registry entry: billing undeclared")
    if entry.kind == HARNESS_KIND:
        return _classify_harness(entry, harness_auth)
    if entry.kind == TRANSPORT_KIND:
        if entry.frontdoor:
            return _classify_frontdoor(entry, entries)
        if is_unmetered(entry):
            return CostVerdict(LOCAL, "billing.mode unmetered")
        return CostVerdict(METERED, "billing not declared unmetered (billing unverified)")
    return CostVerdict(METERED, f"unknown entry kind {entry.kind!r} (billing unverified)")


def cost_class(entry: Optional[EndpointEntry], **kw) -> str:
    """The cost class name only: ``classify(entry, **kw).cost_class``."""
    return classify(entry, **kw).cost_class
