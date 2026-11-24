"""The shared file formats: schemas, shared cases, reader agreement and writers.

The cases under ``tests/formats/`` are the contract every reader runs, in any
language (docs/reference/llm-kit-formats.md). This module runs them against the
Python readers, checks that each Python writer's output conforms to its schema,
and proves the prompt reader change kept the graph node hashes of LF files.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_kit.schema import formats
from llm_kit.graph import loader
from llm_kit.graph.hashing import hash_graph
from llm_kit.graph.loader import load_graph, prompt_body, prompt_parts, prompt_slots, resolve_params
from llm_kit.graph.model import GraphError, Op, OpRegistry, ParamSpec
from llm_kit.graph.prompt import PromptError, PromptFile, parse_prompt_file, read_prompt_file, render_prompt, render_prompt_file
from llm_kit.graph.testing import make_project
from llm_kit.registry import project_models as pm
from llm_kit.registry import request_settings as rs
from llm_kit.schema.json_schema import check_schema, validate
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "packages" / "katsoft-llm-kit"
CASES = REPO / "tests" / "formats"
REGISTRY_CASES = json.loads((CASES / "project-models" / "cases.json").read_text(encoding="utf-8"))
REQUEST_CASES = json.loads((CASES / "project-models" / "requests.json").read_text(encoding="utf-8"))
PROMPT_CASES = json.loads((CASES / "prompt-file" / "cases.json").read_text(encoding="utf-8"))
TRACKED_SCHEMA = formats.load_schema(formats.PROJECT_MODELS_V1)
LOCAL_SCHEMA = formats.load_schema(formats.PROJECT_MODELS_LOCAL_V1)
PROMPT_SCHEMA = formats.load_schema(formats.PROMPT_FILE_V1)


def input_bytes(spec: dict) -> bytes:
    """The bytes a case input names: lines (LF-joined, final LF), text (UTF-8) or base64."""
    (kind, value), = spec.items()
    if kind == "lines":
        return ("\n".join(value) + "\n").encode("utf-8")
    if kind == "text":
        return value.encode("utf-8")
    if kind == "base64":
        return base64.b64decode(value)
    raise AssertionError(f"unknown input kind {kind!r}")


def by_name(cases: list) -> dict:
    return {case["name"]: case for case in cases}


# ---------------------------------------------------------------------------
# the schemas
# ---------------------------------------------------------------------------


def test_every_format_has_one_schema_file_in_the_subset():
    shipped = sorted(p.name for p in (PACKAGE / "src" / "llm_kit" / "schema" / "formats").glob("*.json"))
    assert shipped == sorted(formats.SCHEMA_FILES.values())
    for format_id in formats.SCHEMA_FILES:
        schema = formats.load_schema(format_id)
        check_schema(schema)
        assert schema["$id"] == format_id
        assert "docs/reference/llm-kit-formats.md" in schema["description"]


def test_load_schema_refuses_an_unknown_id_and_returns_a_copy():
    with pytest.raises(KeyError, match="unknown llm-kit format"):
        formats.load_schema("llm-kit.nothing/v1")
    first = formats.load_schema(formats.PROMPT_FILE_V1)
    first["title"] = "changed"
    assert formats.load_schema(formats.PROMPT_FILE_V1)["title"] != "changed"


def test_the_format_ids_match_the_readers():
    assert pm.SCHEMA == formats.PROJECT_MODELS_V1
    assert REGISTRY_CASES["format"] == formats.PROJECT_MODELS_V1
    assert PROMPT_CASES["format"] == formats.PROMPT_FILE_V1
    here = CASES / "project-models"
    assert (here / REGISTRY_CASES["schemas"]["tracked"]).resolve() == \
        PACKAGE / "src" / "llm_kit" / "schema" / "formats" / formats.SCHEMA_FILES[formats.PROJECT_MODELS_V1]
    assert (here / REGISTRY_CASES["schemas"]["local"]).resolve() == \
        PACKAGE / "src" / "llm_kit" / "schema" / "formats" / formats.SCHEMA_FILES[formats.PROJECT_MODELS_LOCAL_V1]
    assert (CASES / "prompt-file" / PROMPT_CASES["schema"]).resolve() == \
        PACKAGE / "src" / "llm_kit" / "schema" / "formats" / formats.SCHEMA_FILES[formats.PROMPT_FILE_V1]


def test_the_contract_document_names_every_format():
    text = (REPO / "docs" / "reference" / "llm-kit-formats.md").read_text(encoding="utf-8")
    for format_id in formats.SCHEMA_FILES:
        assert format_id in text, format_id
    assert text.isascii()


# ---------------------------------------------------------------------------
# the project model file: shared cases
# ---------------------------------------------------------------------------


def _is_json(value) -> bool:
    """Whether ``value`` is JSON data: no cycle, no NaN, no foreign type, text keys only."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False

    def text_keys(item) -> bool:
        if isinstance(item, dict):
            return all(isinstance(k, str) and text_keys(v) for k, v in item.items())
        if isinstance(item, list):
            return all(text_keys(v) for v in item)
        return True

    return text_keys(value)


