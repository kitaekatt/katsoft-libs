"""A provider's stderr reaches the llm.model failure, through a real codex backend and a fake CLI (no network)."""

from __future__ import annotations

import dataclasses
import os
import stat
import sys

import pytest

from llm_kit.adapters import codex_command
from llm_kit.adapters.codex_backend import CodexCliBackend, CodexRunError
from llm_kit.dispatch.invoke import STDERR_TAIL_CHARS
from llm_kit.graph.ops import OpFailed
from llm_kit.graph.ops.model import llm_model
from llm_kit.graph.testing import MockBackendFactory, make_project, op_context

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a POSIX shell script")

SELECTION = "draft.write"
STDERR = ("ERROR: unexpected status 400 Bad Request: invalid_json_schema: schema must be a JSON Schema of "
          "'type: \"object\"', got 'type: \"None\"'")


def fake_codex(directory, stderr_text: str) -> str:
    path = directory / "codex-fake"
    (directory / "codex-fake.stderr").write_text(stderr_text, encoding="utf-8")
    path.write_text('#!/bin/sh\ncat >/dev/null\ncat "$0.stderr" >&2\nexit 1\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    project = make_project(tmp_path / "mock", [SELECTION])
    template = project.root / "prompts" / "write.md"
    template.parent.mkdir()
    template.write_text("Write about {{topic}}.\n", encoding="utf-8")
    monkeypatch.delenv(codex_command.CODEX_EXECUTABLE_ENV, raising=False)
    codex_command.reset_detection_cache()
    with project.environment():
        yield project
    codex_command.reset_detection_cache()


def call_with_codex(project, tmp_path, monkeypatch, stderr_text: str):
    monkeypatch.setenv(codex_command.CODEX_EXECUTABLE_ENV, fake_codex(tmp_path, stderr_text))
    codex_command.reset_detection_cache()
    mock = MockBackendFactory(responses=["unused"])

    def factory(endpoint=None, *, model=None, cheap=False, project_root=None):
        real = mock(endpoint, model=model, cheap=cheap, project_root=project_root)
        backend = CodexCliBackend(sessions_dir=tmp_path / "no-sessions")
        return dataclasses.replace(real, backend=backend)

    params = {"model": SELECTION, "prompt": str(project.root / "prompts" / "write.md"), "slots": {"topic": "tea"}}
    ctx = op_context(project.root, params=params, services={"backend_factory": factory}, node_path="write")
    return llm_model(ctx)


def test_the_provider_stderr_reaches_the_failure(project, tmp_path, monkeypatch):
    with pytest.raises(OpFailed) as caught:
        call_with_codex(project, tmp_path, monkeypatch, STDERR)
    message = str(caught.value)
    assert "codex exec failed (exit 1)" in message
    assert "invalid_json_schema" in message and "got 'type: \"None\"'" in message
    assert caught.value.receipt["error"]["message"].endswith(STDERR)


def test_the_stderr_tail_is_bounded_and_keeps_the_end(project, tmp_path, monkeypatch):
    noise = "x" * (STDERR_TAIL_CHARS * 3)
    with pytest.raises(OpFailed) as caught:
        call_with_codex(project, tmp_path, monkeypatch, noise + "THE-END")
    message = str(caught.value)
    assert "THE-END" in message and "..." in message
    assert len(message) < STDERR_TAIL_CHARS + 600


def test_a_failure_without_stderr_keeps_its_message(project):
    with pytest.raises(OpFailed) as caught:
        params = {"model": SELECTION, "prompt": str(project.root / "prompts" / "write.md"), "slots": {"topic": "t"}}
        ctx = op_context(project.root, params=params, services={"backend_factory": MockBackendFactory(
            responses=[CodexRunError("codex exec failed (exit 1)")])}, node_path="write")
        llm_model(ctx)
    assert str(caught.value).endswith("codex exec failed (exit 1)")
