"""The project graph config: ``.llm-kit/graphs.yaml`` and the personal layer."""

from __future__ import annotations

from pathlib import Path

import pytest

import graph_support as gs
from llm_kit.graph.config import CONFIG_FILE, LOCAL_CONFIG_FILE, expand_run_path, load_config
from llm_kit.graph.model import GraphError

FULL = """\
version: 1
namespaces:
  afterplay.beta: pipeline/moments/beta/graph/ops
graphs:
  afterplay.beta: pipeline/moments/beta/graph/beta.graph.yaml
runs:
  dir: pipeline/moments/beta/work/{item}/graph
  media: pipeline/moments/beta/work/{item}/media
  blobs: "{git_common_dir}/beta-graph/blobs"
  state: "{git_common_dir}/beta-graph/state"
services:
  concurrency: {model-text: 4, heavy: 1}
  retries: none
viewer:
  panels: [pipeline/moments/beta/graph/viewer/panels.mjs]
  theme: pipeline/moments/beta/graph/viewer/theme.css
"""


def _root(tmp_path: Path, tracked: str = FULL, local: str | None = None) -> Path:
    root = tmp_path / "proj"
    gs.write(root / CONFIG_FILE, tracked)
    if local is not None:
        gs.write(root / LOCAL_CONFIG_FILE, local)
    return root


def test_the_full_config_loads_with_absolute_paths(tmp_path):
    root = _root(tmp_path)
    config = load_config(root)
    root = root.resolve()
    assert config.root == root and config.path == root / CONFIG_FILE and config.local_path is None
    assert config.namespaces == {"afterplay.beta": root / "pipeline/moments/beta/graph/ops"}
    assert config.graph_path("afterplay.beta") == root / "pipeline/moments/beta/graph/beta.graph.yaml"
    assert config.services["concurrency"] == {"model-text": 4, "heavy": 1}
    assert config.viewer["panels"] == (root / "pipeline/moments/beta/graph/viewer/panels.mjs",)
    assert config.viewer["theme"] == root / "pipeline/moments/beta/graph/viewer/theme.css"
    assert config.namespace_of("afterplay.beta.plot_errors") == "afterplay.beta"
    assert config.namespace_of("llm.model") is None
    with pytest.raises(GraphError, match="no graph named 'x'"):
        config.graph_path("x")


def test_run_paths_expand_their_placeholders(tmp_path):
    config = load_config(_root(tmp_path))
    assert config.run_path("dir", item="night-petition") == \
        config.root / "pipeline/moments/beta/work/night-petition/graph"
    assert config.run_path("blobs", git_common_dir="/repo/.git") == Path("/repo/.git/beta-graph/blobs")
    with pytest.raises(GraphError, match=r"needs values for \['item'\]"):
        config.run_path("media")
    with pytest.raises(GraphError, match="non-empty string"):
        config.run_path("dir", item="")
    with pytest.raises(GraphError, match="unknown placeholders"):
        expand_run_path(config.root, "{run}/x", {"run": "r"})


def test_the_personal_layer_replaces_keys_one_section_deep(tmp_path):
    local = """\
version: 1
runs:
  blobs: /Users/me/blobs
services:
  concurrency: {heavy: 2}
"""
    config = load_config(_root(tmp_path, local=local))
    assert config.local_path == (tmp_path / "proj" / LOCAL_CONFIG_FILE).resolve()
    assert config.runs["blobs"] == "/Users/me/blobs"
    assert config.runs["dir"] == "pipeline/moments/beta/work/{item}/graph"
    assert config.services == {"concurrency": {"heavy": 2}, "retries": "none"}
    assert config.run_path("blobs") == Path("/Users/me/blobs")


def test_a_minimal_config(tmp_path):
    config = load_config(_root(tmp_path, "version: 1\n"))
    assert config.namespaces == {} and config.graphs == {} and config.runs == {}


@pytest.mark.parametrize("tracked, message", [
    ("version: 2\n", "version must be 1"),
    ("namespaces: {}\n", "version must be 1"),
    (FULL + "extra: 1\n", r"unknown keys \['extra'\]"),
    ("version: 1\nnamespaces: {llm: x}\n", "belongs to llm-kit"),
    ("version: 1\nnamespaces: {llm.extra: x}\n", "belongs to llm-kit"),
    ("version: 1\nnamespaces: {Bad: x}\n", "dotted lower-case name"),
    ("version: 1\nnamespaces: {a: x, a.b: y}\n", "is a prefix of 'a.b'"),
    ("version: 1\nnamespaces: {a: /abs}\n", "is absolute"),
    ("version: 1\nnamespaces: []\n", "namespaces must be a mapping"),
    ("version: 1\ngraphs: {g: ''}\n", "non-empty path"),
    ("version: 1\ngraphs: {'bad name': x}\n", "must be a dotted name"),
    ("version: 1\nruns: {logs: x}\n", r"unknown runs keys \['logs'\]"),
    ("version: 1\nruns: {dir: '{run}/x'}\n", "unknown placeholders"),
    ("version: 1\nruns: {dir: '{item'}\n", "bad path template"),
    ("version: 1\nviewer: {panels: x.mjs}\n", "panels must be a list"),
    ("version: 1\nviewer: {css: x.css}\n", "unknown viewer keys"),
    ("version: 1\nversion: 1\n", "duplicate mapping key"),
])
def test_tracked_refusals(tmp_path, tracked, message):
    with pytest.raises(GraphError, match=message):
        load_config(_root(tmp_path, tracked))


def test_local_refusals(tmp_path):
    with pytest.raises(GraphError, match="version must be 1"):
        load_config(_root(tmp_path, local="runs: {dir: x}\n"))
    with pytest.raises(GraphError, match="belongs to llm-kit"):
        load_config(_root(tmp_path / "b", local="version: 1\nnamespaces: {llm: x}\n"))


def test_a_missing_config_or_root_is_refused(tmp_path):
    with pytest.raises(GraphError, match="needs .llm-kit/graphs.yaml"):
        load_config(tmp_path)
    with pytest.raises(GraphError, match="is not a directory"):
        load_config(tmp_path / "none")
