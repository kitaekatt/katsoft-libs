"""The agent-session directory: mailbox protocol, records, cleanup identity and schemas.

Covers ``llm_kit.jobs.session`` (unit L1b). Every reader and writer test
validates the bytes on disk against the JSON Schema file of its format
(repository AGENTS.md, bar line 6), through the repository's own stdlib
validator ``llm_kit.schema.json_schema``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from importlib import resources
from pathlib import Path

import pytest

import llm_kit
from llm_kit.completion import session_types as st
from llm_kit.jobs import session as sess
from llm_kit.schema.json_schema import check_schema, validate

SID = "0b9f3c3e-8a51-4a0e-9a4f-3d2f7c1e5b10"
TAG = "lst_0123456789abcdef"
NOW = "2026-11-14T10:00:00.000000+00:00"
LATER = "2026-11-14T10:30:00.000000+00:00"
SCHEMA_DIR = resources.files("llm_kit.jobs") / "schemas"


def schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def conformance(name: str, document) -> tuple:
    """The schema errors of ``document`` against the schema file ``name`` (empty = conforms)."""
    loaded = schema(name)
    check_schema(loaded)
    return validate(loaded, document)


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().split(b"\n") if line]


def check_file(path: Path, name: str) -> dict:
    document = json.loads(path.read_text(encoding="ascii"))
    assert conformance(name, document) == (), (path.name, conformance(name, document))
    return document


@pytest.fixture
def paths(tmp_path):
    value = st.SessionPaths.for_attempt(tmp_path.resolve() / "sessions", "unit/7", 1)
    sess.create_attempt_dir(value)
    return value


def session_record(**changes) -> st.SessionRecord:
    base = dict(run_id="run-1", job_id="unit/7", attempt_no=1, session_id=SID, resumed=False,
                cwd=Path("/work/unit-7"), process_tag=TAG, created_at=NOW)
    return st.SessionRecord(**{**base, **changes})


def cleanup_record(**changes) -> st.CleanupRecord:
    base = dict(tag=TAG, leader=st.ProcessIdentity(4242, 1794000000.5), started_at=NOW, updated_at=NOW,
                process_group=4242)
    return st.CleanupRecord(**{**base, **changes})


# -- schema files -------------------------------------------------------------


def test_every_named_schema_file_ships_and_is_in_the_validator_subset():
    shipped = sorted(entry.name for entry in SCHEMA_DIR.iterdir() if entry.name.endswith(".json"))
    assert shipped == sorted(st.SESSION_SCHEMA_FILES)
    for name in st.SESSION_SCHEMA_FILES:
        text = (SCHEMA_DIR / name).read_text(encoding="utf-8")
        assert text.isascii()
        loaded = json.loads(text)
        check_schema(loaded)
        assert loaded["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_the_event_line_schema_is_the_design_11_4_document():
    assert schema(st.EVENT_LINE_SCHEMA_FILE) == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "agent-session event line (opaque Claude stream-json)",
        "type": "object",
        "required": ["type"],
        "properties": {"type": {"type": "string"}},
        "additionalProperties": True,
    }
    assert conformance(st.EVENT_LINE_SCHEMA_FILE, {"type": "assistant", "anything": [1, {"x": None}]}) == ()
    assert conformance(st.EVENT_LINE_SCHEMA_FILE, {"no_type": 1}) != ()
    assert conformance(st.EVENT_LINE_SCHEMA_FILE, ["type"]) != ()


@pytest.mark.parametrize("name", [n for n in st.SESSION_SCHEMA_FILES if n != st.EVENT_LINE_SCHEMA_FILE])
def test_every_schema_example_conforms_and_a_foreign_key_does_not(name):
    loaded = schema(name)
    assert loaded["examples"]
    for example in loaded["examples"]:
        assert validate(loaded, example) == ()
        assert validate(loaded, {**example, "unknown": 1}) != ()


def test_the_closure_and_end_fields_are_tied_together_by_the_schemas():
    open_with_reason = {"schema": 1, "forwarded_through_seq": 0, "closed": False, "closed_at": None,
                        "closed_through_seq": None, "close_reason": "quiescent"}
    assert conformance(st.MAILBOX_STATE_SCHEMA_FILE, open_with_reason) != ()
    closed_without = {**open_with_reason, "closed": True, "close_reason": None}
    assert conformance(st.MAILBOX_STATE_SCHEMA_FILE, closed_without) != ()
    record = json.loads(json.dumps(sess._session_document(session_record())))
    assert conformance(st.SESSION_RECORD_SCHEMA_FILE, {**record, "ended_at": NOW}) != ()
    message = {"schema": 1, "seq": 1, "id": "a", "at": NOW, "kind": "message", "text": "hi", "stop": None}
    assert conformance(st.MAILBOX_RECORD_SCHEMA_FILE, {**message, "kind": "stop"}) != ()
    assert conformance(st.MAILBOX_RECORD_SCHEMA_FILE, {**message, "text": None}) != ()


# -- the session channel type -------------------------------------------------


def test_the_file_channel_implements_the_session_channel_protocol(paths):
    channel = sess.FileSessionChannel(paths)
    assert isinstance(channel, st.SessionChannel)
    with pytest.raises(TypeError):
        sess.FileSessionChannel(str(paths.attempt_dir))


# -- create_attempt_dir -------------------------------------------------------


def test_create_attempt_dir_makes_an_empty_open_mailbox_and_keeps_it_on_a_second_call(paths):
    assert paths.attempt_dir.is_dir()
    assert paths.mailbox.read_bytes() == b""
    assert check_file(paths.mailbox_state, st.MAILBOX_STATE_SCHEMA_FILE) == {
        "schema": 1, "forwarded_through_seq": 0, "closed": False, "closed_at": None,
        "closed_through_seq": None, "close_reason": None}
    assert sess.read_state(paths) == st.MailboxState()
    assert sess.read_records(paths) == ()
    sess.append_message(paths, "kept")
    sess.FileSessionChannel(paths).close(st.StopKind.QUIESCENT, NOW)
    sess.create_attempt_dir(paths)
    assert sess.read_state(paths).closed and len(sess.read_records(paths)) == 1
    if os.name == "posix":
        assert paths.attempt_dir.stat().st_mode & 0o777 == 0o700
        assert paths.mailbox.stat().st_mode & 0o777 == 0o600


def test_create_attempt_dir_rejects_a_non_paths_value(tmp_path):
    with pytest.raises(TypeError):
        sess.create_attempt_dir(tmp_path)


@pytest.mark.parametrize("call", [
    lambda p: sess.append_message(p, "x"),
    lambda p: sess.request_stop(p, st.StopRequestKind.STOP, st.StopMode.INTERRUPT),
    lambda p: sess.read_state(p),
    lambda p: sess.read_records(p),
    lambda p: sess.read_session_record(p),
    lambda p: sess.read_cleanup_record(p),
    lambda p: sess.write_session_record(p, session_record()),
    lambda p: sess.FileSessionChannel(p).poll(0),
    lambda p: sess.FileSessionChannel(p).close(st.StopKind.TIMEOUT, NOW),
])
def test_a_missing_attempt_directory_is_refused_and_never_created(tmp_path, call):
    missing = st.SessionPaths.for_attempt(tmp_path.resolve(), "ghost", 3)
    with pytest.raises(FileNotFoundError):
        call(missing)
    assert not missing.attempt_dir.exists()


# -- mailbox appends ----------------------------------------------------------


def test_append_message_numbers_records_from_one_and_writes_schema_valid_ascii_lines(paths):
    first = sess.append_message(paths, "hello", at=NOW)
    second = sess.append_message(paths, "café ☃ line\nbreak", message_id="m-2")
    assert (first.seq, second.seq) == (1, 2)
    assert first.kind is st.MailboxRecordKind.MESSAGE and first.at == NOW and len(first.id) == 32
    assert second.id == "m-2" and second.text == "café ☃ line\nbreak"
    raw = paths.mailbox.read_bytes()
    assert raw.isascii() and raw.count(b"\n") == 2 and raw.endswith(b"\n")
    for document in lines(paths.mailbox):
        assert conformance(st.MAILBOX_RECORD_SCHEMA_FILE, document) == ()
    assert sess.read_records(paths) == (first, second)


@pytest.mark.parametrize("text", ["", "   ", None, 5])
def test_append_message_refuses_a_bad_text_and_writes_nothing(paths, text):
    with pytest.raises(ValueError):
        sess.append_message(paths, text)
    assert paths.mailbox.read_bytes() == b""


def test_append_message_refuses_a_bad_id_and_nothing_is_written(paths):
    with pytest.raises(ValueError):
        sess.append_message(paths, "ok", message_id=" ")
    assert paths.mailbox.read_bytes() == b""


def test_request_stop_travels_through_the_mailbox_as_a_stop_record(paths):
    sess.append_message(paths, "m")
    record = sess.request_stop(paths, st.StopRequestKind.PAUSE, st.StopMode.FINISH_TURN, at=LATER)
    assert record.seq == 2 and record.kind is st.MailboxRecordKind.STOP and record.at == LATER
    assert record.stop == st.StopRequest(st.StopRequestKind.PAUSE, st.StopMode.FINISH_TURN) and record.text is None
    document = lines(paths.mailbox)[1]
    assert document["stop"] == {"kind": "pause", "mode": "finish_turn"} and document["text"] is None
    assert conformance(st.MAILBOX_RECORD_SCHEMA_FILE, document) == ()
    assert sess.read_records(paths)[1] == record
    with pytest.raises(ValueError):
        sess.request_stop(paths, "halt", st.StopMode.INTERRUPT)
    with pytest.raises(ValueError):
        sess.request_stop(paths, st.StopRequestKind.STOP, "now")
    assert len(sess.read_records(paths)) == 2


def test_a_torn_final_line_is_not_a_record_is_not_counted_and_is_removed_by_the_next_append(paths):
    first = sess.append_message(paths, "one")
    with paths.mailbox.open("ab") as stream:
        stream.write(b'{"schema":1,"seq":2,"id":"torn","at":"2026')
    assert sess.read_records(paths) == (first,)
    assert sess.FileSessionChannel(paths).poll(1) == ()
    second = sess.append_message(paths, "two")
    assert second.seq == 2
    assert paths.mailbox.read_bytes().count(b"\n") == 2
    assert [r["text"] for r in lines(paths.mailbox)] == ["one", "two"]
    assert sess.read_records(paths) == (first, second)


def test_a_malformed_or_misnumbered_complete_line_is_refused_loudly(paths):
    sess.append_message(paths, "one")
    good = paths.mailbox.read_bytes()
    paths.mailbox.write_bytes(good + b"not json\n")
    with pytest.raises(ValueError, match="line 2"):
        sess.read_records(paths)
    with pytest.raises(ValueError, match="line 2"):
        sess.append_message(paths, "x")
    paths.mailbox.write_bytes(good + good)  # the second line repeats seq 1
    with pytest.raises(ValueError, match="numbered"):
        sess.read_records(paths)
    paths.mailbox.write_bytes(good.replace(b'"schema":1', b'"schema":2'))
    with pytest.raises(ValueError, match="schema"):
        sess.read_records(paths)
    paths.mailbox.write_bytes(good.replace(b'"kind":"message"', b'"kind":"shout"'))
    with pytest.raises(ValueError):
        sess.read_records(paths)


_APPENDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from llm_kit.completion.session_types import SessionPaths
    from llm_kit.jobs import session

    paths = SessionPaths(Path(sys.argv[1]))
    label = sys.argv[2]
    for number in range(int(sys.argv[3])):
        session.append_message(paths, "%s-%d" % (label, number))
    """
)


