"""The agent-session backend (``llm_kit.adapters.claude_session``) against a fake ``claude``.

The fake (``fixtures/fake_claude_session.py``) speaks stream-json on stdin and stdout the way Claude Code
2.1.325 does (agent-runs log.md, "verify-stream-json results"). Each test runs one real
:class:`ClaudeAgentSession` over a real session directory (``llm_kit.jobs.session``), so the mailbox, the
records and the process cleanup are the production ones. No model runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import pytest

from llm_kit.adapters.claude_session import (
    ClaudeAgentSession,
    interrupt_request_line,
    user_message_line,
)
from llm_kit.completion.contract_types import POLICY_VALIDATED_RESULT, OutputContract
from llm_kit.completion.session_types import (
    MessageDelivery,
    SessionBounds,
    SessionFailureKind,
    SessionPaths,
    SessionRecord,
    SessionRequest,
    StopKind,
    StopMode,
    StopRequestKind,
)
from llm_kit.completion.types import ERROR, BackendOptions
from llm_kit.jobs.session import (
    FileSessionChannel,
    append_message,
    create_attempt_dir,
    read_cleanup_record,
    read_state,
    request_stop,
    write_session_record,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a POSIX shell wrapper")

FAKE = Path(__file__).parent / "fixtures" / "fake_claude_session.py"
SESSION_ID = "0b6f2c1e-3d4a-4b5c-8d9e-0f1a2b3c4d5e"
FAST = SessionBounds(quiescence_s=0.5, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0)


def _fake_cli(directory: Path) -> Path:
    path = directory / "fake-claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="ascii")
    path.chmod(0o755)
    return path


class Harness:
    """One attempt directory, the fake CLI and its logs."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
        self.root = tmp_path
        self.cli = _fake_cli(tmp_path)
        self.log = tmp_path / "fake"
        monkeypatch.setenv("FAKE_SESSION_SCENARIO", scenario)
        monkeypatch.setenv("FAKE_SESSION_LOG", str(self.log))
        self.paths = SessionPaths.for_attempt(tmp_path / "sessions", "job-1", 1)
        create_attempt_dir(self.paths)
        self.cwd = tmp_path / "work"
        self.cwd.mkdir(exist_ok=True)
        self.tag = "lks_" + uuid.uuid4().hex

    def request(self, *, cwd: Optional[Path] = None, bounds: SessionBounds = FAST, timeout_s: float = 30.0,
                resume: bool = False, **options: object) -> SessionRequest:
        cwd = cwd or self.cwd
        write_session_record(self.paths, SessionRecord(
            run_id="run-1", job_id="job-1", attempt_no=1, session_id=SESSION_ID, resumed=resume, cwd=cwd,
            process_tag=self.tag, created_at="2026-11-14T00:00:00Z",
        ))
        opts = BackendOptions(timeout_s=timeout_s, cwd=cwd, system_prompt_mode="append", **options)  # type: ignore[arg-type]
        return SessionRequest(
            system="SYSTEM TEXT", user="hello", model="haiku", options=opts, cwd=cwd, session_id=SESSION_ID,
            resume=resume, process_tag=self.tag, paths=self.paths, bounds=bounds,
            channel=FileSessionChannel(self.paths),
        )

    def run(self, request: SessionRequest, during: Optional[Callable[[], None]] = None):
        backend = ClaudeAgentSession(executable=str(self.cli), poll_interval_s=0.02)
        if during is None:
            return backend.run(request)
        box: dict[str, object] = {}
        thread = threading.Thread(target=lambda: box.setdefault("outcome", backend.run(request)))
        thread.start()
        during()
        thread.join(timeout=60)
        assert not thread.is_alive(), "the session did not end"
        return box["outcome"]

    def events(self) -> list[dict]:
        if not self.paths.events.exists():
            return []
        return [json.loads(line) for line in self.paths.events.read_bytes().splitlines() if line.strip()]

    def wait_for(self, predicate: Callable[[list[dict]], bool], timeout_s: float = 20.0) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if predicate(self.events()):
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out; events: {self.events()}")

    def stdin_lines(self) -> list[bytes]:
        path = Path(str(self.log) + ".stdin.jsonl")
        return path.read_bytes().splitlines(keepends=True) if path.exists() else []

    def argv(self) -> dict:
        return json.loads(Path(str(self.log) + ".argv.json").read_text())


