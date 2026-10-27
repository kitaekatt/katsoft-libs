"""llm.validate."""

from __future__ import annotations

import pytest

from llm_kit.graph.ops import OpFailed
from llm_kit.graph.ops.validate import llm_validate
from llm_kit.graph.loader import resolve_schema
from llm_kit.graph.testing import op_context

SCHEMA = resolve_schema({
    "type": "object", "required": ["title", "count"], "additionalProperties": False,
    "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
}, ".")


def _call(tmp_path, value, **params):
    return llm_validate(op_context(tmp_path, params={"schema": SCHEMA, **params}), value)


def test_a_matching_value_is_ok(tmp_path):
    result = _call(tmp_path, {"title": "a", "count": 1})
    assert result.out == {"ok": True, "errors": [], "numbered": ""}


def test_report_mode_returns_the_faults(tmp_path):
    result = _call(tmp_path, {"title": 3, "extra": 1}, mode="report")
    assert result.out["ok"] is False
    assert result.out["errors"] == ["/count required", "/extra additionalProperties", "/title type"]
    assert result.out["numbered"] == "\n".join(f"{i}. {e}" for i, e in enumerate(result.out["errors"], 1))
    assert result.receipt == result.out


def test_fail_mode_raises_with_the_faults(tmp_path):
    with pytest.raises(OpFailed) as caught:
        _call(tmp_path, {"title": "a"})
    assert caught.value.code == "schema-mismatch"
    assert "/count required" in str(caught.value)
    assert caught.value.receipt["ok"] is False


def test_a_bad_mode_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="mode must be one of"):
        _call(tmp_path, {}, mode="warn")


def test_the_root_pointer_is_a_slash_and_nested_pointers_name_the_location(tmp_path):
    result = _call(tmp_path, {"title": "a", "count": "x"}, mode="report")
    assert result.out["errors"] == ["/count type"]
    result = _call(tmp_path, 5, mode="report")
    assert result.out["errors"] == ["/ type"]
