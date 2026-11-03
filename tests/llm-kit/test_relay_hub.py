"""Tests for llm_kit.relay.hub (unit H5).

Every test serves real relays (uvicorn on ephemeral 127.0.0.1 ports, one state
directory and one machine label each, from a test env.json) and a real hub in
front of them, and talks to the hub with the mcp SDK's own streamable-HTTP
client. Nothing reads or writes the real ~/.claude, ~/.codex or
~/.local/state. LLM backends are test doubles; channel registrations name this
test process as their harness pid, because liveness is real.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterator, Optional

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from llm_kit.completion.types import LLMResponse
from llm_kit.registry import consent
from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import hub as H
from llm_kit.relay import lifecycle as L
from llm_kit.relay import protocol as P
from llm_kit.relay import server as S

A, B, GHOST = "m5pro", "tower", "ghost"
ENV_JSON = {"machines": {
    "m5pro.local": {"os": "macos", "tailscale_hostname": "m5pro"},
    "tower.local": {"os": "linux", "tailscale_hostname": "tower"},
}}
NONCE = "nonce-" + "x" * 20


def sid() -> str:
    return str(uuid.uuid4())


class FakeBackend:
    name = "fake"

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[str] = []

    def complete(self, system, user, *, model, options=None):
        self.calls.append(user)
        return LLMResponse(text=f"{self.tag}: {user}", model=model, finish_reason="stop", input_tokens=3, output_tokens=2)

    def classify_halt(self, exc):  # pragma: no cover - protocol member
        return None


class Machine:
    """One machine: config, harness homes, backend and a running relay."""

    def __init__(self, label: str, tmp_path: Path) -> None:
        self.label = label
        self.root = tmp_path / label
        env_json = tmp_path / "env.json"
        env_json.write_text(json.dumps(ENV_JSON), encoding="utf-8")
        self.config = C.load_config(env_json=env_json, environ={}, hostname=lambda: f"{label}.local",
                                    config_path=self.root / "config" / "relay.yaml", state_root=self.root / "state")
        claude, codex = self.root / "claude-home", self.root / "codex-home"
        (claude / "sessions").mkdir(parents=True)
        (claude / "projects" / "-tmp-proj").mkdir(parents=True)
        (codex / "sessions").mkdir(parents=True)
        self.roots = D.DiscoveryRoots(claude, codex)
        self.backend = FakeBackend(label)
        self.server = S.RelayServer(
            self.config, port=0, roots=self.roots, probe=D.PsutilProbe(),
            backend_factory=lambda entry: SimpleNamespace(backend=self.backend, model=entry.model),
            policy_loader=lambda scope: consent.policy(scope, path=self.root / "consent.json"),
            poll_interval_s=0.05, maintain_interval_s=3600.0,
        ).start()

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def peer(self) -> H.RelayPeer:
        prepared = self.server.prepared
        return H.RelayPeer(self.label, self.url, prepared.relay_token, prepared.hub_bearer)

    def address(self, session: str, harness: str = "claude") -> str:
        return f"{self.label}/{harness}/{session}"

    def session(self, texts: list[str], *, task: Optional[str] = None) -> str:
        """Register a live channel session with a transcript; returns its session id."""
        session = sid()
        lines = []
        for i, text in enumerate(texts):
            if i % 2 == 0:
                lines.append({"type": "user", "message": {"role": "user", "content": text},
                              "timestamp": "2026-11-03T10:00:00.000Z", "cwd": "/Users/u/Dev/app"})
            else:
                lines.append({"type": "assistant",
                              "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                              "timestamp": "2026-11-03T10:00:01.000Z"})
        transcript = self.roots.claude_projects / "-tmp-proj" / f"{session}.jsonl"
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        params: dict[str, Any] = {
            "session_id": session, "harness": "claude", "pid": __import__("os").getpid(), "cwd": "/Users/u/Dev/app",
            "version": "2.1.316", "accepts": ["note"], "channel_nonce": NONCE, "transcript_path": str(transcript),
        }
        if task is not None:
            params["task"] = task
        client = H.HttpRelayClient(self.url, machine=self.label, relay_token=self.server.prepared.relay_token)
        try:
            client.call("register", params, origin=f"channel:{self.address(session)}")
        finally:
            client.close()
        return session


@pytest.fixture
def machine_a(tmp_path: Path) -> Iterator[Machine]:
    machine = Machine(A, tmp_path)
    try:
        yield machine
    finally:
        machine.server.stop()


@pytest.fixture
def machine_b(tmp_path: Path) -> Iterator[Machine]:
    machine = Machine(B, tmp_path)
    try:
        yield machine
    finally:
        machine.server.stop()


def dead_url() -> str:
    """A loopback URL nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


