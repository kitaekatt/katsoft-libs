"""The production labeller: haiku (or any registry entry) writes the labels, under the user's consent.

:func:`model_labeller` returns a :class:`~llm_kit.graph.render.label_types.Labeller`
that turns each :class:`~llm_kit.graph.render.label_types.LabelCall` into one model
completion through the declaration seam (``llm_kit.dispatch.declaration``). The
library makes no model call of its own: only a caller that builds this labeller
and hands it to the labelling core spends anything.

Consent
-------
The labeller opts in to the consent layer (``llm_kit.registry.consent``) under the
fixed scope :data:`CONSENT_SCOPE`. The first call opens a gate, once, for the
labeller's life:

1. read the policy of that scope;
2. ``describe`` the declaration with that policy and the requirement that the call
   has no tools (see "Text only"), probing authentication and reachability in the
   project root;
3. fix the APPROVED set, the ids of the usable entries of that ranking, and keep
   only their registry entries.

Every call after the gate is restricted to the approved entries: the entry map
handed to ``run`` holds nothing else, so no refused id can be selected, and a
front door's group members that the gate never approved are never expanded. A
fallback can therefore move between approved entries and nowhere else. A refused
gate stays refused: later calls raise the same refusal without reading consent
again.

Text only
---------
Every call carries the ``guarantees`` requirement ``filesystem-write``,
``shell-exec`` and ``subagent-spawn``. The declaration seam admits only an entry
that can honour it, and arms the call: a claude entry runs ``claude -p`` with no
tools, no MCP servers, no skills and no settings (``--tools ""`` and the isolation
flags), so text in the evidence cannot reach a file, a shell or another agent.

Working directory
-----------------
Every call runs in the project root (``BackendOptions(cwd=root)``) and the gate
probes authentication there (``project_root=root``), whatever the caller's working
directory is. The CLI would otherwise run where the caller stands while codex or
claude authentication is judged in the root.

Follow-ups
----------
A follow-up call (``call.followup``) names the entry that served the primary call
(``call.entry``). It is dispatched to that entry alone, so the labels of one graph
file come from one model per call and each call is attributed truthfully.

Threads
-------
A labeller may be called from several threads (the labelling core runs its calls
in a pool). What is shared between threads:

* the gate result (the approved ids and their entries): written once under a
  lock, read-only after;
* the reachability cache: one lock-guarded mapping, so an entry one call finds
  unreachable is skipped by the others, and no call reads it while another writes;
* the frozen call options.

Everything else belongs to one call: ``run`` builds its own re-selector, attempt
list and backend object for each call. The quota verdict cache that
``conserve_usage`` entries use is guarded inside ``llm_kit.dispatch.usage_budget``.

Errors
------
Nothing is dispatched and :class:`~llm_kit.graph.render.label_types.LabelRefused`
is raised when consent does not allow the call, the consent file cannot be read,
no entry is usable, or a follow-up names no approved entry. The message names the
exact ``llm-kit consent set`` command that would allow the call, and
``--no-labels``. A call that ran and gave no usable labels raises
:class:`~llm_kit.graph.render.label_types.LabelFailed`: every approved entry
halted, the call failed, or the answer is not a JSON object that fits the call's
schema. No answer is repaired and no failure is degraded to a partial result.
"""

from __future__ import annotations

import json
import threading
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from llm_kit.completion.types import BackendOptions
from llm_kit.registry import consent as consent_store
from llm_kit.schema.json_schema import validate as validate_value

from .label_types import LabelCall, LabelFailed, LabelRefused, LabelReply, Labeller

__all__ = ["CONSENT_SCOPE", "DEFAULT_LABEL_MODELS", "DEFAULT_TIMEOUT_S", "model_labeller"]

#: The consent scope every label call opts in under. Fixed: ``llm-kit consent set --scope graph-labels ...``.
CONSENT_SCOPE = "graph-labels"

#: The registry entries that write labels when the caller names none.
DEFAULT_LABEL_MODELS = ("haiku",)

#: Wall-clock cap of one label call, in seconds.
DEFAULT_TIMEOUT_S = 120.0

#: The requirement that makes a call text-only: admits only an entry that denies these, and arms it.
_GUARANTEES = {"guarantees": ["filesystem-write", "shell-exec", "subagent-spawn"]}

#: Characters of a backend's text kept in an error message.
_QUOTE_CHARS = 300


