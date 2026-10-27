"""Static checks of ``validate_graph`` and the ``llm-kit graph validate`` command."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import graph_support as gs
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import Op, Port
from llm_kit.graph.validate import validate_graph


@pytest.fixture(autouse=True)
def _expr(monkeypatch):
    gs.use_expr(monkeypatch)


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    return gs.project(tmp_path, monkeypatch)


def _ops(*extra: Op):
    def fn(ctx, **kw):
        return kw

    base = [
        Op("t.num", "1", fn, inputs={"x": Port({"type": "integer"})}, outputs={"type": "integer"}),
        Op("t.any", "1", fn, inputs={"x": Port({}), "y": Port({}, default=None)}),
        Op("t.obj", "1", fn, outputs={"type": "object", "properties": {"n": {"type": "integer"},
                                                                      "s": {"type": "string"}}}),
        Op("t.commit", "1", fn, commit=True, inputs={"bundle": Port({"type": "object"})},
           outputs={"type": "object"}),
        Op("t.badcommit", "1", fn, commit=True, inputs={"x": Port({})}),
    ]
    return gs.registry(*base, *extra)


def _check(tmp_path: Path, root: Path, text: str, *files: tuple[str, str]) -> list[str]:
    for name, body in files:
        gs.write(tmp_path / name, body)
    path = gs.write(tmp_path / "g.graph.yaml", text)
    problems = validate_graph(load_graph(path), registry=_ops(), project_root=root)
    prefix = f"{path}: "
    return [str(p)[len(prefix):] if str(p).startswith(prefix) else str(p) for p in problems]


def test_the_beta_fixtures_validate_clean(root):
    graph = load_graph(gs.BETA / "beta.graph.yaml")
    assert validate_graph(graph, registry=gs.registry(), project_root=root) == []


def test_the_story_structure_stage_validates_alone(root):
    graph = load_graph(gs.BETA / "stages" / "story-structure.graph.yaml")
    assert validate_graph(graph, registry=gs.registry(), project_root=root) == []


GRAPH = """\
id: t.g
inputs:
  x: {type: integer}
  maybe: {schema: {type: integer}, default: 1}
outputs:
  y: {type: integer}
nodes:
{nodes}
outputs_from:
  y: {out}
