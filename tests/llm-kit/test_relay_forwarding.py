"""Tests for relay-to-relay forwarding (amendment 2, katsoft-llm-kit 1.21.0; plan-5090w unit D1).

A source relay (``m5pro``) forwards a ``/direct`` session's calls to a receiving peer (``5090w``) as origin
``relay:m5pro`` with ``on_behalf_of``, authenticated by the peer key the receiver issued (principal ``peer:m5pro``). Most tests run both relays'
``RelayService`` in process: the source's peer client is the real ``HttpRelayClient`` over an
``httpx.MockTransport`` that hands each request to the receiver's service and records it, so failures (a refused
connection, a response lost after the receiver applied the call, a gateway 502) are injected at the transport,
where the client's sent-or-not split runs. One test serves two real relays over loopback HTTP. Nothing binds a
non-loopback address and nothing touches the real ~/.claude, ~/.codex or ~/.local/state.
"""
from __future__ import annotations

import copy
import io
import json
import os
import secrets
import sqlite3
import stat
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

import httpx
import pytest

from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import hub as H
from llm_kit.relay import lifecycle as L
from llm_kit.relay import mailbox as M
from llm_kit.relay import protocol as P
from llm_kit.relay import routing as RT
from llm_kit.relay import server as S
from llm_kit.relay import service as SV
from llm_kit.relay.client import ClientRelayError, HttpRelayClient
from llm_kit.schema import json_schema as JS

from test_relay_peers import FakeRelay, _schema_machine_fields, cli, cli_machines, hub_call, prepare  # noqa: F401
from test_relay_questions import _ledger as question_ledger

SRC, DST, THIRD = "m5pro", "5090w", "laptop"
KEY = "peer-key-" + "k" * 24
THIRD_KEY = "peer-key-" + "t" * 24
DIRECT = "d1d1d1d1-0000-4000-8000-000000000001"
NONCE = "nonce-" + "x" * 20
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay_protocol_1_20"
DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "llm-kit-relay-protocol.md"


def sid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# an in-process relay and a recording wire into it
# --------------------------------------------------------------------------

class FakeLlm:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[dict[str, Any]] = []

    def ask(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        return {"endpoint": params["endpoint"], "text": f"answer from {self.label}"}


class Node:
    """One machine's RelayService over tmp state, harness homes and mailbox."""

    def __init__(self, label: str, tmp: Path, *, peers: Optional[dict[str, Any]] = None, accept_from: tuple = (),
                 accept_keys: Optional[dict[str, str]] = None, ledgers: tuple = ()) -> None:
        self.label = label
        root = tmp / label
        self.paths = C.StatePaths(root / "state")
        L.ensure_state_dir(self.paths)
        self.relay_token = f"relay-token-{label}-" + "r" * 16
        self.hub_bearer = f"hub-bearer-{label}-" + "b" * 16
        self.mailbox = M.Mailbox(root / "mailbox.sqlite3", machine=label)
        claude, codex = root / "claude-home", root / "codex-home"
        (claude / "sessions").mkdir(parents=True)
        (claude / "projects" / "-tmp-proj").mkdir(parents=True)
        (codex / "sessions").mkdir(parents=True)
        self.roots = D.DiscoveryRoots(claude, codex)
        probe = D.PsutilProbe()
        self.table = D.RegistrationTable(label, probe=probe)
        self.discovery = D.Discovery(label, self.roots, table=self.table, probe=probe)
        self.llm = FakeLlm(label)
        self.service = SV.RelayService(
            machine=label, relay_token=self.relay_token, hub_bearer=self.hub_bearer, mailbox=self.mailbox,
            table=self.table, discovery=self.discovery, llm=self.llm, pause=L.PauseStore(self.paths),
            endpoints=("qwen",), ledgers=ledgers, poll_interval_s=0.05, version="1.21.0",
            peers=peers, accept_from=accept_from, accept_keys=accept_keys,
        )
        self.registrations: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.service.close()

    # -- calls ----------------------------------------------------------------

    def handle(self, verb: str, params: dict[str, Any], origin: str, *, grant: Optional[str] = None,
               on_behalf_of: Optional[str] = None, peer_key: Optional[str] = None, hub: bool = False,
               envelope_extra: Optional[dict[str, Any]] = None, token: Optional[bool] = None) -> dict[str, Any]:
        """One call. The relay token is sent unless a peer key is (``token`` overrides: a caller holding both)."""
        envelope = dict(P.make_request(verb, params, origin=origin, request_id="req-" + secrets.token_hex(6),
                                       on_behalf_of=on_behalf_of))
        envelope.update(envelope_extra or {})
        send_token = peer_key is None if token is None else token
        _, response = self.service.handle(envelope, path_verb=verb, relay_token=self.relay_token if send_token else None,
                                          hub_bearer=self.hub_bearer if hub else None, grant=grant, peer_key=peer_key)
        if response["ok"]:
            return response["result"]
        raise P.RelayError.from_error_object(response["error"])

    def origin(self, session: str = DIRECT) -> str:
        return f"direct:{self.label}/claude/{session}"

    def direct(self, verb: str, params: dict[str, Any], *, session: str = DIRECT, grant: Optional[str] = None) -> dict:
        return self.handle(verb, params, self.origin(session), grant=grant)

    def hub(self, verb: str, params: dict[str, Any]) -> dict:
        return self.handle(verb, params, "claude.ai/owner", hub=True)

    def mint(self, verb: str, targets=(), *, uses: int = 1, session: str = DIRECT) -> dict:
        return self.direct("grant_mint", {
            "session": f"{self.label}/claude/{session}", "verb": verb, "targets": list(targets), "expires_in_s": 120,
            "uses": uses, "origin_proof": {"type": "none"}, "command": f"/direct {verb}"}, session=session)["grant"]

    def uses(self, grant: dict) -> int:
        return self.mailbox.get_grant(grant["grant_id"])["uses_remaining"]

    def session(self, texts: tuple = ("hello there", "hi")) -> str:
        """A live, opted-in channel session with a transcript; returns its address."""
        session = sid()
        lines = []
        for i, text in enumerate(texts):
            if i % 2 == 0:
                lines.append({"type": "user", "message": {"role": "user", "content": text},
                              "timestamp": "2026-11-12T10:00:00.000Z", "cwd": "/Users/u/Dev/app"})
            else:
                lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                              "timestamp": "2026-11-12T10:00:01.000Z"})
        transcript = self.roots.claude_projects / "-tmp-proj" / f"{session}.jsonl"
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        address = f"{self.label}/claude/{session}"
        self.registrations[address] = self.handle("register", {
            "session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/Users/u/Dev/app",
            "version": "2.1.317", "accepts": ["note", "instruction", "question"], "channel_nonce": NONCE,
            "transcript_path": str(transcript)}, f"channel:{address}")
        return address

    def poll(self, address: str) -> list[dict[str, Any]]:
        session = address.split("/", 2)[2]
        creds = {"registration_id": self.registrations[address]["registration_id"], "session_id": session,
                 "channel_nonce": NONCE}
        return self.handle("poll", dict(creds, wait_s=0), f"channel:{address}")["messages"]

    def rows(self, address: str) -> list[dict[str, Any]]:
        return self.mailbox.messages_to(address)


class Wire:
    """An ``httpx.MockTransport`` into a node's service: records each request; ``fail`` injects transport failures.

    ``fail(verb, envelope)`` returns None (deliver), ``"refuse"`` (connection refused: nothing reaches the node),
    ``"drop"`` (the node applies the call, then the connection drops before the answer) or ``"gateway"`` (an
    HTTP 502 text answer; nothing reaches the node). ``status`` replaces the node's answer to a ``status`` probe.
    """

    def __init__(self, node: Node) -> None:
        self.node = node
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.fail: Optional[Callable[[str, dict[str, Any]], Optional[str]]] = None
        self.status: Optional[Callable[[], tuple[int, dict[str, Any]]]] = None
        self.hold: Optional[threading.Event] = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        verb = request.url.path.rsplit("/", 1)[1]
        envelope = json.loads(request.content)
        self.requests.append((verb, envelope, dict(request.headers)))
        mode = self.fail(verb, envelope) if self.fail is not None else None
        if mode == "refuse":
            raise httpx.ConnectError("connection refused", request=request)
        if mode == "gateway":
            return httpx.Response(502, text="Bad Gateway")
        if verb == "status" and self.status is not None:
            status, body = self.status()
            return httpx.Response(status, json={**body, "id": envelope["id"]})
        if self.hold is not None and verb != "status":
            self.hold.wait(5)
        auth = request.headers.get("authorization", "")
        status, body = self.node.service.handle(
            request.content, path_verb=verb, relay_token=request.headers.get("x-relay-token"),
            hub_bearer=auth.partition(" ")[2] or None, grant=request.headers.get("x-relay-grant"),
            peer_key=request.headers.get("x-relay-peer-key"))
        if mode == "drop":
            raise httpx.ReadError("connection dropped after the request was sent", request=request)
        return httpx.Response(status, json=body)

    def client(self, *, peer_key: Optional[str] = KEY) -> HttpRelayClient:
        """The client a peer holds: the peer key alone (``peer_key=None``: this node's own local credentials)."""
        creds: dict[str, Any] = {"peer_key": peer_key} if peer_key is not None else {
            "relay_token": self.node.relay_token, "hub_bearer": self.node.hub_bearer}
        return HttpRelayClient("http://127.0.0.1:9", machine=self.node.label, transport=httpx.MockTransport(self.handler),
                               **creds)

    def verbs(self, *, probes: bool = False) -> list[str]:
        return [verb for verb, _, _ in self.requests if probes or verb != "status"]

    def forwarded(self, verb: str) -> list[dict[str, Any]]:
        return [envelope for v, envelope, _ in self.requests if v == verb]


def old_status() -> tuple[int, dict[str, Any]]:
    """What a katsoft-llm-kit 1.20.0 relay answers a relay-origin status probe: relay was not a request origin."""
    error = P.RelayError("origin_not_allowed", "a relay origin may not call status")
    return 403, P.make_error(None, error)