def _clip(text: str, limit: int = _QUOTE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "...[cut]"


class _SharedReachability(MutableMapping):
    """The reachability cache shared by every call of one labeller: every access is under one lock.

    ``describe`` copies and updates the cache it is given, from several threads at
    once here; iteration works on a snapshot so no reader meets a changing dict.
    """

    def __init__(self) -> None:
        self._items: dict = {}
        self._lock = threading.Lock()

    def __getitem__(self, key):
        with self._lock:
            return self._items[key]

    def __setitem__(self, key, value) -> None:
        with self._lock:
            self._items[key] = value

    def __delitem__(self, key) -> None:
        with self._lock:
            del self._items[key]

    def __iter__(self) -> Iterator:
        with self._lock:
            return iter(list(self._items))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class _Approved:
    """What the gate fixed: the ordered approved ids and their registry entries (read-only after the gate)."""

    def __init__(self, ids: Sequence[str], entries: Mapping[str, Any]) -> None:
        self.ids = tuple(ids)
        self.entries = dict(entries)


class _ModelLabeller:
    """A :class:`Labeller` over the declaration seam; see the module docstring for the contract."""

    def __init__(
        self,
        declaration: tuple,
        *,
        root: Path,
        timeout_s: float,
        policy_loader: Callable[[str], Any],
        harness_auth: Optional[Mapping[str, Any]],
        entries: Optional[Mapping[str, Any]],
        backend_factory: Optional[Callable[..., Any]],
    ) -> None:
        self.declaration = declaration
        self._root = root
        self._options = BackendOptions(timeout_s=timeout_s, cwd=root)
        self._policy_loader = policy_loader
        self._harness_auth = harness_auth
        self._entries = entries
        self._backend_factory = backend_factory
        self._reachability = _SharedReachability()
        self._gate_lock = threading.Lock()
        self._approved: Optional[_Approved] = None
        self._refusal: Optional[LabelRefused] = None

    # -- the gate ------------------------------------------------------------

    def _gate(self) -> _Approved:
        """The approved set, opened on first use and fixed for the labeller's life; a refusal is remembered."""
        with self._gate_lock:
            if self._refusal is not None:
                raise LabelRefused(str(self._refusal)) from self._refusal
            if self._approved is None:
                try:
                    self._approved = self._open_gate()
                except LabelRefused as exc:
                    self._refusal = exc
                    raise
            return self._approved

    def _open_gate(self) -> _Approved:
        from llm_kit.dispatch import declaration as dispatch

        try:
            policy = self._policy_loader(CONSENT_SCOPE)
        except (OSError, ValueError) as exc:
            raise LabelRefused(
                "graph labels were not made and nothing was sent to a model: the consent policy for scope "
                f"{CONSENT_SCOPE!r} could not be read: {exc}. Fix that file, or render without labels with --no-labels."
            ) from exc
        entries = self._entries
        if entries is None:
            from llm_kit.registry.models import discover_model_entries

            entries = discover_model_entries(project_root=str(self._root))
        try:
            ranking = dispatch.describe(
                list(self.declaration),
                caller=dispatch.CALLER_PROCESS,
                consent_policy=policy,
                requirements=_GUARANTEES,
                project_root=str(self._root),
                reachability_cache=self._reachability,
                entries=entries,
                backend_factory=self._backend_factory,
                harness_auth=self._harness_auth,
            )
        except dispatch.NoUsableRoutingTarget as exc:
            raise LabelRefused(self._floor_text(exc, policy, dispatch)) from exc
        ids = [state.id for state in ranking.rendered_entries if state.usable]
        return _Approved(ids, {entry_id: entries[entry_id] for entry_id in ids})

    def _floor_text(self, exc: Exception, policy: Any, dispatch: Any) -> str:
        commands: list = []
        for disposition in exc.dispositions:  # type: ignore[attr-defined]
            if disposition.disposition == dispatch.DISPOSITION_SWITCHED_OFF and disposition.cost_class in policy.classes:
                for command in policy.classes[disposition.cost_class].commands:
                    if command not in commands:
                        commands.append(command)
        text = f"graph labels were not made and nothing was sent to a model: {exc}"
        if commands:
            text += f"\nTo allow it, run {' and '.join('`' + c + '`' for c in commands)}."
        return text + "\nTo render without labels, pass --no-labels."

    # -- one call ------------------------------------------------------------

    def __call__(self, call: LabelCall) -> LabelReply:
        approved = self._gate()
        if call.followup:
            if call.entry is None or call.entry not in approved.entries:
                raise LabelRefused(
                    f"nothing was sent for the follow-up of {self._where(call)}: it must go to the entry that served "
                    f"the primary call, and {call.entry!r} is not one of the approved entries {list(approved.ids)}. "
                    "To render without labels, pass --no-labels."
                )
            names = [call.entry]
        else:
            names = list(approved.ids)
        text, entry, model = self._dispatch(call, names, approved)
        return LabelReply(answer=self._parse(call, text), entry=entry, model=model)

    @staticmethod
    def _where(call: LabelCall) -> str:
        return f"{call.file} (call {call.index}{', follow-up' if call.followup else ''})"

    def _dispatch(self, call: LabelCall, names: list, approved: _Approved) -> tuple:
        """One ``run`` over ``names``, restricted to the approved entries: ``(text, serving entry, model)``."""
        from llm_kit.dispatch import declaration as dispatch

        attempts: list = []
        try:
            result = dispatch.run(
                names,
                dispatch.RunRequest(system=call.system, prompt=call.prompt, options=self._options),
                requirements=_GUARANTEES,
                entries=approved.entries,
                project_root=str(self._root),
                reachability_cache=self._reachability,
                backend_factory=self._backend_factory,
                on_attempt=attempts.append,
            )
        except dispatch.NoUsableRoutingTarget as exc:
            if attempts:
                raise LabelFailed(
                    f"every approved entry halted for {self._where(call)}: "
                    f"{'; '.join(f'{a.entry}: {_clip(a.halt or a.error or a.outcome)}' for a in attempts)}"
                ) from exc
            raise LabelRefused(f"nothing was sent for {self._where(call)}: {exc}\nTo render without labels, pass --no-labels.") from exc
        except ValueError as exc:
            raise LabelRefused(f"nothing was sent for {self._where(call)}: {_clip(exc)}") from exc
        except RuntimeError as exc:
            raise LabelFailed(f"the call for {self._where(call)} could not be trusted: {_clip(exc)}") from exc
        if result.status != dispatch.RUN_COMPLETED:
            tried = "; ".join(f"{a.entry}: {_clip(a.halt or a.error or a.outcome)}" for a in result.attempts)
            raise LabelFailed(f"the call for {self._where(call)} did not complete ({result.status}): {_clip(result.detail)} [{tried}]")
        response = result.response
        served = getattr(response, "reported_model", None) or response.model
        return response.text, result.entry, served

    def _parse(self, call: LabelCall, text: str) -> Mapping[str, Any]:
        where = self._where(call)
        if not isinstance(text, str) or not text.strip():
            raise LabelFailed(f"the model gave an empty answer for {where}")
        try:
            answer = json.loads(text)
        except ValueError as exc:
            raise LabelFailed(f"the answer for {where} is not JSON ({exc}): {_clip(ascii(text))}") from exc
        if not isinstance(answer, dict):
            raise LabelFailed(f"the answer for {where} is not a JSON object: {_clip(ascii(text))}")
        errors = validate_value(call.schema, answer)
        if errors:
            shown = ", ".join(f"{pointer or '/'} ({keyword})" for pointer, keyword in errors[:5])
            raise LabelFailed(f"the answer for {where} does not fit the answer schema: {shown}")
        return answer


def model_labeller(
    declaration: Sequence[str] = DEFAULT_LABEL_MODELS,
    *,
    project_root: "str | Path",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    policy_loader: Optional[Callable[[str], Any]] = None,
    harness_auth: Optional[Mapping[str, Any]] = None,
    entries: Optional[Mapping[str, Any]] = None,
    backend_factory: Optional[Callable[..., Any]] = None,
) -> Labeller:
    """A labeller that asks the registry entries named in ``declaration`` to write graph labels.

    Parameters:
        declaration: registry entry ids in preference order (default
            :data:`DEFAULT_LABEL_MODELS`, ``("haiku",)``). It becomes the labeller's
            ``declaration`` attribute, which enters the cache key. Not empty, no
            repeats, every id a non-empty string.
        project_root: the directory every call runs in and where the gate probes
            authentication and reads the project's model file; resolved once.
        timeout_s: wall-clock cap of one call (positive).
        policy_loader: ``scope -> consent Policy``; the default reads the user's
            consent file (``llm_kit.registry.consent.policy``). A test seam.
        harness_auth: ``{harness: HarnessAuth}``; the default probes the claude or
            codex login in ``project_root`` when the gate first needs it. A test seam.
        entries: the registry entry map; the default is the merged registry of
            ``project_root``. A test seam.
        backend_factory: ``factory(entry_id, project_root=...)`` returning a
            ``BackendSelection``; the default builds the real backend. A test seam.

    Returns:
        A ``Labeller``. Building it reads nothing and calls nothing; the first
        call opens the consent gate (see the module docstring), and a render whose
        labels are all cached never calls it. It is safe to call from several threads.

    Raises:
        ValueError: ``declaration`` is malformed, or ``timeout_s`` is not positive.

    A call raises ``LabelRefused`` when nothing was sent (consent, no usable entry,
    a follow-up for an entry the gate did not approve) and ``LabelFailed`` when a
    call ran and gave no usable answer; see the module docstring.
    """
    if isinstance(declaration, (str, bytes)) or not isinstance(declaration, Sequence):
        raise ValueError(f"declaration must be a list of registry entry ids, got {declaration!r}")
    ids = tuple(declaration)
    if not ids:
        raise ValueError("declaration must name at least one registry entry")
    for entry_id in ids:
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError(f"declaration entries must be non-empty strings, got {entry_id!r}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"declaration repeats an entry: {list(ids)}")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError(f"timeout_s must be a positive number, got {timeout_s!r}")
    return _ModelLabeller(
        ids,
        root=Path(project_root).resolve(),
        timeout_s=float(timeout_s),
        policy_loader=policy_loader or consent_store.policy,
        harness_auth=harness_auth,
        entries=entries,
        backend_factory=backend_factory,
    )
