"""Golden fixtures for ``llm-kit models project`` (show, validate, write and their refusals).

Same harness and normalization as ``test_cli_goldens.py`` (its ``env`` fixture is
reused); the fixtures live in ``tests/llm-kit/goldens/cli/``. Update after a
deliberate output change with

    UPDATE_GOLDENS=1 uv run --extra dev pytest tests/llm-kit/test_cli_goldens_project_models.py -v
"""

from __future__ import annotations

import json

import pytest

from test_cli_goldens import env  # noqa: F401 -- fixture
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

_DOC = {
    "version": 1,
    "catalog": [
        {"id": "b", "label": "Beta", "endpoint": "cdx", "model": "gpt-6-luna", "efforts": ["low", "high"]},
        {"id": "a", "label": "Alpha", "endpoint": "local", "model": "m-local",
         "request_defaults": {"extras": {"provider": {"sort": "latency"}}}},
    ],
    "selections": {"second": {"model": "a"}, "first": {"model": "b", "effort": "high"}},
}


@pytest.fixture()
def root(tmp_path):
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _argv(verb, root, *extra):
    return ["models", "project", verb, "--project-root", str(root), *extra]


def test_project_write_ok(env, root):
    got = env("project_write_ok", _argv("write", root, "--input", "-"), stdin=json.dumps(_DOC))
    assert got["exit"] == 0 and got["stderr"] == ""


def test_project_show_ok(env, root):
    env("project_write_ok", _argv("write", root, "--input", "-"), stdin=json.dumps(_DOC))
    got = env("project_show_ok", _argv("show", root))
    assert got["exit"] == 0 and got["stderr"] == ""


def test_project_validate_ok(env, root):
    got = env("project_validate_ok", _argv("validate", root, "--input", "-"), stdin=json.dumps(_DOC))
    assert got["exit"] == 0 and not (root / ".llm-kit").exists()


def test_project_write_file_option(env, root):
    got = env("project_write_file", _argv("write", root, "--file", "cfg/m.yaml", "--input", "-"),
              stdin=json.dumps(_DOC))
    assert got["exit"] == 0 and (root / "cfg" / "m.yaml").is_file()


def test_project_write_stale(env, root):
    env("project_write_ok", _argv("write", root, "--input", "-"), stdin=json.dumps(_DOC))
    got = env("project_write_stale", _argv("write", root, "--input", "-", "--if-match", "0" * 64),
              stdin=json.dumps(_DOC))
    assert got["exit"] == 2


def test_project_write_invalid_effort(env, root):
    bad = json.loads(json.dumps(_DOC))
    bad["selections"]["first"]["effort"] = "none"
    got = env("project_write_invalid_effort", _argv("write", root, "--input", "-"), stdin=json.dumps(bad))
    assert got["exit"] == 2 and not (root / ".llm-kit").exists()


def test_project_write_unknown_model(env, root):
    bad = json.loads(json.dumps(_DOC))
    bad["selections"]["first"]["model"] = "ghost"
    got = env("project_write_unknown_model", _argv("write", root, "--input", "-"), stdin=json.dumps(bad))
    assert got["exit"] == 2 and not (root / ".llm-kit").exists()


def test_project_write_escape(env, root):
    got = env("project_write_escape", _argv("write", root, "--file", "../elsewhere.yaml", "--input", "-"),
              stdin=json.dumps(_DOC))
    assert got["exit"] == 2 and not (root.parent / "elsewhere.yaml").exists()
