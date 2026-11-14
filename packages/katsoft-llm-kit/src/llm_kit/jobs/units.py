"""The unit registry of branch-mode workspaces (agent-runs design 6.1, 6.2, 7.2).

A UNIT of work (a task item or a job) owns one local branch and one worktree at
a fixed path. Successive runs of the unit continue the branch in the same
worktree. The registry is the authoritative unit-level lifecycle record. It is
one SQLite file per worktree root, ``<root>/units.sqlite3``
(:data:`REGISTRY_FILENAME`), separate from the per-run job ledgers, with its
own schema version (:data:`REGISTRY_SCHEMA`).

Identity. A unit is identified by the canonical identity of its repository
(the resolved Git common directory, :func:`repository_identity`) and its unit
id (:func:`check_unit_id`). The first registry that acquires a unit records
itself in the repository's own configuration (``llm-kit-unit.<unit>.registry``
in the common directory's ``config``), so a second registry, at another root,
can never acquire the same unit.

States (:class:`UnitState`). ``acquire_unit`` moves a new, ``parked`` or
``removed`` unit to ``active``; ``finalize_unit`` moves ``active`` to
``parked``, ``dirty_held`` or ``writer_held``, and a held unit back to
``parked`` once resolved; ``remove_unit_worktree`` moves ``parked`` to
``removed``:

- ``active``: a run owns the unit (owner run, job, nonce and process identity).
  An active unit whose owner process died is recovered by :func:`finalize_unit`
  without a lease; while processes of the dead run survive it stays active and
  is reported INDETERMINATE (``held_reason`` set).
- ``parked``: no run owns it; the worktree is clean and retained.
- ``dirty_held``: the end-of-run commit failed or the worktree failed a removal
  check; a human resolves it. Exempt from reaping, blocked from reuse.
- ``writer_held``: a writer may still be alive (a detached survivor, or a
  shutdown that was not confirmed); the commit was not made. Re-finalizable
  once every recorded identity is re-checked gone.
- ``removed``: the worktree was removed; the branch is kept, and a later
  acquire rehydrates the worktree at the same path.

Locking. Every operation that changes a unit, its worktree or its branch runs
under an interprocess lock keyed by the repository identity
(:func:`repository_lock`, a :func:`~llm_kit.storage.file_lock.file_lock` on
``<git common dir>/llm-kit/units.lock``), so ``git worktree add``, acquisition,
rehydration, finalization and removal never interleave across processes or
registries. The lock is not reentrant: never nest two of these calls.

Process liveness (owners, survivors) is checked by identity
(``pid`` + creation time) and needs the package's ``local`` extra (psutil);
without it a liveness check raises :class:`UnitError`.

:class:`UnitRecord.to_mapping` is the JSON form of a record; its schema is
``llm_kit/jobs/schemas/unit_record.json`` (:data:`UNIT_RECORD_SCHEMA_FILE`).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional, Sequence

from llm_kit.completion.session_types import DetachedSurvivor, ProcessIdentity, SessionBounds
from llm_kit.storage import connection as _connection
from llm_kit.storage import transaction as _transaction
from llm_kit.storage.file_lock import file_lock
from llm_kit.storage.schema_version import read_schema_version

from . import _supervisor_process

#: The registry file name inside a worktree root.
REGISTRY_FILENAME = "units.sqlite3"
#: The registry's own schema version (independent of the job ledger's).
REGISTRY_SCHEMA = 1
#: The JSON Schema file of :meth:`UnitRecord.to_mapping`, in ``llm_kit/jobs/schemas/``.
UNIT_RECORD_SCHEMA_FILE = "unit_record.json"
#: The version stamped as ``schema`` in :meth:`UnitRecord.to_mapping`.
UNIT_RECORD_VERSION = 1
#: The default budget, in seconds, of one end-of-run commit (``SessionBounds.finalize_s``).
DEFAULT_FINALIZE_S = SessionBounds().finalize_s
#: The end-of-run commit message; ``{unit}`` and ``{run}`` (the unit's run number) are filled in.
WIP_MESSAGE = "WIP: {unit} run {run}"
#: The repository configuration section that records which registry owns a unit.
CLAIM_SECTION = "llm-kit-unit"

_GIT_PROBE_TIMEOUT_S = 30.0
_GIT_TREE_TIMEOUT_S = 900.0
_UNIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_BUSY_TIMEOUT_MS = 30_000


class UnitError(RuntimeError):
    """A unit operation could not be carried out (a Git, registry or liveness failure)."""


class UnitBusyError(UnitError):
    """:func:`acquire_unit` refused: the unit is active, held, or claimed by another registry."""


class UnitOwnershipError(UnitError):
    """A finalization was refused: the caller does not own the unit, or its owner is alive."""


class UnitRemovalRefused(UnitError):
    """:func:`remove_unit_worktree` refused; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnitState(str, Enum):
    """A unit's lifecycle state (design 6.2); see the module docstring."""

    ACTIVE = "active"
    PARKED = "parked"
    DIRTY_HELD = "dirty_held"
    WRITER_HELD = "writer_held"
    REMOVED = "removed"


