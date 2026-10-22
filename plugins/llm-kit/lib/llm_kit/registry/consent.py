"""Per-machine consent for cost classes (registry layer).

The consent file records which non-free cost classes (usage, spend) the user
allows callers to use. Free is always allowed. The file lives at
:func:`default_consent_path`, resolved on each call (never at import).

File format: ``{"schema": 1, "usage": bool, "spend": bool, "updated_at": iso}``.
Only real booleans are accepted. An absent, unreadable or malformed file means
free-only, with ``Consent.note`` explaining why. Callers that opt in must print
``Consent.note`` themselves; the dispatch floor does not carry it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def default_consent_path() -> Path:
    """``Path.home()/.claude/plugins/data/plugins-kit/llm-kit/consent.json``, resolved per call."""
    raise NotImplementedError("U2")


@dataclass(frozen=True)
class Consent:
    """The loaded consent. ``source`` is ``file``, ``absent`` or ``unreadable``."""

    usage: bool
    spend: bool
    source: str
    path: Path
    note: str

    @property
    def allowed(self) -> frozenset:
        """The allowed cost classes: always ``free``, plus ``usage``/``spend`` when consented."""
        raise NotImplementedError("U2")


def load_consent(path: Optional[Path] = None) -> Consent:
    """Load consent; catches OSError, decode and schema errors -> free-only plus a note."""
    raise NotImplementedError("U2")


def save_consent(*, usage: bool, spend: bool, path: Optional[Path] = None) -> Path:
    """Atomically write the consent file (tmp file in the same directory, then replace).

    Errors propagate. Returns the path written.
    """
    raise NotImplementedError("U2")
