"""Validation of the agent-session interface types (llm_kit.completion.session_types).

These pin the I0 interface the agent-session leaves implement against: the
bound defaults and their rejection, the enum values written to disk and to the
ledger, the option rules, the record invariants and the path layout.
"""
from __future__ import annotations

import math
import uuid
from pathlib import Path

import pytest

from llm_kit.completion import session_types as st
from llm_kit.completion.types import BackendOptions, LLMResponse

SID = "0b9f3c3e-8a51-4a0e-9a4f-3d2f7c1e5b10"
ROOT = Path("/runs/r1/sessions").resolve()


class _Channel:
    def poll(self, after_seq):
        return ()

    def mark_forwarded(self, seq):
        return None

    def close(self, reason, at):
        return st.MailboxState()

    def record_started(self, pid, at):
        return None

    def record_first_init(self, at):
        return None


# -- bounds -----------------------------------------------------------------


def test_bound_defaults_are_the_ruled_values():
    bounds = st.SessionBounds()
    assert (bounds.quiescence_s, bounds.result_wait_s, bounds.eof_wait_s, bounds.sigint_wait_s) == (
        5.0, 30.0, 30.0, 10.0,
    )
    assert bounds.shutdown_allowance_s == 70.0


@pytest.mark.parametrize("field", ["quiescence_s", "result_wait_s", "eof_wait_s", "sigint_wait_s"])
@pytest.mark.parametrize("value", [0, -1, -0.5, math.inf, -math.inf, math.nan, True, False, "5", None])
def test_every_bound_rejects_a_non_positive_non_finite_or_non_number(field, value):
    with pytest.raises(ValueError, match=field):
        st.SessionBounds(**{field: value})


def test_bounds_accept_positive_numbers_and_store_floats():
    bounds = st.SessionBounds(quiescence_s=1, result_wait_s=0.25, eof_wait_s=2, sigint_wait_s=3.5)
    assert bounds.quiescence_s == 1.0 and isinstance(bounds.quiescence_s, float)
    assert bounds.result_wait_s == 0.25


def test_bounds_from_options_reads_the_option_keys_and_validates():
    bounds = st.SessionBounds.from_options({st.OPTION_QUIESCENCE_S: 2, "other": object()})
    assert bounds.quiescence_s == 2.0 and bounds.result_wait_s == 30.0
    with pytest.raises(ValueError, match="sigint_wait_s"):
        st.SessionBounds.from_options({st.OPTION_SIGINT_WAIT_S: 0})


# -- enums ------------------------------------------------------------------


@pytest.mark.parametrize(
    "enum, values",
    [
        (st.SessionMode, {"completion", "agent"}),
        (
            st.StopKind,
            {"quiescent", "operator_stop", "operator_pause", "timeout", "process_exit", "init_mismatch"},
        ),
        (st.StopRequestKind, {"stop", "pause"}),
        (st.StopMode, {"interrupt", "finish_turn"}),
        (st.MailboxRecordKind, {"message", "stop"}),
        (st.MessageDelivery, {"pending", "forwarded", "undelivered"}),
        (st.WorkspaceFinalizeStatus, {"clean", "committed", "dirty_held", "not_applicable"}),
    ],
)
def test_enum_values_are_pinned(enum, values):
    assert {member.value for member in enum} == values


def test_operator_stop_kinds_carry_their_error_codes():
    assert st.StopKind.OPERATOR_STOP.operator_error_code == "operator_stop"
    assert st.StopKind.OPERATOR_PAUSE.operator_error_code == "operator_pause"
    assert {kind for kind in st.StopKind if kind.is_operator} == {
        st.StopKind.OPERATOR_STOP, st.StopKind.OPERATOR_PAUSE,
    }
    assert st.StopKind.TIMEOUT.operator_error_code is None
    assert st.StopRequestKind.PAUSE.stop_kind is st.StopKind.OPERATOR_PAUSE


# -- options ----------------------------------------------------------------