class UnitFinalizeStatus(str, Enum):
    """What :func:`finalize_unit` did.

    - ``clean``: nothing was left to commit; the unit is parked.
    - ``committed``: remaining changes were committed (``WIP: <unit> run <n>``); parked.
    - ``dirty_held``: the commit failed or left changes; the unit is dirty-held.
    - ``writer_held``: a recorded writer may be alive; nothing was committed.
    - ``indeterminate``: recovery found processes of a dead owner alive; the
      unit stays active and nothing was committed.
    """

    CLEAN = "clean"
    COMMITTED = "committed"
    DIRTY_HELD = "dirty_held"
    WRITER_HELD = "writer_held"
    INDETERMINATE = "indeterminate"


def check_unit_id(value: object) -> str:
    """Return ``value`` when it is a valid unit id, else raise ``ValueError``.

    A unit id is 1-128 characters of ``[A-Za-z0-9._-]`` starting with a letter
    or digit and not containing ``..``. It is one path component of the
    worktree path ``<root>/<repo>/<unit>`` and part of the default branch name
    ``agent/<unit>``.
    """
    if not isinstance(value, str) or _UNIT_ID.fullmatch(value) is None or ".." in value:
        raise ValueError(
            "unit id must be 1-128 characters of [A-Za-z0-9._-], start with a letter or "
            f"digit and not contain '..'; got {value!r}"
        )
    return value


