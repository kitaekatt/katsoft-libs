"""Ledger schema 14: the agent-session columns and the job model that carries them.

``fixtures/jobs_ledger_v13.sql`` is a dump of a real schema 13 ledger, written
by the llm-kit sources one commit before schema 14 (three jobs: one with a
completed attempt, one with a lost reservation, one with a live reservation).
The tests migrate it, read it without migrating it, and pin that a reader built
for schema 13 refuses a schema 14 ledger. The column list under test is
``SESSION_LEDGER_COLUMNS``; nothing here re-types a column name or type.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from llm_kit.completion.session_types import (
    SESSION_LEDGER_COLUMNS,
    SESSION_LEDGER_SCHEMA,
    SESSION_LEDGER_TABLES,
    SESSION_OPTION_KEYS,
    SessionMode,
    StopKind,
)
from llm_kit.jobs import store as store_module
from llm_kit.jobs.model import (
    Attempt,
    AttemptReservation,
    Contract,
    Job,
    JobState,
    Prompt,
)
from llm_kit.jobs.store import (
    CANCEL_SCHEMA,
    SESSION_SCHEMA,
    JobStore,
    LedgerReader,
    StoreError,
)

V13_DUMP = Path(__file__).parent / "fixtures" / "jobs_ledger_v13.sql"
ARMED_AT = "2026-09-01T00:00:01Z"
ENDED_AT = "2026-09-01T00:00:02Z"
SESSION_ID = "11111111-2222-4333-8444-555555555555"
ENDPOINT = {"endpoint": "fake-endpoint", "backend": "fake-backend", "model": "fake-model"}


def _job(directory: Path, job_id: str, **extra: object) -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
        **extra,  # type: ignore[arg-type]
    )


def _v13_ledger(tmp_path: Path) -> Path:
    """Materialize the checked-in schema 13 dump as a ledger file."""
    path = tmp_path / "v13.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V13_DUMP.read_text(encoding="ascii"))
        connection.commit()
    finally:
        connection.close()
    return path


def _table_info(path: Path, table: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(str(path))
    try:
        return [
            tuple(row[1:])  # name, type, notnull, dflt_value, pk
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        ]
    finally:
        connection.close()


def _reference_columns(tmp_path: Path) -> list[tuple[object, ...]]:
    """What SQLite makes of each column's ``sql`` clause, added to a bare table."""
    path = tmp_path / "reference.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE ref (x INTEGER)")
        for column in SESSION_LEDGER_COLUMNS:
            connection.execute(f"ALTER TABLE ref ADD COLUMN {column.name} {column.sql}")
        connection.commit()
    finally:
        connection.close()
    return _table_info(path, "ref")[1:]


def _agent_reservation(store: JobStore, job_id: str, tmp_path: Path, **overrides: object):
    kwargs: dict[str, object] = {
        "session_mode": "agent",
        "session_id": SESSION_ID,
        "session_cwd": tmp_path,
        "session_dir": tmp_path / "sessions" / job_id / "attempt-1",
    }
    kwargs.update(overrides)
    return store.reserve_attempt(
        "agent-run", job_id, reserved_at="2026-09-01T00:00:00Z", **ENDPOINT, **kwargs
    )


