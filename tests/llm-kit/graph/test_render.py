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
    assert render_command.run(["--project", str(FIXTURE), "-o", str(out)]) == 0
    assert capsys.readouterr().out.strip() == str(out)
    assert [e["name"] for e in _embedded(out.read_text(encoding="utf-8"))["entries"]] == ["demo.night"]


def test_render_command_takes_a_config_name_or_a_file_without_a_config(tmp_path, capsys, monkeypatch):
    out = tmp_path / "night.html"
    assert render_command.run(["--project", str(FIXTURE), "demo.night", "-o", str(out)]) == 0
    assert _embedded(out.read_text(encoding="utf-8"))["entries"][0]["name"] == "demo.night"
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)  # no project config here: the default output lands in this directory
    assert render_command.run([str(FIXTURE / POSE)]) == 0
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