def _portable(case: dict):
    """``(stage, outcome)``: the first refusal ``("error", (layer, error, data))`` or the results."""
    try:
        tracked_data = pm.parse_yaml_document(input_bytes(case["tracked"]))
    except pm.ProjectModelsError as exc:
        return "error", ("tracked", exc, None)
    try:
        tracked = pm.validate_portable_document(tracked_data)
    except pm.ProjectModelsError as exc:
        return "error", ("tracked", exc, tracked_data)
    if "local" not in case:
        return "ok", (tracked_data, tracked, None, None)
    try:
        local_data = pm.parse_yaml_document(input_bytes(case["local"]))
    except pm.ProjectModelsError as exc:
        return "error", ("local", exc, None)
    try:
        local = pm.validate_portable_local_document(local_data, tracked_data)
    except pm.ProjectModelsError as exc:
        return "error", ("local", exc, local_data)
    return "ok", (tracked_data, tracked, local_data, local)


@pytest.mark.parametrize("name", sorted(by_name(REGISTRY_CASES["cases"])))
def test_registry_case(name):
    case = by_name(REGISTRY_CASES["cases"])[name]
    expect = case["expect"]
    stage, outcome = _portable(case)
    if "error" in expect:
        assert stage == "error", f"accepted: {outcome}"
        layer, exc, data = outcome
        assert (layer, exc.code, exc.where) == (expect["error"]["layer"], expect["error"]["code"],
                                                expect["error"]["path"]), str(exc)
        schema = LOCAL_SCHEMA if layer == "local" else TRACKED_SCHEMA
        if "schema" in expect:
            assert data is not None and _is_json(data)
            verdict = "refuses" if validate(schema, data) else "accepts"
            assert verdict == expect["schema"]
        else:
            assert data is None or not _is_json(data), "a case with JSON data states the schema verdict"
        return
    assert stage == "ok", str(outcome[1])
    tracked_data, tracked, local_data, local = outcome
    assert validate(TRACKED_SCHEMA, tracked_data) == ()
    assert validate(TRACKED_SCHEMA, tracked) == ()
    if "document" in expect:
        assert tracked == expect["document"]
    if "local" in case:
        assert validate(LOCAL_SCHEMA, local_data) == ()
        assert validate(LOCAL_SCHEMA, local) == ()
        assert local == expect["local"]
        assert pm.resolve_selections(tracked["selections"], local["selections"]) == expect["resolved"]


def test_the_registry_cases_cover_the_ruling_list():
    names = set(by_name(REGISTRY_CASES["cases"]))
    for needed in ("yaml-1.1-on-is-true", "yaml-1.1-yes-in-efforts", "yaml-1.1-date-label",
                   "duplicate-top-level-key", "yaml-1.1-integer-forms", "crlf-line-ends", "cr-line-ends",
                   "byte-order-mark", "malformed-utf-8", "omitted-versus-empty-efforts",
                   "omitted-versus-null-effort", "local-only-key", "local-equal-override",
                   "anchors-and-aliases", "alias-cycle", "integer-above-exact-range", "yaml-1.1-float-forms",
                   "wire-model", "wire-model-empty", "declared-none-effort"):
        assert needed in names, needed


# Binding refuses these portable-valid cases (the fake registry below resolves every endpoint).
BINDING_REFUSES = {"reserved-extras-are-binding": "catalog[0].request_defaults.extras"}
MENU = ("none", "low", "medium", "high", "xhigh", "max")
_REGISTRY = "default: api\nmodels:\n  api: {base_url: http://h.invalid/v1, model: m}\n"