def _agent_attempt(reservation: AttemptReservation, **overrides: object) -> Attempt:
    fields: dict[str, object] = {
        "run_id": reservation.run_id,
        "job_id": reservation.job_id,
        "attempt_no": reservation.attempt_no,
        "status": "completed",
        "started_at": ARMED_AT,
        "ended_at": ENDED_AT,
        "response_text": "answer",
        "session_mode": SessionMode.AGENT,
        "session_id": reservation.session_id,
        "session_cwd": reservation.session_cwd,
        "session_dir": reservation.session_dir,
        "stop_kind": StopKind.QUIESCENT,
        **ENDPOINT,
    }
    fields.update(overrides)
    return Attempt(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The schema constants and the columns
# --------------------------------------------------------------------------


def test_the_schema_constants_agree() -> None:
    assert CANCEL_SCHEMA == 13
    assert SESSION_SCHEMA == SESSION_LEDGER_SCHEMA == 14
    assert len(store_module._MIGRATIONS) == SESSION_SCHEMA


def test_a_fresh_ledger_has_exactly_the_session_columns_on_both_tables(
    tmp_path: Path,
) -> None:
    fresh = JobStore(tmp_path / "fresh.sqlite3")
    expected = _reference_columns(tmp_path)
    assert [column.name for column in SESSION_LEDGER_COLUMNS] == [row[0] for row in expected]
    for table in SESSION_LEDGER_TABLES:
        actual = _table_info(fresh.db_path, table)
        # Pinned by position as well as by name: the new columns come last.
        assert actual[-len(expected):] == expected, table
        assert len({row[0] for row in actual}) == len(actual), table


# --------------------------------------------------------------------------
# A real schema 13 ledger
# --------------------------------------------------------------------------


def test_the_fixture_is_a_schema_13_ledger(tmp_path: Path) -> None:
    path = _v13_ledger(tmp_path)
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 13
        for table in SESSION_LEDGER_TABLES:
            names = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert not names & {column.name for column in SESSION_LEDGER_COLUMNS}
    finally:
        connection.close()


def test_the_reader_serves_a_schema_13_ledger_without_migrating_it(
    tmp_path: Path,
) -> None:
    path = _v13_ledger(tmp_path)
    before = path.read_bytes()
    reader = LedgerReader(path)
    assert reader.schema_version() == 13
    snapshot = reader.snapshot("run")
    assert len(snapshot.attempts) == 1 and len(snapshot.reservations) == 3
    for record in (*snapshot.attempts, *snapshot.reservations):
        for column in SESSION_LEDGER_COLUMNS:
            expected = column.pre_v14
            value = getattr(record, column.name)
            if column.name == "session_mode":
                assert value is SessionMode.COMPLETION
            else:
                assert value is None and expected is None, column.name
    assert snapshot.attempts[0].response_text == "the model answer"
    assert path.read_bytes() == before
    assert reader.schema_version() == 13


def test_a_schema_13_ledger_migrates_in_place_and_keeps_every_row(
    tmp_path: Path,
) -> None:
    path = _v13_ledger(tmp_path)
    old = LedgerReader(path).snapshot("run")

    upgraded = JobStore(path, create=False)
    assert LedgerReader(path).schema_version() == 14
    new = upgraded.snapshot("run")
    # Same rows, each now carrying the pre-14 session values the reader supplied.
    assert new.attempts == old.attempts
    assert new.reservations == old.reservations
    assert [r.disposition for r in new.reservations] == ["completed", "process_lost", None]
    connection = sqlite3.connect(str(path))
    try:
        for table in SESSION_LEDGER_TABLES:
            row = connection.execute(
                f"SELECT session_mode, session_id, session_cwd, session_dir, stop_kind "
                f"FROM {table} ORDER BY id"
            ).fetchall()
            assert all(r == ("completion", None, None, None, None) for r in row), table
    finally:
        connection.close()
    # Opening it again migrates nothing further.
    reopened = JobStore(path, create=False).snapshot("run")
    assert LedgerReader(path).schema_version() == 14
    assert reopened.attempts == new.attempts and reopened.reservations == new.reservations


def test_a_migrated_ledger_accepts_agent_rows_beside_the_old_ones(
    tmp_path: Path,
) -> None:
    path = _v13_ledger(tmp_path)
    store = JobStore(path, create=False)
    store.create_run("agent-run", [_job(tmp_path, "agent-job")], created_at=2000.0)
    reservation = _agent_reservation(store, "agent-job", tmp_path)
    store.arm_reservation("agent-run", "agent-job", reservation.attempt_no, invoke_armed_at=ARMED_AT)
    store.append_attempt(
        _agent_attempt(reservation), terminal_state=JobState.ACCEPTED, at=2001.0
    )
    reader = LedgerReader(path)
    assert reader.schema_version() == 14
    snapshot = reader.snapshot("agent-run")
    attempt = snapshot.attempts[0]
    assert (attempt.session_mode, attempt.session_id) == (SessionMode.AGENT, SESSION_ID)
    assert attempt.stop_kind is StopKind.QUIESCENT
    assert snapshot.reservations[0].stop_kind is StopKind.QUIESCENT
    assert reader.snapshot("run").attempts[0].session_mode is SessionMode.COMPLETION


def test_a_reader_built_for_schema_13_refuses_a_schema_14_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _v13_ledger(tmp_path)
    JobStore(path, create=False)
    before = path.read_bytes()
    monkeypatch.setattr(store_module, "_MIGRATIONS", list(store_module._MIGRATIONS)[:13])
    with pytest.raises(StoreError, match=r"schema version 14 is newer .*max 13"):
        JobStore(path)
    with pytest.raises(StoreError, match=r"schema version 14 is newer .*max 13"):
        LedgerReader(path).schema_version()
    with pytest.raises(StoreError, match=r"schema version 14 is newer .*max 13"):
        LedgerReader(path).snapshot("run")
    monkeypatch.undo()
    assert path.read_bytes() == before


# --------------------------------------------------------------------------
# Store write paths
# --------------------------------------------------------------------------


def test_a_reservation_carries_the_session_identity_before_any_attempt(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "agent-job")])
    reservation = _agent_reservation(store, "agent-job", tmp_path)
    assert reservation.session_mode is SessionMode.AGENT
    assert reservation.session_id == SESSION_ID
    assert reservation.session_cwd == tmp_path
    assert reservation.session_dir == tmp_path / "sessions" / "agent-job" / "attempt-1"
    assert reservation.stop_kind is None
    assert store.get_reservation("agent-run", "agent-job", reservation.attempt_no) == reservation
    assert store.list_attempts("agent-run") == []


