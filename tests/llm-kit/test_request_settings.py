"""Request settings from the project model file: the request protocol 2 really sends, and the compare plan.

The shared cases in ``tests/formats/project-models/requests.json`` state the OpenAI-compatible body
fields of each resolved request (``wire``). Here protocol 2 runs every such case through the real
``OpenAICompatBackend`` with a recording client (the ``world`` fixture of ``test_invoke_protocol``),
and the recorded request must carry exactly those fields: the TypeScript reader's ``chatFields`` runs
the same cases, so both languages send the same body. No network and no paid call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_kit.registry import project_models as pm
from llm_kit.registry import request_settings as rs
from llm_kit.registry.models import DEFAULT_MODEL_CONFIG
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture
from test_invoke_protocol import allow, call, world  # noqa: F401 -- world is a fixture

REPO = Path(__file__).resolve().parents[2]
CASES = json.loads((REPO / "tests" / "formats" / "project-models" / "requests.json").read_text(encoding="utf-8"))
SHIPPED_STYLE = DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["effort_style"]
# Protocol 2 carries these call settings; a case whose call needs another is checked by the resolver only.
_PROTOCOL_CALL = {"effort": "effort", "max_output_tokens": "max_tokens"}


def _lines(spec: dict) -> str:
    return "\n".join(spec["lines"]) + "\n"


def _declares_style(case: dict) -> bool:
    return any(line.startswith("endpoints:") for line in case["tracked"]["lines"])


def _wire_cases() -> list:
    """The cases protocol 2 can send: the machine's openrouter style is the case's consumer style when the file has none."""
    out = []
    for case in CASES["cases"]:
        if "wire" not in case or not set(case.get("call", {})) <= set(_PROTOCOL_CALL):
            continue
        if not _declares_style(case) and case["wire"]["endpoint_style"] != SHIPPED_STYLE:
            continue
        out.append(case["name"])
    return out


def test_most_wire_cases_run_through_protocol_2():
    """A guard: the filter above must not quietly drop the wire cases."""
    with_wire = [c for c in CASES["cases"] if "wire" in c]
    assert len(_wire_cases()) >= len(with_wire) - 1, sorted(c["name"] for c in with_wire)


def _sent_fields(kwargs: dict) -> dict:
    """The body fields of one recorded request, as ``chatFields`` names them (no model, messages or user)."""
    fields = {key: kwargs[key] for key in ("max_tokens", "temperature") if key in kwargs}
    fields.update(kwargs.get("extra_body") or {})
    return fields


@pytest.mark.parametrize("name", _wire_cases())
def test_protocol_2_sends_the_wire_of_each_case(world, capsys, name):  # noqa: F811
    case = next(c for c in CASES["cases"] if c["name"] == name)
    models = world.root / pm.DEFAULT_FILE
    models.write_text(_lines(case["tracked"]), encoding="utf-8")
    if "local" in case:
        (world.root / ".llm-kit" / "models.local.yaml").write_text(_lines(case["local"]), encoding="utf-8")
    allow()
    request = {_PROTOCOL_CALL[key]: value for key, value in case.get("call", {}).items()}
    code, doc, err = call(world, capsys, selection=case["key"], **request)
    expect = case["wire"]["expect"]
    if "error" in expect:
        assert code == 4 and doc is None and world.http == [], (code, doc, err)
        return
    assert code == 0, (doc, err)
    assert _sent_fields(world.http[0]) == expect["fields"]


def test_a_harness_row_refuses_a_temperature_it_cannot_send(world, capsys):  # noqa: F811
    """A setting the adapter has no control for is refused before dispatch, not dropped."""
    (world.root / pm.DEFAULT_FILE).write_text(
        "version: 1\ncatalog:\n  - {id: sonnet, label: Sonnet, endpoint: sonnet, model: claude-sonnet-5-5}\n"
        "requests:\n  cold: {temperature: 0}\nselections:\n  k: {model: sonnet, request: cold}\n",
        encoding="utf-8",
    )
    allow()
    code, doc, err = call(world, capsys, selection="k")
    assert code == 4 and doc is None and err["error"]["kind"] == "configuration", err
    assert "temperature" in err["error"]["message"] and world.runner_calls == 0


def test_a_call_effort_beats_a_selection_budget_on_the_wire(world, capsys):  # noqa: F811
    """The call is the top reasoning layer: its effort is sent and the profile's budget is not."""
    (world.root / pm.DEFAULT_FILE).write_text(
        "version: 1\nendpoints:\n  openrouter: {effort_style: reasoning-object}\n"
        "catalog:\n  - {id: a, label: A, endpoint: openrouter, model: vendor/a}\n"
        "requests:\n  think: {reasoning_budget: 512}\nselections:\n  k: {model: a, request: think}\n",
        encoding="utf-8",
    )
    allow()
    code, doc, err = call(world, capsys, selection="k", effort="high")
    assert code == 0, err
    assert world.http[0]["extra_body"] == {"reasoning": {"effort": "high"}}
    assert doc["resolved"]["effort"] == {"requested": "high", "sent": "high", "source": "request"}


