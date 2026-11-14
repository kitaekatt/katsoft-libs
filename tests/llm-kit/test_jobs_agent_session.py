"""``jobs run`` with ``session: agent`` jobs: admission, reservation, the outcome mapping and the run checks.

The agent sessions run the fake ``claude`` of ``fixtures/fake_claude_session.py`` through the real
:class:`~llm_kit.adapters.claude_session.ClaudeAgentSession`, built by ``dispatch`` from a selected
``ClaudeCliBackend`` whose ``executable`` names the fake. No model runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.capabilities import CLAUDE_AGENT_CAPABILITIES, CLAUDE_CAPABILITIES, CODEX_CAPABILITIES
from llm_kit.adapters.claude_session import ClaudeAgentSession
from llm_kit.cli import jobs as cli_jobs
from llm_kit.completion.session_types import (
    DetachedSurvivor,
    ProcessIdentity,
    SessionBounds,
    SessionPaths,
    SessionMode,
    SessionOutcome,
    SessionWorkspaceHook,
    StopKind,
    StopMode,
    StopRequestKind,
    SupervisorBudgetError,
    WorkspaceFinalizeResult,
    WorkspaceFinalizeStatus,
    encode_job_id,
)
from llm_kit.completion.types import LLMResponse
from llm_kit.dispatch.factory import (
    AgentSessionUnsupportedError,
    BackendSelection,
    agent_session_capabilities,
    create_agent_session,
)
from llm_kit.jobs import run as jobs
from llm_kit.jobs import select as jobs_select
from llm_kit.jobs.model import Contract, Job, JobState, Prompt, WorkspaceSpec
from llm_kit.jobs.session import read_session_record, read_state, request_stop
from llm_kit.jobs.store import JobStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a POSIX shell wrapper")

FAKE = Path(__file__).parent / "fixtures" / "fake_claude_session.py"
PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit"
FAST = {
    "session": "agent",
    "session_quiescence_s": 0.5,
    "session_result_wait_s": 3,
    "session_eof_wait_s": 3,
    "session_sigint_wait_s": 2,
}
BUDGET = 100_000.0


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import llm_kit.dispatch.declaration as declaration
    import llm_kit.dispatch.usage_budget as usage
    import llm_kit.registry.models as models

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    monkeypatch.setattr(models, "discover_model_entries", lambda **_kwargs: {})
    monkeypatch.setattr(declaration, "check_many", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(usage, "record_observed_halt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs.BackpressurePolicy, "from_environment",
                        staticmethod(lambda: jobs.BackpressurePolicy(cap_s=0.0)))


def _fake_cli(directory: Path) -> Path:
    path = directory / "fake-claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="ascii")
    path.chmod(0o755)
    return path


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str = "basic") -> None:
        self.root = tmp_path
        self.cli = _fake_cli(tmp_path)
        self.log = tmp_path / "fake"
        monkeypatch.setenv("FAKE_SESSION_SCENARIO", scenario)
        monkeypatch.setenv("FAKE_SESSION_LOG", str(self.log))
        self.work = tmp_path / "work"
        self.work.mkdir(exist_ok=True)
        self.ledger = tmp_path / "ledger.sqlite3"
        self.sessions = tmp_path / "sessions"
        self.backends: list[ClaudeCliBackend] = []

    def factory(self, endpoint: str, **_kwargs: Any) -> BackendSelection:
        backend = ClaudeCliBackend(executable=str(self.cli))
        self.backends.append(backend)
        return BackendSelection(endpoint, "harness", backend, "haiku", capabilities=CLAUDE_CAPABILITIES)

    def job(self, job_id: str = "agent-job", *, options: Optional[dict] = None, max_attempts: int = 1,
            check: str = "echo: do the work") -> Job:
        command = (sys.executable, "-I", "-c", f"import sys; sys.exit(0 if sys.stdin.read() == {check!r} else 1)")
        return Job(
            id=job_id, prompt=Prompt(user="do the work", system="SYS"), models=("h-claude",),
            directory=self.work, max_attempts=max_attempts, options=dict(FAST if options is None else options),
            contract=Contract(command=command, directory=self.work),
        )

    def run(self, jobs_list: list[Job], **kwargs: Any):
        params: dict[str, Any] = dict(
            run_id="agent-run", backend_factory=self.factory, session_root=self.sessions,
            supervisor_budget_s=BUDGET, workspace_root=self.root / "ws",
        )
        params.update(kwargs)
        return jobs.run_jobs(jobs_list, self.ledger, **params)

    def attempt_dir(self, job_id: str, attempt_no: int = 1) -> Path:
        return self.sessions / encode_job_id(job_id) / f"attempt-{attempt_no}"

    def argv(self) -> list[str]:
        return json.loads(Path(str(self.log) + ".argv.json").read_text())["argv"]

    def events(self, job_id: str, attempt_no: int = 1) -> bytes:
        path = self.attempt_dir(job_id, attempt_no) / "events.jsonl"
        return path.read_bytes() if path.exists() else b""


# ---------------------------------------------------------------------------
# the agent path end to end
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated")
def test_an_agent_job_runs_as_a_session_and_its_contract_reads_the_last_result(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    snapshot = world.run([world.job()])
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    (attempt,) = snapshot.attempts
    assert attempt.session_mode is SessionMode.AGENT and attempt.stop_kind is StopKind.QUIESCENT
    assert str(uuid.UUID(attempt.session_id)) == attempt.session_id
    assert attempt.session_dir == world.attempt_dir("agent-job")
    assert attempt.execution_controls_applied == ("permission-bypass",)
    assert attempt.response_text == "echo: do the work"
    argv = world.argv()
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"  # agent default: append
    assert argv[argv.index("--session-id") + 1] == attempt.session_id
    assert "--no-session-persistence" not in argv
    (reservation,) = JobStore(world.ledger).list_reservations("agent-run", "agent-job")
    assert reservation.session_id == attempt.session_id and reservation.stop_kind is StopKind.QUIESCENT
    record = read_session_record(_paths(world, "agent-job"))
    assert record.stop_kind is StopKind.QUIESCENT and record.shutdown_confirmed is True
    assert record.first_init_at is not None and record.session_id == attempt.session_id


def _paths(world: World, job_id: str, attempt_no: int = 1):
    from llm_kit.completion.session_types import SessionPaths

    return SessionPaths(world.attempt_dir(job_id, attempt_no))


@pytest.mark.usefixtures("isolated")
def test_the_reservation_carries_the_session_id_before_any_attempt_row(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    seen: dict[str, Any] = {}
    real = ClaudeAgentSession.run

    def spy(self, request):
        store = JobStore(world.ledger)
        seen["reservations"] = store.list_reservations("agent-run", "agent-job")
        seen["attempts"] = store.list_attempts("agent-run", "agent-job")
        seen["request"] = request
        return real(self, request)

    monkeypatch.setattr(ClaudeAgentSession, "run", spy)
    world.run([world.job()])
    (live,) = seen["reservations"]
    assert seen["attempts"] == []
    assert live.disposition is None and live.session_mode is SessionMode.AGENT
    assert live.session_id == seen["request"].session_id
    assert live.session_cwd == seen["request"].cwd and live.session_dir == seen["request"].paths.attempt_dir


@pytest.mark.usefixtures("isolated")
def test_a_resume_session_job_resumes_that_id(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    resume_id = str(uuid.uuid4())
    snapshot = world.run([world.job(options={**FAST, "resume_session": resume_id})])
    assert snapshot.attempts[0].session_id == resume_id
    argv = world.argv()
    assert argv[argv.index("--resume") + 1] == resume_id and "--session-id" not in argv
    assert read_session_record(_paths(world, "agent-job")).resumed is True


@pytest.mark.usefixtures("isolated")
def test_an_isolated_repository_subdirectory_job_runs_in_its_worktree_subdirectory(tmp_path, monkeypatch):
    """The planned cwd recorded on the reservation is the worktree's repository subdirectory."""
    import os
    import subprocess

    world = World(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    sub = repo / "pkg"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x", encoding="ascii")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    job = world.job()
    job = Job(id=job.id, prompt=job.prompt, models=job.models, directory=sub, options=job.options,
              workspace=WorkspaceSpec(isolate=True), contract=Contract(command=job.contract.command, directory=sub))
    snapshot = world.run([job])
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    (attempt,) = snapshot.attempts
    assert attempt.workspace_status == "isolated" and attempt.workspace is not None
    expected = os.path.realpath(attempt.workspace / "pkg")
    assert str(attempt.session_cwd) == expected
    assert json.loads(Path(str(world.log) + ".argv.json").read_text())["cwd"] == expected
    (reservation,) = JobStore(world.ledger).list_reservations("agent-run", job.id)
    assert os.path.realpath(reservation.session_cwd) == expected


# ---------------------------------------------------------------------------
# operator stop, timeout, failures
# ---------------------------------------------------------------------------


def _stop_when_running(world: World, job_id: str, kind: StopRequestKind = StopRequestKind.STOP) -> threading.Thread:
    def stopper() -> None:
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if b'"init"' in world.events(job_id):
                time.sleep(0.1)
                request_stop(_paths(world, job_id), kind, StopMode.INTERRUPT)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=stopper, daemon=True)
    thread.start()
    return thread


