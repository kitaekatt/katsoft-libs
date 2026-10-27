"""The project graph config: ``.llm-kit/graphs.yaml`` and its personal layer.

::

    version: 1
    namespaces:                  # op namespace -> Python package directory (imported by path)
      afterplay.beta: pipeline/moments/beta/graph/ops
    graphs:                      # graph name -> graph file
      afterplay.beta: pipeline/moments/beta/graph/beta.graph.yaml
    runs:                        # path templates; placeholders {item} and {git_common_dir}
      dir: pipeline/moments/beta/work/{item}/graph
      media: pipeline/moments/beta/work/{item}/media
      blobs: "{git_common_dir}/beta-graph/blobs"
      state: "{git_common_dir}/beta-graph/state"
    services:                    # runtime services (llm_kit.graph.services; checked by check_services)
      concurrency: {model-text: 4, heavy: 1}
      retries: none
      spend: {ledger_env: CONTENT_PIPELINE_SPEND_LEDGER, guards: [], models: {}}
      pause_on_halt: true
    viewer:
      panels: [pipeline/moments/beta/graph/viewer/panels.mjs]
      theme: pipeline/moments/beta/graph/viewer/theme.css

Paths are relative to the project root. The tracked file may not hold an
absolute path (it is shared between machines); the personal layer may.

The personal layer ``.llm-kit/graphs.local.yaml`` (untracked, optional) holds
``version: 1`` and any of the same sections. Each key of a section replaces the
tracked key of that section (``runs: {blobs: ...}`` replaces only
``runs.blobs``); a value is replaced whole, never merged deeper.

The op namespace ``llm`` belongs to llm-kit and may not be configured. Two
namespaces may not be prefixes of one another.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

from .loader import load_yaml
from .model import BUILTIN_OP_NAMESPACE, GraphError
from .services import check_services

__all__ = [
    "CONFIG_FILE",
    "LOCAL_CONFIG_FILE",
    "RUN_KEYS",
    "RUN_PLACEHOLDERS",
    "GraphConfig",
    "expand_run_path",
    "load_config",
]

CONFIG_FILE = ".llm-kit/graphs.yaml"
LOCAL_CONFIG_FILE = ".llm-kit/graphs.local.yaml"
CONFIG_VERSION = 1

_SECTIONS = ("namespaces", "graphs", "runs", "services", "viewer")
RUN_KEYS = ("dir", "media", "blobs", "state")
RUN_PLACEHOLDERS = frozenset({"item", "git_common_dir"})
_VIEWER_KEYS = ("panels", "theme")
NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$")
GRAPH_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)*$")


@dataclass(frozen=True)
class GraphConfig:
    """The merged project config. Paths are absolute; run paths stay templates."""

    root: Path
    path: Path
    local_path: Optional[Path]
    namespaces: Mapping[str, Path] = field(default_factory=dict)
    graphs: Mapping[str, Path] = field(default_factory=dict)
    runs: Mapping[str, str] = field(default_factory=dict)
    services: Mapping[str, Any] = field(default_factory=dict)
    viewer: Mapping[str, Any] = field(default_factory=dict)

    def graph_path(self, name: str) -> Path:
        try:
            return self.graphs[name]
        except KeyError:
            raise GraphError(f"no graph named {name!r} in {self.path}; graphs: {sorted(self.graphs)}") from None

    def namespace_of(self, op_name: str) -> Optional[str]:
        """The configured namespace an op name belongs to, or None."""
        for namespace in self.namespaces:
            if op_name.startswith(namespace + "."):
                return namespace
        return None

    def run_path(self, key: str, **values: str) -> Path:
        """``runs.<key>`` with its placeholders filled, absolute."""
        if key not in self.runs:
            raise GraphError(f"{self.path}: runs.{key} is not configured")
        return expand_run_path(self.root, self.runs[key], values, where=f"{self.path}: runs.{key}")


def _placeholders(template: str, where: str) -> set[str]:
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as exc:
        raise GraphError(f"bad path template {template!r}: {exc}", where) from None
    unknown = sorted(names - RUN_PLACEHOLDERS)
    if unknown:
        raise GraphError(f"unknown placeholders {unknown} in {template!r}; allowed: {sorted(RUN_PLACEHOLDERS)}",
                         where)
    return names


def expand_run_path(root: Path, template: str, values: Mapping[str, str], *, where: str = "") -> Path:
    """Fill a run path template; every placeholder it uses must have a value."""
    names = _placeholders(template, where)
    missing = sorted(names - set(values))
    if missing:
        raise GraphError(f"{template!r} needs values for {missing}", where)
    for name in names:
        value = values[name]
        if not isinstance(value, str) or not value:
            raise GraphError(f"placeholder {name} needs a non-empty string", where)
    path = Path(template.format(**{n: values[n] for n in names}))
    return path if path.is_absolute() else Path(root) / path


def _path_text(value: Any, where: str, *, tracked: bool) -> str:
    if not isinstance(value, str) or not value:
        raise GraphError("must be a non-empty path", where)
    if tracked and Path(value).is_absolute():
        raise GraphError(f"{value!r} is absolute; the tracked config holds project-relative paths "
                         "(use the personal layer for a machine path)", where)
    return value


def _check_section(name: str, value: Any, where: str, *, tracked: bool) -> dict:
    if not isinstance(value, Mapping):
        raise GraphError(f"{name} must be a mapping", where)
    out = dict(value)
    if name == "namespaces":
        for ns, path in out.items():
            if not isinstance(ns, str) or not NAMESPACE_RE.match(ns):
                raise GraphError(f"namespace {ns!r} must be a dotted lower-case name", f"{where}.{ns}")
            if ns == BUILTIN_OP_NAMESPACE or ns.startswith(BUILTIN_OP_NAMESPACE + "."):
                raise GraphError(f"namespace {ns!r} belongs to llm-kit", f"{where}.{ns}")
            _path_text(path, f"{where}.{ns}", tracked=tracked)
    elif name == "graphs":
        for graph, path in out.items():
            if not isinstance(graph, str) or not GRAPH_NAME_RE.match(graph):
                raise GraphError(f"graph name {graph!r} must be a dotted name", f"{where}.{graph}")
            _path_text(path, f"{where}.{graph}", tracked=tracked)
    elif name == "runs":
        unknown = sorted(set(out) - set(RUN_KEYS))
        if unknown:
            raise GraphError(f"unknown runs keys {unknown}; allowed: {list(RUN_KEYS)}", where)
        for key, template in out.items():
            _placeholders(_path_text(template, f"{where}.{key}", tracked=tracked), f"{where}.{key}")
    elif name == "services":
        out = check_services(out, where)
    elif name == "viewer":
        unknown = sorted(set(out) - set(_VIEWER_KEYS))
        if unknown:
            raise GraphError(f"unknown viewer keys {unknown}; allowed: {list(_VIEWER_KEYS)}", where)
        if "panels" in out:
            if not isinstance(out["panels"], list):
                raise GraphError("panels must be a list of paths", f"{where}.panels")
            for index, panel in enumerate(out["panels"]):
                _path_text(panel, f"{where}.panels[{index}]", tracked=tracked)
        if "theme" in out:
            _path_text(out["theme"], f"{where}.theme", tracked=tracked)
    return out


def _read(path: Path, *, tracked: bool) -> dict[str, dict]:
    doc = load_yaml(path)
    where = str(path)
    if not isinstance(doc, Mapping):
        raise GraphError("the config must be a mapping", where)
    unknown = sorted(set(doc) - {"version", *_SECTIONS})
    if unknown:
        raise GraphError(f"unknown keys {unknown}; allowed: {['version', *_SECTIONS]}", where)
    if doc.get("version") != CONFIG_VERSION:
        raise GraphError(f"version must be {CONFIG_VERSION}", f"{where}: version")
    return {name: _check_section(name, doc[name], f"{where}: {name}", tracked=tracked)
            for name in _SECTIONS if name in doc}


def load_config(project_root: Path | str) -> GraphConfig:
    """Read ``.llm-kit/graphs.yaml`` under ``project_root`` and its personal layer."""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise GraphError(f"project root {root} is not a directory")
    path = root / CONFIG_FILE
    if not path.is_file():
        raise GraphError(f"{path} does not exist; a graph project needs {CONFIG_FILE}")
    sections = _read(path, tracked=True)
    local = root / LOCAL_CONFIG_FILE
    local_path = local if local.is_file() else None
    if local_path is not None:
        for name, values in _read(local_path, tracked=False).items():
            sections[name] = {**sections.get(name, {}), **values}
    namespaces = sections.get("namespaces", {})
    names = sorted(namespaces)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if b.startswith(a + "."):
                raise GraphError(f"namespace {a!r} is a prefix of {b!r}; an op name must belong to one namespace",
                                 f"{path}: namespaces")
    viewer = dict(sections.get("viewer", {}))
    if "panels" in viewer:
        viewer["panels"] = tuple(root / p for p in viewer["panels"])
    if "theme" in viewer:
        viewer["theme"] = root / viewer["theme"]
    return GraphConfig(
        root=root,
        path=path,
        local_path=local_path,
        namespaces=MappingProxyType({ns: root / p for ns, p in namespaces.items()}),
        graphs=MappingProxyType({g: root / p for g, p in sections.get("graphs", {}).items()}),
        runs=MappingProxyType(dict(sections.get("runs", {}))),
        services=MappingProxyType(dict(sections.get("services", {}))),
        viewer=MappingProxyType(viewer),
    )