def status_without_forwarding() -> tuple[int, dict[str, Any]]:
    result = {"protocol": P.PROTOCOL, "machine": DST, "now": 1.0, "pause": {
        "relay": {"paused": False, "paused_since": None, "reason": None},
        "hub": {"paused": False, "paused_since": None, "reason": None}}, "harnesses": ["claude", "codex"]}
    return 200, P.make_ok("x", result)


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """The receiver 5090w accepting m5pro, and the source m5pro forwarding to it."""
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY})
    wire = Wire(dst)
    src = Node(SRC, tmp_path, peers={DST: wire.client()})
    try:
        yield SimpleNamespace(src=src, dst=dst, wire=wire)
    finally:
        src.close()
        dst.close()


@pytest.fixture
def trio(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """m5pro forwarding to two receivers, 5090w and laptop."""
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY})
    third = Node(THIRD, tmp_path, accept_from=(SRC,), accept_keys={SRC: THIRD_KEY})
    wire, wire3 = Wire(dst), Wire(third)
    src = Node(SRC, tmp_path, peers={DST: wire.client(), THIRD: wire3.client(peer_key=THIRD_KEY)})
    try:
        yield SimpleNamespace(src=src, dst=dst, third=third, wire=wire, wire3=wire3)
    finally:
        for node in (src, dst, third):
            node.close()


def refused(code: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> P.RelayError:
    with pytest.raises(P.RelayError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code, caught.value
    return caught.value


# --------------------------------------------------------------------------
# peer-key binding (item 1)
# --------------------------------------------------------------------------

def test_peer_key_binds_machine(tmp_path):
    dst = Node(DST, tmp_path, accept_from=(SRC, THIRD), accept_keys={SRC: KEY})
    try:
        assert dst.handle("status", {}, f"relay:{SRC}", peer_key=KEY)["forwarding"] == {"receive": True}  # control
        refused("unauthenticated", dst.handle, "status", {}, f"relay:{SRC}")                       # the relay token only
        refused("unauthenticated", dst.handle, "status", {}, f"relay:{SRC}", peer_key=KEY + "x")   # wrong key
        refused("unauthenticated", dst.handle, "status", {}, f"relay:{SRC}", peer_key=KEY, token=True)  # never both
        refused("origin_not_allowed", dst.handle, "status", {}, "relay:otherbox", peer_key=KEY)    # m5pro's key, another name
        refused("origin_not_allowed", dst.handle, "status", {}, f"relay:{THIRD}", peer_key=KEY)    # accepted, but not m5pro
        refused("unauthenticated", dst.handle, "list_sessions", {"machine": DST}, f"relay:{SRC}",
                on_behalf_of=f"direct:{SRC}/claude/{DIRECT}", peer_key=THIRD_KEY)
    finally:
        dst.close()


def test_relay_origin_outside_accept_from_refused(tmp_path):
    """A key on disk binds nothing once the machine leaves relay.accept_from; this relay itself is never accepted."""
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY, THIRD: THIRD_KEY})
    try:
        refused("origin_not_allowed", dst.handle, "status", {}, f"relay:{THIRD}", peer_key=THIRD_KEY)
        refused("origin_not_allowed", dst.handle, "list_sessions", {"machine": DST}, f"relay:{THIRD}",
                on_behalf_of=f"direct:{THIRD}/claude/{DIRECT}", peer_key=THIRD_KEY)
        assert dst.handle("status", {}, f"relay:{SRC}", peer_key=KEY)["machine"] == DST
    finally:
        dst.close()
    with pytest.raises(ValueError, match="another machine"):
        Node(DST, tmp_path / "self", accept_from=(DST,), accept_keys={DST: KEY})


# --------------------------------------------------------------------------
# principals: a peer key is peer:<M>, never a local caller (security review of D1)
# --------------------------------------------------------------------------

ACCEPT_SRC = "relay:\n  accept_from: [m5pro]\n"


def _present(exported: dict[str, Any]) -> dict[str, str]:
    """Every credential an export object carries, as the headers a holder can send (1.20 or later shapes)."""
    headers = {"Content-Type": P.CONTENT_TYPE}
    for field, header in (("relay_token", P.HEADER_RELAY_TOKEN), ("forward_key", P.HEADER_PEER_KEY),
                          ("peer_key", P.HEADER_PEER_KEY)):
        if field in exported:
            headers[header] = exported[field]
    if "hub_bearer" in exported:
        headers[P.HEADER_AUTHORIZATION] = f"{P.BEARER_SCHEME} {exported['hub_bearer']}"
    return headers


@pytest.mark.parametrize("listed", [True, False], ids=["in_accept_from", "not_in_accept_from"])
def test_exported_credentials_cannot_claim_the_receivers_own_origins(tmp_path, capsys, monkeypatch, listed):
    """The codex/sol HIGH: what `peer export --for m5pro` on 5090w hands m5pro, presented as 5090w's own origins."""
    from test_relay_peers import as_host
    from llm_kit.cli import relay as relay_cli

    fleet = RealFleet(tmp_path)
    receiver_cfg = fleet.config(DST, ACCEPT_SRC)
    L.ensure_state_dir(receiver_cfg.paths)
    for credential in (receiver_cfg.paths.relay_token, receiver_cfg.paths.hub_bearer):
        L.ensure_token(credential)  # what a relay start leaves, so an export has every file it could read
    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: False)
    as_host(monkeypatch, DST)
    out = cli(["peer", "export", "--for", SRC, *fleet.args(DST)], capsys)
    assert out.code == 0, out.err
    headers = _present(json.loads(out.out))
    if not listed:
        receiver_cfg = fleet.config(DST)  # m5pro leaves relay.accept_from; the key it was issued stays on disk
    receiver = fleet.serve(receiver_cfg)
    try:
        address = fleet.register(receiver)
        own = f"direct:{DST}/claude/{DIRECT}"

        def attack(verb: str, params: dict[str, Any], origin: str, **extra: Any) -> dict[str, Any]:
            envelope = P.make_request(verb, params, origin=origin, request_id="req-" + secrets.token_hex(6), **extra)
            return httpx.post(receiver.url + f"/v1/{verb}", content=json.dumps(envelope), headers=headers,
                              timeout=10).json()

        mint = {"session": f"{DST}/claude/{DIRECT}", "verb": "send_message", "targets": [address], "expires_in_s": 60,
                "uses": 1, "origin_proof": {"type": "none"}}
        register = {"session_id": sid(), "harness": "claude", "pid": os.getpid(), "cwd": "/tmp", "version": "2.1.317",
                    "accepts": ["note"], "channel_nonce": NONCE}
        for verb, params, origin in (
            ("grant_mint", mint, own),                                              # a receiver-local grant
            ("send_message", {"to": address, "kind": "note", "body": "as 5090w"}, own),
            ("list_sessions", {}, own),
            ("pause", {"scope": "relay", "reason": "remote"}, f"operator:{DST}"),   # pausing the relay
            ("pause", {"scope": "hub", "reason": "remote"}, f"operator:{DST}"),
            ("status", {}, f"operator:{DST}"),
            ("register", dict(register), f"channel:{DST}/claude/{register['session_id']}"),
        ):
            body = attack(verb, params, origin)
            assert body["ok"] is False and body["error"]["code"] == "origin_not_allowed", (verb, origin, body)
        with HttpRelayClient(receiver.url, machine=DST, relay_token=receiver.prepared.relay_token) as local:
            pause = local.call("status", {}, origin=f"operator:{DST}")["pause"]
        assert not pause["relay"]["paused"] and not pause["hub"]["paused"]
        assert receiver.mailbox.messages_to(address) == []
        allowed = attack("status", {}, f"relay:{SRC}")  # the one thing the key is for, while m5pro is accepted
        assert allowed["ok"] is listed, allowed
    finally:
        receiver.stop()


def test_a_peer_key_claims_only_its_own_relay_or_the_hub(tmp_path):
    dst = Node(DST, tmp_path, accept_from=(SRC, THIRD), accept_keys={SRC: KEY, THIRD: THIRD_KEY})
    try:
        session = f"{THIRD}/claude/{DIRECT}"
        for origin, on_behalf_of in (
            (f"relay:{THIRD}", f"direct:{THIRD}/claude/{DIRECT}"),   # another accepted machine's identity
            (f"direct:{THIRD}/claude/{DIRECT}", None), (f"operator:{THIRD}", None), (f"channel:{session}", None),
            (f"direct:{SRC}/claude/{DIRECT}", None), (f"operator:{SRC}", None),  # its own machine, but not relay:
            (f"channel:{SRC}/claude/{DIRECT}", None),
            (f"direct:{DST}/claude/{DIRECT}", None), (f"operator:{DST}", None), (f"channel:{DST}/claude/{DIRECT}", None),
        ):
            refused("origin_not_allowed", dst.handle, "list_sessions", {}, origin, on_behalf_of=on_behalf_of, peer_key=KEY)
        refused("origin_not_allowed", dst.handle, "list_sessions", {}, f"relay:{SRC}",
                on_behalf_of=f"direct:{THIRD}/claude/{DIRECT}", peer_key=KEY)
        # the two it may claim
        assert dst.handle("list_sessions", {}, f"relay:{SRC}", on_behalf_of=f"direct:{SRC}/claude/{DIRECT}",
                          peer_key=KEY)["machines"][0]["machine"] == DST
        assert dst.handle("list_sessions", {}, "claude.ai/owner", peer_key=KEY)["machines"][0]["machine"] == DST
        # the relay token never authenticates a relay origin, and is never accepted together with a key
        refused("unauthenticated", dst.handle, "list_sessions", {}, f"relay:{SRC}",
                on_behalf_of=f"direct:{SRC}/claude/{DIRECT}")
        refused("unauthenticated", dst.handle, "list_sessions", {}, f"direct:{DST}/claude/{DIRECT}", peer_key=KEY,
                token=True)
        refused("grant_required", dst.handle, "list_sessions", {}, f"direct:{DST}/claude/{DIRECT}")  # local token: past origin
    finally:
        dst.close()
    with pytest.raises(ValueError, match="distinct"):
        Node(DST, tmp_path / "same", accept_from=(SRC,), accept_keys={SRC: f"relay-token-{DST}-" + "r" * 16})


