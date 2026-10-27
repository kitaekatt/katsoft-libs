"""Test support: serve ``MockBackend`` scripts through ``llm.model``.

``llm.model`` calls :func:`llm_kit.dispatch.invoke.invoke`, whose backend
factory returns a ``BackendSelection`` (endpoint, model, capabilities and a
backend that speaks :mod:`llm_kit.completion` types). The pipeline's
``MockBackend`` is none of these: it is a bare ``complete`` that returns
``pipeline.runtime.platform.LLMResponse``, ignores an output contract and has
no capabilities. :class:`MockBackendFactory` adapts it:

* it resolves the selection through the real
  :func:`llm_kit.dispatch.factory.create_backend` (so the project model file,
  the endpoint registry, the effort menu and the capability record are the real
  ones) and puts a :class:`MockCompletionBackend` in place of the transport;
* the adapter follows the transport's order: refuse an unsupported contract,
  append the schema instruction to the system text, take the next scripted
  answer, convert it to a ``completion.types.LLMResponse`` and judge it with
  ``finalize_contract`` (a valid answer gets ``structured``; an invalid one
  raises ``OutputContractViolation`` exactly as the real transport does);
* a scripted exception is raised and classified by ``MockBackend.classify_halt``,
  so a halt marker produces a ``halted`` outcome.

Scripts are ``MockBackend``'s: ``responses`` (a queue of text, dict,
``platform.LLMResponse`` or exception entries) or ``keyed_responses``
(substring of the user prompt -> entry). Every call is recorded on
``factory.calls`` (``system``, ``user``, ``model``, ``options``).

Workspace mode is not mocked: the adapter refuses a call that carries a working
directory. Test an operation that builds files by replacing the operation.

::

    project = make_project(tmp_path, ["draft.write"])
    with project.environment(), mock_backend(['{"title": "x"}']) as backend:
        ...  # run a graph; llm.model uses the script
    assert backend.calls[0]["user"] == "..."
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Optional

from .model import OpContext

__all__ = [
    "MockBackendFactory",
    "MockCompletionBackend",
    "MockProject",
    "make_project",
    "mock_backend",
    "op_context",
]

#: ``LLMResponse`` fields copied from a scripted ``platform.LLMResponse``.
_COPIED = (
    "text", "model", "reasoning", "finish_reason", "input_tokens", "output_tokens", "cache_hit_tokens",
    "total_tokens", "wall_ms", "attempts", "from_cache", "dropped_params", "forwarded_params",
    "execution_controls_applied", "structured", "started_at", "ended_at",
    "reported_cost_usd", "reported_cost_source",
)


@dataclasses.dataclass(frozen=True)
class MockCompletionBackend:
    """A ``completion`` backend that answers from a ``MockBackend``. See the module text.

    ``name`` and ``capabilities`` are those of the transport it replaces;
    ``real`` is that transport (used only for its ``effort_plan``); ``cost_usd``
    reports a provider cost on every answer.
    """

    mock: Any
    name: str
    capabilities: Any = None
    real: Any = None
    cost_usd: Optional[float] = None
    text_only: bool = False

    def __getattr__(self, name: str) -> Any:
        if name == "effort_plan" and self.real is not None and hasattr(self.real, "effort_plan"):
            return self.real.effort_plan
        raise AttributeError(name)

    def complete(self, system: str, user: str, *, model: str, options: Any = None) -> Any:
        from llm_kit.completion.contract import finalize_contract, prepare_contract  # noqa: PLC0415
        from llm_kit.completion.types import COMPLETED, COST_SOURCE_PROVIDER, BackendOptions  # noqa: PLC0415
        from llm_kit.completion.types import LLMResponse  # noqa: PLC0415

        opts = options or BackendOptions()
        if opts.cwd is not None:
            raise NotImplementedError("workspace mode is not mocked; replace the operation that builds files")
        plan = prepare_contract(self.capabilities, opts)
        if plan is not None and plan.instruction is not None:
            system = system + plan.instruction
        scripted = self.mock.complete(system, user, model=model, options=opts)
        if getattr(scripted, "status", COMPLETED) != COMPLETED:
            raise ValueError(f"MockBackend script: a scripted response has status {scripted.status!r}; "
                             "script an exception instead")
        values = {name: getattr(scripted, name) for name in _COPIED if hasattr(scripted, name)}
        if self.cost_usd is not None:
            values["reported_cost_usd"], values["reported_cost_source"] = self.cost_usd, COST_SOURCE_PROVIDER
        return finalize_contract(plan, LLMResponse(**values))

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return self.mock.classify_halt(exc)


class MockBackendFactory:
    """A ``backend_factory`` for ``invoke`` that serves a ``MockBackend`` script.

    Resolution is real (:func:`llm_kit.dispatch.factory.create_backend`), the
    transport is the script. ``calls`` lists the recorded calls.
    """

    def __init__(self, responses: Optional[list] = None, keyed_responses: Optional[dict] = None, *,
                 cost_usd: Optional[float] = None) -> None:
        from llm_kit.pipeline.runtime.backends import MockBackend  # noqa: PLC0415

        self.mock = MockBackend(responses=responses, keyed_responses=keyed_responses)
        self.cost_usd = cost_usd

    @property
    def calls(self) -> list:
        return self.mock.calls

    def __call__(self, endpoint: Optional[str] = None, *, model: Optional[str] = None, cheap: bool = False,
                 project_root: Any = None) -> Any:
        from llm_kit.dispatch import factory  # noqa: PLC0415

        real = factory.create_backend(endpoint, model=model, cheap=cheap, project_root=project_root)
        backend = MockCompletionBackend(mock=self.mock, name=real.backend.name, capabilities=real.capabilities,
                                        real=real.backend, cost_usd=self.cost_usd)
        return dataclasses.replace(real, backend=backend)


@contextlib.contextmanager
def mock_backend(responses: Optional[list] = None, keyed_responses: Optional[dict] = None, *,
                 cost_usd: Optional[float] = None) -> Iterator[MockBackendFactory]:
    """Install a :class:`MockBackendFactory` for every ``llm.model`` call in the block."""
    from .ops.model import set_backend_factory  # noqa: PLC0415

    factory = MockBackendFactory(responses, keyed_responses, cost_usd=cost_usd)
    previous = set_backend_factory(factory)
    try:
        yield factory
    finally:
        set_backend_factory(previous)


# ---------------------------------------------------------------------------
# a project with a model file
# ---------------------------------------------------------------------------

_ENDPOINT = "mock"
_ROW = "mock-row"
_WIRE_MODEL = "mock/model"


@dataclasses.dataclass(frozen=True)
class MockProject:
    """A temporary project root whose ``.llm-kit/models.yaml`` names the given selections.

    Every selection uses one catalogue row on one OpenAI-compatible endpoint
    (an invalid host: nothing can be sent). The endpoint registry lives in
    ``endpoints`` and is found through ``MODEL_ENDPOINTS_REGISTRY``; use
    :meth:`environment` (or set ``env``) around the calls.
    """

    root: Path
    endpoints: Path

    @property
    def env(self) -> dict[str, str]:
        return {"MODEL_ENDPOINTS_REGISTRY": str(self.endpoints)}

    @contextlib.contextmanager
    def environment(self) -> Iterator["MockProject"]:
        """Set ``MODEL_ENDPOINTS_REGISTRY`` for the block, then restore it."""
        previous = {name: os.environ.get(name) for name in self.env}
        os.environ.update(self.env)
        try:
            yield self
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def make_project(directory: Path | str, selections: Iterable[str] | Mapping[str, Optional[str]]) -> MockProject:
    """Write a project under ``directory`` and return it.

    ``selections`` is a list of selection keys, or a mapping key -> effort
    (``None`` for no effort). ``directory`` is created; the project root is
    ``directory/project``.
    """
    base = Path(directory)
    root = base / "project"
    (root / ".llm-kit").mkdir(parents=True, exist_ok=True)
    endpoints = base / "endpoints.yaml"
    endpoints.write_text(
        f"default: {_ENDPOINT}\nmodels:\n  {_ENDPOINT}: {{base_url: http://mock.invalid/v1, model: {_WIRE_MODEL}}}\n",
        encoding="utf-8")
    efforts = dict(selections) if isinstance(selections, Mapping) else {key: None for key in selections}
    lines = ["version: 1", "catalog:",
             f"  - {{id: {_ROW}, label: Mock, endpoint: {_ENDPOINT}, model: {_WIRE_MODEL}}}", "selections:"]
    for key, effort in efforts.items():
        body = f"model: {_ROW}" + (f", effort: {json.dumps(effort)}" if effort is not None else "")
        lines.append(f"  {json.dumps(key)}: {{{body}}}")
    (root / ".llm-kit" / "models.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return MockProject(root=root.resolve(), endpoints=endpoints)


def op_context(project_root: Path | str, *, params: Optional[Mapping[str, Any]] = None,
               services: Optional[Mapping[str, Any]] = None, base_dir: Optional[Path | str] = None,
               node_path: str = "node", item: Optional[str] = None, graph_id: str = "test.graph",
               run_id: str = "test-run") -> OpContext:
    """An :class:`OpContext` for calling an operation function directly in a test."""
    root = Path(project_root).resolve()
    return OpContext(run_id=run_id, item=item, graph_id=graph_id, node_path=node_path, project_root=root,
                     base_dir=Path(base_dir).resolve() if base_dir is not None else root,
                     params=dict(params or {}), services=dict(services or {}))
