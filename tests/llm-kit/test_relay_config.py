"""Tests for llm_kit.relay.config and llm_kit.relay.lifecycle (unit C0).

Every test uses tmp directories: no test reads or writes the real ~/.claude,
~/.config or ~/.local/state.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from llm_kit.registry import model_endpoints as ME
from llm_kit.relay import config as C
from llm_kit.relay import lifecycle as L
from llm_kit.relay import protocol as P

ENV_JSON = {
    "machines": {
        "christina-mac.local": {"os": "macos", "tailscale_hostname": "christinas-macbook-pro"},
        "m5pro.local": {"os": "macos", "tailscale_hostname": "m5pro"},
        "5090RTX": {"os": "ubuntu", "disabled": True, "tailscale_hostname": "5090rtx"},
        "5090W": {"os": "windows", "tailscale_hostname": "5090w"},
        "nolabel.local": {"os": "macos"},
        "badlabel.local": {"os": "macos", "tailscale_hostname": "Not A Label!"},
    }
}


@pytest.fixture
def env_json(tmp_path: Path) -> Path:
    path = tmp_path / "env.json"
    path.write_text(json.dumps(ENV_JSON), encoding="utf-8")
    return path


def _host(name: str):
    return lambda: name


def _config(tmp_path: Path, env_json: Path, **kw) -> C.RelayConfig:
    kw.setdefault("hostname", _host("m5pro.local"))
    kw.setdefault("config_path", tmp_path / "config" / "relay.yaml")
    kw.setdefault("state_root", tmp_path / "state")
    return C.load_config(env_json=env_json, environ={}, **kw)


# --------------------------------------------------------------------------
# machine identity
# --------------------------------------------------------------------------

def test_m5pro_label_comes_from_tailscale_hostname(env_json):
    identity = C.resolve_machine(env_json, hostname=_host("m5pro.local"))
    assert (identity.key, identity.label, identity.tailscale_hostname) == ("m5pro.local", "m5pro", "m5pro")


def test_host_match_is_case_insensitive(env_json):
    identity = C.resolve_machine(env_json, hostname=_host("M5Pro.LOCAL"))
    assert identity.key == "m5pro.local" and identity.label == "m5pro"


def test_label_is_lowercased_and_a_valid_protocol_label(env_json):
    identity = C.resolve_machine(env_json, hostname=_host("5090w"))
    assert identity.label == "5090w" and P.is_machine_label(identity.label)


def test_unknown_host_is_a_hard_error(env_json):
    with pytest.raises(C.RelayConfigError, match="not a machine"):
        C.resolve_machine(env_json, hostname=_host("stranger.local"))


def test_a_bare_prefix_of_a_key_is_not_a_match(env_json):
    with pytest.raises(C.RelayConfigError, match="not a machine"):
        C.resolve_machine(env_json, hostname=_host("m5pro"))


def test_disabled_machine_is_refused(env_json):
    with pytest.raises(C.RelayConfigError, match="disabled"):
        C.resolve_machine(env_json, hostname=_host("5090rtx"))


@pytest.mark.parametrize("host", ["nolabel.local", "badlabel.local"])
def test_missing_or_invalid_label_is_refused(env_json, host):
    with pytest.raises(C.RelayConfigError):
        C.resolve_machine(env_json, hostname=_host(host))


def test_missing_or_malformed_env_json_is_refused(tmp_path):
    with pytest.raises(C.RelayConfigError, match="does not exist"):
        C.resolve_machine(tmp_path / "absent.json", hostname=_host("m5pro.local"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="cannot read"):
        C.resolve_machine(bad, hostname=_host("m5pro.local"))
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="machines"):
        C.resolve_machine(empty, hostname=_host("m5pro.local"))


def test_default_env_json_path_is_home_relative(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert C.default_env_json_path() == tmp_path / ".claude" / "env.json"


# --------------------------------------------------------------------------
# ports, loopback, paths, relay.yaml
# --------------------------------------------------------------------------

def test_defaults(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    assert cfg.bind("relay") == ("127.0.0.1", 4710)
    assert cfg.bind("hub") == ("127.0.0.1", 4711)
    assert cfg.machine_label == "m5pro"
    assert cfg.ledgers == ()
    assert set(cfg.llm_endpoints) == {"qwen38-m5-32k-local", "qwen36-m5-local"}


def test_ports_are_overridable_by_argument_and_file(tmp_path, env_json):
    cfg = _config(tmp_path, env_json, relay_port=5710, hub_port=5711)
    assert (cfg.relay_port, cfg.hub_port) == (5710, 5711)
    conf = tmp_path / "config" / "relay.yaml"
    conf.parent.mkdir()
    conf.write_text("relay: {port: 6710}\nhub: {host: '::1', port: 6711}\n", encoding="utf-8")
    cfg = _config(tmp_path, env_json)
    assert cfg.bind("relay") == ("127.0.0.1", 6710) and cfg.bind("hub") == ("::1", 6711)
    cfg = _config(tmp_path, env_json, relay_port=7710)
    assert cfg.relay_port == 7710  # argument beats file


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.5", "m5pro.local", "localhost", "", "127.0.0.1.evil.com"])
def test_non_loopback_bind_is_refused(tmp_path, env_json, host):
    with pytest.raises(C.RelayConfigError, match="loopback"):
        _config(tmp_path, env_json, relay_host=host)
    with pytest.raises(C.RelayConfigError, match="loopback"):
        _config(tmp_path, env_json, hub_host=host)


def test_non_loopback_bind_in_the_file_is_refused(tmp_path, env_json):
    conf = tmp_path / "config" / "relay.yaml"
    conf.parent.mkdir()
    conf.write_text("relay: {host: 0.0.0.0}\n", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="loopback"):
        _config(tmp_path, env_json)


@pytest.mark.parametrize("port", [0, 65536, -1, "4710", True])
def test_bad_ports_are_refused(tmp_path, env_json, port):
    with pytest.raises(C.RelayConfigError, match="port"):
        _config(tmp_path, env_json, relay_port=port)


def test_relay_and_hub_may_not_share_an_address(tmp_path, env_json):
    with pytest.raises(C.RelayConfigError, match="both bind"):
        _config(tmp_path, env_json, relay_port=4800, hub_port=4800)


def test_unknown_config_keys_are_refused(tmp_path, env_json):
    conf = tmp_path / "config" / "relay.yaml"
    conf.parent.mkdir()
    conf.write_text("prot: 1\n", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="unknown keys"):
        _config(tmp_path, env_json)
    conf.write_text("relay: {hots: 127.0.0.1}\n", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="unknown keys"):
        _config(tmp_path, env_json)


def test_xdg_paths_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert C.state_dir({"XDG_STATE_HOME": str(tmp_path / "s")}) == tmp_path / "s" / "llm-kit" / "relay"
    assert C.state_dir({}) == tmp_path / "home" / ".local" / "state" / "llm-kit" / "relay"
    assert C.state_dir({"XDG_STATE_HOME": "relative/dir"}) == tmp_path / "home" / ".local" / "state" / "llm-kit" / "relay"
    assert C.config_file({"XDG_CONFIG_HOME": str(tmp_path / "c")}) == tmp_path / "c" / "llm-kit" / "relay.yaml"
    assert C.config_file({}) == tmp_path / "home" / ".config" / "llm-kit" / "relay.yaml"


def test_watched_ledgers_come_from_the_file_and_must_be_absolute(tmp_path, env_json):
    conf = tmp_path / "config" / "relay.yaml"
    conf.parent.mkdir()
    ledger = tmp_path / "ledger-a"
    conf.write_text(f"ledgers:\n  - {ledger}\n  - {ledger}\n  - {tmp_path / 'ledger-b'}\n", encoding="utf-8")
    cfg = _config(tmp_path, env_json)
    assert cfg.ledgers == (ledger, tmp_path / "ledger-b")
    conf.write_text("ledgers:\n  - relative/ledger\n", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="not absolute"):
        _config(tmp_path, env_json)


# --------------------------------------------------------------------------
# loopback model registry
# --------------------------------------------------------------------------

def test_prepared_registry_is_valid_loopback_and_has_the_two_entries(tmp_path, env_json):
    state = L.prepare_state(_config(tmp_path, env_json))
    registry = ME.load_endpoint_registry(state.registry_environ())
    assert registry.notes == [] and registry.default_id is None
    assert set(registry.entries) == {"qwen38-m5-32k-local", "qwen36-m5-local"}
    for entry in registry.entries.values():
        assert entry.base_url == "http://127.0.0.1:8000/v1"
        assert entry.kind == ME.TRANSPORT_KIND and entry.billing_owner == "self"
    assert registry.entries["qwen38-m5-32k-local"].model == "qwen3.8-27b-unleashed"
    assert registry.entries["qwen36-m5-local"].model == "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
    assert stat.S_IMODE(state.registry_file.stat().st_mode) == 0o600


def test_remote_registry_entry_is_refused(tmp_path, env_json):
    remote = {"remote": {"base_url": "http://m5pro.localdomain:8000/v1", "model": "m"}}
    with pytest.raises(P.RelayError) as caught:
        _config(tmp_path, env_json, llm_endpoints=remote)
    assert caught.value.code == "endpoint_refused"
    with pytest.raises(P.RelayError) as caught:
        C.render_registry_yaml({"x": {"base_url": "http://5090w.localdomain:8080/v1", "model": "m"}})
    assert caught.value.code == "endpoint_refused"


@pytest.mark.parametrize("url", ["http://127.0.0.1.evil.com/v1", "http://[::1]:8000/v1", "ftp://127.0.0.1/v1", "http://127.0.0.1@evil.com/v1"])
def test_loopback_url_check_is_strict(url):
    ok = url in ("http://[::1]:8000/v1",)
    assert C.is_loopback_url(url) is ok


def test_a_hand_edited_remote_registry_file_is_refused_on_load(tmp_path):
    path = tmp_path / "reg.yaml"
    path.write_text(
        "version: 1\nmodels:\n  fleet:\n    base_url: http://5090w.localdomain:8080/v1\n    model: m\n", encoding="utf-8"
    )
    with pytest.raises(P.RelayError) as caught:
        C.load_loopback_registry(path)
    assert caught.value.code == "endpoint_refused" and caught.value.details == {"endpoint": "fleet"}


def test_registry_default_harness_and_missing_file_are_refused(tmp_path):
    with_default = tmp_path / "d.yaml"
    with_default.write_text(
        "version: 1\ndefault: a\nmodels:\n  a:\n    base_url: http://127.0.0.1:8000/v1\n    model: m\n", encoding="utf-8"
    )
    with pytest.raises(P.RelayError, match="default"):
        C.load_loopback_registry(with_default)
    harness = tmp_path / "h.yaml"
    harness.write_text("version: 1\nmodels:\n  a:\n    harness: opencode\n    model: m\n", encoding="utf-8")
    with pytest.raises(P.RelayError, match="plain transport"):
        C.load_loopback_registry(harness)
    with pytest.raises(P.RelayError) as caught:
        C.load_loopback_registry(tmp_path / "absent.yaml")  # no fall back to the convention path
    assert caught.value.code == "endpoint_refused"


def test_missing_explicit_endpoint_is_refused_with_no_fallback(tmp_path, env_json):
    state = L.prepare_state(_config(tmp_path, env_json))
    registry = C.load_loopback_registry(state.registry_file)
    for name in (None, "", "   ", "not-there", "qwen38-32k"):
        with pytest.raises(P.RelayError) as caught:
            C.require_loopback_endpoint(registry, name)
        assert caught.value.code == "endpoint_refused"
    assert C.require_loopback_endpoint(registry, "qwen36-m5-local").id == "qwen36-m5-local"


def test_empty_endpoint_set_is_refused():
    with pytest.raises(P.RelayError, match="no default endpoint"):
        C.validate_llm_endpoints({})


# --------------------------------------------------------------------------
# state directory and tokens
# --------------------------------------------------------------------------

def test_tokens_are_generated_0600_and_stable(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    first = L.prepare_state(cfg)
    assert stat.S_IMODE(cfg.paths.root.stat().st_mode) == 0o700
    for path in (cfg.paths.relay_token, cfg.paths.hub_bearer):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert first.relay_token != first.hub_bearer and len(first.relay_token) >= 32
    again = L.prepare_state(cfg)
    assert (again.relay_token, again.hub_bearer) == (first.relay_token, first.hub_bearer)


def test_a_loose_token_or_state_dir_is_refused(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    os.chmod(cfg.paths.relay_token, 0o644)
    with pytest.raises(C.RelayConfigError, match="mode 0644"):
        L.prepare_state(cfg)
    os.chmod(cfg.paths.relay_token, 0o600)
    os.chmod(cfg.paths.root, 0o755)
    with pytest.raises(C.RelayConfigError, match="state directory"):
        L.prepare_state(cfg)


def test_an_empty_token_file_is_refused(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    cfg.paths.hub_bearer.write_text("\n", encoding="ascii")
    os.chmod(cfg.paths.hub_bearer, 0o600)
    with pytest.raises(C.RelayConfigError, match="empty"):
        L.read_token(cfg.paths.hub_bearer)


def test_token_comparison():
    assert L.token_matches("abc", "abc")
    assert not L.token_matches("abd", "abc") and not L.token_matches(None, "abc") and not L.token_matches("", "")


# --------------------------------------------------------------------------
# singleton
# --------------------------------------------------------------------------

def test_second_holder_of_a_role_is_refused(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    with L.RoleLock(cfg.paths, "relay"):
        second = L.RoleLock(cfg.paths, "relay")
        with pytest.raises(L.SingletonHeld) as caught:
            second.acquire()
        assert caught.value.code == "registration_conflict" and caught.value.holder_pid == os.getpid()
        assert not second.held
        with L.RoleLock(cfg.paths, "hub"):  # another role is independent
            pass
    with L.RoleLock(cfg.paths, "relay"):  # released: a new holder starts
        pass


def test_a_dead_holders_leftover_pidfile_does_not_block(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    cfg.paths.pidfile("relay").write_text("999999\n", encoding="ascii")
    with L.RoleLock(cfg.paths, "relay") as lock:
        assert lock.held and cfg.paths.pidfile("relay").read_text().strip() == str(os.getpid())


def test_lock_is_released_when_the_holder_process_dies(tmp_path, env_json):
    import subprocess
    import sys

    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from pathlib import Path; from llm_kit.relay import lifecycle as L, config as C;"
        "L.RoleLock(C.StatePaths(Path(sys.argv[2])), 'relay').acquire(); print('held', flush=True)"
    )
    src = str(Path(L.__file__).resolve().parents[2])
    out = subprocess.run([sys.executable, "-c", code, src, str(cfg.paths.root)], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "held", out.stderr  # the child exited while holding it
    with L.RoleLock(cfg.paths, "relay"):
        pass


def test_unknown_role_is_refused(tmp_path):
    with pytest.raises(C.RelayConfigError):
        L.RoleLock(C.StatePaths(tmp_path), "daemon")


# --------------------------------------------------------------------------
# pause
# --------------------------------------------------------------------------

def test_pause_is_persisted_across_a_reload(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    ticks = iter([1000.0, 2000.0, 3000.0])
    first = L.PauseStore(cfg.paths, clock=lambda: next(ticks))
    assert first.state() == {"relay": C_clear(), "hub": C_clear()}
    first.pause("relay", "maintenance")
    reloaded = L.PauseStore(cfg.paths)  # a new object, as after a restart
    assert reloaded.state()["relay"] == {"paused": True, "paused_since": 1000.0, "reason": "maintenance"}
    assert reloaded.state()["hub"] == C_clear()
    assert reloaded.is_paused("relay") and not reloaded.is_paused("hub")
    reloaded.pause("relay", "still")  # already paused: paused_since is kept
    assert L.PauseStore(cfg.paths).state()["relay"]["paused_since"] == 1000.0
    first.pause("hub")
    assert L.PauseStore(cfg.paths).state()["hub"]["paused_since"] == 2000.0
    reloaded.resume("relay")
    final = L.PauseStore(cfg.paths).state()
    assert final["relay"] == C_clear() and final["hub"]["paused"] is True
    assert stat.S_IMODE(cfg.paths.pause_file.stat().st_mode) == 0o600


def C_clear():
    return {"paused": False, "paused_since": None, "reason": None}


def test_paused_error_carries_scope_and_paused_since(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    store = L.PauseStore(cfg.paths, clock=lambda: 5.0)
    store.require_not_paused("relay")
    store.pause("relay", "why")
    with pytest.raises(P.RelayError) as caught:
        store.require_not_paused("relay")
    assert caught.value.code == "paused" and caught.value.retryable and caught.value.http_status == 503
    assert caught.value.details == {"scope": "relay", "paused_since": 5.0}
    store.require_not_paused("hub")  # scopes are independent


def test_pause_state_validates_against_the_protocol_schema(tmp_path, env_json):
    from llm_kit.schema import json_schema as JS

    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    store = L.PauseStore(cfg.paths)
    store.pause("hub", "x")
    assert JS.validate(P.load_schema("pause_state"), store.state()) == ()


def test_bad_scope_and_a_corrupt_pause_file_fail_loudly(tmp_path, env_json):
    cfg = _config(tmp_path, env_json)
    L.prepare_state(cfg)
    store = L.PauseStore(cfg.paths)
    with pytest.raises(P.RelayError) as caught:
        store.pause("everything")
    assert caught.value.code == "bad_request"
    cfg.paths.pause_file.write_text("{oops", encoding="utf-8")
    with pytest.raises(C.RelayConfigError, match="unreadable"):
        store.state()  # a corrupt file never reads as "not paused"
    cfg.paths.pause_file.write_text(json.dumps({"relay": {"paused": True, "paused_since": None, "reason": None}, "hub": C_clear()}))
    with pytest.raises(C.RelayConfigError, match="invalid flag"):
        store.is_paused("relay")
