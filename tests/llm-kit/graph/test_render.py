"""Rendered graph pages: the catalog, its format, the page and ``llm-kit graph render``.

The fixture project ``fixtures/viewer/project`` has one outer graph
(``demo.night``) that runs a subgraph file (``story``), a map over a graph file
(``pose``), a human node and a commit with ``when``; ``story`` holds a repeat
with an inline body. Expected values below are read off those files.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from llm_kit.graph.commands import render as render_command
from llm_kit.graph.config import load_config
from llm_kit.graph.model import GraphError
from llm_kit.graph.render import CATALOG_FORMAT, build_catalog, project_catalog, render_page, write_page
from llm_kit.graph.render import _escape
from llm_kit.graph.render.label_types import LabelCall, LabelFailed, LabelRefused, LabelReply
from llm_kit.schema.formats import GRAPH_CATALOG_V1, GRAPH_CATALOG_V2, load_schema
from llm_kit.schema.json_schema import check_schema, validate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "viewer" / "project"
NIGHT = "graphs/night.graph.yaml"
STORY = "graphs/stages/story.graph.yaml"
POSE = "graphs/stages/pose.graph.yaml"


def _schema() -> dict:
    schema = load_schema(GRAPH_CATALOG_V1)
    check_schema(schema)
    return schema


def _valid(catalog: dict) -> None:
    assert validate(_schema(), json.loads(json.dumps(catalog))) == ()


def _node(nodes: list, node_id: str) -> dict:
    return next(n for n in nodes if n["id"] == node_id)


def _embedded(page: str) -> dict:
    match = re.search(r'<script type="application/json" id="catalog">(.*?)</script>', page, re.DOTALL)
    assert match, "the page has no embedded catalog"
    return json.loads(match.group(1))


@pytest.fixture(scope="module")
def catalog() -> dict:
    return project_catalog(load_config(FIXTURE))


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def test_project_catalog_holds_entries_every_reached_file_and_users(catalog):
    _valid(catalog)
    assert catalog["format"] == CATALOG_FORMAT == GRAPH_CATALOG_V1
    assert catalog["project"] == "project"
    assert catalog["entries"] == [{"name": "demo.night", "file": NIGHT}]
    assert sorted(catalog["files"]) == sorted([NIGHT, STORY, POSE])
    assert catalog["used_by"] == {
        POSE: [{"file": NIGHT, "node": "poses"}],
        STORY: [{"file": NIGHT, "node": "plot"}],
    }


def test_catalog_nodes_carry_edges_inputs_and_refs(catalog):
    night = catalog["files"][NIGHT]
    assert night["outer"] is True
    assert night["inputs"] == [{"name": "moment", "description": ""}]
    assert night["outputs"] == ["bundle"]
    assert night["output_sources"] == {"bundle": ["bundle"]}
    nodes = night["nodes"]
    assert [n["id"] for n in nodes] == ["brief", "plot", "poses", "bundle", "approve", "encode"]
    assert _node(nodes, "brief")["reads_inputs"] == ["moment"]
    assert _node(nodes, "brief")["detail"] == {"op": "llm.model"}
    assert _node(nodes, "plot")["ref"] == STORY and _node(nodes, "plot")["deps"] == ["brief"]
    assert _node(nodes, "poses")["ref"] == POSE and _node(nodes, "poses")["deps"] == ["plot"]
    assert _node(nodes, "bundle")["deps"] == ["plot", "poses"]
    assert _node(nodes, "encode")["when"] == "approve.out.ok"
    assert _node(nodes, "encode")["deps"] == ["approve", "bundle"]
    assert "ref" not in _node(nodes, "bundle") and "body" not in _node(nodes, "bundle")


def test_inline_body_is_nested_and_left_out_of_the_definition(catalog):
    story = catalog["files"][STORY]
    loop = _node(story["nodes"], "loop")
    body = loop["body"]
    assert [n["id"] for n in body["nodes"]] == ["write", "check", "repair", "evaluate"]
    assert body["outputs"] == ["plot", "score"]
    assert body["output_sources"] == {"plot": ["write"], "score": ["evaluate"]}
    assert _node(body["nodes"], "repair")["deps"] == ["check", "write"]
    assert _node(body["nodes"], "write")["reads_inputs"] == ["brief"]
    assert loop["definition"]["repeat"]["body"] == "(inline body)"
    assert loop["detail"]["max"] == "3"
    assert _node(story["nodes"], "keep")["deps"] == ["loop"]
    assert story["output_sources"] == {"plot": ["keep"], "score": ["keep"]}


def test_a_file_run_twice_is_kept_once_with_both_users(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE / "graphs", project / "graphs")
    shutil.copytree(FIXTURE / "prompts", project / "prompts")
    (project / "graphs" / "twice.graph.yaml").write_text(
        "id: demo.twice\n"
        "inputs:\n  brief: {schema: {type: object}, description: the brief}\n"
        "outputs:\n  plot: {schema: {type: object}}\n"
        "nodes:\n"
        "  first:\n    graph: ./stages/story.graph.yaml\n    in: {brief: inputs.brief}\n"
        "  second:\n    graph: ./stages/story.graph.yaml\n    in: {brief: first.out.plot}\n"
        "outputs_from:\n  plot: second.out.plot\n", encoding="utf-8")
    catalog = build_catalog({"twice": project / "graphs" / "twice.graph.yaml"}, root=project, project="demo")
    _valid(catalog)
    assert sorted(catalog["files"]) == ["graphs/stages/story.graph.yaml", "graphs/twice.graph.yaml"]
    assert catalog["used_by"]["graphs/stages/story.graph.yaml"] == [
        {"file": "graphs/twice.graph.yaml", "node": "first"},
        {"file": "graphs/twice.graph.yaml", "node": "second"},
    ]
    assert catalog["files"]["graphs/twice.graph.yaml"]["inputs"] == [{"name": "brief", "description": "the brief"}]
    assert catalog["project"] == "demo"


def test_build_catalog_refuses_nothing_and_project_catalog_an_unknown_name():
    with pytest.raises(ValueError):
        build_catalog({}, root=FIXTURE)
    with pytest.raises(GraphError, match="no graph named"):
        project_catalog(load_config(FIXTURE), ["demo.missing"])


CASES = json.loads((Path(__file__).resolve().parents[2] / "formats" / "graph-catalog" / "cases.json")
                   .read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES["documents"], ids=lambda c: c["name"])
def test_the_shared_catalog_cases(case):
    errors = validate(_schema(), case["document"])
    assert (errors == ()) == (case["expect"] == "valid"), errors


CASES_V2 = json.loads((Path(__file__).resolve().parents[2] / "formats" / "graph-catalog" / "cases.v2.json")
                      .read_text(encoding="utf-8"))
LABELLED = next(c["document"] for c in CASES_V2["documents"] if c["name"] == "labelled-graph")


def test_the_schema_refuses_a_catalog_with_a_wrong_format(catalog):
    broken = json.loads(json.dumps(catalog))
    broken["format"] = "llm-kit.graph-catalog/v2"
    assert validate(_schema(), broken) != ()


# ---------------------------------------------------------------------------
# unused graph files: files under the project root that no entry graph reaches
# ---------------------------------------------------------------------------

SPARE = "graphs/spare.graph.yaml"
BROKEN = "graphs/broken.graph.yaml"
WRAPPER = "graphs/wrapper.graph.yaml"
_SPARE_TEXT = ("id: demo.spare\n"
               "inputs:\n  moment: {schema: {type: object}}\n"
               "outputs:\n  out: {schema: {type: object}}\n"
               "nodes:\n  keep:\n    op: demo.keep\n    in: {moment: inputs.moment}\n"
               "outputs_from:\n  out: keep.out\n")


def _with_extra_graphs(tmp_path: Path) -> Path:
    """A copy of the fixture project plus graph files that nothing runs, and some that must be skipped."""
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project, ignore=shutil.ignore_patterns("state", "work"))
    (project / SPARE).write_text(_SPARE_TEXT, encoding="utf-8")
    (project / BROKEN).write_text("id: demo.broken\noutputs: {}\noutputs_from: {}\n"
                                  "nodes:\n  inner:\n    graph: ./nowhere.graph.yaml\n", encoding="utf-8")
    for skipped in (".git/hooks", "node_modules/pkg", ".venv/lib", "__pycache__", ".hidden/deep"):
        (project / skipped).mkdir(parents=True)
        (project / skipped / "skipped.graph.yaml").write_text(_SPARE_TEXT, encoding="utf-8")
    (project / "graphs" / "notes.yaml").write_text("not: a graph file\n", encoding="utf-8")
    return project


def test_a_graph_file_no_entry_reaches_is_listed_as_unused_and_a_broken_one_carries_its_error(tmp_path):
    project = _with_extra_graphs(tmp_path)
    catalog = project_catalog(load_config(project))
    _valid(catalog)
    assert catalog["entries"] == [{"name": "demo.night", "file": NIGHT}]
    assert sorted(catalog["files"]) == sorted([NIGHT, STORY, POSE, SPARE])
    assert [item["file"] for item in catalog["unused"]] == [BROKEN, SPARE]
    assert "error" not in catalog["unused"][1]
    error = catalog["unused"][0]["error"]
    assert "nowhere.graph.yaml" in error
    assert str(project) not in error and project.as_posix() not in error, "the error names a machine path"
    assert BROKEN not in catalog["files"]
    assert SPARE not in catalog["used_by"], "an unused file is run by nothing"


def test_a_project_whose_files_are_all_reached_has_no_unused_key(catalog):
    assert "unused" not in catalog


def test_naming_graphs_explicitly_lists_no_unused_files(tmp_path):
    project = _with_extra_graphs(tmp_path)
    catalog = project_catalog(load_config(project), ["demo.night"])
    assert "unused" not in catalog and SPARE not in catalog["files"]


def test_an_unused_file_that_runs_another_unreached_file_lists_both(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE / "graphs", project / "graphs")
    shutil.copytree(FIXTURE / "prompts", project / "prompts")
    (project / ".llm-kit").mkdir()
    (project / WRAPPER).write_text(
        "id: demo.wrapper\n"
        "inputs:\n  pose: {schema: {type: object}}\n"
        "outputs:\n  image: {schema: {type: object}}\n"
        "nodes:\n  inner:\n    graph: ./stages/pose.graph.yaml\n    in: {pose: inputs.pose}\n"
        "outputs_from:\n  image: inner.out.image\n", encoding="utf-8")
    (project / ".llm-kit" / "graphs.yaml").write_text(
        "version: 1\ngraphs:\n  demo.story: graphs/stages/story.graph.yaml\n", encoding="utf-8")
    catalog = project_catalog(load_config(project))
    _valid(catalog)
    assert [item["file"] for item in catalog["unused"]] == [NIGHT, POSE, WRAPPER]
    assert catalog["used_by"][POSE] == [{"file": NIGHT, "node": "poses"}, {"file": WRAPPER, "node": "inner"}]


def test_the_walk_stays_inside_the_project_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "far.graph.yaml").write_text(_SPARE_TEXT, encoding="utf-8")
    project = _with_extra_graphs(tmp_path)
    try:
        (project / "graphs" / "linked.graph.yaml").symlink_to(outside / "far.graph.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot make a symbolic link")
    catalog = project_catalog(load_config(project))
    assert "graphs/linked.graph.yaml" not in [item["file"] for item in catalog["unused"]]
    assert all(not key.endswith("far.graph.yaml") for key in catalog["files"])


def _walk_result(project: Path) -> list:
    from llm_kit.graph.render.catalog import _graph_files_under  # noqa: PLC0415

    return [p.relative_to(project).as_posix() for p in _graph_files_under(project)]


def _link_directory(kind: str, target: Path, link: Path) -> None:
    """Make ``link`` a directory link of ``kind`` ("symlink" or "junction") to ``target``, or skip the test."""
    if kind == "junction":
        try:
            import _winapi  # noqa: PLC0415
            _winapi.CreateJunction(str(target), str(link))
        except (ImportError, AttributeError, OSError):
            pytest.skip("this machine cannot make a directory junction")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot make a symbolic link")


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_the_walk_enters_no_directory_link_to_an_ancestor_a_sibling_or_outside(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "far.graph.yaml").write_text(_SPARE_TEXT, encoding="utf-8")
    project = _with_extra_graphs(tmp_path)
    expected = _walk_result(project)
    assert SPARE in expected and "far.graph.yaml" not in " ".join(expected)
    _link_directory(kind, project, project / "loop")                  # to an ancestor (the root itself)
    _link_directory(kind, project / "graphs", project / "alias")      # to a sibling inside the root
    _link_directory(kind, outside, project / "graphs" / "away")       # to a folder outside the root
    assert _walk_result(project) == expected, "a directory link was entered"
    catalog = project_catalog(load_config(project))
    assert [item["file"] for item in catalog["unused"]] == [BROKEN, SPARE]


def test_render_command_without_graphs_draws_the_unused_files(tmp_path):
    project = _with_extra_graphs(tmp_path)
    out = tmp_path / "all.html"
    assert render_command.run(["--project", str(project), "--no-labels", "-o", str(out)]) == 0
    unused = _embedded(out.read_text(encoding="utf-8"))["unused"]
    assert [item["file"] for item in unused] == [BROKEN, SPARE]


def test_render_command_with_a_named_graph_lists_no_unused_files(tmp_path):
    project = _with_extra_graphs(tmp_path)
    out = tmp_path / "one.html"
    assert render_command.run(["--project", str(project), "--no-labels", "demo.night", "-o", str(out)]) == 0
    assert "unused" not in _embedded(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def test_render_page_embeds_the_catalog_and_loads_nothing_from_the_network(catalog):
    page = render_page(catalog)
    assert _embedded(page) == catalog
    assert "<title>project graphs</title>" in page
    assert "new ELK()" in page and "elk.layered" in page
    assert not re.search(r"<(script|link|img)[^>]+(src|href)=", page)


def test_render_page_text_from_a_graph_cannot_close_its_script_element(catalog):
    hostile = json.loads(json.dumps(catalog))
    hostile["files"][NIGHT]["description"] = "</script><script>alert(1)</script><!-- & \u2028"
    page = render_page(hostile, title="<b>x</b>")
    assert "<script>alert(1)" not in page
    assert page.count("</script>") == 3  # the catalog, the layout library and the browser script
    assert _embedded(page)["files"][NIGHT]["description"] == hostile["files"][NIGHT]["description"]
    assert "<title>&lt;b&gt;x&lt;/b&gt;</title>" in page


def test_render_page_refuses_another_format_and_an_empty_catalog(catalog):
    with pytest.raises(ValueError, match="not a llm-kit.graph-catalog/v1 or llm-kit.graph-catalog/v2"):
        render_page({**catalog, "format": "something-else"})
    with pytest.raises(ValueError, match="no entries"):
        render_page({**catalog, "entries": []})


@pytest.mark.parametrize("case", CASES["documents"] + CASES_V2["documents"],
                         ids=lambda c: c["name"] if isinstance(c, dict) else str(c))
def test_render_page_is_the_reader_of_every_shared_catalog_case(case):
    """The page is the only reader of a catalog; it takes every valid case and refuses every invalid one."""
    if case["expect"] == "valid":
        assert _embedded(render_page(case["document"])) == case["document"]
    else:
        with pytest.raises(ValueError):
            render_page(case["document"])


def test_render_page_accepts_a_labelled_catalog_and_names_the_broken_place(catalog):
    assert GRAPH_CATALOG_V2 == LABELLED["format"]
    assert _embedded(render_page(LABELLED)) == LABELLED
    broken = json.loads(json.dumps(LABELLED))
    broken["labels"][NIGHT]["graph"]["title"] = ""
    with pytest.raises(ValueError, match=r"llm-kit.graph-catalog/v2.*/labels/.*minLength"):
        render_page(broken)
    stale = json.loads(json.dumps(catalog))
    stale["labels"] = {}
    with pytest.raises(ValueError, match=r"llm-kit.graph-catalog/v1.*/labels"):
        render_page(stale)


def test_render_page_label_text_cannot_close_its_script_element():
    hostile = json.loads(json.dumps(LABELLED))
    text = "</script><script>alert(1)</script><!-- & \u2028 \u2029"
    labels = hostile["labels"][NIGHT]
    labels["graph"]["title"] = text
    labels["graph"]["purpose"] = text
    node = next(iter(labels["nodes"].values()))
    node["title"], node["description"] = text, text
    node["reads"] = {"x": text}
    page = render_page(hostile)
    assert "<script>alert(1)" not in page
    assert page.count("</script>") == 3  # the catalog, the layout library and the browser script
    assert _embedded(page) == hostile


def test_the_markup_escapes_leave_no_angle_bracket_or_ampersand_and_round_trip():
    text = json.dumps({"a": "</script><!-- & > <", "b": "\u2028\u2029"}, ensure_ascii=False)
    markup = _escape.escape_markup(text)
    assert not any(char in markup for char in "<>&")
    assert json.loads(markup) == json.loads(text)
    script = _escape.escape_script_json(text)
    assert not any(char in script for char in "<>&\u2028\u2029")
    assert json.loads(script) == json.loads(text)
    assert "\u2028" in markup and "\u2028" not in script


def test_write_page_creates_parents_and_writes_the_page(catalog, tmp_path):
    target = write_page(catalog, tmp_path / "out" / "graphs.html")
    assert target.read_text(encoding="utf-8") == render_page(catalog)


# ---------------------------------------------------------------------------
# llm-kit graph render
# ---------------------------------------------------------------------------


def test_render_command_without_graphs_writes_every_config_graph(tmp_path, capsys):
    out = tmp_path / "all.html"
    assert render_command.run(["--project", str(FIXTURE), "--no-labels", "-o", str(out)]) == 0
    assert capsys.readouterr().out.strip() == str(out)
    assert [e["name"] for e in _embedded(out.read_text(encoding="utf-8"))["entries"]] == ["demo.night"]


def test_render_command_takes_a_config_name_or_a_file_without_a_config(tmp_path, capsys, monkeypatch):
    out = tmp_path / "night.html"
    assert render_command.run(["--project", str(FIXTURE), "--no-labels", "demo.night", "-o", str(out)]) == 0
    assert _embedded(out.read_text(encoding="utf-8"))["entries"][0]["name"] == "demo.night"
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)  # no project config here: the default output lands in this directory
    assert render_command.run(["--no-labels", str(FIXTURE / POSE)]) == 0
    written = Path(capsys.readouterr().out.strip())
    assert written.name == "demo.pose.html"
    catalog = _embedded(written.read_text(encoding="utf-8"))
    assert catalog["entries"][0]["name"] == "demo.pose"
    assert list(catalog["files"]) == [(FIXTURE / POSE).resolve().as_posix()]


def test_render_command_usage_errors_exit_2(tmp_path, capsys, monkeypatch):
    assert render_command.run(["--project", str(FIXTURE), "demo.missing", "-o", str(tmp_path / "x.html")]) == 2
    assert "neither a config graph nor a file" in capsys.readouterr().err
    monkeypatch.chdir(tmp_path)
    assert render_command.run([]) == 2
    assert "does not exist" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# llm-kit graph render: labels (a fake labeller stands in for the model)
# ---------------------------------------------------------------------------


class FakeLabeller:
    """Answers every call from the keys it was asked for; counts the calls."""

    declaration = ("fake",)

    def __init__(self, failure: Exception | None = None):
        self.calls: list[LabelCall] = []
        self.failure = failure

    def __call__(self, call: LabelCall) -> LabelReply:
        self.calls.append(call)
        if self.failure is not None:
            raise self.failure
        answer = {"nodes": {p: {"title": f"T {p}", "description": f"D {p}"} for p in call.nodes},
                  "edges": {e: f"E {e}" for e in call.edges}}
        if call.header:
            answer["graph"] = {"title": "A graph", "purpose": "What the graph is for"}
            answer["inputs"] = {n: f"I {n}" for n in call.inputs}
            answer["outputs"] = {n: f"O {n}" for n in call.outputs}
        return LabelReply(answer, "fake", "fake-model")


@pytest.fixture
def cache_home(tmp_path, monkeypatch) -> Path:
    """Point the default label cache at a test directory."""
    home = tmp_path / "cache-home"
    monkeypatch.setenv("XDG_CACHE_HOME", str(home))
    return home


def _render(tmp_path, *args, labeller=None) -> int:
    return render_command.run(["--project", str(FIXTURE), "-o", str(tmp_path / "out.html"), *args], labeller=labeller)


def test_render_command_labels_by_default_and_reports_progress_on_stderr(tmp_path, capsys, cache_home):
    fake = FakeLabeller()
    assert _render(tmp_path, labeller=fake) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(tmp_path / "out.html")
    catalog = _embedded((tmp_path / "out.html").read_text(encoding="utf-8"))
    assert catalog["format"] == GRAPH_CATALOG_V2
    assert sorted(catalog["labels"]) == sorted([NIGHT, STORY, POSE])
    assert catalog["labels"][NIGHT]["nodes"]["encode"]["title"] == "T encode"
    assert len(fake.calls) == 3
    lines = captured.err.strip().splitlines()
    assert lines and all(line.startswith("llm-kit graph render: labels:") for line in lines)
    assert "3 graph files, 0 cached, 3 to make with fake" in lines[0]
    assert (cache_home / "llm-kit" / "graph-labels").is_dir()


def test_render_command_no_labels_writes_version_1_and_touches_no_cache(tmp_path, capsys, cache_home):
    fake = FakeLabeller()
    assert _render(tmp_path, "--no-labels", labeller=fake) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    catalog = _embedded((tmp_path / "out.html").read_text(encoding="utf-8"))
    assert catalog["format"] == CATALOG_FORMAT and "labels" not in catalog
    assert fake.calls == []
    assert not cache_home.exists()


def test_render_command_refusal_exits_3_names_the_remedy_and_writes_no_page(tmp_path, capsys, cache_home):
    refusal = LabelRefused("consent is off; run `llm-kit consent set --scope graph-labels --subscription on` "
                           "or render with --no-labels")
    assert _render(tmp_path, labeller=FakeLabeller(refusal)) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "llm-kit consent set --scope graph-labels" in captured.err and "--no-labels" in captured.err
    assert not (tmp_path / "out.html").exists()


def test_render_command_failure_exits_3_leaves_an_existing_page_and_names_no_labels(tmp_path, capsys, cache_home):
    out = tmp_path / "out.html"
    out.write_text("old page", encoding="utf-8")
    assert _render(tmp_path, labeller=FakeLabeller(LabelFailed("the answer is not JSON"))) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and "the answer is not JSON" in captured.err and "--no-labels" in captured.err
    assert out.read_text(encoding="utf-8") == "old page"


def test_render_command_cache_error_exits_3(tmp_path, capsys, cache_home):
    cache = tmp_path / "cache"
    assert _render(tmp_path, "--label-cache", str(cache), labeller=FakeLabeller()) == 0
    victim = next(cache.glob("*.json"))
    victim.write_text("{not json", encoding="utf-8")
    capsys.readouterr()
    assert _render(tmp_path, "--label-cache", str(cache), labeller=FakeLabeller()) == 3
    assert "--relabel" in capsys.readouterr().err


def test_render_command_warm_cache_makes_no_call_and_relabel_bypasses_it(tmp_path, capsys, cache_home):
    cache = ["--label-cache", str(tmp_path / "cache")]
    first = FakeLabeller()
    assert _render(tmp_path, *cache, labeller=first) == 0
    assert len(first.calls) == 3
    page = (tmp_path / "out.html").read_text(encoding="utf-8")
    capsys.readouterr()
    # A warm render needs no labeller at all: a refusing one is never called.
    refusing = FakeLabeller(LabelRefused("must not be called"))
    assert _render(tmp_path, *cache, labeller=refusing) == 0
    assert refusing.calls == []
    assert (tmp_path / "out.html").read_text(encoding="utf-8") == page
    assert "3 cached, none to make" in capsys.readouterr().err
    third = FakeLabeller()
    assert _render(tmp_path, *cache, "--relabel", labeller=third) == 0
    assert len(third.calls) == 3
    assert not (cache_home).exists()  # --label-cache replaced the default location


def test_render_command_label_jobs_and_batch_reach_the_labelling_core(tmp_path, capsys, cache_home):
    fake = FakeLabeller()
    assert _render(tmp_path, "--label-batch", "2", "--label-jobs", "1", labeller=fake) == 0
    assert len(fake.calls) > 3
    assert max(len(call.nodes) for call in fake.calls) <= 2
    assert "(1 at a time)" in capsys.readouterr().err


def test_render_command_builds_the_model_labeller_from_the_label_model_flag(tmp_path, monkeypatch, cache_home):
    from llm_kit.graph.render import label_model  # noqa: PLC0415

    seen = {}

    def build(declaration, *, project_root, **kwargs):
        seen["declaration"], seen["root"] = tuple(declaration), Path(project_root)
        return FakeLabeller()

    monkeypatch.setattr(label_model, "model_labeller", build)
    assert _render(tmp_path, "--label-model", "haiku, sonnet") == 0
    assert seen == {"declaration": ("haiku", "sonnet"), "root": FIXTURE.resolve()}
    seen.clear()
    assert _render(tmp_path) == 0
    assert seen["declaration"] == label_model.DEFAULT_LABEL_MODELS


@pytest.mark.parametrize("flags", [
    ["--no-labels", "--relabel"], ["--no-labels", "--label-model", "haiku"], ["--no-labels", "--label-jobs", "2"],
    ["--no-labels", "--label-batch", "3"], ["--no-labels", "--label-cache", "x"],
    ["--label-jobs", "0"], ["--label-jobs", "many"], ["--label-batch", "0"], ["--label-batch", "-1"],
    ["--label-model", ""], ["--label-model", "haiku,haiku"],
])
def test_render_command_label_flag_errors_exit_2_and_call_nothing(tmp_path, capsys, cache_home, flags):
    fake = FakeLabeller()
    assert _render(tmp_path, *flags, labeller=None if "--label-model" in flags else fake) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("llm-kit graph render:")
    assert fake.calls == [] and not (tmp_path / "out.html").exists() and not cache_home.exists()


def test_labelled_render_writes_only_the_page_and_the_cache(tmp_path, cache_home):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    before = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))
    out = tmp_path / "elsewhere" / "graphs.html"
    assert render_command.run(["--project", str(project), "-o", str(out)], labeller=FakeLabeller()) == 0
    assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*")) == before
    assert out.is_file()
    written = [p.relative_to(tmp_path).as_posix() for p in cache_home.rglob("*") if p.is_file()]
    assert written and all(w.startswith("cache-home/llm-kit/graph-labels/") and w.endswith(".json") for w in written)


def test_render_command_labels_a_graph_file_without_a_project_config(tmp_path, monkeypatch, capsys, cache_home):
    monkeypatch.chdir(tmp_path)
    fake = FakeLabeller()
    assert render_command.run([str(FIXTURE / POSE)], labeller=fake) == 0
    catalog = _embedded(Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8"))
    assert catalog["format"] == GRAPH_CATALOG_V2 and len(fake.calls) == 1
