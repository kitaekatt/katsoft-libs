"""Tests for the hub's peer relays (unit A2): ``relay.peers``, the credential store, the peer client,
the hub over local plus peers, and ``llm-kit relay peer export|import``.

Peers are other machines' relays. Every test here reaches them over loopback or a local TLS server; nothing
binds a non-loopback address and nothing touches the real ~/.claude, ~/.codex or ~/.local/state. Windows
behaviour is exercised through the fake ``winacl`` of ``test_relay_windows`` and, on a Windows host only, the
native tests at the end of this file.
"""
from __future__ import annotations

import dataclasses
import http.server
import io
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
import pytest

from llm_kit.relay import client as CL
from llm_kit.relay import config as C
from llm_kit.relay import hub as H
from llm_kit.relay import hub_oauth as HO
from llm_kit.relay import lifecycle as L
from llm_kit.relay import protocol as P
from llm_kit.relay import redact as R
from test_relay_hub import A, B, ENV_JSON, ENABLED_YAML, Hub, Machine, dead_url, make_hub
from test_relay_windows import FakeWinacl

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
SRC = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src"
THIRD = "laptop"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def hub_config(local: Machine, peers: dict[str, str], **kw: Any) -> C.RelayConfig:
    """``local``'s config as a hub machine: its relay's real port, and ``peers`` ({label: url}) configured."""
    return dataclasses.replace(
        local.config, relay_port=local.server.address[1],
        peers=tuple(C.PeerConfig(machine, url) for machine, url in peers.items()), **kw)


def store_credentials(local: Machine, peer: Machine) -> None:
    """Import ``peer``'s relay token and hub bearer into ``local``'s peer store (what ``peer import`` does)."""
    prepared = peer.server.prepared
    L.write_peer_credentials(local.config.paths, peer.label, prepared.relay_token, prepared.hub_bearer)


def serving_hub(config: C.RelayConfig, **kw: Any) -> Iterator[Hub]:
    server = make_hub(config, None, **kw).start()
    try:
        yield Hub(server)
    finally:
        server.stop()


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


class Canned(http.server.BaseHTTPRequestHandler):
    """Answers every POST with ``server.status`` and ``server.body`` (and records it); never logs."""

    def do_POST(self) -> None:  # noqa: N802 -- http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.hits.append((self.path, dict(self.headers)))
        self.send_response(self.server.status)
        for name, value in self.server.extra_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", self.server.content_type)
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)

    do_GET = do_POST  # a followed redirect arrives as a GET

    def log_message(self, *_args: object) -> None:
        pass


class CannedServer:
    def __init__(self, *, status: int = 200, body: bytes = b"", content_type: str = "text/plain",
                 extra_headers: Optional[dict[str, str]] = None, tls: Optional[tuple[Path, Path]] = None) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Canned)
        self.httpd.status, self.httpd.body, self.httpd.content_type = status, body, content_type
        self.httpd.extra_headers = extra_headers or {}
        self.httpd.hits = []
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(tls[0]), str(tls[1]))
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def hits(self) -> list[tuple[str, dict[str, str]]]:
        return self.httpd.hits

    def __enter__(self) -> "CannedServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def status_envelope(machine: str) -> bytes:
    """A valid ``list_machines`` response envelope for ``machine`` (the schema's own up example)."""
    entry = dict(P.load_schema("machine")["examples"][0], machine=machine)
    return json.dumps({"protocol": P.PROTOCOL, "id": None, "ok": True, "result": {"machines": [entry]}}).encode("ascii")


# --------------------------------------------------------------------------
# the hub over this machine's relay and a peer
# --------------------------------------------------------------------------

def test_hub_lists_local_and_peer(machine_a, machine_b):
    store_credentials(machine_a, machine_b)
    session = machine_b.session(["hello from the peer", "an answer"])
    config = hub_config(machine_a, {B: machine_b.url})
    server = make_hub(config, None).start()
    try:
        hub = Hub(server)
        assert server.router.machines == (A, B)
        machines = {m["machine"]: m for m in hub.ok("list_machines")["machines"]}
        assert list(machines) == [A, B] and {m["status"] for m in machines.values()} == {"up"}
        listed = hub.ok("list_sessions")
        assert [s["address"] for s in listed["sessions"]] == [machine_b.address(session)]
        assert {m["machine"] for m in listed["machines"]} == {A, B}
        read = hub.ok("read_session", {"address": machine_b.address(session)})
        assert [t["text"] for t in read["turns"]] == ["hello from the peer", "an answer"]
    finally:
        server.stop()


