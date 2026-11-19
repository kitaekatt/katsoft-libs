"""One definition of the Claude child environment, used by every Claude spawn path.

Every path that starts a ``claude`` child builds its environment with
``llm_kit.adapters.claude_env._claude_child_env``, so the parent's ``CLAUDECODE`` (the marker of a
process running inside Claude Code) never reaches a child. ``PATHS`` below names each path. A
command the caller names (a contract command, a compare scorer) is a named exception: see
``llm_kit.adapters.claude_env``.

1. Each path passes, through its real code, an environment that function produced. A spy wraps the
   function and adds a unique marker; the fake runner or Popen must see that marker. A path that
   spawns through an injected runner (``claude_auth``, ``ClaudeCli``, ``ClaudeCliBackend``) is called
   with a spy runner.
2. ``CLAUDECODE`` set in the parent reaches no child.
3. An AST scan of the WHOLE ``llm_kit`` source tree finds every process spawn (``subprocess.*``,
   ``asyncio.create_subprocess_*``, ``os.spawn*``, ``os.exec*``, ``os.system``, ``os.popen``,
   ``os.posix_spawn*``). Each one is either a Claude spawn site, which passes an explicit ``env=``
   and names the ``PATHS`` entries that prove its environment at run time, or a named exception with
   its reason (a child that is not ``claude``). A new spawn anywhere fails until it is classified.
4. Every module that resolves the Claude executable is a module of a ``PATHS`` entry, so a new path
   that starts ``claude`` through an injected runner also fails until it is covered.

No ``claude`` process is started.
"""
from __future__ import annotations

import ast
import json
import subprocess
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


def _harness_auth_env(tmp_path: Path, monkeypatch) -> dict:
    from llm_kit.adapters import harness_auth

    for name in harness_auth.BILLING_DIVERTING_VARS:  # with one set, the probe answers metered unspawned
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(harness_auth.CLAUDE_EXECUTABLE_ENV, raising=False)
    seen: dict = {}

    def runner(argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"authMethod":"claude.ai","apiProvider":"firstParty"}', "")

    assert harness_auth.claude_auth(runner=runner).state == harness_auth.SUBSCRIPTION
    assert seen["argv"] == ["claude", "auth", "status"]
    return dict(seen["env"])


def _reachability_env(tmp_path: Path, monkeypatch) -> dict:
    from llm_kit.dispatch import reachability

    monkeypatch.setattr(reachability, "resolve_claude_cli", lambda *args, **kwargs: ("claude",))
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"2.1.0 (Claude Code)\n", None)

    monkeypatch.setattr(reachability.subprocess, "run", fake_run)
    assert reachability.check_harness("claude").status == reachability.STATUS_REACHABLE
    assert seen["argv"] == ["claude", "--version"]
    return dict(seen["env"])


def _bg_cli_env(verb: str):
    """``ClaudeCli``'s own calls (admin verbs, version, the launch): its injected runner sees the env."""

    def build(tmp_path: Path, monkeypatch) -> dict:
        seen: dict = {}

        def runner(argv, **kwargs):
            seen["argv"] = list(argv)
            seen.update(kwargs)
            return "", "", 0

        cli = claude_bg.ClaudeCli(executable="claude", runner=runner)
        calls = {
            "agents": lambda: cli.agents_json(),
            "stop": lambda: cli.stop("sess-1"),
            "rm": lambda: cli.rm("sess-1"),
            "respawn": lambda: cli.respawn("sess-1"),
            "version": lambda: cli.version(),
            "launch": lambda: cli.launch_bg("do the unit"),
        }
        calls[verb]()
        assert seen["argv"][0] == "claude"
        return dict(seen["env"])

    return build


BG_CLI_VERBS = ("agents", "stop", "rm", "respawn", "version", "launch")

PATHS = {
    "completion": lambda tmp_path, monkeypatch: _completion_env(tmp_path),
    "agent-session": _session_env,
    "relay-resume": _relay_env,
    "bg-driver": _bg_env,
    "harness-auth": _harness_auth_env,
    "reachability": _reachability_env,
    **{f"bg-cli-{verb}": _bg_cli_env(verb) for verb in BG_CLI_VERBS},
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
    if path != "harness-auth":  # that probe never spawns while a billing variable is set
        assert ("ANTHROPIC_API_KEY" in env) is (path != "bg-driver")


def test_a_bg_cli_call_given_an_env_with_the_marker_still_drops_it(parent_env):
    seen: dict = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return "", "", 0

    claude_bg.ClaudeCli(executable="claude", runner=runner).stop("sess-1", env={"CLAUDECODE": "1", KEEP: "k"})
    assert seen["env"] == {KEEP: "k"}


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


def test_a_bg_worker_overlay_that_names_the_marker_raises():
    """The run's recorded overlay may not put CLAUDECODE back into a --bg worker's environment."""
    from types import SimpleNamespace

    from llm_kit.pipeline.execution.adapter import WorkerEnvironment

    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("CLAUDECODE",)))
    run = SimpleNamespace(environment={"CLAUDECODE": "1"})
    with pytest.raises(ValueError, match="removes"):
        claude_bg.compose_worker_environment(run, adapter, base={"PATH": "/bin"})


