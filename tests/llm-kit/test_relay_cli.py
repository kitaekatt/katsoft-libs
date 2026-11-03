"""Golden tests for ``llm-kit relay`` (unit S3).

The CLI runs in process against a real relay served on an ephemeral loopback
port over tmp state; one test runs ``python -m llm_kit.cli relay serve`` as a
child process and stops it with SIGTERM. Every path is under ``tmp_path``
(``--env-json``, ``--config``, ``--state-dir``, HOME): nothing touches the
real ~/.claude, ~/.codex or ~/.local/state.

Normalized fields, and nothing else: epoch times (``now``, ``*_at``,
``paused_since``) -> ``<t>``, the ephemeral port -> ``<port>``, the tmp
directory -> ``<T>``, the child's pid -> ``<pid>``.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from llm_kit.completion.types import LLMResponse
from llm_kit.registry import consent
from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import protocol as P
from llm_kit.relay import server as S
from llm_kit.relay.client import HttpRelayClient
from test_cli_harness import REPO, home, run  # noqa: F401 -- home is a fixture

LABEL = "testbox"
SESSION = "c1c1c1c1-0000-4000-8000-000000000001"
LIB = REPO / "packages" / "katsoft-llm-kit" / "src"
CONTRACTS = REPO / "packages" / "katsoft-contracts" / "src"


class Backend:
    name = "fake"

    def complete(self, system, user, *, model, options=None):
        return LLMResponse(text=f"echo: {user}", model=model, finish_reason="stop", input_tokens=1, output_tokens=1)

    def classify_halt(self, exc):  # pragma: no cover - protocol member
        return None


def scrub(value: Any, tmp: Path, port: int = 0) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("now", "paused_since") or key.endswith("_at"):
                out[key] = None if item is None else "<t>"
            elif key == "library_version":
                continue
            else:
                out[key] = scrub(item, tmp, port)
        return out
    if isinstance(value, list):
        return [scrub(v, tmp, port) for v in value]
    if isinstance(value, str):
        text = value.replace(str(tmp), "<T>")
        return text.replace(f":{port}", ":<port>") if port else text
    return value


@pytest.fixture
def paths(tmp_path: Path, home) -> SimpleNamespace:
    env_json = tmp_path / "env.json"
    env_json.write_text(json.dumps({"machines": {socket.gethostname(): {"tailscale_hostname": LABEL}}}), encoding="utf-8")
    config = tmp_path / "config" / "relay.yaml"
    state = tmp_path / "state"
    claude, codex = tmp_path / "claude-home", tmp_path / "codex-home"
    (claude / "sessions").mkdir(parents=True)
    (claude / "projects").mkdir(parents=True)
    return SimpleNamespace(tmp=tmp_path, env_json=env_json, config=config, state=state,
                           roots=D.DiscoveryRoots(claude, codex),
                           args=["--env-json", str(env_json), "--config", str(config), "--state-dir", str(state)])


@pytest.fixture
def relay(paths) -> Iterator[SimpleNamespace]:
    config = C.load_config(env_json=paths.env_json, environ={}, config_path=paths.config, state_root=paths.state)
    server = S.RelayServer(
        config, port=0, roots=paths.roots, probe=D.PsutilProbe(), poll_interval_s=0.05, maintain_interval_s=3600,
        backend_factory=lambda entry: SimpleNamespace(backend=Backend(), model=entry.model),
        policy_loader=lambda scope: consent.policy(scope, path=paths.tmp / "consent.json"),
    ).start()
    port = server.address[1]
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(f"relay:\n  port: {port}\n", encoding="utf-8")
    client = HttpRelayClient(server.url, machine=LABEL, relay_token=server.prepared.relay_token)
    try:
        yield SimpleNamespace(server=server, port=port, client=client)
    finally:
        server.stop()


def cli(argv: list[str], capsys) -> Any:
    from llm_kit import cli as entry

    return run(entry.main, ["relay", *argv], capsys)


def mint(relay, verb: str, targets=()) -> str:
    return relay.client.call("grant_mint", {
        "session": f"{LABEL}/claude/{SESSION}", "verb": verb, "targets": list(targets), "expires_in_s": 60,
        "uses": 1, "origin_proof": {"type": "none"},
    }, origin=f"direct:{LABEL}/claude/{SESSION}")["grant"]["grant_id"]


PAUSE_CLEAR = {"paused": False, "paused_since": None, "reason": None}


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------

def test_the_group_needs_a_verb_and_lists_them(paths, capsys):
    out = cli([], capsys)
    assert out.code == 2 and "required: verb" in out.err
    out = cli(["--help"], capsys)
    assert out.code == 0 and "usage: llm-kit relay" in out.out
    for verb in ("serve", "pause", "resume", "status", "client"):
        assert verb in out.out


def test_the_client_offers_the_eight_client_verbs_and_no_mint(paths, capsys):
    out = cli(["client", "--help"], capsys)
    assert out.code == 0
    from llm_kit.cli import relay as relay_cli

    listed = list(relay_cli.CLIENT_VERBS)
    assert listed == ["machines", "sessions", "read", "ask", "send", "replies", "questions", "answer"]
    assert sorted(P.CLIENT_VERBS) == sorted(listed)
    for verb in listed:
        assert f"    {verb} " in out.out, verb
    assert "mint" not in out.out
    out = cli(["client", "grant_mint", "--session", SESSION], capsys)
    assert out.code == 2 and "invalid choice: 'grant_mint'" in out.err


# --------------------------------------------------------------------------
# operator verbs
# --------------------------------------------------------------------------

def test_status_pause_and_resume_through_the_relay(relay, paths, capsys):
    out = cli(["status", *paths.args], capsys)
    assert (out.code, out.err) == (0, "")
    assert scrub(json.loads(out.out), paths.tmp) == {
        "protocol": P.PROTOCOL, "machine": LABEL, "now": "<t>", "registrations": 0,
        "pause": {"relay": PAUSE_CLEAR, "hub": PAUSE_CLEAR},
    }
    out = cli(["pause", "--reason", "owner away", *paths.args], capsys)
    assert out.code == 0 and scrub(json.loads(out.out), paths.tmp) == {"pause": {
        "relay": {"paused": True, "paused_since": "<t>", "reason": "owner away"}, "hub": PAUSE_CLEAR}}
    out = cli(["pause", "--scope", "hub", *paths.args], capsys)
    assert json.loads(out.out)["pause"]["hub"]["paused"] is True
    out = cli(["resume", *paths.args], capsys)
    assert out.code == 0 and scrub(json.loads(out.out), paths.tmp) == {"pause": {
        "relay": PAUSE_CLEAR, "hub": {"paused": True, "paused_since": "<t>", "reason": None}}}
    rows = [r for r in relay.server.mailbox.audit_rows() if r["event"] == "call"]
    assert [(r["origin"], r["verb"]) for r in rows] == [(f"operator:{LABEL}", v) for v in ("status", "pause", "pause", "resume")]


def test_offline_pause_writes_the_state_file_without_a_relay(paths, capsys):
    out = cli(["pause", "--offline", "--reason", "maintenance", *paths.args], capsys)
    assert out.code == 0 and scrub(json.loads(out.out), paths.tmp) == {"via": "state", "pause": {
        "relay": {"paused": True, "paused_since": "<t>", "reason": "maintenance"}, "hub": PAUSE_CLEAR}}
    out = cli(["status", "--offline", *paths.args], capsys)
    assert scrub(json.loads(out.out), paths.tmp) == {"via": "state", "machine": LABEL, "pause": {
        "relay": {"paused": True, "paused_since": "<t>", "reason": "maintenance"}, "hub": PAUSE_CLEAR}}
    assert (paths.state / "pause.json").is_file()


def test_status_without_a_relay(paths, capsys):
    out = cli(["status", *paths.args], capsys)
    assert out.code == 2 and out.out == ""
    error = json.loads(out.err)["error"]
    assert error["kind"] == "configuration" and "no relay has run on this state directory" in error["message"]


def test_status_after_the_relay_stopped_is_unreachable(relay, paths, capsys):
    relay.server.stop()
    out = cli(["status", *paths.args], capsys)
    assert out.code == 1 and out.out == ""
    error = json.loads(out.err)["error"]
    assert (error["code"], error["retryable"]) == ("machine_unreachable", True)


# --------------------------------------------------------------------------
# client verbs
# --------------------------------------------------------------------------

def test_a_client_verb_without_a_grant_is_refused(relay, paths, capsys):
    out = cli(["client", "sessions", "--session", SESSION, *paths.args], capsys)
    assert (out.code, out.out) == (1, "")
    assert json.loads(out.err) == {"error": {
        "code": "grant_required", "retryable": False,
        "message": f"no grant of {LABEL}/claude/{SESSION} covers list_sessions",
    }}


def test_client_sessions_and_ask_with_grants(relay, paths, capsys):
    mint(relay, "list_sessions")
    out = cli(["client", "sessions", "--session", SESSION, *paths.args], capsys)
    assert out.code == 0 and json.loads(out.out) == {"sessions": [], "machines": [{"machine": LABEL, "status": "up"}]}

    endpoint = f"{LABEL}/llm/qwen38-m5-32k-local"
    grant = mint(relay, "ask_llm", [endpoint])
    out = cli(["client", "ask", "qwen38-m5-32k-local", "--prompt", "hello", "--session", SESSION,
               "--grant", grant, *paths.args], capsys)
    assert out.code == 0 and json.loads(out.out) == {
        "endpoint": endpoint, "text": "echo: hello", "finish_reason": "stop",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    calls = [r for r in relay.server.mailbox.audit_rows() if r["event"] == "call" and r["verb"] == "ask_llm"]
    assert calls[-1]["grant_id"] == grant and calls[-1]["origin"] == f"direct:{LABEL}/claude/{SESSION}"


def test_client_verbs_the_build_does_not_serve(relay, paths, capsys):
    out = cli(["client", "questions", "--session", SESSION, *paths.args], capsys)
    assert out.code == 1
    error = json.loads(out.err)["error"]
    assert error["code"] == "unknown_verb" and error["details"] == {"verb": "list_questions", "not_implemented": True}


def test_a_malformed_session_is_a_usage_error(paths, capsys):
    out = cli(["client", "machines", "--session", "not a session!", *paths.args], capsys)
    assert out.code == 2 and json.loads(out.err)["error"]["kind"] == "configuration"


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------

def test_serve_refuses_a_non_loopback_host(paths, capsys):
    out = cli(["serve", "--host", "0.0.0.0", *paths.args], capsys)
    assert out.code == 2 and out.out == ""
    assert "not a loopback address" in json.loads(out.err)["error"]["message"]
    assert not paths.state.exists()


def test_a_second_serve_is_refused_already_running(relay, paths, capsys):
    out = cli(["serve", *paths.args], capsys)
    assert out.code == 1 and out.out == ""
    error = json.loads(out.err)["error"]
    assert error["code"] == "already_running" and error["details"]["role"] == "relay"
    assert error["details"]["pid"] == os.getpid()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_serve_runs_in_the_foreground_until_sigterm(paths):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "CLAUDE_", "MODEL_ENDPOINTS"))}
    env.update({"HOME": str(paths.tmp / "home"), "PYTHONPATH": os.pathsep.join([str(LIB), str(CONTRACTS)]),
                "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"})
    child = subprocess.Popen([sys.executable, "-m", "llm_kit.cli", "relay", "serve", "--port", str(port), *paths.args],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = scrub(json.loads(line), paths.tmp, port)
        assert ready == {"serving": "http://127.0.0.1:<port>", "machine": LABEL, "protocol": P.PROTOCOL,
                         "pid": child.pid, "state_dir": "<T>/state"}
        token = (paths.state / "relay.token").read_text(encoding="ascii").strip()
        status = HttpRelayClient(f"http://127.0.0.1:{port}", machine=LABEL, relay_token=token).status(
            {}, origin=f"operator:{LABEL}")
        assert status["machine"] == LABEL
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        child.stderr.close()
