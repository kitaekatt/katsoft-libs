"""The package examples run in a clean venv (AGENTS.md, "Quality bar", line 5).

Each ``packages/katsoft-llm-kit/examples/*.py`` is copied out of the repository
and run with ``python -I`` in the closure venv: the built wheels and their
declared extras, a scratch HOME, no PYTHONPATH, no plugin tree and no client app.

Opt-in (marker ``wheel_closure``); see conftest.py.

    uv run --extra dev pytest tests/packaging --wheel-source local -v
"""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

import closure_support as support

pytestmark = pytest.mark.wheel_closure

EXAMPLES = support.REPO / "packages" / support.KIT_DIST / "examples"
#: Examples that need the ``local`` extra (psutil: agent sessions and branch mode), so they run in
#: ``session_venv``; every other example runs in ``closure_venv``. Both sets are POSIX-only (the
#: example launches a ``/bin/sh`` stand-in for ``claude``).
SESSION_EXAMPLES = ("agent_session_branch.py",)


def example_problems(venv, tmp_path, *, session: bool = False) -> list[str]:
    """Why an example does not run under ``venv``; empty when every example runs.

    ``session`` selects the ``SESSION_EXAMPLES`` instead of all the others.
    """
    copy = tmp_path / "examples"
    shutil.copytree(EXAMPLES, copy)
    home = tmp_path / "home"
    home.mkdir()
    scripts = [path for path in sorted(copy.glob("*.py")) if (path.name in SESSION_EXAMPLES) == session]
    if not scripts:
        return ["no example scripts"]
    problems = []
    for script in scripts:
        proc = subprocess.run([str(support.venv_python(venv)), "-I", str(script)], capture_output=True,
                              text=True, env=support.child_env(venv, home), cwd=tmp_path, timeout=120)
        if proc.returncode != 0 or not proc.stdout.strip():
            problems.append(f"{script.name}: exit {proc.returncode}: {proc.stderr.strip()[-400:]}")
    return problems


def test_every_example_runs_in_the_clean_venv(closure_venv, tmp_path):
    assert example_problems(closure_venv, tmp_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="the agent-session example launches a /bin/sh stand-in")
def test_the_agent_session_examples_run_in_the_clean_venv(session_venv, tmp_path):
    """Agent sessions and branch mode, end to end, from the built wheel (quality bar line 5)."""
    for name in SESSION_EXAMPLES:
        assert (EXAMPLES / name).is_file(), name
    assert example_problems(session_venv, tmp_path, session=True) == []


def test_a_broken_example_is_detected(closure_venv, tmp_path, monkeypatch):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "fails.py").write_text("raise SystemExit('broken on purpose')\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "EXAMPLES", broken)
    problems = example_problems(closure_venv, tmp_path / "run")
    assert problems and "broken on purpose" in problems[0]


def test_the_run_store_example_needs_no_extra(base_venv, tmp_path):
    """llm_kit.runs is standard library only: its example runs with no extra installed."""
    script = tmp_path / "record_runs.py"
    shutil.copy(EXAMPLES / "record_runs.py", script)
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run([str(support.venv_python(base_venv)), "-I", str(script)], capture_output=True,
                          text=True, env=support.child_env(base_venv, home), cwd=tmp_path, timeout=120)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "refused as expected (policy)" in proc.stdout
