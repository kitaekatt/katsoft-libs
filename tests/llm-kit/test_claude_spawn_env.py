"""One definition of the Claude child environment, used by every Claude spawn path.

Four paths start a ``claude`` child: the completion backend (``ClaudeCliBackend``), the agent session
(``ClaudeAgentSession``), the relay's resumed turn (``ResumeRunner._run_claude``) and the ``--bg``
driver's worker launch (``dispatch_wave`` through ``compose_worker_environment``). Each builds the
child's environment with ``llm_kit.adapters.claude_env._claude_child_env``, so the parent's
``CLAUDECODE`` (the marker of a process running inside Claude Code) never reaches a child.

1. Each path passes, through its real code, an environment that function produced. A spy wraps the
   function and adds a unique marker; the fake runner or Popen must see that marker.
2. ``CLAUDECODE`` set in the parent reaches no child.
3. An AST scan: every ``subprocess`` / ``asyncio`` spawn call in ``llm_kit/adapters`` and the Claude
   driver and relay spawn modules passes an explicit ``env=``, except the named exceptions below.

No process is started.
"""
from __future__ import annotations

import ast
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from llm_kit.adapters import claude_command, claude_session
from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.claude_session import ClaudeAgentSession
from llm_kit.completion.session_types import SESSION_TAG_ENV, SessionBounds, SessionPaths, SessionRecord, SessionRequest
from llm_kit.completion.types import BackendOptions
from llm_kit.jobs.session import FileSessionChannel, create_attempt_dir, write_session_record
from llm_kit.pipeline.execution.adapter import RunAdapter
from llm_kit.pipeline.execution.drivers import claude_bg
from llm_kit.pipeline.execution.store import ExecutionStore
from llm_kit.pipeline.execution.workerpack import WorkerCommand
from llm_kit.relay import resume as relay_resume

SESSION_ID = "0b6f2c1e-3d4a-4b5c-8d9e-0f1a2b3c4d5e"
MARK = "LLM_KIT_TEST_ENV_ORIGIN"
KEEP = "LLM_KIT_TEST_SPAWN_SENTINEL"
SRC = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src" / "llm_kit"


class _Spawned(RuntimeError):
    """Raised by a fake spawn so no process starts."""