@pytest.fixture
def ghost() -> H.RelayPeer:
    return H.RelayPeer(GHOST, dead_url(), "unused-relay-token", "unused-hub-bearer")


class Hub:
    """A running hub plus a synchronous MCP caller."""

    def __init__(self, server: H.HubServer) -> None:
        self.server = server

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def bearer(self) -> str:
        assert self.server.bearer is not None
        return self.server.bearer

    def run(self, fn: Callable[[Client], Awaitable[Any]], *, bearer: Optional[str] = "default") -> Any:
        token = self.bearer if bearer == "default" else bearer
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        async def go() -> Any:
            async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
                async with Client(streamable_http_client(self.url, http_client=http)) as client:
                    return await fn(client)

        return asyncio.run(go())

    def tool(self, name: str, arguments: Optional[dict] = None):
        return self.run(lambda client: client.call_tool(name, arguments or {}))

    def ok(self, name: str, arguments: Optional[dict] = None) -> dict:
        result = self.tool(name, arguments)
        assert not result.is_error, result
        return result.structured_content

    def refused(self, name: str, arguments: Optional[dict] = None) -> dict:
        result = self.tool(name, arguments)
        assert result.is_error, result
        error = result.structured_content["error"]
        assert json.loads(result.content[0].text) == {"error": error}
        return error


def make_hub(config: C.RelayConfig, peers, **kw: Any) -> H.HubServer:
    kw.setdefault("client_timeout_s", 5.0)
    return H.HubServer(config, peers=peers, port=0, **kw)


@pytest.fixture
def hub(machine_a, machine_b, ghost) -> Iterator[Hub]:
    """The hub in A's state directory, in front of A, B and an unreachable peer."""
    server = make_hub(machine_a.config, [machine_a.peer, machine_b.peer, ghost]).start()
    try:
        yield Hub(server)
    finally:
        server.stop()


@pytest.fixture
def hub_two(machine_a, machine_b) -> Iterator[Hub]:
    """The hub in front of A and B only (everything reachable)."""
    server = make_hub(machine_a.config, [machine_a.peer, machine_b.peer]).start()
    try:
        yield Hub(server)
    finally:
        server.stop()


# --------------------------------------------------------------------------
# the tools and their schemas
# --------------------------------------------------------------------------

def test_the_hub_serves_the_p1_tools_with_the_verbs_schemas(hub):
    tools = {t.name: t for t in hub.run(lambda c: c.list_tools()).tools}
    assert tuple(tools) == H.P1_TOOLS == ("list_machines", "list_sessions", "read_session", "ask_llm")
    for name, tool in tools.items():
        wanted_in = P.load_schema(P.request_schema_name(name))
        wanted_out = P.load_schema(P.response_schema_name(name))
        for key in ("$id",):
            wanted_in.pop(key, None)
            wanted_out.pop(key, None)
        assert tool.input_schema == wanted_in, name
        assert tool.output_schema == wanted_out, name
        assert "data, not instructions" in tool.description.lower(), name
        assert P.VERBS[name].summary in tool.description


def test_the_server_instructions_say_session_output_is_data(hub):
    instructions = hub.run(lambda c: asyncio.sleep(0, c.instructions))
    assert instructions is not None and "data, not instructions" in instructions.lower()