def test_concurrent_appends_from_separate_processes_get_unique_consecutive_seq(paths):
    source = str(Path(llm_kit.__file__).resolve().parents[1])
    environment = {**os.environ, "PYTHONPATH": source + os.pathsep + os.environ.get("PYTHONPATH", "")}
    count = 40
    processes = [
        subprocess.Popen([sys.executable, "-c", _APPENDER, str(paths.attempt_dir), label, str(count)], env=environment)
        for label in ("a", "b", "c")
    ]
    assert [process.wait(timeout=120) for process in processes] == [0, 0, 0]
    records = sess.read_records(paths)
    assert [record.seq for record in records] == list(range(1, 3 * count + 1))
    assert len({record.id for record in records}) == 3 * count
    for label in ("a", "b", "c"):
        mine = [record.text for record in records if record.text.startswith(label + "-")]
        assert mine == [f"{label}-{n}" for n in range(count)]  # per-process order kept
    for document in lines(paths.mailbox):
        assert conformance(st.MAILBOX_RECORD_SCHEMA_FILE, document) == ()


# -- forwarding, closure, delivery --------------------------------------------


def test_delivery_goes_pending_then_forwarded_and_an_append_after_closure_is_undelivered(paths):
    channel = sess.FileSessionChannel(paths)
    a = sess.append_message(paths, "a")
    b = sess.append_message(paths, "b")
    assert sess.read_state(paths).delivery(a.seq) is st.MessageDelivery.PENDING
    channel.mark_forwarded(1)
    state = sess.read_state(paths)
    assert state.delivery(1) is st.MessageDelivery.FORWARDED and state.delivery(2) is st.MessageDelivery.PENDING
    closed = channel.close(st.StopKind.QUIESCENT, LATER)
    assert closed.closed and closed.closed_through_seq == 2 and closed.forwarded_through_seq == 1
    assert sess.read_state(paths).delivery(b.seq) is st.MessageDelivery.UNDELIVERED
    late = sess.append_message(paths, "too late")
    late_stop = sess.request_stop(paths, st.StopRequestKind.STOP, st.StopMode.INTERRUPT)
    assert (late.seq, late_stop.seq) == (3, 4)
    state = sess.read_state(paths)
    assert state.delivery(3) is st.MessageDelivery.UNDELIVERED and state.delivery(4) is st.MessageDelivery.UNDELIVERED
    assert state.closed_through_seq == 2  # closure is not rewritten by a late append
    assert len(sess.read_records(paths)) == 4
    check_file(paths.mailbox_state, st.MAILBOX_STATE_SCHEMA_FILE)