"""


def _g(nodes: str, out: str = "a.out") -> str:
    return GRAPH.replace("{nodes}", nodes.rstrip("\n")).replace("{out}", out)


A = "  a:\n    op: t.num\n    in: {x: inputs.x}\n"


@pytest.mark.parametrize("nodes, out, expected", [
    (A, "a.out", []),
    (A, "b.out", ["outputs_from.y: 'b.out': unknown name 'b'"]),
    (A, "a.nope", ["outputs_from.y: 'a.nope': node 'a' has no attribute 'nope'; it has ['out']"]),
    (A, "a", ["outputs_from.y: 'a': name an attribute of node 'a', such as a.out"]),
    (A.replace("inputs.x", "inputs.z"), "a.out",
     ["nodes.a.in.x: 'inputs.z': no input 'z'; inputs: ['maybe', 'x']"]),
    (A.replace("inputs.x", "carry.c"), "a.out", ["nodes.a.in.x: 'carry' is not available here"]),
    (A.replace("inputs.x", "item.id"), "a.out", ["nodes.a.in.x: 'item' is not available here"]),
    (A.replace("inputs.x", "round"), "a.out", ["nodes.a.in.x: 'round' is not available here"]),
    (A.replace("inputs.x", "a.out"), "a.out", ["nodes.a.in.x: 'a.out': node 'a' reads its own result"]),
    (A.replace("inputs.x", "inputs.x +"), "a.out", ["nodes.a.in.x: expression 'inputs.x +': "]),
    (A.replace("inputs.x", "\"'text'\""), "a.out", []),                  # not a bare reference: unchecked
    (A.replace("inputs.x", "'text'"), "a.out", ["nodes.a.in.x: 'text': unknown name 'text'"]),  # YAML ate the quotes
    (A.replace("inputs.x", "3"), "a.out", []),
    (A.replace("inputs.x", "'3'").replace("'3'", "true"), "a.out",
     ["nodes.a.in.x: literal True does not match the port schema: [('', 'type')]"]),
    (A + "  b:\n    op: t.obj\n  c:\n    op: t.num\n    in: {x: b.out.s}\n", "c.out",
     ["nodes.c.in.x: the schema of 'b.out.s' does not match the port schema"]),
    (A + "  b:\n    op: t.obj\n  c:\n    op: t.num\n    in: {x: b.out.n}\n", "c.out", []),
    (A + "  b:\n    op: t.obj\n  c:\n    op: t.num\n    in: {x: b.out.missing}\n", "c.out", []),
    (A, "inputs.maybe", []),
    (A.replace("in: {x: inputs.x}", "in: {}"), "a.out", ["nodes.a.in: op t.num needs inputs ['x']"]),
    (A.replace("in: {x: inputs.x}", "in: {x: inputs.x, z: 1}"), "a.out",
     ["nodes.a.in.z: op t.num has no input 'z'; inputs: ['x']"]),
    ("  a:\n    op: t.any\n    in: {x: inputs.x}\n", "inputs.x", []),     # y has a default
    ("  a:\n    op: t.none\n    in: {x: inputs.q}\n", "inputs.x",
     ["nodes.a.op: unknown op 't.none'", "nodes.a.in.x: 'inputs.q': no input 'q'; inputs: ['maybe', 'x']"]),
    ("  a:\n    op: t.commit\n    in: {bundle: inputs.x}\n", "inputs.x",
     ["nodes.a.op: t.commit is a commit adapter; only a commit node may call it",
      "nodes.a.in.bundle: the schema of 'inputs.x' does not match the port schema"]),
    (A + "  b:\n    op: t.num\n    in: {x: c.out}\n  c:\n    op: t.num\n    in: {x: b.out}\n", "a.out",
     ["nodes: cycle: b -> c -> b"]),
    (A.replace("    op: t.num\n", "    op: t.num\n    when: inputs.x > 1 and a.out\n"), "a.out",
     ["nodes.a.when: 'a.out': node 'a' reads its own result"]),
])
def test_references_ops_and_schemas(tmp_path, root, nodes, out, expected):
    found = _check(tmp_path, root, _g(nodes, out))
    if expected and expected[0].endswith("': "):
        assert len(found) == 1 and found[0].startswith(expected[0]), found
    else:
        assert found == expected


REPEAT = """\
id: t.r
inputs:
  x: {{type: integer}}
nodes:
  first:
    op: t.num
    in: {{x: inputs.x}}
  loop:
    repeat:
      max: {max}
      carry:
        best: {{init: first.out, next: {next}}}
      until: {until}
      body:
        nodes:
          step:
            op: t.num
            in: {{x: {step_in}}}
        out: {{score: step.out}}
  after:
    op: t.any
    in: {{x: loop.rounds}}