def test_a_peer_request_is_never_forwarded_onward(tmp_path):
    """5090w accepts m5pro and itself forwards to laptop: nothing m5pro's key sends ever reaches laptop."""
    laptop = Node(THIRD, tmp_path, accept_from=(DST,), accept_keys={DST: THIRD_KEY})
    wire3 = Wire(laptop)
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY}, peers={THIRD: wire3.client(peer_key=THIRD_KEY)})
    try:
        remote = laptop.session()
        own = f"direct:{DST}/claude/{DIRECT}"
        relay = {"origin": f"relay:{SRC}", "on_behalf_of": f"direct:{SRC}/claude/{DIRECT}"}
        mint = {"session": f"{DST}/claude/{DIRECT}", "verb": "read_session", "targets": [remote], "expires_in_s": 60,
                "uses": 1, "origin_proof": {"type": "none"}}
        refused("origin_not_allowed", dst.handle, "grant_mint", mint, own, peer_key=KEY)
        refused("origin_not_allowed", dst.handle, "read_session", {"address": remote}, own, peer_key=KEY)
        for verb, params in (("read_session", {"address": remote}), ("get_replies", {"address": remote}),
                             ("send_message", {"to": remote, "kind": "note", "body": "onward"})):
            for kwargs in (relay, {"origin": "claude.ai/owner"}):  # answered (or refused) by 5090w itself
                try:
                    dst.handle(verb, params, kwargs["origin"], on_behalf_of=kwargs.get("on_behalf_of"), peer_key=KEY)
                except P.RelayError as exc:
                    assert exc.code != "internal", exc
        for kwargs in (relay, {"origin": "claude.ai/owner"}):  # discovery is answered from 5090w alone
            listed = dst.handle("list_sessions", {}, kwargs["origin"], on_behalf_of=kwargs.get("on_behalf_of"),
                                peer_key=KEY)
            assert [m["machine"] for m in listed["machines"]] == [DST]
            refused("message_not_found", dst.handle, "get_replies", {"message_id": "msg_01JBX3V9Q8K2M4N6P7R8S9T0VW"},
                    kwargs["origin"], on_behalf_of=kwargs.get("on_behalf_of"), peer_key=KEY)  # never probed onward
        assert wire3.requests == [] and laptop.rows(remote) == []
        # control: 5090w's own /direct session (the local token) does forward to laptop
        dst.direct("grant_mint", dict(mint))
        assert dst.direct("read_session", {"address": remote})["address"] == remote
        assert wire3.verbs() == ["read_session"]
    finally:
        dst.close()
        laptop.close()


def test_hub_via_peer_send_still_works(tmp_path, capsys, monkeypatch):
    """claude.ai -> the hub on m5pro -> 5090w's session, reached with the one peer key 5090w issued to m5pro."""
    from test_relay_hub import Hub, make_hub
    from test_relay_peers import as_host
    from llm_kit.cli import relay as relay_cli

    fleet = RealFleet(tmp_path)
    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: False)
    as_host(monkeypatch, DST)
    receiver_cfg = fleet.config(DST, ACCEPT_SRC)
    exported = cli(["peer", "export", "--for", SRC, *fleet.args(DST)], capsys)
    assert exported.code == 0, exported.err
    receiver = fleet.serve(receiver_cfg)
    try:
        address = fleet.register(receiver)
        as_host(monkeypatch, SRC)
        fleet.config(SRC, f"hub:\n  enabled: true\nrelay:\n  peers:\n    - {{machine: {DST}, url: '{receiver.url}'}}\n")
        monkeypatch.setattr(sys, "stdin", io.StringIO(exported.out))
        assert cli(["peer", "import", DST, *fleet.args(SRC)], capsys).code == 0
        source_cfg = fleet.config(SRC, f"hub:\n  enabled: true\nrelay:\n  peers:\n    - {{machine: {DST}, url: '{receiver.url}'}}\n")
        source = fleet.serve(source_cfg)
        try:
            import dataclasses

            hub_server = make_hub(dataclasses.replace(source_cfg, relay_port=source.address[1]), None).start()
            try:
                hub = Hub(hub_server)
                assert address in [s["address"] for s in hub.ok("list_sessions")["sessions"]]
                message = hub.ok("send_message", {"to": address, "kind": "note", "body": "from the app"})["message"]
                assert message["sender"] == "claude.ai/owner" and message.get("forwarded_by") is None
                stored = receiver.mailbox.get_message(message["id"])
                assert stored["sender"] == "claude.ai/owner" and stored["body"] == "from the app"
                assert hub.ok("get_replies", {"message_id": message["id"]})["messages"][0]["id"] == message["id"]
            finally:
                hub_server.stop()
        finally:
            source.stop()
    finally:
        receiver.stop()


def test_accept_from_config_parsed_and_refused(tmp_path):
    env = tmp_path / "env.json"
    env.write_text(json.dumps({"machines": {
        "m5pro.local": {"tailscale_hostname": SRC}, "w.local": {"tailscale_hostname": DST},
        "l.local": {"tailscale_hostname": THIRD, "disabled": True}}}), encoding="utf-8")

    def load(text: str) -> C.RelayConfig:
        path = tmp_path / "relay.yaml"
        path.write_text("enabled: true\n" + text, encoding="utf-8")
        return C.load_config(env_json=env, environ={}, hostname=lambda: "w.local", config_path=path,
                             state_root=tmp_path / "state")

    assert load("relay:\n  accept_from: [m5pro]\n").accept_from == (SRC,)
    assert load("").accept_from == ()
    for text, match in (("relay:\n  accept_from: [5090w]\n", "is this machine"),
                        ("relay:\n  accept_from: [ghost]\n", "not a machine in the fleet registry"),
                        ("relay:\n  accept_from: [laptop]\n", "is disabled"),
                        ("relay:\n  accept_from: [m5pro, m5pro]\n", "listed twice"),
                        ("relay:\n  accept_from: m5pro\n", "is not a list"),
                        ("accept_from: [m5pro]\n", "unknown keys: accept_from")):  # never a new top-level key
        with pytest.raises(C.RelayConfigError, match=match):
            load(text)
    load("relay:\n  accept_from: [m5pro]\n")
    assert C.enablement(tmp_path / "relay.yaml").relay  # the opt-in reader takes the nested key


# --------------------------------------------------------------------------
# delegation and laundering (items 2-4)
# --------------------------------------------------------------------------

def test_on_behalf_of_must_match_peer_machine(pair):
    dst = pair.dst
    for delegate in (f"direct:{THIRD}/claude/{DIRECT}", f"channel:{SRC}/claude/{DIRECT}", f"operator:{SRC}",
                     "claude.ai/owner", f"relay:{SRC}"):
        refused("origin_not_allowed", dst.handle, "list_sessions", {"machine": DST}, f"relay:{SRC}",
                on_behalf_of=delegate, peer_key=KEY)
    refused("origin_not_allowed", dst.handle, "list_sessions", {"machine": DST}, f"relay:{SRC}", peer_key=KEY)  # none
    ok = dst.handle("list_sessions", {"machine": DST}, f"relay:{SRC}", on_behalf_of=f"direct:{SRC}/claude/{DIRECT}",
                    peer_key=KEY)
    assert ok["machines"][0]["machine"] == DST
    refused("origin_not_allowed", dst.handle, "grant_mint", {
        "session": f"{SRC}/claude/{DIRECT}", "verb": "list_sessions", "targets": [], "expires_in_s": 60, "uses": 1,
        "origin_proof": {"type": "none"}}, f"relay:{SRC}", on_behalf_of=f"direct:{SRC}/claude/{DIRECT}", peer_key=KEY)


def test_on_behalf_of_refused_from_direct_hub_channel_operator(pair):
    src = pair.src
    delegate = f"direct:{SRC}/claude/{DIRECT}"
    for origin, hub in ((src.origin(), False), ("claude.ai/owner", True), (f"channel:{SRC}/claude/{DIRECT}", False),
                        (f"operator:{SRC}", False)):
        err = refused("bad_request", src.handle, "status", {}, origin, on_behalf_of=delegate, hub=hub)
        assert "on_behalf_of" in err.message
    assert pair.wire.requests == []


def test_channel_origin_not_forwarded(pair):
    src, address = pair.src, pair.src.session()
    refused("origin_not_allowed", src.handle, "list_sessions", {}, f"channel:{address}")
    refused("origin_not_allowed", src.handle, "read_session", {"address": f"{DST}/claude/{DIRECT}"}, f"channel:{address}")
    assert pair.wire.requests == []


def test_operator_origin_not_forwarded(pair):
    src = pair.src
    assert "forwarding" in src.handle("status", {}, f"operator:{SRC}")
    refused("origin_not_allowed", src.handle, "list_machines", {}, f"operator:{SRC}")
    assert pair.wire.requests == []


def test_hub_origin_not_forwarded(pair):
    src = pair.src
    result = src.hub("list_sessions", {"machine": DST})
    assert result["sessions"] == [] and result["machines"][0]["status"] == "unreachable"
    assert [m["machine"] for m in src.hub("list_machines", {})["machines"]] == [SRC]
    assert src.hub("list_sessions", {})["machines"] == [{"machine": SRC, "status": "up"}]
    refused("target_not_found", src.hub, "read_session", {"address": f"{DST}/claude/{DIRECT}"})
    refused("target_not_found", src.hub, "send_message", {"to": f"{DST}/claude/{DIRECT}", "kind": "note", "body": "x"})
    assert pair.wire.requests == []


