"""Tests for llm_kit.adapters.claude_stream: line parsing, init checks, the stream verdict."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from llm_kit.adapters.claude_stream import StreamVerdict, init_problem, parse_stream_line
from llm_kit.completion.session_types import AGENT_PERMISSION_MODE

SID = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-2222-4333-8444-555555555555"


def init_event(tmp_path: Path, **overrides):
    event = {"type": "system", "subtype": "init", "session_id": SID, "cwd": str(tmp_path),
             "permissionMode": AGENT_PERMISSION_MODE}
    event.update(overrides)
    return event


def result_event(session_id: str = SID, **extra):
    return {"type": "result", "subtype": "success", "session_id": session_id, "is_error": False,
            "result": "ok", **extra}


def verdict(tmp_path: Path) -> StreamVerdict:
    return StreamVerdict(session_id=SID, cwd=tmp_path, permission_mode=AGENT_PERMISSION_MODE)


def problem(tmp_path: Path, event):
    return init_problem(event, session_id=SID, cwd=tmp_path, permission_mode=AGENT_PERMISSION_MODE)


# -- parse_stream_line -----------------------------------------------------------------

def test_parse_returns_the_object_for_text_and_bytes():
    assert parse_stream_line('{"a": 1}\n') == {"a": 1}
    assert parse_stream_line(b'{"a": 1}\r\n') == {"a": 1}


@pytest.mark.parametrize("line", ["", "   \n", "not json", '{"a": ', "[1, 2]", '"text"', "3", "null", "\x00"])
def test_parse_rejects_non_json_and_non_object_lines(line):
    assert parse_stream_line(line) is None


def test_parse_replaces_undecodable_bytes_instead_of_raising():
    assert parse_stream_line(b'{"a": "\xff"}') == {"a": "�"}
    assert parse_stream_line(b"\xff\xfe") is None


def test_parse_refuses_a_non_text_argument():
    with pytest.raises(TypeError):
        parse_stream_line(None)  # type: ignore[arg-type]


# -- init_problem ----------------------------------------------------------------------

def test_a_matching_init_has_no_problem(tmp_path):
    assert problem(tmp_path, init_event(tmp_path)) is None


def test_init_cwd_is_compared_by_realpath(tmp_path):
    link = tmp_path.parent / (tmp_path.name + "-link")
    link.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert problem(tmp_path, init_event(tmp_path, cwd=str(link))) is None
    finally:
        link.unlink()


def test_init_cwd_mismatch_is_a_problem(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    text = problem(tmp_path, init_event(tmp_path, cwd=str(other)))
    assert text is not None and text.startswith("cwd ") and str(other) in text


@pytest.mark.parametrize("cwd", [None, 7, "", ["x"]])
def test_init_cwd_missing_or_invalid_is_a_problem_not_an_exception(tmp_path, monkeypatch, cwd):
    monkeypatch.chdir(tmp_path)  # an empty path resolves to the process cwd: the expected one here
    event = init_event(tmp_path)
    event["cwd"] = cwd
    assert problem(tmp_path, event).startswith("cwd ")
    del event["cwd"]
    assert problem(tmp_path, event).startswith("cwd ")


@pytest.mark.parametrize("value", [None, 7, OTHER, ""])
def test_init_session_id_missing_or_wrong_is_a_problem(tmp_path, value):
    event = init_event(tmp_path)
    event["session_id"] = value
    assert problem(tmp_path, event).startswith("session_id ")
    del event["session_id"]
    assert problem(tmp_path, event).startswith("session_id ")


def test_init_permission_mode_mismatch_is_a_problem(tmp_path):
    assert problem(tmp_path, init_event(tmp_path, permissionMode="default")).startswith("permissionMode ")
    event = init_event(tmp_path)
    del event["permissionMode"]
    assert problem(tmp_path, event).startswith("permissionMode ")


def test_init_problems_are_reported_in_a_fixed_order(tmp_path):
    event = init_event(tmp_path, session_id=OTHER, cwd="/nowhere", permissionMode="default")
    assert problem(tmp_path, event).startswith("session_id ")
    event["session_id"] = SID
    assert problem(tmp_path, event).startswith("cwd ")


# -- StreamVerdict ---------------------------------------------------------------------

def test_observe_returns_a_zero_based_index_over_every_event(tmp_path):
    v = verdict(tmp_path)
    assert v.events_seen == 0
    assert v.observe({"type": "assistant"}) == 0
    assert v.observe(init_event(tmp_path)) == 1
    assert v.observe({"type": "user"}) == 2
    assert v.observe(result_event()) == 3
    assert v.events_seen == 4


def test_observe_refuses_a_non_mapping(tmp_path):
    with pytest.raises(TypeError):
        verdict(tmp_path).observe("not an event")  # type: ignore[arg-type]


def test_an_empty_verdict_answers_none_everywhere(tmp_path):
    v = verdict(tmp_path)
    assert (v.inits, v.results) == ((), ())
    assert v.first_init is None and v.first_problem is None and v.first_valid_init is None
    assert v.first_result_after_init is None and v.last_init_index is None and v.last_result is None
    assert v.has_result_after(-1) is False


def test_inits_and_results_keep_arrival_order_and_indexes(tmp_path):
    v = verdict(tmp_path)
    first, second = init_event(tmp_path), init_event(tmp_path)
    r1, r2 = result_event(), result_event(OTHER)
    for event in (first, {"type": "assistant"}, r1, second, r2):
        v.observe(event)
    assert v.inits == ((0, first), (3, second))
    assert v.results == ((2, r1), (4, r2))
    assert v.last_init_index == 3 and v.last_result == (4, r2)


def test_first_init_and_problem_describe_the_first_init_only(tmp_path):
    v = verdict(tmp_path)
    bad = init_event(tmp_path, cwd="/nowhere")
    good = init_event(tmp_path)
    v.observe(bad)
    v.observe(good)
    assert v.first_init is bad
    assert v.first_problem is not None and v.first_problem.startswith("cwd ")
    assert v.first_valid_init == (1, good)


def test_first_valid_init_is_the_first_init_without_a_problem(tmp_path):
    v = verdict(tmp_path)
    v.observe(init_event(tmp_path, permissionMode="default"))
    assert v.first_valid_init is None
    good = init_event(tmp_path)
    v.observe(good)
    v.observe(init_event(tmp_path))
    assert v.first_valid_init == (1, good)


def test_a_result_before_the_first_valid_init_is_never_the_relay_result(tmp_path):
    v = verdict(tmp_path)
    v.observe(result_event(result="stale"))
    assert v.first_result_after_init is None
    v.observe(init_event(tmp_path))
    assert v.first_result_after_init is None
    fresh = result_event(result="fresh")
    v.observe(fresh)
    v.observe(result_event(result="later"))
    assert v.first_result_after_init == (2, fresh)


def test_the_relay_result_must_carry_the_expected_session_id(tmp_path):
    v = verdict(tmp_path)
    v.observe(init_event(tmp_path))
    v.observe(result_event(OTHER))
    assert v.first_result_after_init is None
    mine = result_event()
    v.observe(mine)
    assert v.first_result_after_init == (2, mine)


def test_a_result_after_only_an_invalid_init_does_not_qualify(tmp_path):
    v = verdict(tmp_path)
    v.observe(init_event(tmp_path, cwd="/nowhere"))
    v.observe(result_event())
    assert v.first_result_after_init is None


def test_has_result_after_counts_results_of_any_session(tmp_path):
    v = verdict(tmp_path)
    v.observe(init_event(tmp_path))
    v.observe(result_event(OTHER))
    assert v.has_result_after(0) is True
    assert v.has_result_after(1) is False
    assert v.has_result_after(-1) is True


def test_the_stop_idiom_excludes_results_already_observed(tmp_path):
    v = verdict(tmp_path)
    v.observe(init_event(tmp_path))
    v.observe(result_event())
    mark = v.events_seen - 1
    assert v.has_result_after(mark) is False
    v.observe({"type": "assistant"})
    assert v.has_result_after(mark) is False
    v.observe(result_event())
    assert v.has_result_after(mark) is True


@pytest.mark.parametrize("bad", [1.5, "1", None, True])
def test_has_result_after_refuses_a_non_int(tmp_path, bad):
    with pytest.raises(TypeError):
        verdict(tmp_path).has_result_after(bad)  # type: ignore[arg-type]


def test_a_later_init_voids_the_earlier_results_quiescence_timer(tmp_path):
    v = verdict(tmp_path)
    v.observe(init_event(tmp_path))
    v.observe(result_event())
    index, _ = v.last_result
    assert not (v.last_init_index > index)
    v.observe(init_event(tmp_path))
    assert v.last_init_index > index


def test_the_verdict_tolerates_concurrent_observation(tmp_path):
    v = verdict(tmp_path)
    def feed():
        for _ in range(200):
            v.observe({"type": "assistant"})
            _ = v.first_result_after_init, v.inits
    threads = [threading.Thread(target=feed) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert v.events_seen == 800


def test_parse_then_observe_round_trip(tmp_path):
    v = verdict(tmp_path)
    lines = [json.dumps(init_event(tmp_path)), "garbage", json.dumps(result_event()), "[1]"]
    for line in lines:
        event = parse_stream_line(line)
        if event is not None:
            v.observe(event)
    assert v.events_seen == 2 and v.first_result_after_init is not None
