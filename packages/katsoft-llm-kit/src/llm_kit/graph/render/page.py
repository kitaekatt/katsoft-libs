"""The rendered page: one self-contained HTML file that draws a graph catalog.

The page embeds the catalog (:mod:`llm_kit.graph.render.catalog`) as JSON, the
browser script and style in ``static/``, and the ELK layout library in
``vendor/`` (elkjs, Eclipse Public License 2.0; its licence is
``vendor/elkjs-LICENSE.md``). It makes no network requests, so it opens from
disk in any current browser.

A catalog with one entry and one graph file opens on that graph with the graph
list hidden; any other catalog opens on its first entry with the list shown.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from llm_kit.graph.render.catalog import CATALOG_FORMAT

__all__ = ["render_page", "write_page"]

_HERE = Path(__file__).resolve().parent
_MARKER = re.compile(r"@@(TITLE|CSS|DATA|ELK|APP)@@")


def _asset(*parts: str) -> str:
    return _HERE.joinpath(*parts).read_text(encoding="utf-8")


def _embedded_json(catalog: Mapping[str, Any]) -> str:
    """The catalog as JSON that cannot end its script element early.

    ``<``, ``>`` and ``&`` become ``\\u`` escapes, so no text in a graph file
    (``</script>``, ``<!--``) is read as markup. U+2028 and U+2029 are escaped
    too, for older script parsers.
    """
    text = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    for char, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                         ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(char, escape)
    return text


def render_page(catalog: Mapping[str, Any], *, title: Optional[str] = None) -> str:
    """The HTML page that draws ``catalog``, as text.

    ``title`` is the browser tab title; it defaults to ``"<project> graphs"``.
    Raises ``ValueError`` when ``catalog`` is not a version this module draws
    or has no entries.
    """
    if catalog.get("format") != CATALOG_FORMAT:
        raise ValueError(f"not a {CATALOG_FORMAT} catalog")
    if not catalog.get("entries"):
        raise ValueError("the catalog has no entries")
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
