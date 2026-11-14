"""Construct completion backends from the shared endpoint configuration.

The selection-style factory: an endpoint name (or the configured default) in,
a :class:`BackendSelection` out. The spec-style construction builder that
the pipeline wrappers use is :mod:`llm_kit.dispatch.pipeline_backends`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from llm_kit.adapters.backends import ClaudeCliBackend, OpenAICompatBackend
from llm_kit.adapters.capabilities import CLAUDE_AGENT_CAPABILITIES, OPENAI_COMPAT_CAPABILITIES
from llm_kit.adapters.claude_session import ClaudeAgentSession
from llm_kit.adapters.codex_backend import CodexCliBackend
from llm_kit.adapters.endpoint_profile import endpoint_capabilities, profile_from_resolved
from llm_kit.adapters.opencode_backend import OpencodeCliBackend
from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.session_types import AgentSessionBackend
from llm_kit.completion.types import LLMBackend
from llm_kit.registry.model_endpoints import HARNESS_KIND, EndpointEntry
from llm_kit.registry.models import (
    EndpointResolveError,
    ModelResolveError,
    default_endpoint_name,
    discover_model_entries,
    load_model_config,
    resolve_endpoint,
    resolve_model,
)


@dataclass(frozen=True)
class BackendSelection:
    """A configured backend plus the model and defaults it should receive.

    ``effort`` is the entry's DECLARED default (a harness ``effort`` or a
    transport ``reasoning_effort``); whether a transport delivers it is in
    ``capabilities``, which is the adapter record specialized to this endpoint
    (the family record for a harness, or for a transport that delivers no
    effort). None only for a selection built without the factory.
    """

    endpoint: str
    kind: str
    backend: LLMBackend
    model: str
    effort: Optional[str] = None
    capabilities: Optional[Capabilities] = None


def _harness_entry(
    name: str, *, config: dict, project_root: Optional[str]
) -> Optional[EndpointEntry]:
    raw = (config.get("endpoints") or {}).get(name)
    if isinstance(raw, dict):
        if "harness" not in raw:
            return None
        harness = raw.get("harness")
        model = raw.get("model")
        if not isinstance(harness, str) or not harness.strip():
            raise EndpointResolveError(
                f"endpoint '{name}' is a harness entry and has no 'harness'"
            )
        if not isinstance(model, str) or not model.strip():
            raise EndpointResolveError(
                f"endpoint '{name}' is a harness entry and has no 'model'"
            )
        effort = raw.get("effort")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise EndpointResolveError(
                f"endpoint '{name}' has an invalid 'effort' ({effort!r})"
            )
        return EndpointEntry(
            id=name, base_url=None, model=model.strip(), kind=HARNESS_KIND,
            harness=harness.strip(), effort=effort.strip() if effort else None,
        )
    return discover_model_entries(config=config, project_root=project_root).get(name)


def create_backend(
    endpoint: Optional[str] = None,
    *,
    model: Optional[str] = None,
    cheap: bool = False,
    project_root: Optional[str | Path] = None,
) -> BackendSelection:
    """Create the backend selected by a configured transport or harness entry.

    ``resolve_endpoint`` deliberately rejects harnesses because it is the HTTP
    resolver.  This factory classifies discovery entries before crossing that
    boundary, preserving that invariant while providing one host-neutral
    dispatch API.
    """
    root = str(project_root) if project_root is not None else None
    config = load_model_config(project_root=root)
    name = endpoint or default_endpoint_name(config)
    entry = _harness_entry(name, config=config, project_root=root)
    if entry is not None and entry.kind == HARNESS_KIND:
        selected_model = model or entry.model
        harness = (entry.harness or "").lower()
        if harness == "claude":
            backend: LLMBackend = ClaudeCliBackend()
        elif harness == "codex":
            backend = CodexCliBackend()
        elif harness == "opencode":
            backend = OpencodeCliBackend()
        else:
            raise EndpointResolveError(
                f"endpoint '{name}' uses unsupported harness '{entry.harness}'"
            )
        return BackendSelection(
            endpoint=name,
            kind=HARNESS_KIND,
            backend=backend,
            model=selected_model,
            effort=entry.effort,
            capabilities=getattr(backend, "capabilities", None),
        )

    resolved = resolve_endpoint(name, config=config, project_root=root)
    selected_model = resolve_model(
        model, cheap=cheap, config=config, endpoint=name, project_root=root
    )
    return BackendSelection(
        endpoint=name,
        kind="transport",
        backend=OpenAICompatBackend(endpoint=name, project_root=Path(root) if root else None),
        model=selected_model,
        effort=(resolved.get("request_defaults") or {}).get("reasoning_effort"),
        capabilities=endpoint_capabilities(
            OPENAI_COMPAT_CAPABILITIES, profile_from_resolved(resolved, endpoint=name)
        ),
    )


def create_transport_backend(
    endpoint: Optional[str] = None,
    *,
    model: Optional[str] = None,
    cheap: bool = False,
    project_root: Optional[str | Path] = None,
) -> BackendSelection:
    """``create_backend`` for a caller that dispatches TRANSPORT entries only.

    A harness entry is refused with ``EndpointResolveError``, which
    ``declaration.describe`` reads as "unroutable here" and skips silently. A
    model alias that does not resolve is reported the same way, so a bad
    per-entry override lands in the floor's itemised list instead of escaping
    as a crash. ``model`` and ``cheap`` are the per-entry override: they pick
    within the entry's own model map and selectors.
    """
    root = str(project_root) if project_root is not None else None
    config = load_model_config(project_root=root)
    name = endpoint or default_endpoint_name(config)
    entry = _harness_entry(name, config=config, project_root=root)
    if entry is not None and entry.kind == HARNESS_KIND:
        raise EndpointResolveError(
            f"entry '{name}' is a {entry.harness} harness entry; this caller "
            "dispatches transport entries only"
        )
    try:
        return create_backend(name, model=model, cheap=cheap, project_root=project_root)
    except ModelResolveError as exc:
        raise EndpointResolveError(str(exc)) from exc


class AgentSessionUnsupportedError(ValueError):
    """A selected backend cannot run as an agent session.

    Raised by :func:`create_agent_session` for a selection whose backend is not
    the Claude CLI completion backend, or is that backend in text-only mode
    (an agent session always has tools).
    """


def agent_session_capabilities() -> Capabilities:
    """The advertisement an agent-session job is selected and armed against.

    The record of :class:`~llm_kit.adapters.claude_session.ClaudeAgentSession`
    (adapter ``claude-cli``): no ``no-session-persistence``, no text-only mode,
    ``permission-bypass`` fixed. ``llm_kit.jobs`` reaches the adapter tables
    only through ``dispatch``, so it asks here.
    """
    return CLAUDE_AGENT_CAPABILITIES


def create_agent_session(selection: BackendSelection) -> AgentSessionBackend:
    """The agent-session backend for a selected Claude CLI entry.

    ``selection.backend`` must be a :class:`ClaudeCliBackend` that is not in
    text-only mode; its ``executable`` carries over, so a test or caller that
    names a Claude build for the completion backend gets the same build for the
    session. Returns a :class:`~llm_kit.adapters.claude_session
    .ClaudeAgentSession`. Raises :class:`AgentSessionUnsupportedError`
    otherwise; nothing is started.
    """
    backend = selection.backend
    if not isinstance(backend, ClaudeCliBackend):
        raise AgentSessionUnsupportedError(
            f"entry {selection.endpoint!r} ({getattr(backend, 'name', type(backend).__name__)}) "
            "is not the Claude CLI; only a Claude harness entry can run an agent session"
        )
    if backend.text_only:
        raise AgentSessionUnsupportedError(
            f"entry {selection.endpoint!r} was armed text-only; an agent session cannot run without tools"
        )
    return ClaudeAgentSession(executable=backend.executable)


__all__ = [
    "AgentSessionUnsupportedError",
    "BackendSelection",
    "agent_session_capabilities",
    "create_agent_session",
    "create_backend",
    "create_transport_backend",
]
