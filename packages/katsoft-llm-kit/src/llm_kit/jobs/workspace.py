"""Git worktree isolation, branch-mode unit worktrees and conservative workspace GC.

Two worktree kinds:

- ``isolate: true``: every attempt gets a fresh DETACHED worktree below the
  run's workspace root (``<root>/<job>/attempt-<n>-<uuid>``), created from the
  base ref captured at run start; :func:`gc_workspaces` reclaims them later.
- ``mode: branch`` (design 6.1, 6.2, 11.5): every attempt of the job runs in
  the worktree of one UNIT of work, on the unit's branch (default
  ``agent/<unit>``), at the FIXED path ``<root>/<repo>/<unit>``
  (:func:`unit_worktree_path`). The first run creates the branch from the
  job's ``base_ref`` resolved against the REMOTE merge target
  (:func:`resolve_unit_base_ref`), never the checkout HEAD; later runs reuse
  it, and a removed worktree is rehydrated from the branch at the same path.
  Ownership and the lifecycle live in the unit registry
  (:mod:`llm_kit.jobs.units`): the first attempt acquires the unit, and
  :class:`BranchWorkspaceHook` makes the end-of-run commit when the job's run
  ends. Crash-path finalization (a hard cancel, an unexpected exit) is
  :func:`recover_run_units`. Attempts record ``workspace_status`` ``unit``,
  which :func:`gc_workspaces` never removes.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from llm_kit.completion.session_types import (
    DetachedSurvivor,
    SessionOptions,
    SessionPaths,
    SessionWorkspaceHook,
    StopKind,
    WorkspaceFinalizeResult,
    WorkspaceFinalizeStatus,
)

from .model import (
    Job,
    JobRecord,
    JobState,
    RunRecord,
    TERMINAL_STATES,
    WORKSPACE_STATUSES,
)
from .session import read_cleanup_record
from .store import JobStore, StoreError
from .units import (
    REGISTRY_FILENAME,
    UnitError,
    UnitFinalizeResult,
    UnitFinalizeStatus,
    UnitLease,
    UnitOwnershipError,
    UnitRegistry,
    UnitState,
    _registered_worktrees as _worktree_branches,
    acquire_unit,
    finalize_unit,
    list_units,
    repository_identity,
    repository_lock,
)


WORKSPACE_REASON_NONE = "no git repository detected from the declared directory"
WORKSPACE_REASON_DECLINED = "workspace isolation declined by the job"
#: The ``workspace_status`` of an attempt or reservation in a branch-mode unit worktree.
WORKSPACE_STATUS_UNIT = "unit"
# Two bounds, because the Git commands here have two very different shapes.
# A probe answers from metadata and is hung if it takes this long. A tree
# operation writes or walks a whole working tree, which on a large repository
# legitimately takes minutes.
GIT_PROBE_TIMEOUT_S = 30.0
GIT_TREE_TIMEOUT_S = 900.0

# Git documents no concurrency promise for `worktree add` against one
# repository, so creations are serialised per repository root. The lock is
# keyed on the resolved root: two runs over different repositories never wait
# on each other.
_WORKTREE_LOCK_REGISTRY_GUARD = threading.Lock()
_WORKTREE_LOCK_REGISTRY: dict[Path, threading.Lock] = {}


def _worktree_lock(repo_root: Path) -> threading.Lock:
    """Return the process-wide creation lock for one repository root."""
    with _WORKTREE_LOCK_REGISTRY_GUARD:
        return _WORKTREE_LOCK_REGISTRY.setdefault(repo_root, threading.Lock())


class WorkspaceError(RuntimeError):
    """Base class for workspace preparation failures."""


class WorkspaceCreationError(WorkspaceError):
    """A detected Git repository could not yield a worktree."""


class WorkspaceDetectionError(WorkspaceError):
    """Git could not determine whether the declared directory is a repository."""


@dataclass(frozen=True)
class WorkspaceResolution:
    """The workspace selected for one attempt."""

    path: Optional[Path]
    base_ref: Optional[str]
    repo_root: Optional[Path]
    status: str
    reason: str
    relative_directory: Path = Path()
    unit: Optional[str] = None
    branch: Optional[str] = None

    def __post_init__(self) -> None:
        if self.path is not None:
            object.__setattr__(self, "path", Path(self.path).expanduser().resolve())
        if self.repo_root is not None:
            object.__setattr__(
                self, "repo_root", Path(self.repo_root).expanduser().resolve()
            )
        object.__setattr__(self, "relative_directory", Path(self.relative_directory))
        if self.status not in {"isolated", "none", WORKSPACE_STATUS_UNIT}:
            raise ValueError("workspace resolution status must be isolated, unit or none")
        if self.status in {"isolated", WORKSPACE_STATUS_UNIT} and (
            self.path is None or self.base_ref is None
        ):
            raise ValueError(f"a workspace with status {self.status} requires a path and base_ref")
        if (self.status == WORKSPACE_STATUS_UNIT) != (self.unit is not None and self.branch is not None):
            raise ValueError("a unit workspace, and only one, names its unit and branch")
        if self.status == "none" and self.path is not None:
            raise ValueError("a non-isolated workspace cannot have a path")

    @property
    def working_directory(self) -> Path:
        """Return the declared repository subdirectory inside the worktree."""
        if self.path is None:
            raise ValueError("a non-isolated workspace has no isolated working directory")
        return (self.path / self.relative_directory).resolve()


@dataclass(frozen=True)
class _UnitPlan:
    """Run-start facts of a branch-mode unit."""

    repository: Path
    unit: str
    branch: str
    path: Path
    registry_root: Path


@dataclass(frozen=True)
class _WorkspacePlan:
    """Run-start workspace facts for one job."""

    repo_root: Optional[Path]
    base_ref: Optional[str]
    relative_directory: Path
    status: str
    reason: str
    error: Optional[str] = None
    unit: Optional[_UnitPlan] = None


def _git_result(
    args: Sequence[str], *, cwd: Path, timeout_s: float = GIT_TREE_TIMEOUT_S
) -> Optional[subprocess.CompletedProcess[str]]:
    """Run one local Git command without raising for a missing executable.

    The bound defaults to the tree-operation budget. A caller running a
    metadata probe passes ``GIT_PROBE_TIMEOUT_S`` instead.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_detail(result: Optional[subprocess.CompletedProcess[str]]) -> str:
    """Return bounded deterministic detail for a failed Git command."""
    if result is None:
        return "git command could not be started"
    detail = (result.stderr or result.stdout).strip()
    if not detail:
        detail = f"git exited with code {result.returncode}"
    return detail[:2000]


def _git_detection_result(
    directory: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the repository probe while preserving operational Git failures."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceDetectionError(
            f"could not run Git repository detection in {directory}: {exc}"
        ) from exc


def detect_repo_root(directory: Path) -> Optional[Path]:
    """Detect a repository root from the job's declared directory."""
    declared = Path(directory).expanduser().resolve()
    result = _git_detection_result(declared)
    if result.returncode != 0:
        detail = _git_detail(result)
        if "not a git repository" in detail.lower():
            return None
        raise WorkspaceDetectionError(
            f"Git repository detection failed in {declared}: {detail}"
        )
    if not result.stdout.strip():
        return None
    raw_root = result.stdout.strip()
    return Path(raw_root).expanduser().resolve()


