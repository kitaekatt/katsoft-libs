"""The read-only run and graph inspection viewer (``llm-kit graph serve``).

Standard library only: ``http.server`` for the loopback server, plain HTML, CSS
and JavaScript with custom SVG for the page. Nothing here imports at package
import time; each module is loaded by the module that needs it.

Modules:

``structure``  a loaded graph as a JSON-able tree (nodes, kinds, edges, bodies).
``runs``       the one module that reads run records, blobs and media; the record
               format the viewer assumes is documented there.
``server``     the routes, headers and the loopback server.
``static/``    the page: ``index.html``, ``viewer.css`` and the ES modules.

The reference is ``docs/reference/llm-kit-graph-viewer.md``.
"""
