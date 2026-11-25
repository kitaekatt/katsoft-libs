"""User usage policy contracts. Run: uv run --extra dev pytest tests/llm-kit/test_model_overrides.py.

Triggers: policy parsing, selection, seat discovery, or models CLI changes.
All files, usage readings, probes and session pins are private to each test.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from llm_kit.cli import models as cli
from llm_kit.dispatch import declaration as d, seats as s, usage_budget as ub
from llm_kit.dispatch.reachability import Reachability
from llm_kit.registry.conserve import Budget, ConserveSpec
from llm_kit.registry.model_endpoints import EndpointEntry
from llm_kit.schema import formats
from llm_kit.schema.json_schema import validate

SPEC = ConserveSpec(pool="seven_day")
RULE = ("Rule: choose a usable reset entry from this declaration; choose another only for "
        "independence or after a failed dispatch, and announce why.")
CASES = json.loads((Path(__file__).resolve().parents[1] / "formats/model-overrides/cases.json").read_text())


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "policy" / "overrides.yaml"
    monkeypatch.setenv("LLM_KIT_MODEL_OVERRIDES_FILE", str(path))

    def write(**scopes):
        document = {"schema_version": 1, **scopes}
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return document

    return path, write


def policy_module():
    return importlib.import_module("llm_kit.registry.model_overrides")


def entry(name, harness="codex", tier=3, family="gpt", spec=SPEC):
    return EndpointEntry(id=name, base_url=None, model=name + "-model", kind="harness",
                         harness=harness, tier=tier, family=family, conserve_usage=spec)


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    entries = {"a": entry("a", "claude", family="other"), "b": entry("b"), "c": entry("c")}
    readings = {
        "a": Budget("available", "seven_day", "ahead", .9, .5, 2_000_000_000),
        "b": Budget("out-of-quota", "seven_day", "spent", 0., .5, 2_000_000_000),
        "c": Budget("under-quota", "seven_day", "behind", .2, .5, 2_000_000_000),
    }
    probes = []
    down = set()

    def check(items, **kwargs):
        probes.extend(items)
        return {name: Reachability("unreachable" if name in down else "reachable", "test", "")
                for name in items}

    monkeypatch.setattr(d, "check_many", check)
    monkeypatch.setattr(d, "pinned_evaluate", lambda name, *a: readings[name])
    monkeypatch.setattr(d, "_fresh_reading", lambda name, *a: readings[name])
    monkeypatch.setattr(s, "check_many", check)
    monkeypatch.setattr(s, "pinned_evaluate", lambda name, *a: readings[name])
    monkeypatch.setattr(ub, "evaluate", lambda spec, harness, **kw: readings["a" if harness == "claude" else "b"])
    monkeypatch.setattr(s, "load_model_config", lambda **kw: {})
    monkeypatch.setattr(s, "discover_model_entries", lambda **kw: SimpleNamespace(entries=entries))
    monkeypatch.setattr(ub, "VERDICT_CACHE", tmp_path / "verdicts.json")
    return SimpleNamespace(entries=entries, readings=readings, probes=probes, down=down)


def test_paths_missing_file_and_reads_create_nothing(policy_file, monkeypatch, tmp_path):
    module = policy_module()
    path, _ = policy_file
    policy = module.load_model_overrides()
    assert policy.path == path
    assert policy.resolve("b", "codex").to_json() == {"disabled": False, "reset_mode": False}
    assert not path.parent.exists()
    from llm_kit.registry import user_paths
    monkeypatch.delenv("LLM_KIT_MODEL_OVERRIDES_FILE")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert user_paths.model_overrides_file() == tmp_path / ".config/llm-kit/model-overrides.yaml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert module.load_model_overrides().path == tmp_path / "xdg/llm-kit/model-overrides.yaml"
    assert not (tmp_path / "xdg").exists()


def test_field_precedence_and_disabled_dominates(policy_file):
    path, write = policy_file
    document = write(harnesses={"codex": {"reset_mode": True, "disabled": True}},
                     models={"b": {"disabled": False}, "c": {"reset_mode": False}})
    module = policy_module()
    assert not validate(formats.load_schema(formats.MODEL_OVERRIDES_V1), document)
    policy = module.load_model_overrides()
    assert policy.resolve("b", "codex").to_json() == {"disabled": False, "reset_mode": True}
    assert policy.resolve("c", "codex").to_json() == {"disabled": True, "reset_mode": False}
    assert policy.resolve("other", "codex").to_json() == {"disabled": True, "reset_mode": False}
    assert policy.to_json({"b": entry("b")})["models"]["b"]["reset_mode"] is True
    assert policy.to_json({})["path"] == str(path)


@pytest.mark.parametrize("document,needle", [(case["document"], case["error"]) for case in CASES["cases"]])
def test_invalid_policy_is_loud_and_matches_schema(policy_file, document, needle):
    path, _ = policy_file
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    module = policy_module()
    with pytest.raises(ValueError, match=needle):
        module.load_model_overrides()
    assert validate(formats.load_schema(formats.MODEL_OVERRIDES_V1), document)


@pytest.mark.parametrize("text", CASES["yaml_cases"])
def test_bad_yaml_is_loud(policy_file, text):
    path, _ = policy_file
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="model.overrides|schema_version"):
        policy_module().load_model_overrides()


@pytest.mark.parametrize("caller", ["session", "process"])
def test_reset_exhausted_usable_order_actual_text_and_json(policy_file, fleet, caller):
    policy_file[1](harnesses={"codex": {"reset_mode": True}})
    ranking = d.describe(["a", "b", "c"], entries=fleet.entries, caller=caller)
    assert [row.id for row in ranking.rendered_entries] == ["b", "c", "a"]
    assert ranking.default.id == "b"
    text = ranking.render()
    assert "actual: out of quota" in text
    assert " 0% left" in ranking.default.status_text
    assert "actual: under quota" in text and "pace 40%" in text
    assert RULE in text
    assert "Reset: b, c have reset available (config); actual usage remains displayed." in text
    data = ranking.to_json()
    assert data["reset_ids"] == ["b", "c"]
    row = data["rendered_entries"][0]
    assert row["reset_mode"] and not row["disabled"] and row["usable"]
    assert row["remaining"] == 0. and row["usability"] == "out-of-quota"


def test_model_reset_off_restores_pace_order_and_reselector_reloads(policy_file, fleet):
    write = policy_file[1]
    write(harnesses={"codex": {"reset_mode": True}}, models={"c": {"reset_mode": False}})
    selector = d.Reselector(["a", "b", "c"], caller="process", entries=fleet.entries)
    assert [row.id for row in selector.ranking().rendered_entries] == ["b", "a", "c"]
    assert selector.select().id == "b"
    write(harnesses={"codex": {"disabled": True}})
    assert selector.select().id == "a"


def test_disabled_is_never_probed_or_default_and_harness_floor(policy_file, fleet):
    policy_file[1](harnesses={"codex": {"disabled": True, "reset_mode": True}})
    ranking = d.describe(["b", "a", "c"], entries=fleet.entries, caller="process")
    assert fleet.probes == ["a"] and ranking.default.id == "a"
    assert [row.id for row in ranking.rendered_entries] == ["a"]
    assert [row["id"] for row in ranking.to_json()["disabled"]] == ["b", "c"]
    assert all(row["disabled"] and not row["reset_mode"] for row in ranking.to_json()["disabled"])
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        d.describe(["b", "c"], entries=fleet.entries, caller="process")
    assert [row.id for row in caught.value.dispositions] == ["b", "c"]
    assert "disabled by config" in str(caught.value)
    assert all(row["disabled"] for row in caught.value.to_json()["dispositions"])


def test_reset_respects_exclusion_reachability_and_consent(policy_file, fleet):
    policy_file[1](harnesses={"codex": {"reset_mode": True}})
    fleet.down.add("c")
    ranking = d.describe(["a", "b", "c"], entries=fleet.entries, caller="process", exclude=["b"])
    assert ranking.default.id == "a" and "b" not in fleet.probes
    assert not ranking.to_json()["reset_ids"] and RULE not in ranking.render()
    from llm_kit.registry.consent import policy
    ranking = d.describe(["a", "b", "c"], entries=fleet.entries, caller="process",
                         consent_policy=policy(path=policy_file[0].parent / "missing-consent.json"),
                         harness_auth={"claude": SimpleNamespace(state="subscription", reason="test"),
                                       "codex": SimpleNamespace(state="metered", reason="test")})
    assert ranking.default.id == "a" and not ranking.to_json()["reset_ids"]


def test_observed_halt_beats_reset_for_pool_siblings_then_expires(policy_file, fleet, monkeypatch):
    policy_file[1](harnesses={"codex": {"reset_mode": True}})
    monkeypatch.setenv("LLM_SCRIPTING_KIT_USAGE_SESSION", "override-test")
    monkeypatch.setattr(d, "pinned_evaluate", ub.pinned_evaluate)
    monkeypatch.setattr(s, "pinned_evaluate", ub.pinned_evaluate)
    monkeypatch.setattr(ub, "evaluate", lambda spec, harness, **kw: fleet.readings["a" if harness == "claude" else "b"])
    ub.record_observed_halt("b", SPEC, entries=fleet.entries, now=100., resets_at=2_000_000_000)
    assert not validate(formats.load_schema(formats.USAGE_VERDICTS_V1), json.loads(ub.VERDICT_CACHE.read_text()))
    ranking = d.describe(["a", "b", "c"], entries=fleet.entries, caller="process")
    assert ranking.default.id == "a"
    assert "observed quota/credit halt" in ranking.render()
    assert "b" not in fleet.probes and "c" not in fleet.probes
    fleet.entries["self"] = entry("self", tier=2)
    result = s.discover_seats("self")
    assert [row.endpoint for row in result.out_of_quota] == ["b", "c"]
    with pytest.raises(d.NoUsableRoutingTarget):
        d.describe(["b", "c"], entries=fleet.entries, caller="process")
    monkeypatch.setattr(ub.time, "time", lambda: 2_000_000_001.)
    assert d.describe(["b", "c"], entries=fleet.entries, caller="process").default.id == "b"


def test_jobs_halted_endpoints_still_exclude_reset(policy_file, fleet, monkeypatch):
    policy_file[1](harnesses={"codex": {"reset_mode": True}})
    from llm_kit.jobs import select
    from llm_kit.jobs.model import Job
    from llm_kit.dispatch.factory import BackendSelection
    from llm_kit.adapters.capabilities import adapter_capabilities

    monkeypatch.setattr(select, "_describe", lambda names, **kw: d.describe(names, entries=fleet.entries, **kw))
    def factory(name, **kwargs):
        row = fleet.entries[name]
        return BackendSelection(backend=SimpleNamespace(name="codex-cli"), endpoint=name,
                                model=row.model, kind="harness", effort=None)

    job = Job(id="unit", models=("a", "b", "c"), prompt="test", contract={"command": ["true"]})
    chosen, _ = select.select_endpoint_with_readings(job, backend_factory=factory, capabilities=adapter_capabilities())
    assert chosen.endpoint == "b"
    fleet.probes.clear()
    chosen, _ = select.select_endpoint_with_readings(job, halted_endpoints={"b", "c"},
                                                    backend_factory=factory, capabilities=adapter_capabilities())
    assert chosen.endpoint == "a" and fleet.probes == ["a"]


@pytest.mark.parametrize("disable_c", [True, False])
def test_seat_override_parity_relation_quota_tier_reset(policy_file, fleet, disable_c):
    vector = json.loads((Path(__file__).resolve().parents[1] / "formats/model-overrides/seats-parity.json").read_text())
    fleet.entries.update({"self": entry("self", tier=2), "top": entry("top", "claude", tier=4),
                          "peer": entry("peer", tier=2, family="peer")})
    fleet.readings.update({"top": fleet.readings["a"], "peer": fleet.readings["c"]})
    policy = vector["policy"]
    if not disable_c:
        policy.pop("models")
    policy_file[1](**policy)
    result = s.discover_seats("self")
    assert {
        "seats": [row.endpoint for row in result.seats],
        "disabled": [row.endpoint for row in result.disabled],
        "reset_endpoints": sorted(result.reset_endpoints),
        "out_of_quota": [row.endpoint for row in result.out_of_quota],
    } == vector["expected" if disable_c else "all_reset_expected"]
    assert ("c" not in fleet.probes) == disable_c
    assert not result.out_of_quota
    data = result.to_json()
    assert all(row["disabled"] for row in data["disabled"])
    reset = next(row for row in data["seats"] if row["endpoint"] == "b")
    assert reset["reset_mode"] and reset["budget"]["remaining"] == 0.


def test_cli_overrides_describe_and_seats_show_policy(policy_file, fleet, monkeypatch, capsys):
    path, write = policy_file
    write(harnesses={"codex": {"reset_mode": True}}, models={"c": {"disabled": True}})
    fleet.entries["self"] = entry("self", tier=2)
    monkeypatch.setattr(cli, "discover_model_entries", lambda **kw: SimpleNamespace(entries=fleet.entries))
    monkeypatch.setattr(cli, "describe", lambda ids, **kw: d.describe(ids, caller="session", entries=fleet.entries))
    assert cli.run(["overrides"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["path"] == str(path) and data["models"]["b"]["reset_mode"]
    assert data["models"]["c"]["disabled"]
    assert cli.run(["describe", "a", "b", "--caller", "session", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["reset_ids"] == ["b"]
    assert cli.run(["seats", "--self", "self"]) == 0
    text = capsys.readouterr().out
    assert "reset available (config)" in text and "actual: out-of-quota" in text
    assert "c" in text and "disabled by config" in text


def test_public_surface_documents_new_api(policy_file):
    module = policy_module()
    package = Path(__file__).resolve().parents[2] / "packages/katsoft-llm-kit"
    surface = json.loads((package / "public-surface.json").read_text())
    names = {row["name"] for row in surface["entries"] if row["status"] == "public"}
    assert module.__name__ in surface["covers"]
    assert {f"{module.__name__}.{name}" for name in module.__all__} == {
        name for name in names if name.startswith(module.__name__ + ".")}
    for name in module.__all__:
        assert getattr(module, name).__doc__


def test_a_legacy_observed_halt_pin_still_beats_reset(policy_file, fleet, monkeypatch):
    policy_file[1](harnesses={"codex": {"reset_mode": True}})
    monkeypatch.setenv("LLM_SCRIPTING_KIT_USAGE_SESSION", "override-test")
    ub.record_observed_halt("b", SPEC, entries=fleet.entries, resets_at=2_000_000_000)
    path = ub.VERDICT_CACHE
    data = json.loads(path.read_text())
    for value in data["verdicts"].values():
        value["budget"].pop("observed_halt", None)
    assert not validate(formats.load_schema(formats.USAGE_VERDICTS_V1), data)
    path.write_text(json.dumps(data))
    monkeypatch.setattr(d, "pinned_evaluate", ub.pinned_evaluate)
    assert d.describe(["a", "b", "c"], entries=fleet.entries, caller="process").default.id == "a"


def test_reset_displays_fresh_actual_reading_when_pin_is_stale(policy_file, fleet, monkeypatch):
    policy_file[1](models={"b": {"reset_mode": True}})
    monkeypatch.setattr(d, "pinned_evaluate", lambda *a: Budget("available", "seven_day", "old pin", .9, .5, 100))
    ranking = d.describe(["a", "b"], entries=fleet.entries, caller="process")
    row = ranking.default
    assert row.id == "b" and row.usability == "out-of-quota"
    assert "actual: out of quota" in ranking.render()
    assert "2033-05-18" in row.status_text
    assert row.to_json()["actual_resets_at"] == fleet.readings["b"].resets_at


def test_reset_expanded_member_precedes_nonreset_members(policy_file, fleet):
    from llm_kit.registry.model_endpoints import RoutingConfig
    policy_file[1](models={"second": {"reset_mode": True}})
    fleet.entries.update({
        "door": EndpointEntry(id="door", base_url="http://door.invalid/v1", model="group", frontdoor=True),
        "first": EndpointEntry(id="first", base_url="http://first.invalid/v1", model="m1",
                               routing=RoutingConfig(group="group", order=1)),
        "second": EndpointEntry(id="second", base_url="http://second.invalid/v1", model="m2",
                                routing=RoutingConfig(group="group", order=2)),
    })
    fleet.down.add("door")
    ranking = d.describe(["door", "a"], caller="process", entries=fleet.entries)
    assert ranking.default.id == "second"
    assert ranking.to_json()["reset_ids"] == ["second"]


def test_cli_shows_reset_seat_blocked_by_observed_halt(policy_file, fleet, monkeypatch, capsys):
    policy_file[1](harnesses={"codex": {"reset_mode": True}})
    monkeypatch.setenv("LLM_SCRIPTING_KIT_USAGE_SESSION", "override-test")
    monkeypatch.setattr(s, "pinned_evaluate", ub.pinned_evaluate)
    monkeypatch.setattr(ub, "evaluate", lambda *a, **kw: fleet.readings["a"])
    ub.record_observed_halt("b", SPEC, entries=fleet.entries, resets_at=2_000_000_000)
    fleet.entries["self"] = entry("self", tier=2)
    assert cli.run(["seats", "--self", "self"]) == 0
    text = capsys.readouterr().out
    assert "b" in text and "observed quota/credit halt" in text
    assert "reset available (config)" in text
