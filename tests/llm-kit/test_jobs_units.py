"""The unit registry of branch-mode workspaces (``llm_kit.jobs.units``), against real Git.

Each test builds a repository with an ``origin/dev`` merge target (``unit_repos``) and a unit
worktree on its own branch, then drives the registry: acquisition and its exclusions, the
end-of-run commit and the held states, crash recovery by a dead owner's identity, removal and
its refusals, merge records, the interprocess lock, and the record's JSON Schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from importlib import resources
from pathlib import Path

import pytest

from llm_kit.completion.session_types import DetachedSurvivor, ProcessIdentity
from llm_kit.jobs import units
from llm_kit.jobs.units import (
    UnitBusyError,
    UnitError,
    UnitFinalizeStatus,
    UnitOwnershipError,
    UnitRegistry,
    UnitRemovalRefused,
    UnitState,
    acquire_unit,
    check_unit_id,
    finalize_unit,
    list_units,
    live_processes,
    record_unit_merge,
    remove_unit_worktree,
    repository_identity,
    repository_lock,
)
from llm_kit.schema.json_schema import check_schema, validate
from unit_repos import Repo, git, hermetic_git, make_repo  # noqa: F401 -- hermetic_git is a fixture

pytestmark = [
    pytest.mark.usefixtures("hermetic_git"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX hooks and process helpers"),
]

SRC = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src"
CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "katsoft-contracts" / "src"
SCHEMA = json.loads(
    (resources.files("llm_kit.jobs") / "schemas" / units.UNIT_RECORD_SCHEMA_FILE).read_text(encoding="utf-8")
)


def conforms(record: units.UnitRecord) -> dict:
    document = record.to_mapping()
    check_schema(SCHEMA)
    assert validate(SCHEMA, document) == (), validate(SCHEMA, document)
    return document


class Unit:
    """One repository, one registry, one unit worktree on ``agent/u``."""

    def __init__(self, tmp_path: Path, name: str = "u") -> None:
        self.repo: Repo = make_repo(tmp_path)
        self.registry = UnitRegistry(tmp_path / "worktrees")
        self.repository = repository_identity(self.repo.root)
        self.unit = name
        self.branch = f"agent/{name}"
        self.path = (tmp_path / "worktrees" / "project" / name).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-q", "-b", self.branch, str(self.path), "origin/dev", cwd=self.repo.root)

    def acquire(self, run_id: str = "run-1", job_id: str = "job-1", **extra: object) -> units.UnitLease:
        params: dict = dict(repository=self.repository, unit=self.unit, branch=self.branch,
                            worktree=self.path, run_id=run_id, job_id=job_id)
        params.update(extra)
        return acquire_unit(self.registry, **params)

    def finalize(self, **kwargs: object) -> units.UnitFinalizeResult:
        return finalize_unit(self.registry, self.repository, self.unit, **kwargs)

    def record(self) -> units.UnitRecord:
        record = self.registry.get(self.repository, self.unit)
        assert record is not None
        return record

    def write(self, name: str = "work.txt", text: str = "work\n") -> None:
        (self.path / name).write_text(text, encoding="utf-8")


@pytest.fixture
def unit(tmp_path: Path) -> Unit:
    return Unit(tmp_path)


def _sleeper(seconds: float = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _identity_of(process: subprocess.Popen) -> DetachedSurvivor:
    import psutil

    return DetachedSurvivor(ProcessIdentity(process.pid, psutil.Process(process.pid).create_time()), None)


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "scripts" / "unit_script.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"import sys\nsys.path[:0] = [{str(SRC)!r}, {str(CONTRACTS)!r}]\n" + textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# identity, ids and the registry file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["u", "repo-task-item", "a.b_c-9", "x" * 128])
def test_check_unit_id_accepts_path_safe_ids(value):
    assert check_unit_id(value) == value


@pytest.mark.parametrize("value", ["", "-x", ".x", "a/b", "a..b", "x" * 129, "a b", None, 7])
def test_check_unit_id_refuses_anything_else(value):
    with pytest.raises(ValueError, match="unit id"):
        check_unit_id(value)


def test_repository_identity_is_the_common_directory_from_any_worktree(unit):
    assert unit.repository == unit.repo.common_dir
    assert repository_identity(unit.path) == unit.repository
    assert repository_identity(unit.repo.root / "pkg") == unit.repository


def test_repository_identity_refuses_a_directory_outside_git(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    with pytest.raises(UnitError, match="not a Git repository"):
        repository_identity(outside)


def test_the_registry_has_its_own_schema_and_refuses_a_newer_one(tmp_path):
    registry = UnitRegistry(tmp_path / "root")
    assert registry.path == (tmp_path / "root" / units.REGISTRY_FILENAME).resolve()
    assert registry.schema_version() == units.REGISTRY_SCHEMA == 1
    assert UnitRegistry(tmp_path / "root", create=False).list() == ()
    connection = sqlite3.connect(registry.path)
    connection.execute("UPDATE schema_version SET version = 2")
    connection.commit()
    connection.close()
    with pytest.raises(UnitError, match="schema version 2, newer"):
        UnitRegistry(tmp_path / "root")
    with pytest.raises(UnitError, match="no unit registry"):
        UnitRegistry(tmp_path / "absent", create=False)


# ---------------------------------------------------------------------------
# acquisition
# ---------------------------------------------------------------------------


def test_acquire_makes_this_process_the_exclusive_owner(unit):
    import psutil

    lease = unit.acquire(base_sha=unit.repo.head("origin/dev"))
    record = unit.record()
    assert record.state is UnitState.ACTIVE and record.run_count == lease.run_no == 1
    assert record.owner is not None and record.owner.nonce == lease.nonce
    assert record.owner.process == ProcessIdentity(os.getpid(), psutil.Process().create_time())
    assert (record.owner_run_id, record.owner_job_id) == ("run-1", "job-1")
    assert record.base_sha == unit.repo.head("origin/dev") and record.branch == "agent/u"
    assert conforms(record)["owner"]["nonce"] == lease.nonce
    with pytest.raises(UnitBusyError, match="is active, owned by run 'run-1' job 'job-1'$"):
        unit.acquire(run_id="run-2")


def test_the_first_registry_claims_the_unit_in_the_repository(unit, tmp_path):
    unit.acquire()
    key = f"{units.CLAIM_SECTION}.u.registry"
    assert git("config", "--get", key, cwd=unit.repo.root) == str(unit.registry.path)
    other = UnitRegistry(tmp_path / "other-root")
    with pytest.raises(UnitBusyError, match="belongs to another registry"):
        acquire_unit(other, repository=unit.repository, unit="u", branch="agent/u", worktree=unit.path,
                     run_id="run-9", job_id="job-9")
    assert other.get(unit.repository, "u") is None


def test_the_recorded_branch_and_worktree_are_authoritative(unit, tmp_path):
    lease = unit.acquire()
    unit.finalize(lease=lease)
    with pytest.raises(UnitError, match="records branch 'agent/u'; the job declares 'agent/other'"):
        acquire_unit(unit.registry, repository=unit.repository, unit="u", branch="agent/other",
                     worktree=unit.path, run_id="r", job_id="j")
    with pytest.raises(UnitError, match="records worktree"):
        acquire_unit(unit.registry, repository=unit.repository, unit="u", branch="agent/u",
                     worktree=tmp_path / "elsewhere", run_id="r", job_id="j")


def test_reacquiring_a_parked_unit_counts_the_run(unit):
    unit.finalize(lease=unit.acquire())
    lease = unit.acquire(run_id="run-2")
    assert lease.run_no == 2 and unit.record().owner_run_id == "run-2"
    unit.write()
    result = unit.finalize(lease=lease)
    assert result.status is UnitFinalizeStatus.COMMITTED
    assert unit.repo.log("agent/u")[0] == "WIP: u run 2"


# ---------------------------------------------------------------------------
# the end-of-run commit
# ---------------------------------------------------------------------------


def test_finalize_commits_every_remaining_change_and_parks_the_unit(unit):
    lease = unit.acquire()
    unit.write("tracked-new.txt")
    (unit.path / "README.txt").write_text("changed\n", encoding="ascii")
    (unit.path / "nested").mkdir()
    (unit.path / "nested" / "untracked.txt").write_text("u\n", encoding="ascii")
    result = unit.finalize(lease=lease)
    assert result.status is UnitFinalizeStatus.COMMITTED
    assert result.commit_sha == git("rev-parse", "HEAD", cwd=unit.path) == unit.repo.head("agent/u")
    assert git("status", "--porcelain", "--untracked-files=all", cwd=unit.path) == ""
    assert unit.repo.log("agent/u")[0] == units.WIP_MESSAGE.format(unit="u", run=1) == "WIP: u run 1"
    assert unit.repo.show("agent/u", "nested/untracked.txt") == "u"
    record = unit.record()
    assert record.state is UnitState.PARKED and record.owner is None
    assert record.last_commit_sha == result.commit_sha and record.last_run_ended_at is not None
    assert record.owner_run_id == "run-1"  # the last owning run stays recorded
    conforms(record)


def test_finalize_with_nothing_left_is_clean(unit):
    result = unit.finalize(lease=unit.acquire())
    assert result.status is UnitFinalizeStatus.CLEAN and result.commit_sha is None
    assert unit.record().last_commit_sha == unit.repo.head("origin/dev")
    assert unit.repo.log("agent/u") == ["seed"]


def test_gitignored_content_is_not_committed(unit):
    (unit.path / ".gitignore").write_text("cache/\n", encoding="ascii")
    git("add", ".gitignore", cwd=unit.path)
    git("commit", "-q", "-m", "ignore cache", cwd=unit.path)
    lease = unit.acquire()
    (unit.path / "cache").mkdir()
    (unit.path / "cache" / "blob").write_text("regenerable\n", encoding="ascii")
    assert unit.finalize(lease=lease).status is UnitFinalizeStatus.CLEAN


def test_finalize_refuses_a_lease_that_does_not_own_the_unit(unit):
    lease = unit.acquire()
    unit.finalize(lease=lease)
    with pytest.raises(UnitOwnershipError, match="not held by this lease"):
        unit.finalize(lease=lease)


def test_a_crash_between_the_commit_and_the_registry_update_is_finalized_again(unit, monkeypatch):
    lease = unit.acquire()
    unit.write()
    real = UnitRegistry._update
    calls = {"n": 0}

    def failing(self, record, **changes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash after git, before sqlite")
        return real(self, record, **changes)

    monkeypatch.setattr(UnitRegistry, "_update", failing)
    with pytest.raises(RuntimeError, match="crash after git"):
        unit.finalize(lease=lease)
    assert unit.record().state is UnitState.ACTIVE
    wip = unit.repo.head("agent/u")
    again = unit.finalize(lease=lease)  # repeated recovery: the tree is clean now
    assert again.status is UnitFinalizeStatus.CLEAN
    assert unit.record().state is UnitState.PARKED and unit.record().last_commit_sha == wip


def test_a_rejecting_hook_leaves_the_unit_dirty_held_and_blocks_reuse(unit):
    hook = unit.repo.common_dir / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'rejected by policy' >&2\nexit 1\n", encoding="ascii")
    hook.chmod(0o755)
    lease = unit.acquire()
    unit.write()
    result = unit.finalize(lease=lease)
    assert result.status is UnitFinalizeStatus.DIRTY_HELD
    assert "rejected by policy" in (result.detail or "")
    record = unit.record()
    assert record.state is UnitState.DIRTY_HELD and record.owner is None
    conforms(record)
    with pytest.raises(UnitBusyError, match="dirty_held: the end-of-run commit failed"):
        unit.acquire(run_id="run-2")
    with pytest.raises(UnitOwnershipError, match="resolve_dirty"):
        unit.finalize()
    hook.unlink()
    resolved = unit.finalize(resolve_dirty=True)
    assert resolved.status is UnitFinalizeStatus.COMMITTED
    assert unit.record().state is UnitState.PARKED


def test_head_off_the_recorded_branch_is_dirty_held(unit):
    lease = unit.acquire()
    git("switch", "-q", "-c", "elsewhere", cwd=unit.path)
    result = unit.finalize(lease=lease)
    assert result.status is UnitFinalizeStatus.DIRTY_HELD
    assert "not the recorded branch refs/heads/agent/u" in (result.detail or "")


def test_an_exhausted_finalization_budget_fails_safe(unit):
    hook = unit.repo.common_dir / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nsleep 5\n", encoding="ascii")
    hook.chmod(0o755)
    lease = unit.acquire()
    unit.write()
    started = time.monotonic()
    result = unit.finalize(lease=lease, timeout_s=1.0)
    assert time.monotonic() - started < 4.5
    assert result.status is UnitFinalizeStatus.DIRTY_HELD
    assert "budget ran out" in (result.detail or "")


def test_finalize_validates_its_budget(unit):
    lease = unit.acquire()
    for bad in (0, -1, float("inf"), True):
        with pytest.raises(ValueError, match="timeout_s"):
            unit.finalize(lease=lease, timeout_s=bad)


# ---------------------------------------------------------------------------
# writer-held: survivors and unconfirmed writers
# ---------------------------------------------------------------------------


def test_a_detached_survivor_holds_the_unit_until_it_exits(unit):
    survivor = _sleeper()
    try:
        identity = _identity_of(survivor)
        lease = unit.acquire()
        unit.write()
        held = unit.finalize(lease=lease, writers_stopped=False, processes=(identity,))
        assert held.status is UnitFinalizeStatus.WRITER_HELD
        record = unit.record()
        assert record.state is UnitState.WRITER_HELD and record.held_processes == (identity,)
        assert f"pid {survivor.pid}" in (record.held_reason or "")
        conforms(record)
        assert unit.repo.log("agent/u") == ["seed"]  # nothing committed beside a writer
        with pytest.raises(UnitBusyError, match="writer_held"):
            unit.acquire(run_id="run-2")
        still = unit.finalize()
        assert still.status is UnitFinalizeStatus.WRITER_HELD
    finally:
        survivor.kill()
        survivor.wait()
    done = unit.finalize()
    assert done.status is UnitFinalizeStatus.COMMITTED
    assert unit.record().state is UnitState.PARKED and unit.record().held_processes == ()
    assert unit.repo.log("agent/u")[0] == "WIP: u run 1"


def test_an_unconfirmed_shutdown_never_commits(unit):
    lease = unit.acquire()
    unit.write()
    result = unit.finalize(lease=lease, writers_stopped=False)
    assert result.status is UnitFinalizeStatus.WRITER_HELD
    assert "not confirmed stopped" in (result.detail or "")
    assert unit.repo.log("agent/u") == ["seed"]


def test_a_live_process_blocks_the_commit_even_when_writers_stopped(unit):
    survivor = _sleeper()
    try:
        lease = unit.acquire()
        result = unit.finalize(lease=lease, processes=(_identity_of(survivor),))
        assert result.status is UnitFinalizeStatus.WRITER_HELD
    finally:
        survivor.kill()
        survivor.wait()


def test_live_processes_rechecks_identity_not_just_pid(unit):
    survivor = _sleeper()
    try:
        identity = _identity_of(survivor)
        wrong_time = DetachedSurvivor(ProcessIdentity(survivor.pid, identity.identity.create_time + 100), None)
        assert live_processes((identity, wrong_time)) == (identity,)
    finally:
        survivor.kill()
        survivor.wait()
    assert live_processes((identity,)) == ()


# ---------------------------------------------------------------------------
# crash recovery: an active unit whose owner died
# ---------------------------------------------------------------------------


def _crash_owner(tmp_path: Path, unit: Unit, *, run_id: str = "run-1", job_id: str = "job-1") -> None:
    script = _script(tmp_path, f"""
        import os
        from pathlib import Path
        from llm_kit.jobs.units import UnitRegistry, acquire_unit
        registry = UnitRegistry(Path({str(unit.registry.root)!r}))
        acquire_unit(registry, repository=Path({str(unit.repository)!r}), unit={unit.unit!r},
                     branch={unit.branch!r}, worktree=Path({str(unit.path)!r}),
                     run_id={run_id!r}, job_id={job_id!r})
        Path({str(unit.path)!r}, "left-behind.txt").write_text("crash\\n")
        os._exit(9)
    """)
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert completed.returncode == 9, completed.stderr


def test_recovery_commits_for_an_owner_that_died(unit, tmp_path):
    _crash_owner(tmp_path, unit)
    record = unit.record()
    assert record.state is UnitState.ACTIVE and record.owner is not None
    with pytest.raises(UnitBusyError, match="whose process is gone; recover it first"):
        unit.acquire(run_id="run-2")
    result = unit.finalize()
    assert result.status is UnitFinalizeStatus.COMMITTED
    assert unit.repo.show("agent/u", "left-behind.txt") == "crash"
    assert unit.record().state is UnitState.PARKED


def test_recovery_beside_surviving_processes_is_indeterminate(unit, tmp_path):
    _crash_owner(tmp_path, unit)
    survivor = _sleeper()
    try:
        identity = _identity_of(survivor)
        result = unit.finalize(processes=(identity,))
        assert result.status is UnitFinalizeStatus.INDETERMINATE
        record = unit.record()
        assert record.state is UnitState.ACTIVE and record.indeterminate
        assert record.held_processes == (identity,)
        with pytest.raises(UnitBusyError, match="indeterminate: processes of the dead owning run"):
            unit.acquire(run_id="run-2")
        with pytest.raises(UnitBusyError, match="merge is blocked"):
            record_unit_merge(unit.registry, unit.repository, "u", tip_sha="a" * 40, target_sha="b" * 40)
        with pytest.raises(UnitRemovalRefused, match="is active"):
            remove_unit_worktree(unit.registry, unit.repository, "u")
    finally:
        survivor.kill()
        survivor.wait()
    assert unit.finalize(processes=(identity,)).status is UnitFinalizeStatus.COMMITTED


def test_recovery_refuses_while_the_owner_is_alive(unit):
    unit.acquire()
    with pytest.raises(UnitOwnershipError, match="owner process .* is alive"):
        unit.finalize()


def test_finalizing_a_parked_unit_without_a_lease_changes_nothing(unit):
    unit.finalize(lease=unit.acquire())
    before = unit.record()
    result = unit.finalize()
    assert result.status is UnitFinalizeStatus.CLEAN and result.detail == "already parked"
    assert unit.record() == before
    with pytest.raises(UnitError, match="unknown unit 'nope'"):
        finalize_unit(unit.registry, unit.repository, "nope")


# ---------------------------------------------------------------------------
# removal
# ---------------------------------------------------------------------------


def test_removal_refuses_an_active_unit(unit):
    unit.acquire()
    with pytest.raises(UnitRemovalRefused, match="is active"):
        remove_unit_worktree(unit.registry, unit.repository, "u")
    assert unit.path.is_dir() and unit.record().state is UnitState.ACTIVE


def test_removal_of_a_dirty_tree_marks_it_dirty_held(unit):
    unit.finalize(lease=unit.acquire())
    unit.write("late.txt")
    with pytest.raises(UnitRemovalRefused, match="the worktree is dirty") as caught:
        remove_unit_worktree(unit.registry, unit.repository, "u")
    assert caught.value.reason == "the worktree is dirty"
    assert unit.record().state is UnitState.DIRTY_HELD and unit.path.is_dir()
    with pytest.raises(UnitRemovalRefused, match="is dirty_held"):
        remove_unit_worktree(unit.registry, unit.repository, "u")


def test_removal_refuses_head_not_at_the_branch_tip(unit):
    lease = unit.acquire()
    unit.write()
    unit.finalize(lease=lease)
    git("checkout", "-q", "--detach", "HEAD~1", cwd=unit.path)
    with pytest.raises(UnitRemovalRefused, match="HEAD is a detached HEAD, not the recorded branch"):
        remove_unit_worktree(unit.registry, unit.repository, "u")
    assert unit.record().state is UnitState.DIRTY_HELD and unit.path.is_dir()


def test_removal_refuses_the_wrong_branch(unit):
    unit.finalize(lease=unit.acquire())
    git("switch", "-q", "-c", "agent/other", cwd=unit.path)
    with pytest.raises(UnitRemovalRefused, match="refs/heads/agent/other, not the recorded branch"):
        remove_unit_worktree(unit.registry, unit.repository, "u")
    assert unit.record().state is UnitState.DIRTY_HELD


def test_removal_removes_a_clean_parked_worktree_and_keeps_the_branch(unit):
    lease = unit.acquire()
    unit.write()
    tip = unit.finalize(lease=lease).commit_sha
    record = remove_unit_worktree(unit.registry, unit.repository, "u")
    assert record.state is UnitState.REMOVED and not unit.path.exists()
    assert unit.repo.head("agent/u") == tip
    assert str(unit.path) not in git("worktree", "list", "--porcelain", cwd=unit.repo.root)
    assert remove_unit_worktree(unit.registry, unit.repository, "u") == record  # idempotent
    conforms(record)
    lease = unit.acquire(run_id="run-2")  # a removed unit is reacquired (and rehydrated by workspace)
    assert lease.run_no == 2


def test_removal_after_a_crash_prunes_the_lingering_registration(unit):
    unit.finalize(lease=unit.acquire())
    import shutil

    shutil.rmtree(unit.path)
    assert str(unit.path) in git("worktree", "list", "--porcelain", cwd=unit.repo.root)
    record = remove_unit_worktree(unit.registry, unit.repository, "u")
    assert record.state is UnitState.REMOVED
    assert str(unit.path) not in git("worktree", "list", "--porcelain", cwd=unit.repo.root)


# ---------------------------------------------------------------------------
# merge records and listing
# ---------------------------------------------------------------------------


def test_record_unit_merge_keeps_the_7_2_fields(unit):
    lease = unit.acquire()
    with pytest.raises(UnitBusyError, match="active; merge is blocked"):
        record_unit_merge(unit.registry, unit.repository, "u", tip_sha="a" * 40, target_sha="b" * 40)
    unit.finalize(lease=lease)
    with pytest.raises(ValueError, match="tip_sha"):
        record_unit_merge(unit.registry, unit.repository, "u", tip_sha=" ", target_sha="b" * 40)
    record = record_unit_merge(unit.registry, unit.repository, "u", tip_sha="a" * 40, target_sha="b" * 40)
    assert (record.merged_tip_sha, record.merge_target_sha) == ("a" * 40, "b" * 40)
    assert record.merged_at is not None and record.state is UnitState.PARKED
    conforms(record)


def test_list_units_reads_a_root_and_is_empty_without_a_registry(unit, tmp_path):
    assert list_units(tmp_path / "nothing-here") == ()
    unit.acquire()
    (listed,) = list_units(unit.registry.root)
    assert listed.unit == "u" and listed.state is UnitState.ACTIVE


# ---------------------------------------------------------------------------
# the interprocess lock
# ---------------------------------------------------------------------------


def test_the_repository_lock_serializes_another_process(unit, tmp_path):
    script = _script(tmp_path, f"""
        from pathlib import Path
        from llm_kit.jobs.units import UnitRegistry, acquire_unit
        registry = UnitRegistry(Path({str(unit.registry.root)!r}))
        acquire_unit(registry, repository=Path({str(unit.repository)!r}), unit="u", branch="agent/u",
                     worktree=Path({str(unit.path)!r}), run_id="other", job_id="j")
        print("acquired")
    """)
    with repository_lock(unit.repository):
        child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert child.poll() is None  # blocked on the lock
        assert unit.registry.get(unit.repository, "u") is None
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 0 and out.strip() == "acquired"
    assert unit.record().owner_run_id == "other"
