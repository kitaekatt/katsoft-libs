"""The consent file store (llm_kit.registry.consent): load, save, update and path resolution.

Every test runs against a fake home; nothing touches the real one. The effective
policy (scopes, provenance) and the opt-in surfaces live in test_consent_layer.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_kit.registry import consent as consent_mod
from llm_kit.registry.consent import (
    CLASSES,
    ConsentConfig,
    ConsentConfigError,
    default_consent_path,
    load_config,
    policy,
    save_config,
    update_config,
)

DEFAULT = {"local": True, "subscription": True, "metered": False}


def _set_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    return home


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid(**over):
    data = {
        "schema": 2, "local": True, "subscription": True, "metered": False,
        "scopes": {}, "updated_at": "2026-10-21T00:00:00+00:00",
    }
    data.update(over)
    return data


def _globals(config: ConsentConfig) -> dict:
    return dict(config.globals_)


# ---- a missing file is the default policy, never an error -------------------------------------


def test_missing_file_is_the_default_policy_and_creates_nothing(tmp_path):
    target = tmp_path / "missing" / "consent.json"
    config = load_config(target)
    assert config.source == "default" and config.path == target
    assert dict(config.globals_) == {} and dict(config.scopes) == {}
    result = policy(path=target)
    assert {name: state.allowed for name, state in result.classes.items()} == DEFAULT
    assert all(state.provenance == "default" for state in result.classes.values())
    assert result.source == "default"
    assert not target.parent.exists()  # reading created nothing, not even the directory


def test_default_path_absent_uses_fake_home(fake_home):
    expected = fake_home / ".config/llm-kit/consent.json"
    assert default_consent_path() == expected
    assert policy().source == "default" and policy().path == expected
    assert not (fake_home / ".config").exists()


# ---- a present file that cannot be trusted is a loud error ---------------------------------------


@pytest.mark.parametrize("field", list(CLASSES))
@pytest.mark.parametrize("bad", [1, 0, "true", "false", "yes", None, [], {}, 1.0])
def test_non_bool_class_is_a_configuration_error(tmp_path, field, bad):
    target = _write(tmp_path / "c.json", _valid(**{field: bad}))
    with pytest.raises(ConsentConfigError, match=field):
        load_config(target)


@pytest.mark.parametrize("schema", [0, 1, 3, True, "2", 2.0, None])
def test_bad_schema_is_a_configuration_error(tmp_path, schema):
    target = _write(tmp_path / "c.json", _valid(schema=schema))
    with pytest.raises(ConsentConfigError, match="schema"):
        load_config(target)


def test_missing_schema_is_a_configuration_error(tmp_path):
    data = _valid()
    del data["schema"]
    with pytest.raises(ConsentConfigError, match="schema"):
        load_config(_write(tmp_path / "c.json", data))


def test_the_retired_usage_spend_file_is_refused_not_translated(tmp_path):
    target = _write(tmp_path / "c.json", {"schema": 1, "usage": True, "spend": True, "updated_at": "x"})
    with pytest.raises(ConsentConfigError, match="usage/spend"):
        load_config(target)


@pytest.mark.parametrize("payload", [[], "x", 3, None, True])
def test_non_object_json_is_a_configuration_error(tmp_path, payload):
    target = _write(tmp_path / "c.json", json.dumps(payload))
    with pytest.raises(ConsentConfigError, match="JSON object"):
        load_config(target)


@pytest.mark.parametrize("payload", ["", "{not json", b"\xff\xfe\x00bad"])
def test_decode_error_is_a_configuration_error(tmp_path, payload):
    target = _write(tmp_path / "c.json", payload)
    with pytest.raises(ConsentConfigError, match="not valid JSON"):
        load_config(target)


def test_unreadable_path_is_a_configuration_error(tmp_path):
    target = tmp_path / "c.json"
    target.mkdir()  # a directory raises an OSError other than FileNotFoundError
    with pytest.raises(ConsentConfigError, match="cannot be read"):
        load_config(target)


def test_parent_is_a_file_is_a_configuration_error(tmp_path):
    blocker = _write(tmp_path / "blocker", "x")
    with pytest.raises(ConsentConfigError):
        load_config(blocker / "consent.json")


@pytest.mark.parametrize("depth", [0, 1])
def test_missing_error_with_file_ancestor_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int,
) -> None:
    blocker = _write(tmp_path / "blocker", "x")
    target = blocker / "consent.json" if depth == 0 else blocker / "sub" / "consent.json"
    original = Path.read_bytes

    def windows_missing(path: Path) -> bytes:
        if path == target:
            raise FileNotFoundError(2, "The system cannot find the path specified", str(path))
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", windows_missing)
    with pytest.raises(ConsentConfigError, match="ancestor .* is a file"):
        load_config(target)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ({"free": True}, "free"),
        ({"usage": True}, "usage"),
        ({"scopes": []}, "scopes"),
        ({"scopes": {"x": []}}, "scope 'x'"),
        ({"scopes": {"x": {"free": True}}}, "free"),
        ({"scopes": {"x": {"metered": "no"}}}, "metered"),
        ({"scopes": {"": {"metered": False}}}, "non-empty"),
        ({"scopes": {" pad": {"metered": False}}}, "whitespace"),
    ],
)
def test_unknown_keys_and_bad_scopes_are_configuration_errors(tmp_path, mutation, match):
    target = _write(tmp_path / "c.json", _valid(**mutation))
    with pytest.raises(ConsentConfigError, match=match):
        load_config(target)
    with pytest.raises(ConsentConfigError):
        policy(path=target)


def test_the_error_is_a_value_error_so_the_cli_reports_a_configuration_error():
    assert issubclass(ConsentConfigError, ValueError)


def test_absent_global_keys_fall_back_to_the_default_with_default_provenance(tmp_path):
    target = _write(tmp_path / "c.json", {"schema": 2, "metered": True})
    config = load_config(target)
    assert _globals(config) == {"metered": True}
    result = policy(path=target)
    assert result.classes["metered"].allowed and result.classes["metered"].provenance == "global"
    assert result.classes["local"].allowed and result.classes["local"].provenance == "default"


@pytest.mark.parametrize(
    "sub,met",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_valid_file_allowed_classes(tmp_path, sub, met):
    target = _write(tmp_path / "c.json", _valid(subscription=sub, metered=met))
    result = policy(path=target)
    expected = {"local"} | ({"subscription"} if sub else set()) | ({"metered"} if met else set())
    assert result.allowed == frozenset(expected) and isinstance(result.allowed, frozenset)
    assert result.source == "file" and result.path == target


# ---- the path is resolved per call ---------------------------------------------------------------


def test_path_resolved_per_call(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    _set_home(monkeypatch, home_a)
    first = default_consent_path()
    _set_home(monkeypatch, home_b)
    second = default_consent_path()
    assert first == home_a / ".config/llm-kit/consent.json"
    assert second == home_b / ".config/llm-kit/consent.json"
    # The default path is also honoured by load and save, not only by the accessor.
    saved, _ = update_config({"metered": True})
    assert saved == second
    assert policy().source == "file"
    _set_home(monkeypatch, home_a)
    assert policy().source == "default"


# ---- save and update -----------------------------------------------------------------------------


def test_save_writes_format_and_roundtrips(tmp_path):
    target = tmp_path / "deep" / "dir" / "consent.json"
    written = save_config(
        globals_=DEFAULT, scopes={"databench": {"metered": True}}, path=target
    )
    assert written == target
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data) == {"schema", "local", "subscription", "metered", "scopes", "updated_at"}
    assert data["schema"] == 2 and data["scopes"] == {"databench": {"metered": True}}
    assert [data[c] for c in CLASSES] == [True, True, False]
    from datetime import datetime

    datetime.fromisoformat(data["updated_at"])
    loaded = load_config(target)
    assert _globals(loaded) == DEFAULT and dict(loaded.scopes) == {"databench": {"metered": True}}
    assert loaded.source == "file"


def test_a_global_change_preserves_every_scope_entry(tmp_path):
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={"a": {"metered": False}, "b": {"subscription": False}}, path=target)
    _, config = update_config({"metered": True}, path=target)
    assert _globals(config)["metered"] is True
    assert {k: dict(v) for k, v in config.scopes.items()} == {
        "a": {"metered": False}, "b": {"subscription": False},
    }


def test_a_scope_change_preserves_the_global_policy_and_other_scopes(tmp_path):
    target = tmp_path / "consent.json"
    save_config(globals_={"local": True, "subscription": False, "metered": True}, scopes={"a": {"metered": False}}, path=target)
    _, config = update_config({"subscription": False}, scope="b", path=target)
    assert _globals(config) == {"local": True, "subscription": False, "metered": True}
    assert {k: dict(v) for k, v in config.scopes.items()} == {
        "a": {"metered": False}, "b": {"subscription": False},
    }
    _, config = update_config({"metered": False}, scope="b", path=target)
    assert dict(config.scopes["b"]) == {"subscription": False, "metered": False}


def test_update_starts_from_the_default_when_there_is_no_file(tmp_path):
    target = tmp_path / "consent.json"
    _, config = update_config({"metered": True}, path=target)
    assert _globals(config) == {"local": True, "subscription": True, "metered": True}


def test_update_refuses_to_overwrite_a_malformed_file(tmp_path):
    target = _write(tmp_path / "c.json", "{broken")
    with pytest.raises(ConsentConfigError):
        update_config({"metered": True}, path=target)
    assert target.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("bad_scope", ["", "  ", " x", "x ", 3])
def test_update_rejects_an_unusable_scope_name(tmp_path, bad_scope):
    with pytest.raises(ValueError, match="scope"):
        update_config({"metered": True}, scope=bad_scope, path=tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()


def test_update_rejects_an_unknown_class(tmp_path):
    with pytest.raises(ValueError, match="free"):
        update_config({"free": True}, path=tmp_path / "c.json")


def test_save_atomic(tmp_path, monkeypatch):
    tmp_path = tmp_path / "d"
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={}, path=target)
    old = target.read_bytes()

    calls = []
    real_replace = Path.replace

    def spy(self, dst):
        calls.append((Path(self), Path(dst)))
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", spy)
    save_config(globals_={"local": True, "subscription": True, "metered": True}, scopes={}, path=target)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == target
    assert src != target and src.parent == target.parent
    assert policy(path=target).allowed == frozenset({"local", "subscription", "metered"})
    assert list(tmp_path.iterdir()) == [target]

    # A failure before the replace leaves the old content intact and no tmp behind.
    good = target.read_bytes()

    def boom(self, dst):
        raise OSError("injected failure before replace")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="injected"):
        save_config(globals_=DEFAULT, scopes={}, path=target)
    assert target.read_bytes() == good
    assert list(tmp_path.iterdir()) == [target]
    assert old != good


def test_save_failure_keeps_old_content_when_write_fails(tmp_path, monkeypatch):
    tmp_path = tmp_path / "d"
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={}, path=target)
    before = target.read_bytes()

    def bad_dump(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(consent_mod.json, "dump", bad_dump)
    with pytest.raises(OSError, match="injected"):
        save_config(globals_={"local": True, "subscription": False, "metered": True}, scopes={}, path=target)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_save_error_propagates(tmp_path):
    blocker = _write(tmp_path / "blocker", "x")
    with pytest.raises(OSError):
        save_config(globals_=DEFAULT, scopes={}, path=blocker / "sub" / "consent.json")


def test_save_replace_error_propagates(tmp_path, monkeypatch):
    def boom(self, dst):
        raise PermissionError("injected")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(PermissionError):
        save_config(globals_=DEFAULT, scopes={}, path=tmp_path / "consent.json")


@pytest.mark.parametrize(
    "globals_",
    [
        {"local": 1, "subscription": True, "metered": False},
        {"local": True, "subscription": "no", "metered": False},
        {"local": True, "subscription": True},
    ],
)
def test_save_rejects_non_bool_or_incomplete_globals(tmp_path, globals_):
    target = tmp_path / "consent.json"
    with pytest.raises(TypeError):
        save_config(globals_=globals_, scopes={}, path=target)
    assert not target.exists()


# ---- connection approvals ------------------------------------------------------------------------
#
# The machine owner's approvals of project connections that send a key
# (llm_kit.registry.project_models.endpoint_connection). They live in the same user-level
# file, under ``connections``, and every read and write of them conforms to the schema.

VENDOR = ("direct", "api.vendor.example", "VENDOR_API_KEY")


def _consent_schema() -> dict:
    from llm_kit.schema.formats import CONSENT_V2, load_schema

    return load_schema(CONSENT_V2)


def _conforms(data) -> None:
    from llm_kit.schema.json_schema import validate

    assert validate(_consent_schema(), data) == (), data


def test_an_approval_is_written_read_back_and_conforms_to_the_schema(tmp_path):
    from llm_kit.registry.consent import ConnectionApproval, approve_connection

    target = tmp_path / "consent.json"
    written, config = approve_connection(ConnectionApproval(*VENDOR), path=target)
    assert written == target
    assert config.connections == (ConnectionApproval(*VENDOR),)
    data = json.loads(target.read_text(encoding="utf-8"))
    _conforms(data)
    assert data["connections"] == [{"endpoint": "direct", "host": "api.vendor.example", "key_env": "VENDOR_API_KEY"}]
    assert load_config(target).connections == (ConnectionApproval(*VENDOR),)
    assert [c.to_json() for c in load_config(target).connections] == data["connections"]


def test_approve_and_revoke_are_idempotent_and_keep_the_other_approvals(tmp_path):
    from llm_kit.registry.consent import ConnectionApproval, approve_connection, revoke_connection

    target = tmp_path / "consent.json"
    other = ConnectionApproval("lab", "127.0.0.1:8080", "LAB_KEY")
    approve_connection(ConnectionApproval(*VENDOR), path=target)
    approve_connection(other, path=target)
    _, again = approve_connection(ConnectionApproval(*VENDOR), path=target)
    assert again.connections == (ConnectionApproval(*VENDOR), other)  # sorted, one of each
    _, revoked = revoke_connection(ConnectionApproval(*VENDOR), path=target)
    assert revoked.connections == (other,)
    _, revoked = revoke_connection(ConnectionApproval(*VENDOR), path=target)
    assert revoked.connections == (other,)
    _conforms(json.loads(target.read_text(encoding="utf-8")))
    _, empty = revoke_connection(other, path=target)
    assert empty.connections == ()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "connections" not in data  # a file with no approval reads in every earlier release
    _conforms(data)


def test_a_class_change_keeps_the_approvals_and_an_approval_keeps_the_classes(tmp_path):
    from llm_kit.registry.consent import ConnectionApproval, approve_connection

    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={"a": {"metered": True}}, path=target)
    approve_connection(ConnectionApproval(*VENDOR), path=target)
    _, config = update_config({"metered": True}, path=target)
    assert config.connections == (ConnectionApproval(*VENDOR),)
    assert _globals(config)["metered"] is True and dict(config.scopes["a"]) == {"metered": True}
    _conforms(json.loads(target.read_text(encoding="utf-8")))


def test_the_command_that_approves_is_exact_and_quotes_an_endpoint_name_with_a_space():
    from llm_kit.registry.consent import ConnectionApproval, connection_command

    assert connection_command(ConnectionApproval(*VENDOR)) == (
        "llm-kit consent connection on --endpoint direct --host api.vendor.example --key-env VENDOR_API_KEY")
    assert connection_command(ConnectionApproval("my vendor", "[::1]:8080", "K"), value="off") == (
        "llm-kit consent connection off --endpoint 'my vendor' --host '[::1]:8080' --key-env K")


@pytest.mark.parametrize(("base_url", "host"), [
    ("https://api.vendor.example/v1", "api.vendor.example"),
    ("https://API.Vendor.Example:8443/v1?x=1", "api.vendor.example:8443"),
    ("http://127.0.0.1:8080", "127.0.0.1:8080"),
    ("http://[::1]:8080/v1", "[::1]:8080"),
    ("https://api.vendor.example#frag", "api.vendor.example"),
    ("https://api.vendor.example@collector.example/v1", None),
    ("https://collector.example\\@api.vendor.example/v1", None),
    ("https://api.vendor.example%2ecollector.example/v1", None),
    ("https://-bad.example/v1", None),
    ("https://[not-an-address]/v1", None),
    ("https:///v1", None),
    ("ftp://api.vendor.example/v1", None),
])
def test_the_host_of_a_base_url(base_url, host):
    from llm_kit.registry.consent import connection_host

    assert connection_host(base_url) == host


@pytest.mark.parametrize(
    ("connections", "match"),
    [
        ({"direct": {}}, "'connections' must be a list"),
        (["direct"], r"connections\[0\] must be an object"),
        ([{"endpoint": "direct", "host": "api.vendor.example"}], r"connections\[0\].*key_env"),
        ([{"endpoint": "direct", "host": "api.vendor.example", "key_env": "K", "base_url": "x"}], "base_url"),
        ([{"endpoint": "", "host": "api.vendor.example", "key_env": "K"}], "endpoint"),
        ([{"endpoint": " direct", "host": "api.vendor.example", "key_env": "K"}], "endpoint"),
        ([{"endpoint": "direct", "host": "API.vendor.example", "key_env": "K"}], "host"),
        ([{"endpoint": "direct", "host": "https://api.vendor.example", "key_env": "K"}], "host"),
        ([{"endpoint": "direct", "host": "user@api.vendor.example", "key_env": "K"}], "host"),
        ([{"endpoint": "direct", "host": "api.vendor.example", "key_env": "1K"}], "key_env"),
        ([{"endpoint": "direct", "host": "api.vendor.example", "key_env": None}], "key_env"),
    ],
)
def test_a_malformed_approval_is_a_configuration_error(tmp_path, connections, match):
    target = _write(tmp_path / "c.json", _valid(connections=connections))
    with pytest.raises(ConsentConfigError, match=match):
        load_config(target)


def test_an_approval_with_a_value_it_cannot_hold_is_refused_before_anything_is_written(tmp_path):
    from llm_kit.registry.consent import ConnectionApproval, approve_connection

    target = tmp_path / "consent.json"
    with pytest.raises(ValueError, match="host"):
        approve_connection(ConnectionApproval("direct", "api.vendor.example/v1", "K"), path=target)
    with pytest.raises(ValueError, match="key_env"):
        approve_connection(ConnectionApproval("direct", "api.vendor.example", "NOT A NAME"), path=target)
    assert not target.exists()


@pytest.mark.parametrize("ignore_case", [True, False])
def test_key_variables_compare_as_the_environment_reads_them(monkeypatch, ignore_case):
    from llm_kit.registry.consent import ConnectionApproval, env_name_key

    monkeypatch.setattr(consent_mod, "ENV_NAMES_IGNORE_CASE", ignore_case)
    approval = ConnectionApproval(*VENDOR)
    assert (env_name_key("vendor_api_key") == env_name_key("VENDOR_API_KEY")) is ignore_case
    assert approval.matches("direct", "api.vendor.example", "vendor_api_key") is ignore_case
    assert approval.matches("direct", "api.vendor.example", "VENDOR_API_KEY")
    assert not approval.matches("other", "api.vendor.example", "VENDOR_API_KEY")
    assert not approval.matches("direct", "collector.example", "VENDOR_API_KEY")


def test_the_platform_default_follows_os_environ():
    assert consent_mod.ENV_NAMES_IGNORE_CASE is (os.name == "nt")


def test_every_shape_the_writer_stores_conforms_to_the_schema(tmp_path):
    """The writer's output for each shape it writes: classes only, a scope, and approvals (above)."""
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={}, path=target)
    _conforms(json.loads(target.read_text(encoding="utf-8")))
    update_config({"metered": True}, scope="databench", path=target)
    _conforms(json.loads(target.read_text(encoding="utf-8")))