"""


def _r(max="3", next="round.score", until="round.score >= 10", step_in="carry.best ?? index"):
    return REPEAT.format(max=max, next=next, until=until, step_in=f'"{step_in}"')


@pytest.mark.parametrize("text, expected", [
    (_r(), []),
    (_r(max="0"), ["nodes.loop.repeat.max: max must be an integer of at least 1, not 0"]),
    (_r(max="1.5"), ["nodes.loop.repeat.max: max must be an integer of at least 1, not 1.5"]),
    (_r(max="true"), ["nodes.loop.repeat.max: max must be an integer of at least 1, not True"]),
    (_r(max="inputs.x"), []),
    (_r(next="round.nope"), ["nodes.loop.repeat.carry.best.next: 'round.nope': round has no 'nope'; it has ['score']"]),
    (_r(until="carry.worst > 1"),
     ["nodes.loop.repeat.until: 'carry.worst': carry has no 'worst'; it has ['best']"]),
    (_r(step_in="round.score"), ["nodes.loop.repeat.body.nodes.step.in.x: 'round' is not available here"]),
    (_r(step_in="index.n"), ["nodes.loop.repeat.body.nodes.step.in.x: 'index.n': index is a number"]),
    (_r(step_in="first.out"), []),                                     # an inline body reads outer siblings
    (_r(step_in="after.out"), ["nodes: cycle: loop -> after -> loop"]),
    (_r(step_in="loop.rounds"),
     ["nodes.loop.repeat.body.nodes.step.in.x: 'loop.rounds': node 'loop' reads its own result"]),
    (_r(step_in="step.out"),
     ["nodes.loop.repeat.body.nodes.step.in.x: 'step.out': node 'step' reads its own result"]),
    (_r().replace("in: {x: loop.rounds}", "in: {x: loop.out}"),
     ["nodes.after.in.x: 'loop.out': node 'loop' has no attribute 'out'; it has ['carry', 'rounds']"]),
    (_r().replace("init: first.out", "init: carry.best"),
     ["nodes.loop.repeat.carry.best.init: 'carry' is not available here"]),
])
def test_repeat_scopes(tmp_path, root, text, expected):
    assert _check(tmp_path, root, text) == expected


SUB = """\
id: t.sub
inputs:
  n: {type: integer}
  opt: {schema: {type: string}, default: ""}
outputs:
  v: {type: integer}
nodes:
  a:
    op: t.num
    in: {x: inputs.n}
outputs_from:
  v: a.out
"""

MAP = """\
id: t.m
inputs:
  xs: {{type: array, items: {{type: integer}}}}
nodes:
  each:
    map:
      over: inputs.xs
      key: {key}
      graph: sub.graph.yaml
      in: {bind}
  inline:
    map:
      over: inputs.xs
      key: index
      body:
        nodes:
          one:
            op: t.num
            in: {{x: item}}
        out: {{v: one.out}}
  use:
    op: t.any
    in: {{x: each.out, y: inline.out}}
"""


@pytest.mark.parametrize("key, bind, expected", [
    ("item", "{n: item}", []),
    ("item", "{n: index, opt: \"'x'\"}", []),
    ("item.x", "{n: item}", []),                                     # a key expression is not type-checked
    ("round", "{n: item}", ["nodes.each.map.key: 'round' is not available here"]),
    ("item", "{}", ["nodes.each.map.in: graph t.sub needs inputs ['n']"]),
    ("item", "{n: item, m: 1}", ["nodes.each.map.in.m: graph t.sub has no input 'm'; inputs: ['n', 'opt']"]),
    ("item", "{n: inputs.xs}", ["nodes.each.map.in.n: the schema of 'inputs.xs' does not match the port schema"]),
])
def test_map_scopes_and_subgraph_inputs(tmp_path, root, key, bind, expected):
    text = MAP.format(key=key, bind=bind)
    assert _check(tmp_path, root, text, ("sub.graph.yaml", SUB)) == expected


def test_problems_inside_a_subgraph_name_their_own_file(tmp_path, root):
    sub = gs.write(tmp_path / "sub.graph.yaml", SUB.replace("inputs.n", "inputs.q"))
    found = _check(tmp_path, root, "id: t.p\nnodes:\n  s:\n    graph: sub.graph.yaml\n    in: {n: 1}\n")
    assert found == [f"{sub}: nodes.a.in.x: 'inputs.q': no input 'q'; inputs: ['n', 'opt']"]


OUTER = """\
id: t.o
outer: {outer}
nodes:
  b:
    op: t.obj
  c:
    commit:
      contract: {{type: object, properties: {{n: {{type: integer}}, s: {{type: string}}}}}}
      bundle: {bundle}
      adapter: {adapter}
