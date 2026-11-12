"""Codex registrations and writer-lock liveness in relay discovery and service (unit D3).

Evidence (plan-codex.md, E-K5 and E-K6; measured on codex-cli 0.164.0): a thread id is its rollout uuid
and its lock file name; ``<codex_home>/thread-writer-locks/<id>.lock`` is created and held open by the
owning process from ``thread/start``, before any rollout exists; a clean exit removes it, SIGKILL leaves
the file behind unheld; ``.coordination.lock`` is permanent and not a thread. ``~/.codex`` may be a
symlink, so the open file is reported through its target.

Every fixture is a tmp tree mimicking that layout; no test reads or writes the real ``~/.codex``. Liveness
uses real processes through psutil, because it is the property under test. The relay-level tests reuse the
fixtures of ``test_relay_server.py`` and drive a real relay over HTTP.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import psutil
import pytest

from llm_kit.relay import discovery as D
from llm_kit.relay import protocol as P

from test_relay_discovery import (  # noqa: F401 -- fixtures and helpers
    Clock, MACHINE, NOW, NONCE, dead_pid, make, make_table, own_start, register, roots, sid, sleeper, write_rollout,
)
from test_relay_server import Relay, addr, backend, config, make_server  # noqa: F401 -- fixtures

HOLDER = (
    "import sys, time\n"
    "handle = open(sys.argv[1], 'a')\n"
    "print('held', flush=True)\n"
    "time.sleep(120)\n"
)


def write_lock(roots: D.DiscoveryRoots, thread_id: str, mtime: float = NOW - 30) -> Path:
    roots.codex_locks.mkdir(parents=True, exist_ok=True)
    path = roots.codex_locks / f"{thread_id}.lock"
    path.write_text("", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def held_locks() -> Iterator[list]:
    """Open handles in THIS process (which psutil reports like any other holder); closed at teardown."""
    handles: list = []
    try:
        yield handles
    finally:
        for handle in handles:
            handle.close()


def hold(held_locks: list, path: Path) -> None:
    held_locks.append(open(path, "a"))


def thread_wire(relay: Relay, thread_id: str) -> Optional[Mapping[str, Any]]:
    for session in relay.hub("list_sessions", {"harness": "codex"})["sessions"]:
        if session["session_id"] == thread_id:
            return session
    return None


def codex_register_params(session: str, **extra: Any) -> dict:
    params = {"session_id": session, "harness": "codex", "pid": os.getpid(), "cwd": "/Users/u/Dev/app",
              "version": "codex-cli 0.164.0", "accepts": ["note", "instruction", "question"],
              "channel_nonce": NONCE}
    params.update(extra)
    return params


def codex_channel(relay: Relay, session: str, verb: str, params: dict) -> dict:
    return relay.client.call(verb, params, origin=f"channel:{MACHINE}/codex/{session}")


def codex_creds(registration: dict, session: str) -> dict:
    return {"registration_id": registration["registration_id"], "session_id": session, "channel_nonce": NONCE}


# --------------------------------------------------------------------------
# the lock probe
# --------------------------------------------------------------------------

def test_the_probe_reports_a_lock_held_open_through_a_symlinked_codex_home_and_not_an_unheld_file(
        tmp_path, held_locks) -> None:
    real_home = tmp_path / "real-codex"
    (real_home / "thread-writer-locks").mkdir(parents=True)
    link = tmp_path / "codex-link"
    link.symlink_to(real_home, target_is_directory=True)
    held_id, stale_id = sid(), sid()
    held_real = real_home / "thread-writer-locks" / f"{held_id}.lock"
    held_real.write_text("")
    hold(held_locks, held_real)  # the open file is reported through the TARGET path
    (real_home / "thread-writer-locks" / f"{stale_id}.lock").write_text("")  # a file nobody holds
    via_link = link / "thread-writer-locks"
    locks = {held_id: via_link / f"{held_id}.lock", stale_id: via_link / f"{stale_id}.lock"}  # asked through the symlink

    assert D.PsutilCodexLockProbe().held(locks) == frozenset({held_id})


def test_a_lock_held_by_another_process_is_live_until_that_process_is_killed_and_the_leftover_file_is_not(
        roots, tmp_path) -> None:
    """The SIGKILL shape of E-K6: the file stays behind, unheld, and must not read as live."""
    thread_id = sid()
    lock = write_lock(roots, thread_id)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        discovery = make(roots)
        assert discovery.scan().sessions[0].session_id == thread_id
        assert discovery.scan().sessions[0].state == "live-read-only"
        holder.kill()
        holder.wait()
        assert lock.exists()  # SIGKILL leaves the file behind
        assert make(roots).scan().sessions == ()  # unheld: not live, and with no rollout not listed at all
    finally:
        holder.kill()
        holder.wait()


# --------------------------------------------------------------------------
# scanned threads
# --------------------------------------------------------------------------

def test_a_held_lock_with_a_rollout_is_live_read_only_and_an_unheld_one_is_not(roots, held_locks) -> None:
    live, stale, no_lock = sid(), sid(), sid()
    write_rollout(roots, live)
    write_rollout(roots, stale)
    write_rollout(roots, no_lock)
    hold(held_locks, write_lock(roots, live))
    write_lock(roots, stale)  # the file exists, nobody holds it
    (roots.codex_locks / ".coordination.lock").write_text("")
    hold(held_locks, roots.codex_locks / ".coordination.lock")  # held, and still not a thread

    sessions = {s.session_id: s for s in make(roots).scan().sessions}
    assert sessions[live].state == "live-read-only" and sessions[live].registered is False
    assert sessions[live].accepts == () and sessions[live].transcript is not None
    assert sessions[stale].state == "idle"  # recent rollout, no holder: file existence never counts
    assert sessions[no_lock].state == "idle"
    assert set(sessions) == {live, stale, no_lock}  # .coordination.lock is not a thread


def test_a_held_lock_without_a_rollout_is_listed_from_the_lock_alone(roots, held_locks) -> None:
    thread_id = sid()
    hold(held_locks, write_lock(roots, thread_id, mtime=NOW - 12))
    assert not roots.codex_sessions.joinpath("2026").exists()

    result = make(roots).scan()
    (info,) = result.sessions
    assert (info.harness, info.session_id, info.state) == ("codex", thread_id, "live-read-only")
    assert info.registered is False and info.accepts == () and info.transcript is None
    assert info.last_activity_at == NOW - 12 and info.cwd is None
    wire = info.to_session(MACHINE)
    assert wire["address"] == f"{MACHINE}/codex/{thread_id}" and wire["state"] == "live-read-only"
    assert result.skipped == ()


def test_a_lock_only_thread_is_listed_when_the_codex_home_has_no_sessions_directory(tmp_path, held_locks) -> None:
    roots = D.DiscoveryRoots(tmp_path / "claude-home", tmp_path / "codex-home")  # neither store has a sessions dir
    thread_id = sid()
    hold(held_locks, write_lock(roots, thread_id))
    assert [(s.session_id, s.state) for s in make(roots).scan().sessions] == [(thread_id, "live-read-only")]


def test_a_probe_that_cannot_answer_is_reported_in_skipped_and_nothing_counts_as_held(roots) -> None:
    class Failing:
        def held(self, locks):
            raise D.ProbeError("cannot list open files")

    thread_id = sid()
    write_rollout(roots, thread_id)
    write_lock(roots, thread_id)
    result = make(roots, codex_lock_probe=Failing()).scan()
    assert [s.state for s in result.sessions] == ["idle"]
    assert [(e.path, e.reason) for e in result.skipped] == [(str(roots.codex_locks), "cannot list open files")]


def test_the_probe_is_not_run_when_no_thread_lock_exists(roots) -> None:
    class Never:
        def held(self, locks):  # pragma: no cover - failing the test is the point
            raise AssertionError("probed with no lock files")

    (roots.codex_locks).mkdir(parents=True)
    (roots.codex_locks / ".coordination.lock").write_text("")
    assert make(roots, codex_lock_probe=Never()).scan().sessions == ()


# --------------------------------------------------------------------------
# codex registrations in the table
# --------------------------------------------------------------------------

def test_a_codex_registration_has_a_codex_address_and_the_harness_is_part_of_every_key(sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    shared = sid()
    codex = register(table, shared, os.getpid(), harness="codex").registration
    claude = register(table, shared, sleeper.pid).registration  # the same id under the other harness
    assert (codex.harness, claude.harness) == ("codex", "claude")
    assert table.address(shared, "codex") == f"{MACHINE}/codex/{shared}" and table.address(shared) == f"{MACHINE}/claude/{shared}"
    assert table.get(shared, "codex") is codex and table.get(shared) is claude
    events: list[D.EndEvent] = []
    table.add_end_listener(events.append)
    table.deregister(codex.registration_id, shared, NONCE, "codex")
    assert [e.address for e in events] == [f"{MACHINE}/codex/{shared}"]
    assert table.get(shared) is claude  # the claude registration is untouched
    assert table.ended_reason(shared, "codex") == "deregistered" and table.ended_reason(shared) is None


def test_a_codex_registration_is_refused_by_the_wrong_harness_calls_and_by_an_unknown_harness(sleeper) -> None:
    table = make_table(Clock())
    thread_id = sid()
    registration = register(table, thread_id, os.getpid(), harness="codex").registration
    for call in (lambda: table.touch(registration.registration_id, thread_id, NONCE),
                 lambda: table.deregister(registration.registration_id, thread_id, NONCE),
                 lambda: table.heartbeat(registration.registration_id, thread_id, NONCE)):
        with pytest.raises(P.RelayError) as err:
            call()
        assert err.value.code == "registration_unknown"  # these default to claude: not this registration
    with pytest.raises(P.RelayError) as err:
        register(table, sid(), sleeper.pid, harness="gemini")
    assert err.value.code == "bad_request"
    assert table.touch(registration.registration_id, thread_id, NONCE, "codex").harness == "codex"


def test_a_codex_heartbeat_never_renames_the_thread(roots) -> None:
    table = make_table(Clock())
    thread_id = sid()
    registration = register(table, thread_id, os.getpid(), harness="codex").registration
    with pytest.raises(P.RelayError) as err:
        table.heartbeat(registration.registration_id, sid(), NONCE, previous_session_id=thread_id, harness="codex")
    assert err.value.code == "bad_request" and "does not change" in err.value.message
    assert table.get(thread_id, "codex") is not None  # nothing moved


# --------------------------------------------------------------------------
# merging a registration with the scan
# --------------------------------------------------------------------------

def test_a_codex_registration_is_live_accepting_while_its_pid_lives_and_idle_after(roots, sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    thread_id = sid()
    rollout = write_rollout(roots, thread_id)
    register(table, thread_id, sleeper.pid, harness="codex", title="relay thread", task="plugins-kit/harness-messaging",
             transcript_path=str(rollout))
    discovery = make(roots, table=table, clock=clock)
    (info,) = discovery.scan().sessions
    assert (info.harness, info.state, info.registered) == ("codex", "live-accepting", True)
    assert info.accepts == ("note", "instruction") and info.pid == sleeper.pid and info.title == "relay thread"
    assert info.transcript == rollout and info.address(MACHINE) == f"{MACHINE}/codex/{thread_id}"
    assert discovery.resolve("task:plugins-kit/harness-messaging").session_id == thread_id

    sleeper.kill()
    sleeper.wait()
    (after,) = discovery.scan().sessions
    assert after.state == "idle" and after.registered is True  # the process is gone, the rollout is recent


def test_a_registration_wins_over_a_held_lock_and_a_scan_of_the_same_id(roots, sleeper, held_locks) -> None:
    clock = Clock()
    table = make_table(clock)
    with_rollout, lock_only = sid(), sid()
    write_rollout(roots, with_rollout)
    for thread_id in (with_rollout, lock_only):
        hold(held_locks, write_lock(roots, thread_id))
    register(table, with_rollout, sleeper.pid, harness="codex")
    register(table, lock_only, os.getpid(), harness="codex")

    listed = make(roots, table=table, clock=clock).scan().sessions
    sessions = {s.session_id: s for s in listed}
    assert sorted(s.session_id for s in listed) == sorted([with_rollout, lock_only])  # merged, not listed twice
    for thread_id in (with_rollout, lock_only):
        assert (sessions[thread_id].state, sessions[thread_id].registered) == ("live-accepting", True)
    assert sessions[with_rollout].transcript is not None and sessions[with_rollout].transcript.name.endswith(f"{with_rollout}.jsonl")


def test_a_registered_thread_with_no_rollout_yet_reads_as_zero_turns(roots, sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    thread_id = sid()
    path = roots.codex_sessions / "2026" / "11" / "03" / f"rollout-2026-11-03T09-32-56-{thread_id}.jsonl"
    register(table, thread_id, sleeper.pid, harness="codex", transcript_path=str(path))  # thread.path: not yet written
    discovery = make(roots, table=table, clock=clock)
    assert not path.exists()

    result = discovery.read_session(f"{MACHINE}/codex/{thread_id}")
    assert result["turns"] == [] and result["cursor"] == "c:0" and result["truncated"] is False
    assert result["address"] == f"{MACHINE}/codex/{thread_id}" and result["redactions"] == 0
    assert discovery.read_session(f"{MACHINE}/codex/{thread_id}", since="c:0", limit=5)["turns"] == []
    for bad in ({"limit": 0}, {"since": "tomorrow"}):
        with pytest.raises(P.RelayError) as err:
            discovery.read_session(f"{MACHINE}/codex/{thread_id}", **bad)
        assert err.value.code == "bad_request"

    # a registration naming no path at all is the same: the thread has not run a turn
    other = sid()
    register(table, other, os.getpid(), harness="codex")
    assert discovery.read_session(f"{MACHINE}/codex/{other}")["turns"] == []


def test_a_codex_registration_reads_the_rollout_once_it_exists_even_at_another_path(roots, sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    thread_id = sid()
    rollout = write_rollout(roots, thread_id, lines=[
        {"timestamp": "2026-11-03T15:33:00.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello codex"}]}}])
    register(table, thread_id, sleeper.pid, harness="codex",
             transcript_path=str(roots.codex_sessions / "elsewhere" / "rollout-stale.jsonl"))
    (info,) = make(roots, table=table, clock=clock).scan().sessions
    assert info.transcript == rollout and info.registered is True


def test_other_missing_transcripts_are_still_errors(roots, sleeper) -> None:
    clock = Clock()
    table = make_table(clock)
    claude, scanned_lock_only = sid(), sid()
    register(table, claude, sleeper.pid, transcript_path=str(roots.claude_projects / "gone.jsonl"))
    with pytest.raises(P.RelayError) as err:
        make(roots, table=table, clock=clock).read_session(f"{MACHINE}/claude/{claude}")
    assert err.value.code == "target_not_found"
    handle = open(write_lock(roots, scanned_lock_only), "a")  # a scanned lock-only thread has no transcript to read
    try:
        with pytest.raises(P.RelayError) as err:
            make(roots, table=table, clock=clock).read_session(f"{MACHINE}/codex/{scanned_lock_only}")
        assert err.value.code == "target_not_found"
    finally:
        handle.close()


def test_one_process_one_registration_holds_for_codex_threads(roots) -> None:
    clock = Clock()
    table = make_table(clock)
    old, new = sid(), sid()
    write_rollout(roots, old)
    register(table, old, os.getpid(), harness="codex")
    register(table, new, os.getpid(), harness="codex")  # one process, one registration: the old id ends
    sessions = {s.session_id: s for s in make(roots, table=table, clock=clock).scan().sessions}
    assert sessions[old].state == "ended" and sessions[old].registered is False  # tombstone beats the recent rollout
    assert sessions[new].state == "live-accepting"


# --------------------------------------------------------------------------
# over a real relay
# --------------------------------------------------------------------------

@pytest.fixture
def relay(config, roots, backend, tmp_path) -> Iterator[Relay]:
    server = make_server(config, roots, backend, tmp_path).start()
    try:
        yield Relay(server, roots)
    finally:
        server.stop()


def test_a_codex_register_now_succeeds_and_every_advertised_harness_registers(relay, sleeper) -> None:
    status = relay.operator("status", {})
    assert status["harnesses"] == ["claude", "codex"]
    pids = {"claude": os.getppid(), "codex": sleeper.pid}  # one process, one registration: distinct pids
    for harness in status["harnesses"]:  # the advertisement is true: each listed harness registers
        session = sid()
        params = codex_register_params(session, harness=harness, pid=pids[harness])
        registered = relay.client.call("register", params, origin=f"channel:{MACHINE}/{harness}/{session}")
        assert registered["address"] == f"{MACHINE}/{harness}/{session}" and registered["replaced"] is False
        assert registered["machine"] == MACHINE and registered["lease_s"] == 90

    thread = sid()
    registered = codex_channel(relay, thread, "register", codex_register_params(thread, title="relay codex"))
    assert registered["address"] == f"{MACHINE}/codex/{thread}"
    wire = thread_wire(relay, thread)
    assert wire["state"] == "live-accepting" and wire["registered"] is True and wire["harness"] == "codex"
    assert wire["accepts"] == ["note", "instruction", "question"] and wire["title"] == "relay codex"
    assert wire["version"] == "codex-cli 0.164.0"
    assert relay.operator("status", {})["registrations"] == 3


def test_a_registered_codex_thread_queues_hub_and_granted_direct_sends_and_runs_the_whole_channel(relay) -> None:
    thread = sid()
    target = f"{MACHINE}/codex/{thread}"
    registered = codex_channel(relay, thread, "register", codex_register_params(thread))
    creds = codex_creds(registered, thread)

    via_hub = relay.hub("send_message", {"to": target, "kind": "note", "body": "from the hub"})["message"]
    assert via_hub["state"] == "queued" and via_hub["to"] == target
    via_direct = relay.send(target, kind="question", body="status?")
    assert via_direct["state"] == "queued" and via_direct["to"] == target

    polled = codex_channel(relay, thread, "poll", dict(creds, wait_s=0))
    assert [m["body"] for m in polled["messages"]] == ["from the hub", "status?"]
    for message in polled["messages"]:
        codex_channel(relay, thread, "ack", dict(creds, message_id=message["id"]))
    reply = codex_channel(relay, thread, "reply", dict(creds, in_reply_to=via_direct["id"], state="answered",
                                                       body="all green"))
    assert reply["message_state"] == "answered"
    beat = codex_channel(relay, thread, "heartbeat", dict(creds, title="renamed title"))
    assert beat["address"] == target and beat["session_changed"] is False
    assert thread_wire(relay, thread)["title"] == "renamed title"

    # a clean exit ends it: a later send is session_ended although no rollout or lock ever showed the thread
    ended = codex_channel(relay, thread, "deregister", dict(creds))
    assert ended["address"] == target
    relay.refused("session_ended", relay.hub, "send_message", {"to": target, "kind": "note", "body": "too late"})


def test_a_registered_codex_thread_with_no_rollout_reads_as_zero_turns_over_the_relay(relay) -> None:
    thread = sid()
    path = relay.roots.codex_sessions / "2026" / "11" / "12" / f"rollout-2026-11-12T15-09-21-{thread}.jsonl"
    codex_channel(relay, thread, "register", codex_register_params(thread, transcript_path=str(path)))
    read = relay.hub("read_session", {"address": f"{MACHINE}/codex/{thread}"})
    assert read["turns"] == [] and read["cursor"] == "c:0"


def test_a_scanned_live_codex_thread_is_live_read_only_and_a_send_to_it_is_not_opted_in(relay, held_locks) -> None:
    with_rollout, lock_only, stale = sid(), sid(), sid()
    write_rollout(relay.roots, with_rollout)
    write_rollout(relay.roots, stale, mtime=time.time() - 60)
    write_lock(relay.roots, stale, mtime=time.time() - 60)  # a SIGKILLed thread's file: unheld
    for thread_id in (with_rollout, lock_only):
        hold(held_locks, write_lock(relay.roots, thread_id, mtime=time.time() - 5))
    for thread_id in (with_rollout, lock_only):
        wire = thread_wire(relay, thread_id)
        assert wire["state"] == "live-read-only" and wire["registered"] is False and wire["accepts"] == []
        target = f"{MACHINE}/codex/{thread_id}"
        relay.refused("not_opted_in", relay.hub, "send_message", {"to": target, "kind": "note", "body": "hi"})
        relay.mint("send_message", [target])
        relay.refused("not_opted_in", relay.direct, "send_message", {"to": target, "kind": "note", "body": "hi"})
    assert thread_wire(relay, stale)["state"] == "idle"
    relay.refused("session_ended", relay.hub, "send_message",  # not live: the file alone is nothing
                  {"to": f"{MACHINE}/codex/{stale}", "kind": "note", "body": "hi"})


def test_a_channel_origin_harness_must_match_the_params_and_the_registration(relay, sleeper) -> None:
    thread = sid()
    params = codex_register_params(thread, pid=sleeper.pid)
    wrong_origin = relay.refused("origin_not_allowed", relay.client.call, "register", params,
                                 origin=f"channel:{MACHINE}/claude/{thread}")
    assert "harness" in wrong_origin.message
    claude_params = codex_register_params(thread, harness="claude", pid=sleeper.pid)
    relay.refused("origin_not_allowed", relay.client.call, "register", claude_params,
                  origin=f"channel:{MACHINE}/codex/{thread}")
    assert relay.operator("status", {})["registrations"] == 0  # no registration came of either

    # the same id registered under claude: a codex origin cannot poll or deregister it
    claude = relay.register(thread)
    for verb, extra in (("poll", {"wait_s": 0}), ("deregister", {})):
        relay.refused("registration_unknown", codex_channel, relay, thread, verb, dict(codex_creds(claude, thread), **extra))
    codex = codex_channel(relay, thread, "register", params)
    assert codex["registration_id"] != claude["registration_id"]
    assert relay.operator("status", {})["registrations"] == 2  # one per harness, same id
    relay.refused("registration_unknown", relay.channel, thread, "poll", dict(codex_creds(codex, thread), wait_s=0))


def test_a_codex_heartbeat_with_a_previous_session_id_is_refused_over_the_relay(relay) -> None:
    thread, other = sid(), sid()
    registered = codex_channel(relay, thread, "register", codex_register_params(thread))
    error = relay.refused("bad_request", codex_channel, relay, other, "heartbeat",
                          dict(codex_creds(registered, other), previous_session_id=thread))
    assert "does not change" in error.message
    assert thread_wire(relay, thread)["state"] == "live-accepting"