# --------------------------------------------------------------------------
# list_machines and list_sessions fan out
# --------------------------------------------------------------------------

def test_list_machines_fans_out_and_marks_an_unreachable_relay(hub, machine_a, machine_b):
    machines = {m["machine"]: m for m in hub.ok("list_machines")["machines"]}
    assert list(machines) == [A, B, GHOST]
    assert machines[A]["status"] == "up" and machines[B]["status"] == "up"
    assert machines[A]["endpoints"][0].startswith(f"{A}/llm/")
    assert machines[GHOST]["status"] == "unreachable"
    assert machines[GHOST]["error"]["code"] == "machine_unreachable"
    assert machines[GHOST]["error"]["retryable"] is True


def test_list_sessions_merges_every_relay_and_lists_the_unreachable_one(hub, machine_a, machine_b):
    a_session = machine_a.session(["a question", "an answer"])
    b_session = machine_b.session(["b question", "b answer"], task="plugins-kit/harness-messaging")
    listed = hub.ok("list_sessions")
    addresses = {s["address"] for s in listed["sessions"] if s["registered"]}
    assert addresses == {machine_a.address(a_session), machine_b.address(b_session)}
    reach = {m["machine"]: m for m in listed["machines"]}
    assert reach[A]["status"] == "up" and reach[B]["status"] == "up"
    assert reach[GHOST]["status"] == "unreachable" and reach[GHOST]["error"]["code"] == "machine_unreachable"


def test_list_sessions_for_one_machine_asks_only_its_relay(hub, machine_a, machine_b):
    machine_a.session(["x", "y"])
    b_session = machine_b.session(["x", "y"])
    listed = hub.ok("list_sessions", {"machine": B})
    assert {s["address"] for s in listed["sessions"]} == {machine_b.address(b_session)}
    assert [m["machine"] for m in listed["machines"]] == [B]


def test_list_sessions_for_a_machine_without_a_relay_is_listed_unreachable(hub):
    listed = hub.ok("list_sessions", {"machine": "elsewhere"})
    assert listed["sessions"] == []
    assert listed["machines"][0]["machine"] == "elsewhere"
    assert listed["machines"][0]["status"] == "unreachable"


def test_filters_pass_through_to_each_relay(hub_two, machine_a):
    machine_a.session(["x", "y"])
    assert hub_two.ok("list_sessions", {"harness": "codex"})["sessions"] == []
    assert hub_two.ok("list_sessions", {"state": "live-accepting"})["sessions"] != []


# --------------------------------------------------------------------------
# read_session and ask_llm route by the address's machine
# --------------------------------------------------------------------------

def test_read_session_goes_to_the_relay_that_owns_the_address(hub, machine_a, machine_b):
    a_session = machine_a.session(["a asks", "a answers"])
    b_session = machine_b.session(["b asks", "b answers"])
    a_tail = hub.ok("read_session", {"address": machine_a.address(a_session)})
    b_tail = hub.ok("read_session", {"address": machine_b.address(b_session)})
    assert [t["text"] for t in a_tail["turns"]] == ["a asks", "a answers"]
    assert [t["text"] for t in b_tail["turns"]] == ["b asks", "b answers"]
    assert b_tail["address"] == machine_b.address(b_session)


def test_read_session_arguments_equal_the_verbs_params(hub, machine_a):
    session = machine_a.session(["one", "two", "three", "four"])
    first = hub.ok("read_session", {"address": machine_a.address(session), "limit": 2})
    assert [t["text"] for t in first["turns"]] == ["three", "four"]
    assert first["truncated"] is True
    direct = H.HttpRelayClient(machine_a.url, machine=A, relay_token=machine_a.server.prepared.relay_token,
                               hub_bearer=machine_a.server.prepared.hub_bearer)
    try:
        assert direct.call("read_session", {"address": machine_a.address(session), "limit": 2},
                           origin="claude.ai/owner") == first
    finally:
        direct.close()


