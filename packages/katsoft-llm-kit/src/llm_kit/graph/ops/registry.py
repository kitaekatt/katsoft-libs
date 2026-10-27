"""The op registry of a graph project: the ``@op`` decorator and ``load_registry``.

A consumer registers operations in a namespace (decision Q27). The project
config (``.llm-kit/graphs.yaml``) maps each namespace to a Python package
directory of the consumer repository::

    namespaces:
      afterplay.beta: pipeline/moments/beta/graph/ops

:func:`load_registry` imports that directory *by path* under a synthetic module
name (so relative imports inside the package work and nothing needs to be on
``sys.path``), imports every public module beneath it, and collects the
functions marked by :func:`op`. Every op of the namespace must be named
``<namespace>.<name>``. The ``llm`` namespace belongs to llm-kit: its ops
(``llm.model``, ``llm.validate``, ``llm.subprocess``) are always registered and
no consumer may define one.

The package directory needs an ``__init__.py``. Every ``*.py`` file below it is
imported (sub-packages need an ``__init__.py``); names that start with ``_``
or ``.`` are private and skipped, but are imported when a public module uses
them. Importing runs the consumer's code. An import error fails the load with
the namespace and the cause.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from llm_kit.schema.json_schema import validate as validate_value

from ..config import GraphConfig
from ..loader import resolve_schema
from ..model import (
    ATTRIBUTE_OUT,
    BUILTIN_OP_NAMESPACE,
    GraphError,
    Op,
    OpRegistry,
    ParamSpec,
    Port,
)

__all__ = [
    "GRAPH_OP_ATTRIBUTE",
    "OpError",
    "OpFailed",
    "OpHalted",
    "builtin_ops",
    "load_namespace",
    "load_registry",
    "op",
]

#: The attribute :func:`op` sets on the decorated function.
GRAPH_OP_ATTRIBUTE = "__graph_op__"

_MODULE_PREFIX = "llm_kit_graph_ns"


# ---------------------------------------------------------------------------
# failures an operation raises
# ---------------------------------------------------------------------------


class OpError(RuntimeError):
    """An operation ran and did not produce a result.

    ``code`` is a short machine token (llm-kit's own error code for a model
    call), ``receipt`` the JSON call receipt when the operation made one, and
    ``attributes`` what it had produced before the failure (a model call keeps
    ``sent`` and the raw ``text`` of an answer that failed its schema), so the
    run record can keep them.
    """

    def __init__(self, message: str, *, code: str = "execution", receipt: Optional[Mapping[str, Any]] = None,
                 attributes: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.receipt = receipt
        self.attributes = dict(attributes or {})


class OpFailed(OpError):
    """The call failed. Run again after a change; retrying the same bytes will not help."""


class OpHalted(OpError):
    """The provider stopped the call for a reason that is not about this call (a usage limit,
    an unreachable endpoint). ``halt`` is llm-kit's halt document (``kind`` and details);
    the run pauses and resumes after the cause is gone."""

    def __init__(self, message: str, *, halt: Mapping[str, Any], code: str = "halt",
                 receipt: Optional[Mapping[str, Any]] = None, attributes: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message, code=code, receipt=receipt, attributes=attributes)
        self.halt = dict(halt)


# ---------------------------------------------------------------------------
# the decorator
# ---------------------------------------------------------------------------


def _port(value: Any, base_dir: Path, where: str) -> Port:
    """A :class:`Port` from a schema, ``{schema, default, description}`` or a Port."""
    if isinstance(value, Port):
        return value
    if not isinstance(value, Mapping):
        raise GraphError("a port is a JSON Schema mapping, {schema, default, description} or a Port", where)
    if "schema" not in value:
        return Port(resolve_schema(value, base_dir, where=where))
    unknown = sorted(set(value) - {"schema", "default", "description"})
    if unknown:
        raise GraphError(f"unknown port keys {unknown}; allowed: ['schema', 'default', 'description']", where)
    schema = resolve_schema(value["schema"], base_dir, where=f"{where}.schema")
    description = value.get("description", "")
    if not isinstance(description, str):
        raise GraphError("description must be a string", where)
    if "default" in value:
        errors = validate_value(schema, value["default"])
        if errors:
            raise GraphError(f"default does not match the schema: {list(errors)}", where)
        return Port(schema, value["default"], description)
    return Port(schema, description=description)


def _check_signature(name: str, fn: Callable[..., Any], inputs: Mapping[str, Port]) -> None:
    params = list(inspect.signature(fn).parameters.values())
    kinds = inspect.Parameter
    if not params or params[0].kind not in (kinds.POSITIONAL_ONLY, kinds.POSITIONAL_OR_KEYWORD):
        raise GraphError(f"op {name}: the function must take the OpContext as its first parameter")
    rest = params[1:]
    if any(p.kind == kinds.VAR_POSITIONAL for p in rest):
        raise GraphError(f"op {name}: the function takes inputs by keyword; remove *{rest[0].name}")
    takes_any = any(p.kind == kinds.VAR_KEYWORD for p in rest)
    named = {p.name for p in rest if p.kind in (kinds.POSITIONAL_OR_KEYWORD, kinds.KEYWORD_ONLY)}
    missing = sorted(set(inputs) - named) if not takes_any else []
    if missing:
        raise GraphError(f"op {name}: the function has no parameter for input(s) {missing}")
    unbound = sorted(p.name for p in rest if p.kind in (kinds.POSITIONAL_OR_KEYWORD, kinds.KEYWORD_ONLY)
                     and p.default is kinds.empty and p.name not in inputs)
    if unbound:
        raise GraphError(f"op {name}: parameter(s) {unbound} have no matching input and no default")


def op(
    name: str,
    *,
    version: str,
    inputs: Optional[Mapping[str, Any]] = None,
    outputs: Optional[Mapping[str, Any]] = None,
    params: Optional[Mapping[str, ParamSpec]] = None,
    attributes: Optional[Any] = None,
    output_schema_param: Optional[str] = None,
    paid: bool = False,
    estimate: Optional[Callable[..., float]] = None,
    limit: Optional[str] = None,
    commit: bool = False,
    description: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare ``fn(ctx: OpContext, **inputs)`` as the operation ``name``.

    ``inputs`` maps an input name to a JSON Schema, a ``{schema, default,
    description}`` mapping or a :class:`~llm_kit.graph.model.Port`. ``outputs``
    is the schema of ``out`` (or ``output_schema_param`` names the ``schema``
    parameter that gives it per node). A relative file ``$ref`` in a schema
    resolves against the directory of the file that defines the function. The
    function is returned unchanged (call it directly in a unit test) with the
    declared :class:`~llm_kit.graph.model.Op` on ``__graph_op__``.

    ``attributes`` are the node attributes besides ``out`` that the function
    returns in ``OpResult.attributes``. ``paid``, ``estimate`` and ``limit`` are
    read by the runtime services; ``commit=True`` marks a commit adapter (one
    input, ``bundle``) that only a ``commit`` node may call.

    The declaration is checked now: a wrong signature, schema or name raises
    :class:`~llm_kit.graph.model.GraphError` at import time.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if hasattr(fn, GRAPH_OP_ATTRIBUTE):
            raise GraphError(f"op {name}: {fn.__name__} is already declared as an op")
        try:
            source = inspect.getsourcefile(fn) or inspect.getfile(fn)
        except TypeError as exc:
            raise GraphError(f"op {name}: cannot find the file that defines {fn!r}: {exc}") from None
        base_dir = Path(source).resolve().parent
        where = f"op {name}"
        ports = {key: _port(value, base_dir, f"{where}: inputs.{key}") for key, value in (inputs or {}).items()}
        out_schema = resolve_schema(outputs, base_dir, where=f"{where}: outputs") if outputs is not None else None
        names = frozenset({ATTRIBUTE_OUT, *(attributes or ())})
        _check_signature(name, fn, ports)
        declared = Op(
            name=name, version=version, fn=fn, inputs=ports, outputs=out_schema, params=dict(params or {}),
            attributes=names, output_schema_param=output_schema_param, paid=paid, estimate=estimate,
            limit=limit, commit=commit, description=description,
        )
        setattr(fn, GRAPH_OP_ATTRIBUTE, declared)
        return fn

    return decorate


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def builtin_ops() -> list[Op]:
    """llm-kit's own operations: ``llm.model``, ``llm.validate``, ``llm.subprocess``."""
    from . import model, subprocess, validate  # noqa: PLC0415 -- the model op's heavy imports stay lazy

    return [getattr(fn, GRAPH_OP_ATTRIBUTE) for fn in (model.llm_model, validate.llm_validate,
                                                       subprocess.llm_subprocess)]