def test_peer_down_is_machine_unreachable(machine_a, machine_b):
    store_credentials(machine_a, machine_b)
    for url in (dead_url(), "https://localhost:1"):  # nothing listens: refused connection, over http and over https
        config = hub_config(machine_a, {B: url})
        server = make_hub(config, None).start()
        try:
            machines = {m["machine"]: m for m in Hub(server).ok("list_machines")["machines"]}
            assert machines[A]["status"] == "up"
            assert machines[B]["status"] == "unreachable" and machines[B]["error"]["code"] == "machine_unreachable"
            error = Hub(server).refused("read_session", {"address": machine_b.address("c1c1c1c1-0000-4000-8000-000000000001")})
            assert error["code"] == "machine_unreachable"
        finally:
            server.stop()


@pytest.mark.parametrize("status", [502, 503, 504])
def test_peer_502_text_is_machine_unreachable(machine_a, machine_b, status):
    store_credentials(machine_a, machine_b)
    with CannedServer(status=status, body=b"Bad Gateway: dial tcp 127.0.0.1:4710: connection refused") as serve:
        config = hub_config(machine_a, {B: f"http://127.0.0.1:{serve.port}"})
        server = make_hub(config, None).start()
        try:
            machines = {m["machine"]: m for m in Hub(server).ok("list_machines")["machines"]}
        finally:
            server.stop()
        assert machines[B]["status"] == "unreachable" and machines[B]["error"]["code"] == "machine_unreachable"
        assert machines[B]["error"]["retryable"] is True
        assert len(serve.hits) == 1


def test_a_text_error_that_is_not_a_gateway_status_stays_internal(machine_b):
    with CannedServer(status=500, body=b"oops") as serve:
        client = CL.HttpRelayClient(f"http://127.0.0.1:{serve.port}", machine=B, relay_token="t", hub_bearer="b")
        with pytest.raises(P.RelayError) as caught:
            client.status({}, origin=f"operator:{B}")
        assert caught.value.code == "internal"


def test_peer_label_mismatch_unreachable(machine_a, machine_b):
    # the hub machine is B (tower); its peer "m5pro" is configured at B's OWN relay, which reports "tower"
    prepared = machine_b.server.prepared  # the right credentials for that URL, filed under the wrong label
    L.write_peer_credentials(machine_b.config.paths, A, prepared.relay_token, prepared.hub_bearer)
    config = hub_config(machine_b, {A: machine_b.url})
    server = make_hub(config, None).start()
    try:
        hub = Hub(server)
        machines = {m["machine"]: m for m in hub.ok("list_machines")["machines"]}
        error = machines[A]["error"]
        assert machines[A]["status"] == "unreachable" and error["code"] == "internal"
        assert "'m5pro'" in error["message"] and "'tower'" in error["message"]
        assert machines[B]["status"] == "up"
        reach = {m["machine"]: m for m in hub.ok("list_sessions")["machines"]}  # every fan-out verb applies the check
        assert reach[A]["status"] == "unreachable" and reach[A]["error"]["code"] == "internal"
        assert reach[B]["status"] == "up"
    finally:
        server.stop()


def test_local_token_never_sent_to_peer_url(machine_a, monkeypatch):
    peer_host, peer_url = "tower.example.ts.net", "https://tower.example.ts.net:4710"
    L.write_peer_credentials(machine_a.config.paths, B, "peer-relay-token-b", "peer-hub-bearer-b")
    own_token = L.read_token(machine_a.config.paths.relay_token)
    own_bearer = L.read_token(machine_a.config.paths.hub_bearer)
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, dict(request.headers)))
        return httpx.Response(200, content=status_envelope(B))

    real = H.HttpRelayClient

    def recording(url, **kw):
        if url == peer_url:  # the peer never touches the network; the local relay is the real one
            kw["transport"] = httpx.MockTransport(handler)
        return real(url, **kw)

    monkeypatch.setattr(H, "HttpRelayClient", recording)
    config = hub_config(machine_a, {B: peer_url})
    server = make_hub(config, None).start()
    try:
        machines = {m["machine"]: m for m in Hub(server).ok("list_machines")["machines"]}
    finally:
        server.stop()
    assert machines[B]["status"] == "up"
    assert len(seen) == 1 and seen[0][0] == peer_host
    headers = seen[0][1]
    assert headers[P.HEADER_RELAY_TOKEN.lower()] == "peer-relay-token-b"
    assert headers[P.HEADER_AUTHORIZATION.lower()] == f"{P.BEARER_SCHEME} peer-hub-bearer-b"
    assert own_token not in json.dumps(headers) and own_bearer not in json.dumps(headers)


