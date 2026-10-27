"""Safe expression language for graph conditions and repeat stop rules.

The language reads JSON values from a scope and computes one JSON value.
A hand-written tokenizer and parser build a small tree; a separate evaluator
walks the tree. The module never calls ``eval``, ``exec`` or ``compile`` and
never reads a Python attribute of a value: a reference is a mapping key or a
list index, nothing else. Every error raises ``ExprError``.

Grammar (lowest precedence first)::

    expr      := or
    or        := and ("or" and)*
    and       := not ("and" not)*
    not       := "not" not | compare
    compare   := coalesce (("==" | "!=" | "<" | "<=" | ">" | ">=") coalesce)?
    coalesce  := additive ("??" additive)*
    additive  := unary (("+" | "-") unary)*
    unary     := "-" unary | postfix
    postfix   := primary ("." NAME | "[" expr "]")*
    primary   := NUMBER | STRING | "true" | "false" | "null"
               | "[" (expr ("," expr)* ","?)? "]"
               | NAME "(" (expr ("," expr)* )? ")"      # listed functions only
               | NAME | "(" expr ")"

Functions: ``len max min any all exists count``. The full rules are in
``docs/reference/llm-kit-graph-expr.md``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

__all__ = [
    "Expr",
    "ExprError",
    "ExprMissingError",
    "evaluate",
    "parse",
    "references",
]

# Limits. Each one makes a hostile input fail with ExprError, never hang or
# exhaust the interpreter stack.
MAX_TEXT_LENGTH = 4096        # characters in one expression
MAX_NESTING = 24              # parentheses, lists, call arguments, indexes, prefix operators
MAX_NUMBER_CHARS = 32         # characters in one number literal
MAX_INT = 2**63 - 1           # largest integer magnitude, literal or computed
MAX_COLLECTION = 100_000      # items in a list built by "+"
MAX_STRING = 1_000_000        # characters in a string built by "+"
MAX_VALUE_DEPTH = 64          # nesting of a JSON value that is compared or returned

_KEYWORDS = frozenset({"and", "or", "not", "true", "false", "null"})
_FUNCTIONS = {
    # name: (minimum arguments, maximum arguments or None)
    "len": (1, 1),
    "max": (1, None),
    "min": (1, None),
    "any": (1, None),
    "all": (1, None),
    "exists": (1, 1),
    "count": (1, 2),
}
_COMPARE_OPS = ("==", "!=", "<=", ">=", "<", ">")


class ExprError(ValueError):
    """An expression is invalid or failed to evaluate.

    ``position`` is the zero-based character offset in the expression text.
    """

    def __init__(self, message: str, position: int | None = None) -> None:
        self.message = message
        self.position = position
        text = message if position is None else f"{message} (at position {position})"
        super().__init__(text)


class ExprMissingError(ExprError):
    """A reference named a key or index that is not in the scope."""


# ---------------------------------------------------------------- tokenizer

_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_PART = _NAME_START | frozenset("0123456789")
_DIGITS = frozenset("0123456789")
_SPACE = frozenset(" \t\r\n")
_SIMPLE_ESCAPES = {
    "\\": "\\", '"': '"', "'": "'", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int) -> None:
        self.kind = kind   # "num" "str" "name" "op" "end"
        self.value = value
        self.pos = pos


def _clip(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in _SPACE:
            i += 1
        elif c in _DIGITS:
            tok, i = _read_number(text, i)
            tokens.append(tok)
        elif c == '"' or c == "'":
            tok, i = _read_string(text, i)
            tokens.append(tok)
        elif c in _NAME_START:
            j = i + 1
            while j < n and text[j] in _NAME_PART:
                j += 1
            tokens.append(_Token("name", text[i:j], i))
            i = j
        elif text.startswith("??", i):
            tokens.append(_Token("op", "??", i))
            i += 2
        elif text[i:i + 2] in ("==", "!=", "<=", ">="):
            tokens.append(_Token("op", text[i:i + 2], i))
            i += 2
        elif c in "<>+-()[],.":
            tokens.append(_Token("op", c, i))
            i += 1
        elif c in "&|":
            raise ExprError(f"unknown operator; use 'and'/'or' instead of {c!r}", i)
        elif c == "=":
            raise ExprError("unknown operator '='; use '=='", i)
        elif c == "!":
            raise ExprError("unknown operator '!'; use 'not' or '!='", i)
        elif c == "?":
            raise ExprError("unknown operator '?'; use '??'", i)
        else:
            raise ExprError(f"unexpected character {_clip(c)!r}", i)
    tokens.append(_Token("end", None, n))
    return tokens


def _read_number(text: str, start: int) -> tuple[_Token, int]:
    n = len(text)
    i = start
    while i < n and text[i] in _DIGITS:
        i += 1
    is_float = False
    if i + 1 < n and text[i] == "." and text[i + 1] in _DIGITS:
        is_float = True
        i += 1
        while i < n and text[i] in _DIGITS:
            i += 1
    if i < n and text[i] in "eE":
        j = i + 1
        if j < n and text[j] in "+-":
            j += 1
        if j < n and text[j] in _DIGITS:
            is_float = True
            while j < n and text[j] in _DIGITS:
                j += 1
            i = j
    if i < n and text[i] in _NAME_PART:
        raise ExprError("invalid number literal", start)
    literal = text[start:i]
    if len(literal) > MAX_NUMBER_CHARS:
        raise ExprError(f"number literal longer than {MAX_NUMBER_CHARS} characters", start)
    int_part = literal.split(".")[0].split("e")[0].split("E")[0]
    if len(int_part) > 1 and int_part[0] == "0":
        raise ExprError("number literal has a leading zero", start)
    if is_float:
        value: int | float = float(literal)
        if not math.isfinite(value):
            raise ExprError("number literal is out of range", start)
    else:
        value = int(literal)
        if value > MAX_INT:
            raise ExprError("number literal is out of range", start)
    return _Token("num", value, start), i


def _read_string(text: str, start: int) -> tuple[_Token, int]:
    quote = text[start]
    n = len(text)
    out: list[str] = []
    i = start + 1
    while i < n:
        c = text[i]
        if c == quote:
            return _Token("str", "".join(out), start), i + 1
        if ord(c) < 0x20:
            raise ExprError("control character in string", i)
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        e = text[i + 1]
        if e in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[e])
            i += 2
        elif e == "u":
            code, i = _read_hex4(text, i + 2, i)
            if 0xD800 <= code <= 0xDBFF:
                if text[i:i + 2] != "\\u":
                    raise ExprError("unpaired surrogate in string", i)
                low, i = _read_hex4(text, i + 2, i)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise ExprError("unpaired surrogate in string", i)
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
            elif 0xDC00 <= code <= 0xDFFF:
                raise ExprError("unpaired surrogate in string", i)
            out.append(chr(code))
        else:
            raise ExprError(f"unknown escape '\\{_clip(e, 1)}'", i)
    raise ExprError("unterminated string", start)


def _read_hex4(text: str, i: int, err_pos: int) -> tuple[int, int]:
    digits = text[i:i + 4]
    if len(digits) != 4 or any(d not in "0123456789abcdefABCDEF" for d in digits):
        raise ExprError("invalid \\u escape", err_pos)
    return int(digits, 16), i + 4


# --------------------------------------------------------------------- tree


class _Node:
    __slots__ = ("pos",)

    def __init__(self, pos: int) -> None:
        self.pos = pos


class _Lit(_Node):
    __slots__ = ("value",)

    def __init__(self, pos: int, value: Any) -> None:
        super().__init__(pos)
        self.value = value


class _ListLit(_Node):
    __slots__ = ("items",)

    def __init__(self, pos: int, items: tuple[_Node, ...]) -> None:
        super().__init__(pos)
        self.items = items


class _Path(_Node):
    """A reference: a base (scope name or sub-expression) plus steps.

    A step is ``(pos, field, index_node)``: ``field`` is a name for ``.name``
    and ``None`` for ``[expr]``, whose node is ``index_node``.
    """

    __slots__ = ("base", "steps")

    def __init__(self, pos: int, base: str | _Node, steps: tuple) -> None:
        super().__init__(pos)
        self.base = base
        self.steps = steps


class _Call(_Node):
    __slots__ = ("name", "args")

    def __init__(self, pos: int, name: str, args: tuple[_Node, ...]) -> None:
        super().__init__(pos)
        self.name = name
        self.args = args


class _Unary(_Node):
    __slots__ = ("op", "operand")

    def __init__(self, pos: int, op: str, operand: _Node) -> None:
        super().__init__(pos)
        self.op = op            # "not" or "neg"
        self.operand = operand


class _Compare(_Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, pos: int, op: str, left: _Node, right: _Node) -> None:
        super().__init__(pos)
        self.op = op
        self.left = left
        self.right = right


class _Arith(_Node):
    __slots__ = ("first", "rest")

    def __init__(self, pos: int, first: _Node, rest: tuple) -> None:
        super().__init__(pos)
        self.first = first
        self.rest = rest        # ((op, op_pos, node), ...)


class _Coalesce(_Node):
    __slots__ = ("operands",)

    def __init__(self, pos: int, operands: tuple[_Node, ...]) -> None:
        super().__init__(pos)
        self.operands = operands


class _Logic(_Node):
    __slots__ = ("op", "operands")

    def __init__(self, pos: int, op: str, operands: tuple[_Node, ...]) -> None:
        super().__init__(pos)
        self.op = op            # "and" or "or"
        self.operands = operands


class Expr:
    """A parsed expression. Immutable; build one with ``parse``."""

    __slots__ = ("text", "_root")

    def __init__(self, text: str, root: _Node) -> None:
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "_root", root)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Expr is immutable")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Expr) and other.text == self.text

    def __hash__(self) -> int:
        return hash(self.text)

    def __repr__(self) -> str:
        return f"Expr({_clip(self.text, 80)!r})"

    def __str__(self) -> str:
        return self.text


# ------------------------------------------------------------------- parser


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _tokenize(text)
        self.i = 0
        self.depth = 0

    @property
    def tok(self) -> _Token:
        return self.tokens[self.i]

    def advance(self) -> _Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def is_op(self, value: str) -> bool:
        tok = self.tok
        return tok.kind == "op" and tok.value == value

    def is_word(self, value: str) -> bool:
        tok = self.tok
        return tok.kind == "name" and tok.value == value

    def expect_op(self, value: str) -> _Token:
        if not self.is_op(value):
            raise ExprError(f"expected {value!r}, found {self.describe()}", self.tok.pos)
        return self.advance()

    def describe(self) -> str:
        tok = self.tok
        if tok.kind == "end":
            return "end of expression"
        return repr(_clip(str(tok.value)))

    def enter(self, pos: int) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise ExprError(f"expression nested deeper than {MAX_NESTING} levels", pos)

    def leave(self) -> None:
        self.depth -= 1

    def parse_all(self) -> _Node:
        node = self.parse_or()
        if self.tok.kind != "end":
            raise ExprError(f"unexpected {self.describe()}", self.tok.pos)
        return node

    def parse_or(self) -> _Node:
        start = self.tok.pos
        self.enter(start)
        try:
            first = self.parse_and()
            if not self.is_word("or"):
                return first
            operands = [first]
            while self.is_word("or"):
                self.advance()
                operands.append(self.parse_and())
            return _Logic(start, "or", tuple(operands))
        finally:
            self.leave()

    def parse_and(self) -> _Node:
        start = self.tok.pos
        first = self.parse_not()
        if not self.is_word("and"):
            return first
        operands = [first]
        while self.is_word("and"):
            self.advance()
            operands.append(self.parse_not())
        return _Logic(start, "and", tuple(operands))

    def parse_not(self) -> _Node:
        if self.is_word("not"):
            pos = self.advance().pos
            self.enter(pos)
            try:
                return _Unary(pos, "not", self.parse_not())
            finally:
                self.leave()
        return self.parse_compare()

    def parse_compare(self) -> _Node:
        left = self.parse_coalesce()
        tok = self.tok
        if tok.kind == "op" and tok.value in _COMPARE_OPS:
            self.advance()
            right = self.parse_coalesce()
            node = _Compare(tok.pos, tok.value, left, right)
            nxt = self.tok
            if nxt.kind == "op" and nxt.value in _COMPARE_OPS:
                raise ExprError("comparisons do not chain; combine them with 'and'", nxt.pos)
            return node
        return left

    def parse_coalesce(self) -> _Node:
        start = self.tok.pos
        first = self.parse_additive()
        if not self.is_op("??"):
            return first
        operands = [first]
        while self.is_op("??"):
            self.advance()
            operands.append(self.parse_additive())
        return _Coalesce(start, tuple(operands))

    def parse_additive(self) -> _Node:
        start = self.tok.pos
        first = self.parse_unary()
        rest = []
        while self.is_op("+") or self.is_op("-"):
            op = self.advance()
            rest.append((op.value, op.pos, self.parse_unary()))
        if not rest:
            return first
        return _Arith(start, first, tuple(rest))

    def parse_unary(self) -> _Node:
        if self.is_op("-"):
            pos = self.advance().pos
            self.enter(pos)
            try:
                return _Unary(pos, "neg", self.parse_unary())
            finally:
                self.leave()
        return self.parse_postfix()

    def parse_postfix(self) -> _Node:
        start = self.tok.pos
        base = self.parse_primary()
        steps = []
        while True:
            if self.is_op("."):
                dot = self.advance()
                tok = self.tok
                if tok.kind != "name":
                    raise ExprError("expected a field name after '.'", tok.pos)
                self.advance()
                _check_name(tok.value, tok.pos)
                steps.append((dot.pos, tok.value, None))
            elif self.is_op("["):
                bracket = self.advance()
                index = self.parse_or()
                self.expect_op("]")
                steps.append((bracket.pos, None, index))
            elif self.is_op("("):
                raise ExprError("only the listed functions can be called", self.tok.pos)
            else:
                break
        if not steps:
            return base
        if isinstance(base, _Path) and isinstance(base.base, str):
            # Flatten "name" and its steps into one path.
            return _Path(start, base.base, base.steps + tuple(steps))
        return _Path(start, base, tuple(steps))

    def parse_primary(self) -> _Node:
        tok = self.tok
        if tok.kind == "num" or tok.kind == "str":
            self.advance()
            return _Lit(tok.pos, tok.value)
        if tok.kind == "name":
            word = tok.value
            if word == "true" or word == "false":
                self.advance()
                return _Lit(tok.pos, word == "true")
            if word == "null":
                self.advance()
                return _Lit(tok.pos, None)
            if word in _KEYWORDS:
                raise ExprError(f"unexpected keyword {word!r}", tok.pos)
            self.advance()
            if self.is_op("("):
                return self.parse_call(tok)
            _check_name(word, tok.pos)
            return _Path(tok.pos, word, ())
        if tok.kind == "op" and tok.value == "(":
            self.advance()
            inner = self.parse_or()
            self.expect_op(")")
            return inner
        if tok.kind == "op" and tok.value == "[":
            self.advance()
            items = []
            while not self.is_op("]"):
                items.append(self.parse_or())
                if self.is_op(","):
                    self.advance()
                elif not self.is_op("]"):
                    raise ExprError(f"expected ',' or ']', found {self.describe()}", self.tok.pos)
            self.advance()
            return _ListLit(tok.pos, tuple(items))
        raise ExprError(f"unexpected {self.describe()}", tok.pos)

    def parse_call(self, name_tok: _Token) -> _Node:
        name = name_tok.value
        if name not in _FUNCTIONS:
            raise ExprError(f"unknown function {_clip(name)!r}", name_tok.pos)
        self.expect_op("(")
        args = []
        if not self.is_op(")"):
            while True:
                args.append(self.parse_or())
                if self.is_op(","):
                    self.advance()
                    continue
                break
        self.expect_op(")")
        low, high = _FUNCTIONS[name]
        if len(args) < low or (high is not None and len(args) > high):
            expected = str(low) if low == high else f"{low} to {high}" if high else f"at least {low}"
            raise ExprError(f"{name}() takes {expected} argument(s), got {len(args)}", name_tok.pos)
        if name == "exists":
            arg = args[0]
            if not (isinstance(arg, _Path) and isinstance(arg.base, str)):
                raise ExprError("exists() takes a reference such as a.b or a[0]", arg.pos)
        return _Call(name_tok.pos, name, tuple(args))


def _check_name(name: str, pos: int) -> None:
    if name.startswith("_"):
        raise ExprError(f"name {_clip(name)!r} starts with an underscore", pos)


def parse(text: str) -> Expr:
    """Parse expression text. Raise ``ExprError`` with a position on any fault."""
    if not isinstance(text, str):
        raise ExprError(f"expression must be a string, got {_type_name(text)}")
    if len(text) > MAX_TEXT_LENGTH:
        raise ExprError(f"expression longer than {MAX_TEXT_LENGTH} characters", MAX_TEXT_LENGTH)
    try:
        root = _Parser(text).parse_all()
    except RecursionError as exc:  # backstop; MAX_NESTING should stop first
        raise ExprError("expression is too deeply nested", 0) from exc
    return Expr(text, root)


# --------------------------------------------------------------- references


def references(expr: Expr) -> frozenset[str]:
    """Return the dotted reference paths the expression reads.

    For each reference the result holds the scope name followed by the
    ``.field`` steps that come directly after it, up to the first ``[index]``
    step. ``a.b.c`` gives ``"a.b.c"``; ``a.b[0].c`` gives ``"a.b"``; ``a[i]``
    gives ``"a"``. References inside an index, a call argument or a list are
    collected on their own. Function names and literals are not references.
    """
    _require_expr(expr)
    found: set[str] = set()
    _collect(expr._root, found)
    return frozenset(found)


def _collect(node: _Node, found: set[str]) -> None:
    if isinstance(node, _Lit):
        return
    if isinstance(node, _Path):
        base = node.base
        if isinstance(base, str):
            parts = [base]
            for _pos, field, _index in node.steps:
                if field is None:
                    break
                parts.append(field)
            found.add(".".join(parts))
        else:
            _collect(base, found)
        for _pos, field, index in node.steps:
            if field is None:
                _collect(index, found)
        return
    if isinstance(node, _ListLit):
        for item in node.items:
            _collect(item, found)
    elif isinstance(node, _Call):
        for arg in node.args:
            _collect(arg, found)
    elif isinstance(node, _Unary):
        _collect(node.operand, found)
    elif isinstance(node, _Compare):
        _collect(node.left, found)
        _collect(node.right, found)
    elif isinstance(node, _Arith):
        _collect(node.first, found)
        for _op, _pos, operand in node.rest:
            _collect(operand, found)
    elif isinstance(node, (_Coalesce, _Logic)):
        for operand in node.operands:
            _collect(operand, found)
    else:  # pragma: no cover - the parser builds only the nodes above
        raise ExprError("unknown expression node", node.pos)


# ---------------------------------------------------------------- evaluator


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, Mapping):
        return "object"
    return f"non-JSON {type(value).__name__}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_list(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _check_shallow(value: Any, pos: int | None) -> Any:
    """Reject a scope value that is not a JSON type (one level only)."""
    if value is None or isinstance(value, (bool, int, str, list, tuple, Mapping)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExprError("scope holds a non-finite number", pos)
        return value
    raise ExprError(f"scope holds a {_type_name(value)} value; only JSON values are allowed", pos)


def _check_json(value: Any, pos: int | None, depth: int = 0) -> None:
    """Reject a value that is not JSON all the way down, or is too deep."""
    if depth > MAX_VALUE_DEPTH:
        raise ExprError(f"value nested deeper than {MAX_VALUE_DEPTH} levels", pos)
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_json(item, pos, depth + 1)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExprError("object key is not a string", pos)
            _check_json(item, pos, depth + 1)
    else:
        _check_shallow(value, pos)


def _equal(a: Any, b: Any, pos: int, depth: int = 0) -> bool:
    """JSON equality: booleans are not numbers; 1 equals 1.0."""
    if depth > MAX_VALUE_DEPTH:
        raise ExprError(f"value nested deeper than {MAX_VALUE_DEPTH} levels", pos)
    a_bool = isinstance(a, bool)
    b_bool = isinstance(b, bool)
    if a_bool or b_bool:
        return a_bool and b_bool and a == b
    if _is_number(a) or _is_number(b):
        return _is_number(a) and _is_number(b) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if a is None or b is None:
        return a is None and b is None
    if _is_list(a) or _is_list(b):
        if not (_is_list(a) and _is_list(b)) or len(a) != len(b):
            return False
        return all(_equal(x, y, pos, depth + 1) for x, y in zip(a, b))
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if len(a) != len(b):
            return False
        for key, value in a.items():
            if key not in b or not _equal(value, b[key], pos, depth + 1):
                return False
        return True
    raise ExprError(f"cannot compare {_type_name(a)} and {_type_name(b)}", pos)


def _number_result(value: Any, pos: int) -> int | float:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExprError("number result is out of range", pos)
    elif abs(value) > MAX_INT:
        raise ExprError("number result is out of range", pos)
    return value


def _require_number(value: Any, what: str, pos: int) -> None:
    if not _is_number(value):
        raise ExprError(f"{what} needs a number, got {_type_name(value)}", pos)


def _require_bool(value: Any, what: str, pos: int) -> bool:
    if not isinstance(value, bool):
        raise ExprError(f"{what} needs a boolean, got {_type_name(value)}", pos)
    return value


def _display(path: _Path, upto: int) -> str:
    parts = [path.base if isinstance(path.base, str) else "(...)"]
    for _pos, field, _index in path.steps[:upto]:
        parts.append("." + field if field is not None else "[...]")
    return _clip("".join(parts), 60)


def _resolve(path: _Path, scope: Mapping) -> Any:
    """Read a reference. Raise ``ExprMissingError`` when it names nothing."""
    base = path.base
    if isinstance(base, str):
        if base not in scope:
            raise ExprMissingError(f"missing reference {_clip(base)!r}", path.pos)
        value = _check_shallow(scope[base], path.pos)
    else:
        value = _walk(base, scope)
    for n, (pos, field, index_node) in enumerate(path.steps):
        shown = _display(path, n + 1)
        if value is None:
            raise ExprMissingError(f"missing reference {shown!r} (parent is null)", pos)
        if field is not None:
            if not isinstance(value, Mapping):
                raise ExprError(f"cannot read field {_clip(field)!r} of {_type_name(value)}", pos)
            if field not in value:
                raise ExprMissingError(f"missing reference {shown!r}", pos)
            value = value[field]
        else:
            index = _walk(index_node, scope)
            if _is_list(value):
                if isinstance(index, bool) or not isinstance(index, int):
                    raise ExprError(f"list index needs an integer, got {_type_name(index)}", pos)
                if not -len(value) <= index < len(value):
                    raise ExprMissingError(f"missing reference {shown!r} (index {index} out of range)", pos)
                value = value[index]
            elif isinstance(value, Mapping):
                if not isinstance(index, str):
                    raise ExprError(f"object key needs a string, got {_type_name(index)}", pos)
                if index not in value:
                    raise ExprMissingError(f"missing reference {shown!r} (key {_clip(index)!r})", pos)
                value = value[index]
            else:
                raise ExprError(f"cannot index {_type_name(value)}", pos)
        value = _check_shallow(value, pos)
    return value


def _walk(node: _Node, scope: Mapping) -> Any:
    if isinstance(node, _Lit):
        return node.value
    if isinstance(node, _Path):
        return _resolve(node, scope)
    if isinstance(node, _ListLit):
        return [_walk(item, scope) for item in node.items]
    if isinstance(node, _Call):
        return _call(node, scope)
    if isinstance(node, _Unary):
        value = _walk(node.operand, scope)
        if node.op == "not":
            return not _require_bool(value, "'not'", node.pos)
        _require_number(value, "unary '-'", node.pos)
        return _number_result(-value, node.pos)
    if isinstance(node, _Compare):
        return _compare(node, scope)
    if isinstance(node, _Arith):
        return _arith(node, scope)
    if isinstance(node, _Coalesce):
        last = len(node.operands) - 1
        for n, operand in enumerate(node.operands):
            if n == last:
                return _walk(operand, scope)
            try:
                value = _walk(operand, scope)
            except ExprMissingError:
                continue
            if value is not None:
                return value
        raise ExprError("empty '??' chain", node.pos)  # pragma: no cover
    if isinstance(node, _Logic):
        want = node.op == "or"   # "or" stops at true, "and" stops at false
        for operand in node.operands:
            value = _require_bool(_walk(operand, scope), f"'{node.op}'", operand.pos)
            if value == want:
                return want
        return not want
    raise ExprError("unknown expression node", node.pos)  # pragma: no cover


def _compare(node: _Compare, scope: Mapping) -> bool:
    left = _walk(node.left, scope)
    right = _walk(node.right, scope)
    op = node.op
    if op == "==":
        return _equal(left, right, node.pos)
    if op == "!=":
        return not _equal(left, right, node.pos)
    if _is_number(left) and _is_number(right):
        pass
    elif isinstance(left, str) and isinstance(right, str):
        pass
    else:
        raise ExprError(
            f"cannot order {_type_name(left)} and {_type_name(right)} with {op!r}", node.pos
        )
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right


def _arith(node: _Arith, scope: Mapping) -> Any:
    acc = _walk(node.first, scope)
    for op, pos, operand in node.rest:
        right = _walk(operand, scope)
        if _is_number(acc) and _is_number(right):
            try:
                acc = _number_result(acc + right if op == "+" else acc - right, pos)
            except OverflowError as exc:
                raise ExprError("number result is out of range", pos) from exc
        elif op == "+" and isinstance(acc, str) and isinstance(right, str):
            if len(acc) + len(right) > MAX_STRING:
                raise ExprError(f"string longer than {MAX_STRING} characters", pos)
            acc = acc + right
        elif op == "+" and _is_list(acc) and _is_list(right):
            if len(acc) + len(right) > MAX_COLLECTION:
                raise ExprError(f"list longer than {MAX_COLLECTION} items", pos)
            acc = list(acc) + list(right)
        else:
            raise ExprError(
                f"cannot apply {op!r} to {_type_name(acc)} and {_type_name(right)}", pos
            )
    return acc


def _call(node: _Call, scope: Mapping) -> Any:
    name = node.name
    pos = node.pos
    if name == "exists":
        try:
            return _resolve(node.args[0], scope) is not None
        except ExprMissingError:
            return False
    args = [_walk(arg, scope) for arg in node.args]
    if name == "len":
        value = args[0]
        if isinstance(value, (str, list, tuple, Mapping)):
            return len(value)
        raise ExprError(f"len() needs a string, list or object, got {_type_name(value)}", pos)
    if name == "count":
        items = args[0]
        if not _is_list(items):
            raise ExprError(f"count() needs a list, got {_type_name(items)}", pos)
        target = args[1] if len(args) == 2 else True
        return sum(1 for item in items if _equal(item, target, pos))
    if name in ("max", "min"):
        items = args[0] if len(args) == 1 and _is_list(args[0]) else args
        if not items:
            raise ExprError(f"{name}() of an empty list", pos)
        for item in items:
            _require_number(item, f"{name}()", pos)
        return max(items) if name == "max" else min(items)
    # any / all
    items = args[0] if len(args) == 1 and _is_list(args[0]) else args
    for item in items:
        _require_bool(item, f"{name}()", pos)
    return any(items) if name == "any" else all(items)


def _require_expr(expr: Any) -> None:
    if not isinstance(expr, Expr):
        raise ExprError(f"expected an Expr from parse(), got {_type_name(expr)}")


def evaluate(expr: Expr, scope: Mapping) -> Any:
    """Evaluate a parsed expression against ``scope`` and return a JSON value.

    ``scope`` maps names to JSON values. Any fault raises ``ExprError``; a
    reference to something absent raises its subclass ``ExprMissingError``
    unless ``??`` or ``exists()`` handles it.
    """
    _require_expr(expr)
    if not isinstance(scope, Mapping):
        raise ExprError(f"scope must be a mapping, got {_type_name(scope)}")
    try:
        result = _walk(expr._root, scope)
        _check_json(result, expr._root.pos)
    except RecursionError as exc:
        raise ExprError("value or expression is too deeply nested", 0) from exc
    return result