@pytest.mark.usefixtures("isolated")
@pytest.mark.parametrize("kind, code", [(StopRequestKind.STOP, "operator_stop"), (StopRequestKind.PAUSE, "operator_pause")])
def test_an_operator_stop_cancels_the_job_after_one_attempt(tmp_path, monkeypatch, kind, code):
    world = World(tmp_path, monkeypatch, "busy")
    stopper = _stop_when_running(world, "agent-job", kind)
    snapshot = world.run([world.job(max_attempts=3)])
    stopper.join(timeout=5)
    assert snapshot.jobs[0].state is JobState.CANCELLED
    (attempt,) = snapshot.attempts
    assert attempt.error.code == code and attempt.stop_kind is StopKind(code)
    assert attempt.acceptance is None  # the contract never ran
    (reservation,) = JobStore(world.ledger).list_reservations("agent-run", "agent-job")
    assert reservation.stop_kind is StopKind(code)
    assert read_state(_paths(world, "agent-job")).close_reason is StopKind(code)


@pytest.mark.usefixtures("isolated")
def test_a_session_timeout_keeps_timeout_status_and_is_retried_by_budget(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "busy")
    snapshot = world.run([world.job(max_attempts=2)], timeout_s=1.0)
    assert snapshot.jobs[0].state is JobState.FAILED
    assert [a.status for a in snapshot.attempts] == ["timeout", "timeout"]
    assert [a.error.code for a in snapshot.attempts] == ["timeout", "timeout"]
    assert [a.stop_kind for a in snapshot.attempts] == [StopKind.TIMEOUT, StopKind.TIMEOUT]
    assert world.attempt_dir("agent-job", 2).is_dir()