def test_names_compare_without_case_where_the_platform_ignores_case(monkeypatch):
    """On Windows a caller's mapping may spell the marker in any case; it is still the marker."""
    from llm_kit.adapters.claude_env import _claude_child_env
    from llm_kit.registry import consent

    base = {"claudecode": "1", "ClaudeCode": "1", "claude_code_x": "1", "PATH": "/bin"}
    monkeypatch.setattr(consent, "ENV_NAMES_IGNORE_CASE", True)
    assert _claude_child_env(base, remove_prefixes=("CLAUDE_CODE_",)) == {"PATH": "/bin"}
    for extra in ({"claudeCode": "1"}, {"Claude_Code_Y": "1"}):
        with pytest.raises(ValueError, match="removes"):
            _claude_child_env({}, remove_prefixes=("CLAUDE_CODE_",), extra=extra)
    monkeypatch.setattr(consent, "ENV_NAMES_IGNORE_CASE", False)  # POSIX: names are case-sensitive
    assert _claude_child_env(base, remove_prefixes=("CLAUDE_CODE_",)) == base


@pytest.mark.skipif(sys.platform != "win32", reason="the platform's own case rule")
def test_on_windows_a_lower_case_marker_in_a_caller_mapping_is_removed():
    from llm_kit.adapters.claude_env import _claude_child_env

    assert _claude_child_env({"claudecode": "1", "PATH": "x"}) == {"PATH": "x"}


# -- the AST guard: every process spawn in the package is classified -------------------------------

#: Claude spawn sites ``(module, enclosing function)``: each passes an explicit ``env=``, and the
#: ``PATHS`` entries named prove at run time that the env reaching it came from ``_claude_child_env``.
CLAUDE_SITES = {
    ("adapters/claude_runner.py", "run_cli_streaming"): ("completion",),  # the transport; env from its caller
    ("adapters/claude_session.py", "run"): ("agent-session",),
    ("relay/resume.py", "_run_claude"): ("relay-resume",),
    ("pipeline/execution/drivers/claude_bg.py", "_default_runner"): (  # ClaudeCli's runner
        "bg-driver", *(f"bg-cli-{verb}" for verb in BG_CLI_VERBS)),
    ("dispatch/reachability.py", "_run_version_probe"): ("reachability",),
}

#: Spawn sites that do not start ``claude``, each with its reason.
EXCEPTIONS = {
    ("adapters/codex_command.py", "_detect_codex_uncached"): (
        "`codex --version` probe of the installed Codex CLI: not a Claude child; it inherits the "
        "environment as the Codex backend it detects does"
    ),
    ("bootstrap_guard.py", "reexec_under_plugin_venv"): (
        "re-executes the current Python program under the plugin's virtual environment: the same "
        "program, not a Claude child"
    ),
    ("dispatch/declaration.py", "_git"): "runs git",
    ("graph/records.py", "_git_common_dir"): "runs git",
    ("jobs/units.py", "_git"): "runs git",
    ("jobs/workspace.py", "_git_result"): "runs git",
    ("jobs/workspace.py", "_git_detection_result"): "runs git",
    ("pipeline/artifacts/vcs/git_vcs.py", "_default_runner"): "runs git",
    ("jobs/run.py", "_kill_contract_process_group"): "runs taskkill on a timed-out contract",
    ("jobs/run.py", "run_contract"): (
        "a contract command the job names: the caller's command, not a Claude spawn of this package; "
        "jobs may not import adapters (signed import graph), so it keeps the caller's environment"
    ),
    ("runs/compare.py", "score"): (
        "a scorer command the caller names: not a Claude spawn of this package; runs may not import "
        "adapters (signed import graph), so it inherits the caller's environment"
    ),
    ("relay/codex_appserver.py", "start"): "starts `codex app-server`: not a Claude child",
    ("graph/ops/subprocess.py", "llm_subprocess"): (
        "an allow-list environment (env_allow, default PATH HOME LANG LC_ALL TMPDIR, plus the node's "
        "env): CLAUDECODE reaches the command only when the graph names it"
    ),
    ("jobs/supervisor.py", "start"): "starts the package's own supervisor worker (Python), not a Claude child",
    ("jobs/supervisor.py", "adopt"): "starts the package's own recovery worker (Python), not a Claude child",
    ("jobs/supervisor.py", "_finish_ledger"): "starts the package's own finalizer worker (Python)",
    ("jobs/_supervisor_worker.py", "_run"): "starts the package's own child worker (Python)",
    ("jobs/_supervisor_worker.py", "_child"): (
        "a process primitive: replays the caller's JobProcessSpec argv and env verbatim; the package "
        "starts no model step through it, and the spec's env is the caller's to set"
    ),
}

_SUBPROCESS = {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}


