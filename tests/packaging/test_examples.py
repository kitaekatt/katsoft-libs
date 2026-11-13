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

import pytest

import closure_support as support

pytestmark = pytest.mark.wheel_closure

EXAMPLES = support.REPO / "packages" / support.KIT_DIST / "examples"


def example_problems(venv, tmp_path) -> list[str]:
    """Why an example does not run under ``venv``; empty when every example runs."""
    copy = tmp_path / "examples"
    shutil.copytree(EXAMPLES, copy)
    home = tmp_path / "home"
    home.mkdir()
    scripts = sorted(copy.glob("*.py"))
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


def test_a_broken_example_is_detected(closure_venv, tmp_path, monkeypatch):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "fails.py").write_text("raise SystemExit('broken on purpose')\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "EXAMPLES", broken)
    problems = example_problems(closure_venv, tmp_path / "run")
    assert problems and "broken on purpose" in problems[0]