def _module_name(namespace: str, directory: Path) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", namespace).strip("_")
    return f"{_MODULE_PREFIX}_{safe}_{hashlib.sha256(str(directory).encode()).hexdigest()[:8]}"


def _forget(prefix: str) -> None:
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        del sys.modules[name]


def _public_modules(directory: Path) -> list[str]:
    """Dotted names (relative to the package) of the public modules below ``directory``."""
    found = []
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(directory)
        parts = relative.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts or any(part.startswith(("_", ".")) for part in parts):
            continue
        folders = [directory.joinpath(*parts[:i]) for i in range(1, len(parts))]
        if all((folder / "__init__.py").is_file() for folder in folders):
            found.append(".".join(parts))
    return sorted(set(found))


def _collect(namespace: str, module_name: str, where: str) -> list[Op]:
    """The ops defined by the modules loaded under ``module_name``, checked against ``namespace``."""
    found: dict[int, Op] = {}
    for name, module in sorted(sys.modules.items()):
        if name != module_name and not name.startswith(module_name + "."):
            continue
        for value in list(vars(module).values()):
            declared = getattr(value, GRAPH_OP_ATTRIBUTE, None) if callable(value) else None
            if isinstance(declared, Op) and str(getattr(declared.fn, "__module__", "")).startswith(module_name):
                found[id(declared)] = declared
    ops = sorted(found.values(), key=lambda declared: declared.name)
    if not ops:
        raise GraphError(f"{where}: the package defines no operations (decorate functions with llm_kit.graph.ops.op)")
    for declared in ops:
        if not declared.name.startswith(namespace + "."):
            raise GraphError(f"{where}: op {declared.name!r} is not named {namespace}.<name>")
    return ops


