"""Branch-mode jobs end to end: agent sessions in a unit worktree and the end-of-run commit.

The sessions run the fake ``claude`` of ``fixtures/fake_claude_session.py`` through the real
:class:`~llm_kit.adapters.claude_session.ClaudeAgentSession` (as ``test_jobs_agent_session.py``
does), with ``FAKE_SESSION_WRITE`` making the agent leave an uncommitted file in its worktree. The
repository is real Git with an ``origin/dev`` merge target (``unit_repos``). Every exit path of a
run (accepted, rejected, a contract exception, Ctrl-C, a hard cancel) must leave the change
committed on the unit branch, or the unit held when the commit is refused or a writer survives.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.capabilities import CLAUDE_CAPABILITIES
from llm_kit.cli import jobs as cli_jobs
from llm_kit.completion.session_types import SessionMode, StopKind
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.jobs import run as jobs
from llm_kit.jobs.model import Contract, Job, JobState, Prompt, WorkspaceSpec
from llm_kit.jobs.store import JobStore
from llm_kit.jobs.units import UnitFinalizeStatus, UnitRegistry, UnitState, finalize_unit, repository_identity
from llm_kit.jobs.workspace import unit_worktree_path
from unit_repos import Repo, git, hermetic_git, make_repo  # noqa: F401 -- hermetic_git is a fixture

pytestmark = [
    pytest.mark.usefixtures("hermetic_git", "isolated"),
    pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a POSIX shell wrapper"),
]

FAKE = Path(__file__).parent / "fixtures" / "fake_claude_session.py"
SRC = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src"
CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "katsoft-contracts" / "src"
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


class World:
    """One repository, one unit root, the fake CLI and a ledger."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str = "basic") -> None:
        self.tmp = tmp_path
        self.repo: Repo = make_repo(tmp_path)
        self.cli = tmp_path / "fake-claude"
        self.cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="ascii")
        self.cli.chmod(0o755)
        self.log = tmp_path / "fake"
        monkeypatch.setenv("FAKE_SESSION_SCENARIO", scenario)
        monkeypatch.setenv("FAKE_SESSION_LOG", str(self.log))
        monkeypatch.setenv("FAKE_SESSION_WRITE", "notes.txt")
        self.units = tmp_path / "units"
        self.ledger = tmp_path / "ledger.sqlite3"
        self.sessions = tmp_path / "sessions"
        self.repository = repository_identity(self.repo.root)
        self.path = unit_worktree_path(self.units, self.repository, "item-1")

    def factory(self, endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", ClaudeCliBackend(executable=str(self.cli)), "haiku",
                                capabilities=CLAUDE_CAPABILITIES)

    def job(self, *, check: str = "echo: do the work", max_attempts: int = 1) -> Job:
        command = (sys.executable, "-I", "-c", f"import sys; sys.exit(0 if sys.stdin.read() == {check!r} else 1)")
        return Job(
            id="item-job", prompt=Prompt(user="do the work"), models=("h-claude",), directory=self.repo.root,
            max_attempts=max_attempts, options=dict(FAST),
            contract=Contract(command=command, directory=self.repo.root),
            workspace=WorkspaceSpec(mode="branch", unit="item-1", root=self.units, base_ref="origin/dev"),
        )

    def run(self, job: Optional[Job] = None, *, run_id: str = "run-1"):
        # The session root is run-specific (design 11.1).
        return jobs.run_jobs([job or self.job()], self.ledger, run_id=run_id, backend_factory=self.factory,
                             session_root=self.sessions / run_id, supervisor_budget_s=BUDGET,
                             workspace_root=self.tmp / "detached")

    def unit(self):
        record = UnitRegistry(self.units).get(self.repository, "item-1")
        assert record is not None
        return record


def _assert_committed(world: World, run_no: int = 1) -> None:
    assert world.repo.log("agent/item-1")[0] == f"WIP: item-1 run {run_no}"
    assert world.repo.show("agent/item-1", "notes.txt") == "do the work"
    assert git("status", "--porcelain", "--untracked-files=all", cwd=world.path) == ""
    record = world.unit()
    assert record.state is UnitState.PARKED and record.owner is None
    assert record.last_commit_sha == world.repo.head("agent/item-1")


# ---------------------------------------------------------------------------
# the integration test: an agent session in a branch worktree
# ---------------------------------------------------------------------------