def test_a_peer_url_without_its_own_credentials_is_refused_not_filled_in(machine_a):
    for peer in (H.RelayPeer(B, "https://tower.example.ts.net:4710"),
                 H.RelayPeer(B, "https://tower.example.ts.net:4710", relay_token="only-a-token"),
                 H.RelayPeer(B, "https://tower.example.ts.net:4710", hub_bearer="only-a-bearer")):
        with pytest.raises(C.RelayConfigError, match="its own relay token and hub bearer"):
            make_hub(machine_a.config, [(A, "http://127.0.0.1:4710"), peer]).start()
    make_hub(hub_config(machine_a, {}), [machine_a.peer]).start().stop()  # the failed starts released the lock


def test_non_loopback_http_peer_refused(machine_a):
    with pytest.raises(ValueError, match="https"):
        CL.HttpRelayClient("http://192.168.1.10:4710", machine=B, relay_token="t", hub_bearer="b")
    with pytest.raises(ValueError, match="https"):
        CL.HttpRelayClient("http://tower.example.ts.net:4710", machine=B, relay_token="t", hub_bearer="b")
    with pytest.raises(ValueError, match="own hub bearer"):
        CL.HttpRelayClient("https://tower.example.ts.net:4710", machine=B, relay_token="t")
    with pytest.raises(ValueError, match="loopback"):
        CL.HttpRelayClient("https://127.0.0.1:4710", machine=B, relay_token="t", hub_bearer="b")
    CL.HttpRelayClient("https://tower.example.ts.net:4710", machine=B, relay_token="t", hub_bearer="b").close()
    # the same refusal at configuration time
    for url, message in (("http://tower.example.ts.net:4710", "https"), ("ftp://tower:4710", "https"),
                         ("https://tower.example.ts.net/v1", "origin only"), ("https://u:p@tower:4710", "user info")):
        with pytest.raises(C.RelayConfigError, match=message):
            load_with_peers(machine_a, f"relay:\n  peers:\n    - {{machine: tower, url: '{url}'}}\n")


def test_peer_client_no_redirect_no_proxy(machine_b, monkeypatch):
    with CannedServer(status=200, body=b"must not be reached") as landing, CannedServer() as proxy:
        with CannedServer(status=302, extra_headers={"Location": f"http://127.0.0.1:{landing.port}/v1/status"}) as origin:
            for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
                monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)
            client = CL.HttpRelayClient(f"http://127.0.0.1:{origin.port}", machine=B, relay_token="t", hub_bearer="b")
            with pytest.raises(P.RelayError) as caught:
                client.status({}, origin=f"operator:{B}")
            client.close()
            assert caught.value.code == "internal"  # a redirect is not an envelope, and is not followed
            assert [path for path, _ in origin.hits] == ["/v1/status"]
            assert landing.hits == [] and proxy.hits == []


def test_peer_connect_timeout_is_three_seconds(machine_b, monkeypatch):
    client = CL.HttpRelayClient("https://tower.example.ts.net:4710", machine=B, relay_token="t", hub_bearer="b",
                                timeout_s=11.0)
    recorded: list[Any] = []

    def post(url, **kw):
        recorded.append(kw["timeout"])
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(client._http, "post", post)
    with pytest.raises(P.RelayError) as caught:
        client.status({}, origin=f"operator:{B}")
    assert caught.value.code == "machine_unreachable"
    assert recorded[0].connect == CL.PEER_CONNECT_TIMEOUT_S == 3.0 and recorded[0].read == 11.0
    loopback = CL.HttpRelayClient("http://127.0.0.1:4710", machine=B, relay_token="t", timeout_s=11.0)
    monkeypatch.setattr(loopback._http, "post", post)
    with pytest.raises(P.RelayError):
        loopback.status({}, origin=f"operator:{B}")
    assert recorded[1] == 11.0  # a loopback relay keeps the plain call timeout


def test_the_hub_builds_every_configured_peer_with_the_short_connect_timeout(machine_a, machine_b):
    store_credentials(machine_a, machine_b)
    server = make_hub(hub_config(machine_a, {B: machine_b.url}), None)
    clients = server._build_clients("local-token", "local-bearer")
    try:
        assert [c.machine for c in clients] == [A, B]
        assert [c._connect_timeout_s for c in clients] == [None, CL.PEER_CONNECT_TIMEOUT_S]
        assert clients[1]._token == L.read_token(machine_a.config.paths.peer_relay_token(B))
    finally:
        for client in clients:
            client.close()


# --------------------------------------------------------------------------
# TLS is verified
# --------------------------------------------------------------------------

@pytest.fixture
def self_signed(tmp_path: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl to make a test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    done = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
        capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"openssl could not make a test certificate: {done.stderr[-200:]}")
    return cert, key


