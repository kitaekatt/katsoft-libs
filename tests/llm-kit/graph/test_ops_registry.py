"""The op registry: @op, namespace import by path, load_registry and the validate CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import graph_support as gs
from llm_kit.graph import ops
from llm_kit.graph.commands import validate as validate_command
from llm_kit.graph.config import load_config
from llm_kit.graph.model import GraphError, Op, OpContext, OpRegistry, ParamSpec, Port
from llm_kit.graph.ops import registry as registry_module
from llm_kit.graph.ops.registry import GRAPH_OP_ATTRIBUTE, builtin_ops, load_namespace, load_registry, op
from llm_kit.graph.testing import make_project

LIB = Path(ops.__file__).resolve().parents[3]
FIXTURE_OPS = gs.FIXTURES / "beta_ops"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def package(tmp_path: Path, files: dict[str, str], name: str = "ops") -> Path:
    root = tmp_path / name
    for relative, text in files.items():
        write(root / relative, text)
    return root


# ---------------------------------------------------------------------------
# @op
# ---------------------------------------------------------------------------


def test_op_declares_and_returns_the_function():
    @op("acme.double", version="2", inputs={"n": {"type": "integer"}}, outputs={"type": "integer"},
        paid=True, limit="heavy", description="doubles")
    def double(ctx: OpContext, n):
        return n * 2

    declared = getattr(double, GRAPH_OP_ATTRIBUTE)
    assert isinstance(declared, Op) and declared.ref == "acme.double@2"
    assert declared.inputs["n"] == Port({"type": "integer"})
    assert declared.outputs == {"type": "integer"} and declared.paid and declared.limit == "heavy"
    assert double(None, 4) == 8  # the function is returned unchanged


def test_op_accepts_a_long_form_port_with_a_default():
    @op("acme.greet", version="1", inputs={"who": {"schema": {"type": "string"}, "default": "you"}})
    def greet(ctx, who):
        return who

    port = getattr(greet, GRAPH_OP_ATTRIBUTE).inputs["who"]
    assert port.has_default and port.default == "you"


def test_op_resolves_relative_schema_files_against_the_defining_file(tmp_path):
    root = package(tmp_path, {
        "contracts/thing.schema.json": json.dumps({"type": "object", "required": ["id"],
                                                    "properties": {"id": {"type": "string"}}}),
        "__init__.py": "",
        "thing.py": """
            from llm_kit.graph.ops import op

            @op("acme.thing", version="1", inputs={"thing": {"$ref": "contracts/thing.schema.json"}})
            def thing(ctx, thing):
                return thing
        """,
    })
    (declared,) = load_namespace("acme", root)
    schema = declared.inputs["thing"].schema
    assert schema["type"] == "object" and "$ref" not in schema and "$defs" not in schema


def test_op_checks_the_function_signature():
    with pytest.raises(GraphError, match="OpContext as its first parameter"):
        op("acme.a", version="1")(lambda: None)
    with pytest.raises(GraphError, match=r"no parameter for input\(s\) \['x'\]"):
        op("acme.a", version="1", inputs={"x": {}})(lambda ctx: None)
    with pytest.raises(GraphError, match=r"parameter\(s\) \['y'\] have no matching input"):
        op("acme.a", version="1", inputs={"x": {}})(lambda ctx, x, y: None)
    with pytest.raises(GraphError, match="by keyword"):
        op("acme.a", version="1")(lambda ctx, *rest: None)
    assert op("acme.a", version="1", inputs={"x": {}})(lambda ctx, **inputs: None)  # **kwargs takes any input
    assert op("acme.a", version="1")(lambda ctx, y=1: None)  # a defaulted parameter needs no input


def test_op_checks_the_declaration():
    with pytest.raises(GraphError, match="must be dotted"):
        op("nodots", version="1")(lambda ctx: None)
    with pytest.raises(GraphError, match="invalid schema"):
        op("acme.a", version="1", outputs={"pattern": "x"})(lambda ctx: None)

    def declared(ctx):
        return None

    op("acme.a", version="1")(declared)
    with pytest.raises(GraphError, match="already declared"):
        op("acme.b", version="1")(declared)


# ---------------------------------------------------------------------------
# namespace import
# ---------------------------------------------------------------------------

GOOD = {
    "__init__.py": "",
    "shared.py": "TAG = 'shared'\n",
    "_private.py": "SECRET = 1\n",
    "plot.py": """
        from llm_kit.graph.ops import op
        from . import _private
        from .shared import TAG
        from .sub.deep import deep_value

        @op("acme.plot", version="1", outputs={"type": "string"})
        def plot(ctx):
            return TAG + str(_private.SECRET) + deep_value()
    """,
    "sub/__init__.py": "",
    "sub/deep.py": """
        from llm_kit.graph.ops import op
        from ..shared import TAG

        def deep_value():
            return TAG

        @op("acme.deep", version="1")
        def deep(ctx):
            return deep_value()
    """,
}


def test_a_namespace_imports_by_path_with_relative_imports(tmp_path):
    root = package(tmp_path, GOOD)
    found = load_namespace("acme", root)
    assert [o.name for o in found] == ["acme.deep", "acme.plot"]
    assert found[1].fn(None) == "shared1shared"
    assert str(root) not in sys.path and str(tmp_path) not in sys.path  # nothing leaked onto the import path


def test_an_op_imported_from_elsewhere_is_not_collected(tmp_path):
    root = package(tmp_path, {
        "__init__.py": "from llm_kit.graph.ops.registry import builtin_ops\nfrom llm_kit.graph.ops.validate import "
                       "llm_validate\n",
        "mine.py": """
            from llm_kit.graph.ops import op
            from llm_kit.graph.ops.validate import llm_validate

            @op("acme.mine", version="1")
            def mine(ctx):
                return 1
        """,
    })
    assert [o.name for o in load_namespace("acme", root)] == ["acme.mine"]


def test_loading_again_reimports_changed_source(tmp_path):
    root = package(tmp_path, GOOD)
    first = load_namespace("acme", root)
    write(root / "plot.py", """
        from llm_kit.graph.ops import op

        @op("acme.plot", version="2")
        def plot(ctx):
            return 2
    """)
    second = load_namespace("acme", root)
    assert {o.name: o.version for o in first}["acme.plot"] == "1"
    assert {o.name: o.version for o in second}["acme.plot"] == "2"


@pytest.mark.parametrize("files, message", [
    ({"plot.py": "x = 1"}, "not a Python package"),
    ({"__init__.py": ""}, "defines no operations"),
    ({"__init__.py": "raise RuntimeError('broken import')"}, "importing the package failed: RuntimeError: broken import"),
    ({"__init__.py": "", "bad.py": "from . import missing_module"}, "importing the package failed: ImportError"),
    ({"__init__.py": "", "a.py": "from llm_kit.graph.ops import op\n@op('other.name', version='1')\n"
                                   "def a(ctx): pass"}, "is not named acme.<name>"),
    ({"__init__.py": "", "a.py": "from llm_kit.graph.ops import op\n@op('llm.mine', version='1')\n"
                                   "def a(ctx): pass"}, "is not named acme.<name>"),
])
def test_namespace_faults_are_loud(tmp_path, files, message):
    root = package(tmp_path, files)
    with pytest.raises(GraphError, match=message):
        load_namespace("acme", root)
    prefix = registry_module._module_name("acme", root.resolve())
    assert not [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]  # a failed load leaves nothing


def test_a_missing_directory_is_an_error(tmp_path):
    with pytest.raises(GraphError, match="does not exist"):
        load_namespace("acme", tmp_path / "nope")


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------


def project_with(tmp_path: Path, namespaces: dict[str, str], files: dict[str, dict[str, str]]) -> Path:
    root = tmp_path / "proj"
    for directory, tree in files.items():
        for relative, text in tree.items():
            write(root / directory / relative, text)
    lines = ["version: 1", "namespaces:" if namespaces else "namespaces: {}"]
    lines += [f"  {ns}: {path}" for ns, path in namespaces.items()]
    write(root / ".llm-kit" / "graphs.yaml", "\n".join(lines) + "\n")
    return root


def test_load_registry_has_the_builtin_ops_and_the_configured_namespaces(tmp_path):
    root = project_with(tmp_path, {"acme.one": "one", "acme.two": "two"}, {
        "one": {"__init__.py": "", "a.py": "from llm_kit.graph.ops import op\n@op('acme.one.a', version='1')\n"
                                            "def a(ctx): return 1"},
        "two": {"__init__.py": "", "b.py": "from llm_kit.graph.ops import op\n@op('acme.two.b', version='1')\n"
                                            "def b(ctx): return 2"},
    })
    registry = load_registry(load_config(root))
    assert isinstance(registry, OpRegistry)
    assert registry.names() == ["acme.one.a", "acme.two.b", "llm.model", "llm.subprocess", "llm.validate"]
    assert all(op_.version for op_ in builtin_ops())


def test_load_registry_without_namespaces_has_only_the_builtin_ops(tmp_path):
    root = project_with(tmp_path, {}, {})
    assert load_registry(load_config(root)).names() == ["llm.model", "llm.subprocess", "llm.validate"]


def test_a_name_defined_twice_is_an_error(tmp_path):
    body = "from llm_kit.graph.ops import op\n@op('acme.x', version='1')\ndef x(ctx): return 1"
    root = package(tmp_path, {"__init__.py": "", "a.py": body, "b.py": body.replace("def x", "def y")})
    with pytest.raises(GraphError, match="registered twice"):
        registry = OpRegistry()
        for declared in load_namespace("acme", root):
            registry.register(declared)


def test_the_builtin_op_declarations():
    registry = OpRegistry({o.name: o for o in builtin_ops()})
    model = registry.get("llm.model")
    assert model.output_schema_param == "output_schema" and model.paid
    assert model.attributes == frozenset({"out", "sent", "text", "files"})
    assert model.params["prompt"] == ParamSpec("template", required=True, slots="slots",
                                               description=model.params["prompt"].description)
    assert registry.get("llm.validate").inputs["value"].schema == {}
    assert registry.get("llm.subprocess").inputs["payload"].default is None


# ---------------------------------------------------------------------------
# the validate verb with a registry loaded from config
# ---------------------------------------------------------------------------


def beta_project(tmp_path: Path, monkeypatch) -> Path:
    mock = make_project(tmp_path / "mock", [gs.SELECTION, gs.EVAL_SELECTION])
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(mock.endpoints))
    root = mock.root
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(gs.BETA, root / "beta", ignore=ignore)
    shutil.copytree(FIXTURE_OPS, root / "beta_ops", ignore=ignore)
    write(root / ".llm-kit" / "graphs.yaml", """\
        version: 1
        namespaces:
          afterplay.beta: beta_ops
        graphs:
          afterplay.beta: beta/beta.graph.yaml
    """)
    return root


def test_the_validate_verb_accepts_the_fixture_with_a_registry_from_config(tmp_path, monkeypatch, capsys):
    root = beta_project(tmp_path, monkeypatch)
    assert validate_command.run(["--project", str(root)]) == 0
    assert capsys.readouterr().out == "ok: 1 graph(s)\n"


def test_the_validate_verb_reports_a_removed_op(tmp_path, monkeypatch, capsys):
    root = beta_project(tmp_path, monkeypatch)
    plot = root / "beta_ops" / "plot.py"
    plot.write_text(plot.read_text(encoding="utf-8").replace("afterplay.beta.keep_best", "afterplay.beta.keep_gone"),
                    encoding="utf-8")
    assert validate_command.run(["--project", str(root)]) == 1
    assert "unknown op 'afterplay.beta.keep_best'" in capsys.readouterr().out


def test_the_cli_entry_validates_the_fixture(tmp_path, monkeypatch):
    root = beta_project(tmp_path, monkeypatch)
    env = {**os.environ, "PYTHONPATH": str(LIB), "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"}
    done = subprocess.run([sys.executable, "-m", "llm_kit.cli", "graph", "validate", "--project", str(root)],
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    assert (done.returncode, done.stdout) == (0, "ok: 1 graph(s)\n"), done.stderr


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------


def test_public_surface_lists_the_new_modules():
    surface = json.loads((LIB.parent / "public-surface.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in surface["entries"]}
    for module in ("prompt", "testing", "ops", "ops.registry", "ops.model", "ops.validate", "ops.subprocess"):
        mod = __import__(f"llm_kit.graph.{module}", fromlist=["__all__"])
        for name in mod.__all__:
            assert f"llm_kit.graph.{module}.{name}" in names, f"{module}.{name}"
