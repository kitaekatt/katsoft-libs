"""build_codex_app_server_argv: goldens and refusals."""
from __future__ import annotations

import pytest

from llm_kit.adapters import codex_command
from llm_kit.adapters.codex_command import build_codex_app_server_argv

ROOT = "/abs/root"
PFX = ["codex"]


@pytest.fixture(autouse=True)
def _posix(monkeypatch):
    monkeypatch.setattr(codex_command.os, "name", "posix")


def build(**kw):
    return build_codex_app_server_argv(root=ROOT, argv_prefix=PFX, **kw)


def test_default_golden():
    assert build() == [
        "codex", "app-server", "--listen", "stdio://",
        "-c", 'approval_policy="never"',
        "-c", 'sandbox_mode="workspace-write"',
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "features.hooks=false",
        "-c", "notify=[]",
    ]


def test_full_golden_read_only_network_model_effort():
    assert build(sandbox="read-only", network=True, model="m1", effort="low") == [
        "codex", "app-server", "--listen", "stdio://",
        "-c", 'approval_policy="never"',
        "-c", 'sandbox_mode="read-only"',
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", "features.hooks=false",
        "-c", "notify=[]",
        "-c", 'model="m1"',
        "-c", 'model_reasoning_effort="low"',
    ]


def test_network_false_explicit():
    assert "sandbox_workspace_write.network_access=false" in build(network=False)


def test_hooks_false_emitted_and_omitted_when_true():
    assert "features.hooks=false" in build()
    assert "features.hooks=false" not in build(hooks=True)


def test_notify_always_present():
    assert "notify=[]" in build()
    assert "notify=[]" in build(hooks=True, network=True)


def test_windows_sandbox_on_nt(monkeypatch):
    monkeypatch.setattr(codex_command.os, "name", "nt")
    assert 'windows.sandbox="unelevated"' in build()


def test_argv_is_list_of_str():
    argv = build()
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)


@pytest.mark.parametrize("kw,param", [
    ({"sandbox": "danger-full-access"}, "sandbox"),
    ({"approval_policy": "on-request"}, "approval_policy"),
    ({"model": 'a"b'}, "model"),
    ({"model": "a\nb"}, "model"),
    ({"effort": 'x"'}, "effort"),
    ({"effort": "x\ny"}, "effort"),
])
def test_refusals(kw, param):
    with pytest.raises(ValueError, match=param):
        build(**kw)


def test_relative_root_refused():
    with pytest.raises(ValueError, match="root"):
        build_codex_app_server_argv(root="rel/dir", argv_prefix=PFX)