def test_peer_tls_verified(self_signed):
    cert, key = self_signed
    with CannedServer(status=200, body=status_envelope(B), content_type=P.CONTENT_TYPE, tls=(cert, key)) as serve:
        url = f"https://localhost:{serve.port}"
        # a certificate that no trust root vouches for is refused, and nothing reaches the server
        client = CL.HttpRelayClient(url, machine=B, relay_token="t", hub_bearer="b")
        with pytest.raises(P.RelayError) as caught:
            client.list_machines({}, origin="claude.ai/owner")
        client.close()
        assert caught.value.code == "machine_unreachable" and "CERTIFICATE_VERIFY_FAILED" in caught.value.message
        assert serve.hits == []
        # control: the same server answers a client that trusts this certificate, so verification was the refusal
        trusting = CL.HttpRelayClient(url, machine=B, relay_token="t", hub_bearer="b",
                                      transport=httpx.HTTPTransport(verify=ssl.create_default_context(cafile=str(cert))))
        assert trusting.list_machines({}, origin="claude.ai/owner")["machines"][0]["machine"] == B
        trusting.close()
        assert len(serve.hits) == 1


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def write_env(tmp: Path, *, disabled: tuple[str, ...] = ()) -> Path:
    env = {"machines": {
        "m5pro.local": {"os": "macos", "tailscale_hostname": "m5pro"},
        "tower.local": {"os": "linux", "tailscale_hostname": "tower"},
        "laptop.local": {"os": "macos", "tailscale_hostname": THIRD, **({"disabled": True} if THIRD in disabled else {})},
        "bare.local": {"os": "linux"},
    }}
    path = tmp / "peers-env.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    return path


def load_with_peers(machine: Machine, yaml_text: str, *, disabled: tuple[str, ...] = ()) -> C.RelayConfig:
    tmp = machine.root / "cfg"
    tmp.mkdir(exist_ok=True)
    config_path = tmp / "relay.yaml"
    config_path.write_text(ENABLED_YAML.rstrip("\n").replace("enabled: true\n", "enabled: true\n", 1) + "\n" + yaml_text,
                           encoding="utf-8")
    return C.load_config(env_json=write_env(tmp, disabled=disabled), environ={}, hostname=lambda: f"{machine.label}.local",
                         config_path=config_path, state_root=machine.root / "state")


def test_peers_are_nested_in_the_relay_block_and_parsed(machine_a):
    config = load_with_peers(machine_a, "relay:\n  port: 4710\n  peers:\n    - {machine: tower, url: 'https://Tower.Example.ts.net:4710/'}\n"
                                        "    - machine: laptop\n      url: http://127.0.0.1:9\n")
    assert config.peers == (C.PeerConfig("tower", "https://tower.example.ts.net:4710"), C.PeerConfig("laptop", "http://127.0.0.1:9"))
    assert config.relay_port == 4710


def test_peers_config_refusals(machine_a):
    def refused(entries: str, match: str, **kw: Any) -> None:
        with pytest.raises(C.RelayConfigError, match=match):
            load_with_peers(machine_a, f"relay:\n  peers:\n{entries}", **kw)

    ok = "https://x.example.ts.net:4710"
    refused(f"    - {{machine: ghost, url: '{ok}'}}\n", "not a machine in the fleet registry")            # unknown
    refused(f"    - {{machine: bare, url: '{ok}'}}\n", "not a machine in the fleet registry")             # no tailscale_hostname
    refused(f"    - {{machine: laptop, url: '{ok}'}}\n", "is disabled", disabled=(THIRD,))                # disabled
    refused(f"    - {{machine: m5pro, url: '{ok}'}}\n", "is this machine")                               # self
    refused(f"    - {{machine: tower, url: '{ok}'}}\n    - {{machine: tower, url: '{ok}'}}\n", "configured twice")  # duplicate
    refused(f"    - {{machine: tower, url: '{ok}', token: x}}\n", "unknown keys: token")
    refused(f"    - {{machine: Tower, url: '{ok}'}}\n", "is not a machine label")
    refused("    - tower\n", "is not a mapping")
    refused("    - {machine: tower}\n", "is not a non-empty string")
    with pytest.raises(C.RelayConfigError, match="'relay.peers' is not a list"):
        load_with_peers(machine_a, "relay:\n  peers: tower\n")
    with pytest.raises(C.RelayConfigError, match="unknown keys: peers"):  # never a new top-level key
        load_with_peers(machine_a, f"peers:\n  - {{machine: tower, url: '{ok}'}}\n")
    with pytest.raises(C.RelayConfigError, match="'relay' has unknown keys: accept_from"):
        load_with_peers(machine_a, "relay:\n  accept_from: []\n")


