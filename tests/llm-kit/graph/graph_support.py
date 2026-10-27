"""Shared helpers for the llm_kit.graph format tests.

* :func:`use_expr` makes ``llm_kit.graph.expr`` importable for validate. When
  the expression module (unit L02) is in the tree, the real module is used.
  Until then a stub stands in: it implements only what validate calls --
  ``parse`` (refuses an empty or unbalanced text, an unknown character, a
  text that ends in an operator, or a
  name starting with ``_``), ``references`` (dotted names up to the first
  ``[``, without keywords, function names and string literals) and
  ``ExprError``. Delete the stub once L02 has landed.
* :func:`registry` builds an op registry shaped like the planned Beta ops,
  with an ``llm.model`` stand-in that declares the parameter kinds the generic
  model op is expected to use.
* :func:`project` writes an endpoint registry and a project model file.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

from llm_kit.graph.loader import resolve_schema
from llm_kit.graph.model import Op, OpRegistry, ParamSpec, Port

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BETA = FIXTURES / "beta"

SELECTION = "beta.story-structure.generate.story-structure"
EVAL_SELECTION = "beta.story-structure.evaluate.plot"


# ---------------------------------------------------------------------------
# expression module
# ---------------------------------------------------------------------------


class _StubExprError(ValueError):
    def __init__(self, message: str, position: int | None = None) -> None:
        self.position = position
        super().__init__(message if position is None else f"{message} (at position {position})")


_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_PATH = re.compile(r"(?<![\w.\]])([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)*)(\s*\()?")
_ALLOWED = re.compile(r"^[\w\s.\[\]()+\-<>=!?,'\"]*$")
_NOT_REFS = {"and", "or", "not", "true", "false", "null"}


def _stub_parse(text):
    if not isinstance(text, str) or not text.strip():
        raise _StubExprError("expression is empty", 0)
    bare = _STRING.sub("''", text)
    if not _ALLOWED.match(bare):
        bad = next(i for i, c in enumerate(bare) if not _ALLOWED.match(c))
        raise _StubExprError(f"unexpected character {bare[bad]!r}", bad)
    depth = 0
    for i, c in enumerate(bare):
        depth += c in "(["
        depth -= c in ")]"
        if depth < 0:
            raise _StubExprError("unbalanced bracket", i)
    if depth:
        raise _StubExprError("unbalanced bracket", len(text))
    if re.search(r"[-+<>=!?.,(\[]\s*$", bare):
        raise _StubExprError("expression ends with an operator", len(text))
    for match in _PATH.finditer(bare):
        if match.group(1).startswith("_"):
            raise _StubExprError("name starts with an underscore", match.start())
    return ("stub", bare)


def _stub_references(expr):
    found = set()
    for match in _PATH.finditer(expr[1]):
        name, rest, call = match.groups()
        if call or name in _NOT_REFS:
            continue
        found.add(name + rest)
    return frozenset(found)


def use_expr(monkeypatch) -> None:
    """Make ``llm_kit.graph.expr`` importable: the real module, else the stub."""
    if importlib.util.find_spec("llm_kit.graph.expr") is not None:
        return
    stub = types.ModuleType("llm_kit.graph.expr")
    stub.ExprError = _StubExprError
    stub.parse = _stub_parse
    stub.references = _stub_references
    monkeypatch.setitem(sys.modules, "llm_kit.graph.expr", stub)


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------


def _fn(ctx, **inputs):
    return inputs


def contract(name: str) -> dict:
    """A Beta fixture contract as a self-contained schema."""
    return resolve_schema({"$ref": f"contracts/{name}"}, BETA)


def model_op() -> Op:
    return Op(
        name="llm.model", version="1", fn=_fn,
        params={
            "model": ParamSpec("selection", required=True),
            "prompt": ParamSpec("template", required=True, slots="slots"),
            "slots": ParamSpec("exprs"),
            "suffix": ParamSpec("expr"),
            "output_schema": ParamSpec("schema"),
            "effort": ParamSpec("literal"),
        },
        attributes=frozenset({"out", "sent", "text"}),
        output_schema_param="output_schema",
    )


def registry(*extra: Op) -> OpRegistry:
    """The Beta-shaped registry used by the fixture graphs."""
    plot = contract("plot.v2.schema.json")
    ops = [
        model_op(),
        Op("afterplay.beta.plot_prompt_parts", "1", _fn,
           inputs={"brief": Port(contract("brief.v1.schema.json")), "current": Port({}),
                   "findings": Port({}), "guidance": Port({"type": "string"})},
           outputs={"type": "object", "properties": {k: {"type": "string"} for k in (
               "shapes", "changes", "findings", "feedback", "task", "declared_inputs")}}),
        Op("afterplay.beta.plot_errors", "1", _fn,
           inputs={"answer": Port(contract("plot-answer.v2.schema.json"))},
           outputs={"type": "object", "required": ["ok", "errors", "numbered"],
                    "properties": {"ok": {"type": "boolean"}, "errors": {"type": "array"},
                                   "numbered": {"type": "string"}}}),
        Op("afterplay.beta.plot_accept", "1", _fn, inputs={"answer": Port({})},
           outputs=resolve_schema({"type": "object", "properties": {"plot": {"$ref": "contracts/plot.v2.schema.json"}}},
                                  BETA)),
        Op("afterplay.beta.plot_score", "1", _fn,
           inputs={"evaluation": Port(contract("plot-evaluation.v1.schema.json"))},
           outputs={"type": "object", "properties": {"score": {"type": "integer"}}}),
        Op("afterplay.beta.keep_best", "1", _fn,
           inputs={"incumbent": Port({}), "baseline": Port({}), "rounds": Port({"type": "array"})},
           outputs=resolve_schema({"type": "object", "properties": {
               "plot": {"$ref": "contracts/plot.v2.schema.json"},
               "evaluation": {"$ref": "contracts/plot-evaluation.v1.schema.json"},
               "round": {"type": "integer"}}}, BETA)),
        Op("afterplay.beta.pose_image", "1", _fn,
           inputs={"pose": Port({"type": "object", "properties": {"id": {"type": "string"}}})},
           outputs={"type": "object", "properties": {"image": {"type": "string"}}}),
        Op("afterplay.beta.bundle", "1", _fn,
           inputs={"plot": Port(plot), "images": Port({"type": "array"})},
           outputs=contract("beta-deliverable.v1.schema.json")),
        Op("afterplay.beta.encode", "1", _fn, commit=True,
           inputs={"bundle": Port(contract("beta-deliverable.v1.schema.json"))},
           outputs={"type": "object", "properties": {"preview_url": {"type": "string"},
                                                     "files_sha256": {"type": "string"}}}),
    ]
    return OpRegistry({op.name: op for op in (*ops, *extra)})


# ---------------------------------------------------------------------------
# a project with a model file
# ---------------------------------------------------------------------------

ENDPOINTS = """\
default: plain
models:
  plain: {base_url: http://h.invalid/v1, model: m-plain}
"""


def models_yaml(*selections: str) -> str:
    lines = ["version: 1", "catalog:", "  - {id: mid, label: Mid, endpoint: plain, model: deepseek/x}", "selections:"]
    lines += [f"  {key}: {{model: mid}}" for key in selections]
    return "\n".join(lines) + "\n"


def project(tmp_path: Path, monkeypatch, *, selections=(SELECTION, EVAL_SELECTION), graphs_yaml: str | None = None
            ) -> Path:
    """A project root with an endpoint registry, a model file and (optionally) a graph config."""
    endpoints = tmp_path / "endpoints.yaml"
    endpoints.write_text(ENDPOINTS, encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(endpoints))
    root = tmp_path / "proj"
    (root / ".llm-kit").mkdir(parents=True)
    (root / ".llm-kit" / "models.yaml").write_text(models_yaml(*selections), encoding="utf-8")
    if graphs_yaml is not None:
        (root / ".llm-kit" / "graphs.yaml").write_text(graphs_yaml, encoding="utf-8")
    return root


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