"""


@pytest.mark.parametrize("outer, bundle, adapter, expected", [
    ("true", "b.out", "t.commit", []),
    ("true", "{n: b.out.n}", "t.commit", []),
    ("false", "b.out", "t.commit",
     ["nodes.c: a commit node may only be at the top level of an outer graph (outer: true)"]),
    ("true", "b.out", "t.obj", ["nodes.c.commit.adapter: t.obj is not a commit adapter (commit=True)"]),
    ("true", "b.out", "t.none", ["nodes.c.commit.adapter: unknown op 't.none'"]),
    ("true", "b.out", "t.badcommit",
     ["nodes.c.commit.adapter: commit adapter t.badcommit must take exactly one input, bundle; it takes ['x']"]),
    ("true", "b.out.n", "t.commit",
     ["nodes.c.commit.bundle: the schema of 'b.out.n' does not match the port schema"]),
])
def test_commit_nodes(tmp_path, root, outer, bundle, adapter, expected):
    assert _check(tmp_path, root, OUTER.format(outer=outer, bundle=bundle, adapter=adapter)) == expected


def test_a_commit_node_inside_an_inline_body_is_refused(tmp_path, root):
    text = """\
id: t.o
outer: true
nodes:
  loop:
    repeat:
      max: 1
      body:
        nodes:
          c:
            commit: {contract: {type: object}, bundle: {v: index}, adapter: t.commit}
        out: {v: c.out}
"""
    assert _check(tmp_path, root, text) == [
        "nodes.loop.repeat.body.nodes.c: a commit node may only be at the top level of an outer graph (outer: true)"]


def test_human_payload_references_are_checked(tmp_path, root):
    text = "id: t.h\nnodes:\n  h:\n    human:\n      kind: k\n      schema: {type: object}\n      payload: {u: z.out}\n"
    assert _check(tmp_path, root, text) == ["nodes.h.human.payload.u: 'z.out': unknown name 'z'"]


MODEL = """\
id: t.llm
inputs:
  x: {{type: string}}
nodes:
  w:
    op: llm.model
    model: {model}
    prompt: p.md
    slots: {slots}
    output_schema: {{type: object, properties: {{n: {{type: integer}}}}}}
  use:
    op: t.num
    in: {{x: w.out.n}}
  raw:
    op: t.any
    in: {{x: w.sent, y: w.text}}
"""


@pytest.mark.parametrize("model, slots, expected", [
    (gs.SELECTION, "{a: inputs.x, b: inputs.x}", []),
    (gs.SELECTION, "{a: inputs.x}", ["nodes.w.prompt: template 'p.md': slots with no binding: ['b']"]),
    (gs.SELECTION, "{a: inputs.x, b: inputs.x, c: 1}",
     ["nodes.w.prompt: template 'p.md': bindings with no slot: ['c']"]),
    (gs.SELECTION, "{a: inputs.x, b: w.out}", ["nodes.w.slots.b: 'w.out': node 'w' reads its own result"]),
    ("no.such.key", "{a: inputs.x, b: inputs.x}",
     ["nodes.w.model: selection 'no.such.key' is not in the project model file"]),
])
def test_model_nodes(tmp_path, root, model, slots, expected):
    text = MODEL.format(model=model, slots=slots)
    assert _check(tmp_path, root, text, ("p.md", "header {{z}}\n---PROMPT---\n{{a}} and {{b}}\n")) == expected


def test_a_template_without_slots_param_binds_nothing(tmp_path, root):
    text = MODEL.format(model=gs.SELECTION, slots="{}").replace("    slots: {}\n", "")
    found = _check(tmp_path, root, text, ("p.md", "{{a}}\n"))
    assert found == ["nodes.w.prompt: template 'p.md': slots with no binding: ['a']"]


def test_an_unresolvable_parameter_is_one_problem(tmp_path, root):
    text = MODEL.format(model=gs.SELECTION, slots="{a: inputs.x}").replace("prompt: p.md", "prompt: none.md")
    found = _check(tmp_path, root, text)
    assert len(found) == 1 and found[0].startswith("nodes.w: file 'none.md' does not exist")


def test_a_missing_model_file_is_reported_per_selection(tmp_path, root):
    (root / ".llm-kit" / "models.yaml").unlink()
    text = MODEL.format(model=gs.SELECTION, slots="{a: inputs.x, b: inputs.x}")
    found = _check(tmp_path, root, text, ("p.md", "{{a}}{{b}}"))
    assert len(found) == 1 and "cannot read the project model file" in found[0], found


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

CONFIG = """\
version: 1
graphs:
  beta: ../beta/beta.graph.yaml