def test_relay_origin_never_reforwarded(tmp_path):
    """The receiver has a forwarding peer of its own; a relay-origin call never reaches it."""
    third = Node(THIRD, tmp_path, accept_from=(DST,), accept_keys={DST: THIRD_KEY})
    onward = Wire(third)
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY}, peers={THIRD: onward.client(peer_key=THIRD_KEY)})
    delegate = f"direct:{SRC}/claude/{DIRECT}"
    try:
        for verb, params in (("list_machines", {}), ("list_sessions", {}), ("list_sessions", {"machine": THIRD}),
                             ("list_questions", {})):
            result = dst.handle(verb, params, f"relay:{SRC}", on_behalf_of=delegate, peer_key=KEY)
            assert THIRD not in json.dumps(result.get("sessions", [])), (verb, result)
        refused("target_not_found", dst.handle, "read_session", {"address": f"{THIRD}/claude/{DIRECT}"},
                f"relay:{SRC}", on_behalf_of=delegate, peer_key=KEY)
        refused("message_not_found", dst.handle, "get_replies", {"message_id": "msg_nowhere"}, f"relay:{SRC}",
                on_behalf_of=delegate, peer_key=KEY)
        assert onward.requests == []
    finally:
        dst.close()
        third.close()


def test_receiver_answers_relay_origin_list_locally_only(pair):
    dst, delegate = pair.dst, f"direct:{SRC}/claude/{DIRECT}"
    address = dst.session()
    machines = dst.handle("list_machines", {}, f"relay:{SRC}", on_behalf_of=delegate, peer_key=KEY)["machines"]
    assert [m["machine"] for m in machines] == [DST]
    sessions = dst.handle("list_sessions", {"machine": DST}, f"relay:{SRC}", on_behalf_of=delegate, peer_key=KEY)
    assert [s["address"] for s in sessions["sessions"]] == [address] and sessions["machines"] == [
        {"machine": DST, "status": "up"}]
    other = dst.handle("list_sessions", {"machine": SRC}, f"relay:{SRC}", on_behalf_of=delegate, peer_key=KEY)
    assert other["sessions"] == [] and other["machines"][0]["status"] == "unreachable"
    row = dst.mailbox.audit_rows(limit=500)[-1]
    assert (row["origin"], row["on_behalf_of"], row["verb"]) == (f"relay:{SRC}", delegate, "list_sessions")


# --------------------------------------------------------------------------
# grants (items 4, 4a, 5)
# --------------------------------------------------------------------------

