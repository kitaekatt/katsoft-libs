"""``llm_kit.jobs.workspace``: detached isolation, branch-mode unit worktrees, crash recovery and GC.

Real Git in temporary directories (``unit_repos``). The branch-mode tests drive
:class:`WorkspaceManager` directly: create, reuse and rehydrate at the fixed path; the base resolved
against the REMOTE merge target; the interprocess lock around ``git worktree add``. The crash-path
tests leave a unit owned by a process that died (the shape an unexpected exit or a hard cancel
leaves) and finalize it through ``llm-kit jobs cancel``, a resume and :func:`recover_run_units`.
GC is checked never to remove a unit worktree, whatever an old attempt row says.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from llm_kit.cli import jobs as cli_jobs
from llm_kit.completion.session_types import (
    CleanupRecord,
    ProcessIdentity,
    SessionPaths,
    SessionRecord,
)
from llm_kit.jobs import run as jobs_run
from llm_kit.jobs import workspace as ws
from llm_kit.jobs.model import Attempt, Contract, Job, JobState, Prompt, WorkspaceSpec
from llm_kit.jobs.session import FileSessionChannel, create_attempt_dir, write_session_record
from llm_kit.jobs.store import JobStore
from llm_kit.jobs.units import (
    UnitRegistry,
    UnitState,
    remove_unit_worktree,
    repository_identity,
    repository_lock,
)
from llm_kit.jobs.workspace import (
    WorkspaceCreationError,
    WorkspaceManager,
    capture_base_refs,
    create_worktree,
    detect_repo_root,
    gc_workspaces,
    prepare_workspace,
    recover_run_units,
    resolve_base_ref,
    resolve_unit_base_ref,
    unit_worktree_path,
)
from unit_repos import Repo, git, hermetic_git, make_repo  # noqa: F401 -- hermetic_git is a fixture

pytestmark = [
    pytest.mark.usefixtures("hermetic_git"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX process helpers"),
]

SRC = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src"
CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "katsoft-contracts" / "src"
ENDPOINT = dict(endpoint="fake-endpoint", backend="fake-backend", model="fake-model")


def _job(repo: Repo, job_id: str = "job", *, unit: str = "u", root: Path, directory: Path | None = None,
         **spec: object) -> Job:
    where = directory or repo.root
    workspace = WorkspaceSpec(mode="branch", unit=unit, root=root,
                              base_ref=str(spec.pop("base_ref", "origin/dev")), **spec)  # type: ignore[arg-type]
    return Job(id=job_id, prompt=Prompt(user="work"), models=("fake",), directory=where,
               contract=Contract(command=(sys.executable, "-c", "pass"), directory=where),
               workspace=workspace)


def _isolated_job(repo: Repo, job_id: str = "iso", directory: Path | None = None) -> Job:
    where = directory or repo.root
    return Job(id=job_id, prompt=Prompt(user="work"), models=("fake",), directory=where,
               contract=Contract(command=(sys.executable, "-c", "pass"), directory=where),
               workspace=WorkspaceSpec(isolate=True))


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return make_repo(tmp_path)


def _registry(root: Path) -> UnitRegistry:
    return UnitRegistry(root)


# ---------------------------------------------------------------------------
# repository facts and base refs
# ---------------------------------------------------------------------------


def test_detect_repo_root_finds_the_root_or_none(repo, tmp_path):
    assert detect_repo_root(repo.root / "pkg") == repo.root
    plain = tmp_path / "plain"
    plain.mkdir()
    assert detect_repo_root(plain) is None


def test_resolve_base_ref_observes_head_or_a_requested_ref(repo):
    assert resolve_base_ref(repo.root) == repo.head()
    assert resolve_base_ref(repo.root, "origin/dev") == repo.head("origin/dev")
    with pytest.raises(WorkspaceCreationError, match="must not be empty"):
        resolve_base_ref(repo.root, "  ")
    with pytest.raises(WorkspaceCreationError, match="could not resolve"):
        resolve_base_ref(repo.root, "no-such-ref")


def test_resolve_unit_base_ref_uses_the_remote_merge_target_never_the_checkout(repo):
    (repo.root / "local.txt").write_text("unpushed\n", encoding="ascii")
    git("add", "local.txt", cwd=repo.root)
    git("commit", "-q", "-m", "local only", cwd=repo.root)
    remote_tip = repo.head("origin/dev")
    assert repo.head("dev") != remote_tip
    assert resolve_unit_base_ref(repo.root, "origin/dev") == remote_tip
    assert resolve_unit_base_ref(repo.root, remote_tip) == remote_tip
    for local in ("dev", "HEAD", remote_tip[:12]):
        with pytest.raises(WorkspaceCreationError, match="remote-tracking branch"):
            resolve_unit_base_ref(repo.root, local)


def test_unit_worktree_path_is_fixed_and_repository_specific(repo, tmp_path):
    identity = repository_identity(repo.root)
    path = unit_worktree_path(tmp_path / "wt", identity, "item-1")
    assert path == unit_worktree_path(tmp_path / "wt", identity, "item-1")
    assert path.parent.parent == (tmp_path / "wt").resolve() and path.name == "item-1"
    assert path.parent.name.startswith("project-")
    (tmp_path / "elsewhere").mkdir()
    other = make_repo(tmp_path / "elsewhere", name="project")
    assert unit_worktree_path(tmp_path / "wt", repository_identity(other.root), "item-1") != path


# ---------------------------------------------------------------------------
# detached isolation
# ---------------------------------------------------------------------------


def test_create_worktree_makes_a_detached_worktree_and_refuses_an_existing_path(repo, tmp_path):
    path = create_worktree(repo.root, tmp_path / "ws" / "one", repo.head())
    assert git("rev-parse", "HEAD", cwd=path) == repo.head()
    assert git("symbolic-ref", "-q", "HEAD", cwd=path, check=False) == ""
    with pytest.raises(WorkspaceCreationError, match="already exists"):
        create_worktree(repo.root, path, repo.head())


def test_an_isolated_manager_gives_each_attempt_a_fresh_worktree(repo, tmp_path):
    job = _isolated_job(repo, directory=repo.root / "pkg")
    manager = WorkspaceManager(tmp_path / "ws", (job,))
    first = manager.prepare(job, 1)
    second = manager.prepare(job, 2)
    assert first.status == second.status == "isolated" and first.path != second.path
    assert first.working_directory == (first.path / "pkg").resolve()
    assert manager.working_directory(job, first.path) == first.working_directory
    assert manager.working_directory(job, None) == (repo.root / "pkg").resolve()
    assert manager.base_refs == {"iso": repo.head()} == capture_base_refs((job,))
    assert not manager.is_unit_job("iso")


def test_prepare_workspace_declines_or_reports_none(repo, tmp_path):
    declined = Job(id="d", prompt=Prompt(user="x"), models=("fake",), directory=repo.root,
                   contract=Contract(command=("true",)))
    assert prepare_workspace(declined, tmp_path / "ws").status == "none"
    plain = tmp_path / "plain"
    plain.mkdir()
    resolution = prepare_workspace(_isolated_job(repo, directory=plain), tmp_path / "ws")
    assert resolution.status == "none" and resolution.reason == ws.WORKSPACE_REASON_NONE
    assert prepare_workspace(_isolated_job(repo), tmp_path / "ws").status == "isolated"
    with pytest.raises(WorkspaceCreationError, match="does not finalize"):
        prepare_workspace(_job(repo, root=tmp_path / "units"), tmp_path / "ws")


# ---------------------------------------------------------------------------
# branch mode: create, reuse, rehydrate
# ---------------------------------------------------------------------------


def _manager(job: Job, run_id: str) -> WorkspaceManager:
    return WorkspaceManager(Path("/nonexistent-detached-root"), (job,), run_id=run_id)


def test_branch_mode_creates_reuses_and_rehydrates_at_the_same_path(repo, tmp_path):
    (repo.root / "local.txt").write_text("unpushed\n", encoding="ascii")
    git("add", "local.txt", cwd=repo.root)
    git("commit", "-q", "-m", "local only", cwd=repo.root)
    root = tmp_path / "units"
    job = _job(repo, root=root)
    identity = repository_identity(repo.root)
    fixed = unit_worktree_path(root, identity, "u")

    manager = _manager(job, "run-1")
    assert manager.is_unit_job("job") and manager.allocate_path(job, 1) == manager.allocate_path(job, 7) == fixed
    first = manager.prepare(job, 1)
    assert first.status == ws.WORKSPACE_STATUS_UNIT == "unit"
    assert (first.path, first.unit, first.branch) == (fixed, "u", "agent/u")
    assert first.base_ref == repo.head("origin/dev") == repo.head("agent/u")  # not the local checkout
    assert manager.base_refs == {"job": repo.head("origin/dev")}
    (fixed / "result.txt").write_text("run one\n", encoding="ascii")
    assert manager.prepare(job, 2).path == fixed  # a retry reuses the owned worktree
    done = manager.finalize_unit_run("job", writers_stopped=True)
    assert done is not None and done.status.value == "committed"
    assert manager.finalize_unit_run("job", writers_stopped=True) is None

    second = _manager(job, "run-2").prepare(job, 1)
    assert second.path == fixed and (fixed / "result.txt").read_text() == "run one\n"
    registry = _registry(root)
    assert registry.get(identity, "u").run_count == 2  # type: ignore[union-attr]

    manager3 = _manager(job, "run-2b")
    with pytest.raises(WorkspaceCreationError, match="unit 'u': unit 'u' is active, owned by run 'run-2'"):
        manager3.prepare(job, 1)


def test_a_removed_unit_is_rehydrated_from_its_branch_at_the_same_path(repo, tmp_path):
    root = tmp_path / "units"
    job = _job(repo, root=root)
    manager = _manager(job, "run-1")
    path = manager.prepare(job, 1).path
    (path / "kept.txt").write_text("kept\n", encoding="ascii")
    tip = manager.finalize_unit_run("job", writers_stopped=True).commit_sha  # type: ignore[union-attr]
    remove_unit_worktree(_registry(root), repository_identity(repo.root), "u")
    assert not path.exists()
    again = _manager(job, "run-2").prepare(job, 1)
    assert again.path == path and again.base_ref == tip
    assert (path / "kept.txt").read_text() == "kept\n"
    assert git("symbolic-ref", "HEAD", cwd=path) == "refs/heads/agent/u"


def test_a_deleted_worktree_directory_is_pruned_and_rehydrated(repo, tmp_path):
    root = tmp_path / "units"
    job = _job(repo, root=root)
    manager = _manager(job, "run-1")
    path = manager.prepare(job, 1).path
    manager.finalize_unit_run("job", writers_stopped=True)
    shutil.rmtree(path)
    assert _manager(job, "run-2").prepare(job, 1).path == path and path.is_dir()


def test_a_repository_subdirectory_job_runs_in_the_worktree_subdirectory(repo, tmp_path):
    job = _job(repo, root=tmp_path / "units", directory=repo.root / "pkg")
    manager = _manager(job, "run-1")
    resolution = manager.prepare(job, 1)
    assert resolution.working_directory == (resolution.path / "pkg").resolve()
    assert manager.working_directory(job, resolution.path) == resolution.working_directory


def test_branch_mode_plan_failures_surface_at_allocation(repo, tmp_path):
    local_only = _job(repo, root=tmp_path / "units", base_ref="dev")
    with pytest.raises(WorkspaceCreationError, match="neither a remote-tracking branch"):
        _manager(local_only, "r").allocate_path(local_only, 1)
    bad_branch = _job(repo, root=tmp_path / "units", branch="agent/bad..name")
    with pytest.raises(WorkspaceCreationError, match="invalid unit branch name"):
        _manager(bad_branch, "r").allocate_path(bad_branch, 1)
    plain = tmp_path / "plain"
    plain.mkdir()
    outside = _job(repo, root=tmp_path / "units", directory=plain)
    with pytest.raises(WorkspaceCreationError, match="needs a Git repository"):
        _manager(outside, "r").allocate_path(outside, 1)
    good = _job(repo, root=tmp_path / "units")
    with pytest.raises(WorkspaceCreationError, match="needs the manager's run_id"):
        WorkspaceManager(tmp_path / "ws", (good,)).prepare(good, 1)


def test_branch_mode_refuses_a_foreign_directory_and_a_wrong_branch_at_the_path(repo, tmp_path):
    root = tmp_path / "units"
    job = _job(repo, root=root)
    path = unit_worktree_path(root, repository_identity(repo.root), "u")
    path.mkdir(parents=True)
    manager = _manager(job, "r")
    with pytest.raises(WorkspaceCreationError, match="exists and is not a registered worktree"):
        manager.prepare(job, 1)
    ended = manager.finalize_unit_run("job", writers_stopped=True)  # the run's hook, after the failure
    assert ended is not None and ended.status.value == "clean" and "no worktree" in (ended.detail or "")
    path.rmdir()
    git("worktree", "add", "-q", "-b", "agent/other", str(path), "origin/dev", cwd=repo.root)
    with pytest.raises(WorkspaceCreationError, match="is on refs/heads/agent/other, not the recorded branch"):
        _manager(job, "r2").prepare(job, 1)


def test_hold_owns_a_unit_for_a_continuation_and_ignores_other_jobs(repo, tmp_path):
    job = _job(repo, root=tmp_path / "units")
    manager = _manager(job, "run-1")
    path = manager.hold(job)
    assert path is not None and path.is_dir()
    record = _registry(tmp_path / "units").get(repository_identity(repo.root), "u")
    assert record is not None and record.state is UnitState.ACTIVE
    assert manager.prepare(job, 1).path == path  # the same lease
    assert WorkspaceManager(tmp_path / "ws", (_isolated_job(repo),)).hold(_isolated_job(repo)) is None


def test_hold_refuses_a_branch_mode_job_whose_plan_failed(repo, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    job = _job(repo, root=tmp_path / "units", directory=plain)
    with pytest.raises(WorkspaceCreationError, match="needs a Git repository"):
        _manager(job, "run-1").hold(job)


# ---------------------------------------------------------------------------
# the interprocess lock around worktree creation
# ---------------------------------------------------------------------------


def _prepare_script(tmp_path: Path, repo: Repo, root: Path, unit: str, run_id: str, name: str) -> Path:
    path = tmp_path / "scripts" / f"{name}.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(textwrap.dedent(f"""
        import sys
        sys.path[:0] = [{str(SRC)!r}, {str(CONTRACTS)!r}]
        from pathlib import Path
        from llm_kit.jobs.model import Contract, Job, Prompt, WorkspaceSpec
        from llm_kit.jobs.workspace import WorkspaceManager
        job = Job(id="job", prompt=Prompt(user="w"), models=("fake",), directory=Path({str(repo.root)!r}),
                  contract=Contract(command=("true",)),
                  workspace=WorkspaceSpec(mode="branch", unit={unit!r}, root=Path({str(root)!r}),
                                          base_ref="origin/dev"))
        manager = WorkspaceManager(Path("/nonexistent"), (job,), run_id={run_id!r})
        go = Path({str(tmp_path / "go")!r})
        while not go.exists():
            pass
        print(manager.prepare(job, 1).path)
    """), encoding="utf-8")
    return path


def test_worktree_creation_waits_for_the_repository_lock(repo, tmp_path):
    root = tmp_path / "units"
    (tmp_path / "go").write_text("", encoding="ascii")
    script = _prepare_script(tmp_path, repo, root, "u", "run-1", "blocked")
    target = unit_worktree_path(root, repository_identity(repo.root), "u")
    with repository_lock(repository_identity(repo.root)):
        child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert child.poll() is None and not target.exists()
    out, _ = child.communicate(timeout=60)
    assert child.returncode == 0 and Path(out.strip()) == target and target.is_dir()


def test_two_processes_adding_unit_worktrees_at_once_both_succeed(repo, tmp_path):
    root = tmp_path / "units"
    scripts = [_prepare_script(tmp_path, repo, root, f"u{n}", f"run-{n}", f"racer{n}") for n in range(4)]
    children = [subprocess.Popen([sys.executable, str(s)], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for s in scripts]
    time.sleep(0.5)
    (tmp_path / "go").write_text("", encoding="ascii")
    results = [child.communicate(timeout=120) for child in children]
    assert [child.returncode for child in children] == [0, 0, 0, 0], [err for _, err in results]
    paths = {Path(out.strip()) for out, _ in results}
    assert len(paths) == 4 and all(path.is_dir() for path in paths)
    listed = git("worktree", "list", "--porcelain", cwd=repo.root)
    assert all(f"branch refs/heads/agent/u{n}" in listed for n in range(4))


# ---------------------------------------------------------------------------
# the crash path: a unit owned by a run whose process died
# ---------------------------------------------------------------------------


class CrashedRun:
    """A ledger run with one branch-mode job whose runner died while owning the unit."""

    def __init__(self, tmp_path: Path, repo: Repo, *, terminal: bool = False) -> None:
        self.repo = repo
        self.root = tmp_path / "units"
        self.ledger = tmp_path / "ledger.sqlite3"
        self.job = _job(repo, root=self.root)
        self.store = JobStore(self.ledger)
        self.store.create_run("run", (self.job,), workspace_root=tmp_path / "ws")
        self.path = unit_worktree_path(self.root, repository_identity(repo.root), "u")
        if terminal:
            self.store.mark_failed("run", "job", "the contract already ended it")
        script = tmp_path / "scripts" / "crash.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text(textwrap.dedent(f"""
            import os, sys
            sys.path[:0] = [{str(SRC)!r}, {str(CONTRACTS)!r}]
            from pathlib import Path
            from llm_kit.jobs.store import JobStore
            from llm_kit.jobs.workspace import WorkspaceManager
            store = JobStore(Path({str(self.ledger)!r}))
            job = store.list_jobs("run")[0].job
            manager = WorkspaceManager(Path("/nonexistent"), (job,), run_id="run")
            path = manager.prepare(job, 1).path
            (path / "uncommitted.txt").write_text("written before the crash\\n")
            os._exit(137)
        """), encoding="utf-8")
        completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
        assert completed.returncode == 137, completed.stderr

    def unit(self):
        return UnitRegistry(self.root).get(repository_identity(self.repo.root), "u")

    def cancel(self, capsys) -> dict:
        code = cli_jobs.run(["cancel", "run", "--reason", "hard cancel", "--store", str(self.ledger)])
        assert code == 0
        return json.loads(capsys.readouterr().out)


def test_jobs_cancel_commits_a_unit_left_by_a_hard_cancel(repo, tmp_path, capsys):
    crashed = CrashedRun(tmp_path, repo)
    assert crashed.unit().state is UnitState.ACTIVE
    payload = crashed.cancel(capsys)
    assert payload["jobs"] == [{"id": "job", "outcome": "cancelled", "state": "cancelled"}]
    (entry,) = payload["units"]
    assert (entry["job_id"], entry["unit"], entry["outcome"], entry["state"]) == ("job", "u", "committed", "parked")
    assert repo.log("agent/u")[0] == "WIP: u run 1"
    assert repo.show("agent/u", "uncommitted.txt") == "written before the crash"
    again = crashed.cancel(capsys)  # a repeated cancel is safe
    assert again["units"][0]["outcome"] == "skipped" and again["units"][0]["state"] == "parked"


def test_an_unexpected_exit_of_a_terminal_job_is_finalized_by_cancel(repo, tmp_path, capsys):
    crashed = CrashedRun(tmp_path, repo, terminal=True)
    payload = crashed.cancel(capsys)
    assert payload["jobs"][0]["outcome"] == "already-terminal"
    assert payload["units"][0]["outcome"] == "committed"
    assert crashed.unit().state is UnitState.PARKED


def test_a_resume_finalizes_a_crashed_unit_before_dispatch(repo, tmp_path):
    crashed = CrashedRun(tmp_path, repo, terminal=True)
    snapshot = jobs_run.resume_run("run", crashed.ledger)
    assert snapshot.jobs[0].state is JobState.FAILED
    assert crashed.unit().state is UnitState.PARKED
    assert repo.log("agent/u")[0] == "WIP: u run 1"


def test_recovery_beside_a_surviving_session_process_keeps_the_unit_active(repo, tmp_path):
    crashed = CrashedRun(tmp_path, repo)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        import psutil

        member = ProcessIdentity(sleeper.pid, psutil.Process(sleeper.pid).create_time())
        paths = SessionPaths(tmp_path / "sessions" / "job" / "attempt-1")
        create_attempt_dir(paths)
        tag = "lks_0123456789abcdef"
        session_id = "0b9f3c3e-8a51-4a0e-9a4f-3d2f7c1e5b10"
        write_session_record(paths, SessionRecord(
            run_id="run", job_id="job", attempt_no=1, session_id=session_id, resumed=False,
            cwd=crashed.path, process_tag=tag, created_at="2026-11-14T10:00:00Z"))
        FileSessionChannel(paths).record_cleanup(CleanupRecord(
            tag=tag, leader=ProcessIdentity(1, 1.0), started_at="2026-11-14T10:00:01Z",
            updated_at="2026-11-14T10:00:01Z", members=(member,)))
        # The crashed attempt's reservation names the session directory holding cleanup.json.
        crashed.store.reserve_attempt(
            "run", "job", reserved_at="2026-11-14T10:00:00Z", session_mode="agent", session_id=session_id,
            session_cwd=crashed.path, session_dir=paths.attempt_dir, **ENDPOINT)
        (entry,) = recover_run_units(crashed.store, "run")
        assert entry.outcome == "indeterminate" and entry.state == "active"
        assert f"pid {sleeper.pid}" in (entry.detail or "")
        record = crashed.unit()
        assert record.state is UnitState.ACTIVE and record.indeterminate
        with pytest.raises(WorkspaceCreationError, match="indeterminate"):
            _manager(crashed.job, "run-2").prepare(crashed.job, 1)
    finally:
        sleeper.kill()
        sleeper.wait()
    (entry,) = recover_run_units(crashed.store, "run")
    assert entry.outcome == "committed" and entry.to_mapping()["state"] == "parked"


def test_recovery_skips_units_of_other_runs_and_jobs_without_a_registry(repo, tmp_path):
    store = JobStore(tmp_path / "ledger.sqlite3")
    job = _job(repo, root=tmp_path / "units")
    store.create_run("run", (job, _isolated_job(repo)), workspace_root=tmp_path / "ws")
    (entry,) = recover_run_units(store, "run")
    assert (entry.outcome, entry.state) == ("skipped", None) and "no unit registry" in (entry.detail or "")
    other = _manager(job, "other-run")
    other.prepare(job, 1)
    (entry,) = recover_run_units(store, "run")
    assert entry.outcome == "skipped" and entry.detail == "owned by run 'other-run'"
    assert recover_run_units(store, "run", job_id="iso") == ()


# ---------------------------------------------------------------------------
# GC never removes a unit worktree
# ---------------------------------------------------------------------------


def test_gc_removes_a_clean_isolated_worktree_and_skips_unit_worktrees(repo, tmp_path):
    root = tmp_path / "ws"
    unit_job = _job(repo, "unit-job", root=root)
    iso_job = _isolated_job(repo)
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("old", (unit_job, iso_job), workspace_root=root)
    manager = WorkspaceManager(root, (unit_job, iso_job), run_id="old")
    for job in (unit_job, iso_job):
        reservation = store.reserve_attempt("old", job.id, reserved_at="2026-11-14T10:00:00Z", **ENDPOINT)
        path = manager.allocate_path(job, reservation.attempt_no)
        store.record_reservation_workspace(
            "old", job.id, reservation.attempt_no, workspace_path=path,
            workspace_status="unit" if manager.is_unit_job(job.id) else None,
        )
        resolution = manager.create(job, workspace_path=path)
        store.arm_reservation("old", job.id, reservation.attempt_no, invoke_armed_at="2026-11-14T10:00:01Z")
        store.append_attempt(
            Attempt(run_id="old", job_id=job.id, attempt_no=reservation.attempt_no, status="completed",
                    started_at="2026-11-14T10:00:01Z", ended_at="2026-11-14T10:00:02Z",
                    workspace=resolution.path, base_ref=resolution.base_ref,
                    workspace_status=resolution.status, workspace_reason=resolution.reason, **ENDPOINT),
            terminal_state=JobState.ACCEPTED,
        )
    manager.finalize_unit_run("unit-job", writers_stopped=True)
    unit_path = unit_worktree_path(root, repository_identity(repo.root), "u")
    assert unit_path.is_dir()
    # A newer run now owns the same unit at the same path.
    _manager(unit_job, "new").prepare(unit_job, 1)

    report = gc_workspaces(store, "old", force=True)
    mapping = report.to_mapping()
    assert mapping["counts"] == {"removed": 1, "refused": 0, "skipped": 1}
    (skipped,) = report.skipped
    assert skipped.job_id == "unit-job" and "unit registry decides" in skipped.reason
    assert report.removed[0].job_id == "iso" and not report.removed[0].workspace.exists()
    assert unit_path.is_dir() and git("status", "--porcelain", cwd=unit_path) == ""
    assert mapping["skipped"][0]["workspace"] == str(unit_path)


def test_gc_refuses_a_dirty_isolated_worktree_and_reports_unrecorded_ones(repo, tmp_path):
    root = tmp_path / "ws"
    job = _isolated_job(repo)
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", (job,), workspace_root=root)
    manager = WorkspaceManager(root, (job,))
    reservation = store.reserve_attempt("run", "iso", reserved_at="2026-11-14T10:00:00Z", **ENDPOINT)
    path = manager.allocate_path(job, 1)
    store.record_reservation_workspace("run", "iso", 1, workspace_path=path)
    resolution = manager.create(job, workspace_path=path)
    store.arm_reservation("run", "iso", reservation.attempt_no, invoke_armed_at="2026-11-14T10:00:01Z")
    store.append_attempt(
        Attempt(run_id="run", job_id="iso", attempt_no=1, status="completed", started_at="a", ended_at="b",
                workspace=resolution.path, base_ref=resolution.base_ref, workspace_status="isolated",
                **ENDPOINT),
        terminal_state=JobState.REJECTED,
    )
    (resolution.path / "dirty.txt").write_text("x", encoding="ascii")
    orphan = create_worktree(repo.root, root / "stray" / "orphan", repo.head())
    report = gc_workspaces(store, "run")
    reasons = sorted(entry.reason for entry in report.refused)
    assert reasons == ["registered worktree has no attempt row", "worktree is dirty"]
    assert resolution.path.is_dir() and orphan.is_dir()
    assert gc_workspaces(store, "run", accepted_only=True).skipped[0].reason == "job is not accepted (rejected)"


# ---------------------------------------------------------------------------
# the workspace spec, the run checks and the public surface
# ---------------------------------------------------------------------------


def test_a_branch_mode_spec_round_trips_and_resolves_its_root(tmp_path):
    spec = WorkspaceSpec.from_value(
        {"mode": "branch", "unit": "item-1", "root": "units", "base_ref": "origin/dev"}, base_dir=tmp_path
    )
    assert spec is not None and spec.branch_mode and spec.root == (tmp_path / "units").resolve()
    assert spec.unit_branch == "agent/item-1"
    assert WorkspaceSpec.from_value(spec.to_mapping()) == spec
    custom = WorkspaceSpec(mode="branch", unit="item-1", root=tmp_path, base_ref="origin/dev", branch="work/x")
    assert custom.unit_branch == "work/x" and custom.to_mapping()["branch"] == "work/x"
    with pytest.raises(ValueError, match="only a branch-mode workspace"):
        _ = WorkspaceSpec(isolate=True).unit_branch


@pytest.mark.parametrize("value, message", [
    ({"mode": "branch", "unit": "u", "root": "/r", "base_ref": "origin/dev", "isolate": True}, "exclusive"),
    ({"mode": "branch", "unit": "u", "root": "/r"}, "requires base_ref"),
    ({"mode": "branch", "unit": "u", "base_ref": "origin/dev"}, "requires root"),
    ({"mode": "branch", "unit": "a/b", "root": "/r", "base_ref": "origin/dev"}, "unit id"),
    ({"mode": "trunk", "unit": "u", "root": "/r", "base_ref": "origin/dev"}, "mode must be 'branch'"),
    ({"unit": "u", "root": "/r"}, "unit, root: only with mode: branch"),
    ({"mode": "branch", "unit": "u", "root": "/r", "base_ref": "origin/dev", "branch": " "}, "branch must not"),
])
def test_a_branch_mode_spec_refuses_bad_values(value, message):
    with pytest.raises(ValueError, match=message):
        WorkspaceSpec.from_value(value)


def test_the_supervisor_budget_counts_the_end_of_run_commit_of_a_branch_job(repo, tmp_path):
    options = {"session": "agent", "session_finalize_s": 50}
    branch = _job(repo, root=tmp_path / "units")
    branch = Job(id="b", prompt=branch.prompt, models=branch.models, directory=branch.directory,
                 contract=branch.contract, workspace=branch.workspace, options=options, max_attempts=2)
    plain = Job(id="p", prompt=branch.prompt, models=branch.models, directory=branch.directory,
                contract=branch.contract, options=options, max_attempts=2)
    policy = jobs_run.BackpressurePolicy(cap_s=0.0)
    check = dict(session_root=tmp_path / "s", supervisor_budget_s=1e9, timeout_s=100.0, max_parallel=1,
                 backpressure=policy)
    with_commit = jobs_run.check_agent_session_run([branch], **check)
    without = jobs_run.check_agent_session_run([plain], **check)
    assert with_commit is not None and without is not None
    assert with_commit - without == pytest.approx(2 * 50)


def test_run_job_refuses_a_branch_job_without_a_finalizing_manager(repo, tmp_path, monkeypatch):
    import llm_kit.dispatch.declaration as declaration

    monkeypatch.setattr(declaration, "check_many", lambda *_a, **_k: {})
    job = _job(repo, root=tmp_path / "units")

    class Backend:
        name = "claude-cli"

        def complete(self, *args, **kwargs):  # pragma: no cover - never reached
            raise AssertionError("no attempt may run")

    from llm_kit.adapters.capabilities import CLAUDE_CAPABILITIES
    from llm_kit.dispatch.factory import BackendSelection

    job = Job(id=job.id, prompt=job.prompt, models=("h-claude",), directory=job.directory,
              contract=job.contract, workspace=job.workspace)
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("r", (job,), workspace_root=tmp_path / "ws")
    with pytest.raises(ws.WorkspaceError, match="is branch mode: run it through run_jobs"):
        jobs_run.run_job(
            store, "r", job,
            backend_factory=lambda endpoint, **_k: BackendSelection(
                endpoint, "harness", Backend(), "m", capabilities=CLAUDE_CAPABILITIES),
            capabilities_provider=lambda: {"claude-cli": CLAUDE_CAPABILITIES},
            reachability_cache={},
        )
    assert store.get_job("r", job.id).state is JobState.FAILED  # type: ignore[union-attr]
    assert store.list_reservations("r", job.id) == []


PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit"


@pytest.mark.parametrize("module_name", ["llm_kit.jobs.units", "llm_kit.jobs.workspace"])
def test_public_surface_lists_the_branch_mode_names(module_name):
    import importlib

    surface = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))
    assert module_name in surface["covers"]
    listed = {e["name"].rsplit(".", 1)[1] for e in surface["entries"]
              if e["status"] == "public" and e["name"].rsplit(".", 1)[0] == module_name}
    module = importlib.import_module(module_name)
    assert listed == set(module.__all__)
    for name in module.__all__:
        value = getattr(module, name)
        if callable(value) and not isinstance(value, (str, int, float, frozenset)):
            assert (value.__doc__ or "").strip(), name


def test_gc_does_not_report_a_unit_worktree_of_another_run_as_an_orphan(repo, tmp_path):
    root = tmp_path / "ws"
    job = _job(repo, root=root)
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("quiet", (_isolated_job(repo),), workspace_root=root)
    store.ensure_workspace_root("quiet", root)
    _manager(job, "elsewhere").prepare(job, 1)  # a unit worktree under the same root, no row here
    report = gc_workspaces(store, "quiet")
    assert report.refused == () and report.removed == ()


def test_the_branch_hook_holds_the_unit_on_the_survivors_it_is_given(repo, tmp_path):
    from llm_kit.completion.session_types import DetachedSurvivor, WorkspaceFinalizeStatus

    job = _job(repo, root=tmp_path / "units")
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", (job,), workspace_root=tmp_path / "ws")
    manager = _manager(job, "run")
    hook = ws.BranchWorkspaceHook(manager, store)
    common = dict(run_id="run", job_id="job", final_state="accepted", stop_kind=None)
    nothing = hook.finalize_run(working_directory=repo.root, writers_stopped=True, detached_survivors=(),
                                **common)
    assert nothing.status is WorkspaceFinalizeStatus.NOT_APPLICABLE  # no unit acquired yet
    path = manager.prepare(job, 1).path
    (path / "late.txt").write_text("late\n", encoding="ascii")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        import psutil

        survivor = DetachedSurvivor(ProcessIdentity(sleeper.pid, psutil.Process(sleeper.pid).create_time()),
                                    str(path))
        held = hook.finalize_run(working_directory=path, writers_stopped=False,
                                 detached_survivors=(survivor,), **common)
        assert held.status is WorkspaceFinalizeStatus.WRITER_HELD and str(sleeper.pid) in held.detail
        record = UnitRegistry(tmp_path / "units").get(repository_identity(repo.root), "u")
        assert record.state is UnitState.WRITER_HELD and record.held_processes == (survivor,)
    finally:
        sleeper.kill()
        sleeper.wait()
    # The next run re-finalizes the writer-held unit once the survivor is gone, then acquires it.
    assert _manager(job, "run-2").prepare(job, 1).path == path
    assert repo.log("agent/u")[0] == "WIP: u run 1" and repo.show("agent/u", "late.txt") == "late"
