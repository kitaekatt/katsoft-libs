"""The opt-in ``reasoning-object`` effort style: wire shape, collisions and the front door.

Offline only: a fake OpenAI-shaped client records the request kwargs, and the front door's upstream
call is replaced by a recording fake. No network and no key. OpenRouter documents the
``reasoning`` object (https://openrouter.ai/docs/use-cases/reasoning-tokens); whether a given model
accepts it is NOT established here -- that needs a paid live check.
"""

from __future__ import annotations

import copy
import types

import pytest

from llm_kit.adapters import backends as backends_mod
from llm_kit.completion.types import BackendOptions
from llm_kit.frontdoor import server
from llm_kit.registry import effort as eff
from llm_kit.registry import model_profiles as mp
from llm_kit.registry import project_models as pm
from llm_kit.registry.effort import (
    OUTCOME_CALLER_EXTRAS,
    OUTCOME_SUPPRESSED,
    OUTCOME_TRANSLATED,
    OUTCOME_UNSET,
    extract_effort,
    plan_effort,
    place_effort,
)
from llm_kit.registry.model_endpoints import (
    EndpointEntry,
    EndpointRegistry,
    RoutingConfig,
    deployment_effort_style,
    load_endpoint_registry,
)
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

RO = eff.REASONING_OBJECT


# --- vocabulary and typed effort ---------------------------------------------------------------


def test_the_style_is_registered_deliverable_and_opt_in():
    assert RO == "reasoning-object"
    assert RO in eff.EFFORT_STYLES and RO in eff.DELIVERING_STYLES
    assert eff.EffortDelivery(RO, "endpoint").emits == "reasoning"
    assert eff.style_effort_menu(RO) == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    # No default is changed: the existing menus and the default OpenRouter style stay as they were.
    assert eff.style_effort_menu(eff.TOP_LEVEL) == ("low", "medium", "high")
    assert eff.style_effort_menu(eff.NINFER) == ("none", "low", "medium", "xhigh")
    assert eff.style_effort_menu(eff.CHAT_TEMPLATE_KWARGS) == ("low", "medium", "high")


def test_the_object_menu_is_a_subset_of_the_profile_scale():
    assert set(eff.style_effort_menu(RO)) <= set(mp.EFFORT_SCALE)


