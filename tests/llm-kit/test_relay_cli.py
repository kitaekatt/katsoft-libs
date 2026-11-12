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
import time
import uuid
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
#: relay.yaml text that turns on the relay and the hub (llm_kit.relay.config.require_enabled).
ENABLED_YAML = "enabled: true\nhub:\n  enabled: true\n"
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
    config.parent.mkdir(parents=True)
    config.write_text(ENABLED_YAML, encoding="utf-8")  # the opt-in: nothing listens without it
    state = tmp_path / "state"
    claude, codex = tmp_path / "claude-home", tmp_path / "codex-home"
    (claude / "sessions").mkdir(parents=True)
    (claude / "projects").mkdir(parents=True)
    return SimpleNamespace(tmp=tmp_path, env_json=env_json, config=config, state=state,
                           roots=D.DiscoveryRoots(claude, codex),
                           args=["--env-json", str(env_json), "--config", str(config), "--state-dir", str(state)])


def _serve(paths, ledgers=()) -> Iterator[SimpleNamespace]:
    config = C.load_config(env_json=paths.env_json, environ={}, config_path=paths.config, state_root=paths.state,
                           ledgers=ledgers)
    server = S.RelayServer(
        config, port=0, roots=paths.roots, probe=D.PsutilProbe(), poll_interval_s=0.05, maintain_interval_s=3600,
        backend_factory=lambda entry: SimpleNamespace(backend=Backend(), model=entry.model),
        policy_loader=lambda scope: consent.policy(scope, path=paths.tmp / "consent.json"),
    ).start()
    port = server.address[1]
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(f"{ENABLED_YAML}relay:\n  port: {port}\n", encoding="utf-8")
    client = HttpRelayClient(server.url, machine=LABEL, relay_token=server.prepared.relay_token)
    try:
        yield SimpleNamespace(server=server, port=port, client=client)
    finally:
        server.stop()


@pytest.fixture
def relay(paths) -> Iterator[SimpleNamespace]:
    yield from _serve(paths)


@pytest.fixture
def question_relay(paths) -> Iterator[SimpleNamespace]:
    """A relay watching one real ledger with one open question (``relay.qid``)."""
    from test_relay_questions import _ledger

    ledger = paths.tmp / "ledgers" / "jobs.sqlite3"
    ledger.parent.mkdir()
    _ledger(ledger)
    for served in _serve(paths, ledgers=(ledger,)):
        served.qid = "q:testbox/jobs:run:1"
        yield served


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
    for verb in ("serve", "hub", "pause", "resume", "status", "client"):
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
        "pause": {"relay": PAUSE_CLEAR, "hub": PAUSE_CLEAR}, "harnesses": ["claude", "codex"],
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
    assert out.code == 0 and json.loads(out.out) == {
        "sessions": [], "machines": [{"machine": LABEL, "status": "up"}], "omitted": {"idle": 0, "ended": 0}}

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


def _transcript(paths, session: str, age_s: float) -> None:
    folder = paths.roots.claude_projects / "-tmp-proj"
    folder.mkdir(exist_ok=True)
    path = folder / f"{session}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "old work"},
                                "timestamp": "2026-11-03T10:00:00.000Z", "cwd": "/tmp/proj"}) + "\n", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def test_client_sessions_shows_live_sessions_by_default_and_counts_the_rest(relay, paths, capsys):
    live, idle, ended = (str(uuid.uuid4()) for _ in range(3))
    relay.client.call("register", {
        "session_id": live, "harness": "claude", "pid": os.getpid(), "cwd": "/tmp/proj", "version": "2.1.317",
        "accepts": ["note"], "channel_nonce": "nonce-" + "x" * 20,
    }, origin=f"channel:{LABEL}/claude/{live}")
    _transcript(paths, idle, 3600)              # process gone, written within 7 days
    _transcript(paths, ended, 8 * 86400)        # older than 7 days

    def listed(*extra: str) -> dict:
        mint(relay, "list_sessions")
        out = cli(["client", "sessions", "--session", SESSION, *extra, *paths.args], capsys)
        assert (out.code, out.err) == (0, ""), out
        return json.loads(out.out)

    default = listed()
    assert [s["session_id"] for s in default["sessions"]] == [live]
    assert default["omitted"] == {"idle": 1, "ended": 1}
    every = listed("--state", "all")
    assert {s["session_id"]: s["state"] for s in every["sessions"]} == {
        live: "live-accepting", idle: "idle", ended: "ended"}
    assert "omitted" not in every
    one = listed("--state", "idle")
    assert [s["session_id"] for s in one["sessions"]] == [idle] and "omitted" not in one
    # the relay was asked without a state filter by default and with "all" (only "idle" is a relay state)
    calls = [r for r in relay.server.mailbox.audit_rows() if r["event"] == "call" and r["verb"] == "list_sessions"]
    assert [r["outcome"] for r in calls] == ["ok", "ok", "ok"]