@pytest.mark.usefixtures("isolated")
def test_an_auth_failure_halts_the_entry(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "auth")
    snapshot = world.run([world.job()])
    assert snapshot.jobs[0].state is JobState.HALTED
    assert snapshot.attempts[0].halt_kind == "auth" and snapshot.attempts[0].error.code == "auth"


@pytest.mark.usefixtures("isolated")
def test_an_unconfirmed_shutdown_is_never_accepted_and_never_retried(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)

    def unconfirmed(self, request):
        response = LLMResponse(text="echo: do the work", model="haiku", execution_controls_applied=("permission-bypass",))
        return SessionOutcome(response=response, session_id=request.session_id, cwd=request.cwd,
                              stop_kind=StopKind.QUIESCENT, result_subtype="success", shutdown_confirmed=False,
                              detached_survivors=())

    monkeypatch.setattr(ClaudeAgentSession, "run", unconfirmed)
    snapshot = world.run([world.job(max_attempts=3)])
    assert snapshot.jobs[0].state is JobState.FAILED
    (attempt,) = snapshot.attempts
    assert attempt.error.code == "shutdown_unconfirmed" and attempt.acceptance is None


@pytest.mark.usefixtures("isolated")
def test_a_job_with_only_non_claude_entries_is_unroutable_not_run_as_a_completion(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)

    class Codex:
        name = "codex-cli"
        capabilities = CODEX_CAPABILITIES

        def complete(self, *_args: object, **_kwargs: object) -> LLMResponse:
            raise AssertionError("an agent job must never run as a completion")

    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", Codex(), "gpt", capabilities=CODEX_CAPABILITIES)

    snapshot = world.run([world.job()], backend_factory=factory)
    assert snapshot.jobs[0].state is JobState.UNROUTABLE and snapshot.attempts == ()
    assert JobStore(world.ledger).list_reservations("agent-run", "agent-job") == []