def test_request_for_and_resolve_request_agree():
    document = pm.validate_portable_document(pm.parse_yaml_document(
        b"version: 1\ncatalog:\n  - {id: a, label: A, endpoint: e, model: m, traits: [t]}\n"
        b"requests:\n  p: {temperature: 0.2}\nselections:\n  k: {model: a, request: p}\n"
    ))
    by_key = rs.resolve_request(document, "k", call={"top_p": 0.5})
    assert by_key == rs.request_for(document, {"model": "a", "effort": None, "request": "p"}, call={"top_p": 0.5})
    assert (by_key["temperature"], by_key["top_p"], by_key["traits"]) == (0.2, 0.5, ["t"])


# --- binding: the project file's effort style ------------------------------------------------------------

_UNMEASURED = (
    "version: 1\n{endpoints}catalog:\n  - {{id: k, label: K, endpoint: openrouter, model: vendor/unmeasured}}\n"
    "selections:\n  chat: {{model: k, effort: none}}\n"
)


def test_effort_none_on_an_unmeasured_model_needs_the_project_style(home, tmp_path):  # noqa: F811
    """The machine's openrouter style (top-level) has no none; the project file's reasoning-object has."""
    root = tmp_path / "proj"
    write(root / pm.DEFAULT_FILE, _UNMEASURED.format(endpoints=""))
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.read_project_models(project_root=str(root))
    assert caught.value.where == "selections['chat'].effort"
    write(root / pm.DEFAULT_FILE, _UNMEASURED.format(endpoints="endpoints:\n  openrouter: {effort_style: reasoning-object}\n"))
    loaded = pm.read_project_models(project_root=str(root))
    assert loaded.document["endpoints"] == {"openrouter": {"effort_style": "reasoning-object"}}


def test_a_harness_endpoint_takes_no_project_style(home, tmp_path):  # noqa: F811
    root = tmp_path / "proj"
    write(root / pm.DEFAULT_FILE, "version: 1\nendpoints:\n  luna: {effort_style: top-level}\n"
                                  "catalog:\n  - {id: l, label: L, endpoint: luna, model: gpt-6-luna}\n")
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.read_project_models(project_root=str(root))
    assert caught.value.where == "endpoints['luna'].effort_style"
    # Portable validation cannot know the endpoint is a harness, so it accepts the file.
    assert pm.validate_portable_document(pm.parse_yaml_document((root / pm.DEFAULT_FILE).read_bytes()))


def test_the_catalog_reports_the_project_style_and_the_traits(home, tmp_path):  # noqa: F811
    from llm_kit.adapters.capabilities import adapter_capabilities
    from llm_kit.registry import catalog as catalog_mod

    root = tmp_path / "proj"
    write(root / pm.DEFAULT_FILE, "version: 1\nendpoints:\n  openrouter: {effort_style: reasoning-object}\n"
                                  "catalog:\n  - {id: k, label: K, endpoint: openrouter, model: vendor/m, traits: [t]}\n")
    tracked = pm.read_project_models(project_root=str(root))
    built = catalog_mod.build_catalog(tracked, project_root=str(root),
                                      modes_by_adapter={n: c.call_modes for n, c in adapter_capabilities().items()})
    (row,) = built["models"]
    assert row["effort_delivery"] == {"deliverable": True, "style": "reasoning-object"}
    assert row["traits"] == ["t"] and "none" in row["efforts"]


# --- the personal layer writer ---------------------------------------------------------------------------


def test_overriding_only_the_model_keeps_the_request_profile(home, tmp_path):  # noqa: F811
    root = tmp_path / "proj"
    write(root / pm.DEFAULT_FILE, "version: 1\ncatalog:\n  - {id: a, label: A, endpoint: e, model: m}\n"
                                  "  - {id: b, label: B, endpoint: e, model: n}\n"
                                  "requests:\n  cold: {temperature: 0}\nselections:\n  k: {model: a, request: cold}\n")
    pm.set_local_override("k", "b", project_root=str(root))
    local = pm.read_resolved_models(project_root=str(root)).selections["k"]["effective"]
    assert local == {"model": "b", "effort": None, "request": "cold"}
    pm.set_local_override("k", "b", request=None, project_root=str(root))
    local = pm.read_resolved_models(project_root=str(root)).selections["k"]["effective"]
    assert local == {"model": "b", "effort": None}