def test_the_opt_in_reader_accepts_peers_in_the_relay_block(machine_a):
    config_path = machine_a.root / "config" / "relay.yaml"
    config_path.write_text(ENABLED_YAML + "relay:\n  peers:\n    - {machine: tower, url: 'https://t.example.ts.net:4710'}\n",
                          encoding="utf-8")
    state = C.enablement(config_path)
    assert (state.relay, state.hub) == (True, True)


def test_no_peers_is_todays_hub(machine_a, capsys):
    assert load_with_peers(machine_a, "").peers == () and load_with_peers(machine_a, "relay:\n  peers: []\n").peers == ()
    config = hub_config(machine_a, {})
    server = make_hub(config, None)
    clients = server._build_clients("t", "b")
    assert [(c.machine, c.url) for c in clients] == [(A, f"http://127.0.0.1:{machine_a.server.address[1]}")]
    clients[0].close()
    assert not config.paths.peers_dir.exists()  # nothing peer-related is created or read
    started = make_hub(config, None).start()
    try:
        assert started.router.machines == (A,)
        assert [m["machine"] for m in Hub(started).ok("list_machines")["machines"]] == [A]
    finally:
        started.stop()
    assert not config.paths.peers_dir.exists()


# --------------------------------------------------------------------------
# the credential store
# --------------------------------------------------------------------------

def test_peer_credential_paths_are_secret_paths(tmp_path):
    paths = C.StatePaths(tmp_path / "llm-kit" / "relay")
    assert paths.peer_relay_token(B) == paths.root / "peers" / B / "relay.token"
    assert paths.peer_hub_bearer(B) == paths.root / "peers" / B / "hub.bearer"
    for path in (paths.peer_relay_token(B), paths.peer_hub_bearer(B)):
        assert R.is_secret_path_use("Read", {"file_path": str(path)}) is True
        assert R.is_secret_path_use("Bash", f"cat {path}") is True
    for bad in ("../x", "Tower", "a/b", ""):
        with pytest.raises(C.RelayConfigError, match="not a machine label"):
            paths.peer_dir(bad)


def test_missing_peer_credentials_stop_the_hub_naming_the_import_command(machine_a, machine_b):
    config = hub_config(machine_a, {B: machine_b.url})
    with pytest.raises(C.RelayConfigError, match=r"`llm-kit relay peer import tower`"):
        make_hub(config, None).start()
    # one file missing is named too, and the failed start released the singleton lock
    L.write_peer_credentials(config.paths, B, "peer-token", "peer-bearer")
    config.paths.peer_hub_bearer(B).unlink()
    with pytest.raises(C.RelayConfigError, match=r"no hub bearer for peer 'tower'.*llm-kit relay peer import tower"):
        make_hub(config, None).start()
    L.write_peer_credentials(config.paths, B, "peer-token", "peer-bearer")
    make_hub(config, None).start().stop()


@POSIX_ONLY
def test_peer_dirs_validated(machine_a, machine_b, tmp_path):
    paths = machine_a.config.paths
    store_credentials(machine_a, machine_b)
    assert stat.S_IMODE(paths.peers_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.peer_dir(B).stat().st_mode) == 0o700
    assert L.peer_credentials(paths, B) == (machine_b.server.prepared.relay_token, machine_b.server.prepared.hub_bearer)
    for directory, kind in ((paths.peers_dir, "peers directory"), (paths.peer_dir(B), "peer directory")):
        directory.chmod(0o755)  # a loose directory is refused, never repaired
        with pytest.raises(C.RelayConfigError, match=f"{kind} .* has mode 0755"):
            L.peer_credentials(paths, B)
        with pytest.raises(C.RelayConfigError, match=f"{kind} .* has mode 0755"):
            make_hub(hub_config(machine_a, {B: machine_b.url}), None).start()
        with pytest.raises(C.RelayConfigError, match=f"{kind} .* has mode 0755"):
            L.write_peer_credentials(paths, B, "a", "b")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
        directory.chmod(0o700)
    for file in (paths.peer_relay_token(B), paths.peer_hub_bearer(B)):
        file.chmod(0o644)
        with pytest.raises(C.RelayConfigError, match="credential file .* has mode 0644"):
            L.peer_credentials(paths, B)
        file.chmod(0o600)
    # a peers directory that is a link elsewhere is refused
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(paths.peers_dir), str(elsewhere))
    paths.peers_dir.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(C.RelayConfigError, match="symbolic link"):
        L.peer_credentials(paths, B)
    with pytest.raises(C.RelayConfigError, match="symbolic link"):
        L.write_peer_credentials(paths, B, "a", "b")


