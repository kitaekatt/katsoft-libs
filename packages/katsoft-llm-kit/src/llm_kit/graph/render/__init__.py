"""Rendered graph pages: draw graph files as one self-contained HTML page.

:func:`project_catalog` and :func:`build_catalog` collect the graphs and every
graph file they run into a catalog (format ``llm-kit.graph-catalog/v1``; schema:
``llm_kit.schema.formats.load_schema(GRAPH_CATALOG_V1)``). :func:`render_page`
and :func:`write_page` turn a catalog into a page with a graph list, a laid-out
picture of one graph at a time, and a details panel. ``llm-kit graph render``
is the command-line front end.
"""

from llm_kit.graph.render.catalog import CATALOG_FORMAT, build_catalog, project_catalog
from llm_kit.graph.render.page import render_page, write_page

__all__ = [
    "CATALOG_FORMAT",
    "build_catalog",
    "project_catalog",
    "render_page",
    "write_page",
]
