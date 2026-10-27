"""Make a run record portable: no machine path in it.

A run record is committed in the consumer repository and shared, so it must
not name the machine it ran on. :class:`PathScrubber` rewrites every string of
a record before the record is stored: the project root becomes a
project-relative path (the root itself becomes ``.``) and the home directory
becomes ``~``. An op receipt (``cwd``, ``argv``, a stderr tail) and an error or
stop message are the strings that carry such paths; the scrub covers the whole
record, so a new field cannot leak by omission.

A path is replaced only at a path boundary: ``/home/u/proj2`` is not the root
``/home/u/proj``, and ``/x/home/u`` is not the home ``/home/u``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = ["PathScrubber"]


def _variants(path: Path) -> set[str]:
    texts = {str(path), str(path.resolve())}
    return {t.rstrip("/") for t in texts if len(t.rstrip("/")) > 1}


class PathScrubber:
    """Replace the project root with a relative path and the home directory with ``~``."""

    def __init__(self, project_root: Path | str, home: Path | str | None = None) -> None:
        root = Path(project_root)
        self._project = _variants(root)
        self._home = _variants(Path(home) if home is not None else Path.home()) - self._project
        names = sorted(self._project | self._home, key=len, reverse=True)
        self._pattern = re.compile(
            r"(?<![\w.~/-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w-]|\.\w)(/?)"
        ) if names else None

    def _replace(self, match: "re.Match[str]") -> str:
        if match.group(1) in self._project:
            return "" if match.group(2) else "."
        return "~" + match.group(2)

    def text(self, value: str) -> str:
        return value if self._pattern is None else self._pattern.sub(self._replace, value)

    def scrub(self, value: Any) -> Any:
        """A copy of the JSON ``value`` with every string made portable."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value