def test_a_completion_reservation_is_unchanged(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "plain")])
    reservation = store.reserve_attempt(
        "agent-run", "plain", reserved_at="2026-09-01T00:00:00Z", **ENDPOINT
    )
    assert reservation.session_mode is SessionMode.COMPLETION
    assert reservation.session_id is reservation.session_dir is reservation.stop_kind is None


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"session_id": None}, "requires session_id"),
        ({"session_cwd": None}, "requires session_cwd"),
        ({"session_dir": None}, "requires session_dir"),
        ({"session_id": "not-a-uuid"}, "must be a UUID"),
        ({"session_dir": "relative/dir"}, "must be absolute"),
        ({"session_mode": "bogus"}, "session_mode must be one of"),
    ],
)
def test_an_invalid_agent_reservation_is_refused_before_anything_is_written(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "agent-job")])
    with pytest.raises(ValueError, match=message):
        _agent_reservation(store, "agent-job", tmp_path, **overrides)
    assert store.list_reservations("agent-run") == []
    assert store.get_job("agent-run", "agent-job").state is JobState.PENDING


def test_a_completion_reservation_may_not_set_session_fields(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "plain")])
    with pytest.raises(ValueError, match="completion reservation must not set session_id"):
        store.reserve_attempt(
            "agent-run", "plain", reserved_at="2026-09-01T00:00:00Z",
            session_id=SESSION_ID, **ENDPOINT,
        )
    assert store.list_reservations("agent-run") == []


def test_stop_kind_is_written_on_the_reservation_in_the_attempt_transaction(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "agent-job")])
    reservation = _agent_reservation(store, "agent-job", tmp_path)
    store.arm_reservation("agent-run", "agent-job", reservation.attempt_no, invoke_armed_at=ARMED_AT)
    live = store.get_reservation("agent-run", "agent-job", reservation.attempt_no)
    assert live is not None and live.stop_kind is None and live.disposition is None
    stored = store.append_attempt(
        _agent_attempt(reservation, stop_kind=StopKind.OPERATOR_STOP),
        terminal_state=JobState.FAILED,
        at=2001.0,
    )
    assert stored.stop_kind is StopKind.OPERATOR_STOP
    resolved = store.get_reservation("agent-run", "agent-job", reservation.attempt_no)
    assert resolved is not None
    assert (resolved.disposition, resolved.stop_kind) == ("completed", StopKind.OPERATOR_STOP)