def _is_os_spawn(attr: str) -> bool:
    return attr.startswith(("spawn", "exec", "posix_spawn")) or attr in ("system", "popen", "startfile")


def _spawn_calls(tree: ast.AST) -> list:
    """``(function, line, env)`` of each process spawn; ``env`` is the ``env=`` node or None."""
    aliases: dict = {}  # a bare name imported from subprocess, asyncio or os -> (module, attr)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "asyncio", "os"):
            for alias in node.names:
                aliases[alias.asname or alias.name] = (node.module, alias.name)

    def spawner(func: ast.AST):
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return func.value.id, func.attr
        if isinstance(func, ast.Name) and func.id in aliases:
            return aliases[func.id]
        return None

    found = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call):
                target = spawner(child.func)
                if target is not None:
                    module, attr = target
                    if ((module == "subprocess" and attr in _SUBPROCESS)
                            or (module == "asyncio" and attr.startswith("create_subprocess_"))
                            or (module == "os" and _is_os_spawn(attr))):
                        env = next((kw.value for kw in child.keywords if kw.arg == "env"), None)
                        found.append((function, child.lineno, env))
            visit(child, name)

    visit(tree, "<module>")
    return found


def _package_modules() -> list:
    return sorted(path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py"))


def test_the_scan_reads_the_whole_package():
    modules = _package_modules()
    assert len(modules) > 100
    for module, _function in (*CLAUDE_SITES, *EXCEPTIONS):
        assert module in modules


def test_every_process_spawn_in_the_package_is_a_claude_site_or_a_named_exception():
    unclassified, without_env, used = [], [], set()
    for module in _package_modules():
        for function, line, env in _spawn_calls(ast.parse((SRC / module).read_text(encoding="utf-8"))):
            site = (module, function)
            used.add(site)
            if site in CLAUDE_SITES:
                if env is None or (isinstance(env, ast.Constant) and env.value is None):
                    without_env.append(f"{module}:{line} in {function}()")
            elif site not in EXCEPTIONS:
                unclassified.append(f"{module}:{line} in {function}()")
    assert not unclassified, (
        "process spawns that are neither a Claude site nor a named exception (route a claude child "
        "through _claude_child_env and add a PATHS test, or name the exception and its reason): "
        + ", ".join(unclassified))
    assert not without_env, "Claude spawn sites without an explicit env=: " + ", ".join(without_env)
    assert used >= set(CLAUDE_SITES) | set(EXCEPTIONS), (
        f"stale entries: {sorted((set(CLAUDE_SITES) | set(EXCEPTIONS)) - used)}")


def test_each_claude_site_names_paths_that_exist():
    for site, paths in CLAUDE_SITES.items():
        assert paths and set(paths) <= set(PATHS), site


#: Each module that resolves or names the Claude executable, and the ``PATHS`` entries covering it.
CLAUDE_MODULES = {
    "adapters/claude_command.py": ("completion", "agent-session"),  # defines the resolvers
    "adapters/backends.py": ("completion",),
    "adapters/claude_session.py": ("agent-session",),
    "adapters/harness_auth.py": ("harness-auth",),
    "dispatch/reachability.py": ("reachability",),
    "pipeline/execution/drivers/claude_bg.py": ("bg-driver", *(f"bg-cli-{verb}" for verb in BG_CLI_VERBS)),
    "relay/resume.py": ("relay-resume",),
}

_RESOLVES_CLAUDE = ("resolve_claude_executable", "resolve_claude_cli", 'which("claude")', "which('claude')")


def test_every_module_that_resolves_the_claude_executable_is_covered_by_a_path():
    resolving = {
        module for module in _package_modules()
        if any(token in (SRC / module).read_text(encoding="utf-8") for token in _RESOLVES_CLAUDE)
    }
    assert resolving == set(CLAUDE_MODULES), (
        "a module resolves the claude executable without a PATHS test (an injected-runner spawn "
        f"needs a spy-runner test): {sorted(resolving ^ set(CLAUDE_MODULES))}")
    for paths in CLAUDE_MODULES.values():
        assert set(paths) <= set(PATHS)


def test_the_scan_finds_each_spawn_form_and_reads_env():
    source = (
        "import subprocess, asyncio, os\n"
        "from subprocess import Popen as P\n"
        "def a():\n    subprocess.Popen(['claude'])\n"
        "def b():\n    subprocess.run(['claude'], env=None)\n"
        "async def c():\n    await asyncio.create_subprocess_exec('claude')\n"
        "def d(env):\n    subprocess.run(['claude'], env=env)\n"
        "def e():\n    os.execvpe('claude', ['claude'], {})\n"
        "def f():\n    P(['claude'])\n"
        "def g():\n    os.system('claude')\n"
        "def h():\n    os.spawnv(0, 'claude', ['claude'])\n"
        "def i():\n    print('not a spawn')\n"
    )
    found = _spawn_calls(ast.parse(source))
    assert [function for function, _line, _env in found] == ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert [env is None for _function, _line, env in found] == [True, False, True, False, True, True, True, True]
