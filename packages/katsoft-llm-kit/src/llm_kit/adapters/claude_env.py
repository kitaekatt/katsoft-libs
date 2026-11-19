"""The single definition of the environment a ``claude`` child process gets.

Every Claude spawn path in this package builds its child's environment with
:func:`_claude_child_env` and nothing else:

- the completion backend (``ClaudeCliBackend.complete``, ``adapters/backends.py``),
- the agent session (``ClaudeAgentSession``, ``adapters/claude_session.py``),
- the relay's resumed turn (``relay/resume.py``, ``child_env``),
- the ``--bg`` driver's worker launch (``compose_worker_environment``,
  ``pipeline/execution/drivers/claude_bg.py``).

``tests/llm-kit/test_claude_spawn_env.py`` checks that each path passes an
environment this function made, that the parent's ``CLAUDECODE`` reaches no
child, and that every spawn call in ``llm_kit/adapters`` and those spawn
modules passes an explicit ``env=``.

The module is private to the package: no caller outside ``llm_kit`` needs it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional

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

    Billing variables (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL`` and the
    rest of ``claude_bg.BILLING_DIVERTING_VARS``) are deliberately NOT removed
    here yet: whether every Claude child drops them is an open decision of the
    library's owner. Until it is made, only the ``--bg`` driver removes them,
    as it did before, in ``compose_worker_environment`` after this function.

    Removals each path adds through ``remove_prefixes`` (each one predates this
    function and is kept unchanged):

    - the relay's resumed turn: every ``CLAUDE_CODE_*`` name.

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

    def removed(name: str) -> bool:
        return name in _ALWAYS_REMOVED or any(name.startswith(prefix) for prefix in remove_prefixes)

    clash = sorted(name for name in added if removed(name))
    if clash:
        raise ValueError(f"extra sets {clash!r}, which the Claude child environment removes")
    env = {name: value for name, value in source.items() if not removed(name)}
    env.update(added)
    return env