def test_poll_returns_records_after_a_seq_in_order_and_nothing_once_closed(paths):
    channel = sess.FileSessionChannel(paths)
    assert channel.poll(0) == ()
    records = [sess.append_message(paths, text) for text in ("a", "b", "c")]
    assert channel.poll(0) == tuple(records)
    assert channel.poll(2) == (records[2],)
    assert channel.poll(3) == ()
    channel.close(st.StopKind.OPERATOR_STOP, LATER)
    assert channel.poll(0) == ()
    for bad in (-1, 1.5, "1", True, None):
        with pytest.raises(ValueError):
            channel.poll(bad)


def test_mark_forwarded_demands_the_next_existing_record_of_an_open_mailbox(paths):
    channel = sess.FileSessionChannel(paths)
    with pytest.raises(ValueError, match="no such"):
        channel.mark_forwarded(1)
    sess.append_message(paths, "a")
    sess.append_message(paths, "b")
    with pytest.raises(ValueError, match="next record"):
        channel.mark_forwarded(2)
    channel.mark_forwarded(1)
    with pytest.raises(ValueError, match="next record"):
        channel.mark_forwarded(1)
    channel.mark_forwarded(2)
    sess.append_message(paths, "c")
    channel.close(st.StopKind.QUIESCENT, LATER)
    with pytest.raises(ValueError, match="closed"):
        channel.mark_forwarded(3)
    assert sess.read_state(paths).forwarded_through_seq == 2