def test_peer_credentials_are_private_on_nt_through_winacl(monkeypatch, tmp_path):
    acl = FakeWinacl()
    monkeypatch.setattr(L, "_on_nt", lambda: True)
    monkeypatch.setattr(L, "winacl", acl)
    paths = C.StatePaths(tmp_path / "relay")
    L.write_peer_credentials(paths, B, "peer-relay-token", "peer-hub-bearer")
    provisioned = {p: (d, size) for p, d, size in acl.of("provision")}
    assert provisioned[str(paths.root)][0] is True                        # state root, then both peer directories
    assert provisioned[str(paths.peers_dir)][0] is True and provisioned[str(paths.peer_dir(B))][0] is True
    temps = [p for p in provisioned if Path(p).name.startswith(".relay.token.") or Path(p).name.startswith(".hub.bearer.")]
    assert len(temps) == 2 and all(provisioned[t] == (False, 0) for t in temps)  # a temp file is private before its first byte
    validated = {p for p, _, _ in acl.of("validate")}
    assert {str(paths.peer_relay_token(B)), str(paths.peer_hub_bearer(B)), str(paths.peers_dir), str(paths.peer_dir(B))} <= validated
    assert paths.peer_relay_token(B).read_text(encoding="ascii") == "peer-relay-token\n"
    acl.events.clear()
    assert L.peer_credentials(paths, B) == ("peer-relay-token", "peer-hub-bearer")
    assert {p for p, _, _ in acl.of("validate")} == {str(paths.peers_dir), str(paths.peer_dir(B)),
                                                    str(paths.peer_relay_token(B)), str(paths.peer_hub_bearer(B))}


# --------------------------------------------------------------------------
# the hub's exclusive bind on nt, and the OAuth store's private write
# --------------------------------------------------------------------------

class FakeSocket:
    def __init__(self, *_a: object, **_k: object) -> None:
        self.options: list[tuple[int, int, int]] = []
        self.bound: Any = None
        self.listening = False

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: Any) -> None:
        self.bound = address

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 4321)

    def listen(self, _backlog: int) -> None:
        self.listening = True

    def close(self) -> None:
        pass


@pytest.mark.parametrize("nt_flag", [True, False])
def test_hub_bind_exclusive_on_nt(monkeypatch, nt_flag):
    made: list[FakeSocket] = []
    monkeypatch.setattr(H.socket, "socket", lambda *a, **k: made.append(FakeSocket()) or made[-1])
    monkeypatch.setattr(H, "_on_nt", lambda: nt_flag)
    if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    H.bind_loopback("127.0.0.1", 0)
    expected = socket.SO_EXCLUSIVEADDRUSE if nt_flag else socket.SO_REUSEADDR  # REUSEADDR would allow a second bind on nt
    assert made[0].options == [(socket.SOL_SOCKET, expected, 1)] and made[0].listening


def test_hub_oauth_store_is_written_through_the_private_file_helpers(monkeypatch, tmp_path):
    acl = FakeWinacl()
    monkeypatch.setattr(L, "_on_nt", lambda: True)
    monkeypatch.setattr(L, "winacl", acl)
    path = tmp_path / "hub-oauth.json"
    store = HO.TokenStore(path)
    store._clients["client-1"] = {"redirect_uris": []}
    store._save_locked()
    assert json.loads(path.read_text(encoding="utf-8"))["clients"] == {"client-1": {"redirect_uris": []}}
    provisioned = [(p, size) for p, d, size in acl.of("provision")]
    temps = [(p, size) for p, size in provisioned if Path(p).name.startswith(".hub-oauth.json.")]
    assert len(temps) == 1 and temps[0][1] == 0                      # private before the first byte
    assert str(path) in {p for p, _, _ in acl.of("validate")}        # and validated at its final path
    reloaded = HO.TokenStore(path)                                    # a read validates it again
    assert reloaded.client("client-1") == {"redirect_uris": []}


# --------------------------------------------------------------------------
# the CLI: peer export, peer import, status peers
# --------------------------------------------------------------------------

@pytest.fixture
def cli_machines(tmp_path: Path, monkeypatch) -> Iterator[Any]:
    """Two machines' CLI arguments over one env.json: the exporter (tower) and the hub machine (m5pro)."""
    from types import SimpleNamespace

    env = write_env(tmp_path)

    def args(label: str, peers_yaml: str = "") -> list[str]:
        config = tmp_path / label / "relay.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(ENABLED_YAML + peers_yaml, encoding="utf-8")
        return ["--env-json", str(env), "--config", str(config), "--state-dir", str(tmp_path / label / "state")]

    monkeypatch.setattr(socket, "gethostname", lambda: "tower.local")
    yield SimpleNamespace(tmp=tmp_path, args=args, env=env)


def cli(argv: list[str], capsys) -> Any:
    from llm_kit import cli as entry
    from test_cli_harness import run

    return run(entry.main, ["relay", *argv], capsys)


def as_host(monkeypatch, label: str) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: f"{label}.local")