def test_forward_requires_grant_for_exact_remote_target(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address, other = dst.session(), f"{DST}/claude/{sid()}"
    refused("grant_required", src.direct, "read_session", {"address": address})       # no grant at all
    grant = src.mint("read_session", [address])
    refused("grant_required", src.direct, "read_session", {"address": other})         # another remote address
    refused("grant_wrong_target", src.direct, "read_session", {"address": other}, grant=grant["grant_id"])
    refused("target_invalid", src.direct, "read_session", {"address": f"{DST}/latest"})  # a remote selector
    refused("target_invalid", src.mint, "read_session", [f"{DST}/latest"])              # also at mint
    assert wire.requests == []
    read = src.direct("read_session", {"address": address})
    assert read["address"] == address and [t["text"] for t in read["turns"]][:1] == ["hello there"]
    assert src.uses(grant) == 0 and wire.verbs() == ["read_session"]
    forwarded = wire.forwarded("read_session")[0]
    assert (forwarded["origin"], forwarded["on_behalf_of"]) == (f"relay:{SRC}", src.origin())
    headers = wire.requests[-1][2]
    assert headers["x-relay-peer-key"] == KEY and "x-relay-token" not in headers  # the peer key alone
    assert "authorization" not in headers


def test_remote_selector_target_invalid_no_io(pair):
    src = pair.src
    for verb, params in (("read_session", {"address": f"{DST}/claude/latest"}),
                         ("send_message", {"to": f"{DST}/task:p/s", "kind": "note", "body": "x"}),
                         ("get_replies", {"address": f"{DST}/cwd:app"})):
        refused("target_invalid", src.direct, verb, params)
    assert pair.wire.requests == []


def test_forward_reserves_before_io_concurrent_last_use(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address = dst.session()
    assert src.direct("list_machines", {}, grant=src.mint("list_machines")["grant_id"])  # warms the probe cache
    wire.requests.clear()
    grant = src.mint("read_session", [address], uses=1)
    wire.hold = threading.Event()
    outcomes: list[Any] = []

    def call() -> None:
        try:
            outcomes.append(src.direct("read_session", {"address": address}, grant=grant["grant_id"]))
        except P.RelayError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    settled = threading.Event()
    for _ in range(500):  # until the loser has answered while the winner's one request is held at the wire
        if outcomes and wire.verbs() == ["read_session"]:
            break
        settled.wait(0.01)
    wire.hold.set()
    for thread in threads:
        thread.join(timeout=10)
    errors = [o for o in outcomes if isinstance(o, P.RelayError)]
    assert len(outcomes) == 2 and [e.code for e in errors] == ["grant_used_up"], outcomes
    assert wire.verbs() == ["read_session"]  # one network request; the loser sent nothing
    assert src.uses(grant) == 0


# --------------------------------------------------------------------------
# outcomes (items 5, 6)
# --------------------------------------------------------------------------

def send(src: Node, to: str, *, id: Optional[str] = None, body: str = "run the tests", grant: bool = True) -> dict:
    granted = src.mint("send_message", [to]) if grant else None
    params: dict[str, Any] = {"to": to, "kind": "instruction", "body": body}
    if id is not None:
        params["id"] = id
    return {"grant": granted, "result": src.direct("send_message", params)}


def test_forward_lost_response_keeps_use_spent(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address = dst.session()
    grant = src.mint("send_message", [address])
    wire.fail = lambda verb, envelope: "drop" if verb == "send_message" else None
    err = refused("machine_unreachable", src.direct, "send_message", {"to": address, "kind": "note", "body": "hi"})
    assert err.details["sent"] is True and err.details["outcome"] == "unknown"
    assert P.is_valid_id("message", err.details["message_id"])
    assert [m["id"] for m in dst.rows(address)] == [err.details["message_id"]]  # the receiver applied it
    assert src.uses(grant) == 0  # the outcome is unknown: the use stays spent


def test_retry_same_id_delivers_once(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address = dst.session()
    src.mint("send_message", [address])
    wire.fail = lambda verb, envelope: "drop" if verb == "send_message" else None
    err = refused("machine_unreachable", src.direct, "send_message", {"to": address, "kind": "note", "body": "hi"})
    message_id = err.details["message_id"]
    wire.fail = None
    retry = src.mint("send_message", [address])
    result = src.direct("send_message", {"to": address, "kind": "note", "body": "hi", "id": message_id})
    assert result["replayed"] is True and result["message"]["id"] == message_id
    assert len(dst.rows(address)) == 1 and len(dst.poll(address)) == 1  # delivered once
    assert src.uses(retry) == 1  # a replay consumes nothing


def test_forward_connect_refused_refunds(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address = dst.session()
    assert src.direct("list_machines", {}, grant=src.mint("list_machines")["grant_id"])  # probe cached
    grant = src.mint("send_message", [address])
    wire.fail = lambda verb, envelope: "refuse"
    err = refused("machine_unreachable", src.direct, "send_message", {"to": address, "kind": "note", "body": "hi"})
    assert err.details["sent"] is False and "outcome" not in err.details and "message_id" in err.details
    assert src.uses(grant) == 1 and dst.rows(address) == []


def test_forward_502_keeps_use_spent(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address = dst.session()
    grant = src.mint("read_session", [address])
    wire.fail = lambda verb, envelope: "gateway" if verb == "read_session" else None
    err = refused("machine_unreachable", src.direct, "read_session", {"address": address})
    assert (err.details["sent"], err.details["outcome"]) == (True, "unknown")
    assert src.uses(grant) == 0


def test_forward_receiver_pre_effect_refusal_refunds(pair):
    """A receiver error envelope that is pre-effect (here a stored not_opted_in refusal) gives the use back."""
    src, dst = pair.src, pair.dst
    address = f"{DST}/claude/{sid()}"
    grant = src.mint("read_session", [address])
    refused("target_not_found", src.direct, "read_session", {"address": address})
    assert src.uses(grant) == 1
    dst.handle("pause", {"scope": "relay"}, f"operator:{DST}")
    live = dst.session()
    grant = src.mint("send_message", [live])
    err = refused("paused", src.direct, "send_message", {"to": live, "kind": "note", "body": "x"})
    assert "message_id" in err.details and src.uses(grant) == 1


def test_forwarded_sender_is_direct_identity(pair):
    src, dst = pair.src, pair.dst
    address = dst.session()
    message = send(src, address)["result"]["message"]
    assert (message["sender"], message["forwarded_by"]) == (src.origin(), f"relay:{SRC}")
    row = dst.rows(address)[0]
    assert (row["sender"], row["forwarded_by"]) == (src.origin(), f"relay:{SRC}")
    delivered = dst.poll(address)[0]
    assert (delivered["sender"], delivered["forwarded_by"]) == (src.origin(), f"relay:{SRC}")
    calls = [r for r in dst.mailbox.audit_rows(limit=500) if r["verb"] == "send_message"]
    assert {(r["event"], r["origin"], r["on_behalf_of"]) for r in calls} == {
        ("call", f"relay:{SRC}", src.origin()), ("transition", f"relay:{SRC}", src.origin())}
    source_call = [r for r in src.mailbox.audit_rows(limit=500) if r["verb"] == "send_message"][-1]
    assert (source_call["origin"], source_call["on_behalf_of"]) == (src.origin(), None)
    assert source_call["detail"] == f"forwarded to relay {DST}"


# --------------------------------------------------------------------------
# compatibility (item 8)
# --------------------------------------------------------------------------

def test_legacy_requests_unchanged(tmp_path, pair):
    """Calls without the field, on a relay with no peers and on one with peers, answer alike and send nothing."""
    from test_relay_protocol import LEGACY_CLAUDE_REQUESTS

    schema = P.load_schema("envelope.request")
    for verb, origin, params in LEGACY_CLAUDE_REQUESTS:
        assert list(JS.validate(schema, {"protocol": P.PROTOCOL, "id": "req-l", "verb": verb, "origin": origin,
                                         "params": params})) == []
    plain = Node(SRC, tmp_path / "plain")
    try:
        for node in (plain, pair.src):
            node.session()
            grant = node.mint("list_sessions", [SRC])["grant_id"]
            assert node.direct("list_sessions", {"machine": SRC}, grant=grant)["machines"] == [{"machine": SRC, "status": "up"}]
            hub = node.hub("list_sessions", {"machine": DST})
            assert hub["sessions"] == [] and hub["machines"][0]["error"]["code"] == "machine_unreachable"
            assert node.handle("status", {}, f"operator:{SRC}")["harnesses"] == ["claude", "codex"]
    finally:
        plain.close()
    assert pair.wire.requests == []


def test_old_receiver_refuses_on_behalf_of(pair):
    """Against the pinned 1.20 envelope schema, a forwarded request is bad_request at the envelope stage."""
    pinned = json.loads((FIXTURES / "envelope.request.json").read_text(encoding="utf-8"))
    pinned["$defs"] = json.loads((FIXTURES / "common.json").read_text(encoding="utf-8"))["$defs"]
    assert "on_behalf_of" not in pinned["properties"] and pinned["additionalProperties"] is False
    dst = pair.dst
    real_get = dst.service._schemas.get
    dst.service._schemas.get = lambda name: pinned if name == "envelope.request" else real_get(name)
    err = refused("bad_request", dst.handle, "list_sessions", {"machine": DST}, f"relay:{SRC}",
                  on_behalf_of=f"direct:{SRC}/claude/{DIRECT}", peer_key=KEY)
    assert "envelope" in err.message and dst.mailbox.messages_to(f"{DST}/claude/{DIRECT}") == []


@pytest.mark.parametrize("status", [old_status, status_without_forwarding], ids=["1.20-refusal", "no-field"])
def test_forwarder_refuses_receiver_without_capability(pair, status):
    src, dst, wire = pair.src, pair.dst, pair.wire
    address = dst.session()
    wire.status = status
    grant = src.mint("send_message", [address])
    err = refused("not_implemented", src.direct, "send_message", {"to": address, "kind": "note", "body": "x"})
    assert err.details["sent"] is False
    assert wire.verbs() == [] and wire.verbs(probes=True) == ["status"]  # nothing sent but the probe
    assert src.uses(grant) == 1 and dst.rows(address) == []
    refused("not_implemented", src.direct, "send_message", {"to": address, "kind": "note", "body": "x"})
    assert wire.verbs(probes=True) == ["status"]  # the probe answer is cached


def test_old_receiver_listed_unreachable_in_fanout(pair):
    src, wire = pair.src, pair.wire
    wire.status = old_status
    src.session()
    grant = src.mint("list_sessions")
    result = src.direct("list_sessions", {})
    reach = {m["machine"]: m for m in result["machines"]}
    assert reach[SRC]["status"] == "up" and len(result["sessions"]) == 1
    assert reach[DST]["status"] == "unreachable" and reach[DST]["error"]["code"] == "not_implemented"
    assert wire.verbs() == [] and src.uses(grant) == 0


def test_amendment1_and_status_harnesses_unchanged(pair):
    status = pair.dst.handle("status", {}, f"operator:{DST}")
    assert status["harnesses"] == list(P.CHANNEL_HARNESSES) and status["forwarding"] == {"receive": True}
    schema = P.load_schema("status.response")
    assert schema["properties"]["harnesses"]["items"]["enum"] == ["claude", "codex"]
    assert schema["properties"]["forwarding"]["required"] == ["receive"]
    assert P.CHANNEL_HARNESSES == ("claude", "codex")
    text = DOC.read_text(encoding="ascii")
    assert "- Amendment 1, 2026-11-12, katsoft-llm-kit 1.19.0 (Codex channel origin)." in text
    assert "- Amendment 2, 2026-11-12, katsoft-llm-kit 1.21.0 (relay-to-relay" in text
    assert text.index("- Amendment 1,") < text.index("- Amendment 2,")
    for literal in ("`on_behalf_of`", "`X-Relay-Peer-Key`", "`status.forwarding", "`forwarded_by", "`relay.accept_from`"):
        assert literal in text, literal


# --------------------------------------------------------------------------
# discovery and routing (item 4a)
# --------------------------------------------------------------------------

def test_fanout_list_sessions_includes_peer_sessions(pair):
    src, dst, wire = pair.src, pair.dst, pair.wire
    local, remote = src.session(), dst.session()
    grant = src.mint("list_sessions")
    result = src.direct("list_sessions", {})
    assert sorted(s["address"] for s in result["sessions"]) == sorted([local, remote])
    assert [s["machine"] for s in result["sessions"] if s["address"] == remote] == [DST]
    assert result["machines"] == [{"machine": SRC, "status": "up"}, {"machine": DST, "status": "up"}]
    forwarded = wire.forwarded("list_sessions")
    assert len(forwarded) == 1 and forwarded[0]["params"] == {"machine": DST}
    assert forwarded[0]["on_behalf_of"] == src.origin() and forwarded[0]["origin"] == f"relay:{SRC}"
    assert src.uses(grant) == 0


def test_fanout_list_machines_marks_unreachable_peer(pair):
    src, wire = pair.src, pair.wire
    wire.fail = lambda verb, envelope: "refuse"
    grant = src.mint("list_machines")
    machines = src.direct("list_machines", {})["machines"]
    assert [m["machine"] for m in machines] == [SRC, DST]
    assert machines[1]["status"] == "unreachable" and machines[1]["error"]["code"] == "machine_unreachable"
    assert src.uses(grant) == 0  # an ok fan-out is never refunded


def test_fanout_list_machines_merges_peer_rows(pair):
    src = pair.src
    src.mint("list_machines")
    machines = src.direct("list_machines", {})["machines"]
    assert [(m["machine"], m["status"]) for m in machines] == [(SRC, "up"), (DST, "up")]
    assert machines[1]["endpoints"] == [f"{DST}/llm/qwen"]


def test_fanout_list_questions_merges_peer_ledgers(tmp_path):
    ledgers = {}
    for label in (SRC, DST):
        path = tmp_path / f"{label}-ledgers" / "jobs.sqlite3"
        path.parent.mkdir(parents=True)
        question_ledger(path)
        ledgers[label] = path
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY}, ledgers=(ledgers[DST],))
    wire = Wire(dst)
    src = Node(SRC, tmp_path, peers={DST: wire.client()}, ledgers=(ledgers[SRC],))
    try:
        src.mint("list_questions")
        result = src.direct("list_questions", {})
        assert sorted(q["machine"] for q in result["questions"]) == sorted([SRC, DST])
        assert {q["id"].split("/", 1)[0] for q in result["questions"]} == {f"q:{SRC}", f"q:{DST}"}
        assert sorted(entry["machine"] for entry in result["ledgers"]) == sorted([SRC, DST])
        assert [m["machine"] for m in result["machines"]] == [SRC, DST]
        assert wire.forwarded("list_questions")[0]["params"] == {"machine": DST}
    finally:
        src.close()
        dst.close()


def test_answer_question_routes_by_id_machine(tmp_path):
    path = tmp_path / "ledgers" / "jobs.sqlite3"
    path.parent.mkdir(parents=True)
    question_ledger(path)
    dst = Node(DST, tmp_path, accept_from=(SRC,), accept_keys={SRC: KEY}, ledgers=(path,))
    wire = Wire(dst)
    src = Node(SRC, tmp_path, peers={DST: wire.client()})
    try:
        question = dst.handle("list_questions", {}, f"relay:{SRC}", on_behalf_of=src.origin(),
                              peer_key=KEY)["questions"][0]
        assert question["id"].startswith(f"q:{DST}/")
        grant = src.mint("answer_question", [question["id"]])
        result = src.direct("answer_question", {"id": question["id"], "decision": "reject", "reason": "not now"})
        assert result["id"] == question["id"] and result["state"] == "rejected"
        assert wire.verbs() == ["answer_question"] and src.uses(grant) == 0
    finally:
        src.close()
        dst.close()


def test_list_sessions_machine_peer_forwards_to_that_peer_only(trio):
    src = trio.src
    remote = trio.dst.session()
    trio.third.session()
    src.session()
    grant = src.mint("list_sessions", [DST])
    result = src.direct("list_sessions", {"machine": DST})
    assert [s["address"] for s in result["sessions"]] == [remote]
    assert result["machines"] == [{"machine": DST, "status": "up"}]
    assert trio.wire.verbs() == ["list_sessions"] and trio.wire3.requests == []
    assert src.uses(grant) == 0


def test_ask_llm_routes_to_the_peer_endpoint(pair):
    src, dst = pair.src, pair.dst
    grant = src.mint("ask_llm", [f"{DST}/llm/qwen"])
    result = src.direct("ask_llm", {"endpoint": f"{DST}/llm/qwen", "prompt": "hi"})
    assert result == {"endpoint": f"{DST}/llm/qwen", "text": f"answer from {DST}"}
    assert dst.llm.calls and not src.llm.calls and src.uses(grant) == 0


def test_ungranted_discovery_sends_zero_peer_requests(trio):
    src = trio.src
    for verb, params in (("list_machines", {}), ("list_sessions", {}), ("list_sessions", {"machine": DST}),
                         ("list_questions", {}), ("list_questions", {"machine": DST}),
                         ("get_replies", {"message_id": "msg_abc"}), ("get_replies", {"message_id": "msg_abc", "wait_s": 1})):
        refused("grant_required", src.direct, verb, params)
    assert trio.wire.requests == [] and trio.wire3.requests == []  # status probes included


def test_peer_targeted_grant_does_not_cover_fanout(pair):
    src = pair.src
    grant = src.mint("list_sessions", [DST])
    refused("grant_wrong_target", src.direct, "list_sessions", {}, grant=grant["grant_id"])
    refused("grant_required", src.direct, "list_sessions", {})
    assert pair.wire.requests == [] and src.uses(grant) == 1


def test_empty_target_grant_does_not_cover_peer_machine(pair):
    src = pair.src
    grant = src.mint("list_sessions")
    refused("grant_wrong_target", src.direct, "list_sessions", {"machine": DST}, grant=grant["grant_id"])
    refused("grant_required", src.direct, "list_questions", {"machine": DST})
    assert pair.wire.requests == [] and src.uses(grant) == 1


def test_get_replies_message_id_routes_to_holder(trio):
    src = trio.src
    address = trio.dst.session()
    message_id = send(src, address)["result"]["message"]["id"]
    trio.wire.requests.clear()
    trio.wire3.requests.clear()
    grant = src.mint("get_replies", [message_id])
    result = src.direct("get_replies", {"message_id": message_id, "wait_s": 1})
    assert [m["id"] for m in result["messages"]] == [message_id]
    assert result["messages"][0]["forwarded_by"] == f"relay:{SRC}"
    holder = [e["params"] for e in trio.wire.forwarded("get_replies")]
    assert holder == [{"message_id": message_id}, {"message_id": message_id, "wait_s": 1}]  # probe, then the wait
    assert [e["params"] for e in trio.wire3.forwarded("get_replies")] == [{"message_id": message_id}]
    assert src.uses(grant) == 0


def test_get_replies_message_id_on_this_relay_waits_here(pair):
    src = pair.src
    local = src.session()
    message_id = send(src, local)["result"]["message"]["id"]
    pair.wire.requests.clear()
    src.mint("get_replies", [message_id])
    assert [m["id"] for m in src.direct("get_replies", {"message_id": message_id, "wait_s": 1})["messages"]] == [message_id]
    assert [e["params"] for e in pair.wire.forwarded("get_replies")] == [{"message_id": message_id}]


def test_get_replies_message_id_not_found_everywhere_refunds(trio):
    src = trio.src
    grant = src.mint("get_replies", ["msg_nowhere"])
    err = refused("message_not_found", src.direct, "get_replies", {"message_id": "msg_nowhere", "wait_s": 2})
    assert sorted(err.details["machines"]) == sorted([SRC, DST, THIRD])
    assert src.uses(grant) == 1
    assert all("wait_s" not in e["params"] for e in trio.wire.forwarded("get_replies") + trio.wire3.forwarded("get_replies"))


def test_get_replies_message_id_with_unreachable_peer_keeps_use(trio):
    src = trio.src
    trio.wire3.fail = lambda verb, envelope: "refuse"
    grant = src.mint("get_replies", ["msg_nowhere"])
    err = refused("machine_unreachable", src.direct, "get_replies", {"message_id": "msg_nowhere"})
    assert THIRD in err.message and src.uses(grant) == 0
    trio.wire3.fail = lambda verb, envelope: "drop" if verb == "get_replies" else None
    grant = src.mint("get_replies", ["msg_nowhere"])
    err = refused("machine_unreachable", src.direct, "get_replies", {"message_id": "msg_nowhere"})
    assert src.uses(grant) == 0


def test_get_replies_message_id_two_holders_ambiguous(pair):
    src, dst = pair.src, pair.dst
    message_id = "msg_" + "twice" + secrets.token_hex(4)
    send(src, src.session(), id=message_id)
    send(src, dst.session(), id=message_id)
    grant = src.mint("get_replies", [message_id])
    err = refused("target_ambiguous", src.direct, "get_replies", {"message_id": message_id, "wait_s": 1})
    assert len(err.details["candidates"]) == 2 and src.uses(grant) == 0
    assert all("wait_s" not in e["params"] for e in pair.wire.forwarded("get_replies"))


def test_a_peer_without_a_forward_key_is_listed_and_never_sent_to(tmp_path):
    src = Node(SRC, tmp_path, peers={DST: None})
    try:
        src.mint("list_machines")
        machines = src.direct("list_machines", {})["machines"]
        assert machines[1]["machine"] == DST and machines[1]["error"]["details"]["sent"] is False
        assert "peer export --for m5pro" in machines[1]["error"]["message"]
        grant = src.mint("read_session", [f"{DST}/claude/{DIRECT}"])
        refused("machine_unreachable", src.direct, "read_session", {"address": f"{DST}/claude/{DIRECT}"})
        assert src.uses(grant) == 1
    finally:
        src.close()


# --------------------------------------------------------------------------
# provenance (item 7)
# --------------------------------------------------------------------------

class CannedPeer:
    """A forwarding client whose status advertises forwarding and whose ``verb`` answers are canned."""

    def __init__(self, machine: str, answers: dict[str, Any]) -> None:
        self.machine = machine
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any], str, Optional[str]]] = []

    def call(self, verb: str, params: Any, *, origin: str, grant: Any = None, on_behalf_of: Optional[str] = None):
        self.calls.append((verb, dict(params), origin, on_behalf_of))
        if verb == "status":
            return {"protocol": P.PROTOCOL, "machine": self.machine, "now": 1.0, "pause": {
                "relay": {"paused": False, "paused_since": None, "reason": None},
                "hub": {"paused": False, "paused_since": None, "reason": None}}, "forwarding": {"receive": True}}
        return copy.deepcopy(self.answers[verb])

    def close(self) -> None:
        pass


def record(**fields: Any) -> dict[str, Any]:
    base = copy.deepcopy(P.load_schema("message_record")["examples"][1])  # m5pro/... queued, direct:m5pro sender
    base.update(to=f"{DST}/claude/{DIRECT}")
    base.update(fields)
    return {k: v for k, v in base.items() if v is not None}


def test_forwarded_send_response_validates_at_source(tmp_path):
    to = f"{DST}/claude/{DIRECT}"
    src_origin = f"direct:{SRC}/claude/{DIRECT}"
    cases = {
        "ok": (record(sender=src_origin, forwarded_by=f"relay:{SRC}"), None),
        "other sender": (record(sender=f"direct:{SRC}/claude/{sid()}", forwarded_by=f"relay:{SRC}"), "internal"),
        "hub sender": (record(sender="claude.ai/owner"), "internal"),
        "no stamp": (record(sender=src_origin), "internal"),
        "other stamp": (record(sender=src_origin, forwarded_by=f"relay:{DST}"), "internal"),
        "to elsewhere": (record(sender=src_origin, forwarded_by=f"relay:{SRC}", to=f"{SRC}/claude/{DIRECT}"), "internal"),
    }
    for name, (message, code) in cases.items():
        peer = CannedPeer(DST, {"send_message": {"message": message, "replayed": False}})
        src = Node(SRC, tmp_path / name.replace(" ", "-"), peers={DST: peer})
        try:
            grant = src.mint("send_message", [to])
            if code is None:
                assert src.direct("send_message", {"to": to, "kind": "note", "body": "x"})["message"] == message
            else:
                refused(code, src.direct, "send_message", {"to": to, "kind": "note", "body": "x"})
            assert src.uses(grant) == 0, name  # internal is after the effect: the use stays spent
        finally:
            src.close()


def hub_router(clients: list[Any], tmp: Path) -> H.HubRouter:
    paths = C.StatePaths(tmp / "router-state")
    L.ensure_state_dir(paths)
    return H.HubRouter(clients, pause=L.PauseStore(paths), origin="claude.ai/owner")


def test_hub_get_replies_reads_forwarded_message(pair, tmp_path):
    src, dst = pair.src, pair.dst
    address = dst.session()
    message_id = send(src, address)["result"]["message"]["id"]
    clients = [Wire(src).client(peer_key=None), Wire(dst).client(peer_key=None)]
    router = hub_router(clients, tmp_path)
    try:
        for arguments in ({"address": address}, {"message_id": message_id}):
            messages = router.call("get_replies", arguments)["messages"]
            assert [(m["sender"], m["forwarded_by"]) for m in messages] == [(src.origin(), f"relay:{SRC}")], arguments
    finally:
        router.close()


def test_direct_get_replies_reads_forwarded_message(pair):
    src, dst = pair.src, pair.dst
    address = dst.session()
    hub_sent = dst.hub("send_message", {"to": address, "kind": "note", "body": "from the hub"})["message"]
    local_direct = dst.handle("grant_mint", {
        "session": f"{DST}/claude/{DIRECT}", "verb": "send_message", "targets": [address], "expires_in_s": 60,
        "uses": 1, "origin_proof": {"type": "none"}}, dst.origin())
    assert local_direct
    dst.direct("send_message", {"to": address, "kind": "note", "body": "from a 5090w session"})
    forwarded = send(src, address)["result"]["message"]
    grant = src.mint("get_replies", [address])
    messages = src.direct("get_replies", {"address": address})["messages"]
    assert [m["sender"] for m in messages] == ["claude.ai/owner", dst.origin(), src.origin()]
    assert [m.get("forwarded_by") for m in messages] == [None, None, f"relay:{SRC}"]
    assert {hub_sent["id"], forwarded["id"]} <= {m["id"] for m in messages} and src.uses(grant) == 0


def replies(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"messages": list(messages)}


def hub_reads(tmp: Path, served: dict[str, Any]) -> Any:
    """``get_replies`` through a hub over m5pro and 5090w, 5090w answering ``served``."""
    local = FakeRelay(SRC, {"get_replies": replies()})
    peer = FakeRelay(DST, {"get_replies": served})
    return hub_call(tmp, [local, peer], "get_replies", {"address": f"{DST}/claude/{DIRECT}"})


def test_hub_rejects_foreign_sender_without_forwarded_by(tmp_path):
    with pytest.raises(P.RelayError) as caught:
        hub_reads(tmp_path, replies(record(sender=f"direct:{SRC}/claude/{DIRECT}")))
    assert caught.value.code == "internal" and "get_replies.messages[].sender" in caught.value.message
    assert hub_reads(tmp_path, replies(record(sender=f"direct:{SRC}/claude/{DIRECT}", forwarded_by=f"relay:{SRC}")))


def test_hub_rejects_forwarded_by_mismatch_or_unknown_machine(tmp_path):
    for forged in (record(sender=f"direct:{SRC}/claude/{DIRECT}", forwarded_by=f"relay:{THIRD}"),   # mismatch
                   record(sender=f"direct:{THIRD}/claude/{DIRECT}", forwarded_by=f"relay:{THIRD}"),  # unknown machine
                   record(sender=f"direct:{DST}/claude/{DIRECT}", forwarded_by=f"relay:{DST}"),      # local sender, stamped
                   record(sender="claude.ai/owner", forwarded_by=f"relay:{SRC}"),                   # hub sender, stamped
                   record(sender=f"direct:{SRC}/claude/{DIRECT}", forwarded_by=f"operator:{SRC}"),
                   record(sender=f"direct:{SRC}/claude/{DIRECT}", forwarded_by="not an identity")):
        with pytest.raises(P.RelayError) as caught:
            hub_reads(tmp_path, replies(forged))
        assert caught.value.code == "internal" and "get_replies.messages[]" in caught.value.message, forged


def test_sender_kinds_channel_operator_relay_rejected(tmp_path):
    for sender in (f"channel:{DST}/claude/{DIRECT}", f"operator:{DST}", f"relay:{DST}", f"channel:{SRC}/claude/{DIRECT}"):
        with pytest.raises(P.RelayError) as caught:
            hub_reads(tmp_path, replies(record(sender=sender)))
        assert caught.value.code == "internal", sender
    for sender in ("claude.ai/owner", f"direct:{DST}/claude/{DIRECT}"):  # today's valid senders
        assert hub_reads(tmp_path, replies(record(sender=sender)))["messages"][0]["sender"] == sender


def test_destination_fields_stay_strict(tmp_path):
    """A known machine (m5pro) named by 5090w in a destination or ownership field is refused, as in 1.19.0."""
    session = f"{SRC}/claude/{DIRECT}"
    cases = [
        ("get_replies", {"address": f"{DST}/claude/{DIRECT}"}, replies(record(to=session, sender="claude.ai/owner"))),
        ("read_session", {"address": f"{DST}/claude/{DIRECT}"},
         {"address": session, "turns": [], "cursor": None, "truncated": False, "redactions": 0}),
        ("ask_llm", {"endpoint": f"{DST}/llm/qwen", "prompt": "x"}, {"endpoint": f"{SRC}/llm/qwen", "text": "x"}),
        ("answer_question", {"id": f"q:{DST}/jobs:run:1", "decision": "reject"},
         {"id": f"q:{SRC}/jobs:run:1", "decision": "reject", "state": "rejected", "replayed": False, "resolved_at": 1.0}),
    ]
    for verb, arguments, served in cases:
        with pytest.raises(P.RelayError) as caught:
            hub_call(tmp_path, [FakeRelay(SRC, {}), FakeRelay(DST, {verb: served})], verb, arguments)
        assert caught.value.code == "internal", verb
    rows = hub_call(tmp_path, [FakeRelay(SRC, {"list_machines": {"machines": [{"machine": SRC, "status": "up"}]}}),
                               FakeRelay(DST, {"list_machines": {"machines": [{"machine": SRC, "status": "up"}]}})],
                    "list_machines", {})["machines"]
    assert [(m["machine"], m["status"]) for m in rows] == [(SRC, "up"), (DST, "unreachable")]
    assert rows[1]["error"]["code"] == "internal"


def test_reply_sender_stays_strict(tmp_path):
    reply = {"id": "rpl_x1", "in_reply_to": "msg_01JBX3V9Q8K2M4N6P7R8S9T0VW", "sender": f"channel:{SRC}/claude/{DIRECT}",
             "state": "answered", "body": "x", "at": 1.0}
    with pytest.raises(P.RelayError) as caught:
        hub_reads(tmp_path, replies(record(sender="claude.ai/owner", reply=reply)))
    assert "messages[].reply.sender" in caught.value.message
    ok = dict(reply, sender=f"channel:{DST}/claude/{DIRECT}")
    assert hub_reads(tmp_path, replies(record(sender="claude.ai/owner", reply=ok)))


def test_forwarded_by_only_on_authenticated_relay_send(pair):
    src, dst = pair.src, pair.dst
    address = dst.session()
    dst.handle("grant_mint", {"session": f"{DST}/claude/{DIRECT}", "verb": "send_message", "targets": [address],
                              "expires_in_s": 60, "uses": 2, "origin_proof": {"type": "none"}}, dst.origin())
    assert "forwarded_by" not in dst.direct("send_message", {"to": address, "kind": "note", "body": "a"})["message"]
    assert "forwarded_by" not in dst.hub("send_message", {"to": address, "kind": "note", "body": "b"})["message"]
    refused("bad_request", dst.direct, "send_message", {"to": address, "kind": "note", "body": "c",
                                                         "forwarded_by": f"relay:{SRC}"})
    refused("bad_request", dst.handle, "send_message", {"to": address, "kind": "note", "body": "d"}, dst.origin(),
            envelope_extra={"forwarded_by": f"relay:{SRC}"})
    with pytest.raises(P.RelayError, match="relay defect"):  # the mailbox refuses an inconsistent stamp
        dst.mailbox.send_message(origin=f"direct:{DST}/claude/{DIRECT}", to=address, kind="note", body="e",
                                 forwarded_by=f"relay:{SRC}")
    assert [m.get("forwarded_by") for m in dst.rows(address)] == [None, None]


def test_mailbox_migration_forwarded_by_null_for_old_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    v1 = M._SCHEMA.replace(",\n    forwarded_by TEXT", "").replace(",\n    on_behalf_of TEXT", "")
    assert "forwarded_by" not in v1 and "on_behalf_of" not in v1
    with sqlite3.connect(path) as conn:
        for statement in M._split_script(v1):
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute("INSERT INTO messages (id, to_addr, sender, kind, body, hop, created_at, expires_at, updated_at,"
                     " state, attempts, content_hash) VALUES ('msg_old1', 'm5pro/claude/s1', 'claude.ai/owner', 'note',"
                     " 'old', 0, 1.0, 9999999999.0, 1.0, 'queued', 0, 'h')")
        conn.execute("INSERT INTO audit (at, machine, ingress, event, origin, verb, outcome) VALUES"
                     " (1.0, 'm5pro', 'relay', 'call', 'claude.ai/owner', 'status', 'ok')")
    box = M.Mailbox(path, machine=SRC)
    assert box.schema_version() == 2
    old = box.get_message("msg_old1")
    assert "forwarded_by" not in old and old["body"] == "old"
    assert box.audit_rows()[0]["on_behalf_of"] is None
    assert M.Mailbox(path, machine=SRC).schema_version() == 2  # reopening is a no-op
    stored = box.send_message(origin=f"direct:{DST}/claude/{DIRECT}", to="m5pro/claude/s1", kind="note", body="new",
                              forwarded_by=f"relay:{DST}")["message"]
    assert stored["forwarded_by"] == f"relay:{DST}"


def test_labeled_fields_table_matches_schemas():
    for verb in ("send_message", "get_replies"):
        prefix = "message" if verb == "send_message" else "messages[]"
        kinds = dict(H.LABELED_FIELDS[verb])
        assert kinds[f"{prefix}.sender"] == "sender" and kinds[f"{prefix}.forwarded_by"] == "forwarded_by"
        assert kinds[f"{prefix}.to"] == "address" and kinds[f"{prefix}.reply.sender"] == "identity"
        assert f"{prefix}.forwarded_by" in _schema_machine_fields(verb)  # the fail-closed comparison sees it
    for verb, fields in H.LABELED_FIELDS.items():
        assert {path for path, _ in fields} >= _schema_machine_fields(verb), verb
        assert {kind for _, kind in fields} <= RT.STRICT_KINDS | RT.PROVENANCE_KINDS
    assert H.LABELED_FIELDS is RT.LABELED_FIELDS


def test_hub_routing_unchanged_after_extraction(tmp_path):
    """Routing only: a machine names its relay, a bare selector resolves on exactly one, a message id is probed."""
    def read(machine: str) -> dict[str, Any]:
        return {"address": f"{machine}/claude/{DIRECT}", "turns": [], "cursor": None, "truncated": False, "redactions": 0}

    one, two = FakeRelay(SRC, {"read_session": read(SRC)}), FakeRelay(DST, {"read_session": read(DST)})
    assert hub_call(tmp_path, [one, two], "read_session", {"address": f"{DST}/claude/{DIRECT}"})["address"].startswith(DST)
    with pytest.raises(P.RelayError) as caught:
        hub_call(tmp_path, [one, two], "read_session", {"address": "latest"})
    assert caught.value.code == "target_ambiguous" and len(caught.value.details["candidates"]) == 2

    class Missing(FakeRelay):
        def __getattr__(self, verb: str):
            def call(params: Any, *, origin: str, grant: Any = None) -> Any:
                raise P.RelayError("target_not_found", "none here")
            return call

    assert hub_call(tmp_path, [one, Missing(DST, {})], "read_session", {"address": "latest"})["address"].startswith(SRC)
    with pytest.raises(P.RelayError) as caught:
        hub_call(tmp_path, [one, Missing(DST, {})], "read_session", {"address": f"{THIRD}/claude/{DIRECT}"})
    assert caught.value.code == "machine_unreachable"
    with pytest.raises(P.RelayError) as caught:
        hub_call(tmp_path, [Missing(SRC, {}), Missing(DST, {})], "get_replies", {"message_id": "msg_x"})
    assert caught.value.code == "target_not_found"


# --------------------------------------------------------------------------
# the client's sent-or-not split (item 5)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raise_, sent", [
    (lambda r: httpx.ConnectError("refused", request=r), False),
    (lambda r: httpx.ConnectTimeout("connect timeout", request=r), False),
    (lambda r: httpx.ReadTimeout("read timeout", request=r), True),
    (lambda r: httpx.ReadError("dropped", request=r), True),
    (lambda r: httpx.RemoteProtocolError("closed", request=r), True),
], ids=["connect-refused", "connect-timeout", "read-timeout", "dropped", "protocol"])
def test_client_splits_not_sent_from_unknown(raise_, sent):
    def handler(request: httpx.Request) -> httpx.Response:
        raise raise_(request)

    client = HttpRelayClient("http://127.0.0.1:9", machine=DST, relay_token="t" * 20, transport=httpx.MockTransport(handler))
    with pytest.raises(ClientRelayError) as caught:
        client.call("status", {}, origin=f"operator:{DST}")
    assert caught.value.code == "machine_unreachable" and caught.value.sent is sent
    assert caught.value.details["sent"] is sent and (caught.value.details.get("outcome") == "unknown") is sent