def _git(
    args: Sequence[str], *, cwd: Path, timeout_s: float = _GIT_PROBE_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Run one Git command; raise :class:`UnitError` when it cannot start or times out."""
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnitError(f"git {' '.join(args[:3])} failed to complete in {cwd}: {exc}") from exc


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout).strip()
    return (text or f"git exited with code {result.returncode}")[:2000]


def repository_identity(directory: Path) -> Path:
    """Return the canonical identity of the repository containing ``directory``.

    That is the resolved absolute Git common directory, which every worktree of
    one repository shares. Raises :class:`UnitError` when ``directory`` is not
    inside a Git repository.
    """
    where = Path(directory).expanduser().resolve()
    result = _git(("rev-parse", "--path-format=absolute", "--git-common-dir"), cwd=where)
    if result.returncode != 0 or not result.stdout.strip():
        raise UnitError(f"not a Git repository: {where}: {_detail(result)}")
    return Path(result.stdout.strip()).resolve()


@contextmanager
def repository_lock(repository: Path) -> Iterator[None]:
    """Hold the interprocess lock of one repository identity while the block runs.

    The lock file is ``<repository>/llm-kit/units.lock``, so every registry and
    every process working on that repository shares it. Not reentrant.
    """
    with file_lock(Path(repository) / "llm-kit" / "units.lock"):
        yield


def _identity_document(item: DetachedSurvivor) -> dict[str, object]:
    return {"pid": item.identity.pid, "create_time": item.identity.create_time, "cwd": item.cwd}


def _processes_from_json(text: str) -> tuple[DetachedSurvivor, ...]:
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise UnitError("unit registry held_processes is not a JSON list")
    return tuple(
        DetachedSurvivor(ProcessIdentity(int(item["pid"]), float(item["create_time"])), item["cwd"])
        for item in raw
    )


@dataclass(frozen=True)
class UnitOwner:
    """The run that owns an active unit: its lease ``nonce`` and its process identity."""

    nonce: str
    process: ProcessIdentity


@dataclass(frozen=True)
class UnitRecord:
    """One registry row.

    - ``repository`` / ``unit``: the identity. ``branch``: the RECORDED branch
      (authoritative; validated, never assumed to be ``agent/<unit>``).
      ``worktree``: the fixed path.
    - ``state``: :class:`UnitState`. ``owner``: set exactly while ``active``.
      ``owner_run_id`` / ``owner_job_id``: the current or last owning run.
    - ``run_count``: how many runs acquired the unit (the ``<n>`` of the WIP message).
    - ``held_reason``: why the unit is held (dirty, writer or indeterminate), else ``None``.
    - ``held_processes``: the identities a held unit waits on.
    - ``base_sha``: the commit the branch was created from.
    - ``last_run_ended_at`` / ``last_commit_sha``: when the last run was
      finalized and the branch tip it left.
    - ``merged_tip_sha`` / ``merge_target_sha`` / ``merged_at``: the last
      successful merge (design 7.2), recorded by :func:`record_unit_merge`.
    """

    repository: Path
    unit: str
    branch: str
    worktree: Path
    state: UnitState
    run_count: int
    created_at: float
    updated_at: float
    base_sha: Optional[str] = None
    owner_run_id: Optional[str] = None
    owner_job_id: Optional[str] = None
    owner: Optional[UnitOwner] = None
    held_reason: Optional[str] = None
    held_processes: tuple[DetachedSurvivor, ...] = ()
    last_run_ended_at: Optional[float] = None
    last_commit_sha: Optional[str] = None
    merged_tip_sha: Optional[str] = None
    merge_target_sha: Optional[str] = None
    merged_at: Optional[float] = None

    @property
    def indeterminate(self) -> bool:
        """An active unit that recovery could not finalize because processes survived."""
        return self.state is UnitState.ACTIVE and self.held_reason is not None

    def to_mapping(self) -> dict[str, object]:
        """Return the JSON form (schema ``unit_record.json``)."""
        return {
            "schema": UNIT_RECORD_VERSION,
            "repository": str(self.repository),
            "unit": self.unit,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "state": self.state.value,
            "run_count": self.run_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "base_sha": self.base_sha,
            "owner_run_id": self.owner_run_id,
            "owner_job_id": self.owner_job_id,
            "owner": (
                {
                    "nonce": self.owner.nonce,
                    "pid": self.owner.process.pid,
                    "create_time": self.owner.process.create_time,
                }
                if self.owner is not None
                else None
            ),
            "held_reason": self.held_reason,
            "held_processes": [_identity_document(item) for item in self.held_processes],
            "last_run_ended_at": self.last_run_ended_at,
            "last_commit_sha": self.last_commit_sha,
            "merged_tip_sha": self.merged_tip_sha,
            "merge_target_sha": self.merge_target_sha,
            "merged_at": self.merged_at,
        }


@dataclass(frozen=True)
class UnitLease:
    """Proof of ownership returned by :func:`acquire_unit`; pass it to :func:`finalize_unit`."""

    repository: Path
    unit: str
    nonce: str
    run_no: int
    branch: str
    worktree: Path


@dataclass(frozen=True)
class UnitFinalizeResult:
    """What :func:`finalize_unit` did: ``status``, the resulting ``record``, the
    commit it made (``commit_sha``, set exactly when ``committed``) and ``detail``."""

    status: UnitFinalizeStatus
    record: UnitRecord
    commit_sha: Optional[str] = None
    detail: Optional[str] = None


_SCHEMA_STEPS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE schema_version (version INTEGER NOT NULL)",
        """
        CREATE TABLE units (
            repository TEXT NOT NULL,
            unit TEXT NOT NULL,
            branch TEXT NOT NULL,
            worktree TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN
                ('active', 'parked', 'dirty_held', 'writer_held', 'removed')),
            run_count INTEGER NOT NULL CHECK (run_count >= 0),
            base_sha TEXT,
            owner_run_id TEXT,
            owner_job_id TEXT,
            owner_nonce TEXT,
            owner_pid INTEGER,
            owner_create_time REAL,
            held_reason TEXT,
            held_processes_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_run_ended_at REAL,
            last_commit_sha TEXT,
            merged_tip_sha TEXT,
            merge_target_sha TEXT,
            merged_at REAL,
            PRIMARY KEY (repository, unit),
            CHECK ((state = 'active') = (owner_nonce IS NOT NULL)),
            CHECK ((owner_nonce IS NULL) = (owner_pid IS NULL)),
            CHECK ((owner_nonce IS NULL) = (owner_create_time IS NULL))
        )
        """,
        "INSERT INTO schema_version(version) VALUES (1)",
    ),
)

if len(_SCHEMA_STEPS) != REGISTRY_SCHEMA:  # pragma: no cover - import-time consistency check
    raise RuntimeError("unit registry schema steps are out of step with REGISTRY_SCHEMA")


def _row_to_record(row: sqlite3.Row) -> UnitRecord:
    owner = None
    if row["owner_nonce"] is not None:
        owner = UnitOwner(
            nonce=str(row["owner_nonce"]),
            process=ProcessIdentity(int(row["owner_pid"]), float(row["owner_create_time"])),
        )
    return UnitRecord(
        repository=Path(row["repository"]),
        unit=str(row["unit"]),
        branch=str(row["branch"]),
        worktree=Path(row["worktree"]),
        state=UnitState(row["state"]),
        run_count=int(row["run_count"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        base_sha=row["base_sha"],
        owner_run_id=row["owner_run_id"],
        owner_job_id=row["owner_job_id"],
        owner=owner,
        held_reason=row["held_reason"],
        held_processes=_processes_from_json(row["held_processes_json"]),
        last_run_ended_at=row["last_run_ended_at"],
        last_commit_sha=row["last_commit_sha"],
        merged_tip_sha=row["merged_tip_sha"],
        merge_target_sha=row["merge_target_sha"],
        merged_at=row["merged_at"],
    )


class UnitRegistry:
    """The registry file of one worktree root (``<root>/units.sqlite3``).

    ``create=True`` creates the root and the file when absent and migrates
    the schema; ``create=False`` raises :class:`UnitError` when the file is
    absent. A file with a schema newer than :data:`REGISTRY_SCHEMA` is refused
    with :class:`UnitError` before anything is written. Reads (:meth:`get`,
    :meth:`list`) take no lock; changes go through the module functions,
    which hold :func:`repository_lock`.
    """

    def __init__(self, root: Path, *, create: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / REGISTRY_FILENAME
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise UnitError(f"no unit registry at {self.path}")
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with _connection.connect(
            self.path,
            _connection.ConnectionPolicy(busy_timeout_ms=_BUSY_TIMEOUT_MS),
        ) as conn:
            yield conn

    def _migrate(self) -> None:
        with _connection.connect_probe(self.path, _BUSY_TIMEOUT_MS, set_busy_timeout=False) as probe:
            stored = read_schema_version(probe, as_int=True)
        if stored > REGISTRY_SCHEMA:
            raise UnitError(
                f"unit registry {self.path} has schema version {stored}, newer than this "
                f"llm-kit reads (max {REGISTRY_SCHEMA}); upgrade llm-kit"
            )
        if stored == REGISTRY_SCHEMA:
            return
        with _transaction.write_transaction(self._connect) as conn:
            version = read_schema_version(conn, as_int=True)
            for index in range(version, REGISTRY_SCHEMA):
                for statement in _SCHEMA_STEPS[index]:
                    conn.execute(statement)
                if index > 0:
                    conn.execute("UPDATE schema_version SET version = ?", (index + 1,))

    def schema_version(self) -> int:
        """The stored schema version."""
        with self._connect() as conn:
            return read_schema_version(conn, as_int=True)

    def get(self, repository: Path, unit: str) -> Optional[UnitRecord]:
        """Return the record of ``(repository, unit)``, or ``None``."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM units WHERE repository = ? AND unit = ?",
                (str(Path(repository)), unit),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list(self) -> tuple[UnitRecord, ...]:
        """Every record, ordered by repository and unit."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM units ORDER BY repository, unit").fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def _update(self, record: UnitRecord, **changes: object) -> UnitRecord:
        """Write ``changes`` (column -> value) to one row and return the new record."""
        changes["updated_at"] = time.time()
        columns = ", ".join(f"{name} = ?" for name in changes)
        with _transaction.write_transaction(self._connect) as conn:
            cursor = conn.execute(
                f"UPDATE units SET {columns} WHERE repository = ? AND unit = ?",
                (*changes.values(), str(record.repository), record.unit),
            )
            if cursor.rowcount != 1:  # pragma: no cover - every caller read the row under the lock
                raise UnitError(f"unit {record.unit!r} disappeared from {self.path}")
            row = conn.execute(
                "SELECT * FROM units WHERE repository = ? AND unit = ?",
                (str(record.repository), record.unit),
            ).fetchone()
        return _row_to_record(row)

    def _insert(self, **values: object) -> UnitRecord:
        now = time.time()
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with _transaction.write_transaction(self._connect) as conn:
            conn.execute(f"INSERT INTO units ({names}) VALUES ({marks})", tuple(values.values()))
            row = conn.execute(
                "SELECT * FROM units WHERE repository = ? AND unit = ?",
                (values["repository"], values["unit"]),
            ).fetchone()
        return _row_to_record(row)


def _psutil_checked() -> None:
    try:
        _supervisor_process.psutil_module()
    except _supervisor_process.ProcessError as exc:
        raise UnitError(
            "branch-mode units check process liveness by identity and need "
            "katsoft-llm-kit[local] (psutil)"
        ) from exc


def _alive(identity: ProcessIdentity) -> bool:
    _psutil_checked()
    return _supervisor_process.live(
        _supervisor_process.ProcessIdentity(identity.pid, identity.create_time)
    ) is not None


def live_processes(processes: Sequence[DetachedSurvivor]) -> tuple[DetachedSurvivor, ...]:
    """The entries of ``processes`` whose identity (pid and creation time) is still alive."""
    return tuple(item for item in processes if _alive(item.identity))


def _own_identity() -> ProcessIdentity:
    _psutil_checked()
    me = _supervisor_process.identity(os.getpid())
    if me is None:  # pragma: no cover - the current process always exists
        raise UnitError("cannot establish the current process identity")
    return ProcessIdentity(me.pid, me.create_time)


def _claim(repository: Path, unit: str, registry: UnitRegistry) -> None:
    """Record ``registry`` as the unit's registry in the repository, or refuse another's claim."""
    key = f"{CLAIM_SECTION}.{unit}.registry"
    current = _git(("--git-dir", str(repository), "config", "--local", "--get", key), cwd=repository)
    if current.returncode == 0:
        claimed = current.stdout.strip()
        if claimed != str(registry.path):
            raise UnitBusyError(
                f"unit {unit!r} of repository {repository} belongs to another registry: {claimed}"
            )
        return
    if current.returncode != 1:
        raise UnitError(f"could not read the unit claim {key} in {repository}: {_detail(current)}")
    written = _git(
        ("--git-dir", str(repository), "config", "--local", key, str(registry.path)), cwd=repository
    )
    if written.returncode != 0:
        raise UnitError(f"could not record the unit claim {key} in {repository}: {_detail(written)}")


def acquire_unit(
    registry: UnitRegistry,
    *,
    repository: Path,
    unit: str,
    branch: str,
    worktree: Path,
    run_id: str,
    job_id: str,
    base_sha: Optional[str] = None,
) -> UnitLease:
    """Make ``run_id``/``job_id`` (this process) the exclusive owner of a unit.

    Ownership covers reuse, execution, finalization and removal: until
    :func:`finalize_unit` releases it, every other acquisition is refused.
    A new unit is inserted (``run_count`` 1, ``base_sha`` recorded); a
    ``parked`` or ``removed`` unit is reacquired (``run_count`` + 1).

    Raises :class:`UnitBusyError` when the unit is ``active`` (also when its
    owner died: recover it first with :func:`finalize_unit` without a lease),
    ``dirty_held`` or ``writer_held``, or claimed by another registry;
    :class:`UnitError` when the recorded branch or worktree differs from the
    one given (the recorded branch is authoritative), or for a Git failure.
    """
    check_unit_id(unit)
    repository = Path(repository).resolve()
    worktree = Path(worktree).expanduser().resolve()
    if not run_id or not job_id:
        raise ValueError("acquire_unit needs a run_id and a job_id")
    owner = _own_identity()
    nonce = uuid.uuid4().hex
    with repository_lock(repository):
        _claim(repository, unit, registry)
        record = registry.get(repository, unit)
        owner_values: dict[str, object] = dict(
            state=UnitState.ACTIVE.value,
            owner_run_id=run_id,
            owner_job_id=job_id,
            owner_nonce=nonce,
            owner_pid=owner.pid,
            owner_create_time=owner.create_time,
            held_reason=None,
            held_processes_json="[]",
        )
        if record is None:
            record = registry._insert(
                repository=str(repository),
                unit=unit,
                branch=branch,
                worktree=str(worktree),
                run_count=1,
                base_sha=base_sha,
                **owner_values,
            )
            return UnitLease(repository, unit, nonce, record.run_count, branch, worktree)
        if record.branch != branch:
            raise UnitError(
                f"unit {unit!r} records branch {record.branch!r}; the job declares {branch!r}"
            )
        if record.worktree != worktree:
            raise UnitError(
                f"unit {unit!r} records worktree {record.worktree}; the job plans {worktree}"
            )
        if record.state is UnitState.ACTIVE:
            alive = _alive(record.owner.process) if record.owner is not None else False
            raise UnitBusyError(
                f"unit {unit!r} is active, owned by run {record.owner_run_id!r} job "
                f"{record.owner_job_id!r}"
                + (
                    ""
                    if alive
                    else " whose process is gone; recover it first (llm-kit jobs cancel, a "
                    "resume of that run, or finalize_unit without a lease)"
                )
                + (f"; indeterminate: {record.held_reason}" if record.held_reason else "")
            )
        if record.state in (UnitState.DIRTY_HELD, UnitState.WRITER_HELD):
            raise UnitBusyError(
                f"unit {unit!r} is {record.state.value}: {record.held_reason}; reuse is "
                "blocked until it is resolved"
            )
        record = registry._update(record, run_count=record.run_count + 1, **owner_values)
        return UnitLease(repository, unit, nonce, record.run_count, branch, worktree)


@dataclass(frozen=True)
class _CommitOutcome:
    status: UnitFinalizeStatus
    sha: Optional[str]
    detail: Optional[str]


def _end_of_run_commit(
    repository: Path, worktree: Path, branch: str, message: str, timeout_s: float
) -> _CommitOutcome:
    """Commit every remaining change in ``worktree`` to ``branch``, within ``timeout_s``.

    Never raises for a Git failure or an exhausted budget: either is a
    ``dirty_held`` outcome (fail safe).
    """
    deadline = time.monotonic() + timeout_s

    def git(*args: str) -> Optional[subprocess.CompletedProcess[str]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return _git(args, cwd=worktree, timeout_s=remaining)
        except UnitError:
            return None

    def held(detail: str) -> _CommitOutcome:
        return _CommitOutcome(UnitFinalizeStatus.DIRTY_HELD, None, detail)

    try:
        registered = worktree in _registered_worktrees(repository)
    except UnitError as exc:
        return held(str(exc))
    if not registered or not worktree.is_dir():
        # Nothing of the unit can be left uncommitted where no worktree of the
        # repository exists (setup failed before `worktree add`, or the directory
        # was removed): the branch holds everything, and the next acquisition
        # rehydrates the worktree (or reports what occupies its path).
        return _CommitOutcome(
            UnitFinalizeStatus.CLEAN, None, f"no worktree of the repository at {worktree}"
        )
    head = git("symbolic-ref", "-q", "HEAD")
    if head is None:
        return held("the finalization budget ran out (reading HEAD)")
    if head.returncode != 0 or head.stdout.strip() != f"refs/heads/{branch}":
        found = head.stdout.strip() or "a detached HEAD"
        return held(f"HEAD is {found}, not the recorded branch refs/heads/{branch}")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    if status is None or status.returncode != 0:
        return held(
            "the finalization budget ran out (git status)"
            if status is None
            else f"git status failed: {_detail(status)}"
        )
    if status.stdout.strip():
        added = git("add", "-A")
        if added is None or added.returncode != 0:
            return held(
                "the finalization budget ran out (git add)"
                if added is None
                else f"git add failed: {_detail(added)}"
            )
        committed = git("commit", "-q", "-m", message)
        if committed is None or committed.returncode != 0:
            return held(
                "the finalization budget ran out (git commit)"
                if committed is None
                else f"the end-of-run commit failed: {_detail(committed)}"
            )
        after = git("status", "--porcelain=v1", "--untracked-files=all")
        if after is None or after.returncode != 0 or after.stdout.strip():
            return held("changes remain in the worktree after the end-of-run commit")
    tip = git("rev-parse", "HEAD")
    if tip is None or tip.returncode != 0 or not tip.stdout.strip():
        return held("could not read the branch tip after finalization")
    sha = tip.stdout.strip()
    if status.stdout.strip():
        return _CommitOutcome(UnitFinalizeStatus.COMMITTED, sha, None)
    return _CommitOutcome(UnitFinalizeStatus.CLEAN, sha, None)


def _processes_json(processes: Sequence[DetachedSurvivor]) -> str:
    unique: list[DetachedSurvivor] = []
    for item in processes:
        if all(item.identity != seen.identity for seen in unique):
            unique.append(item)
    return json.dumps([_identity_document(item) for item in unique], sort_keys=True)


def _describe(processes: Sequence[DetachedSurvivor]) -> str:
    return ", ".join(
        f"pid {item.identity.pid} (created {item.identity.create_time}, cwd {item.cwd})"
        for item in processes
    )


def _check_budget(timeout_s: float) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError(f"timeout_s must be a number of seconds, got {timeout_s!r}")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError(f"timeout_s must be finite and positive, got {timeout_s!r}")
    return float(timeout_s)


def finalize_unit(
    registry: UnitRegistry,
    repository: Path,
    unit: str,
    *,
    lease: Optional[UnitLease] = None,
    writers_stopped: bool = True,
    processes: Sequence[DetachedSurvivor] = (),
    resolve_dirty: bool = False,
    timeout_s: float = DEFAULT_FINALIZE_S,
) -> UnitFinalizeResult:
    """End a run of a unit: commit what remains to its branch, or hold it.

    With ``lease`` (the end of the owning run): the unit must be ``active``
    with that nonce, else :class:`UnitOwnershipError`. When
    ``writers_stopped`` is false, or any of ``processes`` (identities that may
    write the worktree: detached survivors, unconfirmed members) is alive,
    nothing is committed and the unit becomes ``writer_held``, waiting on
    ``processes``. Otherwise every remaining change is committed as
    ``WIP: <unit> run <n>`` (Git hooks run) and the unit becomes ``parked``
    (``clean`` or ``committed``), or ``dirty_held`` when HEAD is not on the
    recorded branch, the commit fails, changes remain, or ``timeout_s`` runs
    out.

    Without ``lease`` (deferred and crash-path finalization):

    - ``writer_held``: the held identities (and ``processes``) are re-checked;
      while any is alive it stays ``writer_held``, else the commit is made.
    - ``active`` whose owner process is gone (crash recovery): when any of
      ``processes`` is alive the unit stays ``active`` and the result is
      ``indeterminate`` (``held_reason`` names them); else the commit is
      made. An owner that is alive raises :class:`UnitOwnershipError`.
    - ``dirty_held``: the commit is retried only with ``resolve_dirty``
      (a human's decision), else :class:`UnitOwnershipError`.
    - ``parked`` / ``removed``: nothing to do; returns ``clean`` unchanged.

    Raises :class:`UnitError` for an unknown unit or a liveness check without
    psutil; ``ValueError`` for a bad ``timeout_s``.
    """
    budget = _check_budget(timeout_s)
    repository = Path(repository).resolve()
    with repository_lock(repository):
        record = registry.get(repository, unit)
        if record is None:
            raise UnitError(f"unknown unit {unit!r} of repository {repository} in {registry.path}")
        held = list(processes)
        if lease is not None:
            if record.state is not UnitState.ACTIVE or record.owner is None or (
                record.owner.nonce != lease.nonce
            ):
                raise UnitOwnershipError(
                    f"unit {unit!r} is not held by this lease (state {record.state.value})"
                )
        elif record.state in (UnitState.PARKED, UnitState.REMOVED):
            return UnitFinalizeResult(UnitFinalizeStatus.CLEAN, record, detail=f"already {record.state.value}")
        elif record.state is UnitState.DIRTY_HELD:
            if not resolve_dirty:
                raise UnitOwnershipError(
                    f"unit {unit!r} is dirty_held ({record.held_reason}); pass resolve_dirty "
                    "to retry the commit"
                )
        elif record.state is UnitState.WRITER_HELD:
            held = list(record.held_processes) + held
        else:  # active, no lease: crash recovery
            if record.owner is not None and _alive(record.owner.process):
                raise UnitOwnershipError(
                    f"unit {unit!r} is active and its owner process (pid {record.owner.process.pid}) "
                    "is alive"
                )
            alive = live_processes(held)
            if alive:
                reason = f"processes of the dead owning run survive: {_describe(alive)}"
                record = registry._update(
                    record, held_reason=reason, held_processes_json=_processes_json(alive)
                )
                return UnitFinalizeResult(UnitFinalizeStatus.INDETERMINATE, record, detail=reason)
        if lease is not None and not writers_stopped:
            reason = "workspace writers were not confirmed stopped" + (
                f"; waiting on {_describe(held)}" if held else ""
            )
            return _writer_held(registry, record, reason, held)
        alive = live_processes(held)
        if alive:
            return _writer_held(
                registry, record, f"processes that may write the worktree are alive: {_describe(alive)}",
                held,
            )
        message = WIP_MESSAGE.format(unit=unit, run=record.run_count)
        outcome = _end_of_run_commit(repository, record.worktree, record.branch, message, budget)
        ended = time.time()
        common: dict[str, object] = dict(
            owner_nonce=None, owner_pid=None, owner_create_time=None, held_processes_json="[]",
            last_run_ended_at=ended,
        )
        if outcome.status is UnitFinalizeStatus.DIRTY_HELD:
            record = registry._update(
                record, state=UnitState.DIRTY_HELD.value, held_reason=outcome.detail, **common
            )
            return UnitFinalizeResult(outcome.status, record, detail=outcome.detail)
        record = registry._update(
            record, state=UnitState.PARKED.value, held_reason=None,
            last_commit_sha=outcome.sha or record.last_commit_sha, **common,
        )
        return UnitFinalizeResult(
            outcome.status,
            record,
            commit_sha=outcome.sha if outcome.status is UnitFinalizeStatus.COMMITTED else None,
            detail=outcome.detail,
        )


def _writer_held(
    registry: UnitRegistry, record: UnitRecord, reason: str, held: Sequence[DetachedSurvivor]
) -> UnitFinalizeResult:
    record = registry._update(
        record,
        state=UnitState.WRITER_HELD.value,
        held_reason=reason,
        held_processes_json=_processes_json(held),
        owner_nonce=None,
        owner_pid=None,
        owner_create_time=None,
        last_run_ended_at=time.time(),
    )
    return UnitFinalizeResult(UnitFinalizeStatus.WRITER_HELD, record, detail=reason)


def _registered_worktrees(repository: Path) -> dict[Path, Optional[str]]:
    """Registered worktree paths of ``repository`` -> their ``branch`` line (``refs/heads/..``)."""
    listed = _git(("--git-dir", str(repository), "worktree", "list", "--porcelain"), cwd=repository)
    if listed.returncode != 0:
        raise UnitError(f"could not list the worktrees of {repository}: {_detail(listed)}")
    found: dict[Path, Optional[str]] = {}
    current: Optional[Path] = None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).expanduser().resolve()
            found[current] = None
        elif line.startswith("branch ") and current is not None:
            found[current] = line[len("branch "):]
    return found


def remove_unit_worktree(
    registry: UnitRegistry,
    repository: Path,
    unit: str,
    *,
    timeout_s: float = _GIT_TREE_TIMEOUT_S,
) -> UnitRecord:
    """Remove a unit's worktree (keeping its branch) and mark the unit ``removed``.

    Under the repository lock it refuses, raising :class:`UnitRemovalRefused`,
    when the unit is ``active``, ``dirty_held`` or ``writer_held``, or its
    path is not a registered worktree. A worktree that fails a content check
    (``git status --porcelain`` not empty, HEAD not symbolic on the RECORDED
    branch, HEAD not equal to that branch's tip) is marked ``dirty_held``
    with the reason and refused. A unit already ``removed`` is returned
    unchanged; a worktree already deleted from disk whose registration
    lingers is pruned and the unit marked ``removed`` (a crash between Git
    and the registry). The branch is never deleted here.
    """
    _check_budget(timeout_s)
    repository = Path(repository).resolve()
    with repository_lock(repository):
        record = registry.get(repository, unit)
        if record is None:
            raise UnitError(f"unknown unit {unit!r} of repository {repository} in {registry.path}")
        if record.state is UnitState.REMOVED:
            return record
        if record.state is not UnitState.PARKED:
            raise UnitRemovalRefused(
                f"unit {unit!r} is {record.state.value}"
                + (f": {record.held_reason}" if record.held_reason else "")
            )
        registered = _registered_worktrees(repository)
        path = record.worktree
        if not path.exists():
            if path in registered:
                pruned = _git(("--git-dir", str(repository), "worktree", "prune", "--expire", "now"),
                              cwd=repository)
                if pruned.returncode != 0 or path in _registered_worktrees(repository):
                    raise UnitRemovalRefused(f"could not prune the stale registration of {path}")
            return registry._update(record, state=UnitState.REMOVED.value)
        if path not in registered:
            raise UnitRemovalRefused(f"{path} is not a registered worktree of {repository}")

        def hold(reason: str) -> UnitRemovalRefused:
            registry._update(record, state=UnitState.DIRTY_HELD.value, held_reason=reason)
            return UnitRemovalRefused(reason)

        status = _git(("status", "--porcelain=v1", "--untracked-files=all"), cwd=path,
                      timeout_s=timeout_s)
        if status.returncode != 0:
            raise UnitRemovalRefused(f"could not inspect the worktree: {_detail(status)}")
        if status.stdout.strip():
            raise hold("the worktree is dirty")
        head = _git(("symbolic-ref", "-q", "HEAD"), cwd=path)
        if head.returncode != 0 or head.stdout.strip() != f"refs/heads/{record.branch}":
            found = head.stdout.strip() or "a detached HEAD"
            raise hold(f"HEAD is {found}, not the recorded branch refs/heads/{record.branch}")
        head_sha = _git(("rev-parse", "HEAD"), cwd=path)
        tip = _git(("--git-dir", str(repository), "rev-parse", "--verify", f"refs/heads/{record.branch}"),
                   cwd=repository)
        if head_sha.returncode != 0 or tip.returncode != 0 or head_sha.stdout.strip() != tip.stdout.strip():
            raise hold(f"HEAD is not at the tip of refs/heads/{record.branch}")
        # Remove from outside the worktree, addressing the repository by its
        # common directory: a process whose cwd is the deleted directory keeps
        # it open on Windows.
        removed = _git(("--git-dir", str(repository), "worktree", "remove", str(path)),
                       cwd=path.parent, timeout_s=timeout_s)
        if removed.returncode != 0:
            raise UnitRemovalRefused(f"git worktree remove failed: {_detail(removed)}")
        return registry._update(record, state=UnitState.REMOVED.value)


def record_unit_merge(
    registry: UnitRegistry,
    repository: Path,
    unit: str,
    *,
    tip_sha: str,
    target_sha: str,
) -> UnitRecord:
    """Record a successful merge of the unit branch (design 7.2 step 5).

    ``tip_sha`` is the unit tip that was merged and ``target_sha`` the target
    commit the push created. Refused (:class:`UnitBusyError`) while the unit
    is ``active``, ``dirty_held`` or ``writer_held``, since those block merge.
    A later branch deletion must still check that the branch's CURRENT tip is
    reachable from the target: the branch may have advanced since.
    """
    for name, value in (("tip_sha", tip_sha), ("target_sha", target_sha)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty commit id")
    repository = Path(repository).resolve()
    with repository_lock(repository):
        record = registry.get(repository, unit)
        if record is None:
            raise UnitError(f"unknown unit {unit!r} of repository {repository} in {registry.path}")
        if record.state not in (UnitState.PARKED, UnitState.REMOVED):
            raise UnitBusyError(f"unit {unit!r} is {record.state.value}; merge is blocked")
        return registry._update(
            record, merged_tip_sha=tip_sha.strip(), merge_target_sha=target_sha.strip(),
            merged_at=time.time(),
        )


def list_units(root: Path) -> tuple[UnitRecord, ...]:
    """Every unit recorded under the worktree root ``root``; empty when it has no registry."""
    path = Path(root).expanduser().resolve() / REGISTRY_FILENAME
    if not path.is_file():
        return ()
    return UnitRegistry(path.parent, create=False).list()


__all__ = [
    "CLAIM_SECTION",
    "DEFAULT_FINALIZE_S",
    "REGISTRY_FILENAME",
    "REGISTRY_SCHEMA",
    "UNIT_RECORD_SCHEMA_FILE",
    "UNIT_RECORD_VERSION",
    "WIP_MESSAGE",
    "UnitBusyError",
    "UnitError",
    "UnitFinalizeResult",
    "UnitFinalizeStatus",
    "UnitLease",
    "UnitOwner",
    "UnitOwnershipError",
    "UnitRecord",
    "UnitRegistry",
    "UnitRemovalRefused",
    "UnitState",
    "acquire_unit",
    "check_unit_id",
    "finalize_unit",
    "list_units",
    "live_processes",
    "record_unit_merge",
    "remove_unit_worktree",
    "repository_identity",
    "repository_lock",
]
