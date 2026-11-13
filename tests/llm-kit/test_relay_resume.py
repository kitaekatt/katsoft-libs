"""R1: remote resume (``continue_session``, amendment 3) against a real relay, the fake Codex app-server and a fake claude.

No real Codex, Claude, model or quota. The Codex child is tests/llm-kit/fixtures/fake_codex_app_server.py behind the
launcher of test_relay_codex_channel.py (every start and argv recorded); the Claude child is
tests/llm-kit/fixtures/fake_claude.py (every call recorded). The relay is a real
:class:`~llm_kit.relay.server.RelayServer` on an ephemeral loopback port over tmp harness homes. Crash windows use two
:class:`~llm_kit.relay.resume.ResumeRunner` instances on one mailbox, the way a restarted relay sees its predecessor's
leases. After every case that ends a claim, the claim invariant holds: no claimed message without its lease, no lease
without its claimed message.
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
from typing import Any, Callable, Optional

import psutil
import pytest

from llm_kit.relay import config as C
from llm_kit.relay import mailbox as M
from llm_kit.relay import protocol as P
from llm_kit.relay import resume as R
from llm_kit.relay import service as SV

from test_relay_codex_channel import make_launcher, requests, stub_records, turn_texts
from test_relay_server import (  # noqa: F401 -- fixtures and helpers
    DIRECT, MACHINE, Relay, addr, backend, config, make_server, roots,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="remote resume runs POSIX process groups")

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
ON = "enabled: true\nresume: {enabled: true, timeout_s: 20}\n"


def wait_for(predicate: Callable[[], Any], timeout: float = 20.0, what: str = "condition") -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class World:
    """A started relay with remote resume, its launchers and helpers for idle sessions."""

    def __init__(self, config, roots, backend, tmp_path: Path, monkeypatch, *, yaml: str = ON,
                 stub_cfg: Optional[dict] = None, **resume_options: Any) -> None:
        config.config_path.write_text(yaml, encoding="utf-8")
        self.tmp, self.roots, self.config = tmp_path, roots, config
        self.launcher = make_launcher(tmp_path)
        self.stub_log = tmp_path / "stub.log.jsonl"
        self.stub_log.touch()
        self.claude_log = tmp_path / "claude.log.jsonl"
        monkeypatch.setenv("FAKE_CODEX_LOG", str(self.stub_log))
        monkeypatch.setenv("FAKE_CODEX_CFG", json.dumps(stub_cfg or {}))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.claude_log))
        monkeypatch.setenv("CLAUDECODE", "1")  # the relay must strip it from the child
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        options = {"codex_argv_prefix": [str(self.launcher.path)],
                   "claude_argv": [sys.executable, str(FAKE_CLAUDE)], **resume_options}
        self.server = make_server(config, roots, backend, tmp_path, resume_options=options).start()
        self.relay = Relay(self.server, roots)
        self.work = tmp_path / "work"
        self.work.mkdir(exist_ok=True)
        self.cwd = os.path.realpath(self.work)

    def stop(self) -> None:
        self.server.stop()

    @property
    def mailbox(self) -> M.Mailbox:
        return self.server.mailbox

    # -- idle sessions ----------------------------------------------------------------

    def codex_idle(self, *, opted: bool = True, age: float = 0.0, cwd: Optional[str] = None) -> str:
        thread = str(uuid.uuid4())
        day = self.roots.codex_sessions / "2026" / "11" / "13"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-11-13T00-00-00-{thread}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": thread, "cwd": cwd or self.cwd, "cli_version": "0.164.0"}}) + "\n", encoding="utf-8")
        if age:
            os.utime(path, (time.time() - age, time.time() - age))
        address = f"{MACHINE}/codex/{thread}"
        if opted:
            self.mailbox.record_opt_in(address)
        return address

    def claude_idle(self, *, opted: bool = True) -> str:
        session = str(uuid.uuid4())
        path = self.roots.claude_projects / "-tmp-proj" / f"{session}.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"},
                                    "cwd": self.cwd}) + "\n", encoding="utf-8")
        address = addr(session)
        if opted:
            self.mailbox.record_opt_in(address)
        return address

    # -- calls --------------------------------------------------------------------------

    def hub(self, to: str, body: str = "what next?", **extra: Any) -> dict:
        return self.relay.hub("continue_session", {"to": to, "kind": "question", "body": body, **extra})

    def direct(self, to: str, body: str = "what next?", *, mint: bool = True, **extra: Any) -> dict:
        if mint:
            self.relay.mint("continue_session", [to])
        return self.relay.direct("continue_session", {"to": to, "kind": "question", "body": body, **extra})

    def finished(self, message_id: str, timeout: float = 20.0) -> dict:
        record = wait_for(lambda: (lambda m: m if m["state"] in P.TERMINAL_STATES else None)(
            self.mailbox.get_message(message_id)), timeout, f"message {message_id} to finish")
        wait_for(lambda: not self.mailbox.leases(), 5.0, "the lease to be deleted")
        assert self.mailbox.claim_problems() == []
        return record

    def claude_calls(self) -> list[dict]:
        if not self.claude_log.exists():
            return []
        return [json.loads(line) for line in self.claude_log.read_text().splitlines() if line.strip()]


@pytest.fixture
def world(config, roots, backend, tmp_path, monkeypatch):
    built: list[World] = []

    def make(**kw: Any) -> World:
        w = World(config, roots, backend, tmp_path, monkeypatch, **kw)
        built.append(w)
        return w

    yield make
    for w in built:
        w.stop()


def refused(code: str, call: Callable[[], Any]) -> P.RelayError:
    with pytest.raises(P.RelayError) as caught:
        call()
    assert caught.value.code == code, caught.value
    return caught.value


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def test_resume_config_defaults_off_and_refuses_bypass_and_unknown_keys(config):
    path = config.config_path
    assert C.read_resume_config(path) == C.ResumeConfig(enabled=False, timeout_s=1800, claude_permission_mode="default")
    path.write_text("enabled: true\nresume: {enabled: true, timeout_s: 60}\n", encoding="utf-8")
    assert C.read_resume_config(path) == C.ResumeConfig(enabled=True, timeout_s=60)
    path.write_text("enabled: false\nresume: {enabled: true}\n", encoding="utf-8")
    assert C.read_resume_config(path).enabled is False  # the relay itself must be on too
    for bad in ("resume: {claude_permission_mode: bypassPermissions}", "resume: {claude_permission_mode: auto}",
                "resume: {enabled: yes-please}", "resume: {timeout_s: 0}", "resume: {launch: now}"):
        path.write_text("enabled: true\n" + bad + "\n", encoding="utf-8")
        with pytest.raises(C.RelayConfigError):
            C.read_resume_config(path)


def test_status_reports_whether_continue_session_runs_and_follows_the_file_without_a_restart(world):
    w = world(yaml="enabled: true\n")
    assert w.relay.operator("status", {})["continue_session"] == {"enabled": False}
    w.config.config_path.write_text(ON, encoding="utf-8")
    assert w.relay.operator("status", {})["continue_session"] == {"enabled": True}


# --------------------------------------------------------------------------
# refusals (each with no effect: no row, no launch)
# --------------------------------------------------------------------------

def test_resume_is_off_by_default(world):
    w = world(yaml="enabled: true\n")
    target = w.codex_idle()
    error = refused("not_opted_in", lambda: w.hub(target))
    assert error.details["why"] == "resume_disabled" and error.details["key"] == "resume.enabled"
    assert error.details["config"] == str(w.config.config_path)
    assert w.mailbox.messages_to(target) == [] and w.launcher.launches() == 0


def test_turning_resume_off_takes_effect_on_the_next_call(world):
    w = world()
    target = w.codex_idle()
    w.config.config_path.write_text("enabled: true\nresume: {enabled: false}\n", encoding="utf-8")
    assert refused("not_opted_in", lambda: w.hub(target)).details["why"] == "resume_disabled"
    assert w.launcher.launches() == 0


def test_a_session_that_never_opted_in_is_refused(world):
    w = world()
    target = w.codex_idle(opted=False)
    assert refused("not_opted_in", lambda: w.hub(target)).details["why"] == "never_opted_in"
    assert w.mailbox.messages_to(target) == [] and w.launcher.launches() == 0


def test_register_and_heartbeat_record_the_opt_in(world):
    w = world()
    session = str(uuid.uuid4())
    reg = w.relay.register(session)
    assert w.mailbox.opted_in(addr(session))
    renamed = str(uuid.uuid4())
    w.relay.channel(renamed, "heartbeat", {**w.relay.creds(reg, renamed), "previous_session_id": session})
    assert w.mailbox.opted_in(addr(renamed))  # a /clear rename carries the opt-in to the new id
    other = str(uuid.uuid4())
    w.relay.register(other, accepts=())
    assert not w.mailbox.opted_in(addr(other))  # a read-only registration is no opt-in


def test_a_live_session_takes_send_message(world):
    w = world()
    session = str(uuid.uuid4())
    w.relay.register(session)  # live-accepting: this test process is its pid
    error = refused("invalid_transition", lambda: w.hub(addr(session)))
    assert error.details == {"why": "session_live", "state": "live-accepting", "use": "send_message"}
    assert w.mailbox.messages_to(addr(session)) == []


def test_an_ended_session_is_refused(world):
    w = world()
    target = w.codex_idle(age=30 * 24 * 3600)
    refused("session_ended", lambda: w.hub(target))
    assert w.launcher.launches() == 0


def test_a_session_without_an_existing_cwd_is_refused(world):
    w = world()
    target = w.codex_idle(cwd="/nonexistent/relay-test")
    assert refused("target_invalid", lambda: w.hub(target)).details["why"] == "no_cwd"


def test_negative_origins(world):
    w = world()
    target = w.codex_idle()
    refused("origin_not_allowed", lambda: w.relay.client.call(
        "continue_session", {"to": target, "kind": "note", "body": "x"}, origin=f"channel:{addr(DIRECT)}"))
    refused("origin_not_allowed", lambda: w.relay.operator("continue_session", {"to": target, "kind": "note", "body": "x"}))
    refused("grant_required", lambda: w.direct(target, mint=False))
    assert w.launcher.launches() == 0 and w.mailbox.messages_to(target) == []


def test_a_target_on_another_machine_is_refused_not_forwarded(world):
    w = world()
    error = refused("target_invalid", lambda: w.hub(f"tower/codex/{uuid.uuid4()}"))
    assert error.details["why"] == "not_forwarded"


def test_a_paused_relay_refuses_continue_session(world):
    w = world()
    target = w.codex_idle()
    w.relay.operator("pause", {"scope": "relay"})
    refused("paused", lambda: w.hub(target))
    assert w.launcher.launches() == 0


# --------------------------------------------------------------------------
# Codex runs (C2 over the fake app-server)
# --------------------------------------------------------------------------

def test_a_codex_continue_is_answered_with_the_header_first(world):
    w = world()
    target = w.codex_idle()
    message = w.hub(target, body="where are we?")["message"]
    assert message["state"] == "delivered" and message["sender"].startswith("claude.ai/")
    done = w.finished(message["id"])
    assert done["state"] == "answered" and done["reply"]["body"] == "answer-1"
    assert done["reply"]["sender"] == f"relay:{MACHINE}"
    [text] = turn_texts(w.stub_log)
    assert text.startswith(f"[relay message {message['id']} from claude.ai/")
    assert "counts as the owner's own words" in text.splitlines()[1] and text.endswith("\n\nwhere are we?")
    argv = w.launcher.argv()
    for wanted in ("features.hooks=false", "notify=[]", 'approval_policy="never"',
                   "sandbox_workspace_write.network_access=false", "stdio://"):
        assert wanted in argv, wanted
    [resume] = requests(w.stub_log, "thread/resume")
    sw = resume["params"]["config"]["sandbox_workspace_write"]
    assert resume["params"]["cwd"] == w.cwd and sw["exclude_slash_tmp"] is True and sw["exclude_tmpdir_env_var"] is True
    assert resume["params"]["approvalsReviewer"] == "user" and resume["params"]["threadId"] == target.rsplit("/", 1)[1]


def test_a_direct_continue_consumes_its_grant_in_the_claim_and_a_refusal_consumes_none(world):
    w = world()
    target = w.codex_idle(opted=False)
    grant = w.relay.mint("continue_session", [target])
    refused("not_opted_in", lambda: w.direct(target, mint=False))
    assert w.mailbox.get_grant(grant["grant_id"])["uses_remaining"] == 1
    w.mailbox.record_opt_in(target)
    message = w.direct(target, mint=False)["message"]
    assert message["sender"] == f"direct:{addr(DIRECT)}"
    assert w.mailbox.get_grant(grant["grant_id"])["uses_remaining"] == 0
    assert w.finished(message["id"])["state"] == "answered"


def test_a_codex_config_mismatch_is_declined(world):
    w = world(stub_cfg={"thread_overrides": {"sandbox": {"excludeSlashTmp": False}}})
    target = w.codex_idle()
    done = w.finished(w.hub(target)["message"]["id"])
    assert done["state"] == "declined" and done["reply"]["body"].startswith("config_mismatch")
    assert turn_texts(w.stub_log) == []  # no turn ran under the wrong policy


def test_a_codex_approval_request_is_cancelled_interrupted_and_declined(world):
    w = world()
    target = w.codex_idle()
    done = w.finished(w.hub(target, body="MODE=appr-cmd")["message"]["id"])
    assert done["state"] == "declined" and "approval_requested" in done["reply"]["body"]
    answers = [r["msg"] for r in stub_records(w.stub_log) if r["dir"] == "in" and "result" in r["msg"]]
    assert {"decision": "cancel"} in [a["result"] for a in answers]
    assert requests(w.stub_log, "turn/interrupt")
    assert not any("turn_continued_after_approval" in json.dumps(r) for r in stub_records(w.stub_log))


@pytest.mark.parametrize("mode,why", [("exit", "codex"), ("hang", "timeout")])
def test_a_codex_process_failure_or_timeout_is_declined_and_leaves_no_child(world, mode, why):
    w = world(yaml="enabled: true\nresume: {enabled: true, timeout_s: 2}\n")
    target = w.codex_idle()
    done = w.finished(w.hub(target, body=f"MODE={mode}")["message"]["id"])
    assert done["state"] == "declined" and why in done["reply"]["body"]
    pid = next(r["msg"]["pid"] for r in stub_records(w.stub_log) if r["dir"] == "stub" and "pid" in r["msg"])
    wait_for(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE,
             10, "the app-server child to be gone")


def test_a_replayed_id_runs_once_and_another_call_meanwhile_is_rate_limited(world):
    w = world()
    target = w.codex_idle()
    first = w.hub(target, body="MODE=hang", id="msg_resume_once")
    again = w.hub(target, body="MODE=hang", id="msg_resume_once")
    assert again["replayed"] is True and again["message"]["id"] == first["message"]["id"]
    error = refused("rate_limited", lambda: w.hub(target, body="second"))
    assert error.details["what"] == "resume_running" and error.retryable is True
    assert len(w.mailbox.messages_to(target)) == 1
    refused("idempotency_conflict", lambda: w.relay.hub("send_message", {
        "to": target, "kind": "question", "body": "MODE=hang", "id": "msg_resume_once"}))


def test_two_simultaneous_continue_calls_one_runs(world):
    w = world()
    target = w.codex_idle()
    outcomes: list[Any] = []
    barrier = threading.Barrier(2)

    def call(n: int) -> None:
        barrier.wait()
        try:
            outcomes.append(w.hub(target, body=f"MODE=hang call {n}"))
        except P.RelayError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=call, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    errors = [o for o in outcomes if isinstance(o, P.RelayError)]
    assert len(outcomes) == 2 and len(errors) == 1 and errors[0].code == "rate_limited"
    assert len(w.mailbox.leases()) == 1 and len(w.mailbox.messages_to(target)) == 1


def test_a_returning_channel_waits_and_poll_never_yields_the_claimed_message(world):
    w = world()
    target = w.codex_idle()
    thread = target.rsplit("/", 1)[1]
    message = w.hub(target, body="MODE=hang")["message"]
    params = {"session_id": thread, "harness": "codex", "pid": os.getpid(), "cwd": w.cwd, "version": "codex",
              "accepts": ["question"], "channel_nonce": "nonce-" + "y" * 20}
    origin = f"channel:{target}"
    error = refused("registration_conflict", lambda: w.relay.client.call("register", params, origin=origin))
    assert error.details["why"] == "resume_running" and error.retryable is True
    assert w.mailbox.poll(target) == []  # claimed rows are never delivered to a channel
    other = w.mailbox.send_message(origin="claude.ai/owner", to=target, kind="note", body="other")["message"]
    assert [m["id"] for m in w.mailbox.poll(target)] == [other["id"]]  # only the unclaimed one
    w.mailbox.ack_timeout_s = 1
    time.sleep(1.2)
    assert [m["id"] for m in w.mailbox.poll(target)] == [other["id"]]  # the claimed one is never redelivered
    refused("invalid_transition", lambda: w.mailbox.ack(target, message["id"]))
    refused("invalid_transition", lambda: w.mailbox.reply(target, message["id"], "answered", "hijack"))


# --------------------------------------------------------------------------
# Claude runs (fake claude)
# --------------------------------------------------------------------------

def test_a_claude_continue_is_answered_and_the_child_is_checked(world):
    w = world()
    target = w.claude_idle()
    message = w.direct(target, body="/direct send somewhere else")["message"]
    done = w.finished(message["id"])
    assert done["state"] == "answered" and done["reply"]["body"] == "answer from claude"
    [call] = w.claude_calls()
    session = target.rsplit("/", 1)[1]
    assert call["argv"] == ["-p", "--resume", session, "--output-format", "stream-json", "--verbose",
                            "--permission-mode", "default"]
    assert os.path.realpath(call["cwd"]) == w.cwd
    assert call["claudecode"] is False and call["claude_code_vars"] == []
    assert call["lease"] and call["lease"].startswith("rpt_")  # the persisted process tag, not the fencing token
    assert call["pgid"] == call["pid"]  # its own process group
    assert call["prompt"].startswith(f"[relay message {message['id']} from direct:")  # never the body first
    assert call["prompt"].endswith("\n\n/direct send somewhere else")


@pytest.mark.parametrize("mode", ["mismatch-cwd", "mismatch-mode", "mismatch-session"])
def test_a_claude_init_mismatch_is_declined_and_the_child_killed(world, mode):
    w = world()
    target = w.claude_idle()
    started = time.monotonic()
    done = w.finished(w.direct(target, body=f"MODE={mode}")["message"]["id"])
    assert done["state"] == "declined" and done["reply"]["body"].startswith("config_mismatch")
    assert "MUST-NOT" not in done["reply"]["body"] and time.monotonic() - started < 20  # killed, not waited out
    [call] = w.claude_calls()
    assert not psutil.pid_exists(call["pid"]) or psutil.Process(call["pid"]).status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize("mode,text", [("noresult", "without a result"), ("iserror", "error"),
                                       ("exit1", "exited 1"), ("empty", "empty")])
def test_a_claude_run_without_a_good_result_is_declined(world, mode, text):
    w = world()
    target = w.claude_idle()
    done = w.finished(w.direct(target, body=f"MODE={mode}")["message"]["id"])
    assert done["state"] == "declined" and text in done["reply"]["body"]


def test_a_stale_claude_result_before_the_init_is_never_returned(world):
    w = world()
    target = w.claude_idle()
    done = w.finished(w.direct(target, body="MODE=stale")["message"]["id"])
    assert done["state"] == "answered" and done["reply"]["body"] == "fresh"


def test_a_claude_timeout_kills_the_group_and_declines(world):
    w = world(yaml="enabled: true\nresume: {enabled: true, timeout_s: 1}\n")
    target = w.claude_idle()
    done = w.finished(w.direct(target, body="MODE=hang")["message"]["id"])
    assert done["state"] == "declined" and done["reply"]["body"].startswith("timeout")
    [call] = w.claude_calls()
    wait_for(lambda: not psutil.pid_exists(call["pid"]), 10, "the claude child to be gone")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_relay_stop_kills_a_running_child_and_declines(world, harness):
    w = world()
    target = w.claude_idle() if harness == "claude" else w.codex_idle()
    message = w.direct(target, body="MODE=hang")["message"]
    if harness == "claude":
        wait_for(lambda: w.claude_calls(), 10, "the claude child to start")
        pid = w.claude_calls()[0]["pid"]
    else:
        wait_for(lambda: turn_texts(w.stub_log), 10, "the codex turn to start")
        pid = next(r["msg"]["pid"] for r in stub_records(w.stub_log) if r["dir"] == "stub" and "pid" in r["msg"])
    box = w.mailbox
    w.stop()
    record = box.get_message(message["id"])
    assert record["state"] == "declined" and record["reply"]["body"] == R.REASON_RELAY_STOP
    assert box.leases() == [] and box.claim_problems() == []
    wait_for(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE,
             10, "the child to be gone")


# --------------------------------------------------------------------------
# crash windows: a second runner (the restarted relay) recovers the first one's leases
# --------------------------------------------------------------------------

@pytest.fixture
def box(tmp_path) -> M.Mailbox:
    return M.Mailbox(tmp_path / "mailbox.sqlite3", machine=MACHINE)


def claim(box: M.Mailbox, runner: R.ResumeRunner, body: str = "hi", *,
          proc_tag: Optional[str] = None) -> tuple[str, str]:
    """A stored and claimed continue_session message of ``runner``: (message id, lease id)."""
    target = f"{MACHINE}/claude/{uuid.uuid4()}"
    lease = M.LeaseClaim(R.new_lease_id(), runner.relay_instance, runner.lease_s, proc_tag=proc_tag)
    record = box.send_message(origin="claude.ai/owner", to=target, kind="question", body=body, lease=lease)["message"]
    return record["id"], lease.lease_id


def ended(box: M.Mailbox, message_id: str) -> dict:
    record = box.get_message(message_id)
    assert box.leases() == [] and box.claim_problems() == []
    return record


def sleeper(lease_id: Optional[str] = None) -> subprocess.Popen:
    env = R.child_env(lease_id) if lease_id else dict(os.environ)
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], env=env, start_new_session=True)


def test_crash_after_the_claim_before_the_spawn(box):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box)
    message_id, _ = claim(box, dead)
    assert fresh.recover() == 1
    record = ended(box, message_id)
    assert record["state"] == "declined" and record["reply"]["body"] == R.REASON_BEFORE_START


def test_crash_while_spawning_keeps_the_claim_while_a_tagged_process_runs_and_never_signals_it(box):
    # no pid was recorded, so nothing proves the tagged process is the run's: it is never signalled, and the
    # claim stays held until no process of this user carries the tag
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box)
    message_id, lease_id = claim(box, dead)
    box.set_lease_phase(lease_id, "spawning")
    child = sleeper(lease_id)
    try:
        wait_for(lambda: R.tagged_processes(lease_id), 10, "the tag to be readable")
        assert fresh.recover() == 0
        assert child.poll() is None and len(box.leases()) == 1
        assert box.get_message(message_id)["state"] not in P.TERMINAL_STATES
        child.kill()
        child.wait()
        assert fresh.recover() == 1
        assert ended(box, message_id)["reply"]["body"] == R.REASON_STOPPED
    finally:
        child.kill()


def test_a_copied_tag_never_selects_a_process_and_keeps_the_claim_until_it_ends(box):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.3)
    tag = f"rpt_copied_{uuid.uuid4().hex[:8]}"
    message_id, lease_id = claim(box, dead, proc_tag=tag)
    child = sleeper(tag)
    stranger = sleeper(tag)  # copied the tag: another session, not a descendant of the recorded child
    try:
        wait_for(lambda: len(R.tagged_processes(tag)) == 2, 10, "both tags to be readable")
        box.set_lease_phase(lease_id, "running", child_pid=child.pid,
                            child_start=psutil.Process(child.pid).create_time(), pgid=child.pid)
        assert fresh.recover() == 0
        assert child.wait(10) is not None  # the recorded child is stopped
        assert stranger.poll() is None  # the copied tag is never signalled
        assert len(box.leases()) == 1  # and termination is not confirmed while it runs
        stranger.kill()
        stranger.wait()
        assert fresh.recover() == 1
        assert ended(box, message_id)["reply"]["body"] == R.REASON_STOPPED
    finally:
        for proc in (child, stranger):
            proc.kill()
            proc.wait()


def test_crash_while_running_with_the_child_alive_stops_it_identity_checked(box):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.5)
    message_id, lease_id = claim(box, dead)
    child = sleeper(lease_id)
    try:
        box.set_lease_phase(lease_id, "running", child_pid=child.pid,
                            child_start=psutil.Process(child.pid).create_time(), pgid=child.pid)
        assert fresh.recover() == 1
        assert child.wait(10) is not None
        assert ended(box, message_id)["reply"]["body"] == R.REASON_STOPPED
    finally:
        child.kill()


@pytest.mark.parametrize("window", ["child_exited", "finished_before_finish_claim"])
def test_crash_after_the_child_exited_declines_result_lost(box, window):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box)
    message_id, lease_id = claim(box, dead)
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    start = psutil.Process(child.pid).create_time() if psutil.pid_exists(child.pid) else time.time()
    box.set_lease_phase(lease_id, "running", child_pid=child.pid, child_start=start, pgid=child.pid)
    child.wait()
    assert fresh.recover() == 1
    assert ended(box, message_id)["reply"]["body"] == R.REASON_LOST


def test_a_reused_pid_is_never_signalled(box):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.5)
    message_id, lease_id = claim(box, dead)
    stranger = sleeper()  # holds the recorded pid now, but was created at another time
    try:
        start = psutil.Process(stranger.pid).create_time()
        box.set_lease_phase(lease_id, "running", child_pid=stranger.pid, child_start=start - 3600, pgid=stranger.pid)
        assert fresh.recover() == 1
        assert stranger.poll() is None  # never signalled
        assert ended(box, message_id)["reply"]["body"] == R.REASON_LOST
    finally:
        stranger.kill()
        stranger.wait()


FAMILY = """
import os, signal, subprocess, sys, time
pidfile, leader_exits, untagged = sys.argv[1], sys.argv[2] == "exit", sys.argv[3] == "untagged"
code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
env = {k: v for k, v in os.environ.items() if not (untagged and k == "LLM_KIT_RESUME_LEASE")}
child = subprocess.Popen([sys.executable, "-c", code], env=env)  # same group, ignores SIGTERM
open(pidfile, "w").write(str(child.pid))
if leader_exits:
    os._exit(0)