def prepare(machine_args: list[str]) -> C.RelayConfig:
    """What a relay's start does to the state: both credentials, so ``peer export`` has something to print."""
    config = C.load_config(env_json=Path(machine_args[1]), config_path=Path(machine_args[3]), state_root=Path(machine_args[5]))
    L.ensure_state_dir(config.paths)
    L.ensure_token(config.paths.relay_token)
    L.ensure_token(config.paths.hub_bearer)
    return config


def test_peer_export_refuses_tty(cli_machines, capsys, monkeypatch):
    from llm_kit.cli import relay as relay_cli

    exporter = cli_machines.args("tower")
    config = prepare(exporter)
    token = L.read_token(config.paths.relay_token)
    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: True)
    out = cli(["peer", "export", *exporter], capsys)
    assert out.code == 2 and "refusing to print relay credentials to a terminal" in out.err
    assert out.out == "" and token not in out.err
    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: False)
    out = cli(["peer", "export", *exporter], capsys)
    assert out.code == 0
    assert json.loads(out.out) == {
        "format": "llm-kit-relay-peer/1", "machine": "tower", "relay_token": token,
        "hub_bearer": L.read_token(config.paths.hub_bearer)}


@POSIX_ONLY
def test_peer_export_refuses_a_real_pty(cli_machines):
    import pty

    exporter = cli_machines.args("tower")
    config = prepare(exporter)
    token = L.read_token(config.paths.relay_token)
    master, slave = pty.openpty()
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
               _BOOTSTRAP_GUARD_SKIP_REEXEC="1")
    code = ("import socket, sys; socket.gethostname = lambda: 'tower.local'; "
            "from llm_kit.cli import main; sys.exit(main(['relay', 'peer', 'export'] + sys.argv[1:]))")
    try:
        done = subprocess.run([sys.executable, "-c", code, *exporter], stdout=slave, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, env=env, text=True, timeout=60)
        os.close(slave)
        shown = os.read(master, 65536).decode("utf-8", "replace")
    finally:
        os.close(master)
    assert done.returncode == 2 and "refusing to print relay credentials to a terminal" in done.stderr
    assert token not in shown and token not in done.stderr


def test_peer_import_writes_private(cli_machines, capsys, monkeypatch):
    exporter, hub_args = cli_machines.args("tower"), cli_machines.args("m5pro", "relay:\n  peers:\n    - {machine: tower, url: 'https://t.example.ts.net:4710'}\n")
    exported = cli(["peer", "export", *exporter], capsys) if prepare(exporter) else None
    assert exported.code == 0
    as_host(monkeypatch, "m5pro")
    written: list[Path] = []
    real = L.write_private_file
    monkeypatch.setattr(L, "write_private_file", lambda path, text: (written.append(path), real(path, text))[1])
    monkeypatch.setattr(sys, "stdin", io.StringIO(exported.out))
    out = cli(["peer", "import", "tower", *hub_args], capsys)
    assert (out.code, out.err) == (0, "")
    config = C.load_config(env_json=cli_machines.env, config_path=Path(hub_args[3]), state_root=Path(hub_args[5]),
                           hostname=lambda: "m5pro.local")
    paths = config.paths
    assert json.loads(out.out) == {"imported": "tower", "configured": True,
                                   "files": [str(paths.peer_relay_token("tower")), str(paths.peer_hub_bearer("tower"))]}
    assert written == [paths.peer_relay_token("tower"), paths.peer_hub_bearer("tower")]  # through write_private_file, nothing else
    sent = json.loads(exported.out)
    assert L.peer_credentials(paths, "tower") == (sent["relay_token"], sent["hub_bearer"])
    assert sent["relay_token"] not in out.out + out.err and sent["hub_bearer"] not in out.out + out.err
    if os.name != "nt":
        assert stat.S_IMODE(paths.peer_relay_token("tower").stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.peer_hub_bearer("tower").stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.peers_dir.stat().st_mode) == stat.S_IMODE(paths.peer_dir("tower").stat().st_mode) == 0o700
    # a second import replaces them atomically
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(dict(sent, relay_token="replacement-token"))))
    assert cli(["peer", "import", "tower", *hub_args], capsys).code == 0
    assert L.read_token(paths.peer_relay_token("tower")) == "replacement-token"