def test_client_questions_and_answer_are_served_with_grants(question_relay, paths, capsys):
    relay = question_relay
    out = cli(["client", "questions", "--session", SESSION, *paths.args], capsys)
    assert out.code == 1 and json.loads(out.err)["error"]["code"] == "grant_required"

    mint(relay, "list_questions")
    out = cli(["client", "questions", "--session", SESSION, *paths.args], capsys)
    assert (out.code, out.err) == (0, "")
    listed = json.loads(out.out)
    assert [q["id"] for q in listed["questions"]] == [relay.qid]
    assert listed["machines"] == [{"machine": LABEL, "status": "up"}]

    grant = mint(relay, "answer_question", [relay.qid])
    out = cli(["client", "answer", relay.qid, "--decision", "answer", "--input", '{"approved": true}',
               "--session", SESSION, "--grant", grant, *paths.args], capsys)
    assert (out.code, out.err) == (0, "")
    assert scrub(json.loads(out.out), paths.tmp) == {
        "id": relay.qid, "decision": "answer", "state": "answered", "replayed": False, "resolved_at": "<t>",
        "job_id": "job", "job_state": "waiting",
    }
    out = cli(["client", "answer", relay.qid, "--decision", "reject", "--session", SESSION, "--grant", grant,
               *paths.args], capsys)
    assert out.code == 1 and json.loads(out.err)["error"]["code"] == "grant_used_up"


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


# --------------------------------------------------------------------------
# hub
# --------------------------------------------------------------------------

def test_hub_takes_host_and_port_like_serve(paths, capsys):
    out = cli(["hub", "--help"], capsys)
    assert out.code == 0
    for flag in ("--host", "--port", "--public-url", "--env-json", "--config", "--state-dir"):
        assert flag in out.out


@pytest.mark.parametrize("url", ["http://hub.example.test", "https://hub.example.test/mcp", "https://u@hub.example.test"])
def test_hub_refuses_a_public_url_that_is_not_an_https_origin(paths, capsys, url):
    out = cli(["hub", "--public-url", url, *paths.args], capsys)
    assert out.code == 2 and out.out == ""
    error = json.loads(out.err)["error"]
    assert error["kind"] == "configuration" and "public_url" in error["message"]
    assert not paths.state.exists()


def test_hub_refuses_a_non_loopback_host(paths, capsys):
    out = cli(["hub", "--host", "0.0.0.0", *paths.args], capsys)
    assert out.code == 2 and out.out == ""
    error = json.loads(out.err)["error"]
    assert error["kind"] == "configuration" and "not a loopback address" in error["message"]
    assert not paths.state.exists()


def test_hub_refuses_the_relays_own_address(paths, capsys):
    out = cli(["hub", "--port", "4710", *paths.args], capsys)
    assert out.code == 2 and "would both bind" in json.loads(out.err)["error"]["message"]


def test_a_second_hub_is_refused_already_running(paths, capsys):
    from llm_kit.relay.hub import HubServer

    config = C.load_config(env_json=paths.env_json, environ={}, config_path=paths.config, state_root=paths.state)
    with HubServer(config, port=0):
        out = cli(["hub", "--port", str(_free_port()), *paths.args], capsys)
    assert out.code == 1 and out.out == ""
    error = json.loads(out.err)["error"]
    assert error["code"] == "already_running" and error["details"]["role"] == "hub"
    assert error["details"]["pid"] == os.getpid()


def test_hub_runs_in_the_foreground_until_sigterm(paths):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "CLAUDE_", "MODEL_ENDPOINTS"))}
    env.update({"HOME": str(paths.tmp / "home"), "PYTHONPATH": os.pathsep.join([str(LIB), str(CONTRACTS)]),
                "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"})
    child = subprocess.Popen([sys.executable, "-m", "llm_kit.cli", "relay", "hub", "--port", str(port), *paths.args],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = scrub(json.loads(line), paths.tmp, port)
        assert ready == {"serving": "http://127.0.0.1:<port>/mcp", "machine": LABEL, "protocol": P.PROTOCOL,
                         "public_url": None, "oauth": False}
        bearer = (paths.state / "hub.bearer").read_text(encoding="ascii").strip()
        assert bearer
        import httpx

        refused = httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, trust_env=False)
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "unauthenticated"
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        child.stderr.close()