def test_close_is_idempotent_keeps_the_first_reason_and_validates_its_arguments(paths):
    channel = sess.FileSessionChannel(paths)
    first = channel.close(st.StopKind.TIMEOUT, NOW)
    again = channel.close(st.StopKind.QUIESCENT, LATER)
    assert again == first and again.close_reason is st.StopKind.TIMEOUT and again.closed_at == NOW
    assert sess.read_state(paths) == first
    other = st.SessionPaths.for_attempt(paths.attempt_dir.parent, "other", 1)
    sess.create_attempt_dir(other)
    with pytest.raises(ValueError):
        sess.FileSessionChannel(other).close("done", NOW)
    with pytest.raises(ValueError):
        sess.FileSessionChannel(other).close(st.StopKind.QUIESCENT, " ")
    assert not sess.read_state(other).closed


@pytest.mark.parametrize("mutate", [
    lambda d: {**d, "extra": 1},
    lambda d: {k: v for k, v in d.items() if k != "closed"},
    lambda d: {**d, "schema": 2},
    lambda d: {**d, "schema": True},
    lambda d: {**d, "closed": True},
    lambda d: {**d, "forwarded_through_seq": -1},
    lambda d: {**d, "close_reason": "quiescent"},
])
def test_a_state_file_that_does_not_match_its_format_is_refused(paths, mutate):
    document = json.loads(paths.mailbox_state.read_text())
    paths.mailbox_state.write_text(json.dumps(mutate(document)))
    with pytest.raises(ValueError):
        sess.read_state(paths)
    with pytest.raises(ValueError):
        sess.FileSessionChannel(paths).poll(0)


def test_a_state_file_that_is_not_json_is_refused(paths):
    paths.mailbox_state.write_text("{")
    with pytest.raises(ValueError, match="not valid JSON"):
        sess.read_state(paths)


