"""llm.model through llm_kit.dispatch.invoke with a scripted MockBackend (no network, no spend)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from llm_kit.graph.loader import resolve_schema
from llm_kit.graph.ops import OpFailed, OpHalted, OpResult
from llm_kit.graph.ops.model import llm_model, set_backend_factory
from llm_kit.graph.prompt import PromptError
from llm_kit.graph.testing import MockBackendFactory, make_project, mock_backend, op_context

SELECTION = "draft.write"
TEMPLATE = """\
A header that is never sent.
---PROMPT---
Write about {{topic}}.
Tone: {{tone}}
"""
SCHEMA = resolve_schema({"type": "object", "required": ["title"], "additionalProperties": False,
                         "properties": {"title": {"type": "string"}}}, ".")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def project(tmp_path):
    project = make_project(tmp_path / "mock", [SELECTION, "draft.other"])
    template = project.root / "prompts" / "write.md"
    template.parent.mkdir()
    template.write_text(TEMPLATE, encoding="utf-8")
    with project.environment():
        yield project


def call(project, factory=None, **params):
    base = {"model": SELECTION, "prompt": str(project.root / "prompts" / "write.md"),
            "slots": {"topic": "tea", "tone": "dry"}}
    services = {"backend_factory": factory} if factory is not None else {}
    ctx = op_context(project.root, params={**base, **params}, services=services, node_path="write")
    return llm_model(ctx)


def test_a_text_answer(project):
    factory = MockBackendFactory(responses=["Tea is hot."])
    result = call(project, factory)
    sent = "Write about tea.\nTone: dry\n"
    assert isinstance(result, OpResult)
    assert result.out == "Tea is hot."
    assert result.attributes == {"sent": sent, "text": "Tea is hot.", "files": None}
    (recorded,) = factory.calls
    assert (recorded["user"], recorded["system"], recorded["model"]) == (sent, "", "mock/model")


def test_the_receipt_is_the_result_document_with_hashes_instead_of_text(project):
    result = call(project, MockBackendFactory(responses=["Tea is hot."], cost_usd=0.25))
    receipt = result.receipt
    sent = "Write about tea.\nTone: dry\n"
    assert receipt["status"] == "completed" and receipt["selection"] == {"id": SELECTION, "model": "mock-row"}
    assert receipt["resolved"]["endpoint"] == "mock" and receipt["resolved"]["requested_model"] == "mock/model"
    assert receipt["output"] == {"text_sha256": sha("Tea is hot."), "text_chars": 11, "structured": False}
    assert receipt["sent_sha256"] == sha(sent) and receipt["sent_chars"] == len(sent)
    assert receipt["cost"]["usd"] == 0.25 and receipt["cost"]["source"] == "provider"
    assert "Tea is hot." not in json.dumps(receipt) and sent not in json.dumps(receipt)
    json.dumps(receipt)  # JSON all the way down


def test_the_suffix_is_appended_to_the_prompt(project):
    factory = MockBackendFactory(responses=["x"])
    result = call(project, factory, suffix="\n## Declared inputs\n{}")
    assert result.attributes["sent"] == "Write about tea.\nTone: dry\n\n## Declared inputs\n{}"
    assert factory.calls[0]["user"] == result.attributes["sent"]


def test_slot_errors_are_raised_before_any_call(project):
    factory = MockBackendFactory(responses=["x"])
    with pytest.raises(PromptError, match=r"slots with no value: \['tone'\]"):
        call(project, factory, slots={"topic": "tea"})
    with pytest.raises(PromptError, match=r"values with no slot: \['extra'\]"):
        call(project, factory, slots={"topic": "a", "tone": "b", "extra": "c"})
    with pytest.raises(PromptError, match="must be a string"):
        call(project, factory, slots={"topic": 3, "tone": "b"})
    with pytest.raises(PromptError, match="suffix must be a string"):
        call(project, factory, suffix=None)
    assert factory.calls == []


def test_a_valid_structured_answer_is_the_out(project):
    factory = MockBackendFactory(responses=['{"title": "Tea"}'])
    result = call(project, factory, output_schema=SCHEMA)
    assert result.out == {"title": "Tea"}
    assert result.attributes["text"] == '{"title": "Tea"}'
    assert result.receipt["output"]["structured"] is True
    (recorded,) = factory.calls
    assert recorded["options"].output_contract.schema["properties"] == SCHEMA["properties"]  # the contract rides on the call
    assert "title" in recorded["system"]  # prompt delivery put the schema instruction in the system text


@pytest.mark.parametrize("answer, detail", [
    ('{"name": "x"}', "schema-mismatch"),
    ("not json at all", "unparseable"),
])
def test_an_answer_that_fails_the_schema_raises_and_keeps_the_text(project, answer, detail):
    with pytest.raises(OpFailed) as caught:
        call(project, MockBackendFactory(responses=[answer]), output_schema=SCHEMA)
    assert caught.value.code == "output-contract-violation" and detail in str(caught.value)
    assert caught.value.attributes["text"] == answer
    assert caught.value.attributes["sent"] == "Write about tea.\nTone: dry\n"
    assert caught.value.receipt["status"] == "failed"


class UsageLimit(RuntimeError):
    halt_kind = "quota"


def test_a_halt_raises_op_halted(project):
    with pytest.raises(OpHalted) as caught:
        call(project, MockBackendFactory(responses=[UsageLimit("limit reached")]))
    assert caught.value.halt == {"kind": "quota"}
    assert caught.value.code == "quota" and caught.value.receipt["halt"] == {"kind": "quota"}
    assert caught.value.attributes["sent"] == "Write about tea.\nTone: dry\n"


def test_any_other_call_failure_raises_op_failed(project):
    with pytest.raises(OpFailed) as caught:
        call(project, MockBackendFactory(responses=[ValueError("provider said no")]))
    assert caught.value.code == "execution" and "provider said no" in str(caught.value)
    with pytest.raises(OpFailed, match="MockBackend exhausted"):
        call(project, MockBackendFactory())


def test_a_refused_request_raises_op_failed_with_the_kind(project):
    factory = MockBackendFactory(responses=["x"])
    with pytest.raises(OpFailed) as caught:
        call(project, factory, model="no.such.selection")
    assert caught.value.code == "refused-selection" and "no.such.selection" in str(caught.value)
    with pytest.raises(OpFailed) as caught:
        call(project, factory, effort="bogus")
    assert caught.value.code == "refused-effort"
    with pytest.raises(OpFailed) as caught:
        call(project, factory, mode="workspace", working_dir=".")
    assert caught.value.code == "refused-mode"
    with pytest.raises(OpFailed) as caught:
        call(project, factory, mode="build")
    assert caught.value.code == "refused-mode"
    with pytest.raises(OpFailed) as caught:
        call(project, factory, images=["a.png"])
    assert caught.value.code == "refused-mode"
    assert factory.calls == []


def test_effort_null_sends_none_and_is_accepted(project):
    result = call(project, MockBackendFactory(responses=["x"]), effort=None)
    assert result.receipt["resolved"]["effort"] == {"requested": None, "sent": None, "source": "request"}


def test_max_tokens_and_timeout_reach_the_options(project):
    factory = MockBackendFactory(responses=["x"])
    call(project, factory, max_tokens=77, timeout_s=9)
    options = factory.calls[0]["options"]
    assert (options.max_tokens, options.timeout_s) == (77, 9.0)


def test_the_mock_adapter_refuses_workspace_calls():
    from llm_kit.completion.types import BackendOptions
    from llm_kit.graph.testing import MockCompletionBackend

    backend = MockCompletionBackend(mock=MockBackendFactory(responses=["x"]).mock, name="openrouter")
    with pytest.raises(NotImplementedError, match="workspace mode is not mocked"):
        backend.complete("", "hi", model="m", options=BackendOptions(cwd=Path(".")))


def test_mock_backend_context_installs_and_restores_the_factory(project):
    assert set_backend_factory(None) is None
    with mock_backend(["from the script"]) as factory:
        assert call(project).out == "from the script"
        assert len(factory.calls) == 1
    assert set_backend_factory(None) is None
    with pytest.raises(Exception):  # the real factory: no key, no network. Nothing was sent.
        call(project)


def test_services_win_over_the_installed_factory(project):
    with mock_backend(["installed"]):
        assert call(project, MockBackendFactory(responses=["from services"])).out == "from services"


def test_keyed_responses_match_on_the_prompt(project):
    factory = MockBackendFactory(keyed_responses={"Tone: dry": "dry answer", "Tone: wet": "wet answer"})
    assert call(project, factory).out == "dry answer"
    assert call(project, factory, slots={"topic": "tea", "tone": "wet"}).out == "wet answer"


def test_the_installed_op_runs_from_the_registry_declaration(project):
    from llm_kit.graph.ops import builtin_ops

    declared = {o.name: o for o in builtin_ops()}["llm.model"]
    ctx = op_context(project.root, params={"model": SELECTION, "prompt": str(project.root / "prompts" / "write.md"),
                                           "slots": {"topic": "a", "tone": "b"}},
                     services={"backend_factory": MockBackendFactory(responses=["ok"])})
    assert declared.fn(ctx).out == "ok"