@pytest.fixture()
def bound_root(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.mark.parametrize("name", sorted(by_name(REGISTRY_CASES["cases"])))
def test_binding_never_accepts_what_portable_refuses(bound_root, name):
    """Full validation = portable + binding: the same first problem, or a binding refusal on top."""
    case = by_name(REGISTRY_CASES["cases"])[name]
    expect = case["expect"]
    menu_for = lambda info, model: MENU  # noqa: E731

    def full():
        try:
            tracked_data = pm.parse_yaml_document(input_bytes(case["tracked"]))
            tracked = pm.validate_document(tracked_data, project_root=str(bound_root), menu_for=menu_for)
        except pm.ProjectModelsError as exc:
            return ("tracked", exc.where)
        if "local" not in case:
            return tracked
        local_file = pm.resolve_local_file(pm.resolve_project_file(str(bound_root)))
        project = pm.ProjectModels(pm.DEFAULT_FILE, "0" * 64, tracked)
        try:
            local_data = pm.parse_yaml_document(input_bytes(case["local"]))
            return pm.validate_local_document(local_data, project, local_file, menu_for=menu_for)
        except pm.ProjectModelsError as exc:
            return ("local", exc.where)

    got = full()
    if "error" in expect:
        assert got == (expect["error"]["layer"], expect["error"]["path"])
    elif name in BINDING_REFUSES:
        assert got == ("tracked", BINDING_REFUSES[name])
    elif "document" in expect:
        assert got == expect["document"]
    else:
        assert got == expect["local"]


def test_portable_validation_needs_no_pyyaml(monkeypatch):
    document = {"version": 1, "catalog": [{"id": "a", "label": "A", "endpoint": "api", "model": "m"}],
                "selections": {"k": {"model": "a"}}}
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert pm.validate_portable_document(document)["selections"] == {"k": {"model": "a", "effort": None}}
    assert pm.validate_portable_local_document({"version": 1}, document) == {"version": 1, "selections": {}}
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_document(document)
    assert caught.value.code == pm.CODE_DEPENDENCY


def test_a_portable_local_check_reports_a_bad_tracked_document_first():
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_portable_local_document({"version": 1}, {"version": 2})
    assert caught.value.where == "version"


def test_the_error_path_survives_the_local_file_name(bound_root):
    pm.write_project_models({"version": 1, "catalog": [{"id": "a", "label": "A", "endpoint": "api", "model": "m"}]},
                            project_root=str(bound_root))
    write(bound_root / ".llm-kit" / "models.local.yaml", "version: 1\nselections:\n  k: {model: ghost}\n")
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.read_resolved_models(project_root=str(bound_root))
    assert str(caught.value).startswith(".llm-kit/models.local.yaml: selections['k'].model:")
    assert caught.value.where == "selections['k'].model"


# ---------------------------------------------------------------------------
# the project model file: the resolved request (shared cases)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(by_name(REQUEST_CASES["cases"])))
def test_request_case(name):
    """Each case's request resolves to exactly the expected request, or refuses at the expected path."""
    case = by_name(REQUEST_CASES["cases"])[name]
    stage, outcome = _portable(case)
    assert stage == "ok", str(outcome[1])
    _tracked_data, tracked, _local_data, local = outcome
    overrides = local["selections"] if local is not None else None
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(pm.ProjectModelsError) as caught:
            rs.resolve_request(tracked, case["key"], overrides=overrides, call=case.get("call"))
        assert (caught.value.code, caught.value.where) == (expect["error"]["code"], expect["error"]["path"]), \
            str(caught.value)
        return
    got = rs.resolve_request(tracked, case["key"], overrides=overrides, call=case.get("call"))
    assert got == expect["request"]
    assert json.loads(json.dumps(got)) == got, "the resolved request is JSON data"


def test_the_request_cases_cover_every_rule():
    names = set(by_name(REQUEST_CASES["cases"]))
    for needed in ("row-defaults-only", "profile-over-row", "call-over-profile", "extras-merge-per-key",
                   "legacy-row-extras-are-read-as-settings", "traits-are-not-sent",
                   "project-style-places-effort-none", "row-budget-when-no-effort",
                   "selection-effort-beats-row-budget", "call-effort-beats-profile-budget",
                   "call-null-effort-sends-no-reasoning", "call-effort-and-budget", "local-layer-selection",
                   "unknown-selection", "wire-model-is-resolved"):
        assert needed in names, needed
    assert REQUEST_CASES["format"] == formats.PROJECT_MODELS_V1


# ---------------------------------------------------------------------------
# writers of the project model file
# ---------------------------------------------------------------------------


def test_graph_testing_make_project_writes_a_conforming_file(tmp_path):
    project = make_project(tmp_path, {"a.b": None, "c.d": "high", "with space": None})
    data = pm.parse_yaml_document((project.root / pm.DEFAULT_FILE).read_bytes())
    assert validate(TRACKED_SCHEMA, data) == ()
    assert set(pm.validate_portable_document(data)["selections"]) == {"a.b", "c.d", "with space"}


@pytest.mark.parametrize("name", sorted(n for n, c in by_name(REGISTRY_CASES["cases"]).items()
                                        if "error" not in c["expect"]))
def test_the_canonical_rendering_of_every_valid_case_conforms_and_reads_back(name):
    case = by_name(REGISTRY_CASES["cases"])[name]
    stage, (tracked_data, tracked, _local_data, local) = _portable(case)
    assert stage == "ok"
    rendered = pm.parse_yaml_document(pm.render_yaml(tracked))
    assert validate(TRACKED_SCHEMA, rendered) == ()
    assert pm.validate_portable_document(rendered) == tracked
    if local is not None:
        rendered_local = pm.parse_yaml_document(pm.render_local_yaml(local))
        assert validate(LOCAL_SCHEMA, rendered_local) == ()
        assert pm.validate_portable_local_document(rendered_local, tracked) == local


# ---------------------------------------------------------------------------
# the prompt file: shared cases and reader agreement
# ---------------------------------------------------------------------------


def _template_op() -> Op:
    return Op("t.template", "1", lambda ctx: None,
              params={"prompt": ParamSpec("template", required=True, slots="slots"), "slots": ParamSpec("exprs")})


def _template_graph(tmp_path: Path, data: bytes):
    (tmp_path / "p.md").write_bytes(data)
    write(tmp_path / "g.graph.yaml", "id: t.p\nnodes:\n  w:\n    op: t.template\n    prompt: p.md\n")
    return load_graph(tmp_path / "g.graph.yaml")


@pytest.mark.parametrize("name", sorted(by_name(PROMPT_CASES["cases"])))
def test_prompt_case(tmp_path, name):
    case = by_name(PROMPT_CASES["cases"])[name]
    data = input_bytes(case["input"])
    expect = case["expect"]
    path = tmp_path / "p.md"
    path.write_bytes(data)
    if "error" in expect:
        assert expect["error"] == "invalid"
        with pytest.raises(GraphError):
            parse_prompt_file(data)
        with pytest.raises(GraphError):
            read_prompt_file(path)
        with pytest.raises(GraphError):
            render_prompt_file(path, {})
        graph, op = _template_graph(tmp_path, data), _template_op()
        with pytest.raises(GraphError, match="not UTF-8|it may have one"):
            resolve_params(graph.nodes["w"], op, graph)
        return
    parsed = parse_prompt_file(data)
    assert parsed.to_json() == expect
    assert validate(PROMPT_SCHEMA, parsed.to_json()) == ()
    from_file = read_prompt_file(path)
    assert from_file.source == str(path) and validate(PROMPT_SCHEMA, from_file.to_json()) == ()
    assert from_file == PromptFile(**{**expect, "slots": tuple(expect["slots"]), "source": str(path)})
    # The string readers and the graph loader agree with the file reader.
    text = data.decode("utf-8")
    assert prompt_parts(text) == (expect["header"], expect["body"])
    assert prompt_body(text) == expect["body"]
    assert list(prompt_slots(text)) == expect["slots"]
    graph = _template_graph(tmp_path, data)
    param = resolve_params(graph.nodes["w"], _template_op(), graph)["prompt"]
    assert (param.body, list(param.slots)) == (expect["body"], expect["slots"])
    for entry in case.get("render", []):
        kwargs = {"suffix": entry["suffix"]} if "suffix" in entry else {}
        if "error" in entry:
            assert entry["error"] == "slot"
            with pytest.raises(PromptError):
                render_prompt(text, entry["slots"], **kwargs)
            with pytest.raises(PromptError):
                render_prompt_file(path, entry["slots"], **kwargs)
        else:
            assert render_prompt(text, entry["slots"], **kwargs) == entry["output"]
            assert render_prompt_file(path, entry["slots"], **kwargs) == entry["output"]


def test_the_prompt_cases_cover_the_ruling_list():
    names = set(by_name(PROMPT_CASES["cases"]))
    for needed in ("crlf", "lone-cr", "byte-order-mark", "malformed-utf-8", "two-marker-lines",
                   "two-marker-lines-with-crlf", "value-containing-a-slot", "non-ascii-body"):
        assert needed in names, needed


def test_a_crlf_or_bom_copy_of_a_template_has_the_same_node_hash(tmp_path):
    op = _template_op()
    registry = OpRegistry({op.name: op})
    lf = b"head\n---PROMPT---\nline {{x}}\nend\n"
    hashes = set()
    for index, data in enumerate((lf, lf.replace(b"\n", b"\r\n"), lf.replace(b"\n", b"\r"), b"\xef\xbb\xbf" + lf)):
        directory = tmp_path / str(index)
        directory.mkdir()
        hashes.add(hash_graph(_template_graph(directory, data), registry).nodes["w"])
    assert len(hashes) == 1


def _legacy_prompt_body(text: str, *, where: str = "") -> str:
    """The reader before llm-kit.prompt-file/v1 (1.24.1): split on LF only, no BOM rule."""
    lines = text.split("\n")
    at = [i for i, line in enumerate(lines) if line == loader.PROMPT_MARKER]
    if not at:
        return text
    if len(at) > 1:
        raise GraphError("two markers", where)
    return "\n".join(lines[at[0] + 1:])


FIXTURES = REPO / "tests" / "llm-kit" / "graph" / "fixtures"


def test_existing_template_fixtures_read_as_before():
    templates = sorted(FIXTURES.rglob("prompts/*.md"))
    assert len(templates) >= 8
    for path in templates:
        data = path.read_bytes()
        assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf"), path
        old = _legacy_prompt_body(path.read_text(encoding="utf-8"))
        assert read_prompt_file(path).body == old == prompt_body(data.decode("utf-8")), path


def test_the_node_hashes_of_the_existing_fixture_graphs_do_not_change(monkeypatch):
    import graph_support as gs  # noqa: PLC0415 -- tests/llm-kit/graph is on sys.path for the graph tests

    graphs = sorted(gs.BETA.rglob("*.graph.yaml"))
    assert graphs
    new = {p.name: hash_graph(load_graph(p), gs.registry()) for p in graphs}
    monkeypatch.setattr(loader, "prompt_body", _legacy_prompt_body)
    old = {p.name: hash_graph(load_graph(p), gs.registry()) for p in graphs}
    assert new == old


@pytest.fixture(autouse=True)
def _graph_support_on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "tests" / "llm-kit" / "graph"))