@pytest.fixture
def parent_env(monkeypatch):
    """The parent carries the Claude Code marker, a CLAUDE_CODE_ variable, a billing variable and a sentinel."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-key")
    monkeypatch.setenv(KEEP, "keep-me")
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)  # no claudx propagation in these tests
    monkeypatch.setattr(claude_command, "_on_windows", lambda: False)  # no batch-shim rewrite


@pytest.fixture
def spy(monkeypatch):
    """Wrap ``_claude_child_env`` wherever a loaded llm_kit module binds it; record each environment it made."""
    from llm_kit.adapters import claude_env

    original = claude_env._claude_child_env
    made: list = []

    def wrapper(*args, **kwargs):
        env = original(*args, **kwargs)
        env[MARK] = uuid.uuid4().hex
        made.append(dict(env))
        return env

    for name, module in list(sys.modules.items()):
        if name.startswith("llm_kit") and getattr(module, "_claude_child_env", None) is original:
            monkeypatch.setattr(module, "_claude_child_env", wrapper)
    return made


# -- the four paths ---------------------------------------------------------------------------------


def _completion_env(tmp_path: Path) -> dict:
    seen: dict = {}

    def runner(cmd, request, cwd, **kwargs):
        seen.update(kwargs)
        return json.dumps({"result": "ok", "is_error": False, "usage": {}}), "", 0

    ClaudeCliBackend(runner=runner, executable="claude").complete(
        "sys", "usr", model="m", options=BackendOptions(cwd=tmp_path, timeout_s=5.0)
    )
    return dict(seen["env"])


def _session_env(tmp_path: Path, monkeypatch) -> dict:
    paths = SessionPaths.for_attempt(tmp_path / "sessions", "job-1", 1)
    create_attempt_dir(paths)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    tag = "lks_" + "0" * 32
    write_session_record(paths, SessionRecord(
        run_id="run-1", job_id="job-1", attempt_no=1, session_id=SESSION_ID, resumed=False, cwd=cwd,
        process_tag=tag, created_at="2026-11-14T00:00:00Z",
    ))
    request = SessionRequest(
        system="SYSTEM", user="hello", model="haiku",
        options=BackendOptions(timeout_s=30.0, cwd=cwd, system_prompt_mode="append"),
        cwd=cwd, session_id=SESSION_ID, resume=False, process_tag=tag, paths=paths,
        bounds=SessionBounds(quiescence_s=0.5, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0),
        channel=FileSessionChannel(paths),
    )
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        raise _Spawned

    monkeypatch.setattr(claude_session.subprocess, "Popen", fake_popen)
    with pytest.raises(_Spawned):
        ClaudeAgentSession(executable="claude").run(request)
    assert seen["env"][SESSION_TAG_ENV] == tag
    return dict(seen["env"])


def _relay_env(tmp_path: Path, monkeypatch) -> dict:
    job = relay_resume.ResumeJob(
        lease_id="lse_test", message_id="m1", address="a1", harness="claude", session_id=SESSION_ID,
        cwd=str(tmp_path), prompt="go on", timeout_s=5.0,
    )
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        raise _Spawned

    monkeypatch.setattr(relay_resume.subprocess, "Popen", fake_popen)
    runner = relay_resume.ResumeRunner(None, claude_argv=["claude"])
    with pytest.raises(_Spawned):
        runner._run_claude(relay_resume._Active(job=job), time.monotonic() + 5.0)
    assert seen["env"][relay_resume.LEASE_ENV] == job.tag
    return dict(seen["env"])


def _bg_env(tmp_path: Path, monkeypatch) -> dict:
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    store.create_run("r1", driver="claude_bg", backend="claude", model="m", adapter_version="")
    store.register_units("r1", ["u1"])
    seen: dict = {}

    def fake_dispatch_unit(*args, **kwargs):
        seen.update(kwargs)
        raise _Spawned

    monkeypatch.setattr(claude_bg, "preflight", lambda cli, env=None: None)
    monkeypatch.setattr(claude_bg, "dispatch_unit", fake_dispatch_unit)
    with pytest.raises(_Spawned):
        claude_bg.dispatch_wave(
            store, "r1", [store.get_unit("r1", "u1")], RunAdapter(),
            cli=object(), worker_command=WorkerCommand(argv=("worker",), answer_dir=str(tmp_path)),
            sleep_fn=lambda _s: None,
        )
    return dict(seen["env"])


PATHS = {
    "completion": lambda tmp_path, monkeypatch: _completion_env(tmp_path),
    "agent-session": _session_env,
    "relay-resume": _relay_env,
    "bg-driver": _bg_env,
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_each_path_spawns_with_an_env_from_the_one_definition(path, parent_env, spy, tmp_path, monkeypatch):
    env = PATHS[path](tmp_path, monkeypatch)
    assert MARK in env, f"{path}: the spawn env was not built by _claude_child_env"
    made = [candidate for candidate in spy if candidate[MARK] == env[MARK]]
    assert len(made) == 1
    if path == "bg-driver":
        # The --bg driver then subtracts its own billing and forbidden names (unchanged in this release).
        assert env == {k: v for k, v in made[0].items() if k not in claude_bg.BILLING_DIVERTING_VARS}
    else:
        assert env == made[0]


@pytest.mark.parametrize("path", sorted(PATHS))
def test_claudecode_in_the_parent_reaches_no_child(path, parent_env, tmp_path, monkeypatch):
    env = PATHS[path](tmp_path, monkeypatch)
    assert "CLAUDECODE" not in env
    assert env[KEEP] == "keep-me"


@pytest.mark.parametrize("path", sorted(PATHS))
def test_only_the_relay_drops_claude_code_variables_and_only_bg_drops_billing(path, parent_env, tmp_path, monkeypatch):
    # Pins this release's removals: no path loses a removal it made before, and none gains one beyond
    # CLAUDECODE. Billing variables are deliberately not stripped on the other paths yet.
    env = PATHS[path](tmp_path, monkeypatch)
    assert ("CLAUDE_CODE_ENTRYPOINT" in env) is (path != "relay-resume")
    assert ("ANTHROPIC_API_KEY" in env) is (path != "bg-driver")


# -- the function -----------------------------------------------------------------------------------


def test_the_function_removes_the_marker_keeps_the_rest_and_adds_extras():
    from llm_kit.adapters.claude_env import _claude_child_env

    base = {"CLAUDECODE": "1", "CLAUDE_CODE_X": "y", "PATH": "/bin", "ANTHROPIC_API_KEY": "k"}
    assert _claude_child_env(base) == {"CLAUDE_CODE_X": "y", "PATH": "/bin", "ANTHROPIC_API_KEY": "k"}
    assert _claude_child_env(base, remove_prefixes=("CLAUDE_CODE_",), extra={"TAG": "t"}) == {
        "PATH": "/bin", "ANTHROPIC_API_KEY": "k", "TAG": "t",
    }
    assert base["CLAUDECODE"] == "1"  # the input is not mutated


def test_the_function_reads_the_process_environment_by_default(monkeypatch):
    from llm_kit.adapters.claude_env import _claude_child_env

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv(KEEP, "v")
    env = _claude_child_env()
    assert "CLAUDECODE" not in env and env[KEEP] == "v"


@pytest.mark.parametrize("extra", [{"CLAUDECODE": "1"}, {"CLAUDE_CODE_X": "1"}])
def test_an_extra_may_not_put_back_a_removed_variable(extra):
    from llm_kit.adapters.claude_env import _claude_child_env

    with pytest.raises(ValueError, match="removes"):
        _claude_child_env({}, remove_prefixes=("CLAUDE_CODE_",), extra=extra)


# -- the AST guard ----------------------------------------------------------------------------------

#: Modules scanned besides every module in llm_kit/adapters.
SCANNED_EXTRA = ("pipeline/execution/drivers/claude_bg.py", "relay/resume.py")

#: ``(module, enclosing function)`` spawn calls allowed without ``env=``, each with its reason.
EXCEPTIONS = {
    ("adapters/codex_command.py", "_detect_codex_uncached"): (
        "`codex --version` probe of the installed Codex CLI: no prompt, no model call, not a Claude "
        "child; it inherits the environment as the Codex backend it detects does"
    ),
}

_SPAWNERS = {
    ("subprocess", "Popen"), ("subprocess", "run"), ("subprocess", "call"), ("subprocess", "check_call"),
    ("subprocess", "check_output"), ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
}


def _spawn_calls_without_env(tree: ast.AST) -> list:
    """``(function, line)`` of each spawn call that has no ``env=`` or passes ``env=None`` literally."""
    found = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and isinstance(child.func.value, ast.Name):
                if (child.func.value.id, child.func.attr) in _SPAWNERS:
                    env = [kw for kw in child.keywords if kw.arg == "env"]
                    if not env or (isinstance(env[0].value, ast.Constant) and env[0].value.value is None):
                        found.append((function, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def _scanned_modules() -> list:
    modules = sorted(path.relative_to(SRC).as_posix() for path in (SRC / "adapters").glob("*.py"))
    return modules + list(SCANNED_EXTRA)


def test_the_scan_covers_every_claude_spawn_module():
    modules = _scanned_modules()
    for required in ("adapters/claude_runner.py", "adapters/claude_session.py", *SCANNED_EXTRA):
        assert required in modules and (SRC / required).is_file()


def test_every_cli_spawn_passes_an_explicit_env():
    offenders, used = [], set()
    for module in _scanned_modules():
        tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
        for function, line in _spawn_calls_without_env(tree):
            if (module, function) in EXCEPTIONS:
                used.add((module, function))
            else:
                offenders.append(f"{module}:{line} in {function}()")
    assert not offenders, "spawn calls without an explicit env= (add env=, or name an exception with its reason): " + ", ".join(offenders)
    assert used == set(EXCEPTIONS), f"stale exceptions: {sorted(set(EXCEPTIONS) - used)}"


def test_the_scan_flags_a_spawn_without_env_and_one_with_env_none():
    source = (
        "import subprocess, asyncio\n"
        "def a():\n    subprocess.Popen(['claude'])\n"
        "def b():\n    subprocess.run(['claude'], env=None)\n"
        "async def c():\n    await asyncio.create_subprocess_exec('claude')\n"
        "def d(env):\n    subprocess.run(['claude'], env=env)\n"
    )
    assert [function for function, _line in _spawn_calls_without_env(ast.parse(source))] == ["a", "b", "c"]
