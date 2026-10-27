"""Commit nodes: the only place a graph has a side effect (decisions Q13 and Q13').

Nodes never write product state. A ``commit`` node, at the top level of an
outer graph, takes a bundle (an expression, or ``name -> expression``),
checks it against the consumer's contract schema and only then calls the
consumer's commit adapter: an op declared with ``@op(..., commit=True)`` and
exactly one input, ``bundle``. A bundle that fails the contract fails the
node (error code ``contract``) and the adapter is not called. An op node that
names a commit adapter is refused (statically by ``validate``, and again by
the runner). A paid adapter goes through the spend service like any paid op.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from llm_kit.schema.json_schema import validate as validate_value

from .model import GraphError, Op
from .ops.registry import OpFailed

__all__ = ["check_bundle", "contract_errors"]


def contract_errors(contract: Mapping[str, Any], bundle: Any) -> list[str]:
    """The ways ``bundle`` fails ``contract`` (``<pointer> <keyword>``); empty when it passes."""
    return [f"{pointer or '/'} {keyword}" for pointer, keyword in validate_value(contract, bundle)]


def check_bundle(contract: Mapping[str, Any], bundle: Any, adapter: Op, path: str) -> None:
    """Refuse a commit before its adapter runs: the adapter must be a commit adapter and the
    bundle must satisfy the contract (:class:`~llm_kit.graph.ops.OpFailed`, code ``contract``)."""
    if not adapter.commit:
        raise GraphError(f"commit node {path}: {adapter.name} is not a commit adapter (commit=True)")
    errors = contract_errors(contract, bundle)
    if errors:
        raise OpFailed(f"commit node {path}: the bundle does not match the contract; {adapter.name} was not "
                       f"called: {', '.join(errors)}", code="contract")
