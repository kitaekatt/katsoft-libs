"""The run store, the import reader and the derived index (llm_kit.runs).

Write contract: the store checks a whole batch (validity, blob hashes, policy,
blob completeness, unreferenced blobs, event conflicts) before it writes any
file, and refuses the whole batch on the first failure. Events are write-once:
a started event and a result event per attempt, the result repeating every
started field. The index is derived, metadata-only and git-ignored.
"""

from __future__ import annotations

import base64
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from llm_kit.runs import index as idx
from llm_kit.runs import record as rec
from llm_kit.runs import store as st
from llm_kit.schema import formats
from llm_kit.schema.json_schema import validate

REPO = Path(__file__).resolve().parents[2]
CASES = json.loads((REPO / "tests" / "formats" / "run-record" / "cases.json").read_text(encoding="utf-8"))
RECORDS = {case["name"]: case["record"] for case in CASES["records"]}
RECORD_SCHEMA = formats.load_schema(formats.RUN_RECORD_V1)
IMPORT_SCHEMA = formats.load_schema(formats.RUN_IMPORT_V1)
STORE_SCHEMA = formats.load_schema(formats.RUN_STORE_V1)

QUESTION = "a private question about a real person"
ANSWER = "a private answer"


def _attempt(content: str = "synthetic", *, record_id: str = "20261114T101500123456Z-0001", flow: str = "demo.answer",
             group: str = "g1", model: str = "example/model-a", started_at: str = "2026-11-14T10:15:00.123456Z"):
    started = rec.start_record(
        flow=flow, group={"id": group, "kind": "compare", "label": "label text stays out of the index"},
        case={"id": "c1", "input_sha256": rec.blob_ref(QUESTION)[5:], "input": rec.blob_ref(QUESTION)},
        selection={"name": "demo.write", "model": model}, content=content, tool={"name": "test-runner"},
        record_id=record_id, started_at=started_at,
    )
    result = rec.finish_record(started, status="ok", output={"text": rec.blob_ref(ANSWER)},
                               error=None, cost={"usd": 0.25, "source": "reported"}, ended_at=started_at)
    return started, result


def _blobs(*texts: str) -> dict:
    return {rec.blob_ref(t): t for t in texts}


@pytest.fixture()
def store(tmp_path):
    return st.RunRecordStore.init(tmp_path / "store", accepts=["synthetic", "internal"])


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# -- create and open ------------------------------------------------------------