def test_an_agent_session_runs_in_its_unit_worktree_and_its_work_is_committed(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    snapshot = world.run()
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    (attempt,) = snapshot.attempts
    assert attempt.session_mode is SessionMode.AGENT and attempt.stop_kind is StopKind.QUIESCENT
    assert attempt.workspace_status == "unit" and attempt.workspace == world.path
    assert attempt.base_ref == world.repo.head("origin/dev")
    assert os.path.realpath(attempt.session_cwd) == str(world.path)
    assert json.loads(Path(str(world.log) + ".argv.json").read_text())["cwd"] == str(world.path)
    (reservation,) = JobStore(world.ledger).list_reservations("run-1", "item-job")
    assert reservation.workspace_status == "unit" and reservation.workspace_path == world.path
    _assert_committed(world)
    assert git("symbolic-ref", "HEAD", cwd=world.path) == "refs/heads/agent/item-1"
    assert world.repo.head("dev") == world.repo.head("origin/dev")  # nothing merged, nothing pushed

    # The next run of the unit continues the same branch in the same worktree.
    (world.path / "notes.txt").unlink()
    git("commit", "-q", "-am", "drop notes", cwd=world.path)
    again = world.run(run_id="run-2")
    assert again.jobs[0].state is JobState.ACCEPTED and again.attempts[0].workspace == world.path
    _assert_committed(world, run_no=2)
    assert world.unit().run_count == 2


# ---------------------------------------------------------------------------
# every exit path commits
# ---------------------------------------------------------------------------


def test_a_rejected_run_still_commits_its_work(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    snapshot = world.run(world.job(check="something else"))
    assert snapshot.jobs[0].state is JobState.REJECTED
    _assert_committed(world)


def test_a_contract_exception_still_commits_its_work(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)

    def broken(*_args: object, **_kwargs: object):
        raise RuntimeError("contract runner broke")

    monkeypatch.setattr(jobs, "run_contract", broken)
    snapshot = world.run()
    assert snapshot.jobs[0].state is JobState.FAILED
    assert snapshot.attempts[0].error_code == "contract"
    assert snapshot.attempts[0].error_message == "contract runner broke"
    _assert_committed(world)


def test_ctrl_c_in_the_contract_still_commits_its_work(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)

    def interrupted(*_args: object, **_kwargs: object):
        raise KeyboardInterrupt

    monkeypatch.setattr(jobs, "run_contract", interrupted)
    with pytest.raises(KeyboardInterrupt):
        world.run()
    _assert_committed(world)


def test_a_rejecting_hook_holds_the_unit_and_the_next_run_is_refused(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    hook = world.repo.common_dir / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'no WIP commits here' >&2\nexit 1\n", encoding="ascii")
    hook.chmod(0o755)
    seen: list = []
    real = jobs.BranchWorkspaceHook.finalize_run

    def spy(self, **kwargs):
        seen.append(real(self, **kwargs))
        return seen[-1]

    monkeypatch.setattr(jobs.BranchWorkspaceHook, "finalize_run", spy)
    assert world.run().jobs[0].state is JobState.ACCEPTED
    assert seen[0].status.value == "dirty_held" and "no WIP commits here" in seen[0].detail
    assert world.unit().state is UnitState.DIRTY_HELD
    assert world.repo.log("agent/item-1") == ["seed"]
    refused = world.run(run_id="run-2")
    assert refused.jobs[0].state is JobState.FAILED
    assert "dirty_held" in (refused.jobs[0].error or "")
    assert refused.attempts == ()


def test_a_detached_survivor_holds_the_unit_until_it_exits(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, "daemon")
    monkeypatch.setenv("FAKE_DAEMON_S", "4")
    snapshot = world.run()
    assert snapshot.jobs[0].state is JobState.ACCEPTED  # survivors do not fail the run
    record = world.unit()
    assert record.state is UnitState.WRITER_HELD and record.held_processes
    daemon_pid = int(Path(str(world.log) + ".daemon").read_text())
    assert daemon_pid in {item.identity.pid for item in record.held_processes}
    assert world.repo.log("agent/item-1") == ["seed"]  # nothing committed beside a live writer
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = finalize_unit(UnitRegistry(world.units), world.repository, "item-1")
        if result.status is not UnitFinalizeStatus.WRITER_HELD:
            break
        time.sleep(0.2)
    assert result.status is UnitFinalizeStatus.COMMITTED
    _assert_committed(world)


# ---------------------------------------------------------------------------
# a hard cancel: the runner and the session are killed, then `jobs cancel` commits
# ---------------------------------------------------------------------------


def _runner_script(world: World) -> Path:
    script = world.tmp / "runner.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path[:0] = [{str(SRC)!r}, {str(CONTRACTS)!r}]
        from pathlib import Path
        import llm_kit.dispatch.declaration as declaration
        import llm_kit.dispatch.usage_budget as usage
        import llm_kit.registry.models as models
        from llm_kit.adapters.backends import ClaudeCliBackend
        from llm_kit.adapters.capabilities import CLAUDE_CAPABILITIES
        from llm_kit.dispatch.factory import BackendSelection
        from llm_kit.jobs import run as jobs
        from llm_kit.jobs.model import Contract, Job, Prompt, WorkspaceSpec
        models.discover_model_entries = lambda **_k: {{}}
        declaration.check_many = lambda *_a, **_k: {{}}
        usage.record_observed_halt = lambda *_a, **_k: None
        jobs.BackpressurePolicy.from_environment = staticmethod(lambda: jobs.BackpressurePolicy(cap_s=0.0))
        def factory(endpoint, **_k):
            return BackendSelection(endpoint, "harness", ClaudeCliBackend(executable={str(world.cli)!r}),
                                    "haiku", capabilities=CLAUDE_CAPABILITIES)
        job = Job(id="item-job", prompt=Prompt(user="do the work"), models=("h-claude",),
                  directory=Path({str(world.repo.root)!r}), options={FAST!r},
                  contract=Contract(command=("true",), directory=Path({str(world.repo.root)!r})),
                  workspace=WorkspaceSpec(mode="branch", unit="item-1", root=Path({str(world.units)!r}),
                                          base_ref="origin/dev"))
        jobs.run_jobs([job], Path({str(world.ledger)!r}), run_id="run-1", backend_factory=factory,
                      session_root=Path({str(world.sessions / "run-1")!r}), supervisor_budget_s={BUDGET!r},
                      workspace_root=Path({str(world.tmp / "detached")!r}))
    """), encoding="utf-8")
    return script


def test_jobs_cancel_commits_after_a_hard_cancel(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, "busy")
    runner = subprocess.Popen([sys.executable, str(_runner_script(world))], start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    notes = world.path / "notes.txt"
    argv_log = Path(str(world.log) + ".argv.json")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (notes.exists() and argv_log.exists()):
        assert runner.poll() is None, runner.stderr.read() if runner.stderr else ""
        time.sleep(0.05)
    assert notes.exists(), "the agent never wrote its file"
    claude_pid = json.loads(argv_log.read_text())["pid"]
    # The hard cancel: the supervisor's process-tree kill reaches the runner and the session.
    os.killpg(runner.pid, signal.SIGKILL)
    os.killpg(os.getpgid(claude_pid), signal.SIGKILL)
    runner.wait(timeout=30)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(claude_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    assert world.unit().state is UnitState.ACTIVE  # no runner `finally` ran

    code = cli_jobs.run(["cancel", "run-1", "--reason", "hard cancel", "--store", str(world.ledger)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"][0]["outcome"] == "cancelled"
    assert payload["units"] == [
        {"job_id": "item-job", "unit": "item-1", "outcome": "committed", "state": "parked", "detail": None}
    ]
    _assert_committed(world)


# ---------------------------------------------------------------------------
# a continuation of a branch-mode job whose plan fails never runs unleased
# ---------------------------------------------------------------------------


def test_a_continuation_whose_branch_plan_fails_fails_the_job_without_running_the_contract(tmp_path):
    from test_jobs_cancelled_state import _attempt, _request

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    marker = tmp_path / "contract-ran"
    job = Job(
        id="job", prompt=Prompt(user="work"), models=("fake",), directory=plain, max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"), directory=plain),
        workspace=WorkspaceSpec(mode="branch", unit="item-1", root=tmp_path / "units", base_ref="origin/dev"),
    )
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [job], workspace_root=tmp_path / "ws")
    reservation = store.reserve_attempt("run", "job", endpoint="fake-endpoint", backend="fake-backend",
                                        model="fake-model", reserved_at="2026-09-01T00:00:00Z")
    store.arm_reservation("run", "job", reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z")
    store.append_attempt(_attempt("job", reservation.attempt_no), interrupt=_request())
    record = store.open_interrupt("run", "job")
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True})
    assert store.get_job("run", "job").state is JobState.WAITING

    snapshot = jobs.resume_run("run", store)
    (final,) = snapshot.jobs
    assert final.state is JobState.FAILED and "needs a Git repository" in (final.error or "")
    assert not marker.exists(), "the contract continuation must not run without the unit lease"
    assert store.list_continuations("run", "job") == []