def _has_init(events: list[dict]) -> bool:
    return any(e.get("type") == "system" and e.get("subtype") == "init" for e in events)


def _results(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "result"]


def _stopper(h: Harness, mode: StopMode = StopMode.INTERRUPT, *, after: Callable[[list[dict]], bool] = _has_init,
             kind: StopRequestKind = StopRequestKind.STOP) -> Callable[[], None]:
    def during() -> None:
        h.wait_for(after)
        time.sleep(0.1)
        request_stop(h.paths, kind, mode)
    return during


def _controls_sent(h: Harness) -> list[dict]:
    return [json.loads(line) for line in h.stdin_lines() if b"control_request" in line]


# ---------------------------------------------------------------------------
# argv and the stream
# ---------------------------------------------------------------------------


def test_argv_is_the_stream_json_agent_shape(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    request = h.request(disallowed_tools="Agent", effort="low")
    expected = [
        str(h.cli), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--model", "haiku", "--append-system-prompt", "SYSTEM TEXT", "--permission-mode", "bypassPermissions",
        "--disallowedTools", "Agent", "--effort", "low", "--session-id", SESSION_ID,
    ]
    assert ClaudeAgentSession(executable=str(h.cli)).build_argv(request) == expected
    outcome = h.run(request)
    assert h.argv()["argv"] == expected[1:]
    assert "--no-session-persistence" not in expected and "--allowedTools" not in expected
    assert outcome.response.execution_controls_applied == ("permission-bypass", "disallowed-tools")
    assert h.argv()["tag"] == h.tag and h.argv()["claudecode"] is False


def test_a_resumed_session_uses_resume_and_replace_mode_uses_system_prompt(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    request = h.request(resume=True, allowed_tools="Read")
    request = SessionRequest(**{**request.__dict__, "options": BackendOptions(
        timeout_s=30.0, cwd=h.cwd, system_prompt_mode="replace", allowed_tools="Read")})
    argv = ClaudeAgentSession(executable=str(h.cli)).build_argv(request)
    assert argv[-2:] == ["--resume", SESSION_ID] and "--session-id" not in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM TEXT"
    assert argv[argv.index("--allowedTools") + 1] == "Read"


def test_events_file_is_stdout_verbatim_and_the_prompt_is_the_first_stdin_line(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    outcome = h.run(h.request())
    assert h.paths.events.read_bytes() == Path(str(h.log) + ".stdout").read_bytes()
    assert h.stdin_lines()[0] == user_message_line("hello")
    assert outcome.stop_kind is StopKind.QUIESCENT
    assert outcome.response.text == "echo: hello" and outcome.result_subtype == "success"
    assert outcome.failure is None and outcome.shutdown_confirmed
    assert outcome.session_id == SESSION_ID and outcome.cwd == Path(os.path.realpath(h.cwd))
    state = read_state(h.paths)
    assert state.closed and state.close_reason is StopKind.QUIESCENT
    cleanup = read_cleanup_record(h.paths)
    assert cleanup is not None and cleanup.tag == h.tag and cleanup.confirmed_stopped_at is not None
    assert cleanup.leader.pid == h.argv()["pid"] and cleanup.process_group == h.argv()["pgid"]


def test_a_mailbox_message_is_forwarded_and_recorded_forwarded(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    bounds = SessionBounds(quiescence_s=2.0, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0)
    record = {}

    def during() -> None:
        h.wait_for(lambda events: len(_results(events)) == 1)
        record["r"] = append_message(h.paths, "second message")

    outcome = h.run(h.request(bounds=bounds), during)
    assert h.stdin_lines()[1] == user_message_line("second message")
    assert read_state(h.paths).delivery(record["r"].seq) is MessageDelivery.FORWARDED
    assert outcome.response.text == "echo: second message"
    late = append_message(h.paths, "after the end")
    assert read_state(h.paths).delivery(late.seq) is MessageDelivery.UNDELIVERED


# ---------------------------------------------------------------------------
# quiescence and the attempt timeout
# ---------------------------------------------------------------------------


def test_a_background_turn_after_a_result_is_waited_out(tmp_path, monkeypatch):
    """The background turn's init comes 0.6 s after the first result, inside Q = 1.5 s, and its result 2 s
    later: the init voids the first result's timer, so the session runs until Q after the second result."""
    h = Harness(tmp_path, monkeypatch, "background")
    bounds = SessionBounds(quiescence_s=1.5, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0)
    outcome = h.run(h.request(bounds=bounds))
    assert outcome.stop_kind is StopKind.QUIESCENT
    assert outcome.response.text == "background done"
    assert len(_results(h.events())) == 2
    # Counted time runs to the start of shutdown: past the second result plus Q, not Q after the first.
    assert outcome.response.wall_ms >= 3800


def test_the_timeout_with_an_open_quiescence_window_ends_quiescent(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    bounds = SessionBounds(quiescence_s=60.0, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0)
    started = time.monotonic()
    outcome = h.run(h.request(bounds=bounds, timeout_s=2.0))
    assert time.monotonic() - started < 15
    assert outcome.stop_kind is StopKind.QUIESCENT and outcome.failure is None
    assert outcome.response.text == "echo: hello"


def test_the_timeout_on_a_busy_session_interrupts_and_reports_timeout(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "busy")
    outcome = h.run(h.request(timeout_s=1.5))
    assert outcome.stop_kind is StopKind.TIMEOUT
    assert outcome.failure.kind is SessionFailureKind.TIMEOUT
    assert [c["request"]["subtype"] for c in _controls_sent(h)] == ["interrupt"]
    assert outcome.response.wall_ms < 3000  # counted to the start of shutdown, not through it


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_a_busy_stop_sends_the_interrupt_and_takes_the_new_result(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "busy")
    outcome = h.run(h.request(), _stopper(h))
    sent = _controls_sent(h)
    assert len(sent) == 1 and sent[0]["request"] == {"subtype": "interrupt"}
    assert h.stdin_lines()[-1] == interrupt_request_line(sent[0]["request_id"])
    assert outcome.stop_kind is StopKind.OPERATOR_STOP
    assert outcome.result_subtype == "error_during_execution" and outcome.failure is None
    assert outcome.shutdown_confirmed
    assert read_state(h.paths).close_reason is StopKind.OPERATOR_STOP


def test_an_idle_stop_only_closes_stdin(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    bounds = SessionBounds(quiescence_s=60.0, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0)
    outcome = h.run(h.request(bounds=bounds), _stopper(h, after=lambda events: bool(_results(events))))
    assert _controls_sent(h) == []
    assert outcome.stop_kind is StopKind.OPERATOR_STOP and outcome.response.text == "echo: hello"
    assert not Path(str(h.log) + ".sigint").exists()


def test_a_pause_is_an_operator_pause(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "busy")
    outcome = h.run(h.request(), _stopper(h, kind=StopRequestKind.PAUSE))
    assert outcome.stop_kind is StopKind.OPERATOR_PAUSE and outcome.shutdown_confirmed


def test_a_success_result_racing_the_stop_is_accepted(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "racing")
    outcome = h.run(h.request(), _stopper(h))
    assert outcome.stop_kind is StopKind.OPERATOR_STOP
    assert outcome.response.text == "finished anyway" and outcome.result_subtype == "success"


def test_a_finish_turn_stop_sends_no_interrupt(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "slow_turn")
    outcome = h.run(h.request(), _stopper(h, StopMode.FINISH_TURN))
    assert _controls_sent(h) == []
    assert outcome.response.text == "slow done" and outcome.stop_kind is StopKind.OPERATOR_STOP


def test_an_ignored_interrupt_escalates_to_sigint_then_kill(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "stubborn")
    bounds = SessionBounds(quiescence_s=0.5, result_wait_s=0.5, eof_wait_s=0.5, sigint_wait_s=0.5)
    outcome = h.run(h.request(bounds=bounds), _stopper(h))
    assert len(_controls_sent(h)) == 1
    assert Path(str(h.log) + ".sigint").read_text().count("sigint") == 1
    assert outcome.shutdown_confirmed and outcome.response is None
    child = int(Path(str(h.log) + ".child").read_text())
    cleanup = read_cleanup_record(h.paths)
    assert child in {member.pid for member in cleanup.members}
    alive = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()
    assert alive in ("", "Z") or alive.startswith("Z")


def _alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _gone_soon(pid: int, timeout_s: float = 5.0) -> bool:
    end = time.monotonic() + timeout_s
    while _alive(pid):
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)
    return True


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def test_a_member_recorded_before_it_calls_setsid_stays_attached_and_is_killed(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "setsid_late")
    leader_pid = None
    try:
        outcome = h.run(h.request())
        child = int(Path(str(h.log) + ".child").read_text())
        moved_pid, moved_sid = map(int, Path(str(h.log) + ".setsid").read_text().split())
        leader_pid = h.argv()["pid"]
        assert moved_pid == child and moved_sid == child and moved_sid != h.argv()["pgid"]  # it left the session
        cleanup = read_cleanup_record(h.paths)
        assert child in {member.pid for member in cleanup.members}, "recorded while still in the session"
        assert _gone_soon(child), "a recorded member that called setsid() must still be signalled"
        assert outcome.shutdown_confirmed and outcome.workspace_quiescent
        assert outcome.detached_survivors == () and cleanup.detached_survivors == ()
        assert cleanup.confirmed_stopped_at is not None
    finally:
        child_file = Path(str(h.log) + ".child")
        if child_file.exists():
            _kill_quietly(int(child_file.read_text()))
    assert leader_pid is not None


def test_a_process_in_another_group_of_the_original_session_is_attached_and_killed(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "regroup")
    regroup_file = Path(str(h.log) + ".regroup")
    try:
        outcome = h.run(h.request())
        pid, pgid, sid = map(int, regroup_file.read_text().split())
        leader = h.argv()
        assert pgid != leader["pgid"] and sid == leader["pid"]  # another group, the same session
        assert _gone_soon(pid), "a different process group alone is not detachment"
        assert pid in {member.pid for member in read_cleanup_record(h.paths).members}
        assert outcome.shutdown_confirmed and outcome.workspace_quiescent and outcome.detached_survivors == ()
    finally:
        if regroup_file.exists():
            _kill_quietly(int(regroup_file.read_text().split()[0]))


def test_a_detached_tagged_process_is_never_signalled_and_is_recorded_as_a_survivor(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "daemon")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FAKE_DAEMON_CWD", str(elsewhere))
    bounds = SessionBounds(quiescence_s=0.5, result_wait_s=1.0, eof_wait_s=1.0, sigint_wait_s=0.5)
    daemon_file = Path(str(h.log) + ".daemon")
    try:
        outcome = h.run(h.request(bounds=bounds))
        daemon = int(daemon_file.read_text())
        assert _alive(daemon), "a detached process must never be killed"
        assert not Path(str(h.log) + ".daemon_signal").exists(), "a detached process must never be signalled"
        assert outcome.stop_kind is StopKind.QUIESCENT and outcome.failure is None
        assert outcome.response is not None and outcome.response.text == "echo: hello"
        assert outcome.shutdown_confirmed and not outcome.workspace_quiescent
        (survivor,) = outcome.detached_survivors
        assert survivor.identity.pid == daemon
        assert survivor.cwd == os.path.realpath(elsewhere)  # recorded, though it grants nothing
        cleanup = read_cleanup_record(h.paths)
        assert cleanup.detached_survivors == (survivor,)
        assert daemon not in {member.pid for member in cleanup.members}
        assert cleanup.confirmed_stopped_at is not None  # the session shutdown itself was confirmed
    finally:
        if daemon_file.exists():
            _kill_quietly(int(daemon_file.read_text()))


def test_a_detached_process_that_exits_within_the_wait_is_not_a_survivor(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "daemon")
    monkeypatch.setenv("FAKE_DAEMON_S", "1.5")
    bounds = SessionBounds(quiescence_s=0.3, result_wait_s=1.0, eof_wait_s=1.0, sigint_wait_s=6.0)
    daemon_file = Path(str(h.log) + ".daemon")
    try:
        outcome = h.run(h.request(bounds=bounds))
        assert not Path(str(h.log) + ".daemon_signal").exists()
        assert not _alive(int(daemon_file.read_text()))
        assert outcome.shutdown_confirmed and outcome.workspace_quiescent and outcome.detached_survivors == ()
    finally:
        if daemon_file.exists():
            _kill_quietly(int(daemon_file.read_text()))


# ---------------------------------------------------------------------------
# init validation and failures
# ---------------------------------------------------------------------------


def _repo_subdirectory(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    sub = repo / "packages" / "lib"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return sub


def test_a_repository_subdirectory_cwd_is_accepted_when_init_reports_it(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "basic")
    sub = _repo_subdirectory(tmp_path)
    outcome = h.run(h.request(cwd=sub))
    assert h.argv()["cwd"] == os.path.realpath(sub)
    assert outcome.stop_kind is StopKind.QUIESCENT and outcome.cwd == Path(os.path.realpath(sub))


def test_an_init_naming_the_repository_root_instead_of_the_subdirectory_is_refused(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "cwd_parent")
    sub = _repo_subdirectory(tmp_path)
    outcome = h.run(h.request(cwd=sub))
    assert outcome.stop_kind is StopKind.INIT_MISMATCH and outcome.response is None
    assert outcome.failure.kind is SessionFailureKind.INIT_MISMATCH
    assert outcome.failure.observed_cwd == os.path.dirname(os.path.realpath(sub))
    assert outcome.cwd == sub and outcome.session_id == SESSION_ID
    assert outcome.shutdown_confirmed


def test_an_init_without_a_cwd_is_refused(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, "cwd_missing")
    outcome = h.run(h.request())
    assert outcome.stop_kind is StopKind.INIT_MISMATCH
    assert outcome.failure.kind is SessionFailureKind.INIT_MISMATCH and outcome.failure.observed_cwd is None


def test_an_auth_failure_and_a_nonzero_exit_are_distinct_failures(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    auth = Harness(tmp_path / "a", monkeypatch, "auth")
    outcome = auth.run(auth.request())
    assert outcome.failure.kind is SessionFailureKind.RESULT_ERROR and outcome.failure.halt_kind == "auth"
    assert outcome.response.status == ERROR and outcome.response.error.code == "auth"

    (tmp_path / "b").mkdir()
    exit3 = Harness(tmp_path / "b", monkeypatch, "exit3")
    outcome = exit3.run(exit3.request())
    assert outcome.stop_kind is StopKind.PROCESS_EXIT
    assert outcome.failure.kind is SessionFailureKind.NONZERO_EXIT and outcome.failure.exit_code == 3
    assert outcome.failure.halt_kind is None and outcome.response.text == "echo: hello"

    (tmp_path / "c").mkdir()
    noinit = Harness(tmp_path / "c", monkeypatch, "noinit")
    outcome = noinit.run(noinit.request())
    assert outcome.failure.kind is SessionFailureKind.NO_INIT and outcome.failure.exit_code == 2
    assert "cannot start" in outcome.failure.message and outcome.response is None


# ---------------------------------------------------------------------------
# the output contract
# ---------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"],
           "additionalProperties": False}


def test_structured_output_is_extracted_and_the_contract_is_judged(tmp_path, monkeypatch):
    contract = OutputContract(id="demo.contract", policy=POLICY_VALIDATED_RESULT, schema=_SCHEMA)
    h = Harness(tmp_path, monkeypatch, "structured")
    outcome = h.run(h.request(output_contract=contract))
    assert "--json-schema" in h.argv()["argv"]
    assert outcome.response.text == '{"a":1}' and outcome.response.structured == {"a": 1}

    (tmp_path / "bad").mkdir()
    bad = Harness(tmp_path / "bad", monkeypatch, "bad_struct")
    outcome = bad.run(bad.request(output_contract=contract))
    assert outcome.response.status == ERROR and outcome.response.structured is None
    assert outcome.failure is None  # judged in the response, as a completion's violation is


def test_line_helpers_shape_and_refuse():
    assert json.loads(user_message_line("hi")) == {
        "type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    assert json.loads(interrupt_request_line("r1")) == {
        "type": "control_request", "request_id": "r1", "request": {"subtype": "interrupt"}}
    with pytest.raises(TypeError):
        user_message_line(3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        interrupt_request_line("")