# ---------------------------------------------------------------------------
# the comparison case file: shared cases
# ---------------------------------------------------------------------------

COMPARE_CASES = json.loads((CASES / "compare-case" / "cases.json").read_text(encoding="utf-8"))


def test_the_compare_case_cases_name_the_shipped_schema():
    from llm_kit.runs.compare import COMPARE_CASE_V1  # noqa: PLC0415

    assert COMPARE_CASES["format"] == COMPARE_CASE_V1 == formats.COMPARE_CASE_V1
    assert (CASES / "compare-case" / COMPARE_CASES["schema"]).resolve() == \
        PACKAGE / "src" / "llm_kit" / "schema" / "formats" / formats.SCHEMA_FILES[formats.COMPARE_CASE_V1]


@pytest.mark.parametrize("name", sorted(by_name(COMPARE_CASES["cases"])))
def test_compare_case(name):
    from llm_kit.runs.compare import CompareError, read_cases  # noqa: PLC0415

    case = by_name(COMPARE_CASES["cases"])[name]
    expect = case["expect"]
    data = input_bytes(case["input"])
    if "cases" in expect:
        got = [{"id": c.id, "slots": dict(c.slots)} for c in read_cases(data)]
        assert got == expect["cases"]
        schema = formats.load_schema(formats.COMPARE_CASE_V1)
        assert all(validate(schema, line) == () for line in expect["cases"])
        return
    with pytest.raises(CompareError) as caught:
        read_cases(data)
    assert caught.value.code == expect["error"]
    if expect["line"] is None:
        assert "line " not in str(caught.value)
    else:
        assert str(caught.value).startswith(f"line {expect['line']}")


