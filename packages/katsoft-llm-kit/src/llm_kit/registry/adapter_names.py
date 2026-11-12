"""The adapter family names, and the retired names still accepted on read.

The transport adapter is the OpenAI-compatible HTTP completion. OpenRouter is
one endpoint it serves, not its name, so the family is ``openai-compat``.
Before llm-kit 1.11.0 the family was named ``openrouter``. Stored rows, caller
declarations and injected tables still carry that name, so every lookup and
read-back maps it to the current name, and every new write uses the current
name.
"""

from __future__ import annotations

import warnings
from typing import Mapping, Optional, TypeVar

ADAPTER_OPENAI_COMPAT = "openai-compat"
ADAPTER_CLAUDE_CLI = "claude-cli"
ADAPTER_CODEX_CLI = "codex-cli"
ADAPTER_OPENCODE_CLI = "opencode-cli"

#: Retired adapter name -> the name that replaced it.
LEGACY_ADAPTER_NAMES: Mapping[str, str] = {"openrouter": ADAPTER_OPENAI_COMPAT}

_T = TypeVar("_T")


def canonical_adapter_name(name: str) -> str:
    """``name`` with a retired adapter name mapped to its replacement."""
    return LEGACY_ADAPTER_NAMES.get(name, name)


def canonical_adapter_name_or_none(name: Optional[str]) -> Optional[str]:
    """As :func:`canonical_adapter_name`, passing ``None`` through."""
    return None if name is None else canonical_adapter_name(name)


def lookup_by_adapter(table: Mapping[str, _T], name: Optional[str]) -> Optional[_T]:
    """``table[name]``, accepting either spelling of a renamed adapter.

    The exact name wins; then the current name; then each retired name that
    maps to it. A table keyed by the old name (an injected capability table, a
    caller's constructor table) therefore keeps serving the renamed adapter,
    and a table keyed by the current name serves a caller that still says the
    old one. Returns ``None`` when no spelling is present.
    """
    if not isinstance(name, str):
        return None
    if name in table:
        return table[name]
    current = canonical_adapter_name(name)
    if current in table:
        return table[current]
    for old, new in LEGACY_ADAPTER_NAMES.items():
        if new == current and old in table:
            return table[old]
    return None


#: First release carrying the rename, and the last release that keeps the old class name.
RENAMED_SINCE = "1.11.0"
REMOVE_AFTER = "1.13.0"

OLD_BACKEND_CLASS = "OpenRouterBackend"
NEW_BACKEND_CLASS = "OpenAICompatBackend"
OLD_CAPABILITIES_NAME = "OPENROUTER_CAPABILITIES"
NEW_CAPABILITIES_NAME = "OPENAI_COMPAT_CAPABILITIES"


def warn_renamed(module: str, old: str, new: str) -> None:
    """Emit the ``DeprecationWarning`` for a read of a retired module-level name.

    Called from a module-level ``__getattr__`` so ``module.old`` keeps resolving
    to the renamed object for the deprecation window. ``stacklevel=3`` points at
    the caller of that ``__getattr__``.
    """
    warnings.warn(
        f"{module}.{old} is deprecated since {RENAMED_SINCE} and is removable "
        f"after {REMOVE_AFTER}; use {module}.{new}",
        DeprecationWarning,
        stacklevel=3,
    )


__all__ = [
    "ADAPTER_CLAUDE_CLI",
    "ADAPTER_CODEX_CLI",
    "ADAPTER_OPENAI_COMPAT",
    "ADAPTER_OPENCODE_CLI",
    "LEGACY_ADAPTER_NAMES",
    "NEW_BACKEND_CLASS",
    "NEW_CAPABILITIES_NAME",
    "OLD_BACKEND_CLASS",
    "OLD_CAPABILITIES_NAME",
    "REMOVE_AFTER",
    "RENAMED_SINCE",
    "canonical_adapter_name",
    "canonical_adapter_name_or_none",
    "lookup_by_adapter",
    "warn_renamed",
]
