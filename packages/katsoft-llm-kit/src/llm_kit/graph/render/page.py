"""The rendered page: one self-contained HTML file that draws a graph catalog.

The page embeds the catalog (:mod:`llm_kit.graph.render.catalog`) as JSON, the
browser script and style in ``static/``, and the ELK layout library in
``vendor/`` (elkjs, Eclipse Public License 2.0; its licence is
``vendor/elkjs-LICENSE.md``). It makes no network requests, so it opens from
disk in any current browser.

A catalog with one entry and one graph file opens on that graph with the graph
list hidden; any other catalog opens on its first entry with the list shown.

The page is the one reader of a catalog: the browser script receives only the
catalog this module embeds. :func:`render_page` therefore checks every catalog
against the schema of its version before it embeds it.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from llm_kit.graph.render._escape import escape_script_json
from llm_kit.graph.render.catalog import CATALOG_FORMAT
from llm_kit.graph.render.label_types import LABELLED_CATALOG_FORMAT
from llm_kit.schema.formats import load_schema
from llm_kit.schema.json_schema import validate

__all__ = ["render_page", "write_page"]

_HERE = Path(__file__).resolve().parent
_MARKER = re.compile(r"@@(TITLE|CSS|DATA|ELK|APP)@@")


def _asset(*parts: str) -> str:
    return _HERE.joinpath(*parts).read_text(encoding="utf-8")


def _embedded_json(catalog: Mapping[str, Any]) -> str:
    """The catalog as JSON that cannot end its script element early.

    ``<``, ``>`` and ``&`` become ``\\u`` escapes, so no text in a graph file or a
    label (``</script>``, ``<!--``) is read as markup. U+2028 and U+2029 are
    escaped too, for older script parsers.
    """
    return escape_script_json(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))


def _check_catalog(catalog: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``catalog`` is a catalog of a version this module draws."""
    version = catalog.get("format")
    if version not in (CATALOG_FORMAT, LABELLED_CATALOG_FORMAT):
        raise ValueError(f"not a {CATALOG_FORMAT} or {LABELLED_CATALOG_FORMAT} catalog")
    if not catalog.get("entries"):
        raise ValueError("the catalog has no entries")
    errors = validate(load_schema(version), catalog)
    if errors:
        shown = "; ".join(f"{pointer or '/'} ({keyword})" for pointer, keyword in errors[:5])
        more = f" and {len(errors) - 5} more" if len(errors) > 5 else ""
        raise ValueError(f"invalid {version} catalog: {shown}{more}")


def render_page(catalog: Mapping[str, Any], *, title: Optional[str] = None) -> str:
    """The HTML page that draws ``catalog``, as text.

    ``catalog`` is a ``llm-kit.graph-catalog/v1`` catalog or a ``/v2`` catalog, which
    carries inferred labels (``llm_kit.graph.render.label_types``). ``title``
    is the browser tab title; it defaults to ``"<project> graphs"``. Raises
    ``ValueError`` when ``catalog`` is not one of those versions, has no entries
    or breaks the schema of its version (the message names the first places).
    """
    _check_catalog(catalog)
    values = {
        "TITLE": html.escape(title or f"{catalog.get('project') or 'llm-kit'} graphs"),
        "CSS": _asset("static", "browser.css"),
        "DATA": _embedded_json(catalog),
        "ELK": _asset("vendor", "elk.bundled.js"),
        "APP": _asset("static", "browser.js"),
    }
    return _MARKER.sub(lambda match: values[match.group(1)], _asset("static", "page.html"))


def write_page(catalog: Mapping[str, Any], path: Path | str, *, title: Optional[str] = None) -> Path:
    """Write the page for ``catalog`` to ``path`` (UTF-8) and return the path.

    Parent directories are created. Raises what :func:`render_page` raises and
    ``OSError`` when the file cannot be written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_page(catalog, title=title), encoding="utf-8", newline="\n")
    return target