# ---------------------------------------------------------------------------
# the consent file: shared cases
# ---------------------------------------------------------------------------

CONSENT_CASES = json.loads((CASES / "consent" / "cases.json").read_text(encoding="utf-8"))


def test_the_consent_cases_name_the_shipped_schema():
    assert CONSENT_CASES["format"] == formats.CONSENT_V2 == "llm-kit.consent/v2"
    assert (CASES / "consent" / CONSENT_CASES["schema"]).resolve() == \
        PACKAGE / "src" / "llm_kit" / "schema" / "formats" / formats.SCHEMA_FILES[formats.CONSENT_V2]


@pytest.mark.parametrize("name", sorted(by_name(CONSENT_CASES["cases"])))
def test_consent_case(tmp_path, name):
    from llm_kit.registry.consent import ConsentConfigError, load_config  # noqa: PLC0415

    case = by_name(CONSENT_CASES["cases"])[name]
    expect = case["expect"]
    data = input_bytes(case["input"])
    target = tmp_path / "consent.json"
    target.write_bytes(data)
    if "config" in expect:
        config = load_config(target)
        got = {
            "globals": dict(config.globals_),
            "scopes": {scope: dict(classes) for scope, classes in config.scopes.items()},
            "connections": [approval.to_json() for approval in config.connections],
        }
        assert got == expect["config"]
        assert validate(formats.load_schema(formats.CONSENT_V2), json.loads(data)) == ()
        return
    assert expect == {"error": "invalid"}
    with pytest.raises(ConsentConfigError):
        load_config(target)