# -- session.json -------------------------------------------------------------


def test_session_record_round_trips_validates_and_replaces_atomically(paths):
    record = session_record()
    sess.write_session_record(paths, record)
    document = check_file(paths.session_record, st.SESSION_RECORD_SCHEMA_FILE)
    assert document["cwd"] == "/work/unit-7" and document["first_init_at"] is None
    assert sess.read_session_record(paths) == record
    ended = replace(record, ended_at=LATER, stop_kind=st.StopKind.QUIESCENT, result_subtype="success",
                    shutdown_confirmed=True)
    sess.write_session_record(paths, ended)
    check_file(paths.session_record, st.SESSION_RECORD_SCHEMA_FILE)
    assert sess.read_session_record(paths) == ended
    assert [p.name for p in paths.attempt_dir.iterdir() if p.name.startswith(".")] == []  # no temporary left
    with pytest.raises(TypeError):
        sess.write_session_record(paths, {"run_id": "x"})


def test_a_session_record_rewrite_cannot_change_identity_or_lose_the_first_init(paths):
    sess.write_session_record(paths, session_record())
    channel = sess.FileSessionChannel(paths)
    for change in (dict(run_id="run-2"), dict(job_id="other"), dict(attempt_no=2), dict(resumed=True),
                   dict(session_id="1b9f3c3e-8a51-4a0e-9a4f-3d2f7c1e5b10"), dict(cwd=Path("/elsewhere")),
                   dict(process_tag="lst_ffffffffffffffff"), dict(created_at=LATER)):
        with pytest.raises(ValueError, match="cannot change"):
            sess.write_session_record(paths, session_record(**change))
    channel.record_first_init(NOW)
    with pytest.raises(ValueError, match="first_init_at"):
        sess.write_session_record(paths, session_record())  # a stale copy would drop it
    with pytest.raises(ValueError, match="first_init_at"):
        sess.write_session_record(paths, session_record(first_init_at=LATER))
    sess.write_session_record(paths, session_record(first_init_at=NOW, ended_at=LATER,
                                                    stop_kind=st.StopKind.TIMEOUT, shutdown_confirmed=False))
    assert sess.read_session_record(paths).first_init_at == NOW


def test_record_first_init_stamps_once_and_needs_a_session_record(paths):
    channel = sess.FileSessionChannel(paths)
    with pytest.raises(FileNotFoundError):
        channel.record_first_init(NOW)
    sess.write_session_record(paths, session_record())
    channel.record_first_init(NOW)
    channel.record_first_init(LATER)
    assert sess.read_session_record(paths).first_init_at == NOW
    check_file(paths.session_record, st.SESSION_RECORD_SCHEMA_FILE)
    with pytest.raises(ValueError):
        channel.record_first_init("")


@pytest.mark.parametrize("mutate", [
    lambda d: {**d, "extra": 1},
    lambda d: {k: v for k, v in d.items() if k != "cwd"},
    lambda d: {**d, "schema": 0},
    lambda d: {**d, "cwd": "relative/path"},
    lambda d: {**d, "session_id": "not-a-uuid"},
    lambda d: {**d, "shutdown_confirmed": "yes", "ended_at": NOW, "stop_kind": "quiescent"},
    lambda d: {**d, "ended_at": NOW},
    lambda d: {**d, "first_init_at": 5},
])
def test_a_session_file_that_does_not_match_its_format_is_refused(paths, mutate):
    sess.write_session_record(paths, session_record())
    document = json.loads(paths.session_record.read_text())
    paths.session_record.write_text(json.dumps(mutate(document)))
    with pytest.raises(ValueError):
        sess.read_session_record(paths)


# -- cleanup.json -------------------------------------------------------------


def test_cleanup_is_absent_until_recorded_then_round_trips_and_validates(paths):
    sess.write_session_record(paths, session_record())
    channel = sess.FileSessionChannel(paths)
    assert sess.read_cleanup_record(paths) is None
    first = cleanup_record()
    channel.record_cleanup(first)
    check_file(paths.cleanup, st.CLEANUP_RECORD_SCHEMA_FILE)
    assert sess.read_cleanup_record(paths) == first
    grown = replace(first, updated_at=LATER, members=(st.ProcessIdentity(4300, 1794000001.25),))
    channel.record_cleanup(grown)
    grown = replace(grown, members=grown.members + (st.ProcessIdentity(4301, 1794000002.5),))
    channel.record_cleanup(grown)
    confirmed = replace(grown, confirmed_stopped_at=LATER)
    channel.record_cleanup(confirmed)
    channel.record_cleanup(confirmed)  # an identical rewrite is not a change
    document = check_file(paths.cleanup, st.CLEANUP_RECORD_SCHEMA_FILE)
    assert document["members"] == [{"pid": 4300, "create_time": 1794000001.25},
                                   {"pid": 4301, "create_time": 1794000002.5}]
    assert sess.read_cleanup_record(paths) == confirmed
    if os.name == "posix":
        assert paths.cleanup.stat().st_mode & 0o777 == 0o600


