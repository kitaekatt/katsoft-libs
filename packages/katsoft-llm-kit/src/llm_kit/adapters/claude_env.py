"""The single definition of the environment a ``claude`` child process gets.

Every spawn of the Claude CLI in this package builds its child's environment
with :func:`_claude_child_env`:

- the completion backend (``ClaudeCliBackend.complete``, ``adapters/backends.py``),
- the agent session (``ClaudeAgentSession``, ``adapters/claude_session.py``),
- the relay's resumed turn (``relay/resume.py``, ``child_env``),
- the ``--bg`` driver's worker launch (``compose_worker_environment``) and every
  other ``ClaudeCli`` call (``agents``, ``stop``, ``rm``, ``respawn``,
  ``--version``, the preflight probes), all through ``ClaudeCli._invoke``
  (``pipeline/execution/drivers/claude_bg.py``),
- the sign-in probe ``claude auth status`` (``harness_auth.claude_auth``),
- the reachability probe ``claude --version`` (``dispatch/reachability.py``).

A command the caller names is not a Claude spawn of this package, even when it
starts ``claude`` itself: a job's contract command (``jobs/run.py``
``run_contract``), a compare scorer (``runs/compare.py`` ``command_scorer``),
a supervised process and a graph ``llm.subprocess`` node. Each gets the
environment its caller's own rule gives it, unchanged. The signed import graph
(``docs/reference/llm-kit-architecture.md``) keeps ``jobs`` and ``runs`` from
importing ``adapters``, where this definition lives, and the package cannot see
whether a command, a script or a wrapper starts ``claude``. A caller whose
command starts ``claude`` inside a Claude Code session removes ``CLAUDECODE``
from that command's environment itself.

``tests/llm-kit/test_claude_spawn_env.py`` proves this: each path above is run
through its real code (with a spy runner where the path takes an injected
runner) and must spawn with an environment this function made, without the
parent's ``CLAUDECODE``; and an AST scan of the whole package classifies every
process spawn as one of these sites or as a named exception with its reason
(git, the Codex CLI, the package's own Python workers, and the caller-named
commands above).

The module is private to the package: no caller outside ``llm_kit`` needs it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional

from llm_kit.registry.consent import env_name_key

#: Removed from every Claude child. ``CLAUDECODE`` marks a process as running
#: inside Claude Code; a ``claude`` started from inside a Claude Code session
#: must not inherit it.
_ALWAYS_REMOVED = ("CLAUDECODE",)


def _claude_child_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    remove_prefixes: tuple[str, ...] = (),
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """The environment of a ``claude`` child process: the one definition.

    The child gets the parent's whole environment except ``CLAUDECODE``, plus
    the spawn path's own additions. It is not an allow-list: the CLI needs
    varied sign-in, proxy, certificate, profile and claudx variables, and an
    allow-list would break sign-in on some machines.

    Names compare as the platform compares them
    (:func:`llm_kit.registry.consent.env_name_key`): in any case on Windows, so
    a caller's mapping that spells ``claudecode`` loses it too; exactly
    elsewhere.

    Billing variables (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL`` and the
    rest of ``claude_bg.BILLING_DIVERTING_VARS``) are deliberately NOT removed
    here yet: whether every Claude child drops them is an open decision of the
    library's owner. Until it is made, only the ``--bg`` driver removes them,
    as it did before, in ``compose_worker_environment`` after this function
    (and ``claude_auth`` does not spawn at all while one is set).

    Removals each path adds (each one predates this function and is kept
    unchanged):

    - the relay's resumed turn: every ``CLAUDE_CODE_*`` name (``remove_prefixes``).

    Args:
        base: the parent environment; ``None`` reads ``os.environ`` at the call.
            It is not modified.
        remove_prefixes: further name prefixes to remove, for a path that
            removed them before this definition existed.
        extra: the path's own variables (a process tag, a run's recorded
            overlay), set after the removals.

    Returns:
        A new ``dict``: ``base`` without ``CLAUDECODE`` and without any name
        that starts with one of ``remove_prefixes``, updated with ``extra``.

    Raises:
        ValueError: ``extra`` names a variable this call removes.
    """
    source = os.environ if base is None else base
    added = dict(extra or {})
    always = {env_name_key(name) for name in _ALWAYS_REMOVED}
    prefixes = tuple(env_name_key(prefix) for prefix in remove_prefixes)

    def removed(name: str) -> bool:
        key = env_name_key(name)
        return key in always or key.startswith(prefixes)  # startswith(()) is False

    clash = sorted(name for name in added if removed(name))
    if clash:
        raise ValueError(f"extra sets {clash!r}, which the Claude child environment removes")
    env = {name: value for name, value in source.items() if not removed(name)}
    env.update(added)
    return env
