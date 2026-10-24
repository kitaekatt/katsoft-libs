"""A catalogue row that declares NO effort dial.

``xiaomi/mimo-v2.6-pro`` on OpenRouter lists ``reasoning`` but neither
``reasoning_effort`` nor any ``supported_efforts``. Its live-measured off switch
requires ``reasoning-object``. Other endpoint styles keep an empty menu, so a
legacy row cannot inherit the endpoint's low|medium|high menu. The representation is
``efforts: []``: an empty menu means "no effort dial", an explicit refusal of any
effort (refusal kind ``effort``) and nothing sent to the provider. The row can
get it two ways: a style-incompatible measured profile, or an explicit
``efforts: []`` in the project file for any model.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_kit.registry import catalog as catalog_mod
from llm_kit.registry import model_profiles as mp
from llm_kit.registry import project_models as pm
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture
from test_invoke_protocol import World, allow, call, world  # noqa: F401 -- world is a fixture

MIMO = "xiaomi/mimo-v2.6-pro"

_REGISTRY = """\
default: or
models:
  or: {base_url: https://openrouter.ai/api/v1, model: xiaomi/mimo-v2.6-pro, effort_style: top-level, key_env: OPENROUTER_API_KEY}
  other: {base_url: https://example.invalid/v1, model: some/model, effort_style: top-level, key_env: OPENROUTER_API_KEY}
"""


@pytest.fixture()
def project(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _doc(**row):
    return {
        "version": 1,
        "catalog": [{"id": "mimo", "label": "MiMo", "endpoint": "or", "model": MIMO, **row}],
        "selections": {"k": {"model": "mimo", "effort": None}},
    }


def _catalog_row(project, document):
    pm.write_project_models(document, project_root=str(project))
    stored = pm.read_project_models(project_root=str(project))
    built = catalog_mod.build_catalog(
        stored,
        project_root=str(project),
        modes_by_adapter={"openrouter": ["completion"], "claude-cli": ["completion"], "codex-cli": ["completion"]},
    )
    return built["models"][0]


# --- the shipped measured profile ------------------------------------------------


def test_the_mimo_off_only_profile_requires_its_measured_style() -> None:
    profile = mp.profile_for("openrouter", MIMO)
    assert profile is not None and profile.efforts == ("none",)
    assert not profile.no_effort_dial
    assert profile.source == mp.SOURCE_OPENROUTER_LIVE and profile.measured_on == "2026-10-24"
    assert profile.required_effort_style == "reasoning-object"
    assert mp.exact_effort_menu("openrouter", MIMO) == ()  # Empty blocks endpoint-menu fallback.
    assert mp.exact_effort_menu("openrouter", MIMO, effort_style="reasoning-object") == ("none",)
    assert profile.to_json()["efforts"] == ["none"]
    assert profile.to_json()["required_effort_style"] == "reasoning-object"


@pytest.mark.parametrize("style", [None, "top-level", "ninfer", "chat_template_kwargs", "unsupported"])
def test_mimo_never_inherits_an_incompatible_style_menu(style: Optional[str]) -> None:
    endpoint = pm.EndpointInfo("m", "transport", effort_style=style, route="openrouter")
    assert pm.library_effort_menu_with_source(endpoint, MIMO) == ((), mp.MENU_EXACT_MODEL)
    with pytest.raises(mp.HarnessAdapterError, match="requires effort_style='reasoning-object'"):
        mp.check_effort("none", route="openrouter", model=MIMO, fallback=("none", "high"),
                        subject="x", effort_style=style)


def test_check_effort_accepts_only_the_measured_object_control() -> None:
    mp.check_effort("none", route="openrouter", model=MIMO, fallback=None,
                    subject="x", effort_style="reasoning-object")
    mp.check_effort(None, route="openrouter", model=MIMO, fallback=None,
                    subject="x", effort_style="reasoning-object")
    for unmeasured in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        with pytest.raises(mp.HarnessAdapterError, match="accepted efforts: none"):
            mp.check_effort(unmeasured, route="openrouter", model=MIMO, fallback=None,
                            subject="x", effort_style="reasoning-object")


def test_an_empty_profile_is_only_valid_when_declared_so():
    with pytest.raises(ValueError, match="efforts are required"):
        mp.ModelProfile("openrouter", "a/b", (), mp.SOURCE_OPENROUTER, "2026-10-23")
    with pytest.raises(ValueError, match="must agree"):
        mp.ModelProfile("openrouter", "a/b", ("low",), mp.SOURCE_OPENROUTER, "2026-10-23", no_effort_dial=True)
    assert mp.ModelProfile("openrouter", "a/b", (), mp.SOURCE_OPENROUTER, "2026-10-23", no_effort_dial=True).efforts == ()


def test_a_row_for_mimo_inherits_no_endpoint_menu(project):
    row = _catalog_row(project, _doc())
    assert row["efforts"] == []  # empty list, not missing, and not the endpoint's low|medium|high
    assert row["effort_source"] == mp.MENU_EXACT_MODEL


def test_a_selection_effort_for_the_mimo_row_is_refused_at_write(project):
    document = _doc()
    document["selections"]["k"]["effort"] = "high"
    with pytest.raises(pm.ProjectModelsError, match="'high' is not accepted for 'mimo'"):
        pm.validate_project_models(document, project_root=str(project))


def test_check_effort_refuses_any_effort_for_an_empty_menu():
    from llm_kit.registry.effort import HarnessAdapterError

    mp.check_effort(None, route="openrouter", model=MIMO, fallback=None, subject="x")
    with pytest.raises(HarnessAdapterError, match="accepts no effort"):
        mp.check_effort("low", route="openrouter", model=MIMO, fallback=None, subject="x")


# --- an explicit `efforts: []` in the project file --------------------------------


def test_explicit_empty_efforts_is_accepted_for_a_model_with_a_style_menu(project):
    document = _doc(efforts=[])
    document["catalog"][0].update(endpoint="other", model="some/model")
    row = _catalog_row(project, document)
    assert row["efforts"] == []
    assert row["effort_source"] == "project-no-effort"
    stored = pm.read_project_models(project_root=str(project)).document
    assert stored["catalog"][0]["efforts"] == []
    assert b"efforts: []" in pm.render_yaml(stored)


def test_without_the_empty_declaration_a_styled_endpoint_still_gives_its_menu(project):
    document = _doc()
    document["catalog"][0].update(endpoint="other", model="some/model")
    row = _catalog_row(project, document)
    assert row["efforts"] == ["low", "medium", "high"]
    document["selections"]["k"]["effort"] = "high"
    pm.validate_project_models(document, project_root=str(project))
    document["catalog"][0]["efforts"] = []
    with pytest.raises(pm.ProjectModelsError, match="'high' is not accepted for 'mimo'"):
        pm.validate_project_models(document, project_root=str(project))


def test_a_non_empty_declaration_still_checks_the_menu(project):
    document = _doc(efforts=["low"])
    with pytest.raises(pm.ProjectModelsError, match="no effort menu|not in the library menu"):
        pm.validate_project_models(document, project_root=str(project))
    for bad in (None, "low", {}):
        with pytest.raises(pm.ProjectModelsError, match="efforts"):
            pm.validate_project_models(_doc(efforts=bad), project_root=str(project))


def test_empty_menu_row_has_no_missing_delivery_note(project):
    document = _doc(efforts=[])
    pm.write_project_models(document, project_root=str(project))
    built = catalog_mod.build_catalog(
        pm.read_project_models(project_root=str(project)),
        project_root=str(project),
        modes_by_adapter={"openrouter": ["completion"]},
    )
    assert built["models"][0]["efforts"] == []
    assert not [n for n in built["notes"] if "effort" in n]


# --- requests for the row: refusal kind effort, nothing sent -----------------------


def _rewrite(world: World, *, endpoint: str = "openrouter", **row: Any) -> None:
    document = copy.deepcopy(_doc(**row))
    document["catalog"][0].update(endpoint=endpoint)
    pm.write_project_models(document, project_root=str(world.root))


def _use_mimo_style(world: World, style: str) -> str:
    # A distinct registry id keeps the test independent from the shipped
    # openrouter config wrapper, which retains its own top-level style.
    endpoint = "mimo-wire"
    registry = _REGISTRY.replace("default: or", f"default: {endpoint}").replace("  or:", f"  {endpoint}:")
    registry = registry.replace("effort_style: top-level", f"effort_style: {style}")
    world.monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(world.root / "registry.yaml", registry)))
    return endpoint


@pytest.mark.parametrize("declared", [{}, {"efforts": ["none"]}])
def test_mimo_object_catalog_and_invoke_deliver_only_the_off_switch(
    world: World, capsys: pytest.CaptureFixture[str], declared: dict[str, Any]
) -> None:
    allow()
    endpoint = _use_mimo_style(world, "reasoning-object")
    _rewrite(world, endpoint=endpoint, **declared)
    stored = pm.read_project_models(project_root=str(world.root))
    built = catalog_mod.build_catalog(stored, project_root=str(world.root),
                                      modes_by_adapter={"openrouter": ["completion"]})
    row = built["models"][0]
    assert row["efforts"] == ["none"]
    assert row["effort_delivery"] == {"deliverable": True, "style": "reasoning-object"}
    code, doc, err = call(world, capsys, model="mimo", effort="none")
    assert code == 0 and err is None, (doc, err)
    assert doc["resolved"]["effort"]["sent"] == "none"
    assert world.http[0]["extra_body"] == {"reasoning": {"enabled": False}}


def test_mimo_object_null_effort_omits_the_control_without_claiming_a_provider_default(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    allow()
    endpoint = _use_mimo_style(world, "reasoning-object")
    _rewrite(world, endpoint=endpoint)
    code, doc, err = call(world, capsys, model="mimo", effort=None)
    assert code == 0 and err is None, (doc, err)
    assert doc["resolved"]["effort"] == {"requested": None, "sent": None, "source": "request"}
    assert "extra_body" not in world.http[0]


def test_a_mimo_selection_can_choose_the_measured_object_off_switch(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    allow()
    endpoint = _use_mimo_style(world, "reasoning-object")
    document = _doc(endpoint=endpoint)
    document["selections"]["k"]["effort"] = "none"
    pm.write_project_models(document, project_root=str(world.root))
    code, doc, err = call(world, capsys, selection="k")
    assert code == 0 and err is None, (doc, err)
    assert doc["resolved"]["effort"] == {"requested": "none", "sent": "none", "source": "selection"}
    assert world.http[0]["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.parametrize("style,effort,declared", [
    ("top-level", "none", {}),
    ("reasoning-object", "high", {}),
    ("reasoning-object", "none", {"efforts": []}),
])
def test_mimo_rejects_unmeasured_or_project_disabled_controls_before_dispatch(
    world: World, capsys: pytest.CaptureFixture[str], style: str, effort: str, declared: dict[str, Any]
) -> None:
    allow()
    endpoint = _use_mimo_style(world, style)
    _rewrite(world, endpoint=endpoint, **declared)
    code, doc, err = call(world, capsys, model="mimo", effort=effort)
    assert code == 4 and doc is None and err["error"]["kind"] == "effort", (doc, err)
    assert world.http == [] and world.runner_calls == 0


def test_mimo_legacy_catalog_cannot_declare_the_object_off_switch(project: Path) -> None:
    document = _doc(efforts=["none"])
    with pytest.raises(pm.ProjectModelsError, match="no effort menu|not in the library menu"):
        pm.validate_project_models(document, project_root=str(project))


@pytest.mark.parametrize("declared", [{}, {"efforts": []}])
def test_a_request_with_an_explicit_effort_is_refused_and_nothing_is_sent(world, capsys, declared):
    allow()
    _rewrite(world, **declared)
    code, doc, err = call(world, capsys, model="mimo", effort="high")
    assert code == 4 and doc is None and err["error"]["kind"] == "effort", (doc, err)
    assert world.runner_calls == 0


@pytest.mark.parametrize("declared", [{}, {"efforts": []}])
def test_a_request_with_no_effort_sends_no_effort_to_the_provider(world, capsys, declared):
    allow()
    _rewrite(world, **declared)
    code, doc, err = call(world, capsys, model="mimo")
    assert code == 0, (doc, err)
    assert doc["resolved"]["effort"] == {"requested": None, "sent": None, "source": "none"}
    body = world.http[0].get("extra_body", {})
    assert "reasoning_effort" not in body and "chat_template_kwargs" not in body


@pytest.mark.parametrize("declared", [{}, {"efforts": []}])
def test_the_refusal_says_the_model_has_no_effort_dial(world, capsys, declared):
    allow()
    _rewrite(world, **declared)
    code, doc, err = call(world, capsys, model="mimo", effort="high")
    assert code == 4 and err["error"]["kind"] == "effort", (doc, err)
    message = err["error"]["message"]
    assert "no effort dial; send null" in message, message
    assert "no measured effort menu" not in message