def test_hub_with_a_public_url_serves_the_oauth_sign_in(paths):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "CLAUDE_", "MODEL_ENDPOINTS"))}
    env.update({"HOME": str(paths.tmp / "home"), "PYTHONPATH": os.pathsep.join([str(LIB), str(CONTRACTS)]),
                "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"})
    child = subprocess.Popen([sys.executable, "-m", "llm_kit.cli", "relay", "hub", "--port", str(port),
                              "--public-url", "https://Hub.Example.test/", *paths.args],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = scrub(json.loads(line), paths.tmp, port)
        assert ready == {"serving": "http://127.0.0.1:<port>/mcp", "machine": LABEL, "protocol": P.PROTOCOL,
                         "public_url": "https://hub.example.test/mcp", "oauth": True}
        assert "sign-in is on for https://hub.example.test/mcp" in child.stderr.readline()
        import httpx

        metadata = httpx.get(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server", trust_env=False)
        assert metadata.status_code == 200 and metadata.json()["issuer"] == "https://hub.example.test"
        refused = httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, trust_env=False)
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == (
            'Bearer resource_metadata="https://hub.example.test/.well-known/oauth-protected-resource/mcp"')
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        child.stderr.close()


# --------------------------------------------------------------------------
# opt-in: serve and hub run only when relay.yaml turns them on
# --------------------------------------------------------------------------

def _child(paths, argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "CLAUDE_", "MODEL_ENDPOINTS"))}
    env.update({"HOME": str(paths.tmp / "home"), "PYTHONPATH": os.pathsep.join([str(LIB), str(CONTRACTS)]),
                "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1", "LLM_KIT_RELAY_ENABLED": "1"})
    return subprocess.run([sys.executable, "-m", "llm_kit.cli", "relay", *argv], env=env, capture_output=True,
                          text=True, timeout=timeout)


@pytest.mark.parametrize("verb,text,needed", [
    ("serve", "", "`enabled: true`"),
    ("serve", "enabled: false\nhub: {enabled: true}\n", "`enabled: true`"),
    ("hub", "", "`enabled: true` and `hub: {enabled: true}`"),
    ("hub", "enabled: true\n", "`hub: {enabled: true}`"),
    ("hub", "hub: {enabled: true}\n", "`enabled: true` and `hub: {enabled: true}`"),
])
def test_serve_and_hub_refuse_unless_relay_yaml_turns_them_on(paths, verb, text, needed):
    paths.config.write_text(text, encoding="utf-8")
    argv = [verb, "--port", str(_free_port()), *paths.args]
    if verb == "hub":
        argv += ["--public-url", "https://hub.example.test"]
    done = _child(paths, argv)
    assert done.returncode == 2 and done.stdout == "", done.stderr
    error = json.loads(done.stderr)["error"]
    assert error["kind"] == "configuration"
    assert needed in error["message"] and str(paths.config) in error["message"]
    assert not paths.state.exists()


def test_serve_and_hub_refuse_without_any_relay_yaml(paths):
    paths.config.unlink()
    for verb in ("serve", "hub"):
        done = _child(paths, [verb, "--port", str(_free_port()), *paths.args])
        assert done.returncode == 2 and "is not enabled on this machine" in done.stderr, done.stderr
    assert not paths.state.exists() and not paths.config.exists()


def test_no_command_line_flag_turns_the_relay_on(paths, capsys):
    for verb in ("serve", "hub"):
        out = cli([verb, "--help"], capsys)
        assert out.code == 0 and "--enable" not in out.out and "--force" not in out.out


# --------------------------------------------------------------------------
# codex: the Codex channel (unit CH). The app-server is the fake stub behind a launcher script.
# --------------------------------------------------------------------------