@pytest.mark.parametrize("value", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_a_typed_effort_is_nested_under_reasoning(value):
    plan = plan_effort({"provider": {"sort": "latency"}}, value, RO)
    assert plan.extra_body == {"provider": {"sort": "latency"}, "reasoning": {"effort": value}}
    assert plan.outcome == OUTCOME_TRANSLATED and plan.applied == {"effort": value}["effort"]
    assert "reasoning_effort" not in plan.extra_body


def test_none_effort_disables_reasoning_and_sends_no_off_literal():
    plan = plan_effort(None, "none", RO)
    assert plan.extra_body == {"reasoning": {"enabled": False}}
    assert plan.outcome == OUTCOME_TRANSLATED


def test_a_typed_effort_of_none_injects_nothing():
    plan = plan_effort({"x": 1}, None, RO)
    assert plan.extra_body == {"x": 1} and plan.outcome == OUTCOME_UNSET


@pytest.mark.parametrize("bad", ["off", "ultra", "", "HIGH"])
def test_an_unaccepted_typed_effort_is_refused(bad):
    with pytest.raises(ValueError, match="not accepted"):
        plan_effort(None, bad, RO)


# --- collisions: a caller reasoning object wins as a whole -------------------------------------


def test_a_caller_object_beats_the_typed_effort_and_keeps_every_member():
    raw = {"effort": "low", "exclude": True, "future_member": [1, 2]}
    plan = plan_effort({"reasoning": raw, "provider": {"order": ["a"]}}, "max", RO)
    assert plan.extra_body == {"reasoning": raw, "provider": {"order": ["a"]}}
    assert plan.outcome == OUTCOME_CALLER_EXTRAS and plan.applied is None
    assert not plan.effort_delivered


def test_a_caller_object_is_sent_verbatim_with_no_translation_or_validation_of_members():
    raw = {"max_tokens": 2000, "effort": "unheard-of", "enabled": True}
    assert plan_effort({"reasoning": raw}, None, RO).extra_body == {"reasoning": raw}
    assert plan_effort({"reasoning": {}}, "high", RO).extra_body == {"reasoning": {}}


def test_a_caller_object_is_copied_not_aliased_and_the_input_is_not_mutated():
    extras = {"reasoning": {"effort": "low", "deep": {"a": [1]}}, "reasoning_effort": None,
              "chat_template_kwargs": {"reasoning_effort": None, "keep": 1}}
    before = copy.deepcopy(extras)
    plan = plan_effort(extras, "high", RO)
    assert extras == before
    assert plan.extra_body["reasoning"] == before["reasoning"]
    assert plan.extra_body["reasoning"] is not extras["reasoning"]
    assert plan.extra_body["reasoning"]["deep"] is not extras["reasoning"]["deep"]
    plan.extra_body["reasoning"]["deep"]["a"].append(2)
    assert extras == before
    # The null legacy controls are removed; unrelated nested members survive.
    assert "reasoning_effort" not in plan.extra_body
    assert plan.extra_body["chat_template_kwargs"] == {"keep": 1}


def test_a_null_reasoning_suppresses_the_object_and_the_typed_effort():
    extras = {"reasoning": None, "provider": {"x": 1}}
    plan = plan_effort(extras, "high", RO)
    assert plan.extra_body == {"provider": {"x": 1}}
    assert plan.outcome == OUTCOME_SUPPRESSED
    assert extras == {"reasoning": None, "provider": {"x": 1}}


@pytest.mark.parametrize("bad", ["high", 3, True, ["effort"], 0, ""])
def test_a_non_mapping_non_null_reasoning_is_refused(bad):
    with pytest.raises(ValueError, match="mapping or null"):
        plan_effort({"reasoning": bad}, None, RO)


@pytest.mark.parametrize(
    "legacy",
    [
        {"reasoning_effort": "high"},
        {"chat_template_kwargs": {"reasoning_effort": "high"}},
        {"reasoning_effort": "low", "chat_template_kwargs": {"reasoning_effort": "high"}},
    ],
)
def test_an_object_beside_a_non_null_legacy_control_is_refused(legacy):
    with pytest.raises(ValueError, match="legacy reasoning_effort"):
        plan_effort({"reasoning": {"exclude": True}, **legacy}, None, RO)


def test_other_styles_keep_reasoning_an_opaque_extra_and_their_precedence():
    for style in (eff.TOP_LEVEL, eff.NINFER, eff.CHAT_TEMPLATE_KWARGS, eff.UNSUPPORTED, None):
        # A non-mapping, and a mapping beside a legacy scalar, are NOT refused outside the opt-in.
        assert plan_effort({"reasoning": "x"}, None, style).extra_body == {"reasoning": "x"}
        both = plan_effort({"reasoning": {"exclude": True}, "reasoning_effort": "high"}, "low", style)
        assert both.extra_body == {"reasoning": {"exclude": True}, "reasoning_effort": "high"}
        assert both.outcome == OUTCOME_CALLER_EXTRAS
    assert plan_effort({"reasoning": {"a": 1}}, "low", eff.TOP_LEVEL).extra_body == {
        "reasoning": {"a": 1},
        "reasoning_effort": "low",
    }


@pytest.mark.parametrize(
    "style,effort,expected",
    [
        (eff.TOP_LEVEL, "high", {"reasoning_effort": "high"}),
        (eff.NINFER, "high", {"reasoning_effort": "xhigh"}),
        (eff.CHAT_TEMPLATE_KWARGS, "medium", {"chat_template_kwargs": {"reasoning_effort": "medium"}}),
    ],
)
def test_legacy_styles_still_emit_their_exact_wire(style, effort, expected):
    assert plan_effort(None, effort, style).extra_body == expected


def test_place_effort_for_the_object_style_does_not_touch_other_keys():
    body = {"reasoning_effort": "stale", "model": "m"}
    assert place_effort(body, "xhigh", RO) == "xhigh"
    assert body == {"reasoning_effort": "stale", "model": "m", "reasoning": {"effort": "xhigh"}}


# --- the direct SDK ----------------------------------------------------------------------------

_REGISTRY = """\
version: 1
default: obj
models:
  obj: {base_url: https://openrouter.ai/api/v1, model: a/b, effort_style: reasoning-object, key_env: OPENROUTER_API_KEY}
  top: {base_url: https://openrouter.ai/api/v1, model: a/b, effort_style: top-level, key_env: OPENROUTER_API_KEY}
  nin: {base_url: https://example.invalid/v1, model: a/b, effort_style: ninfer, key_env: OPENROUTER_API_KEY}
  tpl: {base_url: https://example.invalid/v1, model: a/b, effort_style: chat_template_kwargs, key_env: OPENROUTER_API_KEY}
  uns: {base_url: https://example.invalid/v1, model: a/b, effort_style: unsupported, key_env: OPENROUTER_API_KEY}
"""


@pytest.fixture()
def endpoints(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")


def _client():
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        message = types.SimpleNamespace(content="ok", reasoning_content=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_tokens_details={}),
            model="m",
        )

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    client.with_options = lambda **_: client
    return client, sent


def _complete(endpoint, **options):
    client, sent = _client()
    backend = backends_mod.OpenRouterBackend(endpoint=endpoint, client=client)
    backend.complete("s", "u", model="a/b", options=BackendOptions(**options))
    assert len(sent) == 1
    return sent[0], backend


def test_the_default_openrouter_style_is_still_top_level():
    from llm_kit.registry.models import DEFAULT_MODEL_CONFIG

    assert DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["effort_style"] == "top-level"


@pytest.mark.parametrize(
    "endpoint,effort,expected",
    [
        ("obj", "high", {"reasoning": {"effort": "high"}}),
        ("obj", "max", {"reasoning": {"effort": "max"}}),
        ("obj", "none", {"reasoning": {"enabled": False}}),
        ("top", "high", {"reasoning_effort": "high"}),
        ("nin", "high", {"reasoning_effort": "xhigh"}),
        ("tpl", "low", {"chat_template_kwargs": {"reasoning_effort": "low"}}),
        ("uns", "high", None),
    ],
)
def test_the_wire_body_per_style(endpoints, endpoint, effort, expected):
    sent, backend = _complete(endpoint, effort=effort)
    assert sent.get("extra_body") == expected
    assert "reasoning" not in sent and "reasoning_effort" not in sent  # nothing as a top-level SDK kwarg


def test_the_object_style_reports_reasoning_as_the_emitted_container(endpoints):
    _sent, backend = _complete("obj", effort="low")
    assert backend.endpoint_profile().effort.emits == "reasoning"
    capability = backend.endpoint_capabilities().params["effort"]
    assert capability.emits == "reasoning"
    assert backend.effort_plan(BackendOptions(effort="low")).effort_delivered
    assert "effort" not in backend.params_report(BackendOptions(effort="low"))[0]


def test_no_effort_on_the_object_style_sends_no_extra_body(endpoints):
    sent, _backend = _complete("obj")
    assert "extra_body" not in sent


def test_a_caller_object_on_the_wire_wins_and_keeps_its_members(endpoints):
    extras = {"reasoning": {"max_tokens": 1500, "exclude": True}, "provider": {"sort": "price"}}
    before = copy.deepcopy(extras)
    sent, _ = _complete("obj", effort="high", extras=extras)
    assert sent["extra_body"] == {"reasoning": {"max_tokens": 1500, "exclude": True}, "provider": {"sort": "price"}}
    assert extras == before


def test_a_caller_object_with_no_typed_effort_resolves_the_style_and_is_sent(endpoints):
    sent, backend = _complete("obj", extras={"reasoning": {"effort": "low", "exclude": True}})
    assert sent["extra_body"] == {"reasoning": {"effort": "low", "exclude": True}}
    # A typed effort the caller's object replaced is reported as dropped, not delivered.
    dropped, forwarded = backend.params_report(
        BackendOptions(effort="low", extras={"reasoning": {"exclude": True}})
    )
    assert "effort" in dropped and "extras.reasoning" in forwarded


def test_null_reasoning_removes_the_object_from_the_wire(endpoints):
    sent, _ = _complete("obj", effort="high", extras={"reasoning": None, "provider": {"sort": "price"}})
    assert sent["extra_body"] == {"provider": {"sort": "price"}}


def test_a_null_reasoning_alone_sends_no_extra_body(endpoints):
    sent, _ = _complete("obj", extras={"reasoning": None})
    assert "extra_body" not in sent


@pytest.mark.parametrize(
    "options",
    [
        {"extras": {"reasoning": "high"}},
        {"extras": {"reasoning": 5}, "effort": "low"},
        {"extras": {"reasoning": {"exclude": True}, "reasoning_effort": "high"}},
        {"extras": {"reasoning": {"exclude": True}, "chat_template_kwargs": {"reasoning_effort": "low"}}, "effort": "low"},
        {"effort": "off"},
        {"effort": "ultra"},
    ],
)
def test_malformed_input_fails_before_any_client_or_network(endpoints, options):
    """A refused call builds no client, resolves no model and sends nothing."""
    client, sent = _client()
    backend = backends_mod.OpenRouterBackend(endpoint="obj", client=client)
    backend._ensure_client = lambda: pytest.fail("client built before validation")  # noqa: SLF001
    backend._resolve_model = lambda model: pytest.fail("model resolved before validation")  # noqa: SLF001
    backend.client = None
    with pytest.raises(ValueError):
        backend.complete("s", "u", model="a/b", options=BackendOptions(**options))
    assert sent == []
    # The failure envelope's own report does not raise a second time.
    backend.params_report(BackendOptions(**options))


def test_the_same_object_input_is_opaque_on_every_other_style(endpoints):
    for endpoint in ("top", "nin", "tpl", "uns"):
        sent, _ = _complete(endpoint, extras={"reasoning": "free-form"})
        assert sent["extra_body"] == {"reasoning": "free-form"}
        sent, _ = _complete(endpoint, extras={"reasoning": {"exclude": True}, "reasoning_effort": "high"})
        assert sent["extra_body"] == {"reasoning": {"exclude": True}, "reasoning_effort": "high"}


def test_the_registry_accepts_and_resolves_the_style(endpoints):
    entry = load_endpoint_registry().entries["obj"]
    assert entry.effort_style == RO and entry.effort_style_declared
    assert eff.effort_menu("obj") == eff.style_effort_menu(RO)
    assert eff.effort_menu("top") == ("low", "medium", "high")
    assert eff.lower_effort("obj", "low") == "minimal"
    assert eff.lower_effort("obj", "none") is None


def test_a_declared_default_effort_is_accepted_for_the_object_style(home, tmp_path, monkeypatch):  # noqa: F811
    registry = (
        "version: 1\ndefault: obj\nmodels:\n"
        "  obj: {base_url: https://openrouter.ai/api/v1, model: a/b, effort_style: reasoning-object,"
        " reasoning_effort: medium, key_env: OPENROUTER_API_KEY}\n"
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", registry)))
    assert load_endpoint_registry().entries["obj"].reasoning_effort == "medium"


# --- the project model file --------------------------------------------------------------------


def _doc(endpoint, **row):
    return {
        "version": 1,
        "catalog": [{"id": "r", "label": "R", "endpoint": endpoint, "model": "a/b", **row}],
        "selections": {},
    }


def test_the_object_menu_is_the_library_menu_for_an_unprofiled_model(endpoints, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    document = pm.validate_document(_doc("obj", efforts=["none", "low", "max"]), project_root=str(root))
    assert document["catalog"][0]["efforts"] == ["none", "low", "max"]
    with pytest.raises(pm.ProjectModelsError):
        pm.validate_document(_doc("top", efforts=["max"]), project_root=str(root))


def test_a_row_default_reasoning_is_refused_only_on_the_object_style(endpoints, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    rd = {"request_defaults": {"extras": {"reasoning": {"exclude": True}}}}
    with pytest.raises(pm.ProjectModelsError, match="reserved"):
        pm.validate_document(_doc("obj", **rd), project_root=str(root))
    for endpoint in ("top", "nin", "tpl", "uns"):
        kept = pm.validate_document(_doc(endpoint, **rd), project_root=str(root))
        assert kept["catalog"][0]["request_defaults"]["extras"] == {"reasoning": {"exclude": True}}


# --- the front door ----------------------------------------------------------------------------


def _entry(style, *, declared=True, routing_style=None):
    routing = RoutingConfig(group="g", effort_style=routing_style or eff.TOP_LEVEL)
    return EndpointEntry(
        id="d",
        base_url="https://upstream.invalid/v1",
        model="up/model",
        effort_style=style,
        effort_style_declared=declared,
        routing=routing,
    )


def test_extract_effort_without_a_style_is_the_unchanged_legacy_call():
    body = {"reasoning_effort": "low", "chat_template_kwargs": {"reasoning_effort": "high", "k": 1}, "reasoning": {"x": 1}}
    assert extract_effort(body) == "low"
    assert body == {"chat_template_kwargs": {"reasoning_effort": "high", "k": 1}, "reasoning": {"x": 1}}


@pytest.mark.parametrize(
    "incoming,expected",
    [
        ({"reasoning_effort": "high"}, {"reasoning": {"effort": "high"}}),
        ({"reasoning_effort": "none"}, {"reasoning": {"enabled": False}}),
        ({"chat_template_kwargs": {"reasoning_effort": "low"}}, {"reasoning": {"effort": "low"}}),
        (
            {"chat_template_kwargs": {"reasoning_effort": "low", "keep": 1}},
            {"chat_template_kwargs": {"keep": 1}, "reasoning": {"effort": "low"}},
        ),
        # Top-level wins; the nested duplicate is dropped from the single translated object.
        (
            {"reasoning_effort": "medium", "chat_template_kwargs": {"reasoning_effort": "high"}},
            {"reasoning": {"effort": "medium"}},
        ),
        ({"messages": [], "reasoning_effort": None}, {"messages": []}),
    ],
)
def test_frontdoor_translates_legacy_scalars_to_the_nested_object(incoming, expected):
    before = copy.deepcopy(incoming)
    forwarded, user = server._normalize_body({"model": "g", "user": "u1", **incoming}, _entry(RO))  # noqa: SLF001
    assert forwarded == {"model": "up/model", **expected}
    assert user == "u1"
    assert incoming == before


def test_frontdoor_forwards_a_raw_object_opaque_and_intact():
    raw = {"effort": "low", "exclude": True, "max_tokens": 3, "extra": {"n": [1]}}
    incoming = {"model": "g", "messages": [], "reasoning": raw}
    before = copy.deepcopy(incoming)
    forwarded, _ = server._normalize_body(incoming, _entry(RO))  # noqa: SLF001
    assert forwarded == {"model": "up/model", "messages": [], "reasoning": raw}
    assert forwarded["reasoning"] is not raw
    assert incoming == before
    # Nothing is flattened into a scalar legacy channel.
    assert "reasoning_effort" not in forwarded and "chat_template_kwargs" not in forwarded


def test_frontdoor_null_reasoning_is_removed_for_the_object_target():
    forwarded, _ = server._normalize_body({"model": "g", "reasoning": None, "reasoning_effort": None}, _entry(RO))  # noqa: SLF001
    assert forwarded == {"model": "up/model"}


@pytest.mark.parametrize(
    "incoming",
    [
        {"reasoning": "high"},
        {"reasoning": {"exclude": True}, "reasoning_effort": "high"},
        {"reasoning": {"exclude": True}, "chat_template_kwargs": {"reasoning_effort": "low"}},
        {"reasoning_effort": "off"},
        {"reasoning_effort": "ultra"},
        {"chat_template_kwargs": {"reasoning_effort": 7}},
    ],
)
def test_frontdoor_refuses_malformed_input_for_the_object_target(incoming):
    with pytest.raises(ValueError):
        server._normalize_body({"model": "g", **incoming}, _entry(RO))  # noqa: SLF001


@pytest.mark.parametrize("style", [eff.TOP_LEVEL, eff.NINFER, eff.CHAT_TEMPLATE_KWARGS, eff.UNSUPPORTED, None])
def test_frontdoor_other_targets_keep_the_legacy_behavior(style):
    declared = style is not None
    entry = _entry(style, declared=declared, routing_style=None if declared else eff.TOP_LEVEL)
    raw = {"exclude": True}
    forwarded, _ = server._normalize_body(  # noqa: SLF001
        {"model": "g", "reasoning": raw, "reasoning_effort": "high", "chat_template_kwargs": {"reasoning_effort": "low", "k": 1}},
        entry,
    )
    resolved = deployment_effort_style(entry)
    assert forwarded["reasoning"] == raw  # opaque
    if resolved == eff.NINFER:
        assert forwarded["reasoning_effort"] == "xhigh"
    elif resolved in (eff.TOP_LEVEL,):
        assert forwarded["reasoning_effort"] == "high"
    elif resolved == eff.CHAT_TEMPLATE_KWARGS:
        assert forwarded["chat_template_kwargs"] == {"reasoning_effort": "high", "k": 1} and "reasoning_effort" not in forwarded
    else:
        assert "reasoning_effort" not in forwarded
    # Even an unaccepted scalar is not refused outside the opt-in target.
    server._normalize_body({"model": "g", "reasoning_effort": "off"}, entry)  # noqa: SLF001


def test_frontdoor_http_400_for_a_malformed_object_and_the_upstream_body_for_a_good_one(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    posted = []

    class FakeResponse:
        status_code = 200
        content = b'{"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}'
        headers = {"content-type": "application/json"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, json=None, headers=None):
            posted.append(json)
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    registry = EndpointRegistry(entries={"d": _entry(RO)})
    app = server.create_app(registry, access_log=tmp_path / "access.jsonl")
    client = TestClient(app)

    bad = client.post("/v1/chat/completions", json={"model": "g", "messages": [], "reasoning": "high"})
    assert bad.status_code == 400 and "mapping or null" in bad.json()["error"]["message"]
    assert posted == []

    good = client.post(
        "/v1/chat/completions",
        json={"model": "g", "messages": [], "reasoning": {"effort": "low", "exclude": True}},
    )
    assert good.status_code == 200
    assert posted == [{"model": "up/model", "messages": [], "reasoning": {"effort": "low", "exclude": True}}]

    scalar = client.post("/v1/chat/completions", json={"model": "g", "messages": [], "reasoning_effort": "none"})
    assert scalar.status_code == 200
    assert posted[-1] == {"model": "up/model", "messages": [], "reasoning": {"enabled": False}}

    # The refused call released nothing it did not take: a later call still gets a slot.
    assert client.get("/health").json()["deployments"][0]["in_flight"] == 0


# --- the pipeline's registry backend -----------------------------------------------------------


def test_the_pipeline_backend_keys_and_sends_the_object_style(endpoints):
    from llm_kit.pipeline.runtime.backends import ModelEndpointBackend

    client, sent = _client()
    backend = ModelEndpointBackend(endpoint="obj", client=client)
    effective = backend.effective_options(BackendOptions(effort="medium"))
    assert effective.extras == {"reasoning": {"effort": "medium"}}
    raw = {"reasoning": {"exclude": True}}
    # An object-only call resolves the style too, and the object wins over the entry's own default.
    assert backend.effective_options(BackendOptions(extras=raw)).extras == raw
    assert backend.effective_options(BackendOptions(effort="low", extras=raw)).extras == raw
    with pytest.raises(ValueError, match="mapping or null"):
        backend.effective_options(BackendOptions(extras={"reasoning": "x"}))
    assert sent == []
    backend.complete("s", "u", model="a/b", options=BackendOptions(effort="none"))
    assert sent[0]["extra_body"] == {"reasoning": {"enabled": False}}
