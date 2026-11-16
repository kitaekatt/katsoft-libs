"""``llm_kit.runs.compare`` and ``llm-kit runs compare``: compare selections, record every attempt.

The Python tests drive :func:`plan_compare` and :func:`run_compare` with a fake
caller. The CLI tests run the real composition (the project model file, the
pricing table, ``llm_kit.dispatch.invoke`` and the OpenAI-compatible backend)
with a fake client in place of the network. No paid call is made.

What each test pins: record before send, no retry, reuse by the full reuse key
(and the scorer hash), the group budget with failed and pending attempts, the
scorer identity, and a dry run that calls nothing.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from llm_kit import cli
from llm_kit.cli import _runs_compare
from llm_kit.dispatch import factory
from llm_kit.registry import project_models as pm
from llm_kit.runs import compare as cmp
from llm_kit.runs import record as rec
from llm_kit.runs import store as st
from test_cli_harness import home  # noqa: F401 -- home is a fixture

PROMPT = "Notes for people.\n---PROMPT---\nSummarize in one line: {{text}}"
CASES = [cmp.CompareCase("c1", {"text": "The cat sat on the mat."}),
         cmp.CompareCase("c2", {"text": "Rain fell all day."})]
PRICE = cmp.Price(input=1.0, output=4.0)


# --- Python API fixtures ---------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "summary.md").write_text(PROMPT, encoding="utf-8")
    store = st.RunRecordStore.init(tmp_path / "store", accepts=["synthetic", "internal"])
    return types.SimpleNamespace(root=root, prompt=root / "prompts" / "summary.md", store=store)


def candidate(name: str, model: str, **changes: Any) -> cmp.Candidate:
    fields = {"selection": name, "row": model, "model": model, "endpoint": "example", "effort": None,
              "max_output_tokens": 100, "price": PRICE}
    fields.update(changes)
    return cmp.Candidate(**fields)


PAIR = [candidate("a", "example/model-a"), candidate("b", "example/model-b", effort="high")]


class Caller:
    """A fake model: answers ``<model>: <prompt>``; records each call and the events present at that moment."""

    def __init__(self, store: st.RunRecordStore, **result: Any) -> None:
        self.store = store
        self.calls: list[tuple[str, str]] = []
        self.seen: list[list[str]] = []
        self.result = result
        self.by_selection: dict[str, dict] = {}

    def __call__(self, candidate: cmp.Candidate, prompt: str) -> cmp.CallResult:
        self.calls.append((candidate.selection, prompt))
        self.seen.append(sorted(p.name.split(".", 1)[1] for p in self.store.event_files()))
        fields = {"status": "completed", "text": f"{candidate.model}: {prompt}", "input_tokens": 20,
                  "output_tokens": 10, "reported_usd": 0.001, "total_ms": 12.0}
        fields.update(self.result)
        fields.update(self.by_selection.get(candidate.selection, {}))
        return cmp.CallResult(**fields)


def plan(project, **changes: Any) -> cmp.ComparePlan:
    arguments = {"project_root": project.root, "prompt": project.prompt, "cases": CASES, "candidates": PAIR,
                 "flow": "demo.summary", "content": "synthetic", "max_usd": 1.0}
    arguments.update(changes)
    return cmp.plan_compare(project.store, **arguments)


def records(store: st.RunRecordStore) -> list[dict]:
    return list(store.records())


# --- record before send, no retry --------------------------------------------------------------


def test_each_call_writes_its_started_event_before_the_call_and_its_result_after(project):
    caller = Caller(project.store)
    result = cmp.run_compare(plan(project), caller)
    assert result.stopped is None and len(caller.calls) == 4
    # at call n the store holds n-1 finished attempts and exactly one more started event: this one
    for n, seen in enumerate(caller.seen):
        assert seen.count("started.json") == n + 1 and seen.count("result.json") == n
    stored = records(project.store)
    assert [r["status"] for r in stored] == ["ok"] * 4
    first = stored[0]
    assert first["group"]["kind"] == "compare" and first["group"]["id"].startswith("compare-")
    assert first["prompt"]["file"] == "prompts/summary.md"
    assert first["labels"] == {cmp.LABEL_BOUND: repr(cmp.attempt_bound_usd(PAIR[0], caller.calls[0][1]))}
    assert first["cost"] == {"usd": 0.001, "source": "reported"}
    assert project.store.get_blob(first["output"]["text"]).decode() == f"example/model-a: {caller.calls[0][1]}"
    assert caller.calls[0][1] == "Summarize in one line: The cat sat on the mat."
    assert [c[0] for c in caller.calls] == ["a", "b", "a", "b"]  # each case on every selection, case by case


def test_a_failed_call_is_recorded_once_and_the_run_goes_on(project):
    caller = Caller(project.store)
    caller.by_selection["b"] = {"status": "failed", "text": None, "error": {"code": "provider", "message": "502"},
                                "reported_usd": None, "input_tokens": None, "output_tokens": None}
    result = cmp.run_compare(plan(project), caller)
    assert result.stopped is None and [c[0] for c in caller.calls] == ["a", "b", "a", "b"]
    failed = [r for r in records(project.store) if r["status"] == "failed"]
    assert len(failed) == 2 and failed[0]["error"] == {"code": "provider", "message": "502"}
    assert failed[0]["cost"] == {"usd": None, "source": "unknown"}
    assert result.to_dict()["selections"]["b"]["statuses"] == {"failed": 2}


def test_a_halted_call_is_recorded_and_stops_the_run(project):
    caller = Caller(project.store, status="halted", text=None, error={"code": "insufficient_credit", "message": "no"})
    result = cmp.run_compare(plan(project), caller)
    assert result.stopped == "halted" and len(caller.calls) == 1
    assert [o.action for o in result.outcomes] == ["call", "not-run", "not-run", "not-run"]
    assert [r["status"] for r in records(project.store)] == ["failed"]


def test_an_exception_from_the_caller_is_recorded_as_failed_and_raised(project):
    def broken(candidate, prompt):
        raise RuntimeError("socket closed")

    with pytest.raises(RuntimeError, match="socket closed"):
        cmp.run_compare(plan(project), broken)
    (only,) = records(project.store)
    assert only["status"] == "failed" and only["error"]["code"] == "caller-error" and only["cost"] is None


# --- reuse -------------------------------------------------------------------------------------


def test_a_second_run_reuses_every_ok_record_and_calls_nothing(project):
    cmp.run_compare(plan(project), Caller(project.store))
    again = plan(project)
    assert [a.action for a in again.attempts] == ["done"] * 4 and again.bound_usd == 0
    caller = Caller(project.store)
    result = cmp.run_compare(again, caller)
    assert caller.calls == [] and len(records(project.store)) == 4
    assert result.to_dict()["cases"][0]["answers"]["a"]["text"].startswith("example/model-a: ")


def test_another_group_reuses_the_answer_with_no_call(project):
    first = cmp.run_compare(plan(project), Caller(project.store))
    other = plan(project, group_id="second-look")
    assert [a.action for a in other.attempts] == ["copy"] * 4
    caller = Caller(project.store)
    result = cmp.run_compare(other, caller)
    assert caller.calls == [] and result.spent_usd == 0
    copies = [r for r in records(project.store) if r["group"]["id"] == "second-look"]
    originals = {o.record["id"] for o in first.outcomes}
    assert len(copies) == 4 and {c["labels"][cmp.LABEL_REUSED_FROM] for c in copies} == originals
    assert all(c["cost"] is None and c["usage"] is None and c["status"] == "ok" for c in copies)
    assert plan(project, group_id="second-look").spent_usd == 0  # a copy costs the group nothing
    # a copy of a copy names the original
    third = plan(project, group_id="third-look")
    assert {a.record_id for a in third.attempts} == originals


def _change_prompt(project):
    project.prompt.write_text(PROMPT + " Be brief.", encoding="utf-8")
    return {}


@pytest.mark.parametrize("change", [
    pytest.param(lambda p: {"flow": "demo.other"}, id="flow"),
    pytest.param(_change_prompt, id="prompt-body"),
    pytest.param(lambda p: {"cases": [CASES[0], cmp.CompareCase("c2", {"text": "Snow."})]}, id="case-input"),
    pytest.param(lambda p: {"candidates": [PAIR[0], dataclasses.replace(PAIR[1], model="example/model-c")]},
                 id="model"),
    pytest.param(lambda p: {"candidates": [PAIR[0], dataclasses.replace(PAIR[1], effort="low")]}, id="effort"),
    pytest.param(lambda p: {"candidates": [PAIR[0], dataclasses.replace(PAIR[1], max_output_tokens=50)]},
                 id="output-cap"),
    pytest.param(lambda p: {"candidates": [PAIR[0], dataclasses.replace(PAIR[1], extras={"top_p": 0.5})]},
                 id="request-extras"),
])
def test_a_change_to_an_input_of_the_reuse_key_calls_again(project, change):
    cmp.run_compare(plan(project, group_id="g"), Caller(project.store))
    again = plan(project, group_id="g", **change(project))
    assert "call" in [a.action for a in again.attempts]
    caller = Caller(project.store)
    cmp.run_compare(again, caller)
    assert len(caller.calls) == sum(a.action == "call" for a in again.attempts) > 0


def test_two_selections_that_ask_the_same_question_are_refused(project):
    twin = dataclasses.replace(PAIR[0], selection="a2")
    with pytest.raises(cmp.CompareError) as caught:
        plan(project, candidates=[PAIR[0], twin])
    assert caught.value.code == "invalid" and "same question" in str(caught.value)


# --- scorers -----------------------------------------------------------------------------------


def length_scorer(sha: str, *, note: bool = False) -> cmp.Scorer:
    def score(line: dict) -> dict:
        answer = line["blobs"][line["record"]["output"]["text"]]["text"]
        entry = {"name": "chars", "value": len(answer), "scale": None,
                 "by": {"kind": "rule", "name": "length", "selection": None}, "note": None}
        if note:
            entry["note"] = rec.blob_ref("long enough")
            line["blobs"][entry["note"]] = {"text": "long enough"}
        line["record"]["scores"] = [entry]
        return line

    return cmp.Scorer(name="length", sha256=sha, score=score, version="1")


def test_the_scorer_identity_is_in_provenance_and_a_new_scorer_rescores_with_no_call(project):
    cmp.run_compare(plan(project, group_id="g", scorer=length_scorer("a" * 64, note=True)), Caller(project.store))
    scored = records(project.store)
    assert scored[0]["provenance"]["scorer"] == {"name": "length", "version": "1", "sha256": "a" * 64}
    assert scored[0]["scores"][0]["value"] == len(project.store.get_blob(scored[0]["output"]["text"]))
    assert project.store.get_blob(scored[0]["scores"][0]["note"]) == b"long enough"
    same = plan(project, group_id="g", scorer=length_scorer("a" * 64))
    assert [a.action for a in same.attempts] == ["done"] * 4
    newer = plan(project, group_id="g", scorer=length_scorer("b" * 64))
    assert [a.action for a in newer.attempts] == ["copy"] * 4
    caller = Caller(project.store)
    result = cmp.run_compare(newer, caller)
    assert caller.calls == [] and result.to_dict()["selections"]["a"]["scores"]["chars"] > 0
    assert {r["provenance"]["scorer"]["sha256"] for r in records(project.store)} == {"a" * 64, "b" * 64}


def test_a_scorer_failure_records_the_answer_as_invalid_and_raises(project):
    def bad(line):
        line["record"]["flow"] = "changed"
        return line

    scorer = cmp.Scorer(name="bad", sha256="c" * 64, score=bad)
    caller = Caller(project.store)
    with pytest.raises(cmp.CompareError) as caught:
        cmp.run_compare(plan(project, scorer=scorer), caller)
    assert caught.value.code == "scorer-failed" and caught.value.result.stopped == "scorer-failed"
    assert len(caller.calls) == 1
    (only,) = records(project.store)
    assert only["status"] == "invalid" and only["error"]["code"] == "scorer-failed" and only["output"] is not None


def test_a_command_scorer_runs_a_program_and_hashes_its_file(project, tmp_path):
    script = tmp_path / "score.py"
    script.write_text(
        "import json, sys\n"
        "line = json.loads(sys.stdin.read())\n"
        "text = line['blobs'][line['record']['output']['text']]['text']\n"
        "line['record']['scores'] = [{'name': 'words', 'value': len(text.split()), 'scale': None,\n"
        "    'by': {'kind': 'rule', 'name': 'words', 'selection': None}, 'note': None}]\n"
        "print(json.dumps(line))\n", encoding="utf-8")
    scorer = cmp.command_scorer([sys.executable, str(script)], version="1")
    assert scorer.name == "score.py"
    cmp.run_compare(plan(project, scorer=scorer), Caller(project.store))
    assert all(r["scores"][0]["name"] == "words" and r["scores"][0]["value"] > 3 for r in records(project.store))
    script.write_text(script.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    assert cmp.command_scorer([sys.executable, str(script)]).sha256 != scorer.sha256
    failing = tmp_path / "fail.py"
    failing.write_text("import sys\nsys.exit('no score')\n", encoding="utf-8")
    with pytest.raises(cmp.CompareError) as caught:
        cmp.command_scorer([sys.executable, str(failing)]).score({})
    assert caught.value.code == "scorer-failed" and "no score" in str(caught.value)


# --- the budget --------------------------------------------------------------------------------


def test_the_bound_is_the_prompt_bytes_and_the_output_cap_at_the_price():
    prompt = "x" * 936
    assert cmp.attempt_bound_usd(candidate("a", "m"), prompt) == pytest.approx(
        ((936 + cmp.INPUT_OVERHEAD_TOKENS) * 1.0 + 100 * 4.0) / 1e6)
    windowed = candidate("a", "m", max_output_tokens=None, price=cmp.Price(1.0, 4.0, context_window=500))
    assert cmp.attempt_bound_usd(windowed, prompt) == pytest.approx((1000 * 1.0 + 500 * 4.0) / 1e6)
    assert cmp.attempt_bound_usd(candidate("a", "m", price=cmp.Price(0, 0), prompt_only=False), prompt) == 0
    for unbounded in (candidate("a", "m", price=None), candidate("a", "m", prompt_only=False),
                      candidate("a", "m", max_output_tokens=None)):
        with pytest.raises(cmp.CompareError) as caught:
            cmp.attempt_bound_usd(unbounded, prompt)
        assert caught.value.code == "unpriced"


def test_the_budget_refuses_an_attempt_whose_bound_would_cross_it(project):
    first = plan(project)
    bound = first.attempts[0].bound_usd
    tight = plan(project, max_usd=bound * 2.5)
    caller = Caller(project.store, reported_usd=None, input_tokens=None, output_tokens=None)
    result = cmp.run_compare(tight, caller)
    assert result.stopped == "budget" and len(caller.calls) == 2  # each unknown cost counts at its bound
    assert len(list(project.store.event_files())) == 4  # two started and two results; nothing for the rest
    assert [o.action for o in result.outcomes][2:] == ["not-run", "not-run"]


def test_failed_and_pending_attempts_of_the_group_count_against_the_budget(project):
    base = plan(project, group_id="g")
    attempt = base.attempts[0]
    pending = rec.start_record(**attempt.fields, labels={cmp.LABEL_BOUND: "0.25"})
    project.store.put(pending, {attempt.fields["case"]["input"]: '{"text":"The cat sat on the mat."}'})
    failed = rec.finish_record(rec.start_record(**attempt.fields, labels={cmp.LABEL_BOUND: "0.5"}),
                               status="failed", error={"code": "provider"}, cost={"usd": None, "source": "unknown"})
    project.store.put(failed)
    paid = rec.finish_record(rec.start_record(**attempt.fields, labels={cmp.LABEL_BOUND: "0.5"}),
                             status="failed", error={"code": "provider"}, cost={"usd": 0.125, "source": "reported"})
    project.store.put(paid)
    again = plan(project, group_id="g", max_usd=0.875)
    assert again.spent_usd == pytest.approx(0.25 + 0.5 + 0.125)
    caller = Caller(project.store)
    result = cmp.run_compare(again, caller)
    assert result.stopped == "budget" and caller.calls == []
    unbudgeted = rec.start_record(**attempt.fields)
    project.store.put(unbudgeted)
    with pytest.raises(cmp.CompareError) as caught:
        plan(project, group_id="g")
    assert caught.value.code == "conflict"


def test_the_cost_is_reported_else_the_larger_estimate_else_unknown():
    paid = candidate("a", "m")
    tokens = {"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 400}
    priced = (600 * 1.0 + 400 * 1.0 + 100 * 4.0) / 1e6
    assert cmp._cost(paid, cmp.CallResult("completed", "x", reported_usd=0.5, **tokens)) == \
        {"usd": 0.5, "source": "reported"}
    assert cmp._cost(paid, cmp.CallResult("completed", "x", estimated_usd=0.0001, **tokens)) == \
        {"usd": pytest.approx(priced), "source": "estimated"}
    assert cmp._cost(paid, cmp.CallResult("completed", "x", estimated_usd=0.5, **tokens))["usd"] == 0.5
    assert cmp._cost(candidate("a", "m", price=cmp.Price(0, 0)),
                     cmp.CallResult("completed", "x", estimated_usd=0.5)) == {"usd": 0.0, "source": "estimated"}
    assert cmp._cost(paid, cmp.CallResult("completed", "x")) == {"usd": None, "source": "unknown"}


# --- plan refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize("changes, code", [
    ({"content": "sensitive"}, "policy"),
    ({"content": "secret"}, "invalid"),
    ({"candidates": PAIR[:1]}, "invalid"),
    ({"candidates": [PAIR[0], dataclasses.replace(PAIR[1], selection="a")]}, "invalid"),
    ({"candidates": [PAIR[0], dataclasses.replace(PAIR[1], price=None)]}, "unpriced"),
    ({"cases": [cmp.CompareCase("c1", {"words": "x"})]}, "invalid"),
    ({"cases": [CASES[0], CASES[0]]}, "invalid"),
    ({"max_usd": -1}, "invalid"),
    ({"max_usd": float("nan")}, "invalid"),
    ({"flow": " "}, "invalid"),
])
def test_the_plan_refuses_before_any_write(project, changes, code):
    with pytest.raises(cmp.CompareError) as caught:
        plan(project, **changes)
    assert caught.value.code == code
    assert list(project.store.event_files()) == []


def test_a_prompt_outside_the_project_is_refused(project, tmp_path):
    outside = tmp_path / "prompt.md"
    outside.write_text("{{text}}", encoding="utf-8")
    with pytest.raises(cmp.CompareError, match="not under the project root"):
        plan(project, prompt=outside)


def test_read_cases_reads_json_lines_and_refuses_bad_lines(tmp_path):
    good = b'{"id": "c1", "slots": {"text": "a"}}\r\n{"id": "c2", "slots": {}}\n'
    assert cmp.read_cases(good) == [cmp.CompareCase("c1", {"text": "a"}), cmp.CompareCase("c2", {})]
    path = tmp_path / "cases.jsonl"
    path.write_bytes(good)
    assert len(cmp.read_cases(path)) == 2
    for bad in (b"", b"\n", b'{"id": "c1", "slots": {}}\n\n{"id": "c2", "slots": {}}\n', b"not json\n",
                b'{"id": "c1", "slots": {"text": 1}}\n', b'{"id": "c1", "slots": {}}\n{"id": "c1", "slots": {}}\n',
                b"\xff\n"):
        with pytest.raises(cmp.CompareError) as caught:
            cmp.read_cases(bad)
        assert caught.value.code == "invalid"


def test_the_inputs_refuse_bad_values():
    with pytest.raises(cmp.CompareError):
        cmp.Price(-1, 0)
    with pytest.raises(cmp.CompareError):
        cmp.Price(1, 1, context_window=0)
    with pytest.raises(cmp.CompareError):
        cmp.Price.from_entry({"input": 1, "output": 1, "per_call": 2})
    assert cmp.Price.from_entry({"input": 1, "output": 2, "alias": "m"}) == cmp.Price(1, 2)
    for bad in ({"status": "done"}, {"status": "completed"}, {"status": "failed", "text": "x"}):
        with pytest.raises(cmp.CompareError):
            cmp.CallResult(**bad)
    with pytest.raises(cmp.CompareError):
        cmp.command_scorer([])


# --- the CLI: the project model file and the dispatch caller -------------------------------------

_MODELS = {
    "version": 1,
    "catalog": [
        {"id": "flash", "label": "Flash", "endpoint": "openrouter", "model": "deepseek/deepseek-v4.1-flash",
         "efforts": ["none", "low", "high", "max"], "max_output_tokens": 200},
        {"id": "other", "label": "Other", "endpoint": "openrouter", "model": "example/model-b",
         "request_defaults": {"extras": {"provider": {"sort": "latency"}}}},
        {"id": "luna", "label": "Luna", "endpoint": "luna", "model": "gpt-6-luna"},
    ],
    "selections": {"a": {"model": "flash", "effort": "high"}, "b": {"model": "other"}, "h": {"model": "luna"}},
}


class Wire:
    """A fake OpenAI-compatible client: records each request and each ``with_options`` call."""

    def __init__(self, store_root: Path) -> None:
        self.requests: list[dict] = []
        self.options: list[dict] = []
        self.store_root = store_root
        self.started_at_send: list[int] = []

    def client(self) -> Any:
        wire = self

        def create(**kwargs: Any) -> Any:
            wire.requests.append(kwargs)
            wire.started_at_send.append(len(list((wire.store_root / "records").glob("*/*.started.json"))))
            usage = types.SimpleNamespace(prompt_tokens=30, completion_tokens=8, cost=0.0002)
            message = types.SimpleNamespace(content=f"answer of {kwargs['model']}", reasoning_content=None)
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            return types.SimpleNamespace(choices=[choice], usage=usage, model=kwargs["model"])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        def with_options(**kwargs: Any) -> Any:
            wire.options.append(kwargs)
            return client

        client.with_options = with_options
        return client


@pytest.fixture()
def cli_project(home, tmp_path, monkeypatch):  # noqa: F811
    root = tmp_path / "proj"
    (root / "prompts").mkdir(parents=True)
    pm.write_project_models(_MODELS, project_root=str(root))
    (root / "prompts" / "summary.md").write_text(PROMPT, encoding="utf-8")
    (root / "cases.jsonl").write_text("".join(json.dumps({"id": c.id, "slots": dict(c.slots)}) + "\n"
                                              for c in CASES), encoding="utf-8")
    (root / "pricing.json").write_text(json.dumps({
        "deepseek/deepseek-v4.1-flash": {"input": 0.3, "output": 1.2},
        "example/model-b": {"input": 1.0, "output": 2.0, "context_window": 4000},
    }), encoding="utf-8")
    store = st.RunRecordStore.init(tmp_path / "store", accepts=["synthetic", "internal"])
    wire = Wire(store.root)
    real = factory.create_backend

    def build(endpoint=None, **kwargs):
        selection = real(endpoint, **kwargs)
        if selection.backend.name != "openai-compat":  # pragma: no cover - only transport rows are called here
            raise AssertionError(f"unexpected backend {selection.backend.name}")
        return dataclasses.replace(selection, backend=dataclasses.replace(selection.backend, client=wire.client()))

    monkeypatch.setattr(factory, "create_backend", build)
    monkeypatch.chdir(root)
    return types.SimpleNamespace(root=root, store=store, wire=wire)


def compare_cli(capsys, project, *extra: str) -> tuple[int, Any, Any]:
    argv = ["runs", "compare", "--store", str(project.store.root), "--prompt", "prompts/summary.md",
            "--cases", "cases.jsonl", "--select", "a,b", "--flow", "demo.summary", "--content", "synthetic",
            "--max-usd", "1", "--pricing", "pricing.json", *extra]
    capsys.readouterr()
    code = cli.main(argv)
    out = capsys.readouterr()
    return (code, json.loads(out.out) if out.out.strip() else None,
            json.loads(out.err) if out.err.strip() else None)


def test_the_cli_compares_selections_of_the_project_model_file_and_sends_each_once(cli_project, capsys):
    code, out, err = compare_cli(capsys, cli_project)
    assert code == 0 and err is None, err
    wire = cli_project.wire
    assert len(wire.requests) == 4 and wire.options == [{"max_retries": 0}] * 4
    assert wire.started_at_send == [1, 2, 3, 4]  # each started event is on disk before its send
    assert [r["model"] for r in wire.requests] == ["deepseek/deepseek-v4.1-flash", "example/model-b"] * 2
    assert wire.requests[0]["max_tokens"] == 200 and "max_tokens" not in wire.requests[1]
    by_selection = {r["selection"]["name"]: r for r in cli_project.store.records()}
    flash, other = by_selection["a"], by_selection["b"]
    assert flash["resolved"] == {"row": "flash", "endpoint": "openrouter", "model": "deepseek/deepseek-v4.1-flash",
                                 "effort": "high", "registry_sha256": flash["resolved"]["registry_sha256"]}
    assert flash["request"]["max_output_tokens"] == 200 and other["request"]["max_output_tokens"] is None
    assert other["request"]["extras"] == {"provider": {"sort": "latency"}}
    assert flash["cost"] == {"usd": 0.0002, "source": "reported"}  # openrouter's usage.cost is the charge
    assert flash["provenance"]["tool"]["name"] == "llm-kit runs compare"
    assert out["stopped"] is None and out["cases"][0]["answers"]["b"]["text"] == "answer of example/model-b"
    code, out, _ = compare_cli(capsys, cli_project)
    assert code == 0 and len(wire.requests) == 4  # the second run reuses every answer


def test_the_cli_dry_run_prints_the_plan_and_calls_and_writes_nothing(cli_project, capsys):
    code, out, err = compare_cli(capsys, cli_project, "--dry-run", "--max-output-tokens", "50")
    assert code == 0 and err is None and out["dry_run"] is True
    assert [a["action"] for a in out["attempts"]] == ["call"] * 4
    assert out["bound_usd"] > 0 and out["fits"] is True and out["spent_usd"] == 0
    assert cli_project.wire.requests == [] and list(cli_project.store.event_files()) == []


def test_the_cli_refuses_an_unpriced_or_unbounded_selection_before_any_write(cli_project, capsys):
    code, _, err = compare_cli(capsys, cli_project, "--select", "h")
    assert code == 1 and err["error"]["code"] == "unpriced" and "no price" in err["error"]["message"]
    pricing = json.loads((cli_project.root / "pricing.json").read_text(encoding="utf-8"))
    (cli_project.root / "pricing.json").write_text(json.dumps({**pricing, "gpt-6-luna": {"input": 1, "output": 1}}),
                                                   encoding="utf-8")
    code, _, err = compare_cli(capsys, cli_project, "--select", "h")
    assert code == 1 and err["error"]["code"] == "unpriced" and "harness" in err["error"]["message"]
    (cli_project.root / "pricing.json").write_text(json.dumps({"example/model-b": {"input": 1, "output": 2}}),
                                                   encoding="utf-8")
    code, _, err = compare_cli(capsys, cli_project)
    assert code == 1 and err["error"]["code"] == "unpriced" and "'a'" in err["error"]["message"]
    code, _, err = compare_cli(capsys, cli_project, "--select", "nothing")
    assert code == 1 and err["error"]["code"] == "invalid"
    assert cli_project.wire.requests == [] and list(cli_project.store.event_files()) == []


def test_the_cli_loads_a_file_with_an_unknown_endpoint_and_refuses_only_its_selection(cli_project, capsys):
    models = {**_MODELS, "catalog": [*_MODELS["catalog"], {"id": "far", "label": "Far", "endpoint": "nowhere",
                                                          "model": "example/far"}],
              "selections": {**_MODELS["selections"], "f": {"model": "far"}}}
    pm.write_project_models(models, project_root=str(cli_project.root))
    code, out, err = compare_cli(capsys, cli_project, "--dry-run")
    assert code == 0 and err is None and out["dry_run"] is True
    code, _, err = compare_cli(capsys, cli_project, "--select", "a,f", "--dry-run")
    assert code == 1 and err["error"]["code"] == "invalid"
    assert "'f'" in err["error"]["message"] and "'nowhere'" in err["error"]["message"]
    assert cli_project.wire.requests == [] and list(cli_project.store.event_files()) == []


def test_the_cli_reports_a_budget_stop_with_the_partial_result(cli_project, capsys):
    code, out, err = compare_cli(capsys, cli_project, "--max-usd", "0.0003")
    assert code == 1 and err["error"]["code"] == "budget"
    assert out["stopped"] == "budget" and len(cli_project.wire.requests) == 1


def test_a_harness_candidate_has_no_output_cap_and_a_free_price_bounds_it(cli_project):
    (h,) = _runs_compare.candidates(cli_project.root, file=None, selections=["h"],
                                    pricing={"gpt-6-luna": cmp.Price(0, 0)}, max_output_tokens=99)
    assert (h.prompt_only, h.max_output_tokens, h.row, h.model) == (False, None, "luna", "gpt-6-luna")
    assert cmp.attempt_bound_usd(h, "x") == 0


def test_the_public_surface_lists_the_compare_names():
    surface = json.loads((Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit"
                          / "public-surface.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in surface["entries"] if e["status"] == "public"}
    assert "llm_kit.runs.compare" in surface["covers"]
    listed = {n.rsplit(".", 1)[1] for n in names if n.rsplit(".", 1)[0] == "llm_kit.runs.compare"}
    assert listed == set(cmp.__all__)
    for name in cmp.__all__:
        value = getattr(cmp, name)
        if callable(value):
            assert value.__doc__ and len(value.__doc__.strip()) > 40, name


def test_a_candidate_carries_the_request_settings_of_its_selection_in_the_reuse_key(cli_project):
    """The temperature and budget the call sends are part of the record's request, so a change is a new key."""
    models = {**_MODELS, "endpoints": {"openrouter": {"effort_style": "reasoning-object"}},
              "catalog": [{**_MODELS["catalog"][0], "traits": ["label_only"]}, *_MODELS["catalog"][1:]],
              "requests": {"cold": {"temperature": 0, "reasoning_budget": 300, "max_output_tokens": 900}},
              "selections": {**_MODELS["selections"], "a": {"model": "flash", "request": "cold"}}}
    pm.write_project_models(models, project_root=str(cli_project.root))
    (a,) = _runs_compare.candidates(cli_project.root, file=None, selections=["a"], pricing={}, max_output_tokens=None)
    assert a.max_output_tokens == 900 and a.effort is None
    assert a.extras == {"temperature": 0, "reasoning": {"max_tokens": 300}}  # no trait reaches the request
    models["requests"]["cold"]["temperature"] = 0.5
    pm.write_project_models(models, project_root=str(cli_project.root))
    (warm,) = _runs_compare.candidates(cli_project.root, file=None, selections=["a"], pricing={}, max_output_tokens=None)
    assert warm.extras["temperature"] == 0.5