def _codex_env(paths, launcher, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "CLAUDE_", "MODEL_ENDPOINTS"))}
    env.update({"HOME": str(paths.tmp / "home"), "PYTHONPATH": os.pathsep.join([str(LIB), str(CONTRACTS)]),
                "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1", "LLM_KIT_CODEX_EXECUTABLE": str(launcher.path),
                "FAKE_CODEX_LOG": str(paths.tmp / "stub.log.jsonl"), "FAKE_CODEX_CFG": "{}"})
    env.update(extra)
    return env


@pytest.fixture
def codex_launcher(paths, monkeypatch):
    from test_relay_codex_channel import make_launcher

    launcher = make_launcher(paths.tmp)
    monkeypatch.setenv("LLM_KIT_CODEX_EXECUTABLE", str(launcher.path))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(paths.tmp / "stub.log.jsonl"))
    monkeypatch.setenv("FAKE_CODEX_CFG", "{}")
    (paths.tmp / "work").mkdir()
    return launcher


@pytest.fixture
def old_relay(paths, monkeypatch) -> Iterator[SimpleNamespace]:
    """A relay that behaves as katsoft-llm-kit 1.18.0 or earlier: its status lists no ``harnesses``."""
    from test_relay_server import _as_a_relay_without_amendment_1

    _as_a_relay_without_amendment_1(monkeypatch)
    yield from _serve(paths)


def test_codex_exits_2_and_spawns_nothing_unless_relay_yaml_opts_in(paths, capsys, codex_launcher):
    for text in ("", "enabled: false\n"):
        paths.config.write_text(text, encoding="utf-8")
        out = cli(["codex", "--cd", str(paths.tmp / "work"), *paths.args], capsys)
        assert out.code == 2 and out.out == ""
        error = json.loads(out.err)["error"]
        assert error["kind"] == "configuration" and "`enabled: true`" in error["message"] and str(paths.config) in error["message"]
        assert codex_launcher.launches() == 0
    assert not paths.state.exists()


def test_codex_exits_2_for_a_missing_directory_and_for_bad_flags(paths, capsys, codex_launcher):
    out = cli(["codex", "--cd", str(paths.tmp / "nope"), *paths.args], capsys)
    assert out.code == 2 and "is not an existing directory" in json.loads(out.err)["error"]["message"]
    work = str(paths.tmp / "work")
    for flags in (["--turn-timeout", "0"], ["--turn-timeout", "soon"], ["--accepts", "note,shout"],
                  ["--sandbox", "danger-full-access"], ["--sandbox", "read-only", "--approval-policy", "on-request"]):
        assert cli(["codex", "--cd", work, *flags, *paths.args], capsys).code == 2, flags
    assert cli(["codex", *paths.args], capsys).code == 2  # --cd is required
    assert codex_launcher.launches() == 0


def test_codex_exits_1_when_no_relay_has_run_and_spawns_nothing(paths, capsys, codex_launcher):
    out = cli(["codex", "--cd", str(paths.tmp / "work"), *paths.args], capsys)  # enabled, but no token and no relay
    assert out.code == 1 and out.out == ""
    assert "llm-kit relay serve" in json.loads(out.err)["error"]["message"]
    assert codex_launcher.launches() == 0


def test_codex_exits_1_when_the_running_relay_lists_no_codex_harness_and_spawns_nothing(old_relay, paths, capsys, codex_launcher):
    status = old_relay.client.status({}, origin=f"operator:{LABEL}")
    assert "harnesses" not in status
    out = cli(["codex", "--cd", str(paths.tmp / "work"), *paths.args], capsys)
    assert out.code == 1 and out.out == ""
    assert json.loads(out.err)["error"]["message"] == (
        "the running relay is older than katsoft-llm-kit 1.19.0; restart llm-kit relay serve")
    assert codex_launcher.launches() == 0
    assert old_relay.client.status({}, origin=f"operator:{LABEL}")["registrations"] == 0


def test_codex_exits_1_with_the_relays_refusal_of_the_registration(relay, paths, capsys, codex_launcher, monkeypatch):
    real = HttpRelayClient.call

    def refusing(self, verb, params, **kw):
        if verb == "register":
            raise P.RelayError("bad_request", "register params fail their schema: stubbed")
        return real(self, verb, params, **kw)

    monkeypatch.setattr(HttpRelayClient, "call", refusing)
    out = cli(["codex", "--cd", str(paths.tmp / "work"), *paths.args], capsys)
    assert out.code == 1 and out.out == ""
    error = json.loads(out.err)["error"]
    assert error["code"] == "bad_request" and "stubbed" in error["message"]


