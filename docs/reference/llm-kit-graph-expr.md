# llm-kit graph expressions

`llm_kit.graph.expr` is a small, safe expression language. A graph file uses it
for `when` conditions and for `repeat` stop rules. Anything the language cannot
express is an operation (a node), not a bigger language.

The module uses only the Python standard library. A hand-written tokenizer and
parser read the text. The module never calls `eval`, `exec` or `compile`, and
it never reads a Python attribute of a value.

## Interface

```python
from llm_kit.graph.expr import parse, evaluate, references, ExprError

expr = parse("rounds[-1].score >= 8 or len(rounds) >= 5")   # -> Expr
evaluate(expr, {"rounds": [{"score": 9}]})                   # -> True
references(expr)                                             # -> frozenset({"rounds"})
```

- `parse(text) -> Expr`. Raises `ExprError` for any fault in the text.
- `evaluate(expr, scope) -> JSON value`. `scope` is a `Mapping` of names to JSON
  values. Raises `ExprError` for any fault.
- `references(expr) -> frozenset[str]`. The reference paths the expression reads.
- `ExprError` is a `ValueError`. It has `message` and `position` (zero-based
  character offset in the text, or `None` when no text position applies).
  `str(error)` ends with `(at position N)`.
- `ExprMissingError` is a subclass of `ExprError`. It means a reference named
  a key or index that is not in the scope.
- `Expr` is immutable and hashable. `Expr.text` is the source text.

## Values

JSON values only: `null`, booleans, numbers (integer or float), strings, lists,
and objects (any `Mapping` with string keys). There is no object literal. A
scope value of any other type raises `ExprError` when the expression reads it.
Tuples count as lists.

## Grammar

Lowest precedence first.

```
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
           | NAME "(" (expr ("," expr)*)? ")"
           | NAME
           | "(" expr ")"
```

- `NAME` is `[A-Za-z_][A-Za-z0-9_]*`. A name that starts with `_` is a parse error
  (this blocks dunder names). `and or not true false null` are keywords; a
  field after `.` may use any name that does not start with `_`.
- `NUMBER` is digits with an optional fraction and exponent (`3`, `0.5`, `2e3`).
  No sign, no leading zero, no hex, no underscores. A leading `-` is the unary
  operator.
- `STRING` uses `"` or `'`. Escapes: `\\ \" \' \/ \b \f \n \r \t \uXXXX`
  (surrogate pairs combine; a lone surrogate is an error). A raw control
  character in a string is an error.
- Whitespace is space, tab, CR and LF. Comments do not exist.
- Comparisons do not chain: `a < b < c` is an error. Write `a < b and b < c`.

## Semantics

- **No truthiness.** `and`, `or` and `not` need booleans. `and` and `or` stop
  at the first decisive operand, so `exists(x) and x > 1` is safe.
- **Equality** (`==`, `!=`) works on any two JSON values and never raises for
  a type difference: different types are unequal. A boolean is not a number
  (`true != 1`); `1 == 1.0`. Lists and objects compare deeply.
- **Ordering** (`< <= > >=`) needs two numbers or two strings (code point order).
- **`+`** adds two numbers, joins two strings, or joins two lists. **`-`** needs
  two numbers. Unary `-` needs a number.
- **References.** A name reads `scope[name]`. `.field` reads an object key.
  `[expr]` reads a list item (integer; negative counts from the end) or an
  object key (string). `.field` on a list, or an integer index on an object,
  is a type error. There is no attribute access on any value.
- **Missing.** An absent key, or an index out of range, raises
  `ExprMissingError`. Reading a field or index of `null` is also missing.
- **`a ?? b`** gives `a` unless `a` is `null` or evaluating `a` raised
  `ExprMissingError` anywhere inside it; then it gives `b`. The last operand
  is evaluated strictly and returned as is. A type error in `a` still raises.
- **Functions** (arity is checked at parse time; any other name is a parse error):
  - `len(x)`: characters of a string, items of a list, keys of an object.
  - `max(...)`, `min(...)`: one list of numbers, or several numbers. An empty
    list raises.
  - `any(...)`, `all(...)`: one list of booleans, or several booleans.
    `any([])` is `false`; `all([])` is `true`.
  - `exists(ref)`: `true` when the reference resolves to a value that is not
    `null`; `false` when it is missing or `null`. The argument must be a
    reference (`a`, `a.b`, `a[0].c`). A type error still raises.
  - `count(list)`: number of items equal to `true`. `count(list, v)`: number of
    items equal to `v` (same equality as `==`).

## references()

For every reference chain that starts at a scope name, the result holds the
name plus the `.field` steps that follow it directly, up to the first
`[index]` step.

| Expression | Result |
|---|---|
| `a` | `{"a"}` |
| `a.b.c` | `{"a.b.c"}` |
| `a.b[0].c` | `{"a.b"}` |
| `a['k'].z` | `{"a"}` |
| `a[b.c]` | `{"a", "b.c"}` |
| `len(x.items) > 2 and exists(y.out)` | `{"x.items", "y.out"}` |
| `1 + 2`, `len('a.b')` | `{}` |

The result is static: it lists every reference in the text, including those
on the unreached side of `and`, `or` and `??`. Function names, literals and
keywords are not references. A reference inside an index, a call argument, a
list literal or a parenthesised group is listed on its own. The scope name is
the part of a path before the first `.`.

## Limits

Each limit makes a hostile input raise `ExprError` instead of hanging.

| Limit | Value |
|---|---|
| Expression text | 4096 characters |
| Nesting (parentheses, lists, call arguments, indexes, prefix `not` and `-`) | 24 levels |
| Number literal | 32 characters |
| Integer magnitude, literal or computed | 2**63 - 1 |
| Floats | must be finite |
| String built with `+` | 1,000,000 characters |
| List built with `+` | 100,000 items |
| JSON value compared or returned | 64 levels deep |

Long flat chains (`a + b + c + ...`, `and`, `or`, `??`) do not count as
nesting.

## Not in the language

Object literals, `*` `/` `%`, ternary, lambdas, comprehensions, assignment,
method calls, string methods, regular expressions, and any function outside
the list. Put that work in an operation.