def test_peer_import_requires_the_machine_to_match(cli_machines, capsys, monkeypatch):
    exporter, hub_args = cli_machines.args("tower"), cli_machines.args("m5pro")
    prepare(exporter)
    exported = cli(["peer", "export", *exporter], capsys).out
    sent = json.loads(exported)
    as_host(monkeypatch, "m5pro")

    def attempt(machine: str, stdin: str, match: str, code: int = 2) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        out = cli(["peer", "import", machine, *hub_args], capsys)
        assert out.code == code and match in out.err, out.err
        assert sent["relay_token"] not in out.err and sent["hub_bearer"] not in out.err
        assert not (Path(hub_args[5]) / "peers").exists()  # nothing was stored

    attempt("laptop", exported, "are for machine 'tower', not 'laptop'")      # the object names another machine
    attempt("m5pro", exported, "is this machine")
    attempt("ghost", exported, "not a machine in the fleet registry")
    attempt("Tower", exported, "not a machine label")
    attempt("tower", "not json", "not a JSON object")
    attempt("tower", json.dumps([1]), "is not a llm-kit-relay-peer/1 object")
    attempt("tower", json.dumps(dict(sent, extra="x")), "is not a llm-kit-relay-peer/1 object")
    attempt("tower", json.dumps(dict(sent, relay_token="two words")), "single printable ASCII token")
    attempt("tower", json.dumps(dict(sent, hub_bearer="")), "single printable ASCII token")
    attempt("tower", "x" * 5000, "too large")
    from llm_kit.cli import relay as relay_cli

    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: stream is sys.stdin)
    attempt("tower", exported, "pipe it in")
    # a disabled machine cannot be a peer
    env = json.loads(cli_machines.env.read_text(encoding="utf-8"))
    env["machines"]["laptop.local"]["disabled"] = True
    cli_machines.env.write_text(json.dumps(env), encoding="utf-8")
    monkeypatch.setattr(relay_cli, "_is_tty", lambda stream: False)
    attempt("laptop", json.dumps(dict(sent, machine="laptop")), "is disabled")


def test_status_lists_peers_without_secrets(cli_machines, capsys, monkeypatch):
    as_host(monkeypatch, "m5pro")
    peers = "relay:\n  peers:\n    - {machine: tower, url: 'https://t.example.ts.net:4710'}\n    - {machine: laptop, url: 'https://l.example.ts.net:4710'}\n"
    hub_args = cli_machines.args("m5pro", peers)
    config = C.load_config(env_json=cli_machines.env, config_path=Path(hub_args[3]), state_root=Path(hub_args[5]),
                           hostname=lambda: "m5pro.local")
    L.write_peer_credentials(config.paths, "tower", "tower-secret-token", "tower-secret-bearer")
    out = cli(["status", "--offline", *hub_args], capsys)
    assert out.code == 0
    shown = json.loads(out.out)
    assert shown["peers"] == [
        {"machine": "tower", "url": "https://t.example.ts.net:4710", "credentials": {"relay_token": "stored", "hub_bearer": "stored"}},
        {"machine": "laptop", "url": "https://l.example.ts.net:4710", "credentials": {"relay_token": "missing", "hub_bearer": "missing"}},
    ]
    assert "tower-secret" not in out.out + out.err
    # no peers configured: no `peers` key at all
    out = cli(["status", "--offline", *cli_machines.args("m5pro")], capsys)
    assert "peers" not in json.loads(out.out)


# --------------------------------------------------------------------------
# native Windows (skipped elsewhere)
# --------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="native Windows ACLs")
def test_native_peer_credentials_provisioned_and_loose_dirs_refused(tmp_path):
    from llm_kit.relay import winacl

    paths = C.StatePaths(tmp_path / "relay")
    L.write_peer_credentials(paths, B, "peer-relay-token", "peer-hub-bearer")
    for path in (paths.root, paths.peers_dir, paths.peer_dir(B), paths.peer_relay_token(B), paths.peer_hub_bearer(B)):
        winacl.validate(path)
    assert L.peer_credentials(paths, B) == ("peer-relay-token", "peer-hub-bearer")
    # an Everyone grant on the peers directory is refused by every reader and writer, and nothing repairs it
    done = subprocess.run(["icacls", str(paths.peers_dir), "/grant", "*S-1-1-0:(R)"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    with pytest.raises(C.RelayConfigError, match="grants access to"):
        L.peer_credentials(paths, B)
    with pytest.raises(C.RelayConfigError, match="grants access to"):
        L.write_peer_credentials(paths, B, "a", "b")
    subprocess.run(["icacls", str(paths.peers_dir), "/remove:g", "*S-1-1-0"], capture_output=True, text=True, check=True)
    assert L.peer_credentials(paths, B) == ("peer-relay-token", "peer-hub-bearer")
    # a peer credential file a person made by hand (inherited ACL, not protected) is refused
    paths.peer_hub_bearer(B).unlink()
    paths.peer_hub_bearer(B).write_text("hand-made\n", encoding="ascii")
    with pytest.raises(C.RelayConfigError, match="unprotected DACL"):
        L.peer_credentials(paths, B)
