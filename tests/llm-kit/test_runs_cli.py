"""``llm-kit runs``: init, import (and --check), list, show (and --with-blobs), reindex.

Exit codes: 0 success, 1 the store refused (one JSON error object on stderr,
nothing written), 2 usage (including no store given or configured).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_kit import cli
from llm_kit.cli import runs as runs_cli
from llm_kit.runs import record as rec
from llm_kit.runs import store as st

QUESTION = "What is two plus two?"
ANSWER = "Four."


def _main(argv, capsys):
    code = cli.main(["runs", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def _line(content: str = "synthetic", record_id: str = "20261114T101500123456Z-0001") -> str:
    started = rec.start_record(
        flow="demo.answer", group={"id": "g1", "kind": "compare"},
        case={"id": "c1", "input_sha256": rec.blob_ref(QUESTION)[5:], "input": rec.blob_ref(QUESTION)},
        selection={"model": "example/model-a"}, content=content, tool={"name": "test-runner"},
        record_id=record_id, started_at="2026-11-14T10:15:00Z",
    )
    result = rec.finish_record(started, status="ok", output={"text": rec.blob_ref(ANSWER)},
                               ended_at="2026-11-14T10:15:01Z")
    return json.dumps({"format": st.RUN_IMPORT_V1, "record": result,
                       "blobs": {rec.blob_ref(QUESTION): {"text": QUESTION}, rec.blob_ref(ANSWER): {"text": ANSWER}}})


@pytest.fixture()
def store_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(st.STORE_ENV, raising=False)
    root = tmp_path / "store"
    code, out, _ = _main(["init", "--store", str(root), "--accepts", "synthetic,internal"], capsys)
    assert code == 0 and json.loads(out) == {"store": str(root), "accepts": ["internal", "synthetic"]}
    return root


def test_import_list_show_and_reindex(store_dir, tmp_path, capsys, monkeypatch):
    path = tmp_path / "run.jsonl"
    path.write_text(_line() + "\n", encoding="utf-8")
    code, out, _ = _main(["import", "--store", str(store_dir), str(path), "--check"], capsys)
    assert code == 0 and json.loads(out)["checked_only"] is True and json.loads(out)["events_written"] == 1
    assert not (store_dir / "records").exists()
    monkeypatch.setenv(st.STORE_ENV, str(store_dir))
    code, out, _ = _main(["import", str(path)], capsys)
    assert code == 0 and json.loads(out)["events_written"] == 1 and json.loads(out)["blobs_written"] == 2
    code, out, _ = _main(["list", "--model", "example/model-a", "--since", "2026-11-14"], capsys)
    rows = [json.loads(line) for line in out.splitlines()]
    assert code == 0 and [r["id"] for r in rows] == ["20261114T101500123456Z-0001"]
    code, out, _ = _main(["list", "--flow", "nothing"], capsys)
    assert code == 0 and out == ""
    code, out, _ = _main(["show", "20261114T101500123456Z-0001"], capsys)
    assert code == 0 and json.loads(out)["status"] == "ok"
    code, out, _ = _main(["show", "20261114T101500123456Z-0001", "--with-blobs"], capsys)
    line = json.loads(out)
    assert code == 0 and line["blobs"][rec.blob_ref(ANSWER)] == {"text": ANSWER}
    code, out, _ = _main(["reindex"], capsys)
    assert code == 0 and json.loads(out) == {"records": 1, "updated": 1, "removed": 0}


def test_a_refused_import_exits_1_with_one_json_error_and_writes_nothing(store_dir, tmp_path, capsys):
    path = tmp_path / "run.jsonl"
    path.write_text(_line() + "\n" + _line("sensitive", "20261114T101500123456Z-0002") + "\n", encoding="utf-8")
    code, out, err = _main(["import", "--store", str(store_dir), str(path)], capsys)
    assert code == 1 and out == ""
    assert json.loads(err)["error"]["code"] == "policy" and json.loads(err)["error"]["kind"] == "runs"
    assert not (store_dir / "records").exists() and not (store_dir / "blobs").exists()


def test_show_of_an_unknown_id_exits_1(store_dir, capsys):
    code, _, err = _main(["show", "--store", str(store_dir), "20261114T101500123456Z-ffff"], capsys)
    assert code == 1 and json.loads(err)["error"]["code"] == "not-found"


def test_no_store_and_bad_arguments_are_usage_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(st.STORE_ENV, raising=False)
    code, _, err = _main(["list"], capsys)
    assert code == 2 and json.loads(err)["error"]["code"] == "no-store"
    code, _, _ = _main(["list", "--store", str(tmp_path), "--status", "done"], capsys)
    assert code == 2
    code, _, _ = _main([], capsys)
    assert code == 2
    code, out, _ = _main(["--help"], capsys)
    assert code == 0 and all(verb in out for verb in runs_cli.VERBS)


def test_every_verb_has_help(capsys):
    for verb in runs_cli.VERBS:
        assert runs_cli.run([verb, "--help"]) == 0
        assert "--store" in capsys.readouterr().out


def test_open_of_a_non_store_directory_exits_1(tmp_path, capsys):
    code, _, err = _main(["list", "--store", str(tmp_path)], capsys)
    assert code == 1 and json.loads(err)["error"]["code"] == "not-found"
    assert not Path(tmp_path / ".index.sqlite").exists()