def load_namespace(namespace: str, directory: Path | str) -> list[Op]:
    """Import the package ``directory`` as the op namespace and return its ops.

    Every op must be named ``<namespace>.<name>`` and defined in the package.
    A fresh import each call: modules loaded for the namespace earlier are
    dropped first.
    """
    package = Path(directory).resolve()
    where = f"namespace {namespace!r} ({package})"
    init = package / "__init__.py"
    if not package.is_dir():
        raise GraphError(f"{where}: the directory does not exist")
    if not init.is_file():
        raise GraphError(f"{where}: the directory is not a Python package (no __init__.py)")
    module_name = _module_name(namespace, package)
    _forget(module_name)
    try:
        spec = importlib.util.spec_from_file_location(module_name, init, submodule_search_locations=[str(package)])
        if spec is None or spec.loader is None:
            raise GraphError(f"{where}: cannot import the package")
        root = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = root
        spec.loader.exec_module(root)
        for dotted in _public_modules(package):
            importlib.import_module(f"{module_name}.{dotted}")
    except GraphError:
        _forget(module_name)
        raise
    except Exception as exc:
        _forget(module_name)
        raise GraphError(f"{where}: importing the package failed: {type(exc).__name__}: {exc}") from exc
    try:
        return _collect(namespace, module_name, where)
    except GraphError:
        _forget(module_name)
        raise


def load_registry(config: GraphConfig) -> OpRegistry:
    """The :class:`~llm_kit.graph.model.OpRegistry` of a project.

    llm-kit's own ops (:func:`builtin_ops`) plus the ops of every namespace the
    config names, imported by path (:func:`load_namespace`). A name defined
    twice is an error.
    """
    registry = OpRegistry()
    for declared in builtin_ops():
        registry.register(declared)
    for namespace, directory in sorted(config.namespaces.items()):
        if namespace == BUILTIN_OP_NAMESPACE or namespace.startswith(BUILTIN_OP_NAMESPACE + "."):
            raise GraphError(f"namespace {namespace!r} belongs to llm-kit")
        for declared in load_namespace(namespace, directory):
            registry.register(declared)
    return registry
