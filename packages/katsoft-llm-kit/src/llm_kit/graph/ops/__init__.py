"""Operations: the registry, the ``@op`` decorator and llm-kit's own ops.

A consumer writes ``from llm_kit.graph.ops import op, OpContext`` and decorates
its functions (``registry.py``); ``load_registry(config)`` imports the
configured namespaces by path and adds ``llm.model`` (``model.py``),
``llm.validate`` (``validate.py``) and ``llm.subprocess`` (``subprocess.py``).
See ``docs/reference/llm-kit-graph-ops.md``.

Importing this package imports the registry module only; the model call's heavy
imports (dispatch, adapters) load when ``llm.model`` runs.
"""

from __future__ import annotations

from ..model import OpContext, OpResult
from .registry import OpError, OpFailed, OpHalted, builtin_ops, load_namespace, load_registry, op

__all__ = [
    "OpContext",
    "OpError",
    "OpFailed",
    "OpHalted",
    "OpResult",
    "builtin_ops",
    "load_namespace",
    "load_registry",
    "op",
]