@pytest.mark.parametrize("status,body", [(502, "Bad Gateway"), (200, "not json"), (200, '{"protocol": "x"}')])
def test_client_marks_gateway_and_malformed_replies_sent(status, body):
    client = HttpRelayClient("http://127.0.0.1:9", machine=DST, relay_token="t" * 20,
                             transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body)))
    with pytest.raises(ClientRelayError) as caught:
        client.call("status", {}, origin=f"operator:{DST}")
    assert caught.value.sent is True


def test_a_peer_client_sends_the_peer_key_alone_and_a_local_client_never_does():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        envelope = json.loads(request.content)
        return httpx.Response(200, json=P.make_ok(envelope["id"], {}))

    peer = HttpRelayClient("http://127.0.0.1:9", machine=DST, peer_key=KEY, transport=httpx.MockTransport(handler))
    peer.call("status", {}, origin=f"relay:{SRC}")
    peer.call("status", {}, origin="claude.ai/owner")
    local = HttpRelayClient("http://127.0.0.1:9", machine=DST, relay_token="t" * 20, hub_bearer="b" * 20,
                            transport=httpx.MockTransport(handler))
    local.call("status", {}, origin="claude.ai/owner")
    for headers in seen[:2]:  # every origin: the peer key, never a relay token or a hub bearer
        assert headers["x-relay-peer-key"] == KEY and "x-relay-token" not in headers and "authorization" not in headers
    assert "x-relay-peer-key" not in seen[2] and seen[2]["x-relay-token"] == "t" * 20
    for creds in ({"peer_key": KEY, "relay_token": "t" * 20}, {"peer_key": KEY, "hub_bearer": "b" * 20}, {"peer_key": ""}):
        with pytest.raises(ValueError):
            HttpRelayClient("http://127.0.0.1:9", machine=DST, **creds)
    with pytest.raises(ValueError, match="peer key it issued to this machine alone"):
        HttpRelayClient("https://w.example.ts.net:4710", machine=DST, relay_token="t" * 20, hub_bearer="b" * 20)


