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
from llm_kit.schema.json_schema import check_schema, validate
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "packages" / "katsoft-llm-kit"
CASES = REPO / "tests" / "formats"
REGISTRY_CASES = json.loads((CASES / "project-models" / "cases.json").read_text(encoding="utf-8"))
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
                   "anchors-and-aliases", "alias-cycle", "integer-above-exact-range", "yaml-1.1-float-forms"):
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
# the public surface and the example
# ---------------------------------------------------------------------------


def test_public_surface_lists_the_format_names():
    surface = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in surface["entries"] if e["status"] == "public"}
    for module_name in ("llm_kit.schema.formats", "llm_kit.registry.project_models"):
        assert module_name in surface["covers"]
        module = __import__(module_name, fromlist=["__all__"])
        for name in module.__all__:
            assert f"{module_name}.{name}" in names, name
            assert hasattr(module, name), name
        listed = {n.rsplit(".", 1)[1] for n in names if n.rsplit(".", 1)[0] == module_name}
        assert listed == set(module.__all__)


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