def test_codex_exits_2_when_the_thread_to_resume_never_ran_and_1_when_it_is_busy(relay, paths, capsys, codex_launcher, monkeypatch):
    work = str(paths.tmp / "work")
    thread = "0192aaaa-0000-7000-8000-00000000cafe"
    monkeypatch.setenv("FAKE_CODEX_CFG", json.dumps({"resume_error": "no_rollout"}))
    out = cli(["codex", "--cd", work, "--resume", thread, *paths.args], capsys)
    assert out.code == 2 and json.loads(out.err)["error"]["message"] == f"thread {thread} never ran a turn; it cannot be resumed"
    monkeypatch.setenv("FAKE_CODEX_CFG", json.dumps({"resume_error": "active_writer"}))
    out = cli(["codex", "--cd", work, "--resume", thread, *paths.args], capsys)
    assert out.code == 1 and json.loads(out.err)["error"]["message"] == f"thread {thread} is open in another Codex process"
    assert relay.client.status({}, origin=f"operator:{LABEL}")["registrations"] == 0


def _tcp_listeners(pid: int) -> list[Any]:
    """Every listening TCP socket held by ``pid`` or any descendant."""
    import psutil

    found = []
    root = psutil.Process(pid)
    for proc in [root, *root.children(recursive=True)]:
        try:
            found += [c for c in proc.net_connections(kind="inet") if c.status == psutil.CONN_LISTEN]
        except psutil.NoSuchProcess:
            continue
    return found


def test_the_listener_probe_does_see_a_listening_process_tree(tmp_path):
    """Positive control: the probe the e2e test relies on reports a listening child (and a grandchild)."""
    code = ("import socket,subprocess,sys,time\n"
            "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(1)\n"
            "subprocess.Popen([sys.executable,'-c','import socket,time; s=socket.socket(); s.bind((\"127.0.0.1\",0)); s.listen(1); time.sleep(30)'])\n"
            "print('up', flush=True); time.sleep(30)\n")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "up"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(_tcp_listeners(child.pid)) < 2:
            time.sleep(0.1)
        assert len(_tcp_listeners(child.pid)) == 2
    finally:
        import psutil

        for proc in [psutil.Process(child.pid), *psutil.Process(child.pid).children(recursive=True)]:
            proc.kill()
        child.wait()
        child.stdout.close()


def test_codex_runs_as_a_child_answers_a_granted_send_binds_nothing_and_stops_on_sigterm(relay, paths, codex_launcher):
    work = paths.tmp / "work"
    child = subprocess.Popen([sys.executable, "-m", "llm_kit.cli", "relay", "codex", "--cd", str(work), *paths.args],
                             env=_codex_env(paths, codex_launcher), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = child.stdout.readline()
        assert line, child.stderr.read()
        ready = json.loads(line)  # the effective policy is the first stdout line
        assert ready["codex_channel"] == "ready" and ready["address"] == f"{LABEL}/codex/{ready['thread_id']}"
        assert ready["policy"] == {"sandbox": "workspace-write", "network": False, "approval_policy": "never",
                                   "approvals_reviewer": "user", "tmp_excluded": True, "hooks": False, "notify": "off"}
        assert ready["turn_timeout_s"] == 1800.0 and ready["cwd"] == os.path.realpath(work)
        argv = codex_launcher.argv()
        assert argv[:3] == ["app-server", "--listen", "stdio://"] and "features.hooks=false" in argv and "notify=[]" in argv
        sent = relay.client.call("send_message", {"to": ready["address"], "kind": "question", "body": "ping?"},
                                 origin=f"direct:{LABEL}/claude/{SESSION}", grant=mint(relay, "send_message", [ready["address"]]))
        mid = sent["message"]["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and relay.server.mailbox.get_message(mid)["state"] != "answered":
            time.sleep(0.1)
        assert relay.server.mailbox.get_message(mid)["state"] == "answered"
        assert _tcp_listeners(child.pid) == []  # nothing in the process tree listens on a port
        assert relay.client.status({}, origin=f"operator:{LABEL}")["registrations"] == 1
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
        assert relay.client.status({}, origin=f"operator:{LABEL}")["registrations"] == 0  # deregistered
        assert "deregistered" in child.stderr.read()
        rest = [json.loads(l) for l in child.stdout.read().splitlines() if l.strip()]
        assert [e["event"] for e in rest if e.get("event") in ("message", "reply")] == ["message", "reply"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        child.stderr.close()