def test_detached_survivors_round_trip_and_validate(paths):
    sess.write_session_record(paths, session_record())
    channel = sess.FileSessionChannel(paths)
    survivors = (st.DetachedSurvivor(st.ProcessIdentity(4410, 1794000003.75), "/home/user/project"),
                 st.DetachedSurvivor(st.ProcessIdentity(4411, 1794000004.0), None))
    record = cleanup_record(members=(st.ProcessIdentity(4300, 1794000001.25),), confirmed_stopped_at=LATER,
                            detached_survivors=survivors)
    channel.record_cleanup(record)
    document = check_file(paths.cleanup, st.CLEANUP_RECORD_SCHEMA_FILE)
    assert document["detached_survivors"] == [
        {"pid": 4410, "create_time": 1794000003.75, "cwd": "/home/user/project"},
        {"pid": 4411, "create_time": 1794000004.0, "cwd": None},
    ]
    assert sess.read_cleanup_record(paths) == record
    for mutated in ({**document, "detached_survivors": [{"pid": 4410, "create_time": 1794000003.75}]},
                    {**document, "detached_survivors": "none"}):
        paths.cleanup.write_text(json.dumps(mutated))
        with pytest.raises(ValueError):
            sess.read_cleanup_record(paths)
    missing = {key: value for key, value in document.items() if key != "detached_survivors"}
    paths.cleanup.write_text(json.dumps(missing))
    with pytest.raises(ValueError):
        sess.read_cleanup_record(paths)


def test_a_cleanup_record_for_another_tag_or_without_a_session_record_is_refused(paths):
    channel = sess.FileSessionChannel(paths)
    with pytest.raises(FileNotFoundError):
        channel.record_cleanup(cleanup_record())
    sess.write_session_record(paths, session_record())
    with pytest.raises(ValueError, match="differs from the session record"):
        channel.record_cleanup(cleanup_record(tag="lst_ffffffffffffffff"))
    with pytest.raises(ValueError):
        channel.record_cleanup({"tag": TAG})
    assert not paths.cleanup.exists()


@pytest.mark.parametrize("change", [
    dict(leader=st.ProcessIdentity(4243, 1794000000.5)),
    dict(leader=st.ProcessIdentity(4242, 1794000009.0)),
    dict(process_group=4300),
    dict(process_group=None),
    dict(started_at="2026-11-14T09:00:00.000000+00:00"),
    dict(members=()),
    dict(members=(st.ProcessIdentity(4301, 1794000002.5),)),
], ids=["leader-pid", "leader-create-time", "process-group", "process-group-dropped", "started-at",
        "members-shrunk", "member-replaced"])
def test_a_cleanup_record_that_rewrites_identity_or_drops_a_member_is_refused(paths, change):
    sess.write_session_record(paths, session_record())
    channel = sess.FileSessionChannel(paths)
    base = cleanup_record(members=(st.ProcessIdentity(4300, 1794000001.25),))
    channel.record_cleanup(base)
    before = paths.cleanup.read_bytes()
    with pytest.raises(ValueError, match="cannot"):
        channel.record_cleanup(replace(base, updated_at=LATER, **change))
    assert paths.cleanup.read_bytes() == before


def test_a_cleanup_file_that_does_not_match_its_format_is_refused(paths):
    sess.write_session_record(paths, session_record())
    sess.FileSessionChannel(paths).record_cleanup(cleanup_record())
    document = json.loads(paths.cleanup.read_text())
    for mutated in ({**document, "extra": 1}, {**document, "schema": 3}, {**document, "members": "none"},
                    {**document, "leader": {"pid": 1}}, {**document, "tag": "short"}):
        paths.cleanup.write_text(json.dumps(mutated))
        with pytest.raises(ValueError):
            sess.read_cleanup_record(paths)