# ---------------------------------------------------------------------------
# the public surface and the example
# ---------------------------------------------------------------------------


def test_public_surface_lists_the_format_names():
    surface = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in surface["entries"] if e["status"] == "public"}
    for module_name in ("llm_kit.schema.formats", "llm_kit.registry.project_models", "llm_kit.registry.fallback_chains", "llm_kit.registry.reply_cache"):
        assert module_name in surface["covers"]
        module = __import__(module_name, fromlist=["__all__"])
        for name in module.__all__:
            assert f"{module_name}.{name}" in names, name
            assert hasattr(module, name), name
        listed = {n.rsplit(".", 1)[1] for n in names if n.rsplit(".", 1)[0] == module_name}
        assert listed == set(module.__all__)


# The names release 1.37.0 adds or changes in modules the surface does not list whole.
# Each is listed one by one, resolves in the code, and is documented: a callable or class
# by its docstring, a constant by a comment on the line before it or a docstring after it.
SURFACE_1_37_NAMES = (
    "llm_kit.registry.consent.ConnectionApproval",
    "llm_kit.registry.consent.ConsentConfig",
    "llm_kit.registry.consent.ConsentConfigError",
    "llm_kit.registry.consent.ENV_NAMES_IGNORE_CASE",
    "llm_kit.registry.consent.approve_connection",
    "llm_kit.registry.consent.connection_command",
    "llm_kit.registry.consent.connection_host",
    "llm_kit.registry.consent.env_name_key",
    "llm_kit.registry.consent.load_config",
    "llm_kit.registry.consent.revoke_connection",
    "llm_kit.registry.consent.save_config",
    "llm_kit.adapters.claude_command.batch_shim_spawn_argv",
    "llm_kit.adapters.claude_command.check_batch_shim_argv",
    "llm_kit.adapters.claude_command.is_batch_shim",
    "llm_kit.completion.types.ALL_TOOLS",
    "llm_kit.completion.types.BackendOptions",
    "llm_kit.adapters.backends.ClaudeCliBackend",
    "llm_kit.registry.models.CONNECTION_KEYS",
    "llm_kit.registry.models.COST_CLASS_KEYS",
    "llm_kit.registry.models.MACHINE_ONLY_ENDPOINT_KEYS",
    "llm_kit.registry.models.load_model_config",
    "llm_kit.frontdoor.server.create_app",
)


@pytest.mark.parametrize("qualified", SURFACE_1_37_NAMES)
def test_public_surface_lists_the_1_37_names_and_each_is_documented(qualified):
    import importlib  # noqa: PLC0415
    import inspect  # noqa: PLC0415
    import re  # noqa: PLC0415

    surface = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))
    entries = {e["name"]: e for e in surface["entries"]}
    assert entries[qualified]["status"] == "public"
    module_name, name = qualified.rsplit(".", 1)
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    if inspect.isfunction(value) or inspect.isclass(value):
        assert (value.__doc__ or "").strip(), qualified
        return
    lines = inspect.getsource(module).splitlines()
    at = next(i for i, line in enumerate(lines) if re.match(rf"{name}\s*(:[^=]*)?=", line))
    after = lines[at + 1].lstrip() if at + 1 < len(lines) else ""
    assert lines[at - 1].lstrip().startswith("#") or after.startswith(('"""', "r\"\"\"")), qualified


