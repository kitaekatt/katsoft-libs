"""Rendered graph pages: draw graph files as one self-contained HTML page.

:func:`project_catalog` and :func:`build_catalog` collect the graphs and every
graph file they run into a catalog (format ``llm-kit.graph-catalog/v1``; schema:
``llm_kit.schema.formats.load_schema(GRAPH_CATALOG_V1)``). :func:`render_page`
and :func:`write_page` turn a catalog into a page with a graph list, a laid-out
picture of one graph at a time, and a details panel. ``llm-kit graph render``
is the command-line front end.

A catalog can carry labels a model inferred (format ``llm-kit.graph-catalog/v2``):
:func:`label_catalog` adds them through a
:class:`~llm_kit.graph.render.label_types.Labeller`, and :func:`model_labeller`
builds the one that asks a model under the user's consent. ``llm-kit graph
render`` labels by default. The label names are re-exported lazily from
``labels``, ``label_model`` and ``label_types``, so importing this package loads
none of that code; each name is documented where it is defined.
"""

from __future__ import annotations

import importlib
from typing import Any

from llm_kit.graph.render.catalog import CATALOG_FORMAT, build_catalog, project_catalog
from llm_kit.graph.render.page import render_page, write_page

#: Lazily re-exported modules, each listing its public names in its own ``__all__``.
_LAZY_MODULES = ("labels", "label_model", "label_types")

_LAZY: dict[str, str] = {}


def _lazy_names() -> dict[str, str]:
    if not _LAZY:
        for module in _LAZY_MODULES:
            for name in importlib.import_module(f"{__name__}.{module}").__all__:
                if name in _LAZY:
                    raise ImportError(f"{__name__}: {name!r} is exported by both {_LAZY[name]} and {module}")
                _LAZY[name] = module
    return _LAZY


def __getattr__(name: str) -> Any:
    if name.startswith("_"):
        raise AttributeError(name)
    module = _lazy_names().get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_lazy_names()})


__all__ = [
    "CATALOG_FORMAT",
    "build_catalog",
    "project_catalog",
    "render_page",
    "write_page",
]