def test_a_hard_cancelled_attempt_keeps_a_null_stop_kind(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "agent-job")])
    reservation = _agent_reservation(store, "agent-job", tmp_path)
    store.arm_reservation("agent-run", "agent-job", reservation.attempt_no, invoke_armed_at=ARMED_AT)
    stored = store.append_attempt(_agent_attempt(reservation, stop_kind=None), at=2001.0)
    assert stored.stop_kind is None
    resolved = store.get_reservation("agent-run", "agent-job", reservation.attempt_no)
    assert resolved is not None and resolved.stop_kind is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_mode": SessionMode.COMPLETION, "session_id": None, "session_cwd": None,
         "session_dir": None, "stop_kind": None},
        {"session_id": str(uuid.UUID(int=7))},
    ],
)
def test_an_attempt_that_disagrees_with_its_reservation_is_refused(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("agent-run", [_job(tmp_path, "agent-job")])
    reservation = _agent_reservation(store, "agent-job", tmp_path)
    store.arm_reservation("agent-run", "agent-job", reservation.attempt_no, invoke_armed_at=ARMED_AT)
    with pytest.raises(StoreError, match="differs from its reservation"):
        store.append_attempt(_agent_attempt(reservation, **overrides), at=2001.0)
    assert store.list_attempts("agent-run") == []
    live = store.get_reservation("agent-run", "agent-job", reservation.attempt_no)
    assert live is not None and live.disposition is None


# --------------------------------------------------------------------------
# Model invariants
# --------------------------------------------------------------------------


def test_the_model_fields_use_the_ledger_column_names() -> None:
    attempt = Attempt(
        run_id="r", job_id="j", attempt_no=1, status="completed",
        started_at=None, ended_at=None, **ENDPOINT,
    )
    reservation = AttemptReservation(
        run_id="r", job_id="j", attempt_no=1, budget_no=1, workspace_path=None,
        reserved_at="x", **ENDPOINT,
    )
    for record in (attempt, reservation):
        mapping = record.to_mapping()
        for column in SESSION_LEDGER_COLUMNS:
            assert column.name in mapping
            assert getattr(record, column.name) == (
                SessionMode.COMPLETION if column.name == "session_mode" else None
            )
        assert mapping["session_mode"] == "completion"


def test_an_agent_record_needs_its_identity_and_a_completion_record_has_none(
    tmp_path: Path,
) -> None:
    base = dict(
        run_id="r", job_id="j", attempt_no=1, status="completed",
        started_at=None, ended_at=None, **ENDPOINT,
    )
    with pytest.raises(ValueError, match="agent attempt requires"):
        Attempt(**base, session_mode="agent")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="completion attempt must not set stop_kind"):
        Attempt(**base, stop_kind=StopKind.QUIESCENT)  # type: ignore[arg-type]
    agent = Attempt(
        **base,  # type: ignore[arg-type]
        session_mode="agent", session_id=SESSION_ID,
        session_cwd=str(tmp_path), session_dir=str(tmp_path / "d"), stop_kind="timeout",
    )
    assert agent.session_mode is SessionMode.AGENT
    assert agent.stop_kind is StopKind.TIMEOUT
    assert agent.session_cwd == tmp_path
    mapping = agent.to_mapping()
    assert mapping["stop_kind"] == "timeout" and mapping["session_dir"] == str(tmp_path / "d")
    with pytest.raises(ValueError, match="stop_kind must be one of"):
        replace(agent, stop_kind="bogus")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Job options
# --------------------------------------------------------------------------


def test_the_explicit_completion_default_is_accepted_as_a_session_option(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path, "j", options={"session": "completion"})
    assert job.options["session"] == "completion"
    assert Job.from_mapping(job.to_mapping(), base_dir=tmp_path).options == job.options
    assert _job(tmp_path, "p").options == {}


@pytest.mark.parametrize(
    "options",
    [
        {"session": "agent"},
        {"session": "agent", "resume_session": SESSION_ID},
        {"session": "agent", "session_quiescence_s": 7},
        {"session": "agent", "session_result_wait_s": 7},
        {"session": "agent", "session_eof_wait_s": 7},
        {"session": "agent", "session_sigint_wait_s": 7},
        {"session": "agent", "session_finalize_s": 7},
    ],
)
def test_agent_session_options_are_accepted_and_round_trip(
    tmp_path: Path, options: dict[str, object]
) -> None:
    """``jobs run`` acts on agent-session options (L1d), so the model accepts them."""
    job = _job(tmp_path, "j", options=options)
    assert dict(job.options) == options
    assert Job.from_mapping(job.to_mapping(), base_dir=tmp_path).options == job.options


@pytest.mark.parametrize("key", sorted(SESSION_OPTION_KEYS))
def test_a_model_level_session_option_is_rejected(tmp_path: Path, key: str) -> None:
    value: object = "agent" if key == "session" else SESSION_ID if key == "resume_session" else 5
    with pytest.raises(ValueError, match="job-level only"):
        _job(tmp_path, "j", model_options={"fake": {key: value}})


@pytest.mark.parametrize(
    "options, message",
    [
        ({"session": "bogus"}, "session must be one of"),
        ({"session_quiescence_s": 5}, "require session: agent"),
        ({"resume_session": SESSION_ID}, "requires session: agent"),
        ({"session": "agent", "resume_session": "nope"}, "must be a UUID"),
        ({"session": "agent", "session_finalize_s": 0}, "finalize_s must be positive"),
    ],
)
def test_an_invalid_session_option_is_rejected(
    tmp_path: Path, options: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _job(tmp_path, "j", options=options)