def test_public_surface_lists_no_private_module_or_name():
    """Private names (``claude_env._claude_child_env``) and modules (``adapters._cmd_shell``) stay unlisted."""
    surface = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))
    private = [entry["name"] for entry in surface["entries"]
               if any(part.startswith("_") and not part.endswith("__") for part in entry["name"].split("."))]
    assert private == []
    assert "_claude_child_env" in surface["purpose"] and "llm_kit.adapters._cmd_shell" in surface["purpose"]


def test_every_public_format_name_has_a_docstring():
    import llm_kit.graph.prompt as prompt_module  # noqa: PLC0415

    callables = [formats.load_schema, pm.validate_portable_document, pm.validate_portable_local_document,
                 pm.ProjectModelsError, prompt_module.PromptFile, parse_prompt_file, read_prompt_file,
                 prompt_parts, render_prompt, render_prompt_file]
    for item in callables:
        assert item.__doc__ and len(item.__doc__.strip()) > 40, item


def test_the_example_runs_from_the_source_tree(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(PACKAGE / "src"), str(REPO / "packages" / "katsoft-contracts" / "src")])}
    examples = sorted((PACKAGE / "examples").glob("*.py"))
    assert examples
    for example in examples:
        done = subprocess.run([sys.executable, str(example)], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=120, check=False)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip()


# ---------------------------------------------------------------------------
# graph catalog v1 and v2, graph labels: shared cases and the label interface
# ---------------------------------------------------------------------------

SCHEMA_DIR = PACKAGE / "src" / "llm_kit" / "schema" / "formats"
CATALOG_V1_CASES = json.loads((CASES / "graph-catalog" / "cases.json").read_text(encoding="utf-8"))
CATALOG_V2_CASES = json.loads((CASES / "graph-catalog" / "cases.v2.json").read_text(encoding="utf-8"))
LABELS_CASES = json.loads((CASES / "graph-labels" / "cases.json").read_text(encoding="utf-8"))


def _catalog_documents():
    return [(version, case) for version, cases in (("v1", CATALOG_V1_CASES), ("v2", CATALOG_V2_CASES))
            for case in cases["documents"]]


def test_the_graph_case_files_name_their_format_and_schema():
    for cases, folder, format_id in (
            (CATALOG_V1_CASES, "graph-catalog", formats.GRAPH_CATALOG_V1),
            (CATALOG_V2_CASES, "graph-catalog", formats.GRAPH_CATALOG_V2),
            (LABELS_CASES, "graph-labels", formats.GRAPH_LABELS_V1)):
        assert cases["format"] == format_id
        assert (CASES / folder / cases["schema"]).resolve() == SCHEMA_DIR / formats.SCHEMA_FILES[format_id]
        assert cases["description"].isascii()


@pytest.mark.parametrize("version,case", _catalog_documents(), ids=lambda v: v if isinstance(v, str) else v["name"])
def test_the_shared_catalog_cases_hold_for_their_own_schema(version, case):
    format_id = formats.GRAPH_CATALOG_V1 if version == "v1" else formats.GRAPH_CATALOG_V2
    errors = validate(formats.load_schema(format_id), case["document"])
    assert (errors == ()) == (case["expect"] == "valid"), errors


@pytest.mark.parametrize("case", LABELS_CASES["documents"], ids=lambda c: c["name"])
def test_the_shared_graph_labels_cases(case):
    errors = validate(formats.load_schema(formats.GRAPH_LABELS_V1), case["document"])
    assert (errors == ()) == (case["expect"] == "valid"), errors


def test_the_shared_cases_hold_both_outcomes_for_each_graph_format():
    for cases in (CATALOG_V1_CASES, CATALOG_V2_CASES, LABELS_CASES):
        assert {c["expect"] for c in cases["documents"]} == {"valid", "invalid"}
        names = [c["name"] for c in cases["documents"]]
        assert len(names) == len(set(names))


def test_an_older_reader_refuses_a_labelled_catalog():
    """Version 1 refuses every version 2 file, so no v1 reader drops labels in silence."""
    v1 = formats.load_schema(formats.GRAPH_CATALOG_V1)
    valid = [c for c in CATALOG_V2_CASES["documents"] if c["expect"] == "valid"]
    assert valid
    for case in valid:
        assert validate(v1, case["document"]) != (), case["name"]