def test_session_options_default_to_completion_with_replace():
    options = st.SessionOptions.from_job_options({})
    assert options.mode is st.SessionMode.COMPLETION
    assert options.default_system_prompt_mode == "replace"
    agent = st.SessionOptions.from_job_options({st.OPTION_SESSION: "agent", st.OPTION_RESUME_SESSION: SID})
    assert agent.default_system_prompt_mode == "append" and agent.resume_session == SID


@pytest.mark.parametrize(
    "options, match",
    [
        ({st.OPTION_SESSION: "interactive"}, "session must be one of"),
        ({st.OPTION_RESUME_SESSION: SID}, "requires session: agent"),
        ({st.OPTION_SESSION: "completion", st.OPTION_EOF_WAIT_S: 5}, "require session: agent"),
        ({st.OPTION_SESSION: "agent", st.OPTION_RESUME_SESSION: "--fork-session"}, "UUID"),
        ({st.OPTION_SESSION: "agent", st.OPTION_RESUME_SESSION: SID.upper()}, "canonical"),
        ({st.OPTION_SESSION: "agent", st.OPTION_QUIESCENCE_S: -1}, "quiescence_s"),
    ],
)
def test_session_options_reject(options, match):
    with pytest.raises(ValueError, match=match):
        st.SessionOptions.from_job_options(options)


def test_session_option_keys_are_the_six_job_level_keys():
    assert st.SESSION_OPTION_KEYS == {
        "session", "resume_session", "session_quiescence_s", "session_result_wait_s",
        "session_eof_wait_s", "session_sigint_wait_s",
    }


# -- layout -----------------------------------------------------------------


def test_session_paths_layout():
    paths = st.SessionPaths.for_attempt(ROOT, "fix/the thing", 3)
    component = st.encode_job_id("fix/the thing")
    assert paths.attempt_dir == ROOT / component / "attempt-3"
    assert component.startswith("fix_the_thing-") and len(component.split("-")[-1]) == 12
    assert [p.name for p in (paths.events, paths.mailbox, paths.mailbox_state, paths.mailbox_lock, paths.session_record)] == [
        "events.jsonl", "mailbox.jsonl", "mailbox-state.json", "mailbox.lock", "session.json",
    ]
    assert st.encode_job_id("a/b") != st.encode_job_id("a_b")
    assert st.encode_job_id("...").startswith("job-")


@pytest.mark.parametrize("attempt_no", [0, -1, True, 1.0])
def test_session_paths_reject_a_bad_attempt_number(attempt_no):
    with pytest.raises(ValueError, match="attempt_no"):
        st.SessionPaths.for_attempt(ROOT, "job", attempt_no)


def test_session_paths_reject_relative_roots_and_empty_ids():
    with pytest.raises(ValueError, match="absolute"):
        st.SessionPaths.for_attempt(Path("rel"), "job", 1)
    with pytest.raises(ValueError, match="nonempty"):
        st.encode_job_id("  ")


def test_schema_file_names():
    assert st.SESSION_SCHEMA_FILES == (
        "mailbox_record.json", "mailbox_state.json", "session_record.json", "event_line.json",
    )


# -- mailbox ----------------------------------------------------------------


def test_mailbox_records_enforce_kind_shape():
    st.MailboxRecord(1, "a", "2026-11-14T00:00:00Z", "message", text="hi")
    st.MailboxRecord(2, "b", "2026-11-14T00:00:00Z", "stop", stop=st.StopRequest("pause", "finish_turn"))
    with pytest.raises(ValueError, match="message text"):
        st.MailboxRecord(1, "a", "t", "message")
    with pytest.raises(ValueError, match="requires a StopRequest"):
        st.MailboxRecord(1, "a", "t", "stop")
    with pytest.raises(ValueError, match="no text"):
        st.MailboxRecord(1, "a", "t", "stop", text="x", stop=st.StopRequest())
    with pytest.raises(ValueError, match="seq"):
        st.MailboxRecord(0, "a", "t", "message", text="hi")
    with pytest.raises(ValueError):
        st.MailboxRecord(1, "a", "t", "note", text="hi")


