"""``llm.validate``: check a value against a JSON Schema.

::

    check:
      op: llm.validate
      in: {value: write.out}
      schema: {$ref: ../contracts/plot.v2.schema.json}
      mode: report

``schema`` is in the :mod:`llm_kit.schema.json_schema` subset. ``out`` is
``{ok, errors, numbered}`` in both modes: ``errors`` is one ``"<pointer>
<keyword>"`` string per fault (the pointer is ``/`` for the root, sorted),
``numbered`` the same lines as ``1. ...`` text for a repair prompt.

``mode`` is ``fail`` (the default: a value that does not match raises
:class:`OpFailed` with code ``schema-mismatch`` and the faults in the
message) or ``report`` (the faults are the result; a later node decides).
"""

from __future__ import annotations

from typing import Any

from llm_kit.schema.json_schema import validate as validate_value

from ..model import OpContext, OpResult, ParamSpec, Port
from .registry import OpFailed, op

__all__ = ["MODES", "RESULT_SCHEMA", "llm_validate"]

MODES = ("fail", "report")

RESULT_SCHEMA = {
    "type": "object",
    "required": ["ok", "errors", "numbered"],
    "properties": {
        "ok": {"type": "boolean"},
        "errors": {"type": "array", "items": {"type": "string"}},
        "numbered": {"type": "string"},
    },
}


@op(
    "llm.validate",
    version="1",
    inputs={"value": Port({}, description="the value to check")},
    outputs=RESULT_SCHEMA,
    params={
        "schema": ParamSpec("schema", required=True, description="the schema the value must match"),
        "mode": ParamSpec("literal", description="fail (default) or report"),
    },
    description="Check a value against a JSON Schema.",
)
def llm_validate(ctx: OpContext, value: Any) -> OpResult:
    mode = ctx.params.get("mode", "fail")
    if mode not in MODES:
        raise ValueError(f"llm.validate {ctx.node_path}: mode must be one of {list(MODES)}, not {mode!r}")
    errors = [f"{pointer or '/'} {keyword}" for pointer, keyword in validate_value(ctx.params["schema"], value)]
    result = {"ok": not errors, "errors": errors,
              "numbered": "\n".join(f"{i}. {line}" for i, line in enumerate(errors, 1))}
    if errors and mode == "fail":
        raise OpFailed(f"llm.validate {ctx.node_path}: the value does not match the schema: " + "; ".join(errors),
                       code="schema-mismatch", receipt=result)
    return OpResult(out=result, receipt=result)
