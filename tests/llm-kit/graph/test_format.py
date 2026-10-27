"""The graph file format: the model, the loader, schema bundling, templates, lazy exports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import graph_support as gs
from llm_kit.graph import loader, model
from llm_kit.graph.loader import load_graph, prompt_body, prompt_slots, resolve_params, resolve_schema
from llm_kit.graph.model import GraphError, Literal, Op, OpRegistry, ParamSpec, UnknownOpError
from llm_kit.schema.json_schema import check_schema, validate as validate_value

LIB = Path(model.__file__).resolve().parents[2]


def _graph(tmp_path: Path, text: str, name: str = "g.graph.yaml") -> Path:
    return gs.write(tmp_path / name, text)


MINIMAL = """\
id: t.minimal
inputs:
  x: {type: integer}
outputs:
  y: {type: integer}
nodes:
  a:
    op: t.ops.inc
    in: {x: inputs.x}
outputs_from:
  y: a.out
"""


# ---------------------------------------------------------------------------
# the Beta-shaped fixtures load
# ---------------------------------------------------------------------------


class TestBetaFixtures:
    def test_outer_graph_and_every_node_kind(self):
        graph = load_graph(gs.BETA / "beta.graph.yaml")
        assert graph.id == "afterplay.beta" and graph.outer and not graph.inline
        kinds = {node_id: node.kind for node_id, node in graph.nodes.items()}
        assert kinds == {"plot": "graph", "pose_images": "map", "bundle": "op", "encode": "commit", "played": "human"}
        pose = graph.nodes["pose_images"].map
        assert not pose.inline and pose.over == "inputs.moment.poses" and pose.key == "item.id"
        assert pose.body.id == "afterplay.beta.pose-image" and pose.bind == {"pose": "item"}
        encode = graph.nodes["encode"].commit
        assert encode.adapter == "afterplay.beta.encode" and encode.bundle == "bundle.out"
        check_schema(encode.contract)
        played = graph.nodes["played"].human
        assert played.kind == "playtest-played" and set(played.payload) == {"url", "files_sha256"}

    def test_story_structure_repeat_with_inline_body(self):
        stage = load_graph(gs.BETA / "beta.graph.yaml").nodes["plot"].subgraph
        assert stage.id == "afterplay.beta.story-structure" and not stage.outer
        assert stage.inputs["guidance"].default == "" and stage.inputs["max_rounds"].default == 3
        assert not stage.inputs["brief"].has_default
        loop = stage.nodes["loop"].repeat
        assert loop.inline and loop.max == "inputs.max_rounds"
        assert loop.carry["best"].init == "baseline.out.score ?? null"
        assert list(loop.body.nodes) == ["prep", "write", "check", "repair", "accept", "evaluate"]
        assert loop.body.inline and loop.body.location == "nodes.loop.repeat.body"
        assert loop.body.id == "afterplay.beta.story-structure#loop"
        assert set(loop.body.out) == {"plot", "evaluation", "score"}
        write = loop.body.nodes["write"]
        assert write.op == "llm.model" and set(write.params) == {"model", "prompt", "slots", "suffix", "output_schema"}
        assert stage.nodes["loop"].repeat.body.nodes["repair"].when == "not check.out.ok"

    def test_a_subgraph_file_loads_once(self):
        stage = load_graph(gs.BETA / "stages" / "story-structure.graph.yaml")
        assert stage.nodes["baseline"].subgraph is stage.nodes["loop"].repeat.body.nodes["evaluate"].subgraph

    def test_literal_scalars_are_literals(self, tmp_path):
        path = _graph(tmp_path, MINIMAL.replace("in: {x: inputs.x}", "in: {x: 3}\n    when: true"))
        node = load_graph(path).nodes["a"]
        assert node.bind == {"x": Literal(3)} and node.when == Literal(True)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class TestResolveSchema:
    def test_file_references_are_bundled_with_their_own_defs(self):
        schema = resolve_schema({"$ref": "contracts/plot-answer.v2.schema.json"}, gs.BETA)
        check_schema(schema)
        assert schema["$ref"] == "#/$defs/file:contracts~1plot-answer.v2.schema.json"
        defs = schema["$defs"]
        assert set(defs) == {"file:contracts/plot-answer.v2.schema.json", "file:contracts/plot.v2.schema.json",
                             "file:contracts/plot.v2.schema.json#beat"}
        assert defs["file:contracts/plot.v2.schema.json"]["properties"]["beats"]["items"] == {
            "$ref": "#/$defs/file:contracts~1plot.v2.schema.json#beat"}
        good = {"plot": {"beats": [{"id": "b1"}]}}
        assert validate_value(schema, good) == ()
        assert validate_value(schema, {"plot": {"beats": [{"turn": "x"}]}}) != ()

    def test_relative_keys_follow_the_root_directory(self):
        schema = resolve_schema({"$ref": "../contracts/brief.v1.schema.json"}, gs.BETA / "stages")
        assert "file:../contracts/brief.v1.schema.json" in schema["$defs"]

    def test_enum_and_const_values_are_not_rewritten(self, tmp_path):
        schema = resolve_schema({"enum": [{"$ref": "x.json"}], "const": {"$ref": "y"}}, tmp_path)
        assert schema == {"enum": [{"$ref": "x.json"}], "const": {"$ref": "y"}}

    def test_root_defs_stay_local(self, tmp_path):
        schema = resolve_schema({"$ref": "#/$defs/a", "$defs": {"a": {"type": "string"}}}, tmp_path)
        assert schema == {"$ref": "#/$defs/a", "$defs": {"a": {"type": "string"}}}

    @pytest.mark.parametrize("schema, files, message", [
        ({"$ref": "x.json#/$defs/a"}, {"x.json": "{}"}, "without a fragment"),
        ({"$ref": "/abs/x.json"}, {}, "must be a relative path"),
        ({"$ref": "missing.json"}, {}, "does not exist"),
        ({"$ref": "bad.json"}, {"bad.json": "{nope"}, "not valid JSON"),
        ({"$ref": "dup.json"}, {"dup.json": '{"type": "string", "type": "integer"}'}, "duplicate key"),
        ({"$ref": "nan.json"}, {"nan.json": '{"maximum": NaN}'}, "not valid JSON"),
        ({"$ref": "list.json"}, {"list.json": "[]"}, "must hold a JSON object"),
        ({"type": "string", "pattern": "a+"}, {}, "unsupported schema keyword 'pattern'"),
        ({"$ref": "#/$defs/none"}, {}, "does not resolve"),
        ({"$defs": {"file:x.json": {}}, "$ref": "x.json"}, {"x.json": "{}"}, "reserved for bundled"),
        ([], {}, "must be a mapping"),
    ])
    def test_refusals(self, tmp_path, schema, files, message):
        for name, text in files.items():
            gs.write(tmp_path / name, text)
        with pytest.raises(GraphError, match=message):
            resolve_schema(schema, tmp_path)

    def test_a_file_cycle_is_bundled_once(self, tmp_path):
        gs.write(tmp_path / "a.json", '{"type": "object", "properties": {"b": {"$ref": "b.json"}}}')
        gs.write(tmp_path / "b.json", '{"type": "object", "properties": {"a": {"$ref": "a.json"}}}')
        schema = resolve_schema({"$ref": "a.json"}, tmp_path)
        assert set(schema["$defs"]) == {"file:a.json", "file:b.json"}


# ---------------------------------------------------------------------------
# prompt templates
# ---------------------------------------------------------------------------


class TestPromptTemplates:
    def test_header_is_not_prompt_text(self):
        text = (gs.BETA / "prompts" / "story-structure.md").read_text(encoding="utf-8")
        assert prompt_slots(text) == ("shapes", "changes", "findings", "feedback", "task")
        assert prompt_body(text).startswith("You write the plot structure")
        assert "not_a_slot" not in prompt_body(text)

    def test_without_a_marker_the_whole_file_is_prompt(self):
        assert prompt_body("a {{b}}\n") == "a {{b}}\n"
        assert prompt_slots("{{b}} {{a}} {{b}} {{Not}} {{ c }}") == ("b", "a")

    def test_two_markers_are_refused(self):
        with pytest.raises(GraphError, match="2 '---PROMPT---' lines"):
            prompt_body("h\n---PROMPT---\na\n---PROMPT---\nb")

    def test_the_marker_must_be_the_whole_line(self):
        assert prompt_body("x ---PROMPT---\n{{a}}") == "x ---PROMPT---\n{{a}}"


# ---------------------------------------------------------------------------
# structural refusals
# ---------------------------------------------------------------------------


def _replace(old: str, new: str) -> str:
    assert old in MINIMAL
    return MINIMAL.replace(old, new)


class TestStructure:
    @pytest.mark.parametrize("text, message", [
        (MINIMAL + "extra: 1\n", r"unknown graph keys \['extra'\]"),
        (_replace("id: t.minimal", "id: 'bad id'"), "must be a dotted name"),
        (_replace("id: t.minimal\n", "id: t.minimal\nouter: yes please\n"), "outer must be true or false"),
        (_replace("    op: t.ops.inc\n", "    op: t.ops.inc\n    graph: x.graph.yaml\n"), "exactly one kind key"),
        (_replace("    op: t.ops.inc\n", ""), "exactly one kind key"),
        (_replace("  a:\n", "  inputs:\n").replace("a.out", "inputs.x"), "node name 'inputs' is reserved"),
        (_replace("  a:\n", "  _a:\n"), "must match"),
        (_replace("  a:\n", "  a-b:\n"), "must match"),
        (_replace("  y: {type: integer}\n", "  y: {type: integer}\n  z: {type: integer}\n"),
         "outputs_from must bind exactly"),
        (_replace("op: t.ops.inc", "op: inc"), "must be a dotted op name"),
        (_replace("in: {x: inputs.x}", "in: {x: ''}"), "may not be empty"),
        (_replace("in: {x: inputs.x}", "in: {x: [1]}"), "expression \\(a string\\) or a literal"),
        (_replace("  x: {type: integer}", "  x: {schema: {type: integer}, default: nope}"),
         "default does not match"),
        (_replace("  x: {type: integer}", "  x: {schema: {type: integer}, deflt: 1}"), "unknown port keys"),
        (_replace("  x: {type: integer}", "  len: {type: integer}"), "port name 'len' is reserved"),
        (_replace("  x: {type: integer}", "  null: {type: integer}"), "mapping key None is not a string"),
        (_replace("nodes:\n  a:\n    op: t.ops.inc\n    in: {x: inputs.x}\n", "nodes: {}\n"),
         "nodes must be a non-empty mapping"),
        (MINIMAL.replace("  a:\n", "  a:\n    x: 1\n  a:\n"), "duplicate mapping key"),
        (_replace("in: {x: inputs.x}", "in: {x: 2026-10-26}"), "date values are not JSON"),
        (_replace("in: {x: inputs.x}", "in: {x: .nan}"), "non-finite"),
        ("", "file is empty"),
        ("id: [unclosed\n", "not valid YAML"),
    ])
    def test_refusals(self, tmp_path, text, message):
        with pytest.raises(GraphError, match=message):
            load_graph(_graph(tmp_path, text))

    COMPOSITE = """\