# ---------------------------------------------------------------------------
# completion jobs are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated")
def test_a_completion_job_argv_is_unchanged(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def runner(argv, *_args, **_kwargs):
        calls.append(list(argv))
        return json.dumps({"result": "echo: do the work", "usage": {}}), "", 0

    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", ClaudeCliBackend(runner=runner, executable="claude"), "haiku")

    snapshot = jobs.run_jobs([world.job(options={})], world.ledger, run_id="c", backend_factory=factory,
                             workspace_root=tmp_path / "ws")
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert calls == [[
        "claude", "-p", "--model", "haiku", "--system-prompt", "SYS", "--output-format", "json",
        "--no-session-persistence", "--permission-mode", "bypassPermissions", "--allowedTools", "",
    ]]
    (attempt,) = snapshot.attempts
    assert attempt.session_mode is SessionMode.COMPLETION and attempt.session_id is None
    assert attempt.execution_controls_applied == ("allowed-tools", "permission-bypass", "no-session-persistence")


# ---------------------------------------------------------------------------
# run-level checks before any reservation
# ---------------------------------------------------------------------------


def _jobs_file(world: World, jobs_list: list[Job]) -> Path:
    path = world.root / "jobs.yaml"
    path.write_text(json.dumps({"jobs": [job.to_mapping() for job in jobs_list]}), encoding="utf-8")
    return path


@pytest.mark.usefixtures("isolated")
def test_a_missing_session_root_is_refused_for_the_whole_file_before_the_ledger_exists(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    path = _jobs_file(world, [world.job("plain", options={}), world.job("agent")])
    with pytest.raises(jobs.AgentSessionConfigError, match="--session-root"):
        jobs.run_job_file(path, store_path=world.ledger, backend_factory=world.factory, supervisor_budget_s=BUDGET)
    assert not world.ledger.exists()
    with pytest.raises(jobs.AgentSessionConfigError, match="--supervisor-budget"):
        jobs.run_job_file(path, store_path=world.ledger, backend_factory=world.factory,
                          session_root=world.sessions)
    assert not world.ledger.exists()


@pytest.mark.usefixtures("isolated")
def test_the_cli_reports_a_missing_session_root_clearly(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch)
    path = _jobs_file(world, [world.job()])
    code = cli_jobs.run(["run", str(path), "--store", str(world.ledger), "--supervisor-budget", "1000"])
    assert code == cli_jobs.EXIT_RUNNER_FAILURE
    assert "--session-root" in capsys.readouterr().err
    assert not world.ledger.exists()


@pytest.mark.usefixtures("isolated")
def test_a_too_small_supervisor_budget_is_refused_before_anything_is_reserved(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    with pytest.raises(SupervisorBudgetError) as caught:
        world.run([world.job(max_attempts=2)], supervisor_budget_s=100.0, timeout_s=60.0)
    assert not world.ledger.exists()
    # attempt = timeout 60 + shutdown allowance (3 + 3 + 2) = 68, two attempts
    assert caught.value.required_s == pytest.approx(136.0)


def test_check_agent_session_run_ignores_a_run_without_agent_jobs(tmp_path):
    plain = Job(id="p", prompt=Prompt(user="u"), models=("m",), directory=tmp_path,
                contract=Contract(command=(sys.executable, "-c", "pass"), directory=tmp_path))
    assert jobs.check_agent_session_run([plain], session_root=None, supervisor_budget_s=None,
                                        timeout_s=10.0, max_parallel=1) is None


def test_check_agent_session_run_budgets_every_job_and_the_pool(tmp_path):
    def job(job_id: str, options: dict, attempts: int) -> Job:
        return Job(id=job_id, prompt=Prompt(user="u"), models=("m",), directory=tmp_path, max_attempts=attempts,
                   options=options, contract=Contract(command=(sys.executable, "-c", "pass"), directory=tmp_path))

    listed = [job("a", {"session": "agent"}, 2), job("c", {}, 1)]
    policy = jobs.BackpressurePolicy(cap_s=5.0)
    required = jobs.check_agent_session_run(listed, session_root=tmp_path, supervisor_budget_s=10_000.0,
                                            timeout_s=100.0, max_parallel=2, backpressure=policy)
    agent = 2 * (100.0 + SessionBounds().shutdown_allowance_s + 5.0)
    completion = 100.0 + 5.0
    assert required == pytest.approx((agent + completion) / 2 + 0.5 * max(agent, completion))
    with pytest.raises(jobs.AgentSessionConfigError, match="absolute"):
        jobs.check_agent_session_run(listed, session_root="relative", supervisor_budget_s=1.0,
                                     timeout_s=1.0, max_parallel=1)


# ---------------------------------------------------------------------------
# the workspace hook, admission and dispatch construction
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated")
def test_the_workspace_hook_is_called_once_per_job_with_the_last_stop_kind(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    calls: list[dict] = []

    class Hook:
        def finalize_run(self, **kwargs: Any) -> WorkspaceFinalizeResult:
            calls.append(kwargs)
            return WorkspaceFinalizeResult(status=WorkspaceFinalizeStatus.NOT_APPLICABLE)

    assert isinstance(Hook(), SessionWorkspaceHook)
    world.run([world.job()], workspace_hook=Hook())
    assert len(calls) == 1
    assert calls[0]["job_id"] == "agent-job" and calls[0]["stop_kind"] is StopKind.QUIESCENT
    assert calls[0]["writers_stopped"] is True and calls[0]["final_state"] == "accepted"
    assert calls[0]["detached_survivors"] == ()
    assert jobs.NoWorkspaceHook().finalize_run(
        run_id="r", job_id="j", working_directory=tmp_path, writers_stopped=True, detached_survivors=(),
        final_state=None, stop_kind=None,
    ) == WorkspaceFinalizeResult(status=WorkspaceFinalizeStatus.NOT_APPLICABLE)
    survivor = DetachedSurvivor(ProcessIdentity(4410, 1794000003.75), "/somewhere")
    held = jobs.NoWorkspaceHook().finalize_run(
        run_id="r", job_id="j", working_directory=tmp_path, writers_stopped=False,
        detached_survivors=(survivor,), final_state="accepted", stop_kind=StopKind.QUIESCENT,
    )
    assert held.status is WorkspaceFinalizeStatus.NOT_APPLICABLE and "pid 4410" in held.detail


@pytest.mark.usefixtures("isolated")
def test_a_detached_survivor_does_not_fail_the_job_but_reaches_the_hook_as_not_quiescent(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "daemon")
    calls: list[dict] = []

    class Hook:
        def finalize_run(self, **kwargs: Any) -> WorkspaceFinalizeResult:
            calls.append(kwargs)
            return WorkspaceFinalizeResult(status=WorkspaceFinalizeStatus.DIRTY_HELD)

    daemon_file = Path(str(world.log) + ".daemon")
    try:
        snapshot = world.run([world.job(max_attempts=2)], workspace_hook=Hook())
        daemon = int(daemon_file.read_text())
        assert not Path(str(world.log) + ".daemon_signal").exists()
        assert snapshot.jobs[0].state is JobState.ACCEPTED  # the result and contract proceed normally
        (attempt,) = snapshot.attempts
        assert attempt.error is None and attempt.acceptance is not None
        (call,) = calls
        assert call["writers_stopped"] is False and call["final_state"] == "accepted"
        assert [item.identity.pid for item in call["detached_survivors"]] == [daemon]
        cleanup = json.loads((world.attempt_dir("agent-job") / "cleanup.json").read_text())
        assert [item["pid"] for item in cleanup["detached_survivors"]] == [daemon]
        assert read_session_record(SessionPaths(world.attempt_dir("agent-job"))).shutdown_confirmed is True
    finally:
        if daemon_file.exists():
            try:
                os.kill(int(daemon_file.read_text()), 9)
            except OSError:
                pass


_KILL_DAEMON_AND_REJECT = (
    "import os, sys, time\n"
    "pid = int(open(sys.argv[1]).read())\n"
    "os.kill(pid, 9)\n"
    "end = time.monotonic() + 10\n"
    "while time.monotonic() < end:\n"
    "    try:\n"
    "        os.kill(pid, 0)\n"
    "    except ProcessLookupError:\n"
    "        break\n"
    "    time.sleep(0.02)\n"
    "sys.exit(1)\n"
)


def _survivor_pids(world: World, job_id: str) -> list[int]:
    pids = []
    for attempt_dir in sorted((world.sessions / encode_job_id(job_id)).glob("attempt-*")):
        cleanup = attempt_dir / "cleanup.json"
        if cleanup.exists():
            pids += [item["pid"] for item in json.loads(cleanup.read_text())["detached_survivors"]]
    return pids


def _kill_all(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _rejecting_job(world: World, directory: Path, *, kill_daemon: bool, isolate: bool = False) -> Job:
    if kill_daemon:
        command = (sys.executable, "-I", "-c", _KILL_DAEMON_AND_REJECT, str(world.log) + ".daemon")
    else:
        command = (sys.executable, "-I", "-c", "import sys; sys.exit(1)")
    return Job(id="agent-job", prompt=Prompt(user="do the work", system="SYS"), models=("h-claude",),
               directory=directory, max_attempts=2, options=dict(FAST),
               workspace=WorkspaceSpec(isolate=True) if isolate else None,
               contract=Contract(command=command, directory=directory))


@pytest.mark.usefixtures("isolated")
def test_a_retry_in_the_same_directory_beside_a_live_survivor_is_refused(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "daemon")
    try:
        snapshot = world.run([_rejecting_job(world, world.work, kill_daemon=False)])
        (survivor,) = _survivor_pids(world, "agent-job")
        (record,) = snapshot.jobs
        assert record.state is JobState.FAILED
        assert record.error.startswith("workspace_not_quiescent:") and f"pid {survivor}" in record.error
        assert len(snapshot.attempts) == 1, "no retry may start beside a live detached survivor"
        assert not world.attempt_dir("agent-job", 2).exists()
        assert JobStore(world.ledger).list_reservations("agent-run", "agent-job")[-1].attempt_no == 1
    finally:
        _kill_all(_survivor_pids(world, "agent-job"))


@pytest.mark.usefixtures("isolated")
def test_a_retry_in_the_same_directory_proceeds_once_every_survivor_is_gone(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "daemon")
    try:
        snapshot = world.run([_rejecting_job(world, world.work, kill_daemon=True)])
        assert len(_survivor_pids(world, "agent-job")) == 2  # each attempt left one, killed by its contract
        assert len(snapshot.attempts) == 2
        assert snapshot.jobs[0].state is JobState.REJECTED  # the attempt budget, not the survivor, ended it
        assert "workspace_not_quiescent" not in (snapshot.jobs[0].error or "")
    finally:
        _kill_all(_survivor_pids(world, "agent-job"))


@pytest.mark.usefixtures("isolated")
def test_a_retry_in_a_fresh_isolated_worktree_is_not_blocked_by_a_survivor(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "daemon")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("x", encoding="ascii")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    try:
        snapshot = world.run([_rejecting_job(world, repo, kill_daemon=False, isolate=True)])
        first, second = snapshot.attempts
        assert first.workspace != second.workspace and second.workspace_status == "isolated"
        assert len(_survivor_pids(world, "agent-job")) == 2
        assert "workspace_not_quiescent" not in (snapshot.jobs[0].error or "")
    finally:
        _kill_all(_survivor_pids(world, "agent-job"))


def test_dispatch_builds_the_session_only_from_a_tool_bearing_claude_backend():
    selection = BackendSelection("h", "harness", ClaudeCliBackend(executable="/x/claude"), "haiku")
    session = create_agent_session(selection)
    assert isinstance(session, ClaudeAgentSession) and session.executable == "/x/claude"
    with pytest.raises(AgentSessionUnsupportedError, match="text-only"):
        create_agent_session(BackendSelection("h", "harness", ClaudeCliBackend(text_only=True), "haiku"))
    with pytest.raises(AgentSessionUnsupportedError, match="not the Claude CLI"):
        create_agent_session(BackendSelection("h", "harness", object(), "haiku"))  # type: ignore[arg-type]
    assert agent_session_capabilities() is CLAUDE_AGENT_CAPABILITIES


def test_the_session_table_has_only_the_agent_record():
    table = jobs_select.session_capabilities()
    assert table == {"claude-cli": CLAUDE_AGENT_CAPABILITIES}
    controls = {control.id for control in CLAUDE_AGENT_CAPABILITIES.execution_controls}
    assert "no-session-persistence" not in controls and "text-only-mode" not in controls


# ---------------------------------------------------------------------------
# the public surface
# ---------------------------------------------------------------------------

#: Module -> its public names: ``__all__``, or for claude_stream (no ``__all__``) the names its module
#: docstring declares public.
_SURFACE_MODULES = {
    "llm_kit.completion.session_types": None,
    "llm_kit.adapters.claude_stream": ("StreamVerdict", "init_problem", "parse_stream_line"),
    "llm_kit.adapters.claude_session": None,
    "llm_kit.jobs.session": None,
    "llm_kit.storage.file_lock": None,
    "llm_kit.jobs.run": None,
}


@pytest.mark.parametrize("module_name", sorted(_SURFACE_MODULES))
def test_public_surface_lists_the_agent_session_names(module_name):
    surface = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))
    assert module_name in surface["covers"]
    names = {e["name"] for e in surface["entries"] if e["status"] == "public"}
    module = __import__(module_name, fromlist=["__all__"])
    public = _SURFACE_MODULES[module_name] or tuple(module.__all__)
    listed = {n.rsplit(".", 1)[1] for n in names if n.rsplit(".", 1)[0] == module_name}
    assert listed == set(public)
    for name in public:
        assert hasattr(module, name), name


def test_the_new_run_names_are_exported():
    for name in ("AgentSessionConfigError", "NoWorkspaceHook", "check_agent_session_run"):
        assert name in jobs.__all__ and getattr(jobs, name).__doc__
    for name in ("select_session_endpoint_with_readings", "session_capabilities"):
        assert name in jobs_select.__all__ and getattr(jobs_select, name).__doc__
