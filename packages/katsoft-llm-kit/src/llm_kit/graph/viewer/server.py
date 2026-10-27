"""The viewer's routes and its loopback HTTP server.

:class:`ViewerApp` turns a request (method, target, headers, body) into a
:class:`Response`; it opens no socket, so tests call it directly. :func:`serve`
binds ``127.0.0.1`` and runs it with ``http.server``.

Routes (GET and HEAD; every other method is refused):

====================================  ==========================================
``/``                                 the page
``/static/<file>``                    the page's fixed files (a fixed list)
``/theme.css``                        the project theme (``viewer.theme``), or empty
``/panels/<n>.mjs``                   the n-th panel module (``viewer.panels``)
``/project/<path>``                   a ``.mjs``, ``.js`` or ``.css`` file inside the project
``/api/session``                      project, graph names, panel and theme URLs
``/api/graph/<name>``                 a graph's structure
``/api/runs``                         every run on disk
``/api/run/<item>/<run_id>``          one run: its graph structure and a summary per node path
``/api/run/<item>/<run_id>/node``     one node's record (``?path=``)
``/api/ref/<item>/<ref>``             the whole text of a ``value:`` or ``blob:`` reference
``/media/<item>/<file>``              a media file of the item (``<16 hex><ext>``)
====================================  ==========================================

Protection: the server binds the loopback address only; a request whose
``Host`` is not the loopback name and port is refused (DNS rebinding); every
response carries a strict content security policy (own origin only, no inline
script or style) and ``nosniff``; stored text is served as plain text and media
only under an image, audio or video type chosen by file extension.

A write route is a hook for the answer forms (unit L05): :meth:`ViewerApp.add_post`
registers one. A POST is accepted only for a registered path, with the
per-process token in ``X-Viewer-Token`` and an ``Origin`` equal to the viewer's
own. Without a registered route every POST answers 405.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from llm_kit.graph.config import GraphConfig
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import GraphError
from llm_kit.graph.viewer.runs import NotFound, RunReader
from llm_kit.graph.viewer.structure import graph_structure

__all__ = ["HOST", "STATIC_FILES", "Response", "ViewerApp", "make_server", "serve"]

HOST = "127.0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: Project files a panel or theme may load (``/project/<path>``), by extension.
PROJECT_TYPES = {".mjs": "text/javascript; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}

_JS = "text/javascript; charset=utf-8"
#: The page's files: name -> content type. Nothing else is served from ``static/``.
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "viewer.css": "text/css; charset=utf-8",
    "app.js": _JS,
    "dom.js": _JS,
    "popover.js": _JS,
    "graphview.js": _JS,
    "inspector.js": _JS,
    "runlist.js": _JS,
}

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

MAX_POST_BYTES = 1_000_000


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> Optional[str]:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


def _json_text(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    # defence in depth: no raw markup characters even inside JSON strings
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode("ascii")


def _json(status: int, payload: Any) -> Response:
    return Response(status, [("Content-Type", "application/json; charset=utf-8")], _json_text(payload))


def _error(status: int, message: str) -> Response:
    return _json(status, {"error": message})


def _text(content_type: str, body: bytes, status: int = 200) -> Response:
    return Response(status, [("Content-Type", content_type)], body)


class ViewerApp:
    """The viewer for one project. ``port`` is the port the server listens on."""

    def __init__(self, config: GraphConfig, *, port: int) -> None:
        self.config = config
        self.port = port
        self.store = RunReader(config)
        self.token = secrets.token_urlsafe(32)
        self.post_routes: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
        self.panels = tuple(config.viewer.get("panels", ()))
        self.theme: Optional[Path] = config.viewer.get("theme")
        self.root = config.root.resolve()
        for path in (*self.panels, *([self.theme] if self.theme else [])):
            if not Path(path).is_file():
                raise GraphError(f"viewer file {path} does not exist (set in {config.path}: viewer)")

    # -- write hook (unit L05) ---------------------------------------------

    def add_post(self, path: str, handler: Callable[[Mapping[str, Any]], Any]) -> None:
        """Register a JSON POST route. ``handler(body)`` returns a JSON value."""
        if not path.startswith("/api/") or path in self.post_routes:
            raise GraphError(f"POST route {path!r} must be new and start with /api/")
        self.post_routes[path] = handler

    # -- entry point -------------------------------------------------------

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"})

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.allowed_hosts)

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes = b"") -> Response:
        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            if lowered.get("host", "") not in self.allowed_hosts:
                response = _error(403, "the viewer answers only to its own loopback address")
            elif method in ("GET", "HEAD"):
                response = self._get(target)
            elif method == "POST":
                response = self._post(target, lowered, body)
            else:
                response = _error(405, "the viewer is read-only")
                response.headers.append(("Allow", "GET, HEAD"))
        except NotFound as exc:
            response = _error(404, str(exc))
        except GraphError as exc:
            response = _error(422, str(exc))
        response.headers.extend([
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
            ("Referrer-Policy", "no-referrer"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            ("X-Frame-Options", "DENY"),
        ])
        if method == "HEAD":
            response.headers.append(("Content-Length", str(len(response.body))))
            response.body = b""
        return response

    # -- GET ---------------------------------------------------------------

    def _get(self, target: str) -> Response:
        split = urlsplit(target)
        segments = [unquote(s) for s in split.path.split("/")[1:]]
        query = parse_qs(split.query, keep_blank_values=True)
        head = segments[0] if segments else ""
        if split.path == "/":
            return self._static("index.html")
        if head == "static" and len(segments) == 2:
            return self._static(segments[1])
        if split.path == "/theme.css":
            body = self.theme.read_bytes() if self.theme else b"/* no project theme */\n"
            return _text("text/css; charset=utf-8", body)
        if head == "panels" and len(segments) == 2:
            return self._panel(segments[1])
        if head == "project" and len(segments) >= 2:
            return self._project_file(segments[1:])
        if head == "api":
            return self._api(segments[1:], query)
        if head == "media" and len(segments) == 3:
            return self._media(*segments[1:])
        raise NotFound(f"no route {split.path}")

    def _static(self, name: str) -> Response:
        if name not in STATIC_FILES:
            raise NotFound(f"no static file {name!r}")
        return _text(STATIC_FILES[name], (STATIC_DIR / name).read_bytes())

    def _panel(self, name: str) -> Response:
        match = re.fullmatch(r"(\d{1,3})\.mjs", name)
        if not match or int(match.group(1)) >= len(self.panels):
            raise NotFound(f"no panel {name!r}")
        return _text(_JS, Path(self.panels[int(match.group(1))]).read_bytes())

    def _project_file(self, segments: list[str]) -> Response:
        """A script or stylesheet inside the project root, so a panel can import its neighbours."""
        for part in segments:
            if not part or part.startswith(".") or "/" in part or "\\" in part or "\0" in part:
                raise NotFound("no such project file")
        content_type = PROJECT_TYPES.get(Path(segments[-1]).suffix)
        if content_type is None:
            raise NotFound("no such project file")
        target = (self.root / Path(*segments)).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            raise NotFound("no such project file")
        return _text(content_type, target.read_bytes())

    def _asset_url(self, path: Path, fallback: str) -> str:
        """``/project/<path>`` for a file inside the project (so relative imports and urls work)."""
        try:
            rel = Path(path).resolve().relative_to(self.root)
        except ValueError:
            return fallback
        if any(part.startswith(".") for part in rel.parts) or rel.suffix not in PROJECT_TYPES:
            return fallback
        return "/project/" + "/".join(quote(part) for part in rel.parts)

    def _api(self, segments: list[str], query: Mapping[str, list[str]]) -> Response:
        if segments == ["session"]:
            return _json(200, self._session())
        if len(segments) == 2 and segments[0] == "graph":
            return _json(200, self._graph(segments[1]))
        if segments == ["runs"]:
            return _json(200, self._runs())
        if len(segments) == 3 and segments[0] == "run":
            return _json(200, self._run(segments[1], segments[2]))
        if len(segments) == 4 and segments[0] == "run" and segments[3] == "node":
            paths = query.get("path", [])
            if len(paths) != 1 or not paths[0]:
                return _error(400, "node needs exactly one ?path=")
            return _json(200, self.store.node_detail(segments[1], segments[2], paths[0]))
        if len(segments) == 3 and segments[0] == "ref":
            return _text("text/plain; charset=utf-8", self.store.stored_text(segments[1], segments[2]).encode("utf-8"))
        raise NotFound("no API route /api/" + "/".join(segments))

    def _session(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "project": str(self.config.root),
            "config": str(self.config.path),
            "graphs": sorted(self.config.graphs),
            "panels": [self._asset_url(p, f"/panels/{i}.mjs") for i, p in enumerate(self.panels)],
            "theme": self._asset_url(self.theme, "/theme.css") if self.theme else None,
            "runs_configured": self.store.configured,
            "writable": bool(self.post_routes),
        }
        if self.post_routes:
            out["token"] = self.token
            out["post_routes"] = sorted(self.post_routes)
        return out

    def _graph(self, name: str) -> dict[str, Any]:
        if name not in self.config.graphs:
            raise NotFound(f"no graph named {name!r}; graphs: {sorted(self.config.graphs)}")
        graph = load_graph(self.config.graphs[name])
        return {"name": name, "structure": graph_structure(graph, root=self.config.root)}

    def _runs(self) -> dict[str, Any]:
        if not self.store.configured:
            return {"configured": False, "runs": [], "error": None,
                    "note": f"{self.config.path} has no runs.dir; the viewer shows graphs only"}
        try:
            return {"configured": True, "runs": self.store.list_runs(), "error": None}
        except GraphError as exc:
            return {"configured": True, "runs": [], "error": str(exc)}

    def _run(self, item: str, run_id: str) -> dict[str, Any]:
        return self.store.run_view(item, run_id)

    def _media(self, item: str, name: str) -> Response:
        path, mime = self.store.media_file(item, name)
        if mime:
            return _text(mime, path.read_bytes())
        return Response(200, [("Content-Type", "application/octet-stream"),
                              ("Content-Disposition", f'attachment; filename="{name}"')], path.read_bytes())

    # -- POST (hook) -------------------------------------------------------

    def _post(self, target: str, headers: Mapping[str, str], body: bytes) -> Response:
        path = urlsplit(target).path
        handler = self.post_routes.get(path)
        if handler is None:
            response = _error(405, "the viewer is read-only")
            response.headers.append(("Allow", "GET, HEAD"))
            return response
        if headers.get("origin") not in self.allowed_origins:
            return _error(403, "POST needs the viewer's own Origin")
        if not secrets.compare_digest(headers.get("x-viewer-token", ""), self.token):
            return _error(403, "POST needs the viewer token")
        if not headers.get("content-type", "").startswith("application/json"):
            return _error(415, "POST takes application/json")
        if len(body) > MAX_POST_BYTES:
            return _error(413, "POST body is too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _error(400, "POST body is not JSON")
        if not isinstance(payload, Mapping):
            return _error(400, "POST body must be a JSON object")
        return _json(200, handler(payload))


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "llm-kit-viewer"
    protocol_version = "HTTP/1.1"

    def _dispatch(self) -> None:
        app: ViewerApp = self.server.app  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, MAX_POST_BYTES + 1)) if length else b""
        if length > MAX_POST_BYTES:
            self.close_connection = True  # the unread rest of the body must not become the next request
        response = app.handle(self.command, self.path, dict(self.headers.items()), body)
        self.send_response(response.status)
        has_length = False
        for key, value in response.headers:
            has_length = has_length or key.lower() == "content-length"
            self.send_header(key, value)
        if not has_length:
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write("viewer: %s - %s\n" % (self.address_string(), format % args))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: ViewerApp


def make_server(config: GraphConfig, port: int = 0) -> _Server:
    """A server bound to the loopback address; ``port`` 0 picks a free port."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise GraphError(f"port {port!r} must be 0-65535")
    server = _Server((HOST, port), _Handler)
    try:
        server.app = ViewerApp(config, port=server.server_address[1])
    except Exception:
        server.server_close()
        raise
    return server


def serve(config: GraphConfig, port: int = 0, *, announce: Callable[[str], None] = print) -> None:
    """Serve until interrupted."""
    server = make_server(config, port)
    announce(f"llm-kit graph viewer: http://{HOST}:{server.server_address[1]}/  (project {config.root}; Ctrl-C stops)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
