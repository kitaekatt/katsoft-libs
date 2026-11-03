"""Tests for llm_kit.relay.discovery and llm_kit.relay.transcript (unit D2).

Every test builds its own fixture tree under ``tmp_path`` and injects it as the
discovery roots: no test reads or writes the real harness homes. Liveness uses
real processes through psutil (this test process, a sleeping child, a reaped
child), because liveness is the property under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

import psutil
import pytest

from llm_kit.relay import discovery as D
from llm_kit.relay import protocol as P
from llm_kit.relay import transcript as T

MACHINE = "m5pro"
NOW = 1_800_000_000.0
NONCE = "n" * 24
SSH_SECRET = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
API_KEY = "sk-" + "ant-api03-" + "A1b2C3d4E5f6G7h8I9j0"


def sid() -> str:
    return str(uuid.uuid4())


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def roots(tmp_path: Path) -> D.DiscoveryRoots:
    claude, codex = tmp_path / "claude-home", tmp_path / "codex-home"
    (claude / "sessions").mkdir(parents=True)
    (claude / "projects" / "-tmp-proj").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)
    return D.DiscoveryRoots(claude, codex)


@pytest.fixture
def own_start() -> float:
    return psutil.Process(os.getpid()).create_time()


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_session_file(roots: D.DiscoveryRoots, pid: int, session_id: str, started_at_s: Optional[float], **extra: Any) -> Path:
    data: dict[str, Any] = {"pid": pid, "sessionId": session_id, "cwd": "/Users/u/Dev/app", "version": "2.1.316",
                            "status": "idle", "messagingSocketPath": "/tmp/x.sock", "futureField": {"a": 1}}
    if started_at_s is not None:
        data["startedAt"] = int(started_at_s * 1000)
    data.update(extra)
    path = roots.claude_sessions / f"{pid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_transcript(roots: D.DiscoveryRoots, session_id: str, lines: list[dict[str, Any]], mtime: float = NOW - 60) -> Path:
    path = roots.claude_projects / "-tmp-proj" / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def user(text: str, at: str = "2026-11-03T10:00:00.000Z") -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": at, "cwd": "/Users/u/Dev/app"}


def assistant(blocks: list[dict[str, Any]], at: str = "2026-11-03T10:00:01.000Z") -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}, "timestamp": at}


def tool_result(tool_use_id: str, content: Any, at: str = "2026-11-03T10:00:02.000Z") -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}, "timestamp": at}


def make(roots: D.DiscoveryRoots, **kw: Any) -> D.Discovery:
    clock = kw.pop("clock", Clock())
    table = kw.pop("table", None)
    return D.Discovery(MACHINE, roots, table=table, probe=D.PsutilProbe(), clock=clock, **kw)


def by_id(result: D.ScanResult) -> dict[str, D.SessionInfo]:
    return {s.session_id: s for s in result.sessions}


# --------------------------------------------------------------------------
# scanned Claude sessions: liveness and states
# --------------------------------------------------------------------------

def test_scanned_claude_states_and_read_only(roots, own_start, dead_pid) -> None:
    live, dead, mismatch, old = sid(), sid(), sid(), sid()
    write_session_file(roots, os.getpid(), live, own_start)
    write_session_file(roots, dead_pid, dead, own_start)
    # another live pid number is needed for the mismatch case: reuse a sleeper-free trick -- the
    # parent's pid with a start an hour off
    write_session_file(roots, os.getppid(), mismatch, own_start - 3600)
    write_transcript(roots, live, [user("hi")])
    write_transcript(roots, dead, [user("hi")], mtime=NOW - 120)
    write_transcript(roots, mismatch, [user("hi")], mtime=NOW - 120)
    write_transcript(roots, old, [user("hi")], mtime=NOW - 30 * 86400)

    result = make(roots).scan()
    sessions = by_id(result)
    assert sessions[live].state == "live-read-only"
    assert sessions[dead].state == "idle"
    assert sessions[mismatch].state == "idle"          # pid alive, start time inconsistent: not live
    assert sessions[old].state == "ended"              # transcript only, past the idle window
    assert all(s.registered is False and s.accepts == () for s in result.sessions)
    assert sessions[live].pid == os.getpid() and sessions[live].version == "2.1.316"
    assert sessions[live].cwd == "/Users/u/Dev/app"
    wire = sessions[live].to_session(MACHINE)
    assert wire["address"] == f"{MACHINE}/claude/{live}" and wire["registered"] is False and wire["accepts"] == []


def test_start_time_mismatch_is_not_live(roots, own_start) -> None:
    session = sid()
    write_session_file(roots, os.getpid(), session, own_start - 7200)
    write_transcript(roots, session, [user("hi")])
    assert by_id(make(roots).scan())[session].state == "idle"
    write_session_file(roots, os.getpid(), session, own_start)
    assert by_id(make(roots).scan())[session].state == "live-read-only"


def test_missing_started_at_cannot_be_proved_live(roots) -> None:
    session = sid()
    write_session_file(roots, os.getpid(), session, None)
    assert by_id(make(roots).scan())[session].state != "live-read-only"


def test_zombie_pid_is_not_live(roots, own_start) -> None:
    # a reaped child is gone; a pid that never existed behaves the same
    session = sid()
    write_session_file(roots, 2**22 - 7, session, own_start)
    assert by_id(make(roots).scan())[session].state in ("idle", "ended")


def test_malformed_session_files_are_skipped_not_fatal(roots, own_start) -> None:
    good = sid()
    write_session_file(roots, os.getpid(), good, own_start)
    (roots.claude_sessions / "111.json").write_text("{not json", encoding="utf-8")
    (roots.claude_sessions / "112.json").write_text("[1, 2]", encoding="utf-8")
    (roots.claude_sessions / "113.json").write_text(json.dumps({"pid": 113, "sessionId": "../evil"}), encoding="utf-8")
    (roots.claude_sessions / "114.json").write_text(json.dumps({"pid": "x", "sessionId": sid()}), encoding="utf-8")
    (roots.claude_sessions / "115.json").write_text(json.dumps({"pid": 999, "sessionId": sid()}), encoding="utf-8")
    key = roots.claude_sessions / "116.0abc.key"
    key.write_text("SECRET-MATERIAL", encoding="utf-8")

    result = make(roots).scan()
    assert good in by_id(result)
    skipped = {Path(s.path).name: s.reason for s in result.skipped}
    assert set(skipped) == {"111.json", "112.json", "113.json", "114.json", "115.json"}
    assert all(reason for reason in skipped.values())
    assert "116.0abc.key" not in skipped              # key files are never opened
    assert all(s.session_id != "../evil" for s in result.sessions)


def test_transcript_only_sessions_are_idle_with_cwd_and_title(roots) -> None:
    session = sid()
    write_transcript(roots, session, [
        {"type": "custom-title", "customTitle": "my work", "sessionId": session},
        user("hello"),
    ], mtime=NOW - 600)
    info = by_id(make(roots).scan())[session]
    assert info.state == "idle" and info.title == "my work" and info.cwd == "/Users/u/Dev/app"
    assert info.pid is None and info.last_activity_at == NOW - 600


def test_max_transcripts_bounds_the_scan(roots) -> None:
    ids = [sid() for _ in range(6)]
    for index, session in enumerate(ids):
        write_transcript(roots, session, [user("x")], mtime=NOW - 100 - index)
    result = make(roots, max_transcripts=3).scan()
    assert [s.session_id for s in result.sessions] == ids[:3]


# --------------------------------------------------------------------------
# registrations: the table and the merged states
# --------------------------------------------------------------------------

def make_table(clock: Clock, probe: Optional[D.ProcessProbe] = None, **kw: Any) -> D.RegistrationTable:
    return D.RegistrationTable(MACHINE, probe=probe or D.PsutilProbe(), clock=clock, **kw)


def register(table: D.RegistrationTable, session: str, pid: int, accepts=("note", "instruction"), **kw: Any) -> D.RegisterOutcome:
    return table.register(session_id=session, pid=pid, cwd="/Users/u/Dev/app", version="2.1.316",
                          accepts=list(accepts), channel_nonce=NONCE, **kw)


def test_all_four_states_with_registrations(roots, sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    accepting, readonly, gone, ended = sid(), sid(), sid(), sid()
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        register(table, accepting, os.getpid(), task="plugins-kit/harness-messaging")
        register(table, readonly, sleeper.pid, accepts=())
        register(table, gone, victim.pid)
        write_transcript(roots, gone, [user("work")], mtime=NOW - 30)
        write_transcript(roots, ended, [user("old")], mtime=NOW - 40 * 86400)
    finally:
        victim.kill()
        victim.wait()                                  # the process is gone; its lease is still valid

    sessions = by_id(make(roots, table=table, clock=clock).scan())
    assert sessions[accepting].state == "live-accepting" and sessions[accepting].registered is True
    assert sessions[accepting].accepts == ("note", "instruction")
    assert sessions[accepting].task == "plugins-kit/harness-messaging"
    assert sessions[readonly].state == "live-read-only" and sessions[readonly].registered is True
    assert sessions[readonly].accepts == ()
    assert sessions[gone].state == "idle" and sessions[gone].registered is True
    assert sessions[ended].state == "ended"
    assert {s.state for s in sessions.values()} == set(P.SESSION_STATES)


def test_registration_overrides_scan_for_the_same_session(roots, own_start) -> None:
    clock = Clock()
    table = make_table(clock)
    session = sid()
    write_session_file(roots, os.getpid(), session, own_start)
    write_transcript(roots, session, [user("hi")])
    register(table, session, os.getpid())
    only = [s for s in make(roots, table=table, clock=clock).scan().sessions if s.session_id == session]
    assert len(only) == 1 and only[0].registered and only[0].state == "live-accepting"
    assert only[0].transcript is not None


def test_registration_with_inconsistent_start_time_is_not_live(roots, own_start) -> None:
    class Shifting:
        def __init__(self) -> None:
            self.offset = 0.0

        def start_time(self, pid: int) -> Optional[float]:
            real = psutil.Process(pid).create_time()
            return real + self.offset

    probe = Shifting()
    clock = Clock()
    table = D.RegistrationTable(MACHINE, probe=probe, clock=clock)
    session = sid()
    register(table, session, os.getpid())
    discovery = D.Discovery(MACHINE, roots, table=table, probe=probe, clock=clock)
    assert by_id(discovery.scan())[session].state == "live-accepting"
    probe.offset = 3600.0                      # the pid now belongs to a process that started later
    assert by_id(discovery.scan())[session].state != "live-accepting"


def test_register_validates_and_handles_conflicts(sleeper, dead_pid) -> None:
    clock = Clock()
    table = make_table(clock)
    session = sid()
    first = register(table, session, os.getpid())
    assert first.replaced is False and P.is_valid_id("registration", first.registration.registration_id)
    assert first.registration.lease_expires_at == NOW + P.REGISTRATION_LEASE_S

    again = register(table, session, os.getpid())                  # same pid: replaces
    assert again.replaced is True
    assert again.registration.registration_id != first.registration.registration_id
    with pytest.raises(P.RelayError) as info:                       # another live pid, unexpired lease
        register(table, session, sleeper.pid)
    assert info.value.code == "registration_conflict" and info.value.retryable is True

    clock.now += P.REGISTRATION_LEASE_S + 1                         # lease lapsed: the new pid may take it
    taken = register(table, session, sleeper.pid)
    assert taken.replaced is False

    for bad in (dict(pid=dead_pid), dict(pid=0), dict(session="../x"), dict(accepts=("nope",))):
        kwargs = dict(session=sid(), pid=os.getpid(), accepts=("note",)) | bad
        with pytest.raises(P.RelayError) as err:
            register(table, kwargs["session"], kwargs["pid"], kwargs["accepts"])
        assert err.value.code == "bad_request"
    with pytest.raises(P.RelayError) as err:
        table.register(session_id=sid(), pid=os.getpid(), cwd="/x", version="1", accepts=[], channel_nonce="short")
    assert err.value.code == "bad_request"


def test_conflict_is_not_raised_when_the_holder_died(sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    session = sid()
    try:
        register(table, session, victim.pid)
    finally:
        victim.kill()
        victim.wait()
    outcome = register(table, session, sleeper.pid)               # lease unexpired, but the holder is gone
    assert outcome.replaced is True


def test_heartbeat_renews_lease_and_checks_identity() -> None:
    clock = Clock()
    table = make_table(clock)
    session = sid()
    reg = register(table, session, os.getpid()).registration
    clock.now += 45
    beat = table.heartbeat(reg.registration_id, session, NONCE, title="a title", accepts=["note"])
    assert beat.session_changed is False and beat.registration.lease_expires_at == NOW + 45 + 90
    assert beat.registration.title == "a title" and beat.registration.accepts == ("note",)
    with pytest.raises(P.RelayError) as err:
        table.heartbeat(reg.registration_id, session, "x" * 24)
    assert err.value.code == "nonce_mismatch"
    with pytest.raises(P.RelayError) as err:
        table.heartbeat("reg_nope", session, NONCE)
    assert err.value.code == "registration_unknown"
    with pytest.raises(P.RelayError) as err:
        table.heartbeat(reg.registration_id, sid(), NONCE)         # wrong session id, no rename
    assert err.value.code == "registration_unknown"
    clock.now += 91                                                # lease lapses
    with pytest.raises(P.RelayError) as err:
        table.heartbeat(reg.registration_id, session, NONCE)
    assert err.value.code == "registration_unknown"


def test_lease_lapse_emits_an_end_event_and_deregister_ends_cleanly() -> None:
    clock = Clock()
    table = make_table(clock)
    events: list[D.EndEvent] = []
    table.add_end_listener(events.append)
    lapsing, leaving = sid(), sid()
    register(table, lapsing, os.getpid())
    reg = register(table, leaving, os.getppid() if psutil.pid_exists(os.getppid()) else os.getpid()).registration
    clock.now += 100
    table.expire_lapsed()
    assert sorted(e.reason for e in events) == ["lease_lapsed", "lease_lapsed"]
    assert {e.address for e in events} == {f"{MACHINE}/claude/{lapsing}", f"{MACHINE}/claude/{leaving}"}
    assert table.get(lapsing) is None and table.ended_reason(lapsing) == "lease_lapsed"

    clock.now += 1
    again = register(table, sid(), os.getpid()).registration
    event = table.deregister(again.registration_id, again.session_id, NONCE)
    assert event.reason == "deregistered" and events[-1] == event
    with pytest.raises(P.RelayError):
        table.touch(again.registration_id, again.session_id, NONCE)
    del reg


def test_session_change_after_clear_uses_previous_session_id(roots) -> None:
    clock = Clock()
    table = make_table(clock)
    events: list[D.EndEvent] = []
    table.add_end_listener(events.append)
    old, new = sid(), sid()
    reg = register(table, old, os.getpid()).registration
    write_transcript(roots, old, [user("before clear")], mtime=NOW - 10)
    write_transcript(roots, new, [user("after clear")], mtime=NOW - 5)

    beat = table.heartbeat(reg.registration_id, new, NONCE, previous_session_id=old)
    assert beat.session_changed is True and beat.old_address == f"{MACHINE}/claude/{old}"
    assert beat.registration.registration_id == reg.registration_id       # keeps its id, takes the new address
    assert [(e.address, e.reason) for e in events] == [(f"{MACHINE}/claude/{old}", "session_changed")]
    assert table.get(old) is None and table.get(new) is not None
    assert table.ended_reason(old) == "session_changed"

    repeat = table.heartbeat(reg.registration_id, new, NONCE, previous_session_id=old)   # a retried heartbeat
    assert repeat.session_changed is False and len(events) == 1

    sessions = by_id(make(roots, table=table, clock=clock).scan())
    assert sessions[new].state == "live-accepting" and sessions[new].registered
    assert sessions[old].state == "ended"                                 # never redirected to the new conversation

    other = register(table, sid(), os.getppid()).registration
    with pytest.raises(P.RelayError) as err:                              # renaming onto a held session id
        table.heartbeat(other.registration_id, new, NONCE, previous_session_id=other.session_id)
    assert err.value.code == "registration_conflict"


def test_register_for_a_process_that_moved_on_ends_its_old_registration() -> None:
    clock = Clock()
    table = make_table(clock)
    events: list[D.EndEvent] = []
    table.add_end_listener(events.append)
    old, new = sid(), sid()
    register(table, old, os.getpid())
    register(table, new, os.getpid())
    assert [e.reason for e in events] == ["session_changed"] and table.get(new) is not None and table.get(old) is None


# --------------------------------------------------------------------------
# Codex rollouts
# --------------------------------------------------------------------------

def write_rollout(roots: D.DiscoveryRoots, session_id: str, *, day: str = "2026/11/03", mtime: float = NOW - 60,
                  meta_id: Optional[str] = None, lines: Optional[list[dict[str, Any]]] = None) -> Path:
    directory = roots.codex_sessions / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-11-03T09-32-56-{session_id}.jsonl"
    head = {"timestamp": "2026-11-03T15:32:56.704Z", "type": "session_meta",
            "payload": {"id": meta_id or session_id, "cwd": "/Users/u/Dev/libs", "cli_version": "0.164.0"}}
    path.write_text("".join(json.dumps(x) + "\n" for x in [head, *(lines or [])]), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_codex_rollouts_are_idle_or_ended_by_mtime_never_live(roots, own_start) -> None:
    fresh, stale, wrong = sid(), sid(), sid()
    write_rollout(roots, fresh)
    write_rollout(roots, stale, day="2026/09/01", mtime=NOW - 60 * 86400)
    write_rollout(roots, wrong, meta_id=sid())
    (roots.codex_sessions / "2026" / "11" / "03" / "rollout-no-id-here.jsonl").write_text("{}\n", encoding="utf-8")
    # even a pid file naming this live process cannot make a codex session live
    write_session_file(roots, os.getpid(), sid(), own_start)

    result = make(roots).scan()
    sessions = by_id(result)
    assert sessions[fresh].harness == "codex" and sessions[fresh].state == "idle"
    assert sessions[fresh].cwd == "/Users/u/Dev/libs" and sessions[fresh].version == "0.164.0"
    assert sessions[stale].state == "ended"
    assert wrong not in sessions
    assert sessions[fresh].registered is False and sessions[fresh].accepts == ()
    assert {Path(s.path).name for s in result.skipped} == {
        f"rollout-2026-11-03T09-32-56-{wrong}.jsonl", "rollout-no-id-here.jsonl"}
    wire = sessions[fresh].to_session(MACHINE)
    assert wire["address"] == f"{MACHINE}/codex/{fresh}" and wire["state"] == "idle"


def test_list_sessions_filters(roots, own_start) -> None:
    a, c = sid(), sid()
    write_session_file(roots, os.getpid(), a, own_start)
    write_transcript(roots, a, [user("x")])
    write_rollout(roots, c)
    discovery = make(roots)
    assert {s.session_id for s in discovery.list_sessions(harness="codex").sessions} == {c}
    assert {s.session_id for s in discovery.list_sessions(state="live-read-only").sessions} == {a}
    with pytest.raises(P.RelayError) as err:
        discovery.list_sessions(state="busy")
    assert err.value.code == "bad_request"


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------

def test_resolve_addresses_and_selectors(roots, own_start, sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    one, two, idle = sid(), sid(), sid()
    register(table, one, os.getpid(), task="plugins-kit/harness-messaging")
    register(table, two, sleeper.pid)
    write_transcript(roots, one, [user("a")], mtime=NOW - 5)
    write_transcript(roots, two, [user("b")], mtime=NOW - 50)
    write_transcript(roots, idle, [user("c")], mtime=NOW - 500)
    discovery = make(roots, table=table, clock=clock)

    assert discovery.resolve(f"{MACHINE}/claude/{idle}").state == "idle"            # idle: by address only
    assert discovery.resolve("latest").session_id == one
    assert discovery.resolve(f"{MACHINE}/claude/latest").session_id == one
    assert discovery.resolve("task:plugins-kit/harness-messaging").session_id == one
    with pytest.raises(P.RelayError) as err:
        discovery.resolve("cwd:app")                                               # both live sessions share the cwd
    assert err.value.code == "target_ambiguous" and len(err.value.details["candidates"]) == 2
    with pytest.raises(P.RelayError) as err:
        discovery.resolve("cwd:/Users/u/Dev/other")
    assert err.value.code == "target_not_found"
    with pytest.raises(P.RelayError) as err:
        discovery.resolve(f"other/claude/{one}")
    assert err.value.code == "target_not_found"
    with pytest.raises(P.RelayError) as err:
        discovery.resolve(f"{MACHINE}/claude/{sid()}")
    assert err.value.code == "target_not_found"
    with pytest.raises(P.RelayError) as err:
        discovery.resolve("not a target")
    assert err.value.code == "target_invalid"
    with pytest.raises(P.RelayError) as err:
        discovery.resolve(f"{MACHINE}/llm/qwen38-m5-32k-local")
    assert err.value.code == "target_invalid"


# --------------------------------------------------------------------------
# read_session
# --------------------------------------------------------------------------

def secret_transcript(roots: D.DiscoveryRoots, session: str) -> Path:
    return write_transcript(roots, session, [
        user("please look at my keys"),
        assistant([{"type": "text", "text": f"I will read it. The old key was {API_KEY} (rotated)."},
                   {"type": "thinking", "thinking": "hidden"}]),
        assistant([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat ~/.ssh/id_rsa"}}]),
        tool_result("t1", SSH_SECRET),
        assistant([{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls src"}}]),
        tool_result("t2", "main.py\nutil.py"),
        assistant([{"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/work/.env"}}]),
        tool_result("t3", [{"type": "text", "text": "DB_PASSWORD=hunter2hunter2"}]),
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}},
        {"type": "system", "subtype": "x"},
        user("thanks"),
    ])


def test_read_session_redacts_and_withholds_secret_path_outputs(roots) -> None:
    session = sid()
    secret_transcript(roots, session)
    out = make(roots).read_session(f"{MACHINE}/claude/{session}")
    text = json.dumps(out)
    assert "b3BlbnNzaC1rZXk" not in text and "hunter2" not in text and API_KEY not in text
    assert "hidden" not in text and "system-reminder" not in text
    roles = [t["role"] for t in out["turns"]]
    assert roles[0] == "user" and roles[-1] == "user" and out["turns"][-1]["text"] == "thanks"
    results = [t["text"] for t in out["turns"] if "result]" in t["text"]]
    assert any("withheld" in r for r in results)
    assert any("main.py" in r for r in results)                    # ordinary output is returned
    assert out["redactions"] == 3                                  # api key + two withheld outputs
    assert out["address"] == f"{MACHINE}/claude/{session}" and out["truncated"] is False
    assert out["cursor"].startswith("c:")


def test_tool_result_without_its_call_in_the_window_is_withheld(roots) -> None:
    session = sid()
    write_transcript(roots, session, [tool_result("never-seen", "SHOULD-NOT-APPEAR"), user("later")])
    out = make(roots).read_session(f"{MACHINE}/claude/{session}")
    assert "SHOULD-NOT-APPEAR" not in json.dumps(out) and out["redactions"] == 1


def test_read_session_caps_and_cursor_walk(roots) -> None:
    session = sid()
    write_transcript(roots, session, [user(f"message {i}") for i in range(30)])
    discovery = make(roots)
    address = f"{MACHINE}/claude/{session}"

    tail = discovery.read_session(address, limit=5)
    assert [t["text"] for t in tail["turns"]] == [f"message {i}" for i in range(25, 30)]
    assert tail["truncated"] is True
    assert discovery.read_session(address)["turns"][0]["text"] == "message 10"      # default 20 turns

    with open(roots.claude_projects / "-tmp-proj" / f"{session}.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(user("message 30")) + "\n")
    more = discovery.read_session(address, since=tail["cursor"], limit=10)
    assert [t["text"] for t in more["turns"]] == ["message 30"] and more["truncated"] is False

    walked: list[str] = []
    cursor = "c:0"
    for _ in range(10):
        page = discovery.read_session(address, since=cursor, limit=7)
        walked += [t["text"] for t in page["turns"]]
        cursor = page["cursor"]
        if not page["truncated"]:
            break
    assert walked == [f"message {i}" for i in range(31)]                            # no gap, no repeat


def test_read_session_byte_and_turn_caps(roots) -> None:
    session = sid()
    write_transcript(roots, session, [user("x" * 3000 + f" {i}") for i in range(10)])
    discovery = make(roots, read_limits=T.ReadLimits(max_bytes=7000, max_turn_chars=1000))
    out = discovery.read_session(f"{MACHINE}/claude/{session}", limit=200)
    assert len(out["turns"]) == 7 and out["truncated"] is True                      # 7000 // 1000
    assert all(t.get("truncated") is True and len(t["text"]) <= 1000 for t in out["turns"])
    assert out["turns"][-1]["text"].startswith("x") and sum(len(t["text"].encode()) for t in out["turns"]) <= 7000


def test_scan_cap_bounds_how_much_of_a_big_transcript_is_read(roots) -> None:
    session = sid()
    write_transcript(roots, session, [user("y" * 200 + f" {i}") for i in range(200)])
    discovery = make(roots, read_limits=T.ReadLimits(max_scan_bytes=4096))
    out = discovery.read_session(f"{MACHINE}/claude/{session}", limit=200)
    assert 0 < len(out["turns"]) < 200 and out["truncated"] is True
    assert out["turns"][-1]["text"].endswith(" 199")


def test_bad_cursors_are_refused(roots) -> None:
    session = sid()
    path = write_transcript(roots, session, [user("a"), user("b")])
    discovery = make(roots)
    address = f"{MACHINE}/claude/{session}"
    for bad in ("12", "c:abc", "c:-1", f"c:{path.stat().st_size + 5}", "c:3"):
        with pytest.raises(P.RelayError) as err:
            discovery.read_session(address, since=bad)
        assert err.value.code == "bad_request", bad
    with pytest.raises(P.RelayError) as err:
        discovery.read_session(address, limit=0)
    assert err.value.code == "bad_request"
    with pytest.raises(P.RelayError) as err:
        discovery.read_session(address, limit=P.MAX_READ_TURNS + 1)
    assert err.value.code == "bad_request"


def test_oversize_line_is_skipped_with_a_note_on_a_forward_read(roots) -> None:
    session = sid()
    path = write_transcript(roots, session, [user("a")])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(user("z" * 20000)) + "\n" + json.dumps(user("after")) + "\n")
    discovery = make(roots, read_limits=T.ReadLimits(max_scan_bytes=4096))
    texts: list[str] = []
    cursor = "c:0"
    for _ in range(5):
        page = discovery.read_session(f"{MACHINE}/claude/{session}", since=cursor)
        texts += [t["text"] for t in page["turns"]]
        cursor = page["cursor"]
        if not page["truncated"]:
            break
    assert "a" in texts and "after" in texts and any("skipped" in t for t in texts)


def test_malformed_transcript_lines_are_counted(roots) -> None:
    session = sid()
    path = write_transcript(roots, session, [user("a")])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{broken\n")
        handle.write(json.dumps(user("b")) + "\n")
    out = make(roots).read_session(f"{MACHINE}/claude/{session}")
    assert [t["text"] for t in out["turns"]] == ["a", "b"] and out["skipped_lines"] == 1


def test_session_without_a_transcript_is_not_found(roots, own_start) -> None:
    session = sid()
    write_session_file(roots, os.getpid(), session, own_start)
    with pytest.raises(P.RelayError) as err:
        make(roots).read_session(f"{MACHINE}/claude/{session}")
    assert err.value.code == "target_not_found"


def test_read_codex_rollout(roots) -> None:
    session = sid()

    def item(payload: dict[str, Any], at: str = "2026-11-03T15:33:00.000Z") -> dict[str, Any]:
        return {"timestamp": at, "type": "response_item", "payload": payload}

    write_rollout(roots, session, lines=[
        item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "system rules"}]}),
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /x"}]}),
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "review this"}]}),
        item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"token {API_KEY}"}]}),
        item({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "cat ~/.ssh/id_ed25519"}),
        item({"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": SSH_SECRET}]}),
        item({"type": "custom_tool_call", "call_id": "c2", "name": "exec", "input": "ls"}),
        item({"type": "custom_tool_call_output", "call_id": "c2", "output": "README.md"}),
        item({"type": "reasoning", "summary": []}),
        {"timestamp": "2026-11-03T15:33:05.000Z", "type": "event_msg", "payload": {"type": "token_count"}},
    ])
    out = make(roots).read_session(f"{MACHINE}/codex/{session}")
    text = json.dumps(out)
    assert "system rules" not in text and "AGENTS.md" not in text
    assert "b3BlbnNzaC1rZXk" not in text and API_KEY not in text
    assert [t["role"] for t in out["turns"]] == ["user", "assistant", "tool", "tool", "tool", "tool"]
    assert "README.md" in text and out["redactions"] == 2
    assert out["turns"][0]["at"] == pytest.approx(1793719980.0, abs=1.0)


def test_results_validate_against_the_frozen_schemas(roots, own_start) -> None:
    from llm_kit.schema import json_schema as JS

    session = sid()
    secret_transcript(roots, session)
    write_session_file(roots, os.getpid(), session, own_start)
    write_rollout(roots, sid())
    discovery = make(roots)
    out = discovery.read_session(f"{MACHINE}/claude/{session}", limit=4)
    assert JS.validate(P.load_schema(P.response_schema_name("read_session")), out) == ()
    listing = {"sessions": [s.to_session(MACHINE) for s in discovery.list_sessions().sessions],
               "machines": [{"machine": MACHINE, "status": "up"}]}
    assert len(listing["sessions"]) == 2
    assert JS.validate(P.load_schema(P.response_schema_name("list_sessions")), listing) == ()