def test_catalog_v2_is_v1_plus_a_required_labels():
    v1, v2 = formats.load_schema(formats.GRAPH_CATALOG_V1), formats.load_schema(formats.GRAPH_CATALOG_V2)
    assert v2["properties"]["format"] == {"const": formats.GRAPH_CATALOG_V2}
    assert {k: v for k, v in v2["properties"].items() if k not in ("format", "labels")} == \
        {k: v for k, v in v1["properties"].items() if k != "format"}
    assert v2["required"] == v1["required"] + ["labels"]
    assert v2["additionalProperties"] is False
    for name, definition in v1["$defs"].items():
        assert v2["$defs"][name] == definition, name


def test_the_label_limits_are_one_set_in_both_schemas():
    """The catalog and the cache entry state each label limit alike."""
    catalog = formats.load_schema(formats.GRAPH_CATALOG_V2)["$defs"]
    answer = formats.load_schema(formats.GRAPH_LABELS_V1)["$defs"]["answer"]["properties"]
    graph = catalog["graphLabels"]["properties"]["graph"]["properties"]
    assert graph["title"]["maxLength"] == answer["graph"]["properties"]["title"]["maxLength"] == 80
    assert graph["purpose"]["maxLength"] == answer["graph"]["properties"]["purpose"]["maxLength"] == 400
    node_label = catalog["graphLabels"]["properties"]["nodes"]["additionalProperties"]["properties"]
    node_answer = answer["nodes"]["additionalProperties"]["properties"]
    assert node_label["title"]["maxLength"] == node_answer["title"]["maxLength"] == 80
    assert node_label["description"]["maxLength"] == node_answer["description"]["maxLength"] == 300
    assert node_label["reads"]["additionalProperties"]["maxLength"] == \
        answer["edges"]["additionalProperties"]["maxLength"] == 60
    assert graph["inputs"]["additionalProperties"]["maxLength"] == \
        graph["outputs"]["additionalProperties"]["maxLength"] == \
        answer["inputs"]["additionalProperties"]["maxLength"] == \
        answer["outputs"]["additionalProperties"]["maxLength"] == 160


def test_the_label_interface_ids_match_the_formats():
    from llm_kit.graph.render import label_types as lt  # noqa: PLC0415

    assert lt.LABELLED_CATALOG_FORMAT == formats.GRAPH_CATALOG_V2
    assert lt.LABELS_PROMPT_ID == "llm-kit.graph-labels.prompt/1"
    assert sorted(lt.__all__) == sorted(["LABELS_PROMPT_ID", "LABELLED_CATALOG_FORMAT", "LabelCall", "LabelReply",
                                         "Labeller", "LabelError", "LabelRefused", "LabelFailed"])


def test_the_label_call_and_reply_are_frozen_values_with_the_planned_defaults():
    import dataclasses  # noqa: PLC0415

    from llm_kit.graph.render.label_types import LabelCall, LabelReply  # noqa: PLC0415

    call = LabelCall(file="g.graph.yaml", index=0, header=True, nodes=("a",), edges=(), inputs=("x",), outputs=("y",),
                     system="s", prompt="p", schema={"type": "object"}, key="0" * 64)
    assert call.followup is False and call.entry is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.key = "other"  # type: ignore[misc]
    reply = LabelReply(answer={"nodes": {}}, entry="haiku", model="m")
    assert (reply.entry, reply.model) == ("haiku", "m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        reply.entry = "other"  # type: ignore[misc]


def test_a_labeller_is_any_callable_with_a_declaration():
    from llm_kit.graph.render.label_types import LabelCall, LabelReply, Labeller  # noqa: PLC0415

    class Fixed:
        declaration = ("haiku",)

        def __call__(self, call: LabelCall) -> LabelReply:
            return LabelReply(answer={"nodes": {}, "edges": {}}, entry="haiku", model="m")

    labeller: Labeller = Fixed()
    call = LabelCall(file="g", index=0, header=False, nodes=(), edges=(), inputs=(), outputs=(),
                     system="", prompt="", schema={}, key="k")
    assert labeller(call).entry == "haiku" and labeller.declaration == ("haiku",)


def test_a_refusal_and_a_failure_are_label_errors():
    from llm_kit.graph.render.label_types import LabelError, LabelFailed, LabelRefused  # noqa: PLC0415

    assert issubclass(LabelRefused, LabelError) and issubclass(LabelFailed, LabelError)
    assert not issubclass(LabelRefused, LabelFailed) and not issubclass(LabelFailed, LabelRefused)
    assert issubclass(LabelError, Exception)