"""


@pytest.fixture()
def cli_project(tmp_path, monkeypatch):
    beta = tmp_path / "beta"
    shutil.copytree(gs.BETA, beta)
    project = gs.project(tmp_path, monkeypatch, graphs_yaml=CONFIG)
    from llm_kit.graph.commands import validate as command

    monkeypatch.setattr(command, "load_registry", lambda config: gs.registry())
    return project, beta


def _cli(argv, capsys):
    from llm_kit.cli import main

    capsys.readouterr()
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_validates_every_configured_graph(cli_project, capsys):
    project, _ = cli_project
    assert _cli(["graph", "validate", "--project", str(project)], capsys) == (0, "ok: 1 graph(s)\n", "")


def test_cli_prints_one_line_per_problem_and_exits_1(cli_project, capsys, tmp_path):
    project, beta = cli_project
    path = beta / "stages" / "pose-image.graph.yaml"
    path.write_text(path.read_text().replace("inputs.pose", "inputs.nope"), encoding="utf-8")
    broken = gs.write(tmp_path / "broken.graph.yaml", "id: t.b\nnodes: {}\n")
    code, out, err = _cli(["graph", "validate", "--project", str(project), "beta", str(broken)], capsys)
    assert code == 1 and err == ""
    assert out.splitlines() == [
        f"{path.resolve()}: nodes.draw.in.pose: 'inputs.nope': no input 'nope'; inputs: ['pose']",
        f"{broken.resolve()}: nodes: nodes must be a non-empty mapping",
    ]


def test_cli_reports_a_bad_config_or_target(cli_project, capsys, tmp_path):
    project, _ = cli_project
    code, out, _ = _cli(["graph", "validate", "--project", str(project), "nosuch"], capsys)
    assert code == 1 and "'nosuch' is neither a graph in" in out
    code, out, _ = _cli(["graph", "validate", "--project", str(tmp_path / "empty")], capsys)
    assert code == 1 and "is not a directory" in out


def test_cli_without_the_op_registry_says_so(tmp_path, monkeypatch, capsys):
    project = gs.project(tmp_path, monkeypatch, graphs_yaml="version: 1\n")
    monkeypatch.setitem(sys.modules, "llm_kit.graph.ops.registry", None)
    code, out, _ = _cli(["graph", "validate", "--project", str(project), str(gs.BETA / "beta.graph.yaml")], capsys)
    assert (code, out) == (1, "the op registry (llm_kit.graph.ops.registry) is not in this build\n")


def test_graph_group_routes_verbs(monkeypatch, capsys):
    from llm_kit.cli import graph as group

    code, out, _ = _cli(["graph", "--help"], capsys)
    assert code == 0 and "usage: llm-kit graph" in out
    for verb in group.VERBS:
        assert verb in out
    assert list(group.VERBS) == ["validate", "run", "status", "answer", "resume", "serve", "replay"]
    assert group.verb_module("run") == "llm_kit.graph.commands.run"
    code, _, err = _cli(["graph", "nonesuch"], capsys)
    assert code == 2 and "invalid choice" in err
    monkeypatch.setitem(group.VERBS, "absent", "a verb with no module")
    code, _, err = _cli(["graph", "absent"], capsys)
    assert code == 2 and "'absent' is not in this build (llm_kit.graph.commands.absent does not exist)" in err
    code, out, _ = _cli(["graph", "validate", "--help"], capsys)
    assert code == 0 and "usage: llm-kit graph validate" in out