id: t.composite
nodes:
  n:
{body}
"""

    @pytest.mark.parametrize("body, message", [
        ("    map: {over: inputs.xs, graph: s.graph.yaml}", "map needs key"),
        ("    map: {key: item, graph: s.graph.yaml}", "map needs over"),
        ("    map: {over: x, key: item}", "exactly one of graph"),
        ("    map: {over: x, key: item, graph: s.graph.yaml, body: {nodes: {a: {op: t.o}}, out: {v: a.out}}}",
         "exactly one of graph"),
        ("    map: {over: x, key: item, body: {nodes: {a: {op: t.o}}, out: {v: a.out}}, in: {v: x}}",
         "an inline body reads the scope"),
        ("    map: {over: x, key: item, body: {nodes: {a: {op: t.o}}}}", "an inline body needs out"),
        ("    map: {over: x, key: item, body: {nodes: {a: {op: t.o}}, out: {}}}", "out must bind at least one"),
        ("    map: {over: x, key: item, body: {nodes: {a: {op: t.o}}, out: {v: a.out}, extra: 1}}",
         "unknown body keys"),
        ("    repeat: {graph: s.graph.yaml}", "repeat needs max"),
        ("    repeat: {max: 2, graph: s.graph.yaml, carry: {c: {init: 1}}}", "needs init and next"),
        ("    repeat: {max: 2, graph: s.graph.yaml, carry: {round: {init: 1, next: 2}}}", "carry name 'round'"),
        ("    repeat: {max: 2, graph: s.graph.yaml, stop: x}", "unknown repeat keys"),
        ("    repeat: {max: 2, graph: missing.graph.yaml}", "does not exist"),
        ("    human: {schema: {type: object}}", "human needs kind"),
        ("    human: {kind: k}", "human needs schema"),
        ("    commit: {contract: {type: object}, bundle: x}", r"commit needs \['adapter'\]"),
        ("    commit: {contract: {type: object}, bundle: x, adapter: nodots}", "dotted op name"),
        ("    graph: s.graph.yaml\n    model: x", "unknown graph node keys"),
        ("    graph: /abs/s.graph.yaml", "must be a relative path"),
        ("    human: {kind: k, schema: {type: object}}\n    in: {a: x}", "unknown human node keys"),
    ])
    def test_composite_refusals(self, tmp_path, body, message):
        gs.write(tmp_path / "s.graph.yaml", "id: t.s\nnodes:\n  a: {op: t.o}\n")
        with pytest.raises(GraphError, match=message):
            load_graph(_graph(tmp_path, self.COMPOSITE.format(body=body)))

    def test_a_subgraph_cycle_is_refused(self, tmp_path):
        gs.write(tmp_path / "a.graph.yaml", "id: t.a\nnodes:\n  n: {graph: b.graph.yaml}\n")
        gs.write(tmp_path / "b.graph.yaml", "id: t.b\nnodes:\n  n: {graph: a.graph.yaml}\n")
        with pytest.raises(GraphError, match="subgraph cycle"):
            load_graph(tmp_path / "a.graph.yaml")

    def test_an_outer_graph_cannot_be_a_subgraph(self, tmp_path):
        gs.write(tmp_path / "o.graph.yaml", "id: t.o\nouter: true\nnodes:\n  n: {op: t.x}\n")
        with pytest.raises(GraphError, match="an outer graph cannot be a subgraph"):
            load_graph(_graph(tmp_path, "id: t.p\nnodes:\n  n: {graph: o.graph.yaml}\n"))

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(GraphError, match="file does not exist"):
            load_graph(tmp_path / "none.graph.yaml")

    def test_the_error_names_the_file_and_location(self, tmp_path):
        path = _graph(tmp_path, _replace("in: {x: inputs.x}", "in: {x: [1]}"))
        with pytest.raises(GraphError) as info:
            load_graph(path)
        assert info.value.where == f"{path}: nodes.a.in.x"


# ---------------------------------------------------------------------------
# op parameters
# ---------------------------------------------------------------------------


class TestResolveParams:
    def _write_node(self, tmp_path, params: str):
        gs.write(tmp_path / "p.md", "head\n---PROMPT---\n{{a}} {{b}}\n")
        gs.write(tmp_path / "data.bin", "bytes")
        return load_graph(_graph(tmp_path, f"id: t.p\nnodes:\n  w:\n    op: llm.model\n{params}"))

    def test_kinds_resolve(self, tmp_path):
        graph = self._write_node(tmp_path, "    model: sel\n    prompt: p.md\n    slots: {a: x.out, b: 2}\n"
                                           "    suffix: y.out\n    output_schema: {type: string}\n    effort: high\n")
        params = resolve_params(graph.nodes["w"], gs.model_op(), graph)
        assert params["prompt"].slots == ("a", "b") and params["prompt"].body == "{{a}} {{b}}\n"
        assert params["prompt"].path == (tmp_path / "p.md").resolve()
        assert params["slots"].bindings == {"a": "x.out", "b": Literal(2)}
        assert params["suffix"].bindings == {"": "y.out"}
        assert params["output_schema"].schema == {"type": "string"}
        assert params["effort"].value == "high" and params["model"].value == "sel"

    def test_file_parameter_reads_bytes(self, tmp_path):
        op = Op("t.f", "1", lambda ctx: None, params={"data": ParamSpec("file", required=True)})
        graph = self._write_node(tmp_path, "    data: data.bin\n")
        node = graph.nodes["w"]
        node = type(node)(**{**node.__dict__, "op": "t.f"})
        assert resolve_params(node, op, graph)["data"].data == b"bytes"

    @pytest.mark.parametrize("params, message", [
        ("    model: sel\n    prompt: p.md\n    temperature: 1\n", r"takes no parameters \['temperature'\]"),
        ("    prompt: p.md\n", "needs parameter 'model'"),
        ("    model: sel\n    prompt: none.md\n", "does not exist"),
        ("    model: sel\n    prompt: /abs.md\n", "must be a relative path"),
        ("    model: 3\n    prompt: p.md\n", "must be a model selection key"),
        ("    model: sel\n    prompt: p.md\n    slots: [a]\n", "mapping of name -> expression"),
        ("    model: sel\n    prompt: p.md\n    output_schema: {type: nope}\n", "invalid schema"),
    ])
    def test_refusals(self, tmp_path, params, message):
        graph = self._write_node(tmp_path, params)
        with pytest.raises(GraphError, match=message):
            resolve_params(graph.nodes["w"], gs.model_op(), graph)


# ---------------------------------------------------------------------------
# the operation interfaces
# ---------------------------------------------------------------------------


class TestOpInterfaces:
    def _fn(self, ctx):
        return None

    @pytest.mark.parametrize("kwargs, message", [
        ({"name": "nodots"}, "must be dotted"),
        ({"version": ""}, "version must be"),
        ({"fn": 3}, "not callable"),
        ({"attributes": frozenset({"sent"})}, "must include 'out'"),
        ({"attributes": frozenset({"out", "carry"})}, "not a valid name"),
        ({"params": {"in": ParamSpec("literal")}}, "reserved node key"),
        ({"params": {"p": ParamSpec("template", slots="s")}}, "not an 'exprs' parameter"),
        ({"output_schema_param": "x"}, "not a 'schema' parameter"),
        ({"params": {"s": ParamSpec("schema")}, "output_schema_param": "s", "outputs": {}}, "both outputs"),
    ])
    def test_op_refusals(self, kwargs, message):
        args = {"name": "t.op", "version": "1", "fn": self._fn, **kwargs}
        with pytest.raises(GraphError, match=message):
            Op(**args)

    def test_param_spec_refusals(self):
        with pytest.raises(GraphError, match="unknown parameter kind"):
            ParamSpec("text")
        with pytest.raises(GraphError, match="only a template parameter"):
            ParamSpec("exprs", slots="x")
        with pytest.raises(GraphError, match="only a template parameter"):
            ParamSpec("template")

    def test_registry(self):
        op = Op("t.op", "2", self._fn)
        registry = OpRegistry({"t.op": op})
        assert registry.get("t.op") is op and "t.op" in registry and registry.names() == ["t.op"]
        assert op.ref == "t.op@2"
        with pytest.raises(GraphError, match="registered twice"):
            registry.register(Op("t.op", "3", self._fn))
        with pytest.raises(UnknownOpError, match="unknown op 't.none'"):
            registry.get("t.none")
        with pytest.raises(KeyError):
            registry.get("t.none")


# ---------------------------------------------------------------------------
# the package: lazy exports and opt-in
# ---------------------------------------------------------------------------


def _python(code: str) -> str:
    contracts = LIB.parents[1] / "katsoft-contracts" / "src"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(LIB), str(contracts), os.environ.get("PYTHONPATH", "")])}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


class TestPackage:
    def test_importing_the_package_imports_no_module(self):
        out = _python("import sys, llm_kit.graph; "
                      "print(sorted(m for m in sys.modules if m.startswith('llm_kit.graph.')))")
        assert out.strip() == "[]"

    def test_names_resolve_lazily_to_their_module(self):
        import llm_kit.graph as graph

        assert graph.Op is model.Op and graph.load_graph is loader.load_graph
        assert graph.validate_graph.__module__ == "llm_kit.graph.validate"
        assert {"Op", "GraphConfig", "hash_graph", "Problem"} <= set(dir(graph))
        with pytest.raises(AttributeError):
            graph.no_such_name  # noqa: B018

    def test_every_listed_module_name_is_unique(self):
        import llm_kit.graph as graph

        index = graph._exports()
        assert index["Op"] == "model" and index["load_config"] == "config"

    def test_the_core_import_path_does_not_import_the_graph_package(self):
        out = _python("import sys, llm_kit.cli, llm_kit.dispatch.invoke, llm_kit.pipeline.runtime.platform; "
                      "print(any(m == 'llm_kit.graph' or m.startswith('llm_kit.graph.') for m in sys.modules))")
        assert out.strip() == "False"

    def test_the_graph_extra_is_declared(self):
        import tomllib

        project = tomllib.loads((LIB.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        assert project["optional-dependencies"]["graph"] == ["pyyaml>=6.0"]

    def test_public_surface_lists_the_graph_names(self):
        surface = json.loads((LIB.parent / "public-surface.json").read_text(encoding="utf-8"))
        names = {e["name"] for e in surface["entries"]}
        assert "llm_kit.graph" in surface["covers"]
        for module in ("model", "loader", "config", "hashing", "validate"):
            mod = __import__(f"llm_kit.graph.{module}", fromlist=["__all__"])
            for name in mod.__all__:
                assert f"llm_kit.graph.{module}.{name}" in names, name