time.sleep(300)
"""


def family(tmp_path: Path, tag: str, *, leader_exits: bool, untagged: bool,
           reap: bool = True) -> tuple[subprocess.Popen, int]:
    """A tagged leader in its own group with a descendant that ignores SIGTERM (and, ``untagged``, dropped the
    tag from its environment, so only its process group ties it to the run); (leader, descendant pid)."""
    pidfile = tmp_path / f"desc-{uuid.uuid4().hex}.pid"
    leader = subprocess.Popen([sys.executable, "-c", FAMILY, str(pidfile), "exit" if leader_exits else "stay",
                               "untagged" if untagged else "tagged"], env=R.child_env(tag), start_new_session=True)
    desc = int(wait_for(lambda: pidfile.exists() and pidfile.read_text(), 10, "the descendant to start"))
    wanted = None if untagged else tag
    wait_for(lambda: psutil.Process(desc).environ().get(R.LEASE_ENV, None) == wanted or None, 10,
             "the descendant's environment")
    if leader_exits and reap:
        leader.wait(10)
    elif leader_exits:  # left a zombie: this test process is its parent and has not reaped it
        wait_for(lambda: psutil.Process(leader.pid).status() == psutil.STATUS_ZOMBIE, 10, "the leader to exit")
    return leader, desc


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.parametrize("leader_exits,untagged", [(False, False), (False, True), (True, False)],
                         ids=["leader_alive-tagged", "leader_alive-untagged", "leader_already_dead-tagged"])
def test_recovery_stops_a_descendant_that_ignores_sigterm(box, tmp_path, leader_exits, untagged):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.3)
    tag = f"rpt_family_{uuid.uuid4().hex[:8]}"
    message_id, lease_id = claim(box, dead, proc_tag=tag)
    leader, desc = family(tmp_path, tag, leader_exits=leader_exits, untagged=untagged)
    try:
        start = -1.0 if leader_exits else psutil.Process(leader.pid).create_time()
        box.set_lease_phase(lease_id, "running", child_pid=leader.pid, child_start=start, pgid=leader.pid)
        assert fresh.recover() == 1
        assert gone(desc)  # SIGKILL reached it although the leader had exited (or exited on SIGTERM)
        assert leader.wait(10) is not None
        ended(box, message_id)
    finally:
        for pid in (desc, leader.pid):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


def test_an_untagged_survivor_of_a_reaped_leader_is_never_signalled_and_keeps_the_claim(box, tmp_path):
    # the leader is gone and reaped: its group is the run's only with the tag as well, so an untagged member is
    # not signalled, and termination is not confirmed while it runs
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.3)
    tag = f"rpt_family_{uuid.uuid4().hex[:8]}"
    message_id, lease_id = claim(box, dead, proc_tag=tag)
    leader, desc = family(tmp_path, tag, leader_exits=True, untagged=True)
    try:
        box.set_lease_phase(lease_id, "running", child_pid=leader.pid, child_start=-1.0, pgid=leader.pid)
        assert fresh.recover() == 0
        assert not gone(desc) and len(box.leases()) == 1
        os.kill(desc, 9)
        wait_for(lambda: gone(desc), 10, "the survivor to die")
        assert fresh.recover() == 1
        ended(box, message_id)
    finally:
        try:
            os.kill(desc, 9)
        except ProcessLookupError:
            pass


def test_a_zombie_leader_with_its_recorded_identity_proves_its_group_so_untagged_survivors_stop(box, tmp_path):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.3)
    tag = f"rpt_zombie_{uuid.uuid4().hex[:8]}"
    message_id, lease_id = claim(box, dead, proc_tag=tag)
    leader, desc = family(tmp_path, tag, leader_exits=True, untagged=True, reap=False)
    try:
        start = psutil.Process(leader.pid).create_time()  # a zombie keeps its create time
        box.set_lease_phase(lease_id, "running", child_pid=leader.pid, child_start=start, pgid=leader.pid)
        assert fresh.recover() == 1
        assert gone(desc)  # untagged, but a member of the group the zombie leader still proves
        ended(box, message_id)
    finally:
        try:
            os.kill(desc, 9)
        except ProcessLookupError:
            pass
        leader.wait(10)


def test_a_crash_between_fence_and_kill_still_finds_the_run_by_its_persisted_tag(box, tmp_path):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.3)
    message_id, lease_id = claim(box, dead, proc_tag="rpt_persisted_" + uuid.uuid4().hex[:8])
    tag = box.leases()[0]["proc_tag"]
    leader, desc = family(tmp_path, tag, leader_exits=True, untagged=False)
    try:
        box.set_lease_phase(lease_id, "running", child_pid=leader.pid, child_start=-1.0, pgid=leader.pid)
        # a recovering relay fenced the lease, then crashed before it stopped the run
        box.take_lease(lease_id, new_lease_id=R.new_lease_id(), relay_instance="rin_crashed")
        assert box.leases()[0]["proc_tag"] == tag
        assert fresh.recover() == 1
        assert gone(desc)  # a member of the reaped leader's group that carries the persisted tag
        ended(box, message_id)
    finally:
        try:
            os.kill(desc, 9)
        except ProcessLookupError:
            pass


def test_a_claude_run_stops_an_untagged_group_survivor_before_reaping_its_leader(world):
    w = world()
    target = w.claude_idle()
    done = w.finished(w.direct(target, body="MODE=orphan")["message"]["id"])
    assert done["state"] == "answered", done["reply"]["body"]
    orphan = int(Path(str(w.claude_log) + ".orphan").read_text())
    assert gone(orphan)


def test_a_run_whose_processes_did_not_stop_keeps_its_claim_until_a_sweep_confirms_it(box, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log.jsonl"))
    runner = R.ResumeRunner(box, claude_argv=[sys.executable, str(FAKE_CLAUDE)], term_grace_s=0.3)
    tag = f"rpt_unstopped_{uuid.uuid4().hex[:8]}"
    message_id, lease_id = claim(box, runner, proc_tag=tag)
    address = box.leases()[0]["address"]
    stranger = sleeper(tag)  # a process with the run's tag that is not provably the run's: never confirmed gone
    try:
        wait_for(lambda: R.tagged_processes(tag), 10, "the tag to be readable")
        job = R.ResumeJob(lease_id=lease_id, message_id=message_id, address=address, harness="claude",
                          session_id=address.rsplit("/", 1)[1], cwd=str(tmp_path), prompt="hi", timeout_s=20,
                          proc_tag=tag)
        runner.start(job).join(30)
        lease = box.leases()
        assert len(lease) == 1 and lease[0]["phase"] == "stopping"  # the run ended; its claim did not
        assert box.get_message(message_id)["state"] not in P.TERMINAL_STATES
        assert runner.recover() == 0 and len(box.leases()) == 1  # every sweep retries; still unconfirmed
        stranger.kill()
        stranger.wait()
        assert runner.recover() == 1
        record = ended(box, message_id)
        assert record["state"] == "answered" and record["reply"]["body"] == "answer from claude"
    finally:
        stranger.kill()
        stranger.wait()


def test_a_pid_created_half_a_second_later_is_a_stranger(box):
    dead, fresh = R.ResumeRunner(box), R.ResumeRunner(box, term_grace_s=0.3)
    message_id, lease_id = claim(box, dead)
    stranger = sleeper()
    try:
        start = psutil.Process(stranger.pid).create_time()
        box.set_lease_phase(lease_id, "running", child_pid=stranger.pid, child_start=start - 0.5, pgid=stranger.pid)
        assert fresh.recover() == 1
        assert stranger.poll() is None  # never signalled: the identity must match exactly
        assert ended(box, message_id)["reply"]["body"] == R.REASON_LOST
    finally:
        stranger.kill()
        stranger.wait()


def test_a_claude_run_never_signals_a_process_group_by_number(world, monkeypatch):
    w = world()
    target = w.claude_idle()

    def refuse(*args):
        raise AssertionError(f"os.killpg{args}: a group id may name a stranger's group once its leader is reaped")

    monkeypatch.setattr(os, "killpg", refuse)
    done = w.finished(w.direct(target)["message"]["id"])
    assert done["state"] == "answered", done["reply"]["body"]


def test_a_heartbeat_rename_onto_a_leased_address_is_refused(world):
    w = world()
    target = w.claude_idle()
    message = w.direct(target, body="MODE=hang")["message"]
    wait_for(lambda: w.claude_calls(), 10, "the claude child to start")
    other = str(uuid.uuid4())
    reg = w.relay.register(other)
    leased = target.rsplit("/", 1)[1]
    error = refused("registration_conflict", lambda: w.relay.channel(
        leased, "heartbeat", {**w.relay.creds(reg, leased), "previous_session_id": other}))
    assert error.details["why"] == "resume_running"
    assert w.mailbox.get_message(message["id"])["state"] == "delivered"  # still the run's
    assert w.relay.channel(other, "heartbeat", w.relay.creds(reg, other))["session_changed"] is False


def test_a_running_lease_is_renewed_so_the_sweep_leaves_it_alone(box, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    runner = R.ResumeRunner(box, lease_s=1.0, renew_s=0.25, claude_argv=[sys.executable, str(FAKE_CLAUDE)],
                            term_grace_s=0.5)
    message_id, lease_id = claim(box, runner, body="MODE=hang")
    thread = runner.start(R.ResumeJob(lease_id, message_id, f"{MACHINE}/claude/x", "claude", "x", str(tmp_path),
                                      "[relay message]\n\nMODE=hang", 3.0))
    wait_for(lambda: (box.leases() or [{}])[0].get("phase") == "running", 10, "the run to record its child")
    time.sleep(1.5)  # past the first expiry: only renewal keeps the lease
    assert runner.recover() == 0
    thread.join(15)
    assert ended(box, message_id)["reply"]["body"].startswith("timeout")


def test_a_live_runner_of_the_dead_relay_writes_nothing_after_recovery(box, tmp_path, monkeypatch):
    """Restart with the old runner still alive: the new relay stops its child; the old runner then gets LeaseLost."""
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    old = R.ResumeRunner(box, claude_argv=[sys.executable, str(FAKE_CLAUDE)])
    new = R.ResumeRunner(box, term_grace_s=0.5)
    cwd = str(tmp_path)
    message_id, lease_id = claim(box, old, body="MODE=hang")
    thread = old.start(R.ResumeJob(lease_id, message_id, f"{MACHINE}/claude/x", "claude", "x", cwd,
                                   "[relay message]\n\nMODE=hang", 60.0))
    wait_for(lambda: (box.leases() or [{}])[0].get("phase") == "running", 10, "the run to record its child")
    stop_children = new._stop_children

    def stop_then_let_the_old_runner_write_first(lease):
        stopped = stop_children(lease)
        thread.join(15)  # the old runner sees its child die and tries its final write before recovery's
        return stopped

    monkeypatch.setattr(new, "_stop_children", stop_then_let_the_old_runner_write_first)
    assert new.recover() == 1
    assert not thread.is_alive()
    record = ended(box, message_id)
    assert record["reply"]["body"] == R.REASON_STOPPED  # the late runner wrote nothing over it
    rows = [r for r in box.audit_rows(message_id=message_id, limit=50) if r["to_state"] in ("answered", "declined")]
    assert len(rows) == 1


def test_lease_expiry_while_the_relay_lives_is_recovered_and_the_hung_runner_writes_nothing(box, tmp_path, monkeypatch):
    """A run hangs past its deadline: renewal stops, the lease expires, the sweep recovers it; the late runner
    then returns an answer, which the fence refuses."""
    runner = R.ResumeRunner(box, lease_s=1.0, renew_s=0.25, term_grace_s=0.5)
    release = threading.Event()
    child = sleeper()

    def hung_run(active, deadline):
        runner._record(active, child.pid, child.pid)
        release.wait(30)  # ignores its own deadline: a hung run
        return "answered", "LATE-ANSWER"

    monkeypatch.setattr(runner, "_run_claude", hung_run)
    message_id, lease_id = claim(box, runner)
    try:
        thread = runner.start(R.ResumeJob(lease_id, message_id, f"{MACHINE}/claude/x", "claude", "x", str(tmp_path),
                                          "prompt", 0.5))
        wait_for(lambda: (box.leases() or [{}])[0].get("phase") == "running", 10, "the run to record its child")
        assert runner.recover() == 0  # owned and renewed
        wait_for(lambda: runner.recover() == 1, 10, "the expired lease to be recovered")
        assert child.wait(10) is not None  # its child was stopped
        release.set()
        thread.join(15)
        record = ended(box, message_id)
        assert record["reply"]["body"] == R.REASON_EXPIRED and "LATE" not in json.dumps(record)
    finally:
        release.set()
        child.kill()


def test_relay_start_recovers_a_lease_left_by_a_dead_relay(config, roots, backend, tmp_path):
    config.config_path.write_text(ON, encoding="utf-8")
    state = config.paths.root
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    old = M.Mailbox(M.mailbox_db_path(config.paths), machine=MACHINE)
    message_id, _ = claim(old, R.ResumeRunner(old))
    server = make_server(config, roots, backend, tmp_path).start()
    try:
        record = server.mailbox.get_message(message_id)
        assert record["state"] == "declined" and record["reply"]["body"] == R.REASON_BEFORE_START
        later, _ = claim(server.mailbox, R.ResumeRunner(server.mailbox))  # another relay instance's lease
        server.service.maintain()  # the periodic sweep recovers it too
        assert server.mailbox.get_message(later)["state"] == "declined"
        assert server.mailbox.leases() == [] and server.mailbox.claim_problems() == []
    finally:
        server.stop()


# --------------------------------------------------------------------------
# mailbox fences and invariants
# --------------------------------------------------------------------------

def test_lease_writes_are_fenced_and_claims_end_only_through_finish_claim(box):
    runner = R.ResumeRunner(box)
    message_id, lease_id = claim(box, runner)
    for write in (lambda: box.renew_lease("lse_other", 90), lambda: box.set_lease_phase("lse_other", "spawning"),
                  lambda: box.finish_claim("lse_other", "answered", "x")):
        with pytest.raises(M.LeaseLost):
            write()
    target = box.get_message(message_id)["to"]
    assert box.end_session(target) == 0  # a claimed message never becomes session-ended
    box._clock = lambda: time.time() + 2 * P.MAX_EXPIRES_IN_S
    assert box.expire() == 0  # nor lapses
    box._clock = time.time
    box.finish_claim(lease_id, "answered", "done")
    with pytest.raises(M.LeaseLost):
        box.finish_claim(lease_id, "declined", "again")  # a second end writes nothing
    assert ended(box, message_id)["reply"]["body"] == "done"


def test_claim_message_claims_exactly_one_queued_id(box):
    a = box.send_message(origin="claude.ai/owner", to=f"{MACHINE}/codex/t1", kind="note", body="a")["message"]
    b = box.send_message(origin="claude.ai/owner", to=f"{MACHINE}/codex/t2", kind="note", body="b")["message"]
    box.claim_message(a["id"], lease_id="lse_a", relay_instance="rin_x", lease_s=90)
    assert box.get_message(a["id"])["state"] == "delivered" and box.get_message(b["id"])["state"] == "queued"
    with pytest.raises(P.RelayError) as caught:
        box.claim_message(a["id"], lease_id="lse_b", relay_instance="rin_x", lease_s=90)
    assert caught.value.code == "invalid_transition"
    assert box.claim_problems() == []


# --------------------------------------------------------------------------
# forwarding and the hub
# --------------------------------------------------------------------------

class _Peer:
    machine = "tower"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, verb, params, *, origin, grant=None, on_behalf_of=None):
        self.calls.append(verb)
        return {"protocol": P.PROTOCOL, "machine": "tower", "now": time.time(),
                "pause": {"relay": {"paused": False, "paused_since": None, "reason": None},
                          "hub": {"paused": False, "paused_since": None, "reason": None}},
                "forwarding": {"receive": True}}


def test_a_direct_continue_naming_a_peer_is_never_forwarded(config, roots, backend, tmp_path):
    config.config_path.write_text(ON, encoding="utf-8")
    peer = _Peer()
    server = make_server(config, roots, backend, tmp_path, peer_clients={"tower": peer}).start()
    try:
        relay = Relay(server, roots)
        target = f"tower/codex/{uuid.uuid4()}"
        relay.mint("continue_session", [target])
        with pytest.raises(P.RelayError) as caught:
            relay.direct("continue_session", {"to": target, "kind": "note", "body": "x"})
        assert caught.value.code == "target_invalid" and caught.value.details["why"] == "not_forwarded"
        assert peer.calls == []
    finally:
        server.stop()


def test_the_hub_routes_continue_session_to_the_one_relay_its_address_names(tmp_path):
    from test_relay_peers import FakeRelay, example, hub_call

    calls: list[tuple[str, str]] = []

    class Recording(FakeRelay):
        def __getattr__(self, verb: str):
            inner = super().__getattr__(verb)

            def call(params: Any, *, origin: str, grant: Any = None) -> Any:
                calls.append((self.machine, verb))
                return inner(params, origin=origin, grant=grant)
            return call

    here = Recording("m5pro", {"continue_session": example("continue_session")})
    there = Recording("tower", {"continue_session": example("continue_session")})
    result = hub_call(tmp_path, [here, there], "continue_session",
                      {"to": f"m5pro/codex/{uuid.uuid4()}", "kind": "note", "body": "hi"})
    assert result["message"]["to"].startswith("m5pro/")
    assert calls == [("m5pro", "continue_session")]


def test_the_cli_client_continue_verb_builds_the_params():
    from llm_kit.cli import relay as relay_cli

    args = relay_cli._parser().parse_args(["client", "continue", f"{MACHINE}/codex/t1", "--kind", "question",
                                           "--body", "hi", "--id", "msg_x1", "--session", DIRECT])
    assert relay_cli._client_params(args, MACHINE) == ("continue_session", {
        "to": f"{MACHINE}/codex/t1", "kind": "question", "body": "hi", "id": "msg_x1"})
