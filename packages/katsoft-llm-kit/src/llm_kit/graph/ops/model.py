"""``llm.model``: one model call from a prompt template and a selection key.

The node names a selection key of the project's ``.llm-kit/models.yaml``, a
prompt template file the consumer owns and the values for its slots::

    write:
      op: llm.model
      model: beta.story-structure.generate.story-structure
      prompt: ../prompts/story-structure.md
      slots: {shapes: prep.out.shapes, task: prep.out.task}
      suffix: prep.out.declared_inputs
      output_schema: {$ref: ../contracts/plot-answer.v2.schema.json}

The prompt is rendered by :func:`llm_kit.graph.prompt.render_prompt` (strict,
suffix appended as written) and sent with
:func:`llm_kit.dispatch.invoke.invoke`, the engine behind
``llm-kit completion complete``: the same project file, selection, effort menu,
consent and receipts. Node attributes: ``out`` (the validated object when
``output_schema`` is given, else the answer text), ``sent`` (the exact prompt),
``text`` (the raw answer) and ``files`` (the files a workspace call wrote, else
null). The receipt is the result document with the answer text and structured
answer replaced by hashes (the run record keeps ``sent`` and ``text`` as
blobs).

A completed answer is the only success. ``halted`` raises :class:`OpHalted`;
any other failure (a refused request, a provider error, an answer that fails
``output_schema``, a missing expected file) raises :class:`OpFailed` with
llm-kit's own error code, and the exception carries ``sent`` and the raw
``text``.

The backend factory is ``ctx.services["backend_factory"]`` when the runner sets
one, else the one installed by :func:`set_backend_factory` (tests), else
:func:`llm_kit.dispatch.factory.create_backend`.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from ..model import OpContext, OpResult, ParamSpec
from ..prompt import PromptError, render_prompt_file
from .registry import OpFailed, OpHalted, op

__all__ = ["MODES", "llm_model", "set_backend_factory"]

MODES = ("completion", "workspace")

#: Request keys a node parameter passes to the call unchanged (name in the node -> request key).
_PASSED = ("mode", "working_dir", "images", "expect_files", "watch_files", "max_tokens", "timeout_s")

_installed: Optional[Callable[..., Any]] = None


def set_backend_factory(factory: Optional[Callable[..., Any]]) -> Optional[Callable[..., Any]]:
    """Install ``factory`` (or ``None`` to remove it) for every ``llm.model`` call; return the previous one."""
    global _installed
    previous, _installed = _installed, factory
    return previous


def _backend_factory(ctx: OpContext) -> Callable[..., Any]:
    given = ctx.services.get("backend_factory")
    if given is not None:
        if not callable(given):
            raise TypeError("services['backend_factory'] must be callable")
        return given
    if _installed is not None:
        return _installed
    from llm_kit.dispatch import factory  # noqa: PLC0415 -- the adapters load only for a real call

    return lambda *args, **kwargs: factory.create_backend(*args, **kwargs)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    """``value`` as plain JSON containers (the request protocol wants dict and list)."""
    return json.loads(json.dumps(value, allow_nan=False))


def _receipt(document: Mapping[str, Any], sent: str) -> dict:
    receipt = copy.deepcopy(dict(document))
    output = document.get("output")
    if output is not None:
        text = output.get("text") or ""
        receipt["output"] = {"text_sha256": _sha256(text), "text_chars": len(text),
                             "structured": output.get("structured") is not None}
    receipt["sent_sha256"] = _sha256(sent)
    receipt["sent_chars"] = len(sent)
    return receipt


@op(
    "llm.model",
    version="1",
    params={
        "model": ParamSpec("selection", required=True,
                           description="a selection key of the project's .llm-kit/models.yaml"),
        "prompt": ParamSpec("template", required=True, slots="slots",
                            description="the prompt template file (the consumer owns it)"),
        "slots": ParamSpec("exprs", description="slot name -> expression yielding a string"),
        "suffix": ParamSpec("expr", description="a string appended to the filled prompt as written"),
        "output_schema": ParamSpec("schema", description="the answer must be a JSON value that matches it"),
        "effort": ParamSpec("literal", description="an effort of the selection's menu, or null for none"),
        "mode": ParamSpec("literal", description="completion (default) or workspace"),
        "working_dir": ParamSpec("expr", description="workspace: a directory under the project root"),
        "images": ParamSpec("expr", description="a list of relative image files (Codex only)"),
        "expect_files": ParamSpec("expr", description="workspace: files that must exist afterwards"),
        "watch_files": ParamSpec("expr", description="workspace: files the receipt hashes"),
        "max_tokens": ParamSpec("literal", description="output token cap"),
        "timeout_s": ParamSpec("literal", description="wall-clock cap for the call in seconds"),
    },
    attributes=("sent", "text", "files"),
    output_schema_param="output_schema",
    paid=True,
    limit="model-text",
    description="One model call from a prompt template, through llm_kit.dispatch.invoke.",
)
def llm_model(ctx: OpContext) -> OpResult:
    from llm_kit.completion.invoke_protocol import InvokeRefusal, parse_invoke_request  # noqa: PLC0415
    from llm_kit.dispatch.invoke import OUTCOME_COMPLETED, OUTCOME_HALTED, invoke  # noqa: PLC0415

    params = ctx.params
    if "suffix" in params and not isinstance(params["suffix"], str):
        raise PromptError(f"suffix must be a string, not {type(params['suffix']).__name__}", ctx.node_path)
    sent = render_prompt_file(Path(params["prompt"]), params.get("slots") or {}, suffix=params.get("suffix"))
    payload: dict[str, Any] = {"protocol": 2, "project_root": str(ctx.project_root),
                               "selection": params["model"], "prompt": sent}
    for key in _PASSED:
        if key in params:
            payload[key] = _plain(params[key])
    if "effort" in params:
        payload["effort"] = params["effort"]
    if "output_schema" in params:
        payload["output_schema"] = _plain(params["output_schema"])
    where = f"llm.model {ctx.node_path} ({params['model']})"
    try:
        outcome = invoke(parse_invoke_request(payload), default_project_root=str(ctx.project_root),
                         backend_factory=_backend_factory(ctx))
    except InvokeRefusal as exc:
        raise OpFailed(f"{where}: refused ({exc.kind}): {exc}", code=f"refused-{exc.kind}",
                       attributes={"sent": sent}) from None
    document = outcome.document
    receipt = _receipt(document, sent)
    output = document.get("output") or {}
    text = output.get("text") or ""
    attributes = {"sent": sent, "text": text, "files": document.get("files")}
    if outcome.outcome == OUTCOME_HALTED:
        error = document.get("error") or {}
        raise OpHalted(f"{where}: halted: {error.get('message', '')}", halt=document.get("halt") or {},
                       code=error.get("code", "halt"), receipt=receipt, attributes=attributes)
    if outcome.outcome != OUTCOME_COMPLETED:
        error = document.get("error") or {}
        raise OpFailed(f"{where}: {error.get('code', 'execution')}: {error.get('message', '')}",
                       code=error.get("code", "execution"), receipt=receipt, attributes=attributes)
    if "output_schema" in params:
        out = output.get("structured")
        if out is None:
            raise OpFailed(f"{where}: the call completed without a structured answer", code="no-structured-output",
                           receipt=receipt, attributes=attributes)
    else:
        out = text
    return OpResult(out=out, attributes=attributes, receipt=receipt)
