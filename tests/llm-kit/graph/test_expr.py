"""Tests for the graph expression language (llm_kit.graph.expr)."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_expr():
    """Import llm_kit.graph.expr; load it by path while the package is absent."""
    try:
        return importlib.import_module("llm_kit.graph.expr")
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("llm_kit.graph"):
            raise
    path = (
        Path(__file__).resolve().parents[3]
        / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "graph" / "expr.py"
    )
    name = "_graph_expr_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


expr_mod = _load_expr()
Expr = expr_mod.Expr
ExprError = expr_mod.ExprError
ExprMissingError = expr_mod.ExprMissingError
parse = expr_mod.parse
evaluate = expr_mod.evaluate
references = expr_mod.references

SCOPE = {
    "score": 7,
    "ratio": 0.5,
    "name": "night",
    "ok": True,
    "none": None,
    "items": [3, 1, 2],
    "flags": [True, False, True],
    "obj": {"a": 1, "b": {"c": "deep"}, "list": [10, 20]},
    "rounds": [{"score": 4}, {"score": 9}],
    "empty": [],
}


def ev(text, scope=None):
    return evaluate(parse(text), SCOPE if scope is None else scope)


# ------------------------------------------------------------------ values


@pytest.mark.parametrize(
    "text, expected",
    [
        ("null", None),
        ("true", True),
        ("false", False),
        ("0", 0),
        ("42", 42),
        ("1.5", 1.5),
        ("2e3", 2000.0),
        ("1.5E-2", 0.015),
        ('"a b"', "a b"),
        ("'single'", "single"),
        (r'"q\"uote\n\t\\ A"', 'q"uote\n\t\\ A'),
        (r'"\ud83d\ude00"', "\U0001F600"),
        ("[]", []),
        ("[1, 'a', [true], null,]", [1, "a", [True], None]),
        ("  ( 5 )  ", 5),
        ("-5", -5),
        ("--5", 5),
        ("- 2.5", -2.5),
    ],
)
def test_literals(text, expected):
    result = ev(text, {})
    assert result == expected
    assert type(result) is type(expected)


def test_references_and_indexes():
    assert ev("score") == 7
    assert ev("obj.a") == 1
    assert ev("obj.b.c") == "deep"
    assert ev("obj.list[1]") == 20
    assert ev("obj.list[-1]") == 20
    assert ev("items[0]") == 3
    assert ev("rounds[-1].score") == 9
    assert ev("obj['b']['c']") == "deep"
    assert ev("items[score - 6]") == 1
    assert ev("[10, 20][1]") == 20
    assert ev("(obj).a") == 1
    assert ev("(obj.b).c") == "deep"


def test_tuple_scope_values_and_mapping_scope():
    from types import MappingProxyType

    assert evaluate(parse("a[1]"), MappingProxyType({"a": (1, 2)})) == 2


# --------------------------------------------------------------- operators


def test_comparisons():
    assert ev("score == 7") is True
    assert ev("score != 7") is False
    assert ev("score > 6 and score <= 7") is True
    assert ev("score < 7") is False
    assert ev("score >= 8") is False
    assert ev("1 == 1.0") is True
    assert ev("ratio < 1") is True
    assert ev("name == 'night'") is True
    assert ev("'a' < 'b'") is True
    assert ev("'b' >= 'a'") is True


def test_equality_is_json_equality():
    assert ev("true == 1") is False
    assert ev("false == 0") is False
    assert ev("true != 1") is True
    assert ev("null == false") is False
    assert ev("none == null") is True
    assert ev("1 == '1'") is False
    assert ev("[1, [2]] == [1.0, [2]]") is True
    assert ev("[true] == [1]") is False
    assert ev("obj == obj") is True


def test_object_literals_do_not_exist():
    with pytest.raises(ExprError):
        parse("{}")


def test_logic_is_strict_and_short_circuits():
    assert ev("ok and score > 1") is True
    assert ev("not ok") is False
    assert ev("not not ok") is True
    assert ev("false or ok") is True
    assert ev("false and missing.value") is False   # right side never runs
    assert ev("true or missing.value") is True
    assert ev("exists(missing) and missing > 1") is False
    # precedence: and binds tighter than or; not binds tighter than and
    assert ev("true or false and false") is True
    assert ev("not false and false") is False
    with pytest.raises(ExprError):
        ev("score and ok")
    with pytest.raises(ExprError):
        ev("ok and 1")
    with pytest.raises(ExprError):
        ev("not 1")
    with pytest.raises(ExprError):
        ev("not null")


def test_arithmetic():
    assert ev("1 + 2 - 3 + 10") == 10
    assert ev("score - 10") == -3
    assert ev("1 + 0.5") == 1.5
    assert ev("'a' + 'b'") == "ab"
    assert ev("[1] + [2, 3]") == [1, 2, 3]
    assert ev("-score") == -7
    assert ev("1 - -1") == 2
    assert ev("2 > 1 + 0") is True   # + binds tighter than comparison


def test_coalesce():
    assert ev("missing ?? 5") == 5
    assert ev("none ?? 5") == 5
    assert ev("score ?? 5") == 7
    assert ev("missing ?? none ?? 'x'") == "x"
    assert ev("obj.nope ?? 0") == 0
    assert ev("none.field ?? 'n'") == "n"          # traversal through null is missing
    assert ev("items[10] ?? -1") == -1
    assert ev("(missing + 1) ?? 0") == 0
    assert ev("missing ?? 0 > 3") is False         # ?? binds tighter than comparison
    assert ev("missing ?? 1 + 1") == 2             # + binds tighter than ??
    assert ev("none ?? none") is None              # the last operand is returned as is
    with pytest.raises(ExprMissingError):
        ev("missing ?? other")                     # the last operand is strict
    with pytest.raises(ExprError):
        ev("score.x ?? 1")                         # a type fault is not "missing"


def test_missing_references_raise():
    for text in ("nope", "obj.nope", "items[3]", "items[-4]", "obj['x']", "none.a", "rounds[5].score"):
        with pytest.raises(ExprMissingError):
            ev(text)
    assert issubclass(ExprMissingError, ExprError)


# --------------------------------------------------------------- functions


def test_len():
    assert ev("len(items)") == 3
    assert ev("len(name)") == 5
    assert ev("len(obj)") == 3
    assert ev("len([])") == 0
    for bad in ("len(5)", "len(null)", "len(true)"):
        with pytest.raises(ExprError):
            ev(bad)


def test_max_min():
    assert ev("max(items)") == 3
    assert ev("min(items)") == 1
    assert ev("max(1, 5, 2)") == 5
    assert ev("min(2.5, 1)") == 1
    assert ev("max(rounds[0].score, rounds[1].score)") == 9
    for bad in ("max(empty)", "max(['a'])", "max(true, 1)", "min(null)", "max([true])"):
        with pytest.raises(ExprError):
            ev(bad)


def test_any_all():
    assert ev("any(flags)") is True
    assert ev("all(flags)") is False
    assert ev("any([])") is False
    assert ev("all([])") is True
    assert ev("any(false, ok)") is True
    assert ev("all(ok, score > 1)") is True
    for bad in ("any([1])", "all(['x'])", "any(1)", "all(null)", "any(items)"):
        with pytest.raises(ExprError):
            ev(bad)


def test_count():
    assert ev("count(flags)") == 2                  # elements equal to true
    assert ev("count(items, 1)") == 1
    assert ev("count([1, 1.0, true, 1], 1)") == 3   # true is not 1
    assert ev("count(empty)") == 0
    assert ev("count(['a', 'b', 'a'], 'a')") == 2
    with pytest.raises(ExprError):
        ev("count(5)")
    with pytest.raises(ExprError):
        ev("count(obj)")


def test_exists():
    assert ev("exists(score)") is True
    assert ev("exists(missing)") is False
    assert ev("exists(none)") is False
    assert ev("exists(obj.b.c)") is True
    assert ev("exists(obj.b.zzz)") is False
    assert ev("exists(none.x)") is False
    assert ev("exists(items[2])") is True
    assert ev("exists(items[3])") is False
    assert ev("exists(rounds[-1].score)") is True
    with pytest.raises(ExprError):
        ev("exists(score.x)")        # a type fault still raises
    for bad in ("exists(1)", "exists(a + b)", "exists([a])", "exists(len(a))", "exists(a ?? b)"):
        with pytest.raises(ExprError):
            parse(bad)


# -------------------------------------------------------------- references


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1 + 2", set()),
        ("a", {"a"}),
        ("a.b.c", {"a.b.c"}),
        ("a.b[0].c", {"a.b"}),
        ("a[0]", {"a"}),
        ("a['k'].z", {"a"}),
        ("a[b.c]", {"a", "b.c"}),
        ("a.x[b.y[c]]", {"a.x", "b.y", "c"}),
        ("len(a.list) > 2 and exists(b.out)", {"a.list", "b.out"}),
        ("max(a, 3, b.c) ?? d", {"a", "b.c", "d"}),
        ("[x, y.z][0]", {"x", "y.z"}),
        ("(a.b).c", {"a.b.c"}),
        ("count(r.flags, true) >= n", {"r.flags", "n"}),
        ("a.b or a.b or not a.c", {"a.b", "a.c"}),
        ("len('a.b')", set()),
    ],
)
def test_references(text, expected):
    refs = references(parse(text))
    assert isinstance(refs, frozenset)
    assert refs == expected


def test_references_are_static_and_not_short_circuited():
    assert references(parse("false and hidden.x")) == {"hidden.x"}


# ------------------------------------------------------------ parse errors


@pytest.mark.parametrize(
    "text, position",
    [
        ("", 0),
        ("   ", 3),
        ("1 +", 3),
        ("(1", 2),
        ("1)", 1),
        ("[1, 2", 5),
        ("a.", 2),
        ("a..b", 2),
        ("a[1", 3),
        ("1 2", 2),
        ("a b", 2),
        ("1 < 2 < 3", 6),
        ("a = 1", 2),
        ("a && b", 2),
        ("a || b", 2),
        ("!a", 0),
        ("a ? b", 2),
        ("a @ b", 2),
        ("'abc", 0),
        ('"abc', 0),
        (r'"\q"', 1),
        (r'"\u12"', 1),
        (r'"\ud800"', 7),
        (r'"\udc00"', 7),
        ("'a\nb'", 2),
        ("1e", 0),
        ("01", 0),
        ("1_000", 0),
        ("0x10", 0),
        ("1abc", 0),
        ("1.5.2", 4),
        (".5", 0),
        ("and", 0),
        ("a and", 5),
        ("not", 3),
        ("true.x", None),   # parses; faults at evaluation, not here
    ],
)
def test_parse_errors_carry_position(text, position):
    if position is None:
        parse(text)
        return
    with pytest.raises(ExprError) as info:
        parse(text)
    assert info.value.position == position, str(info.value)
    assert f"position {position}" in str(info.value)
    assert isinstance(info.value, ValueError)


def test_non_string_input():
    for bad in (None, 5, b"1", ["1"]):
        with pytest.raises(ExprError):
            parse(bad)


def test_evaluate_input_checks():
    with pytest.raises(ExprError):
        evaluate("1 + 1", {})
    with pytest.raises(ExprError):
        evaluate(parse("1"), [])
    with pytest.raises(ExprError):
        references("a")


def test_expr_is_immutable_and_hashable():
    e = parse("a + 1")
    with pytest.raises(AttributeError):
        e.text = "b"
    assert e == parse("a + 1")
    assert hash(e) == hash(parse("a + 1"))
    assert e.text == "a + 1"
    assert "a + 1" in repr(e)


# ----------------------------------------------------------- type mismatch


@pytest.mark.parametrize(
    "text",
    [
        "1 + 'a'",
        "'a' + 1",
        "[1] + 1",
        "null + 1",
        "true + true",
        "'a' - 'b'",
        "[1] - [1]",
        "-'a'",
        "-true",
        "-null",
        "-[1]",
        "1 < 'a'",
        "'a' < 1",
        "true < false",
        "null < 1",
        "[1] < [2]",
        "obj < obj",
        "score.x",
        "name.x",
        "items.x",
        "score[0]",
        "name[0]",
        "items['a']",
        "items[true]",
        "items[1.0]",
        "obj[0]",
        "obj[null]",
        "true.x",
        "(1 + 2).x",
    ],
)
def test_type_mismatches_raise(text):
    with pytest.raises(ExprError):
        ev(text)


# --------------------------------------------------------- hostile inputs


@pytest.mark.parametrize(
    "text",
    [
        "__class__",
        "_x",
        "a.__class__",
        "a._b",
        "a.__class__.__mro__",
        "__import__('os')",
        "a.__init__.__globals__",
        "().__class__",
        "[].__class__",
        "'x'.__class__",
        "obj.__dict__",
    ],
)
def test_dunder_and_underscore_names_are_rejected_at_parse(text):
    with pytest.raises(ExprError):
        parse(text)


def test_dunder_string_key_is_only_a_key_lookup():
    # A string index is a mapping key lookup; it never reaches Python attributes.
    assert ev("a['__class__']", {"a": {"__class__": 3}}) == 3
    with pytest.raises(ExprMissingError):
        ev("a['__class__']", {"a": {}})
    with pytest.raises(ExprError):
        ev("a['__class__']", {"a": [1]})
    with pytest.raises(ExprError):
        ev("a['__class__']", {"a": "text"})


@pytest.mark.parametrize(
    "text",
    [
        "foo(1)",
        "eval('1')",
        "exec('1')",
        "open('x')",
        "print(1)",
        "str(1)",
        "int(1)",
        "float(1)",
        "abs(1)",
        "sum([1])",
        "getattr(a, 'b')",
        "Len(a)",
        "LEN(a)",
        "a.len(b)",
        "a.b(c)",
        "a[0](1)",
        "(len)(a)",
        "len(a)(b)",
        "lambda: 1",
        "import os",
        "a if b else c",
        "[x for x in y]",
        "a.b = 1",
        "a := 1",
        "a; b",
        "a, b",
        "`a`",
        "$a",
        "a\\b",
        "\x00",
        "a # comment",
        "caf\u00e9",
        "\u0661",
        "a\u2028b",
    ],
)
def test_unknown_functions_and_foreign_syntax_are_rejected(text):
    with pytest.raises(ExprError):
        parse(text)


@pytest.mark.parametrize(
    "text",
    ["len()", "len(1, 2)", "exists()", "exists(a, b)", "max()", "min()", "any()", "all()",
     "count()", "count(a, b, c)"],
)
def test_arity_is_checked_at_parse_time(text):
    with pytest.raises(ExprError):
        parse(text)


def test_keywords_cannot_be_references():
    for text in ("and", "or", "not", "and.x", "x.and + 1 + or"):
        with pytest.raises(ExprError):
            parse(text)
    # A field named like a keyword is a plain key.
    assert ev("a.null", {"a": {"null": 1}}) == 1


def test_non_json_scope_values_are_rejected():
    class Thing:
        value = 1

    with pytest.raises(ExprError):
        evaluate(parse("t"), {"t": Thing()})
    with pytest.raises(ExprError):
        evaluate(parse("t.value"), {"t": Thing()})   # no attribute access
    with pytest.raises(ExprError):
        evaluate(parse("t[0]"), {"t": {1, 2}})
    with pytest.raises(ExprError):
        evaluate(parse("t"), {"t": {"nested": object()}})
    with pytest.raises(ExprError):
        evaluate(parse("t"), {"t": {1: "non-string key"}})
    with pytest.raises(ExprError):
        evaluate(parse("t"), {"t": float("nan")})
    with pytest.raises(ExprError):
        evaluate(parse("t"), {"t": float("inf")})
    with pytest.raises(ExprError):
        evaluate(parse("t + 1"), {"t": b"bytes"})


def test_object_with_getitem_is_not_a_mapping():
    class Sneaky:
        def __getitem__(self, key):
            return 1

        def __contains__(self, key):
            return True

    with pytest.raises(ExprError):
        evaluate(parse("s.x"), {"s": Sneaky()})
    with pytest.raises(ExprError):
        evaluate(parse("s['x']"), {"s": Sneaky()})


def test_scope_is_not_a_namespace_for_python():
    # Names such as "len" or "True" are scope keys, not Python builtins.
    with pytest.raises(ExprMissingError):
        ev("True")
    with pytest.raises(ExprMissingError):
        ev("len")
    assert ev("len", {"len": 3}) == 3


def test_deep_nesting_raises_not_recurses():
    limit = expr_mod.MAX_NESTING
    assert ev("(" * (limit - 1) + "1" + ")" * (limit - 1), {}) == 1
    with pytest.raises(ExprError):
        parse("(" * limit + "1" + ")" * limit)
    for opener, closer in (("(", ")"), ("[", "]")):
        text = opener * 1000 + "1" + closer * 1000
        with pytest.raises(ExprError) as info:
            parse(text)
        assert "nested" in str(info.value)
    with pytest.raises(ExprError):
        parse("not " * 1000 + "true")
    with pytest.raises(ExprError):
        parse("-" * 1000 + "1")
    with pytest.raises(ExprError):
        parse("len(" * 500 + "1" + ")" * 500)
    with pytest.raises(ExprError):
        parse("a[" * 500 + "1" + "]" * 500)
    # The text bound stops anything bigger before the parser starts.
    with pytest.raises(ExprError):
        parse("(" * 100000 + "1" + ")" * 100000)


def test_long_flat_chains_are_fine():
    n = 200
    assert ev(" + ".join(["1"] * n), {}) == n
    assert ev(" and ".join(["true"] * n), {}) is True
    assert ev(" ?? ".join(["missing"] * (n - 1) + ["7"]), {}) == 7
    assert ev("a" + ".b" * 200, _nested(200)) == 1


def _nested(depth):
    value = 1
    for _ in range(depth):
        value = {"b": value}
    return {"a": value}


def test_deep_scope_values_are_bounded():
    value = []
    for _ in range(expr_mod.MAX_VALUE_DEPTH + 5):
        value = [value]
    with pytest.raises(ExprError):
        evaluate(parse("v"), {"v": value})
    with pytest.raises(ExprError):
        evaluate(parse("v == v"), {"v": value})
    # Self-referential data stops at the depth bound instead of recursing.
    cyc = []
    cyc.append(cyc)
    with pytest.raises(ExprError):
        evaluate(parse("c"), {"c": cyc})
    with pytest.raises(ExprError):
        evaluate(parse("c == c"), {"c": cyc})


def test_text_length_bound():
    parse("1" + " " * (expr_mod.MAX_TEXT_LENGTH - 1))
    with pytest.raises(ExprError):
        parse("1" + " " * expr_mod.MAX_TEXT_LENGTH)


def test_huge_numbers_raise():
    big = expr_mod.MAX_INT
    assert ev(str(big), {}) == big
    for text in (
        str(big + 1),
        "9" * 30,
        "9" * 5000,
        "1" + "0" * 400,
        "1e999",
        "1e99999999999",
        "1" + "0" * 40 + ".5",
        "0." + "0" * 40 + "1",
        "123456789012345678901234567890123.0",
    ):
        with pytest.raises(ExprError):
            parse(text)
    with pytest.raises(ExprError):
        ev(f"{big} + 1", {})
    with pytest.raises(ExprError):
        ev(f"-{big} - 2", {})
    with pytest.raises(ExprError):
        ev("x + x", {"x": big})
    with pytest.raises(ExprError):
        ev("x + 1.0", {"x": 10**400})          # int too large for float
    with pytest.raises(ExprError):
        ev("x + x", {"x": 1.7e308})            # float overflow
    assert ev("1e308 - 1e308", {}) == 0.0


def test_big_collections_are_bounded():
    s = "x" * (expr_mod.MAX_STRING // 2 + 1)
    with pytest.raises(ExprError):
        ev("s + s", {"s": s})
    items = [0] * (expr_mod.MAX_COLLECTION // 2 + 1)
    with pytest.raises(ExprError):
        ev("l + l", {"l": items})


def test_no_python_evaluation_paths():
    source = Path(expr_mod.__file__).read_text(encoding="ascii")
    for banned in ("eval(", "exec(", "compile(", "getattr(", "import ast", "__import__", "pickle"):
        assert banned not in source.replace("``eval``, ``exec`` or ``compile``", ""), banned


def test_scope_is_not_mutated_and_results_are_json():
    scope = {"a": [1, 2], "b": {"c": 1}}
    before = repr(scope)
    result = evaluate(parse("[a, b, a + a]"), scope)
    assert repr(scope) == before
    assert result == [[1, 2], {"c": 1}, [1, 2, 1, 2]]
