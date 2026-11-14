"""The run record format (llm-kit.run-record/v1): shared cases, builders and the reuse key.

The cases under tests/formats/run-record/ are the contract every reader runs
(docs/reference/llm-kit-formats.md). Their reuse keys were computed from the
documented key contract with hashlib and json alone, not with llm_kit.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from llm_kit import runs
from llm_kit.runs import record as rec
from llm_kit.schema import formats
from llm_kit.schema.json_schema import validate

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "packages" / "katsoft-llm-kit"
CASES_DIR = REPO / "tests" / "formats" / "run-record"
CASES = json.loads((CASES_DIR / "cases.json").read_text(encoding="utf-8"))
RECORD_SCHEMA = formats.load_schema(formats.RUN_RECORD_V1)
SURFACE = json.loads((PACKAGE / "public-surface.json").read_text(encoding="utf-8"))


def by_name(cases: list) -> dict:
    return {case["name"]: case for case in cases}


RECORD_CASES = by_name(CASES["records"])


def test_the_cases_name_the_shipped_schemas():
    assert CASES["format"] == formats.RUN_RECORD_V1
    for key, format_id in (("record", formats.RUN_RECORD_V1), ("import", formats.RUN_IMPORT_V1),
                           ("store", formats.RUN_STORE_V1)):
        assert (CASES_DIR / CASES["schemas"][key]).resolve() == \
            PACKAGE / "src" / "llm_kit" / "schema" / "formats" / formats.SCHEMA_FILES[format_id]


@pytest.mark.parametrize("name", sorted(RECORD_CASES))
def test_record_case(name):
    case = RECORD_CASES[name]
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(rec.RunRecordError) as caught:
            rec.validate_record(case["record"])
        assert (caught.value.code, caught.value.where) == (expect["error"]["code"], expect["error"]["path"]), \
            str(caught.value)
        return
    rec.validate_record(case["record"])
    assert validate(RECORD_SCHEMA, case["record"]) == ()
    assert rec.reuse_key(case["record"]) == expect["reuse_key"]


def test_the_schema_refuses_each_schema_level_fault_the_reader_refuses():
    """A fault the subset can state is refused by the whole schema too, not only by the branch the reader picks."""
    for name in ("ok-without-output", "ok-with-error", "failed-without-error", "started-with-ended-at",
                 "started-with-output", "unknown-status", "missing-content-class", "unknown-content-class",
                 "negative-token-count", "token-count-above-exact-range", "unknown-top-level-key"):
        assert validate(RECORD_SCHEMA, RECORD_CASES[name]["record"]) != (), name


def test_the_cases_cover_the_status_and_class_rules():
    names = set(RECORD_CASES)
    for needed in ("started", "ok-minimal", "invalid-with-output-and-error", "failed-without-output",
                   "refused-with-output", "ok-without-output", "failed-without-error", "started-with-ended-at",
                   "unknown-content-class", "missing-content-class", "same-question-other-group"):
        assert needed in names, needed


def test_the_reuse_key_ignores_who_asked_and_why_but_not_what_was_asked():
    base = RECORD_CASES["ok-full"]["record"]
    key = rec.reuse_key(base)
    unchanged = copy.deepcopy(base)
    unchanged["provenance"]["scorer"] = None
    unchanged["labels"] = {"other": "label"}
    unchanged["selection"]["name"] = "renamed.selection"
    unchanged["content"] = "internal"
    unchanged["scores"] = []
    assert rec.reuse_key(unchanged) == key
    for path, value in ((("request", "extras"), {"temperature": 0.6}), (("inputs",), {"rules": "0" * 64}),
                        (("resolved", "effort"), "high"), (("prompt", "slots_sha256"), "1" * 64),
                        (("case", "input_sha256"), "2" * 64), (("flow",), "other.flow"),
                        (("request", "sent_sha256"), "3" * 64)):
        changed = copy.deepcopy(base)
        target = changed
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        if path == ("case", "input_sha256"):
            changed["case"]["input"] = None
        assert rec.reuse_key(changed) != key, path


def test_start_and_finish_build_valid_events_that_share_every_started_field():
    started = rec.start_record(
        flow="summary.write", group={"id": "g1", "kind": "eval"},
        case={"id": "c1", "input_sha256": "a" * 64}, selection={"model": "example/model"},
        content="synthetic", tool={"name": "example-runner"},
    )
    assert started["status"] == "started" and started["format"] == rec.RUN_RECORD_V1
    assert started["group"]["label"] is None and started["request"]["extras"] == {}
    answer = rec.blob_ref("an answer")
    done = rec.finish_record(started, status="ok", output={"text": answer},
                             usage={"input_tokens": 3, "output_tokens": 2, "cached_tokens": None,
                                    "reasoning_tokens": None})
    assert done["output"] == {"text": answer, "parsed": None} and done["error"] is None
    assert all(done[key] == value for key, value in started.items() if key != "status")
    assert rec.blob_refs(done) == [answer]
    assert "ended_at" not in started


def test_finish_refuses_a_result_the_status_does_not_allow():
    started = rec.start_record(flow="f", group={"id": "g", "kind": "single"},
                               case={"id": "c", "input_sha256": "a" * 64}, selection={"model": "m"},
                               content="internal", tool={"name": "t"})
    with pytest.raises(rec.RunRecordError) as caught:
        rec.finish_record(started, status="ok")
    assert caught.value.where == "/output"
    with pytest.raises(rec.RunRecordError) as caught:
        rec.finish_record(started, status="failed")
    assert caught.value.where == "/error"
    failed = rec.finish_record(started, status="failed", error={"code": "timeout"})
    with pytest.raises(rec.RunRecordError, match="needs a started record"):
        rec.finish_record(failed, status="ok", output={"text": rec.blob_ref("x")})
    with pytest.raises(rec.RunRecordError, match="not a result status"):
        rec.finish_record(started, status="started")


def test_start_refuses_an_unknown_content_class():
    with pytest.raises(rec.RunRecordError) as caught:
        rec.start_record(flow="f", group={"id": "g", "kind": "single"}, case={"id": "c", "input_sha256": "a" * 64},
                         selection={"model": "m"}, content="private", tool={"name": "t"})
    assert caught.value.where == "/content"


def test_blob_ref_hashes_text_as_utf8_and_refuses_other_types():
    assert rec.blob_ref("\u00e9") == rec.blob_ref("\u00e9".encode("utf-8"))
    assert rec.blob_ref(b"") == "blob:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    with pytest.raises(TypeError):
        rec.blob_ref(3)  # type: ignore[arg-type]


def test_validate_record_refuses_non_json_values():
    record = copy.deepcopy(RECORD_CASES["ok-minimal"]["record"])
    record["request"]["extras"] = {"temperature": float("nan")}
    with pytest.raises(rec.RunRecordError) as caught:
        rec.validate_record(record)
    assert caught.value.where == ""
    with pytest.raises(rec.RunRecordError):
        rec.validate_record([record])


def test_public_surface_lists_the_runs_names():
    names = {e["name"] for e in SURFACE["entries"] if e["status"] == "public"}
    for module_name in ("llm_kit.runs", "llm_kit.runs.record", "llm_kit.runs.store", "llm_kit.runs.index",
                        "llm_kit.cli.runs"):
        assert module_name in SURFACE["covers"], module_name
        module = __import__(module_name, fromlist=["__all__"])
        listed = {n.rsplit(".", 1)[1] for n in names if n.rsplit(".", 1)[0] == module_name}
        assert listed == set(module.__all__), module_name
        for name in module.__all__:
            value = getattr(module, name)
            if callable(value):
                assert value.__doc__, f"{module_name}.{name}"
    assert set(runs.__all__) == set(rec.__all__) | set(runs.store.__all__) | set(runs.index.__all__)
