"""llm_kit.graph: data-defined generation graphs (opt-in; the ``graph`` extra).

A graph file declares typed input and output ports (JSON Schema) and nodes:
operations, subgraphs, per-item maps, bounded repeats, human-input waits and a
commit step. ``llm-kit graph validate`` checks graph files statically. The file
format, the project config and the static checks are in
``docs/reference/llm-kit-graph.md``.

Nothing in llm_kit imports this package except the ``llm-kit graph`` CLI group,
and importing it imports nothing: every public name is re-exported lazily from
the module that defines it. A module's public names are its ``__all__``; the
modules searched are :data:`MODULES`, and a planned module that is not in this
build yet is skipped. Two modules that export one name are an error.
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

#: The modules whose ``__all__`` names this package re-exports, in search order.
MODULES = (
    "model", "loader", "config", "hashing", "validate", "expr",
    "run", "records", "freshness", "media",
    "prompt", "testing",
    "services", "spend", "human", "commit",
    "replay",
)

_index: dict[str, str] | None = None


def _exports() -> dict[str, str]:
    global _index
    if _index is None:
        index: dict[str, str] = {}
        for module in MODULES:
            dotted = f"{__name__}.{module}"
            if importlib.util.find_spec(dotted) is None:
                continue
            for name in importlib.import_module(dotted).__all__:
                if name in index:
                    raise ImportError(f"{__name__}: {name!r} is exported by both {index[name]} and {module}")
                index[name] = module
        _index = index
    return _index


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    module = _exports().get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_exports()})
