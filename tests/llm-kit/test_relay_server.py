"""Tests for llm_kit.relay.server, .service and .client (unit S3).

Every test serves a real relay (uvicorn on an ephemeral 127.0.0.1 port) over a
tmp state directory and tmp harness homes, and talks to it through the real
HTTP client (or raw httpx for transport-level refusals). Nothing reads or
writes the real ~/.claude, ~/.codex or ~/.local/state. The LLM backend is a
test double; channel registrations name this test process (or a sleeping
child) as their harness pid, because liveness is real.
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import httpx
import psutil
import pytest

from llm_kit.completion.types import LLMResponse
from llm_kit.registry import consent
from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import lifecycle as L
from llm_kit.relay import protocol as P
from llm_kit.relay import server as S
from llm_kit.relay import service as SV
from llm_kit.relay.client import HttpRelayClient

MACHINE = "m5pro"
ENV_JSON = {"machines": {"m5pro.local": {"os": "macos", "tailscale_hostname": "m5pro"}}}
DIRECT = "d1d1d1d1-0000-4000-8000-000000000001"
OTHER = "d2d2d2d2-0000-4000-8000-000000000002"
ENDPOINT = f"{MACHINE}/llm/qwen38-m5-32k-local"
NONCE = "nonce-" + "x" * 20


def sid() -> str:
    return str(uuid.uuid4())


def addr(session: str) -> str:
    return f"{MACHINE}/claude/{session}"


# --------------------------------------------------------------------------
# doubles and fixtures
# --------------------------------------------------------------------------

class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, system, user, *, model, options=None):
        self.calls.append(user)
        return LLMResponse(text=f"echo: {user}", model=model, finish_reason="stop", input_tokens=5, output_tokens=2)

    def classify_halt(self, exc):  # pragma: no cover - protocol member
        return None


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


@pytest.fixture
def config(tmp_path: Path) -> C.RelayConfig:
    env_json = tmp_path / "env.json"
    env_json.write_text(json.dumps(ENV_JSON), encoding="utf-8")
    return C.load_config(env_json=env_json, environ={}, hostname=lambda: "m5pro.local",
                         config_path=tmp_path / "config" / "relay.yaml", state_root=tmp_path / "state")


@pytest.fixture
def roots(tmp_path: Path) -> D.DiscoveryRoots:
    claude, codex = tmp_path / "claude-home", tmp_path / "codex-home"
    (claude / "sessions").mkdir(parents=True)
    (claude / "projects" / "-tmp-proj").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)
    return D.DiscoveryRoots(claude, codex)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


def make_server(config, roots, backend, tmp_path, **kw) -> S.RelayServer:
    kw.setdefault("poll_interval_s", 0.05)
    kw.setdefault("maintain_interval_s", 3600.0)
    return S.RelayServer(
        config, port=0, roots=roots, probe=D.PsutilProbe(),
        backend_factory=lambda entry: SimpleNamespace(backend=backend, model=entry.model),
        policy_loader=lambda scope: consent.policy(scope, path=tmp_path / "consent.json"),
        **kw,
    )


class Relay:
    """The running server plus callers for each origin kind."""

    def __init__(self, server: S.RelayServer, roots: D.DiscoveryRoots) -> None:
        self.server = server
        self.roots = roots
        self.client = HttpRelayClient(server.url, machine=MACHINE, relay_token=server.prepared.relay_token,
                                      hub_bearer=server.prepared.hub_bearer)

    @property
    def mailbox(self):
        return self.server.mailbox

    def direct(self, verb: str, params: dict, *, session: str = DIRECT, grant: Optional[str] = None) -> dict:
        return self.client.call(verb, params, origin=f"direct:{addr(session)}", grant=grant)

    def hub(self, verb: str, params: dict) -> dict:
        return self.client.call(verb, params, origin="claude.ai/owner@example.com")

    def operator(self, verb: str, params: dict) -> dict:
        return self.client.call(verb, params, origin=f"operator:{MACHINE}")

    def channel(self, session: str, verb: str, params: dict) -> dict:
        return self.client.call(verb, params, origin=f"channel:{addr(session)}")

    def mint(self, verb: str, targets=(), *, uses: int = 1, session: str = DIRECT, ttl: int = 120) -> dict:
        return self.direct("grant_mint", {
            "session": addr(session), "verb": verb, "targets": list(targets), "expires_in_s": ttl,
            "uses": uses, "origin_proof": {"type": "none"}, "command": f"/direct {verb}",
        }, session=session)["grant"]

    def register(self, session: str, *, pid: Optional[int] = None, accepts=("note", "instruction", "question"),
                 transcript: Optional[Path] = None, task: Optional[str] = None) -> dict:
        params: dict[str, Any] = {
            "session_id": session, "harness": "claude", "pid": pid or os.getpid(), "cwd": "/Users/u/Dev/app",
            "version": "2.1.316", "accepts": list(accepts), "channel_nonce": NONCE,
        }
        if transcript is not None:
            params["transcript_path"] = str(transcript)
        if task is not None:
            params["task"] = task
        return self.channel(session, "register", params)

    def creds(self, registration: dict, session: str) -> dict:
        return {"registration_id": registration["registration_id"], "session_id": session, "channel_nonce": NONCE}

    def send(self, to: str, *, kind: str = "instruction", body: str = "run the tests", grant_uses: int = 1) -> dict:
        self.mint("send_message", [to], uses=grant_uses)
        return self.direct("send_message", {"to": to, "kind": kind, "body": body})["message"]

    def refused(self, code: str, call, *args, **kwargs) -> P.RelayError:
        with pytest.raises(P.RelayError) as caught:
            call(*args, **kwargs)
        assert caught.value.code == code, caught.value
        return caught.value


@pytest.fixture
def relay(config, roots, backend, tmp_path) -> Iterator[Relay]:
    server = make_server(config, roots, backend, tmp_path).start()
    try:
        yield Relay(server, roots)
    finally:
        server.stop()


def write_transcript(roots: D.DiscoveryRoots, session: str, texts: list[str], *, mtime: Optional[float] = None) -> Path:
    path = roots.claude_projects / "-tmp-proj" / f"{session}.jsonl"
    lines = []
    for i, text in enumerate(texts):
        if i % 2 == 0:
            lines.append({"type": "user", "message": {"role": "user", "content": text},
                          "timestamp": "2026-11-03T10:00:00.000Z", "cwd": "/Users/u/Dev/app"})
        else:
            lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                          "timestamp": "2026-11-03T10:00:01.000Z"})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_session_file(roots: D.DiscoveryRoots, pid: int, session: str) -> None:
    start = psutil.Process(pid).create_time()
    (roots.claude_sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session, "cwd": "/Users/u/Dev/other", "startedAt": int(start * 1000),
    }), encoding="utf-8")


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def raw_post(relay: Relay, path: str, body: Any, *, token: Optional[str] = "", content_type: str = "application/json",
             method: str = "POST", headers: Optional[dict] = None) -> httpx.Response:
    sent = dict(headers or {})
    sent["Content-Type"] = content_type
    if token is not None:
        sent[P.HEADER_RELAY_TOKEN] = token or relay.server.prepared.relay_token
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    with httpx.Client(trust_env=False) as http:
        return http.request(method, relay.server.url + path, content=data, headers=sent)


def envelope(verb: str, params: dict, origin: str = f"operator:{MACHINE}", **extra: Any) -> dict:
    return {"protocol": P.PROTOCOL, "id": "req-1", "verb": verb, "origin": origin, "params": params, **extra}


# --------------------------------------------------------------------------
# every read verb, through the real client
# --------------------------------------------------------------------------

def test_every_read_verb_answers_through_the_real_client(relay, roots, backend):
    channel = sid()
    transcript = write_transcript(roots, channel, ["What is the plan?", "Ship the relay."])
    relay.register(channel, transcript=transcript, task="plugins-kit/harness-messaging")

    relay.mint("list_machines")
    machines = relay.direct("list_machines", {})["machines"]
    assert [m["machine"] for m in machines] == [MACHINE]
    assert machines[0]["status"] == "up" and machines[0]["protocol"] == P.PROTOCOL
    assert sorted(machines[0]["endpoints"]) == [f"{MACHINE}/llm/qwen36-m5-local", ENDPOINT]

    relay.mint("list_sessions")
    listed = relay.direct("list_sessions", {})
    session = next(s for s in listed["sessions"] if s["session_id"] == channel)
    assert session["state"] == "live-accepting" and session["registered"] is True
    assert session["task"] == "plugins-kit/harness-messaging"
    assert listed["machines"] == [{"machine": MACHINE, "status": "up"}]

    relay.mint("read_session", [addr(channel)])
    tail = relay.direct("read_session", {"address": addr(channel)})
    assert tail["address"] == addr(channel)
    assert [t["text"] for t in tail["turns"]] == ["What is the plan?", "Ship the relay."]

    relay.mint("get_replies", [addr(channel)])
    assert relay.direct("get_replies", {"address": addr(channel)}) == {"messages": []}

    relay.mint("ask_llm", [ENDPOINT])
    answer = relay.direct("ask_llm", {"endpoint": ENDPOINT, "prompt": "ping"})
    assert answer["text"] == "echo: ping" and answer["endpoint"] == ENDPOINT
    assert backend.calls == ["ping"]

    status = relay.operator("status", {})
    assert status["protocol"] == P.PROTOCOL and status["machine"] == MACHINE and status["registrations"] == 1
    assert relay.direct("status", {})["machine"] == MACHINE  # status needs no grant

    other = relay.hub("list_sessions", {"machine": "5090w"})
    assert other["sessions"] == [] and other["machines"][0]["status"] == "unreachable"
    assert other["machines"][0]["error"]["code"] == "machine_unreachable"


def test_a_selector_is_resolved_at_mint_and_at_the_call(relay, roots):
    channel = sid()
    relay.register(channel, transcript=write_transcript(roots, channel, ["hello"]))
    grant = relay.mint("read_session", ["latest"])
    assert grant["targets"] == [addr(channel)]
    assert relay.direct("read_session", {"address": "latest"})["address"] == addr(channel)


# --------------------------------------------------------------------------
# grants
# --------------------------------------------------------------------------

def test_a_direct_read_without_a_grant_is_refused(relay, roots, backend):
    relay.refused("grant_required", relay.direct, "list_sessions", {})
    relay.refused("grant_required", relay.direct, "list_machines", {})
    relay.refused("grant_required", relay.direct, "ask_llm", {"endpoint": ENDPOINT, "prompt": "x"})
    assert backend.calls == []  # refused before the effect, not after it
    relay.mint("read_session", [addr(OTHER)])
    relay.refused("grant_required", relay.direct, "read_session", {"address": addr(sid())})
    named = relay.mint("read_session", [addr(OTHER)])
    relay.refused("grant_wrong_target", relay.direct, "read_session", {"address": addr(sid())}, grant=named["grant_id"])
    relay.refused("grant_wrong_verb", relay.direct, "list_sessions", {}, grant=named["grant_id"])
    relay.refused("grant_wrong_session", relay.direct, "read_session", {"address": addr(OTHER)},
                  session=OTHER, grant=named["grant_id"])
    relay.refused("grant_unknown", relay.direct, "list_sessions", {}, grant="grt_nonesuch")


def test_a_grant_use_is_consumed_only_by_a_call_that_returns_ok(relay, roots):
    ghost = addr(sid())  # granted, but no such session: the call fails after the grant stage
    failing = relay.mint("read_session", [ghost])
    relay.refused("target_not_found", relay.direct, "read_session", {"address": ghost})
    assert relay.mailbox.get_grant(failing["grant_id"])["uses_remaining"] == 1

    channel = sid()
    relay.register(channel, transcript=write_transcript(roots, channel, ["hi"]))
    grant = relay.mint("read_session", [addr(channel)], uses=1)
    check = relay.direct("grant_check", {"session": addr(DIRECT), "verb": "read_session", "target": addr(channel)})
    assert check == {"covered": True, "grant_id": grant["grant_id"], "code": None}
    assert relay.mailbox.get_grant(grant["grant_id"])["uses_remaining"] == 1  # grant_check never consumes

    relay.direct("read_session", {"address": addr(channel)})
    assert relay.mailbox.get_grant(grant["grant_id"])["state"] == "used-up"
    relay.refused("grant_required", relay.direct, "read_session", {"address": addr(channel)})
    relay.refused("grant_used_up", relay.direct, "read_session", {"address": addr(channel)}, grant=grant["grant_id"])


def test_grant_mint_is_direct_only_for_its_own_session(relay):
    params = {"session": addr(OTHER), "verb": "list_sessions", "targets": [], "expires_in_s": 60, "uses": 1,
              "origin_proof": {"type": "none"}}
    relay.refused("origin_not_allowed", relay.direct, "grant_mint", params)  # minting for another session
    relay.refused("origin_not_allowed", relay.hub, "grant_mint", dict(params, session=addr(DIRECT)))
    relay.refused("origin_not_allowed", relay.operator, "grant_mint", dict(params, session=addr(DIRECT)))
    enclave = dict(params, session=addr(DIRECT), origin_proof={"type": "enclave-signature", "signature": "c2ln"})
    relay.refused("proof_unsupported", relay.direct, "grant_mint", enclave)
    relay.refused("bad_request", relay.direct, "grant_mint", dict(params, session=addr(DIRECT), expires_in_s=601))


def test_hub_reads_need_the_bearer_and_no_grant_and_hub_writes_are_refused(relay, roots):
    channel = sid()
    relay.register(channel)
    assert any(s["session_id"] == channel for s in relay.hub("list_sessions", {})["sessions"])
    no_bearer = HttpRelayClient(relay.server.url, machine=MACHINE, relay_token=relay.server.prepared.relay_token)
    relay.refused("unauthenticated", no_bearer.call, "list_sessions", {}, origin="claude.ai/owner@example.com")
    wrong = HttpRelayClient(relay.server.url, machine=MACHINE, relay_token=relay.server.prepared.relay_token,
                            hub_bearer="not-the-bearer")
    relay.refused("unauthenticated", wrong.call, "status", {}, origin="claude.ai/owner@example.com")
    relay.refused("grant_required", relay.hub, "send_message", {"to": addr(channel), "kind": "note", "body": "hi"})
    assert relay.mailbox.messages_to(addr(channel)) == []


def test_the_relay_token_is_required_on_every_origin(relay):
    bad = HttpRelayClient(relay.server.url, machine=MACHINE, relay_token="not-the-token",
                          hub_bearer=relay.server.prepared.hub_bearer)
    for origin in (f"operator:{MACHINE}", f"direct:{addr(DIRECT)}", "claude.ai/owner@example.com",
                   f"channel:{addr(DIRECT)}"):
        relay.refused("unauthenticated", bad.call, "status", {}, origin=origin)
    response = raw_post(relay, "/v1/status", envelope("status", {}), token=None)
    assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated"


# --------------------------------------------------------------------------
# messaging and the channel verbs
# --------------------------------------------------------------------------

def test_send_poll_ack_reply_and_get_replies(relay):
    channel = sid()
    registration = relay.register(channel)
    assert registration["address"] == addr(channel) and registration["heartbeat_s"] == 30
    assert registration["lease_s"] == 90 and registration["ack_timeout_s"] == 60 and registration["max_wait_s"] == 25
    message = relay.send(addr(channel), kind="question", body="Are the tests green?")
    assert message["state"] == "queued" and message["sender"] == f"direct:{addr(DIRECT)}"

    creds = relay.creds(registration, channel)
    polled = relay.channel(channel, "poll", dict(creds, wait_s=0))
    assert [m["id"] for m in polled["messages"]] == [message["id"]] and polled["paused"] is False
    assert relay.channel(channel, "ack", dict(creds, message_id=message["id"])) == {"message_id": message["id"], "state": "acked"}
    replied = relay.channel(channel, "reply", dict(creds, in_reply_to=message["id"], state="answered", body="Yes."))
    assert replied["message_state"] == "answered" and replied["replayed"] is False

    relay.mint("get_replies", [message["id"]])
    records = relay.direct("get_replies", {"message_id": message["id"]})["messages"]
    assert records[0]["state"] == "answered" and records[0]["reply"]["body"] == "Yes."


def test_a_long_poll_wakes_when_a_message_arrives(relay):
    channel = sid()
    registration = relay.register(channel)
    relay.mint("send_message", [addr(channel)])
    threading.Timer(0.3, lambda: relay.direct("send_message", {"to": addr(channel), "kind": "note", "body": "now"})).start()
    started = time.monotonic()
    polled = relay.channel(channel, "poll", dict(relay.creds(registration, channel), wait_s=10))
    assert len(polled["messages"]) == 1 and time.monotonic() - started < 5


def test_send_refusals_come_from_registration_state(relay, roots, sleeper):
    read_only = sid()
    write_session_file(roots, sleeper.pid, read_only)  # live, scanned, never registered
    relay.mint("send_message", [addr(read_only)])
    err = relay.refused("not_opted_in", relay.direct, "send_message", {"to": addr(read_only), "kind": "note", "body": "x"})
    assert relay.mailbox.get_message(err.details["message_id"])["state"] == "refused"

    idle = sid()
    write_transcript(roots, idle, ["old work"], mtime=time.time() - 3600)  # process gone, transcript recent
    relay.mint("send_message", [addr(idle)])
    relay.refused("session_ended", relay.direct, "send_message", {"to": addr(idle), "kind": "note", "body": "x"})

    notes_only = sid()
    relay.register(notes_only, accepts=("note",))
    relay.mint("send_message", [addr(notes_only)])
    relay.refused("not_opted_in", relay.direct, "send_message", {"to": addr(notes_only), "kind": "instruction", "body": "x"})

    unknown = addr(sid())
    relay.mint("send_message", [unknown])
    relay.refused("target_not_found", relay.direct, "send_message", {"to": unknown, "kind": "note", "body": "x"})
    assert relay.mailbox.messages_to(unknown) == []


# --------------------------------------------------------------------------
# pause
# --------------------------------------------------------------------------

def test_relay_pause_refuses_writes_and_empties_poll_until_resume(relay):
    channel = sid()
    registration = relay.register(channel)
    queued = relay.send(addr(channel), body="before the pause")
    creds = relay.creds(registration, channel)

    paused = relay.operator("pause", {"scope": "relay", "reason": "owner away"})["pause"]["relay"]
    assert paused["paused"] is True and paused["reason"] == "owner away"
    grant = relay.mint("send_message", [addr(channel)])
    err = relay.refused("paused", relay.direct, "send_message", {"to": addr(channel), "kind": "note", "body": "x"})
    assert err.details["scope"] == "relay" and err.retryable is True
    assert relay.mailbox.get_grant(grant["grant_id"])["uses_remaining"] == 1

    started = time.monotonic()
    polled = relay.channel(channel, "poll", dict(creds, wait_s=10))
    assert polled == {"messages": [], "paused": True, "lease_expires_at": polled["lease_expires_at"]}
    assert time.monotonic() - started < 3
    assert relay.mailbox.get_message(queued["id"])["state"] == "queued"
    assert relay.operator("status", {})["pause"]["relay"]["paused"] is True  # reads continue

    relay.operator("resume", {"scope": "relay"})
    polled = relay.channel(channel, "poll", dict(creds, wait_s=0))
    assert [m["id"] for m in polled["messages"]] == [queued["id"]]


def test_hub_pause_refuses_every_hub_call_and_not_direct(relay):
    relay.operator("pause", {"scope": "hub"})
    relay.refused("paused", relay.hub, "list_sessions", {})
    relay.refused("paused", relay.hub, "status", {})
    relay.mint("list_sessions")
    assert relay.direct("list_sessions", {})["machines"][0]["status"] == "up"
    relay.operator("resume", {"scope": "hub"})
    relay.hub("list_sessions", {})


# --------------------------------------------------------------------------
# the registration end listener
# --------------------------------------------------------------------------

def test_a_session_change_ends_open_messages_to_the_old_address(relay):
    old, new = sid(), sid()
    registration = relay.register(old)
    message = relay.send(addr(old))
    beat = relay.channel(new, "heartbeat", dict(relay.creds(registration, new), previous_session_id=old))
    assert beat["session_changed"] is True and beat["address"] == addr(new) and beat["ended_messages"] == 1
    record = relay.mailbox.get_message(message["id"])
    assert record["state"] == "session-ended" and record["state_reason"] == "session_changed"
    relay.mint("send_message", [addr(old)])
    relay.refused("session_ended", relay.direct, "send_message", {"to": addr(old), "kind": "note", "body": "x"})


def test_deregister_and_a_lapsed_lease_end_open_messages(config, roots, backend, tmp_path):
    clock = Clock()
    server = make_server(config, roots, backend, tmp_path, clock=clock).start()
    try:
        relay = Relay(server, roots)
        first = sid()
        registration = relay.register(first)
        message = relay.send(addr(first))
        gone = relay.channel(first, "deregister", relay.creds(registration, first))
        assert gone["ended_messages"] == 1
        assert relay.mailbox.get_message(message["id"])["state_reason"] == "deregistered"

        second = sid()
        relay.register(second)
        lapsing = relay.send(addr(second))
        clock.offset = P.REGISTRATION_LEASE_S + 1
        server.service.maintain()
        record = relay.mailbox.get_message(lapsing["id"])
        assert record["state"] == "session-ended" and record["state_reason"] == "lease_lapsed"
        assert relay.operator("status", {})["registrations"] == 0
    finally:
        server.stop()


# --------------------------------------------------------------------------
# lifecycle: loopback bind, singleton
# --------------------------------------------------------------------------

# 192.0.2.1 is TEST-NET-1 (RFC 5737): never this machine's address, so a guard
# removed for a fail demonstration cannot expose anything; the others never bind.
@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::"])
def test_a_non_loopback_bind_is_refused_before_any_state_exists(config, roots, backend, tmp_path, host):
    exposed = dataclasses.replace(config, relay_host=host)
    with pytest.raises(C.RelayConfigError, match="not a loopback address"):
        make_server(exposed, roots, backend, tmp_path).start()
    assert not config.paths.root.exists()
    with pytest.raises(C.RelayConfigError, match="not a loopback address"):
        S.bind_loopback(host, 0)


def test_a_second_serve_is_refused_already_running(relay, config, roots, backend, tmp_path):
    with pytest.raises(L.SingletonHeld) as caught:
        make_server(config, roots, backend, tmp_path).start()
    assert caught.value.code == "already_running" and caught.value.http_status == 409
    assert relay.operator("status", {})["machine"] == MACHINE  # the first relay keeps serving


def test_the_lock_is_released_on_stop(config, roots, backend, tmp_path):
    make_server(config, roots, backend, tmp_path).start().stop()
    server = make_server(config, roots, backend, tmp_path).start()
    server.stop()


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def test_every_request_with_a_known_verb_and_origin_writes_a_call_row(relay):
    relay.refused("grant_required", relay.direct, "list_sessions", {})
    grant = relay.mint("list_sessions")
    relay.direct("list_sessions", {})
    relay.hub("status", {})
    raw_post(relay, "/v1/status", b"{not json")  # no parseable origin: no row
    calls = [r for r in relay.mailbox.audit_rows(limit=100) if r["event"] == "call"]
    summary = [(r["origin"], r["verb"], r["outcome"], r["code"], r["grant_id"]) for r in calls]
    assert summary == [
        (f"direct:{addr(DIRECT)}", "list_sessions", "refused", "grant_required", None),
        (f"direct:{addr(DIRECT)}", "grant_mint", "ok", None, None),
        (f"direct:{addr(DIRECT)}", "list_sessions", "ok", None, grant["grant_id"]),
        ("claude.ai/owner@example.com", "status", "ok", None, None),
    ]
    assert all(r["ingress"] == "relay" and r["machine"] == MACHINE and r["request_id"] for r in calls)


# --------------------------------------------------------------------------
# envelope, transport and origin refusals
# --------------------------------------------------------------------------

def test_envelope_and_transport_refusals(relay):
    def error(response: httpx.Response) -> tuple[int, str]:
        return response.status_code, response.json()["error"]["code"]

    bad_json = raw_post(relay, "/v1/status", b"{not json")
    assert error(bad_json) == (400, "bad_request") and bad_json.json()["id"] is None
    assert error(raw_post(relay, "/v1/status", envelope("status", {}, protocol="relay-protocol/2"))) == (400, "unsupported_protocol")
    assert error(raw_post(relay, "/v1/nonesuch", envelope("status", {}))) == (404, "unknown_verb")
    assert error(raw_post(relay, "/v2/status", envelope("status", {}))) == (404, "unknown_verb")
    assert error(raw_post(relay, "/v1/pause", envelope("status", {}))) == (400, "bad_request")
    assert error(raw_post(relay, "/v1/status", envelope("status", {}), method="PUT")) == (400, "bad_request")
    assert error(raw_post(relay, "/v1/status", envelope("status", {}), content_type="text/plain")) == (400, "bad_request")
    assert error(raw_post(relay, "/v1/status", b"x" * (P.MAX_REQUEST_BYTES + 1))) == (413, "too_large")
    assert error(raw_post(relay, "/v1/status", envelope("status", {"extra": 1}))) == (400, "bad_request")
    assert error(raw_post(relay, "/v1/status", envelope("status", {}, origin="nobody"))) == (400, "bad_request")
    for verb in ("list_questions", "answer_question"):
        response = raw_post(relay, f"/v1/{verb}", envelope(verb, {}, origin=f"direct:{addr(DIRECT)}"))
        assert error(response) == (404, "unknown_verb") and response.json()["error"]["details"]["not_implemented"] is True
    ok = raw_post(relay, "/v1/status", envelope("status", {}))
    assert ok.status_code == 200 and ok.json()["id"] == "req-1" and ok.json()["ok"] is True


def test_origin_rules(relay):
    relay.refused("origin_not_allowed", relay.channel, DIRECT, "list_sessions", {})
    relay.refused("origin_not_allowed", relay.operator, "read_session", {"address": addr(DIRECT)})
    relay.refused("origin_not_allowed", relay.direct, "pause", {"scope": "relay"})
    relay.refused("origin_not_allowed", relay.client.call, "status", {}, origin="operator:5090w")
    relay.refused("origin_not_allowed", relay.client.call, "list_sessions", {}, origin=f"direct:5090w/claude/{DIRECT}")
    session = sid()
    params = {"session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/x", "version": "1",
              "accepts": [], "channel_nonce": NONCE}
    relay.refused("origin_not_allowed", relay.channel, sid(), "register", params)  # origin names another session


def test_a_result_that_fails_its_schema_is_an_internal_error(relay, monkeypatch):
    monkeypatch.setitem(relay.server.service._handlers, "status", lambda *args: {"protocol": P.PROTOCOL})
    err = relay.refused("internal", relay.operator, "status", {})
    assert "fails its schema" in err.message


def test_the_client_refuses_a_non_loopback_url_and_reports_an_unreachable_relay(relay):
    with pytest.raises(ValueError, match="not a loopback"):
        HttpRelayClient("http://192.168.1.10:4710", machine=MACHINE, relay_token="t")
    with pytest.raises(ValueError, match="not a loopback"):
        HttpRelayClient("http://localhost:4710", machine=MACHINE, relay_token="t")
    host, port = relay.server.address
    relay.server.stop()
    dead = HttpRelayClient(f"http://{host}:{port}", machine=MACHINE, relay_token="t")
    err = relay.refused("machine_unreachable", dead.status, {}, origin=f"operator:{MACHINE}")
    assert err.retryable is True


def test_the_http_client_satisfies_the_relay_client_protocol(relay):
    assert isinstance(relay.client, P.RelayClient)
    assert relay.client.machine == MACHINE


# --------------------------------------------------------------------------
# security read: grant reservation, refund rule, pre-body auth, bounds, failed-auth audit
# --------------------------------------------------------------------------

class GateBackend(FakeBackend):
    """Holds every completion until a second one arrives or ``hold_s`` passes."""

    def __init__(self, hold_s: float = 0.6, need: int = 2) -> None:
        super().__init__()
        self.hold_s, self.need = hold_s, need
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.fail: Optional[BaseException] = None

    def complete(self, system, user, *, model, options=None):
        with self.lock:
            self.calls.append(user)
            if len(self.calls) >= self.need:
                self.release.set()
        self.release.wait(self.hold_s)
        if self.fail is not None:
            raise self.fail
        return LLMResponse(text=f"echo: {user}", model=model, finish_reason="stop", input_tokens=5, output_tokens=2)


def _served(config, roots, backend, tmp_path, **kw) -> Iterator[Relay]:
    server = make_server(config, roots, backend, tmp_path, **kw).start()
    try:
        yield Relay(server, roots)
    finally:
        server.stop()


def _wait_for(predicate, what: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def test_two_concurrent_ask_llm_calls_cannot_share_one_remaining_grant_use(config, roots, tmp_path):
    backend = GateBackend(need=2)
    gen = _served(config, roots, backend, tmp_path)
    relay = next(gen)
    try:
        grant = relay.mint("ask_llm", [ENDPOINT], uses=1)
        outcomes: list[str] = []

        def call() -> None:
            try:
                relay.direct("ask_llm", {"endpoint": ENDPOINT, "prompt": "ping"}, grant=grant["grant_id"])
                outcomes.append("ok")
            except P.RelayError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(outcomes) == ["grant_used_up", "ok"]
        assert backend.calls == ["ping"], "the operation ran for a call whose grant use was never reserved"
        assert relay.mailbox.get_grant(grant["grant_id"])["state"] == "used-up"
    finally:
        gen.close()


def _connect_refused() -> BaseException:
    try:
        try:
            raise ConnectionRefusedError(61, "Connection refused")
        except OSError as inner:
            raise RuntimeError("Connection error.") from inner
    except RuntimeError as outer:
        return outer


def test_a_reserved_use_is_refunded_only_when_the_operation_failed_before_any_request_was_sent(config, roots, tmp_path):
    backend = GateBackend(hold_s=0.0)
    gen = _served(config, roots, backend, tmp_path)
    relay = next(gen)
    try:
        grant = relay.mint("ask_llm", [ENDPOINT, f"{MACHINE}/llm/nonesuch"], uses=3)
        gid = grant["grant_id"]
        ask = {"endpoint": ENDPOINT, "prompt": "ping"}
        left = lambda: relay.mailbox.get_grant(gid)["uses_remaining"]

        backend.fail = _connect_refused()          # connection refused: no request was sent
        relay.refused("upstream_unavailable", relay.direct, "ask_llm", ask, grant=gid)
        assert left() == 3

        backend.fail = RuntimeError("read timed out")  # the request may have been processed
        relay.refused("upstream_unavailable", relay.direct, "ask_llm", ask, grant=gid)
        assert left() == 2

        backend.fail = None                          # a refusal before the backend exists is free
        relay.refused("endpoint_refused", relay.direct, "ask_llm",
                      {"endpoint": f"{MACHINE}/llm/nonesuch", "prompt": "x"}, grant=gid)
        assert left() == 2
        assert relay.direct("ask_llm", ask, grant=gid)["text"] == "echo: ping" and left() == 1
    finally:
        gen.close()


def _raw_exchange(server: S.RelayServer, head: bytes, *, wait_s: float) -> tuple[bytes, float]:
    """Send ``head`` (no body) to the server and return the raw reply and the seconds it took."""
    host, port = server.address
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=wait_s) as sock:
        sock.settimeout(wait_s)
        sock.sendall(head)
        chunks = b""
        try:
            while b"\r\n\r\n" not in chunks:
                data = sock.recv(4096)
                if not data:
                    break
                chunks += data
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks += data
                if b"}" in data:
                    break
        except socket.timeout:
            pass
    return chunks, time.monotonic() - started


def _head(token: Optional[str], length: int = 1000) -> bytes:
    lines = ["POST /v1/status HTTP/1.1", "Host: 127.0.0.1", "Content-Type: application/json",
             f"Content-Length: {length}", "Connection: close"]
    if token is not None:
        lines.append(f"{P.HEADER_RELAY_TOKEN}: {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def test_the_relay_token_is_checked_before_the_body_is_read(config, roots, backend, tmp_path):
    gen = _served(config, roots, backend, tmp_path, body_deadline_s=30.0)
    relay = next(gen)
    try:
        for token in (None, "not-the-token"):
            reply, took = _raw_exchange(relay.server, _head(token), wait_s=3.0)
            assert reply.startswith(b"HTTP/1.1 401"), reply[:200]
            assert b'"unauthenticated"' in reply and took < 2.5, "the relay waited for a body it had no right to"
        assert [r for r in relay.mailbox.audit_rows(limit=100) if r["code"] == "unauthenticated"] == []
    finally:
        gen.close()


def test_a_body_that_does_not_arrive_in_time_is_refused(config, roots, backend, tmp_path):
    gen = _served(config, roots, backend, tmp_path, body_deadline_s=0.3)
    relay = next(gen)
    try:
        reply, took = _raw_exchange(relay.server, _head(relay.server.prepared.relay_token), wait_s=5.0)
        assert reply.startswith(b"HTTP/1.1 400"), reply[:200]
        assert b'"bad_request"' in reply and took < 4.0
        assert relay.operator("status", {})["machine"] == MACHINE  # the relay still answers
    finally:
        gen.close()


def test_requests_in_flight_are_bounded(config, roots, tmp_path):
    backend = GateBackend(hold_s=10.0, need=99)
    gen = _served(config, roots, backend, tmp_path, max_in_flight=2)
    relay = next(gen)
    try:
        grant = relay.mint("ask_llm", [ENDPOINT], uses=2)
        threads = [threading.Thread(target=lambda: relay.direct(
            "ask_llm", {"endpoint": ENDPOINT, "prompt": "hold"}, grant=grant["grant_id"])) for _ in range(2)]
        for t in threads:
            t.start()
        _wait_for(lambda: len(backend.calls) == 2, "both calls to reach the backend")
        err = relay.refused("rate_limited", relay.operator, "status", {})
        assert err.retryable is True
        backend.release.set()
        for t in threads:
            t.join(30)
        assert relay.operator("status", {})["machine"] == MACHINE  # the slots came back
    finally:
        backend.release.set()
        gen.close()


def _fail_auth(service, origin: str = f"operator:{MACHINE}", verb: str = "status") -> None:
    body = json.dumps(envelope(verb, {}, origin=origin)).encode()
    status, response = service.handle(body, path_verb=verb, relay_token="not-the-token")
    assert status == 401 and response["error"]["code"] == "unauthenticated"


def _unauth_rows(relay: Relay) -> list:
    return [r for r in relay.mailbox.audit_rows(limit=100_000) if r["code"] == "unauthenticated"]


def test_failed_auth_audit_rows_are_coalesced_per_origin_and_window(config, roots, backend, tmp_path):
    clock = Clock()
    gen = _served(config, roots, backend, tmp_path, clock=clock)
    relay = next(gen)
    try:
        service = relay.server.service
        for _ in range(300):
            _fail_auth(service)
        assert len(_unauth_rows(relay)) == 1

        clock.offset += SV.AUTH_AUDIT_WINDOW_S + 1
        service.maintain()                        # the idle relay still closes the window
        rows = _unauth_rows(relay)
        assert len(rows) == 2 and "299" in rows[1]["detail"] and rows[1]["origin"] == f"operator:{MACHINE}"

        _fail_auth(service)                       # a new window records again
        assert len(_unauth_rows(relay)) == 3
    finally:
        gen.close()


def test_failed_auth_audit_rows_are_bounded_across_origins(config, roots, backend, tmp_path):
    clock = Clock()
    gen = _served(config, roots, backend, tmp_path, clock=clock)
    relay = next(gen)
    try:
        service = relay.server.service
        for _ in range(SV.AUTH_AUDIT_MAX_ROWS_PER_WINDOW * 4):
            _fail_auth(service, origin=f"direct:{addr(sid())}", verb="list_sessions")
        assert len(_unauth_rows(relay)) == SV.AUTH_AUDIT_MAX_ROWS_PER_WINDOW
    finally:
        gen.close()


def test_failed_auth_audit_rows_stop_at_a_total_cap_that_survives_a_restart(config, roots, backend, tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "AUTH_AUDIT_MAX_TOTAL_ROWS", 5)
    clock = Clock()
    first = _served(config, roots, backend, tmp_path, clock=clock)
    relay = next(first)
    try:
        for _ in range(4):
            _fail_auth(relay.server.service, origin=f"direct:{addr(sid())}", verb="list_sessions")
        clock.offset += SV.AUTH_AUDIT_WINDOW_S + 1
        for _ in range(4):
            _fail_auth(relay.server.service, origin=f"direct:{addr(sid())}", verb="list_sessions")
        assert len(_unauth_rows(relay)) == 5
    finally:
        first.close()
    second = _served(config, roots, backend, tmp_path, clock=clock)
    relay = next(second)
    try:
        _fail_auth(relay.server.service, origin=f"direct:{addr(sid())}", verb="list_sessions")
        assert len(_unauth_rows(relay)) == 5
    finally:
        second.close()