def resolve_base_ref(repo_root: Path, requested: Optional[str] = None) -> str:
    """Resolve a requested ref, or observe the repository HEAD, as a commit id.

    Raises :class:`WorkspaceCreationError` for an empty or unresolvable ref.
    """
    root = Path(repo_root).expanduser().resolve()
    if requested is None:
        args: tuple[str, ...] = ("rev-parse", "HEAD")
    else:
        candidate = str(requested).strip()
        if not candidate:
            raise WorkspaceCreationError("workspace base_ref must not be empty")
        args = ("rev-parse", "--verify", f"{candidate}^{{commit}}")
    result = _git_result(args, cwd=root)
    if result is None or result.returncode != 0:
        detail = _git_detail(result)
        raise WorkspaceCreationError(
            f"could not resolve workspace base ref in {root}: {detail}"
        )
    resolved = result.stdout.strip()
    if not resolved:
        raise WorkspaceCreationError(f"Git returned no workspace base ref in {root}")
    return resolved


def _job_path_component(job_id: str) -> str:
    """Make a stable, bounded path component from a caller job identifier."""
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in job_id
    ).strip("._")
    if not safe:
        safe = "job"
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:80]}-{digest}"


def resolve_unit_base_ref(repo_root: Path, base_ref: str) -> str:
    """Resolve a branch-mode ``base_ref`` against the REMOTE merge target.

    ``base_ref`` names a remote-tracking branch (``origin/dev`` resolves
    ``refs/remotes/origin/dev``) or is a full commit id. A local branch, a tag
    or ``HEAD`` is refused: the unit branch starts from the task's remote
    merge target, not from a possibly stale local checkout (design 6.1). The
    remote-tracking ref is read as it is; fetching it first is the caller's
    choice. Returns the commit id; raises :class:`WorkspaceCreationError`.
    """
    root = Path(repo_root).expanduser().resolve()
    candidate = str(base_ref).strip()
    if not candidate:
        raise WorkspaceCreationError("workspace base_ref must not be empty")
    remote = _git_result(
        ("rev-parse", "--verify", "--quiet", f"refs/remotes/{candidate}^{{commit}}"),
        cwd=root,
        timeout_s=GIT_PROBE_TIMEOUT_S,
    )
    if remote is not None and remote.returncode == 0 and remote.stdout.strip():
        return remote.stdout.strip()
    if len(candidate) in (40, 64) and all(c in "0123456789abcdef" for c in candidate):
        return resolve_base_ref(root, candidate)
    raise WorkspaceCreationError(
        f"branch-mode base_ref {candidate!r} is neither a remote-tracking branch "
        f"(refs/remotes/{candidate}) nor a full commit id in {root}: the unit branch starts "
        "from the task's remote merge target (fetch it first), never the local checkout"
    )


def unit_worktree_path(root: Path, repository: Path, unit: str) -> Path:
    """The fixed worktree path ``<root>/<repo>/<unit>`` of a branch-mode unit.

    ``repository`` is the repository identity (its Git common directory,
    :func:`~llm_kit.jobs.units.repository_identity`). ``<repo>`` is the
    repository's directory name made path-safe, plus a 12-hex digest of the
    identity, so two repositories with one name never share a directory.
    """
    identity = Path(repository).resolve()
    name = identity.parent.name if identity.name == ".git" else identity.name.removesuffix(".git")
    safe = "".join(
        c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in name
    ).strip("._") or "repo"
    digest = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:12]
    return Path(root).expanduser().resolve() / f"{safe[:40]}-{digest}" / unit