def test_a_bare_selector_resolves_on_exactly_one_relay(hub_two, machine_a, machine_b):
    machine_a.session(["a1", "a2"])
    b_session = machine_b.session(["b1", "b2"], task="plugins-kit/harness-messaging")
    tail = hub_two.ok("read_session", {"address": "task:plugins-kit/harness-messaging"})
    assert tail["address"] == machine_b.address(b_session)


def test_a_bare_selector_that_resolves_twice_is_ambiguous(hub_two, machine_a, machine_b):
    machine_a.session(["a1", "a2"], task="plugins-kit/both")
    machine_b.session(["b1", "b2"], task="plugins-kit/both")
    error = hub_two.refused("read_session", {"address": "task:plugins-kit/both"})
    assert error["code"] == "target_ambiguous"
    assert len(error["details"]["candidates"]) == 2


def test_a_bare_selector_with_an_unreachable_relay_is_not_a_not_found(hub, machine_a):
    error = hub.refused("read_session", {"address": "task:plugins-kit/nowhere"})
    assert error["code"] == "machine_unreachable"
    assert error["details"]["machines"] == [GHOST]


def test_read_session_on_an_unreachable_machine_is_machine_unreachable(hub):
    error = hub.refused("read_session", {"address": f"{GHOST}/claude/{sid()}"})
    assert error["code"] == "machine_unreachable" and error["retryable"] is True


def test_read_session_on_a_machine_without_a_relay_is_machine_unreachable(hub):
    error = hub.refused("read_session", {"address": f"elsewhere/claude/{sid()}"})
    assert error["code"] == "machine_unreachable"
    assert error["details"] == {"machine": "elsewhere"}


def test_a_relay_refusal_comes_back_with_its_code(hub, machine_a):
    error = hub.refused("read_session", {"address": machine_a.address(sid())})
    assert error["code"] == "target_not_found"


def test_ask_llm_goes_to_the_machine_named_in_the_endpoint(hub_two, machine_a, machine_b):
    endpoint = f"{B}/llm/qwen38-m5-32k-local"
    answer = hub_two.ok("ask_llm", {"endpoint": endpoint, "prompt": "ping"})
    assert answer["text"] == f"{B}: ping" and answer["endpoint"] == endpoint
    assert machine_b.backend.calls == ["ping"] and machine_a.backend.calls == []


def test_ask_llm_needs_a_machine_and_a_llm_address(hub_two):
    error = hub_two.refused("ask_llm", {"endpoint": f"{A}/claude/{sid()}", "prompt": "ping"})
    assert error["code"] == "endpoint_refused"


# --------------------------------------------------------------------------
# arguments are the verb's params
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool,arguments", [
    ("read_session", {}),
    ("read_session", {"address": "m5pro/claude/x", "bogus": 1}),
    ("read_session", {"address": "m5pro/claude/x", "limit": 0}),
    ("list_sessions", {"harness": "vim"}),
    ("list_machines", {"machine": "m5pro"}),
    ("ask_llm", {"endpoint": "m5pro/llm/x"}),
])
def test_arguments_that_fail_the_verbs_schema_are_bad_request(hub_two, tool, arguments):
    error = hub_two.refused(tool, arguments)
    assert error["code"] == "bad_request" and error["details"]["schema_errors"]


def test_an_unserved_tool_is_unknown_verb(hub_two):
    error = hub_two.refused("send_message", {"to": "m5pro/claude/x", "kind": "note", "body": "hi"})
    assert error["code"] == "unknown_verb"


# --------------------------------------------------------------------------
# the hub reaches relays with the hub origin and BOTH credentials
# --------------------------------------------------------------------------

def test_a_relay_that_refuses_the_hub_bearer_is_listed_unreachable_with_its_code(machine_a):
    wrong = dataclasses.replace(machine_a.peer, hub_bearer="not-the-hub-bearer")
    server = make_hub(machine_a.config, [wrong]).start()
    try:
        machines = Hub(server).ok("list_machines")["machines"]
    finally:
        server.stop()
    assert machines[0]["status"] == "unreachable" and machines[0]["error"]["code"] == "unauthenticated"