def test_mailbox_state_delivery_rule():
    open_state = st.MailboxState(forwarded_through_seq=2)
    assert open_state.delivery(2) is st.MessageDelivery.FORWARDED
    assert open_state.delivery(3) is st.MessageDelivery.PENDING
    closed = st.MailboxState(2, True, "t", 4, "quiescent")
    assert closed.close_reason is st.StopKind.QUIESCENT
    assert closed.delivery(3) is st.MessageDelivery.UNDELIVERED
    assert closed.delivery(9) is st.MessageDelivery.UNDELIVERED  # appended after closure


@pytest.mark.parametrize(
    "kwargs",
    [
        {"closed": False, "closed_at": "t"},
        {"closed": True, "closed_at": "t", "closed_through_seq": 1},
        {"closed": True, "closed_at": "t", "closed_through_seq": 1, "close_reason": "quiescent",
         "forwarded_through_seq": 2},
        {"forwarded_through_seq": -1},
    ],
)
def test_mailbox_state_rejects_inconsistent_closure(kwargs):
    with pytest.raises(ValueError):
        st.MailboxState(**kwargs)


# -- records, request, outcome ----------------------------------------------


def test_session_record_end_fields_are_set_together():
    base = dict(run_id="r", job_id="j", attempt_no=1, session_id=SID, resumed=False,
                cwd=ROOT, created_at="t")
    st.SessionRecord(**base)
    st.SessionRecord(**base, ended_at="t", stop_kind="timeout", shutdown_confirmed=True)
    with pytest.raises(ValueError, match="set together"):
        st.SessionRecord(**base, ended_at="t")
    with pytest.raises(ValueError, match="UUID"):
        st.SessionRecord(**{**base, "session_id": "nope"})


def _request(**overrides):
    values = dict(
        system="", user="do it", model="opus", options=BackendOptions(), cwd=ROOT,
        session_id=str(uuid.uuid4()), resume=False,
        paths=st.SessionPaths.for_attempt(ROOT, "j", 1), bounds=st.SessionBounds(), channel=_Channel(),
    )
    values.update(overrides)
    return st.SessionRequest(**values)


def test_session_request_validation():
    _request()
    with pytest.raises(ValueError, match="differs"):
        _request(options=BackendOptions(cwd=ROOT / "other"))
    with pytest.raises(ValueError, match="absolute"):
        _request(cwd=Path("rel"))
    with pytest.raises(ValueError, match="SessionChannel"):
        _request(channel=object())
    with pytest.raises(ValueError, match="user prompt"):
        _request(user="")


def test_session_outcome_invariants():
    response = LLMResponse(text="done", model="opus")
    st.SessionOutcome(response, SID, ROOT, "quiescent", "success", True)
    st.SessionOutcome(None, SID, ROOT, "process_exit", None, False)
    with pytest.raises(ValueError, match="exactly when"):
        st.SessionOutcome(response, SID, ROOT, "quiescent", None, True)
    with pytest.raises(ValueError, match="quiescent"):
        st.SessionOutcome(None, SID, ROOT, "quiescent", None, True)
    with pytest.raises(ValueError, match="init_mismatch"):
        st.SessionOutcome(response, SID, ROOT, "init_mismatch", "success", True)


def test_workspace_finalize_result_sha_rule():
    st.WorkspaceFinalizeResult("committed", "abc123")
    st.WorkspaceFinalizeResult("dirty_held", detail="hook rejected")
    with pytest.raises(ValueError):
        st.WorkspaceFinalizeResult("committed")
    with pytest.raises(ValueError):
        st.WorkspaceFinalizeResult("clean", "abc123")


# -- ledger 14 --------------------------------------------------------------


def test_ledger_v14_columns_are_pinned():
    assert st.SESSION_LEDGER_SCHEMA == 14
    assert st.SESSION_LEDGER_TABLES == ("attempts", "reservations")
    assert [(c.name, c.sql, c.pre_v14) for c in st.SESSION_LEDGER_COLUMNS] == [
        ("session_mode", "TEXT NOT NULL DEFAULT 'completion'", "completion"),
        ("session_id", "TEXT", None),
        ("session_cwd", "TEXT", None),
        ("session_dir", "TEXT", None),
        ("stop_kind", "TEXT", None),
    ]


def test_public_names_resolve():
    for name in st.__all__:
        assert hasattr(st, name), name