def _branch_exists(repository: Path, branch: str) -> Optional[str]:
    """The tip of ``refs/heads/<branch>``, or ``None`` when the branch does not exist."""
    result = _git_result(
        ("--git-dir", str(repository), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"),
        cwd=repository,
        timeout_s=GIT_PROBE_TIMEOUT_S,
    )
    if result is None:
        raise WorkspaceCreationError(f"could not read refs/heads/{branch} in {repository}")
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _ensure_unit_worktree(plan: _UnitPlan, base_sha: Optional[str]) -> str:
    """Create, reuse or rehydrate the unit worktree; return its HEAD commit.

    Runs under the repository lock. A worktree registered at the path is
    reused when it is on the recorded branch; a registration whose directory
    is gone is pruned and the worktree rehydrated from the branch; an absent
    worktree is added on the existing branch, or with ``-b`` from
    ``base_sha`` when the branch does not exist yet.
    """
    repository = plan.repository
    with repository_lock(repository):
        try:
            registered = _worktree_branches(repository)
        except UnitError as exc:
            raise WorkspaceCreationError(str(exc)) from exc
        expected = f"refs/heads/{plan.branch}"
        if plan.path in registered and not plan.path.is_dir():
            pruned = _git_result(
                ("--git-dir", str(repository), "worktree", "prune", "--expire", "now"),
                cwd=repository,
            )
            if pruned is None or pruned.returncode != 0:
                raise WorkspaceCreationError(f"git worktree prune failed: {_git_detail(pruned)}")
            registered = _worktree_branches(repository)
        if plan.path in registered:
            if registered[plan.path] != expected:
                raise WorkspaceCreationError(
                    f"the unit worktree {plan.path} is on {registered[plan.path] or 'a detached HEAD'}, "
                    f"not the recorded branch {expected}"
                )
        else:
            if plan.path.exists():
                raise WorkspaceCreationError(
                    f"the unit worktree path {plan.path} exists and is not a registered worktree"
                )
            try:
                plan.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WorkspaceCreationError(
                    f"could not create workspace parent {plan.path.parent}: {exc}"
                ) from exc
            if _branch_exists(repository, plan.branch) is not None:
                args: tuple[str, ...] = ("worktree", "add", str(plan.path), plan.branch)
            elif base_sha is not None:
                args = ("worktree", "add", "-b", plan.branch, str(plan.path), base_sha)
            else:
                raise WorkspaceCreationError(
                    f"the unit branch {plan.branch} does not exist and no base_ref was resolved"
                )
            added = _git_result(("--git-dir", str(repository), *args), cwd=plan.path.parent)
            if added is None or added.returncode != 0:
                raise WorkspaceCreationError(
                    f"git worktree add failed for {plan.path}: {_git_detail(added)}"
                )
    head = _git_result(("symbolic-ref", "-q", "HEAD"), cwd=plan.path, timeout_s=GIT_PROBE_TIMEOUT_S)
    if head is None or head.stdout.strip() != f"refs/heads/{plan.branch}":
        raise WorkspaceCreationError(
            f"the unit worktree {plan.path} is not on the recorded branch {plan.branch}"
        )
    tip = _git_result(("rev-parse", "HEAD"), cwd=plan.path, timeout_s=GIT_PROBE_TIMEOUT_S)
    if tip is None or tip.returncode != 0 or not tip.stdout.strip():
        raise WorkspaceCreationError(f"could not read HEAD of {plan.path}: {_git_detail(tip)}")
    return tip.stdout.strip()


def create_worktree(repo_root: Path, workspace_path: Path, base_ref: str) -> Path:
    """Create one detached worktree at a new path; raise :class:`WorkspaceCreationError`.

    Creations are serialised per repository root within the process.
    """
    root = Path(repo_root).expanduser().resolve()
    path = Path(workspace_path).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceCreationError(
            f"could not create workspace parent {path.parent}: {exc}"
        ) from exc
    with _worktree_lock(root):
        if path.exists():
            raise WorkspaceCreationError(f"workspace path already exists: {path}")
        result = _git_result(
            ("-C", str(root), "worktree", "add", "--detach", str(path), base_ref),
            cwd=root,
        )
    if result is None or result.returncode != 0:
        raise WorkspaceCreationError(
            f"git worktree creation failed for {path}: {_git_detail(result)}"
        )
    if not path.is_dir():
        raise WorkspaceCreationError(
            f"git worktree creation returned success without a directory: {path}"
        )
    return path


class WorkspaceManager:
    """Capture run-start Git facts and create attempt worktrees.

    ``workspace_root`` holds detached per-attempt worktrees; ``jobs`` are
    planned at construction (repository, base ref, branch-mode unit), with
    ``base_refs`` (job id -> commit) replacing the observation for jobs a
    resumed run already captured. ``run_id`` is required for branch-mode jobs:
    it names the run that acquires their units. Plan failures surface when an
    attempt allocates or creates its workspace, as
    :class:`WorkspaceCreationError`.

    A branch-mode job's unit is acquired by its first :meth:`create` (or
    :meth:`hold`) and stays owned by this manager across attempts and the
    contract until :meth:`finalize_unit_run`.
    """

    def __init__(
        self,
        workspace_root: Path,
        jobs: Sequence[Job],
        *,
        base_refs: Optional[Mapping[str, str]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.run_id = run_id
        observed_heads: dict[Path, str] = {}
        captured_refs = dict(base_refs or {})
        self._plans: dict[str, _WorkspacePlan] = {}
        self._jobs: dict[str, Job] = {}
        self._leases: dict[str, UnitLease] = {}
        for job in jobs:
            self._jobs[job.id] = job
            self._plans[job.id] = self._plan_job(
                job, observed_heads, captured_refs.get(job.id)
            )

    @staticmethod
    def _plan_job(
        job: Job,
        observed_heads: dict[Path, str],
        captured_base_ref: Optional[str] = None,
    ) -> _WorkspacePlan:
        """Capture one job's repository and run-start base-ref facts."""
        if job.workspace is not None and job.workspace.branch_mode:
            return WorkspaceManager._plan_unit_job(job, captured_base_ref)
        if job.workspace is None or not job.workspace.isolate:
            return _WorkspacePlan(
                repo_root=None,
                base_ref=None,
                relative_directory=Path(),
                status="none",
                reason=WORKSPACE_REASON_DECLINED,
            )
        try:
            repo_root = detect_repo_root(job.declared_directory)
        except WorkspaceError as exc:
            return _WorkspacePlan(
                repo_root=None,
                base_ref=None,
                relative_directory=Path(),
                status="isolated",
                reason="Git repository detection failed",
                error=str(exc),
            )
        if repo_root is None:
            return _WorkspacePlan(
                repo_root=None,
                base_ref=None,
                relative_directory=Path(),
                status="none",
                reason=WORKSPACE_REASON_NONE,
            )
        try:
            relative_directory = job.declared_directory.relative_to(repo_root)
        except ValueError as exc:
            return _WorkspacePlan(
                repo_root=repo_root,
                base_ref=None,
                relative_directory=Path(),
                status="isolated",
                reason="Git repository detected",
                error=f"declared directory is outside detected repository root: {exc}",
            )
        requested = job.workspace.base_ref if job.workspace is not None else None
        try:
            if captured_base_ref is not None:
                base_ref = captured_base_ref
            elif requested is None:
                base_ref = observed_heads.get(repo_root)
                if base_ref is None:
                    base_ref = resolve_base_ref(repo_root)
                    observed_heads[repo_root] = base_ref
            else:
                base_ref = resolve_base_ref(repo_root, requested)
        except WorkspaceError as exc:
            return _WorkspacePlan(
                repo_root=repo_root,
                base_ref=None,
                relative_directory=relative_directory,
                status="isolated",
                reason="Git repository detected",
                error=str(exc),
            )
        return _WorkspacePlan(
            repo_root=repo_root,
            base_ref=base_ref,
            relative_directory=relative_directory,
            status="isolated",
            reason="Git detached worktree",
        )

    @staticmethod
    def _plan_unit_job(job: Job, captured_base_ref: Optional[str]) -> _WorkspacePlan:
        """Plan a branch-mode job: repository identity, branch, fixed path and base."""
        spec = job.workspace
        assert spec is not None and spec.branch_mode and spec.unit is not None and spec.root is not None

        def failed(error: str, repo_root: Optional[Path] = None) -> _WorkspacePlan:
            return _WorkspacePlan(
                repo_root=repo_root, base_ref=None, relative_directory=Path(),
                status=WORKSPACE_STATUS_UNIT, reason="Git unit worktree", error=error,
            )

        try:
            repo_root = detect_repo_root(job.declared_directory)
        except WorkspaceError as exc:
            return failed(str(exc))
        if repo_root is None:
            return failed(f"workspace mode: branch needs a Git repository at {job.declared_directory}")
        try:
            relative_directory = job.declared_directory.relative_to(repo_root)
        except ValueError as exc:
            return failed(f"declared directory is outside detected repository root: {exc}", repo_root)
        try:
            repository = repository_identity(repo_root)
        except UnitError as exc:
            return failed(str(exc), repo_root)
        branch = spec.unit_branch
        checked = _git_result(
            ("check-ref-format", "--branch", branch), cwd=repo_root, timeout_s=GIT_PROBE_TIMEOUT_S
        )
        if checked is None or checked.returncode != 0:
            return failed(f"invalid unit branch name {branch!r}: {_git_detail(checked)}", repo_root)
        try:
            base_ref = captured_base_ref or _branch_exists(repository, branch)
            if base_ref is None:
                assert spec.base_ref is not None  # WorkspaceSpec requires it in branch mode
                base_ref = resolve_unit_base_ref(repo_root, spec.base_ref)
        except WorkspaceError as exc:
            return failed(str(exc), repo_root)
        unit = _UnitPlan(
            repository=repository,
            unit=spec.unit,
            branch=branch,
            path=unit_worktree_path(spec.root, repository, spec.unit),
            registry_root=spec.root,
        )
        return _WorkspacePlan(
            repo_root=repo_root,
            base_ref=base_ref,
            relative_directory=relative_directory,
            status=WORKSPACE_STATUS_UNIT,
            reason=f"Git unit worktree (unit {spec.unit}, branch {branch})",
            unit=unit,
        )

    def _plan(self, job: Job) -> _WorkspacePlan:
        plan = self._plans.get(job.id)
        if plan is None:
            plan = self._plan_job(job, {})
            self._plans[job.id] = plan
            self._jobs[job.id] = job
        return plan

    def is_unit_job(self, job_id: str) -> bool:
        """Whether the job ``job_id`` was planned as a branch-mode unit job."""
        plan = self._plans.get(job_id)
        return plan is not None and plan.status == WORKSPACE_STATUS_UNIT

    def working_directory(self, job: Job, workspace_path: Optional[str | Path]) -> Path:
        """The execution cwd of an attempt whose workspace is at ``workspace_path``.

        Without a workspace path it is the job's declared directory; with one it
        is that path plus the declared repository subdirectory.
        """
        if workspace_path is None:
            return Path(job.declared_directory).expanduser().resolve()
        plan = self._plan(job)
        return (Path(workspace_path).expanduser().resolve() / plan.relative_directory).resolve()

    @property
    def base_refs(self) -> dict[str, str]:
        """Return resolved base refs suitable for durable run storage."""
        return {
            job_id: plan.base_ref
            for job_id, plan in self._plans.items()
            if plan.status in {"isolated", WORKSPACE_STATUS_UNIT}
            and plan.error is None
            and plan.base_ref is not None
        }

    def _attempt_path(self, job: Job, attempt_no: int) -> Path:
        """Return a unique and root-confined path for one attempt."""
        if attempt_no <= 0:
            raise ValueError("attempt_no must be positive")
        candidate = (
            self.workspace_root
            / _job_path_component(job.id)
            / f"attempt-{attempt_no}-{uuid.uuid4().hex}"
        ).resolve()
        try:
            relative = candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise WorkspaceCreationError(
                f"workspace path escapes the configured workspace root: {candidate}"
            ) from exc
        if not relative.parts:
            raise WorkspaceCreationError("workspace path must be below its root")
        return candidate

    def prepare(self, job: Job, attempt_no: int) -> WorkspaceResolution:
        """Create the worktree for an attempt or report explicit no-isolation."""
        path = self.allocate_path(job, attempt_no)
        return self.create(job, workspace_path=path)

    def allocate_path(self, job: Job, attempt_no: int) -> Optional[Path]:
        """Allocate a root-confined path without creating a Git worktree.

        A branch-mode job always gets its unit's fixed path; an isolated job a
        new unique path per call; a non-isolated job ``None``.
        """
        plan = self._plan(job)
        if plan.error is not None:
            raise WorkspaceCreationError(plan.error)
        if plan.status == "none":
            return None
        if plan.unit is not None:
            if attempt_no <= 0:
                raise ValueError("attempt_no must be positive")
            return plan.unit.path
        if plan.repo_root is None or plan.base_ref is None:
            raise WorkspaceCreationError("Git workspace plan is incomplete")
        return self._attempt_path(job, attempt_no)

    def create(
        self, job: Job, *, workspace_path: Optional[str | Path]
    ) -> WorkspaceResolution:
        """Create the Git worktree at a previously durable path.

        For a branch-mode job this acquires the unit on first use (refused,
        as :class:`WorkspaceCreationError`, while another run owns it or it is
        held) and creates, reuses or rehydrates its worktree at the fixed
        path, which ``workspace_path`` must be.
        """
        plan = self._plan(job)
        if plan.error is not None:
            raise WorkspaceCreationError(plan.error)
        if plan.unit is not None:
            return self._create_unit(job, plan, workspace_path)
        if plan.status == "none":
            if workspace_path is not None:
                raise WorkspaceCreationError(
                    "a non-isolated workspace cannot have an allocated path"
                )
            return WorkspaceResolution(
                path=None,
                base_ref=None,
                repo_root=None,
                status="none",
                reason=plan.reason,
                relative_directory=plan.relative_directory,
            )
        if plan.repo_root is None or plan.base_ref is None:
            raise WorkspaceCreationError("Git workspace plan is incomplete")
        if workspace_path is None:
            raise WorkspaceCreationError("an isolated workspace requires an allocated path")
        path = Path(workspace_path).expanduser().resolve()
        created = create_worktree(plan.repo_root, path, plan.base_ref)
        return WorkspaceResolution(
            path=created,
            base_ref=plan.base_ref,
            repo_root=plan.repo_root,
            status="isolated",
            reason=plan.reason,
            relative_directory=plan.relative_directory,
        )


    def _create_unit(
        self, job: Job, plan: _WorkspacePlan, workspace_path: Optional[str | Path]
    ) -> WorkspaceResolution:
        unit = plan.unit
        assert unit is not None
        if workspace_path is None or Path(workspace_path).expanduser().resolve() != unit.path:
            raise WorkspaceCreationError(
                f"a unit worktree lives at its fixed path {unit.path}, not {workspace_path}"
            )
        self._acquire(job, plan)
        head = _ensure_unit_worktree(unit, plan.base_ref)
        return WorkspaceResolution(
            path=unit.path,
            base_ref=head,
            repo_root=plan.repo_root,
            status=WORKSPACE_STATUS_UNIT,
            reason=plan.reason,
            relative_directory=plan.relative_directory,
            unit=unit.unit,
            branch=unit.branch,
        )

    def _acquire(self, job: Job, plan: _WorkspacePlan) -> UnitLease:
        """Acquire the job's unit once; a ``writer_held`` unit is re-finalized first."""
        unit = plan.unit
        assert unit is not None
        lease = self._leases.get(job.id)
        if lease is not None:
            return lease
        if self.run_id is None:
            raise WorkspaceCreationError("a branch-mode workspace needs the manager's run_id")
        try:
            registry = UnitRegistry(unit.registry_root)
            record = registry.get(unit.repository, unit.unit)
            if record is not None and record.state is UnitState.WRITER_HELD:
                finalize_unit(registry, unit.repository, unit.unit, timeout_s=_finalize_budget(job))
            lease = acquire_unit(
                registry,
                repository=unit.repository,
                unit=unit.unit,
                branch=unit.branch,
                worktree=unit.path,
                run_id=self.run_id,
                job_id=job.id,
                base_sha=plan.base_ref,
            )
        except UnitError as exc:
            raise WorkspaceCreationError(f"unit {unit.unit!r}: {exc}") from exc
        self._leases[job.id] = lease
        return lease

    def hold(self, job: Job) -> Optional[Path]:
        """Own a branch-mode job's unit and its worktree outside an attempt.

        Used before a contract continuation, which runs in the recorded
        worktree without a reservation. Returns the worktree root, or ``None``
        (doing nothing) for a job that is not branch mode. Raises
        :class:`WorkspaceCreationError` like :meth:`create`, including for a
        branch-mode job whose plan failed (it has no unit plan).
        """
        plan = self._plan(job)
        if plan.status != WORKSPACE_STATUS_UNIT:
            return None
        if plan.error is not None:
            raise WorkspaceCreationError(plan.error)
        assert plan.unit is not None  # a branch-mode plan without an error has one
        self._acquire(job, plan)
        _ensure_unit_worktree(plan.unit, plan.base_ref)
        return plan.unit.path

    def finalize_unit_run(
        self,
        job_id: str,
        *,
        writers_stopped: bool,
        processes: Sequence[DetachedSurvivor] = (),
    ) -> Optional[UnitFinalizeResult]:
        """End the run of the job's unit (:func:`~llm_kit.jobs.units.finalize_unit`).

        Releases this manager's lease. Returns ``None`` when no unit was
        acquired for the job in this run. The commit budget is the job's
        ``session_finalize_s`` (default 120 s).
        """
        lease = self._leases.pop(job_id, None)
        if lease is None:
            return None
        plan = self._plans[job_id]
        assert plan.unit is not None
        return finalize_unit(
            UnitRegistry(plan.unit.registry_root),
            lease.repository,
            lease.unit,
            lease=lease,
            writers_stopped=writers_stopped,
            processes=processes,
            timeout_s=_finalize_budget(self._jobs[job_id]),
        )


def _finalize_budget(job: Job) -> float:
    """The end-of-run commit budget of ``job``: its ``session_finalize_s`` bound."""
    return SessionOptions.from_job_options(job.options).bounds.finalize_s


def prepare_workspace(
    job: Job, workspace_root: Path, attempt_no: int = 1
) -> WorkspaceResolution:
    """Prepare one workspace using facts observed at this call's run start.

    For a non-branch job (a branch-mode job needs a run: use
    :class:`WorkspaceManager` with ``run_id``). Raises
    :class:`WorkspaceCreationError` as :meth:`WorkspaceManager.prepare`.
    """
    if job.workspace is not None and job.workspace.branch_mode:
        raise WorkspaceCreationError(
            "prepare_workspace does not finalize; a branch-mode job runs through a "
            "WorkspaceManager with a run_id and BranchWorkspaceHook"
        )
    return WorkspaceManager(workspace_root, (job,)).prepare(job, attempt_no)


def capture_base_refs(jobs: Sequence[Job]) -> dict[str, str]:
    """Observe job base refs without creating any worktrees."""
    return WorkspaceManager(Path.cwd(), jobs).base_refs


def _job_process_evidence(store: JobStore, run_id: str, job_id: str) -> tuple[DetachedSurvivor, ...]:
    """Every process identity the job's agent attempts recorded in ``cleanup.json``.

    The leader, each attached member and each detached survivor of every
    attempt and reservation of the job in this run, alive or not (callers
    re-check liveness). Completion-mode attempts record none.
    """
    directories: list[Path] = []
    for item in (*store.list_attempts(run_id, job_id), *store.list_reservations(run_id, job_id)):
        if item.session_dir is not None and item.session_dir not in directories:
            directories.append(item.session_dir)
    found: list[DetachedSurvivor] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        record = read_cleanup_record(SessionPaths(directory))
        if record is None:
            continue
        found.extend(DetachedSurvivor(identity) for identity in (record.leader, *record.members))
        found.extend(record.detached_survivors)
    unique: list[DetachedSurvivor] = []
    for item in found:
        if all(item.identity != seen.identity for seen in unique):
            unique.append(item)
    return tuple(unique)


class BranchWorkspaceHook:
    """The :class:`~llm_kit.completion.session_types.SessionWorkspaceHook` of branch mode.

    ``jobs/run.py`` calls it once per branch-mode job when the job's run ends
    for ANY reason, after every attempt and the contract. With
    ``writers_stopped`` it commits every remaining change to the unit branch
    (``WIP: <unit> run <n>``) and parks the unit (``clean``/``committed``), or
    holds it (``dirty_held``) when the commit fails. Without it nothing is
    committed: the unit becomes ``writer_held``, waiting on the detached
    survivors and on every identity the job's attempts recorded in
    ``cleanup.json``, and the result is ``writer_held`` with the reason in
    ``detail``. A job whose run acquired no unit gets ``not_applicable``.
    """

    def __init__(self, manager: WorkspaceManager, store: JobStore) -> None:
        self.manager = manager
        self.store = store

    def finalize_run(
        self,
        *,
        run_id: str,
        job_id: str,
        working_directory: Path,
        writers_stopped: bool,
        detached_survivors: Sequence[DetachedSurvivor],
        final_state: Optional[str],
        stop_kind: Optional[StopKind],
    ) -> WorkspaceFinalizeResult:
        """Commit the unit's remaining changes, or hold the unit; see the class docstring."""
        processes: tuple[DetachedSurvivor, ...] = tuple(detached_survivors)
        if not writers_stopped:
            processes = processes + _job_process_evidence(self.store, run_id, job_id)
        result = self.manager.finalize_unit_run(
            job_id, writers_stopped=writers_stopped, processes=processes
        )
        if result is None:
            return WorkspaceFinalizeResult(
                status=WorkspaceFinalizeStatus.NOT_APPLICABLE,
                detail="no unit was acquired for this job in this run",
            )
        return _hook_result(result)


def _hook_result(result: UnitFinalizeResult) -> WorkspaceFinalizeResult:
    if result.status is UnitFinalizeStatus.COMMITTED:
        return WorkspaceFinalizeResult(
            status=WorkspaceFinalizeStatus.COMMITTED, commit_sha=result.commit_sha
        )
    if result.status is UnitFinalizeStatus.CLEAN:
        return WorkspaceFinalizeResult(status=WorkspaceFinalizeStatus.CLEAN)
    if result.status is UnitFinalizeStatus.DIRTY_HELD:
        return WorkspaceFinalizeResult(status=WorkspaceFinalizeStatus.DIRTY_HELD, detail=result.detail)
    if result.status is UnitFinalizeStatus.WRITER_HELD:
        return WorkspaceFinalizeResult(status=WorkspaceFinalizeStatus.WRITER_HELD, detail=result.detail)
    # INDETERMINATE comes only from crash recovery (no lease); the hook holds one.
    raise UnitError(f"end-of-run finalization returned {result.status.value}: {result.detail}")


@dataclass(frozen=True)
class UnitRecoveryEntry:
    """What :func:`recover_run_units` did for one branch-mode job.

    ``outcome`` is a :class:`~llm_kit.jobs.units.UnitFinalizeStatus` value
    when finalization ran, else ``skipped``; ``state`` is the unit's state
    afterwards (``None`` when there is no record); ``detail`` says why.
    """

    job_id: str
    unit: str
    outcome: str
    state: Optional[str]
    detail: Optional[str] = None

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible entry."""
        return {
            "job_id": self.job_id,
            "unit": self.unit,
            "outcome": self.outcome,
            "state": self.state,
            "detail": self.detail,
        }


def recover_run_units(
    store: JobStore, run_id: str, *, job_id: Optional[str] = None
) -> tuple[UnitRecoveryEntry, ...]:
    """Finalize the units a run's branch-mode jobs still own (the crash path).

    A runner ``finally`` cannot guarantee the end-of-run commit after a hard
    cancel or a crash, and an unexpected child exit is settled ``finished``
    without any cancellation, so this inspects every branch-mode job of the
    run (or only ``job_id``), whether or not the job is terminal. A unit
    owned by this run and job is finalized without a lease
    (:func:`~llm_kit.jobs.units.finalize_unit`): an ``active`` unit whose
    owner process is gone is committed, or stays ``active`` and
    ``indeterminate`` while a process its attempts recorded in
    ``cleanup.json`` survives; a ``writer_held`` unit is re-checked. A unit
    whose owner process is alive, or that another run owns, is skipped.

    Called by ``llm-kit jobs cancel`` after the ledger cancellation and by
    every run and resume pass before dispatch. Raises
    :class:`~llm_kit.jobs.units.UnitError` when liveness cannot be checked
    (no psutil) or the registry cannot be read.
    """
    entries: list[UnitRecoveryEntry] = []
    records = store.list_jobs(run_id)
    if job_id is not None:
        records = [record for record in records if record.id == job_id]
    for record in records:
        spec = record.job.workspace
        if spec is None or not spec.branch_mode:
            continue
        assert spec.unit is not None and spec.root is not None

        def entry(outcome: str, state: Optional[str], detail: Optional[str]) -> UnitRecoveryEntry:
            return UnitRecoveryEntry(record.id, str(spec.unit), outcome, state, detail)

        if not (spec.root / REGISTRY_FILENAME).is_file():
            entries.append(entry("skipped", None, f"no unit registry under {spec.root}"))
            continue
        repo_root = detect_repo_root(record.job.declared_directory)
        if repo_root is None:
            entries.append(entry("skipped", None, "the job's directory is not a Git repository"))
            continue
        repository = repository_identity(repo_root)
        registry = UnitRegistry(spec.root, create=False)
        unit = registry.get(repository, spec.unit)
        if unit is None:
            entries.append(entry("skipped", None, "the unit was never acquired"))
            continue
        if (unit.owner_run_id, unit.owner_job_id) != (run_id, record.id):
            entries.append(entry("skipped", unit.state.value, f"owned by run {unit.owner_run_id!r}"))
            continue
        if unit.state not in (UnitState.ACTIVE, UnitState.WRITER_HELD):
            entries.append(entry("skipped", unit.state.value, "nothing to finalize"))
            continue
        try:
            result = finalize_unit(
                registry,
                repository,
                spec.unit,
                processes=_job_process_evidence(store, run_id, record.id),
                timeout_s=_finalize_budget(record.job),
            )
        except UnitOwnershipError as exc:
            entries.append(entry("skipped", unit.state.value, str(exc)))
            continue
        entries.append(entry(result.status.value, result.record.state.value, result.detail))
    return tuple(entries)


@dataclass(frozen=True)
class WorkspaceGCEntry:
    """One workspace action reported by garbage collection."""

    run_id: str
    job_id: str
    attempt_id: int
    workspace: Path
    reason: str

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible GC action."""
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "workspace": str(self.workspace),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkspaceGCReport:
    """The removed, refused and intentionally skipped workspace actions."""

    runs: tuple[str, ...]
    accepted_only: bool
    removed: tuple[WorkspaceGCEntry, ...] = ()
    refused: tuple[WorkspaceGCEntry, ...] = ()
    skipped: tuple[WorkspaceGCEntry, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible GC report."""
        return {
            "runs": list(self.runs),
            "accepted_only": self.accepted_only,
            "removed": [entry.to_mapping() for entry in self.removed],
            "refused": [entry.to_mapping() for entry in self.refused],
            "skipped": [entry.to_mapping() for entry in self.skipped],
            "counts": {
                "removed": len(self.removed),
                "refused": len(self.refused),
                "skipped": len(self.skipped),
            },
        }


def _workspace_repo_root(workspace: Path) -> tuple[Optional[Path], Optional[str]]:
    """Find the Git repository root that owns a recorded worktree."""
    result = _git_result(
        ("rev-parse", "--show-toplevel"), cwd=workspace, timeout_s=GIT_PROBE_TIMEOUT_S
    )
    if result is None or result.returncode != 0:
        return None, f"workspace is not a readable Git worktree: {_git_detail(result)}"
    raw_root = result.stdout.strip()
    if not raw_root:
        return None, "workspace Git root was empty"
    return Path(raw_root).expanduser().resolve(), None


def _registered_worktrees(repo_root: Path) -> tuple[set[Path], Optional[str]]:
    """Read the registered worktree paths from the owning repository."""
    result = _git_result(
        ("worktree", "list", "--porcelain"),
        cwd=repo_root,
        timeout_s=GIT_PROBE_TIMEOUT_S,
    )
    if result is None or result.returncode != 0:
        return set(), f"could not list Git worktrees: {_git_detail(result)}"
    paths = {
        Path(line[len("worktree ") :]).expanduser().resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }
    return paths, None


def _reclaim_workspace(
    attempt_workspace: Path, run: RunRecord, *, force: bool = False
) -> Optional[str]:
    """Return a refusal reason or remove one registered worktree."""
    if run.workspace_root is None:
        return "run has no recorded workspace root"
    workspace_root = run.workspace_root.resolve()
    workspace = attempt_workspace.expanduser().resolve()
    try:
        relative = workspace.relative_to(workspace_root)
    except ValueError:
        return "workspace is outside the run workspace root"
    if not relative.parts:
        return "workspace path is the run workspace root"
    if not workspace.is_dir():
        return "workspace directory does not exist"

    repo_root, refusal = _workspace_repo_root(workspace)
    if refusal is not None or repo_root is None:
        return refusal or "workspace repository root could not be detected"
    status = _git_result(
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        cwd=workspace,
    )
    if status is None or status.returncode != 0:
        return f"could not inspect workspace status: {_git_detail(status)}"
    if status.stdout.strip() and not force:
        return "worktree is dirty"
    registered, refusal = _registered_worktrees(repo_root)
    if refusal is not None:
        return refusal
    if workspace not in registered:
        return "workspace is not a registered Git worktree"

    removal_args: tuple[str, ...] = ("worktree", "remove")
    if force:
        removal_args += ("--force",)
    # Remove from outside the worktree: a process whose current directory is
    # the directory being deleted keeps it open on Windows ("Permission
    # denied"), so address the owning repository by its common Git directory
    # and run from the worktree's parent.
    common = _git_result(
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        cwd=workspace,
        timeout_s=GIT_PROBE_TIMEOUT_S,
    )
    if common is None or common.returncode != 0 or not common.stdout.strip():
        return f"could not locate the owning Git directory: {_git_detail(common)}"
    removed = _git_result(
        ("--git-dir", common.stdout.strip(), *removal_args, str(workspace)),
        cwd=workspace.parent,
    )
    if removed is None or removed.returncode != 0:
        return f"git worktree removal failed: {_git_detail(removed)}"
    return None


_UNIT_SKIP_REASON = "branch-mode unit worktree: the unit registry decides its lifecycle"


def _discover_orphan_worktrees(
    run: RunRecord, recorded_workspaces: set[Path]
) -> tuple[Path, ...]:
    """Find registered worktrees below the run root that have no attempt row.

    Unit worktrees recorded in a unit registry at that root are not orphans.
    """
    if run.workspace_root is None or not run.workspace_root.is_dir():
        return ()
    units = {record.worktree for record in list_units(run.workspace_root)}
    candidates: set[Path] = set()
    for marker in run.workspace_root.glob("*/*/.git"):
        if not marker.is_file():
            continue
        candidate = marker.parent.resolve()
        if candidate in recorded_workspaces or candidate in units:
            continue
        repo_root, refusal = _workspace_repo_root(candidate)
        if repo_root is None or refusal is not None:
            continue
        registered, refusal = _registered_worktrees(repo_root)
        if refusal is None and candidate in registered:
            candidates.add(candidate)
    return tuple(sorted(candidates, key=str))


def _registered_workspace_repository(
    workspace: Path, jobs: Sequence[JobRecord]
) -> tuple[Optional[Path], bool, bool]:
    """Find the run repository that still registers a workspace."""
    recorded_workspace = workspace.expanduser().resolve()
    inspected = False
    uninspected = False
    for record in jobs:
        try:
            repo_root = detect_repo_root(record.job.declared_directory)
        except WorkspaceError:
            uninspected = True
            continue
        if repo_root is None:
            continue
        registered, refusal = _registered_worktrees(repo_root)
        if refusal is not None:
            uninspected = True
            continue
        inspected = True
        if recorded_workspace in registered:
            return repo_root, True, uninspected
    return None, inspected, uninspected


def _workspace_registration_state(
    workspace: Path, jobs: Sequence[JobRecord]
) -> Optional[bool]:
    """Check whether a missing workspace is still registered by a run repo."""
    repo_root, inspected, uninspected = _registered_workspace_repository(
        workspace, jobs
    )
    if repo_root is not None:
        return True
    return False if inspected and not uninspected else None


def _prune_interrupted_workspace(
    workspace: Path, jobs: Sequence[JobRecord]
) -> Optional[str]:
    """Prune a stale Git registration left by an interrupted worktree removal."""
    repo_root, inspected, uninspected = _registered_workspace_repository(
        workspace, jobs
    )
    if repo_root is None:
        if inspected and not uninspected:
            return None
        return "could not verify completed Git worktree removal"
    pruned = _git_result(
        ("worktree", "prune", "--expire", "now"), cwd=repo_root
    )
    if pruned is None or pruned.returncode != 0:
        return f"git worktree prune failed: {_git_detail(pruned)}"
    registered, refusal = _registered_worktrees(repo_root)
    if refusal is not None:
        return f"could not verify Git worktree pruning: {refusal}"
    if workspace.expanduser().resolve() in registered:
        return "workspace remains a registered Git worktree after pruning"
    return None


def gc_workspaces(
    store: JobStore | str | Path,
    run_id: Optional[str] = None,
    *,
    accepted_only: bool = False,
    force: bool = False,
) -> WorkspaceGCReport:
    """Reclaim eligible worktrees and report every refusal or skip.

    Examines the attempts and lost reservations of ``run_id`` (or of every
    run in ``store``). Only a terminal job's recorded ``isolated`` worktree is
    removed (clean, unless ``force``; ``accepted_only`` keeps those of jobs
    that were not accepted). A branch-mode ``unit`` worktree is always
    skipped: its path is reused by later runs, so an old attempt must never
    authorize removing it (:func:`~llm_kit.jobs.units.remove_unit_worktree`
    is the removal path). Registered worktrees below a run root with no row
    are reported as refused, never removed.
    """
    store_object = store if isinstance(store, JobStore) else JobStore(store, create=False)
    all_run_ids = tuple(store_object.list_run_ids())
    if run_id is not None and run_id not in all_run_ids:
        store_object.snapshot(run_id)
    run_ids = (run_id,) if run_id is not None else all_run_ids
    snapshots = {
        current_run_id: store_object.snapshot(current_run_id)
        for current_run_id in all_run_ids
    }
    all_recorded_workspaces = {
        attempt.workspace.resolve()
        for snapshot in snapshots.values()
        for attempt in snapshot.attempts
        if attempt.workspace is not None
    }
    all_recorded_workspaces.update(
        reservation.workspace_path.resolve()
        for snapshot in snapshots.values()
        for reservation in snapshot.reservations
        if reservation.workspace_path is not None
    )
    removed: list[WorkspaceGCEntry] = []
    refused: list[WorkspaceGCEntry] = []
    skipped: list[WorkspaceGCEntry] = []
    scanned_roots: set[Path] = set()

    for current_run_id in run_ids:
        snapshot = snapshots[current_run_id]
        jobs = {record.id: record for record in snapshot.jobs}
        for attempt in snapshot.attempts:
            if attempt.workspace is None:
                continue
            if attempt.id is None:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=current_run_id,
                        job_id=attempt.job_id,
                        attempt_id=-1,
                        workspace=attempt.workspace,
                        reason="attempt row has no database id",
                    )
                )
                continue
            job_record = jobs.get(attempt.job_id)
            if job_record is None:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=current_run_id,
                        job_id=attempt.job_id,
                        attempt_id=attempt.id,
                        workspace=attempt.workspace,
                        reason="attempt has no matching job row",
                    )
                )
                continue
            entry = WorkspaceGCEntry(
                run_id=current_run_id,
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                workspace=attempt.workspace,
                reason="",
            )
            if attempt.workspace_status == WORKSPACE_STATUS_UNIT:
                skipped.append(replace(entry, reason=_UNIT_SKIP_REASON))
                continue
            if not job_record.terminal:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=f"job is non-terminal ({job_record.state.value})",
                    )
                )
                continue
            if accepted_only and job_record.state is not JobState.ACCEPTED:
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=f"job is not accepted ({job_record.state.value})",
                    )
                )
                continue
            if attempt.workspace_removed_at is not None or attempt.workspace_status == "removed":
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason="workspace was already removed",
                    )
                )
                continue
            if attempt.workspace_status not in {"isolated", "removing"}:
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason="attempt does not record an isolated worktree",
                    )
                )
                continue

            cleanup_started = attempt.workspace_status == "removing"
            recorded_forced = attempt.workspace_removal_forced
            # A retry keeps a forced intent, even when the new CLI call omits --force.
            effective_force = force or recorded_forced
            if cleanup_started and not attempt.workspace.is_dir():
                registration = _workspace_registration_state(
                    attempt.workspace, snapshot.jobs
                )
                if registration is True:
                    refusal = _prune_interrupted_workspace(
                        attempt.workspace, snapshot.jobs
                    )
                    if refusal is not None:
                        refused.append(
                            WorkspaceGCEntry(
                                run_id=entry.run_id,
                                job_id=entry.job_id,
                                attempt_id=entry.attempt_id,
                                workspace=entry.workspace,
                                reason=refusal,
                            )
                        )
                        continue
                    store_object.record_workspace_removed(
                        attempt.id,
                        at=time.time(),
                        forced=attempt.workspace_removal_forced,
                    )
                    removed.append(
                        WorkspaceGCEntry(
                            run_id=entry.run_id,
                            job_id=entry.job_id,
                            attempt_id=entry.attempt_id,
                            workspace=entry.workspace,
                            reason=(
                                "forced stale Git worktree registration pruned after interrupted removal"
                                if attempt.workspace_removal_forced
                                else "stale Git worktree registration pruned after interrupted removal"
                            ),
                        )
                    )
                    continue
                if registration is None:
                    refused.append(
                        WorkspaceGCEntry(
                            run_id=entry.run_id,
                            job_id=entry.job_id,
                            attempt_id=entry.attempt_id,
                            workspace=entry.workspace,
                            reason="could not verify completed Git worktree removal",
                        )
                    )
                    continue
                store_object.record_workspace_removed(
                    attempt.id,
                    at=time.time(),
                    forced=attempt.workspace_removal_forced,
                )
                removed.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=(
                            "forced worktree removal was already completed"
                            if attempt.workspace_removal_forced
                            else "clean worktree removal was already completed"
                        ),
                    )
                )
                continue
            try:
                store_object.mark_workspace_removing(
                    attempt.id, forced=effective_force
                )
            except StoreError as exc:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=f"could not record workspace removal intent: {exc}",
                    )
                )
                continue

            refusal = _reclaim_workspace(
                attempt.workspace, snapshot.run, force=effective_force
            )
            if refusal is not None:
                if not effective_force:
                    try:
                        store_object.restore_workspace_isolated(attempt.id)
                    except StoreError:
                        pass
                refused.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=refusal,
                    )
                )
                continue
            store_object.record_workspace_removed(
                attempt.id,
                at=time.time(),
                forced=effective_force,
            )
            removed.append(
                WorkspaceGCEntry(
                    run_id=entry.run_id,
                    job_id=entry.job_id,
                    attempt_id=entry.attempt_id,
                    workspace=entry.workspace,
                    reason=(
                        "forced worktree removed"
                        if effective_force
                        else "clean worktree removed"
                    ),
                )
            )

        for reservation in snapshot.reservations:
            if (
                reservation.disposition
                not in {
                    "process_lost",
                    "process_lost_before_invoke",
                    "pre_invoke_failure",
                    "cancelled",
                }
                or reservation.workspace_path is None
            ):
                continue
            reservation_id = reservation.id if reservation.id is not None else -1
            entry = WorkspaceGCEntry(
                run_id=current_run_id,
                job_id=reservation.job_id,
                attempt_id=reservation_id,
                workspace=reservation.workspace_path,
                reason="",
            )
            if reservation.workspace_status == WORKSPACE_STATUS_UNIT:
                skipped.append(replace(entry, reason=_UNIT_SKIP_REASON))
                continue
            if reservation.workspace_removed_at is not None or (
                reservation.workspace_status == "removed"
            ):
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason="workspace was already removed",
                    )
                )
                continue
            if reservation.workspace_status not in {"isolated", "removing"}:
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason="reservation does not record an isolated worktree",
                    )
                )
                continue
            cleanup_started = reservation.workspace_status == "removing"
            recorded_forced = reservation.workspace_removal_forced
            # A retry keeps a forced intent, even when the new CLI call omits --force.
            effective_force = force or recorded_forced
            if cleanup_started and not reservation.workspace_path.is_dir():
                registration = _workspace_registration_state(
                    reservation.workspace_path, snapshot.jobs
                )
                if registration is True:
                    refusal = _prune_interrupted_workspace(
                        reservation.workspace_path, snapshot.jobs
                    )
                    if refusal is not None:
                        refused.append(replace(entry, reason=refusal))
                        continue
                    store_object.record_reservation_workspace_removed(
                        reservation_id,
                        at=time.time(),
                        forced=reservation.workspace_removal_forced,
                    )
                    removed.append(
                        replace(
                            entry,
                            reason=(
                                "forced stale Git worktree registration pruned after interrupted removal"
                                if reservation.workspace_removal_forced
                                else "stale Git worktree registration pruned after interrupted removal"
                            ),
                        )
                    )
                    continue
                if registration is None:
                    refused.append(
                        replace(
                            entry, reason="could not verify completed Git worktree removal"
                        )
                    )
                    continue
                store_object.record_reservation_workspace_removed(
                    reservation_id,
                    at=time.time(),
                    forced=reservation.workspace_removal_forced,
                )
                removed.append(
                    replace(
                        entry,
                        reason=(
                            "forced worktree removal was already completed"
                            if reservation.workspace_removal_forced
                            else "clean worktree removal was already completed"
                        ),
                    )
                )
                continue
            try:
                store_object.mark_reservation_workspace_removing(
                    reservation_id, forced=effective_force
                )
            except StoreError as exc:
                refused.append(
                    replace(
                        entry, reason=f"could not record workspace removal intent: {exc}"
                    )
                )
                continue
            refusal = _reclaim_workspace(
                reservation.workspace_path, snapshot.run, force=effective_force
            )
            if refusal is not None:
                if not effective_force:
                    try:
                        store_object.restore_reservation_workspace_isolated(reservation_id)
                    except StoreError:
                        pass
                refused.append(replace(entry, reason=refusal))
                continue
            store_object.record_reservation_workspace_removed(
                reservation_id,
                at=time.time(),
                forced=effective_force,
            )
            removed.append(
                replace(
                    entry,
                    reason=(
                        "forced worktree removed"
                        if effective_force
                        else "clean worktree removed"
                    ),
                )
            )

        workspace_root = snapshot.run.workspace_root
        if workspace_root is not None:
            resolved_root = workspace_root.resolve()
            if resolved_root not in scanned_roots:
                scanned_roots.add(resolved_root)
                for orphan in _discover_orphan_worktrees(
                    snapshot.run, all_recorded_workspaces
                ):
                    refused.append(
                        WorkspaceGCEntry(
                            run_id=current_run_id,
                            job_id="<unrecorded>",
                            attempt_id=-1,
                            workspace=orphan,
                            reason="registered worktree has no attempt row",
                        )
                    )

    return WorkspaceGCReport(
        runs=tuple(run_ids),
        accepted_only=accepted_only,
        removed=tuple(removed),
        refused=tuple(refused),
        skipped=tuple(skipped),
    )


__all__ = [
    "WORKSPACE_STATUSES",
    "WORKSPACE_STATUS_UNIT",
    "WORKSPACE_REASON_NONE",
    "WORKSPACE_REASON_DECLINED",
    "GIT_PROBE_TIMEOUT_S",
    "GIT_TREE_TIMEOUT_S",
    "WorkspaceError",
    "WorkspaceCreationError",
    "WorkspaceDetectionError",
    "WorkspaceResolution",
    "detect_repo_root",
    "resolve_base_ref",
    "resolve_unit_base_ref",
    "unit_worktree_path",
    "create_worktree",
    "WorkspaceManager",
    "BranchWorkspaceHook",
    "UnitRecoveryEntry",
    "recover_run_units",
    "prepare_workspace",
    "capture_base_refs",
    "WorkspaceGCEntry",
    "WorkspaceGCReport",
    "gc_workspaces",
]