def test_init_writes_the_descriptor_and_ignores_the_index(tmp_path):
    store = st.RunRecordStore.init(tmp_path / "s", accepts=["internal", "synthetic"])
    descriptor = json.loads((tmp_path / "s" / "store.json").read_text(encoding="utf-8"))
    assert validate(STORE_SCHEMA, descriptor) == ()
    assert descriptor["accepts"] == ["synthetic", "internal"]
    assert ".index.sqlite*" in (tmp_path / "s" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert store.accepts == {"synthetic", "internal"}
    assert st.RunRecordStore.init(tmp_path / "s", accepts=["synthetic", "internal"]).accepts == store.accepts
    with pytest.raises(st.RunStoreError) as caught:
        st.RunRecordStore.init(tmp_path / "s", accepts=["sensitive"])
    assert caught.value.code == "conflict"
    with pytest.raises(st.RunStoreError) as caught:
        st.RunRecordStore.init(tmp_path / "t", accepts=["player"])
    assert caught.value.code == "invalid"


def test_init_adds_the_index_line_to_an_existing_gitignore(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    (root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    st.RunRecordStore.init(root, accepts=["internal"])
    assert (root / ".gitignore").read_text(encoding="utf-8") == "*.tmp\n.index.sqlite*\n"


@pytest.mark.parametrize("case", CASES["stores"], ids=lambda c: c["name"])
def test_store_case(tmp_path, case):
    (tmp_path / "store.json").write_text(json.dumps(case["document"]), encoding="utf-8")
    if "error" in case["expect"]:
        with pytest.raises(st.RunStoreError) as caught:
            st.RunRecordStore.open(tmp_path)
        assert caught.value.code == case["expect"]["error"]
        return
    assert sorted(st.RunRecordStore.open(tmp_path).accepts) == case["expect"]["accepts"]


def test_open_refuses_a_directory_that_is_not_a_store(tmp_path):
    with pytest.raises(st.RunStoreError) as caught:
        st.RunRecordStore.open(tmp_path)
    assert caught.value.code == "not-found"


def test_resolve_store_path_takes_the_flag_then_the_variable_and_never_guesses(tmp_path):
    assert st.resolve_store_path(tmp_path, env={}) == tmp_path
    assert st.resolve_store_path(None, env={st.STORE_ENV: str(tmp_path)}) == tmp_path
    with pytest.raises(st.RunStoreError) as caught:
        st.resolve_store_path(None, env={})
    assert caught.value.code == "no-store" and st.STORE_ENV in str(caught.value)


# -- write: events -------------------------------------------------------------


def test_a_started_event_then_a_result_keep_both_files_and_the_record_is_the_result(store):
    started, result = _attempt()
    path = store.put(started, _blobs(QUESTION))
    assert path == store.root / "records" / "2026-11" / f"{started['id']}.started.json"
    assert store.get(started["id"])["status"] == "started"
    store.put(result, _blobs(ANSWER))
    assert store.get(started["id"]) == result
    events = store.events(started["id"])
    assert events["started"] == started and events["result"] == result
    for event in events.values():
        assert validate(RECORD_SCHEMA, json.loads(json.dumps(event))) == ()
    assert store.ids() == [started["id"]]
    assert store.get_blob(result["output"]["text"]) == ANSWER.encode("utf-8")


def test_an_unfinished_attempt_stays_visible_as_started(store):
    started, _ = _attempt()
    store.put(started, _blobs(QUESTION))
    assert [r["status"] for r in store.records()] == ["started"]
    assert [row["status"] for row in store.index().query()] == ["started"]


def test_a_result_may_arrive_without_a_started_event(store):
    _, result = _attempt()
    store.put(result, _blobs(QUESTION, ANSWER))
    assert store.events(result["id"])["started"] is None


def test_events_are_write_once_and_a_repeat_with_the_same_bytes_is_a_no_op(store):
    started, result = _attempt()
    first = store.put_many([(started, _blobs(QUESTION)), (result, _blobs(ANSWER))])
    assert (first.events_written, first.blobs_written) == (2, 2)
    again = store.put_many([(started, _blobs(QUESTION)), (result, _blobs(ANSWER))])
    assert (again.events_written, again.events_present, again.blobs_written, again.blobs_present) == (0, 2, 0, 2)
    changed = copy.deepcopy(result)
    changed["cost"] = {"usd": 9.0, "source": "reported"}
    before = _files(store.root)
    with pytest.raises(st.RunStoreError) as caught:
        store.put(changed)
    assert caught.value.code == "conflict"
    assert _files(store.root) == before


def test_a_result_that_changes_a_started_field_is_refused(store):
    started, result = _attempt()
    store.put(started, _blobs(QUESTION))
    moved = copy.deepcopy(result)
    moved["selection"]["model"] = "example/model-b"
    with pytest.raises(st.RunStoreError) as caught:
        store.put(moved, _blobs(ANSWER))
    assert caught.value.code == "conflict" and "selection" in str(caught.value)
    with pytest.raises(st.RunStoreError) as caught:
        store.put_many([(started, {}), (moved, _blobs(ANSWER))])
    assert caught.value.code == "conflict"


def test_a_started_event_that_differs_from_a_stored_result_is_refused(store):
    started, result = _attempt()
    store.put(result, _blobs(QUESTION, ANSWER))
    other = copy.deepcopy(started)
    other["flow"] = "demo.other"
    with pytest.raises(st.RunStoreError) as caught:
        store.put(other)
    assert caught.value.code == "conflict"
    store.put(started)
    assert store.events(started["id"])["started"] == started


def test_two_different_events_of_one_kind_in_a_batch_are_refused(store):
    _, result = _attempt()
    other = copy.deepcopy(result)
    other["cost"] = None
    with pytest.raises(st.RunStoreError) as caught:
        store.put_many([(result, _blobs(QUESTION, ANSWER)), (other, {})])
    assert caught.value.code == "conflict"
    assert _files(store.root) == [".gitignore", "store.json"]


# -- write: classification and completeness ----------------------------------------------


def test_the_policy_refuses_the_whole_batch_before_any_file_is_written(store):
    _, synthetic = _attempt()
    _, sensitive = _attempt("sensitive", record_id="20261114T101500123456Z-0002")
    with pytest.raises(st.RunStoreError) as caught:
        store.put_many([(synthetic, _blobs(QUESTION, ANSWER)), (sensitive, {})])
    assert caught.value.code == "policy" and "sensitive" in str(caught.value)
    assert _files(store.root) == [".gitignore", "store.json"]


def test_a_store_that_accepts_sensitive_takes_it(tmp_path):
    local = st.RunRecordStore.init(tmp_path / "local", accepts=["sensitive"])
    _, sensitive = _attempt("sensitive")
    local.put(sensitive, _blobs(QUESTION, ANSWER))
    assert local.get(sensitive["id"])["content"] == "sensitive"


def test_a_named_blob_that_is_neither_supplied_nor_stored_refuses_the_batch(store):
    _, result = _attempt()
    with pytest.raises(st.RunStoreError) as caught:
        store.put(result, _blobs(QUESTION))
    assert caught.value.code == "incomplete" and rec.blob_ref(ANSWER) in str(caught.value)
    assert _files(store.root) == [".gitignore", "store.json"]


def test_a_blob_already_in_the_store_completes_a_record(store):
    started, result = _attempt()
    store.put(started, _blobs(QUESTION))
    report = store.put(result, _blobs(ANSWER))
    assert report.name.endswith(".result.json")


def test_a_supplied_blob_no_record_names_is_refused(store):
    _, result = _attempt()
    with pytest.raises(st.RunStoreError) as caught:
        store.put(result, _blobs(QUESTION, ANSWER, "stray text of unknown class"))
    assert caught.value.code == "unreferenced"
    assert _files(store.root) == [".gitignore", "store.json"]


def test_supplied_bytes_must_hash_to_their_reference(store):
    _, result = _attempt()
    blobs = _blobs(QUESTION)
    blobs[rec.blob_ref(ANSWER)] = "different bytes"
    with pytest.raises(st.RunStoreError) as caught:
        store.put(result, blobs)
    assert caught.value.code == "blob-hash"


def test_an_invalid_record_refuses_the_batch(store):
    _, result = _attempt()
    broken = copy.deepcopy(result)
    broken["output"] = None
    with pytest.raises(st.RunStoreError) as caught:
        store.put_many([(result, _blobs(QUESTION, ANSWER)), (broken, {})])
    assert caught.value.code == "invalid" and "item 1" in str(caught.value)


def test_check_many_reports_without_writing(store):
    started, result = _attempt()
    report = store.check_many([(started, _blobs(QUESTION)), (result, _blobs(ANSWER))])
    assert (report.events_written, report.blobs_written) == (2, 2)
    assert _files(store.root) == [".gitignore", "store.json"]


# -- import ------------------------------------------------------------------------


def _import_bytes(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.mark.parametrize("case", CASES["import_lines"], ids=lambda c: c["name"])
def test_import_line_case(case):
    data = _import_bytes(case["lines"])
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(st.RunStoreError) as caught:
            st.read_import_file(data)
        assert caught.value.code == expect["error"]
        assert f"line {expect['line']}" in str(caught.value)
        return
    items = st.read_import_file(data)
    assert len(items) == expect["records"]
    for line in case["lines"]:
        assert validate(IMPORT_SCHEMA, json.loads(line)) == ()


def test_import_file_writes_a_batch_and_export_line_round_trips(store, tmp_path):
    started, result = _attempt()
    lines = [json.dumps({"format": st.RUN_IMPORT_V1, "record": started,
                         "blobs": {rec.blob_ref(QUESTION): {"text": QUESTION}}}),
             json.dumps({"format": st.RUN_IMPORT_V1, "record": result,
                         "blobs": {rec.blob_ref(ANSWER): {"base64": base64.b64encode(ANSWER.encode()).decode()}}})]
    path = tmp_path / "run.jsonl"
    path.write_bytes(_import_bytes(lines))
    assert st.import_file(store, path, check=True).events_written == 2
    assert store.ids() == []
    assert st.import_file(store, path).events_written == 2
    line = store.export_line(result["id"])
    assert validate(IMPORT_SCHEMA, line) == ()
    other = st.RunRecordStore.init(tmp_path / "other", accepts=["synthetic"])
    st.import_file(other, _import_bytes([json.dumps(line)]))
    assert other.get(result["id"]) == result
    assert other.get_blob(rec.blob_ref(QUESTION)) == QUESTION.encode()


def test_export_line_carries_binary_blobs_as_base64(store):
    binary = b"\xff\xfe\x00"
    started = rec.start_record(flow="f", group={"id": "g", "kind": "single"},
                               case={"id": "c", "input_sha256": rec.blob_ref(binary)[5:], "input": rec.blob_ref(binary)},
                               selection={"model": "m"}, content="synthetic", tool={"name": "t"})
    store.put(started, {rec.blob_ref(binary): binary})
    line = store.export_line(started["id"])
    assert line["blobs"] == {rec.blob_ref(binary): {"base64": base64.b64encode(binary).decode()}}


def test_import_refuses_a_class_the_store_does_not_accept_and_writes_nothing(store):
    _, sensitive = _attempt("sensitive")
    data = _import_bytes([json.dumps({"format": st.RUN_IMPORT_V1, "record": sensitive,
                                      "blobs": {k: {"text": v} for k, v in _blobs(QUESTION, ANSWER).items()}})])
    with pytest.raises(st.RunStoreError) as caught:
        st.import_file(store, data)
    assert caught.value.code == "policy"
    assert _files(store.root) == [".gitignore", "store.json"]


# -- index -------------------------------------------------------------------------


def test_the_index_is_metadata_only_and_finds_records_by_flow_model_group_and_date(store):
    started, result = _attempt()
    store.put_many([(started, _blobs(QUESTION)), (result, _blobs(ANSWER))])
    _, other = _attempt(record_id="20261115T000000000000Z-0002", flow="demo.other", group="g2",
                        model="example/model-b", started_at="2026-11-15T00:00:00.5Z")
    store.put(other, _blobs(QUESTION, ANSWER))
    index = store.index()
    rows = index.query()
    assert [r["id"] for r in rows] == [result["id"], other["id"]]
    assert set(rows[0]) == set(idx.INDEX_COLUMNS)
    assert rows[0]["status"] == "ok" and rows[0]["cost_usd"] == 0.25 and rows[0]["reuse_key"] == rec.reuse_key(result)
    assert [r["id"] for r in index.query(flow="demo.other")] == [other["id"]]
    assert [r["id"] for r in index.query(model="example/model-a")] == [result["id"]]
    assert [r["id"] for r in index.query(group="g1")] == [result["id"]]
    assert [r["id"] for r in index.query(since="2026-11-15")] == [other["id"]]
    assert [r["id"] for r in index.query(until="2026-11-15")] == [result["id"]]
    assert [r["id"] for r in index.query(key=rec.reuse_key(result))] == [result["id"]]
    assert [r["id"] for r in index.query(limit=1)] == [other["id"]]
    dump = "\n".join(sqlite3.connect(str(index.path)).iterdump())
    for text in (QUESTION, ANSWER, "label text stays out of the index"):
        assert text not in dump


def test_the_index_follows_files_that_arrive_or_leave_outside_the_library(store, tmp_path):
    _, result = _attempt()
    store.put(result, _blobs(QUESTION, ANSWER))
    index = store.index()
    assert index.sync()["records"] == 1
    clone = st.RunRecordStore.init(tmp_path / "clone", accepts=["synthetic"])
    _, newer = _attempt(record_id="20261116T000000000000Z-0003")
    clone.put(newer, _blobs(QUESTION, ANSWER))
    target = store.event_path(newer["id"], "result")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(clone.event_path(newer["id"], "result").read_bytes())
    assert [r["id"] for r in index.query()] == [result["id"], newer["id"]]
    store.event_path(result["id"], "result").unlink()
    assert [r["id"] for r in index.query()] == [newer["id"]]
    assert index.rebuild() == {"records": 1, "updated": 1, "removed": 0}


def test_the_index_refuses_to_open_when_the_store_does_not_ignore_it(store):
    (store.root / ".gitignore").write_text("", encoding="utf-8")
    with pytest.raises(st.RunStoreError) as caught:
        store.index()
    assert caught.value.code == "index-not-ignored"
    assert not (store.root / st.INDEX_FILE).exists()
