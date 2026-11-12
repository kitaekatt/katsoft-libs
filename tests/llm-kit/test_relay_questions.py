"""Tests for llm_kit.relay.questions (unit Q10).

Every ledger is a real job ledger in tmp, with a job taken to ``waiting`` on a
real interrupt through the jobs store API (the same calls the jobs tests use).
The parity tests build two identical ledgers, resolve one through
``llm-kit jobs resolve`` (``llm_kit.cli.jobs.run``) and the other through the
service, and compare the stored resolution, the job and the execution events.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_kit.cli import jobs as jobs_cli
from llm_kit.jobs.interrupts import REQUEST_ENVELOPE_V1
from llm_kit.jobs.model import (
    Acceptance,
    Attempt,
    Contract,
    InterruptRequest,
    Job,
    JobState,
    Prompt,
    Usage,
)
from llm_kit.jobs.store import JobStore
from llm_kit.relay import protocol as P
from llm_kit.relay import questions as Q
from llm_kit.schema import json_schema as JS

MACHINE = "m5pro"
CREATED = 1000.0
STARTED = "2026-09-01T00:00:01Z"
ENDED = "2026-09-01T00:00:02Z"
RUN = "run"
APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# ledger builders
# --------------------------------------------------------------------------

def _job(directory: Path, job_id: str) -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def _attempt(run_id: str, job_id: str, attempt_no: int) -> Attempt:
    return Attempt(
        run_id=run_id,
        job_id=job_id,
        attempt_no=attempt_no,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        status="completed",
        started_at=STARTED,
        ended_at=ENDED,
        usage=Usage(input_tokens=3, output_tokens=5),
        response_text="the model answer",
        acceptance=Acceptance(
            command=("contract",), directory=Path.cwd(), exit_code=0, stdout="", stderr="",
            wall_ms=1, accepted=False, outcome="interrupt_requested",
        ),
    )


def _request(expires_in_s: Optional[int] = None) -> InterruptRequest:
    return InterruptRequest(
        envelope=REQUEST_ENVELOPE_V1,
        kind="approval",
        request_schema=dict(APPROVAL),
        payload={"action": "push tag"},
        expires_in_s=expires_in_s,
    )


def _ledger(
    path: Path, *, run_id: str = RUN, job_id: str = "job", expires_in_s: Optional[int] = None
) -> JobStore:
    """A real ledger with one job waiting on one unresolved interrupt."""
    store = JobStore(path)
    store.create_run(run_id, [_job(path.parent, job_id)])
    reservation = store.reserve_attempt(
        run_id, job_id, endpoint="fake-endpoint", backend="fake-backend", model="fake-model",
        reserved_at="2026-09-01T00:00:00Z",
    )
    store.arm_reservation(run_id, job_id, reservation.attempt_no, invoke_armed_at=STARTED)
    store.append_attempt(
        _attempt(run_id, job_id, reservation.attempt_no), interrupt=_request(expires_in_s), at=CREATED
    )
    assert store.get_job(run_id, job_id).state is JobState.WAITING
    return store


def _service(*ledgers: Path, clock: Any = lambda: CREATED + 5) -> Q.QuestionService:
    return Q.QuestionService(machine=MACHINE, ledgers=list(ledgers), clock=clock)


def _only(service: Q.QuestionService, **params: Any) -> dict[str, Any]:
    result = service.list_questions(params)
    assert len(result["questions"]) == 1, result
    return result["questions"][0]


def _refused(call: Any, params: dict[str, Any]) -> P.RelayError:
    with pytest.raises(P.RelayError) as info:
        call(params)
    return info.value


def _resolution_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT interrupt_id, outcome, input_json, reason FROM interrupt_resolutions ORDER BY interrupt_id"
        ).fetchall()
    finally:
        conn.close()


def _event_names(store: JobStore, run_id: str = RUN) -> list[tuple[str, Any]]:
    return [(e["event"], e.get("payload", {}).get("phase")) for e in store.list_events(run_id)]


def _schema_errors(name: str, value: Any) -> list:
    schema = P.load_schema(name)
    return list(JS.validate(schema, value))


# --------------------------------------------------------------------------
# id encoding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("run_id", ["run", "run-20261103-01", "a:b:c", "a/b", "x" * 40])
def test_question_id_round_trips_through_the_grammar(run_id: str) -> None:
    qid = Q.encode_question_id(MACHINE, "jobs-main", run_id, "7")
    assert qid.startswith(f"q:{MACHINE}/")
    assert Q.parse_question_id(qid) == (MACHINE, "jobs-main", run_id, "7")


@pytest.mark.parametrize(
    "text",
    [
        None, 7, "", "q:", "m5pro/jobs:run:1", "q:m5pro", "q:m5pro/jobs", "q:m5pro/jobs:run",
        "q:m5pro/jobs:run:x", "q:m5pro/jobs:run:", "q:m5pro/:run:1", "q:M5PRO/jobs:run:1",
        "q:m5pro/jobs::1", "q:m5pro/jobs:run:" + "1" * 300,
    ],
)
def test_malformed_question_ids_are_bad_request(text: Any) -> None:
    with pytest.raises(P.RelayError) as info:
        Q.parse_question_id(text)
    assert info.value.code == "bad_request"


def test_ledger_keys_are_the_sanitised_stem_and_collisions_are_disambiguated(tmp_path: Path) -> None:
    a, b, c = tmp_path / "a" / "jobs.sqlite3", tmp_path / "b" / "jobs.sqlite3", tmp_path / "odd name!.db"
    keys = Q.ledger_keys([a, b, c])
    assert set(keys.values()) == {a, b, c} and len(keys) == 3
    assert "odd-name" in keys
    clashing = sorted(k for k in keys if k != "odd-name")
    assert all(k.startswith("jobs-") and len(k) == len("jobs-") + 8 for k in clashing)
    assert Q.ledger_keys([a]) == {"jobs": a}
    assert Q.ledger_keys([a, b, c]) == keys  # stable for one config


# --------------------------------------------------------------------------
# list_questions
# --------------------------------------------------------------------------

def test_a_waiting_job_lists_as_one_open_question_valid_against_f0(tmp_path: Path) -> None:
    path = tmp_path / "jobs-main.sqlite3"
    store = _ledger(path)
    interrupt = store.open_interrupt(RUN, "job")
    result = _service(path).list_questions({})
    assert _schema_errors("list_questions.response", result) == []
    question = _only(_service(path))
    assert question == {
        "id": f"q:{MACHINE}/jobs-main:{RUN}:{interrupt.id}",
        "machine": MACHINE,
        "source": {"kind": "job", "ledger": "jobs-main", "run_id": RUN, "job_id": "job",
                   "interrupt_id": interrupt.id},
        "kind": "approval",
        "payload": {"action": "push tag"},
        "request_schema": APPROVAL,
        "created_at": CREATED,
        "expires_at": None,
        "state": "open",
    }
    assert result["machines"] == [{"machine": MACHINE, "status": "up"}]
    assert result["ledgers"] == [{"ledger": "jobs-main", "status": "ok"}]


def test_questions_span_ledgers_and_runs_in_creation_order(tmp_path: Path) -> None:
    one, two = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _ledger(one, run_id="r1")
    store = _ledger(two, run_id="r2")
    store.create_run("r3", [_job(tmp_path, "job")])
    result = _service(one, two).list_questions({})
    assert [q["source"]["ledger"] for q in result["questions"]] == ["one", "two"]
    assert [q["source"]["run_id"] for q in result["questions"]] == ["r1", "r2"]
    assert [x["status"] for x in result["ledgers"]] == ["ok", "ok"]


def test_resolved_questions_leave_the_open_list_and_stay_in_all(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path)
    service = _service(path)
    qid = _only(service)["id"]
    service.answer_question({"id": qid, "decision": "answer", "input": {"approved": True}})
    assert service.list_questions({})["questions"] == []
    assert service.list_questions({"state": "open"})["questions"] == []
    listed = _only(service, state="all")
    assert listed["id"] == qid and listed["state"] == "answered"


def test_a_lapsed_unresolved_interrupt_is_expired_not_open(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path, expires_in_s=10)
    late = _service(path, clock=lambda: CREATED + 500)
    assert late.list_questions({})["questions"] == []
    assert _only(late, state="all")["state"] == "expired"
    assert _only(_service(path, clock=lambda: CREATED + 5))["state"] == "open"


def test_a_cancelled_interrupt_lists_as_rejected(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    store = _ledger(path)
    store.mark_cancelled(RUN, "job", "operator stopped the run", at=CREATED + 2)
    assert _only(_service(path), state="all")["state"] == "rejected"


def test_an_unreadable_ledger_is_reported_and_does_not_hide_the_others(tmp_path: Path) -> None:
    good, missing = tmp_path / "good.sqlite3", tmp_path / "missing.sqlite3"
    _ledger(good)
    result = _service(good, missing).list_questions({})
    assert len(result["questions"]) == 1
    by_key = {x["ledger"]: x for x in result["ledgers"]}
    assert by_key["good"]["status"] == "ok"
    assert by_key["missing"]["status"] == "unavailable" and "error" in by_key["missing"]
    assert not missing.exists()  # listing never creates a ledger


def test_another_machine_is_unreachable_not_empty(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path)
    result = _service(path).list_questions({"machine": "other"})
    assert result["questions"] == []
    assert result["machines"][0]["status"] == "unreachable"
    assert result["machines"][0]["error"]["code"] == "machine_unreachable"
    assert _schema_errors("list_questions.response", result) == []
    assert len(_service(path).list_questions({"machine": MACHINE})["questions"]) == 1


def test_a_bad_state_filter_is_bad_request(tmp_path: Path) -> None:
    assert _refused(_service(tmp_path / "x.sqlite3").list_questions, {"state": "pending"}).code == "bad_request"


def test_the_machine_reach_comes_from_the_caller_when_given(tmp_path: Path) -> None:
    service = Q.QuestionService(
        machine=MACHINE, ledgers=[], reach=lambda: {"machine": MACHINE, "status": "paused"}
    )
    assert service.list_questions({})["machines"] == [{"machine": MACHINE, "status": "paused"}]


# --------------------------------------------------------------------------
# answer_question: parity with `llm-kit jobs resolve`
# --------------------------------------------------------------------------

def _cli_resolve(path: Path, interrupt_id: str, *args: str) -> int:
    return jobs_cli.run(["resolve", RUN, interrupt_id, *args, "--store", str(path)])


def _twin_ledgers(tmp_path: Path) -> tuple[Path, Path]:
    cli, relay = tmp_path / "cli" / "l.sqlite3", tmp_path / "relay" / "l.sqlite3"
    for path in (cli, relay):
        path.parent.mkdir()
        _ledger(path)
    return cli, relay


def _snapshot_of(path: Path) -> dict[str, Any]:
    store = JobStore(path, create=False)
    resolved = store.list_interrupts(RUN)
    mapping = [{k: v for k, v in r.to_mapping().items() if k != "created_at"} for r in resolved]
    for item in mapping:
        if item["resolution"] is not None:
            item["resolution"] = {k: v for k, v in item["resolution"].items() if k != "resolved_at"}
    return {
        "rows": _resolution_rows(path),
        "interrupts": mapping,
        "job_state": store.get_job(RUN, "job").state,
        "events": _event_names(store),
    }


def test_answer_stores_what_jobs_resolve_stores(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cli, relay = _twin_ledgers(tmp_path)
    interrupt_id = JobStore(cli, create=False).open_interrupt(RUN, "job").id
    assert _cli_resolve(cli, interrupt_id, "--input", '{"approved": true}') == 0
    cli_out = json.loads(capsys.readouterr().out)

    service = _service(relay)
    qid = _only(service)["id"]
    result = service.answer_question({"id": qid, "decision": "answer", "input": {"approved": True}})

    assert _schema_errors("answer_question.response", result) == []
    assert result["state"] == "answered" and result["replayed"] is False
    assert result["decision"] == "answer" and result["id"] == qid
    assert result["job_id"] == cli_out["job_id"] and result["job_state"] == cli_out["state"]
    assert cli_out["outcome"] == "recorded"
    assert _snapshot_of(relay) == _snapshot_of(cli)
    assert _snapshot_of(relay)["rows"][0][1:] == ("answered", '{"approved":true}', None)
    # and it did not resume the run
    assert _snapshot_of(relay)["job_state"] is JobState.WAITING


def test_reject_stores_what_jobs_resolve_stores(tmp_path: Path) -> None:
    cli, relay = _twin_ledgers(tmp_path)
    interrupt_id = JobStore(cli, create=False).open_interrupt(RUN, "job").id
    assert _cli_resolve(cli, interrupt_id, "--reject", "--reason", "not now") == 0

    service = _service(relay)
    result = service.answer_question(
        {"id": _only(service)["id"], "decision": "reject", "reason": "not now"}
    )
    assert result["state"] == "rejected" and result["job_state"] == JobState.OPERATOR_REJECTED.value
    assert _snapshot_of(relay) == _snapshot_of(cli)
    assert _snapshot_of(relay)["rows"][0][1] == "rejected"
    assert _snapshot_of(relay)["rows"][0][3] == "not now"


def test_reject_without_a_reason_matches_the_cli(tmp_path: Path) -> None:
    cli, relay = _twin_ledgers(tmp_path)
    interrupt_id = JobStore(cli, create=False).open_interrupt(RUN, "job").id
    assert _cli_resolve(cli, interrupt_id, "--reject") == 0
    service = _service(relay)
    service.answer_question({"id": _only(service)["id"], "decision": "reject"})
    assert _snapshot_of(relay) == _snapshot_of(cli)


def test_a_schema_invalid_answer_is_refused_like_the_cli_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cli, relay = _twin_ledgers(tmp_path)
    interrupt_id = JobStore(cli, create=False).open_interrupt(RUN, "job").id
    assert _cli_resolve(cli, interrupt_id, "--input", '{"approved": false}') == 1
    cli_refusal = json.loads(capsys.readouterr().out)
    before = _snapshot_of(relay)

    service = _service(relay)
    qid = _only(service)["id"]
    error = _refused(service.answer_question, {"id": qid, "decision": "answer", "input": {"approved": False}})
    assert error.code == "answer_invalid" and error.http_status == 422 and not error.retryable
    assert error.details["errors"]  # the validator's (pointer, keyword) pairs
    assert cli_refusal  # the CLI refused the same input
    assert _resolution_rows(relay) == [] == _resolution_rows(cli)
    assert _snapshot_of(relay) == before
    assert _only(service)["state"] == "open"  # still answerable
    ok = service.answer_question({"id": qid, "decision": "answer", "input": {"approved": True}})
    assert ok["state"] == "answered"


@pytest.mark.parametrize("bad", [{"approved": True, "extra": 1}, {}, [], "yes", None])
def test_every_schema_violation_is_answer_invalid(tmp_path: Path, bad: Any) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path)
    service = _service(path)
    error = _refused(service.answer_question, {"id": _only(service)["id"], "decision": "answer", "input": bad})
    assert error.code == "answer_invalid"
    assert _resolution_rows(path) == []


def test_a_double_answer_replays_when_equal_and_is_refused_when_different(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path)
    service = _service(path)
    qid = _only(service)["id"]
    first = service.answer_question({"id": qid, "decision": "answer", "input": {"approved": True}})
    again = service.answer_question({"id": qid, "decision": "answer", "input": {"approved": True}})
    assert first["replayed"] is False and again["replayed"] is True
    assert again["resolved_at"] == first["resolved_at"]
    assert len(_resolution_rows(path)) == 1

    other = _refused(service.answer_question, {"id": qid, "decision": "reject", "reason": "changed my mind"})
    assert other.code == "invalid_transition" and other.http_status == 409
    assert other.details == {"stored": "answered"}
    different = _refused(service.answer_question, {"id": qid, "decision": "answer", "input": {"approved": True, "x": 1}})
    assert different.code == "invalid_transition" and different.details == {"stored": "answered"}
    assert len(_resolution_rows(path)) == 1
    assert _resolution_rows(path)[0][1] == "answered"


def test_answering_a_lapsed_interrupt_is_refused_and_records_the_expiry(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    store = _ledger(path, expires_in_s=10)
    service = _service(path, clock=lambda: CREATED + 500)
    qid = Q.encode_question_id(MACHINE, "l", RUN, store.open_interrupt(RUN, "job").id)
    error = _refused(service.answer_question, {"id": qid, "decision": "answer", "input": {"approved": True}})
    assert error.code == "invalid_transition" and error.details == {"stored": "expired"}
    assert _resolution_rows(path)[0][1] == "expired"
    assert store.get_job(RUN, "job").state is JobState.EXPIRED
    again = _refused(service.answer_question, {"id": qid, "decision": "answer", "input": {"approved": True}})
    assert again.code == "invalid_transition"


def test_answering_a_cancelled_interrupt_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    store = _ledger(path)
    interrupt_id = store.open_interrupt(RUN, "job").id
    store.mark_cancelled(RUN, "job", "operator stopped the run", at=CREATED + 2)
    error = _refused(
        _service(path).answer_question,
        {"id": Q.encode_question_id(MACHINE, "l", RUN, interrupt_id), "decision": "answer", "input": {"approved": True}},
    )
    assert error.code == "invalid_transition"
    assert _resolution_rows(path)[0][1] == "cancelled"


# --------------------------------------------------------------------------
# answer_question: refusals before the ledger
# --------------------------------------------------------------------------

def test_unknown_ids_are_question_not_found(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    store = _ledger(path)
    real = store.open_interrupt(RUN, "job").id
    service = _service(path)
    body = {"decision": "answer", "input": {"approved": True}}
    cases = {
        "wrong machine": f"q:other/l:{RUN}:{real}",
        "unknown ledger": f"q:{MACHINE}/nope:{RUN}:{real}",
        "unknown run": f"q:{MACHINE}/l:ghost:{real}",
        "unknown interrupt": f"q:{MACHINE}/l:{RUN}:999",
    }
    for label, qid in cases.items():
        error = _refused(service.answer_question, {"id": qid, **body})
        assert error.code == "question_not_found" and error.http_status == 404, label
    assert _resolution_rows(path) == []
    assert _only(service)["state"] == "open"


def test_an_interrupt_of_another_run_is_not_found(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    store = _ledger(path, run_id="r1")
    store.create_run("r2", [_job(tmp_path, "job")])
    real = store.open_interrupt("r1", "job").id
    error = _refused(
        _service(path).answer_question,
        {"id": Q.encode_question_id(MACHINE, "l", "r2", real), "decision": "answer", "input": {"approved": True}},
    )
    assert error.code == "question_not_found"
    assert _resolution_rows(path) == []


def test_a_missing_ledger_file_is_not_found_and_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "gone.sqlite3"
    error = _refused(
        _service(path).answer_question,
        {"id": f"q:{MACHINE}/gone:{RUN}:1", "decision": "answer", "input": {"approved": True}},
    )
    assert error.code == "question_not_found"
    assert not path.exists()


@pytest.mark.parametrize(
    "params",
    [
        {"id": "nonsense", "decision": "answer", "input": 1},
        {"id": f"q:{MACHINE}/l:{RUN}:1", "decision": "maybe"},
        {"id": f"q:{MACHINE}/l:{RUN}:1", "decision": "answer"},
        {"id": f"q:{MACHINE}/l:{RUN}:1", "decision": "answer", "input": 1, "reason": "x"},
        {"id": f"q:{MACHINE}/l:{RUN}:1", "decision": "reject", "input": 1},
        {"id": f"q:{MACHINE}/l:{RUN}:1", "decision": "reject", "reason": ""},
        {"id": f"q:{MACHINE}/l:{RUN}:1", "decision": "reject", "reason": "x" * 1001},
        {"decision": "reject"},
    ],
)
def test_malformed_params_are_bad_request(tmp_path: Path, params: dict[str, Any]) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path)
    assert _refused(_service(path).answer_question, params).code == "bad_request"
    assert _resolution_rows(path) == []


def test_a_machine_that_cannot_validate_answers_refuses_internal_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "l.sqlite3"
    _ledger(path)
    service = _service(path)
    qid = _only(service)["id"]

    def unusable() -> None:
        raise ImportError("no json_schema validator on this machine")

    monkeypatch.setattr(Q._interrupts, "_schema_validator", unusable)
    error = _refused(service.answer_question, {"id": qid, "decision": "answer", "input": {"approved": True}})
    assert error.code == "internal"
    assert _resolution_rows(path) == []


def test_from_config_uses_the_label_and_watched_ledgers(tmp_path: Path) -> None:
    from types import SimpleNamespace

    path = tmp_path / "jobs-main.sqlite3"
    _ledger(path)
    service = Q.QuestionService.from_config(SimpleNamespace(machine_label=MACHINE, ledgers=(path,)))
    assert dict(service.ledger_keys) == {"jobs-main": path}
    assert _only(service)["source"]["ledger"] == "jobs-main"