# --------------------------------------------------------------------------
# the CLI: peer export --for, peer import of a forward key
# --------------------------------------------------------------------------

def test_peer_export_for_mints_and_import_stores_the_peer_key(cli_machines, capsys, monkeypatch):
    from test_relay_peers import as_host
    from llm_kit.cli import relay as relay_cli

    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: False)
    exporter = cli_machines.args("tower", "relay:\n  accept_from: [m5pro]\n")
    config = prepare(exporter)
    out = cli(["peer", "export", "--for", "laptop", *exporter], capsys)
    assert out.code == 2 and "not in relay.accept_from" in out.err and not config.paths.accept_dir.exists()
    out = cli(["peer", "export", "--for", "m5pro", *exporter], capsys)
    assert out.code == 0
    sent = json.loads(out.out)
    assert set(sent) == {"format", "machine", "peer_key"}  # no relay token, no hub bearer
    assert sent["peer_key"] == L.read_token(config.paths.accept_peer_key("m5pro"))
    assert json.loads(cli(["peer", "export", "--for", "m5pro", *exporter], capsys).out)["peer_key"] == sent["peer_key"]
    assert L.accept_peer_keys(config.paths) == {"m5pro": sent["peer_key"]}
    if os.name != "nt":
        assert stat.S_IMODE(config.paths.accept_peer_key("m5pro").stat().st_mode) == 0o600
        assert stat.S_IMODE(config.paths.accept_machine_dir("m5pro").stat().st_mode) == 0o700
    assert cli(["peer", "export", *exporter], capsys).code == 2  # --for is required: nothing general is exported

    as_host(monkeypatch, "m5pro")
    importer = cli_machines.args("m5pro", "relay:\n  peers:\n    - {machine: tower, url: 'https://t.example.ts.net:4710'}\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(out.out))
    done = cli(["peer", "import", "tower", *importer], capsys)
    assert done.code == 0, done.err
    paths = C.StatePaths(Path(importer[5]))
    assert json.loads(done.out)["files"] == [str(paths.peer_key("tower"))]
    assert L.peer_key(paths, "tower", own="m5pro") == sent["peer_key"] and sent["peer_key"] not in done.out


# --------------------------------------------------------------------------
# end to end: two real relays over loopback HTTP
# --------------------------------------------------------------------------

class RealFleet:
    """Real relays (and a hub) over loopback HTTP: one env.json, and per machine a relay.yaml, state and harness homes."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.env = tmp / "env.json"
        self.env.write_text(json.dumps({"machines": {f"{label}.local": {"tailscale_hostname": label}
                                                     for label in (SRC, DST, THIRD)}}), encoding="utf-8")

    def args(self, label: str) -> list[str]:
        """The CLI's ``--env-json/--config/--state-dir`` for ``label``."""
        return ["--env-json", str(self.env), "--config", str(self.tmp / label / "relay.yaml"),
                "--state-dir", str(self.tmp / label / "state")]

    def config(self, label: str, text: str = "") -> C.RelayConfig:
        path = self.tmp / label / "relay.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("enabled: true\n" + text, encoding="utf-8")
        return C.load_config(env_json=self.env, environ={}, hostname=lambda: f"{label}.local", config_path=path,
                             state_root=self.tmp / label / "state")

    def serve(self, cfg: C.RelayConfig) -> S.RelayServer:
        from test_relay_server import FakeBackend

        from llm_kit.registry import consent

        label = cfg.machine_label
        claude, codex = self.tmp / label / "claude", self.tmp / label / "codex"
        for directory in (claude / "sessions", claude / "projects", codex / "sessions"):
            directory.mkdir(parents=True, exist_ok=True)
        backend = FakeBackend()
        return S.RelayServer(cfg, port=0, roots=D.DiscoveryRoots(claude, codex), probe=D.PsutilProbe(),
                             backend_factory=lambda entry: SimpleNamespace(backend=backend, model=entry.model),
                             policy_loader=lambda scope: consent.policy(scope, path=self.tmp / label / "consent.json"),
                             poll_interval_s=0.05, maintain_interval_s=3600.0).start()

    @staticmethod
    def register(server: S.RelayServer, accepts: tuple = ("note",)) -> str:
        """A live channel session on ``server`` (registered with that machine's own relay token); its address."""
        session = sid()
        address = f"{server.config.machine_label}/claude/{session}"
        with HttpRelayClient(server.url, machine=server.config.machine_label,
                             relay_token=server.prepared.relay_token) as channel:
            channel.call("register", {"session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/tmp",
                                      "version": "2.1.317", "accepts": list(accepts), "channel_nonce": NONCE},
                         origin=f"channel:{address}")
        return address


def test_forwarding_end_to_end_over_two_real_relays(tmp_path):
    fleet = RealFleet(tmp_path)

    def config(label: str, _host: str, text: str) -> C.RelayConfig:
        return fleet.config(label, text)

    def serve(cfg: C.RelayConfig, _label: str) -> S.RelayServer:
        return fleet.serve(cfg)

    receiver_cfg = config(DST, "b.local", "relay:\n  accept_from: [m5pro]\n")
    L.ensure_state_dir(receiver_cfg.paths)
    key = L.ensure_accept_peer_key(receiver_cfg.paths, SRC)  # peer export --for m5pro, before the start
    receiver = serve(receiver_cfg, DST)
    try:
        source_cfg = config(SRC, "a.local", f"relay:\n  peers:\n    - {{machine: {DST}, url: '{receiver.url}'}}\n")
        # a peer directory a 1.19.0 / 1.20.0 import left (general relay token and hub bearer, no key) stops the relay
        L.write_peer_key(source_cfg.paths, DST, "placeholder")
        source_cfg.paths.peer_key(DST).unlink()
        L.write_private_file(source_cfg.paths.peer_relay_token(DST), receiver.prepared.relay_token + "\n")
        with pytest.raises(C.RelayConfigError, match=f"Re-export.*`ssh {DST} llm-kit relay peer export --for {SRC} \\| "
                                                     f"llm-kit relay peer import {DST}`"):
            serve(source_cfg, SRC)
        L.write_peer_key(source_cfg.paths, DST, key)
        source = serve(source_cfg, SRC)
        try:
            session = sid()
            address = f"{DST}/claude/{session}"
            channel = HttpRelayClient(receiver.url, machine=DST, relay_token=receiver.prepared.relay_token)
            channel.call("register", {"session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/tmp",
                                      "version": "2.1.317", "accepts": ["note"], "channel_nonce": NONCE},
                         origin=f"channel:{address}")
            direct = HttpRelayClient(source.url, machine=SRC, relay_token=source.prepared.relay_token)
            origin = f"direct:{SRC}/claude/{DIRECT}"

            def mint(verb: str, targets: list[str]) -> None:
                direct.call("grant_mint", {"session": f"{SRC}/claude/{DIRECT}", "verb": verb, "targets": targets,
                                           "expires_in_s": 60, "uses": 1, "origin_proof": {"type": "none"}}, origin=origin)

            mint("list_sessions", [])
            listed = direct.call("list_sessions", {}, origin=origin)
            assert address in [s["address"] for s in listed["sessions"]]
            assert [m["machine"] for m in listed["machines"]] == [SRC, DST]
            mint("send_message", [address])
            message = direct.call("send_message", {"to": address, "kind": "note", "body": "across"}, origin=origin)["message"]
            assert (message["sender"], message["forwarded_by"]) == (origin, f"relay:{SRC}")
            assert receiver.mailbox.get_message(message["id"])["forwarded_by"] == f"relay:{SRC}"
            assert source.service.forwarding_peers == (DST,)
            channel.close()
            direct.close()
        finally:
            source.stop()
    finally:
        receiver.stop()