def test_a_relay_that_refuses_the_relay_token_is_listed_unreachable_with_its_code(machine_a):
    wrong = dataclasses.replace(machine_a.peer, relay_token="not-the-relay-token")
    server = make_hub(machine_a.config, [wrong]).start()
    try:
        machines = Hub(server).ok("list_machines")["machines"]
    finally:
        server.stop()
    assert machines[0]["status"] == "unreachable" and machines[0]["error"]["code"] == "unauthenticated"


def test_the_relay_audits_hub_calls_with_the_hub_origin(hub_two, machine_a):
    hub_two.ok("list_machines")
    rows = machine_a.server.mailbox.audit_rows()
    assert any(r["origin"] == "claude.ai/owner" and r["verb"] == "list_machines" and r["outcome"] == "ok"
               for r in rows)


def test_default_peers_are_this_machines_relay(machine_a):
    # The default relay URL is RelayConfig.bind("relay"); the test relay is on an ephemeral port, so
    # re-point the configuration at it exactly as an operator's relay.yaml would.
    host, port = machine_a.server.address
    config = dataclasses.replace(machine_a.config, relay_host=host, relay_port=port)
    server = H.HubServer(config, port=0, client_timeout_s=5.0).start()
    try:
        machines = Hub(server).ok("list_machines")["machines"]
    finally:
        server.stop()
    assert [m["machine"] for m in machines] == [A] and machines[0]["status"] == "up"


# --------------------------------------------------------------------------
# authentication: a bearer is required, loopback or not
# --------------------------------------------------------------------------

def test_a_request_without_the_bearer_is_refused_before_any_mcp_handling(hub_two):
    with httpx.Client(trust_env=False) as http:
        response = http.post(hub_two.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "unauthenticated" and body["id"] is None
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", [
    "Bearer not-the-bearer", "Basic abc", "Bearer", "bearer wrong-token", "",
])
def test_a_wrong_or_malformed_bearer_is_refused(hub_two, header):
    with httpx.Client(trust_env=False) as http:
        response = http.post(hub_two.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Authorization": header, "Accept": "application/json, text/event-stream"})
    assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated"


def test_the_relay_token_is_not_the_hub_bearer(hub_two, machine_a):
    with httpx.Client(trust_env=False) as http:
        response = http.post(
            hub_two.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {machine_a.server.prepared.relay_token}",
                     "Accept": "application/json, text/event-stream"})
    assert response.status_code == 401


def test_the_mcp_client_cannot_connect_without_the_bearer(hub_two):
    with pytest.raises(Exception):
        hub_two.run(lambda c: c.list_tools(), bearer=None)


def test_get_and_delete_also_need_the_bearer(hub_two):
    with httpx.Client(trust_env=False) as http:
        for method in ("GET", "DELETE"):
            assert http.request(method, hub_two.url).status_code == 401


# --------------------------------------------------------------------------
# loopback only
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "localhost", "example.com"])
def test_a_non_loopback_bind_is_refused_before_any_state_is_touched(machine_a, tmp_path, host):
    state = tmp_path / "untouched-state"
    config = dataclasses.replace(machine_a.config, hub_host=host, paths=C.StatePaths(state))
    with pytest.raises(C.RelayConfigError, match="loopback"):
        H.HubServer(config, peers=[machine_a.peer], port=0).start()
    assert not state.exists()


def test_bind_loopback_refuses_a_wildcard_address():
    with pytest.raises(C.RelayConfigError, match="loopback"):
        H.bind_loopback("0.0.0.0", 0)


def test_the_hub_listens_on_loopback_only(hub_two):
    host, _ = hub_two.server.address
    assert host == "127.0.0.1"


def test_the_default_hub_bind_is_4711_on_loopback(machine_a):
    assert machine_a.config.bind("hub") == ("127.0.0.1", 4711)


# --------------------------------------------------------------------------
# pause
# --------------------------------------------------------------------------

