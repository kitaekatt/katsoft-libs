"""Ledger schema 15: the workspace status ``unit`` of branch-mode worktrees.

``fixtures/jobs_ledger_v14.sql`` is a dump of a real schema 14 ledger, written by the llm-kit
sources one commit before schema 15 (one job with an isolated completed attempt, one agent
reservation, one live reservation). The tests migrate it, read it without migrating it, store
``unit`` rows in the migrated ledger, and pin that a reader built for schema 14 refuses a schema 15
ledger rather than failing on a ``unit`` row.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from llm_kit.jobs import store as store_module
from llm_kit.jobs.model import WORKSPACE_STATUSES, Attempt, AttemptReservation, Contract, Job, JobState, Prompt
from llm_kit.jobs.store import LEDGER_SCHEMA, SESSION_SCHEMA, UNIT_SCHEMA, JobStore, LedgerReader, StoreError

V14_DUMP = Path(__file__).parent / "fixtures" / "jobs_ledger_v14.sql"
ENDPOINT = dict(endpoint="fake-endpoint", backend="fake-backend", model="fake-model")


def _v14_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "v14.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V14_DUMP.read_text(encoding="ascii"))
        connection.commit()
    finally:
        connection.close()
    return path


def _schema_sql(path: Path) -> list[tuple]:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        connection.close()


def test_the_schema_constants_agree():
    assert SESSION_SCHEMA == 14 and UNIT_SCHEMA == LEDGER_SCHEMA == 15 == len(store_module._MIGRATIONS)
    assert store_module._MIGRATIONS[UNIT_SCHEMA - 1] == []  # workspace_status has no CHECK to widen
    assert "unit" in WORKSPACE_STATUSES


def test_the_fixture_is_a_schema_14_ledger_and_reads_without_migrating(tmp_path):
    path = _v14_ledger(tmp_path)
    before = path.read_bytes()
    reader = LedgerReader(path)
    assert reader.schema_version() == 14
    snapshot = reader.snapshot("run")
    assert [job.id for job in snapshot.jobs] == ["isolated", "agent", "live"]
    assert snapshot.attempts[0].workspace_status == "isolated"
    assert path.read_bytes() == before


def test_a_schema_14_ledger_migrates_in_place_and_keeps_every_row(tmp_path):
    path = _v14_ledger(tmp_path)
    old = LedgerReader(path).snapshot("run")
    tables_before = _schema_sql(path)
    upgraded = JobStore(path, create=False)
    assert LedgerReader(path).schema_version() == 15
    assert _schema_sql(path) == tables_before  # no table or index changed
    new = upgraded.snapshot("run")
    assert new.jobs == old.jobs and new.attempts == old.attempts and new.reservations == old.reservations
    assert JobStore(path, create=False).snapshot("run").attempts == new.attempts
    assert LedgerReader(path).schema_version() == 15


def test_a_migrated_ledger_records_unit_workspaces(tmp_path):
    path = _v14_ledger(tmp_path)
    store = JobStore(path, create=False)
    work = tmp_path / "unit-worktree"
    job = Job(id="unit-job", prompt=Prompt(user="x"), models=("fake",), directory=tmp_path,
              contract=Contract(command=(sys.executable, "-c", "pass")))
    store.create_run("unit-run", (job,))
    store.reserve_attempt("unit-run", "unit-job", reserved_at="2026-11-14T00:00:00Z", **ENDPOINT)
    reservation = store.record_reservation_workspace("unit-run", "unit-job", 1, workspace_path=work,
                                                     workspace_status="unit")
    assert isinstance(reservation, AttemptReservation) and reservation.workspace_status == "unit"
    store.arm_reservation("unit-run", "unit-job", 1, invoke_armed_at="2026-11-14T00:00:01Z")
    store.append_attempt(
        Attempt(run_id="unit-run", job_id="unit-job", attempt_no=1, status="completed",
                started_at="2026-11-14T00:00:01Z", ended_at="2026-11-14T00:00:02Z", workspace=work,
                base_ref="a" * 40, workspace_status="unit", **ENDPOINT),
        terminal_state=JobState.ACCEPTED,
    )
    snapshot = LedgerReader(path).snapshot("unit-run")
    assert snapshot.attempts[0].workspace_status == "unit"
    assert snapshot.reservations[0].workspace_status == "unit"


@pytest.mark.parametrize("status, path", [("unit", None), ("none", "x"), ("removing", "x")])
def test_record_reservation_workspace_refuses_a_status_that_does_not_fit(tmp_path, status, path):
    store = JobStore(tmp_path / "ledger.sqlite3")
    job = Job(id="j", prompt=Prompt(user="x"), models=("fake",), directory=tmp_path,
              contract=Contract(command=("true",)))
    store.create_run("r", (job,))
    store.reserve_attempt("r", "j", reserved_at="2026-11-14T00:00:00Z", **ENDPOINT)
    with pytest.raises(ValueError, match="does not fit"):
        store.record_reservation_workspace("r", "j", 1, workspace_path=tmp_path / path if path else None,
                                           workspace_status=status)


def test_a_reader_built_for_schema_14_refuses_a_schema_15_ledger(tmp_path, monkeypatch):
    path = _v14_ledger(tmp_path)
    JobStore(path, create=False)
    before = path.read_bytes()
    monkeypatch.setattr(store_module, "_MIGRATIONS", list(store_module._MIGRATIONS)[:SESSION_SCHEMA])
    with pytest.raises(StoreError, match=r"schema version 15 is newer .*max 14"):
        JobStore(path)
    with pytest.raises(StoreError, match=r"schema version 15 is newer .*max 14"):
        LedgerReader(path).snapshot("run")
    assert path.read_bytes() == before