def test_a_paused_hub_refuses_every_tool_call(hub, machine_a):
    session = machine_a.session(["x", "y"])
    store = L.PauseStore(machine_a.config.paths)
    store.pause("hub", "testing")
    try:
        for tool, arguments in [
            ("list_machines", {}), ("list_sessions", {}),
            ("read_session", {"address": machine_a.address(session)}),
            ("ask_llm", {"endpoint": f"{A}/llm/qwen38-m5-32k-local", "prompt": "ping"}),
        ]:
            error = hub.refused(tool, arguments)
            assert error["code"] == "paused" and error["retryable"] is True, tool
            assert error["details"]["scope"] == "hub" and error["details"]["paused_since"] is not None
        assert machine_a.backend.calls == []
        assert len(hub.run(lambda c: c.list_tools()).tools) == 4  # listing is not a call
    finally:
        store.resume("hub")
    assert hub.ok("list_machines")["machines"][0]["status"] == "up"


def test_a_relay_pause_does_not_pause_the_hub(hub_two, machine_a):
    store = L.PauseStore(machine_a.config.paths)
    store.pause("relay", "testing")
    try:
        assert hub_two.ok("list_machines")["machines"][0]["status"] == "paused"
    finally:
        store.resume("relay")


# --------------------------------------------------------------------------
# singleton and restart
# --------------------------------------------------------------------------

def test_a_second_hub_on_the_same_state_directory_is_refused(hub_two, machine_a):
    with pytest.raises(P.RelayError) as caught:
        make_hub(machine_a.config, [machine_a.peer]).start()
    assert caught.value.code == "already_running"
    assert hub_two.ok("list_machines")  # the first hub is untouched


def test_the_lock_is_released_on_stop(machine_a):
    first = make_hub(machine_a.config, [machine_a.peer]).start()
    first.stop()
    second = make_hub(machine_a.config, [machine_a.peer]).start()
    second.stop()


def test_a_hub_restart_keeps_a_connected_client_working(machine_a):
    """The transport is stateless: no session id dies with the process, so a client needs no restart."""
    first = make_hub(machine_a.config, [machine_a.peer]).start()
    port = first.address[1]
    bearer = first.bearer
    headers = {"Authorization": f"Bearer {bearer}"}

    async def go() -> None:
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
            async with Client(streamable_http_client(first.url, http_client=http)) as client:
                assert (await client.call_tool("list_machines", {})).structured_content["machines"][0]["status"] == "up"
                first.stop()
                second = H.HubServer(machine_a.config, peers=[machine_a.peer], port=port, client_timeout_s=5.0).start()
                try:
                    assert second.bearer == bearer
                    result = await client.call_tool("list_machines", {})
                    assert not result.is_error and result.structured_content["machines"][0]["machine"] == A
                finally:
                    second.stop()

    try:
        asyncio.run(go())
    finally:
        first.stop()


# --------------------------------------------------------------------------
# the relay list
# --------------------------------------------------------------------------

def test_peers_may_be_plain_machine_url_pairs(machine_a):
    # a plain pair carries no credentials: it uses the hub's own state credentials, which are this relay's
    host, port = machine_a.server.address
    config = dataclasses.replace(machine_a.config, relay_host=host, relay_port=port)
    server = make_hub(config, [(A, machine_a.url)]).start()
    try:
        assert Hub(server).ok("list_machines")["machines"][0]["status"] == "up"
    finally:
        server.stop()


def test_a_relay_configured_twice_or_a_bad_entry_is_refused(machine_a, tmp_path):
    with pytest.raises(C.RelayConfigError, match="twice"):
        make_hub(machine_a.config, [machine_a.peer, machine_a.peer]).start()
    with pytest.raises(C.RelayConfigError, match="relay entry"):
        make_hub(machine_a.config, ["m5pro"]).start()


def test_a_non_loopback_relay_url_is_refused(machine_a):
    with pytest.raises(ValueError, match="loopback"):
        make_hub(machine_a.config, [(A, "http://192.168.1.5:4710")]).start()
    # the failed open released the lock
    make_hub(machine_a.config, [machine_a.peer]).start().stop()
