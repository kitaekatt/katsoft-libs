"""llm_kit.graph run records, content stores, list_items and the run/status verbs."""

from __future__ import annotations

import hashlib
import json

import pytest

from llm_kit.graph.config import load_config
from llm_kit.graph.media import ContentStore, StoreError, ref_kind
from llm_kit.graph.records import RUN_FORMAT, RunStore, list_items
from test_run_support import Calls, registry, runner, write

GRAPH = """\
id: t.records
inputs:
  topic: {type: string}
outputs:
  final: {}
nodes:
  draft:
    op: t.write
    prompt: ./draft.md
    slots: {topic: inputs.topic}
  image: {op: t.picture, in: {x: draft.out.draft}}
  loop:
    repeat:
      max: 2
      carry:
        n: {init: 0, next: "carry.n + 1"}
      body:
        nodes:
          step: {op: t.echo, in: {x: "carry.n"}}
        out: {n: step.out}
  skipped: {op: t.echo, when: "false", in: {x: 1}}
outputs_from:
  final: "[draft.out.draft, image.out.image, loop.carry.n]"
"""


def _run(tmp_path, calls=None):
    write(tmp_path / "proj" / "draft.md", "Write about {{topic}}.")
    r = runner(tmp_path, GRAPH, calls or Calls())
    return r, r.run({"topic": "rain"})


def test_record_round_trip(tmp_path):
    r, result = _run(tmp_path)
    store = r.store
    assert result.record_path == store.runs_dir / f"{result.run_id}.json"
    record = json.loads(result.record_path.read_text(encoding="ascii"))
    assert record == store.load_run(result.run_id)
    assert record["format"] == RUN_FORMAT and record["kind"] == "run" and record["status"] == "done"
    assert record["graph"] == {"id": "t.records", "file": "main.graph.yaml", "hash": r.hashes.graph}
    assert store.resolve(record["inputs"]) == {"topic": "rain"}
    assert store.resolve(record["outputs"]) == result.outputs
    assert set(record["files"]) == {"main.graph.yaml", "draft.md"}
    assert store.resolve(record["files"]["draft.md"]) == "Write about {{topic}}."
    root = record["graphs"][r.hashes.graph]
    assert root["nodes"]["image"]["deps"] == ["draft"] and root["nodes"]["loop"]["body"] in record["graphs"]
    draft = record["nodes"]["draft"]
    assert draft["op"] == "t.write@1" and draft["status"] == "done" and draft["error"] is None
    assert store.resolve(draft["attributes"]["out"]) == {"draft": "Write about rain."}
    assert ref_kind(draft["attributes"]["sent"]) == "blob"
    assert store.resolve(draft["attributes"]["text"]) == "WRITE ABOUT RAIN."
    assert draft["receipt"] == {"model": "fake", "cost": {"usd": 0}}
    assert store.resolve(draft["reads"]) == {"inputs.topic": {"value": "rain"}}
    assert store.resolve(draft["params"]) == {"prompt": "draft.md", "slots": {"topic": "rain"}}
    assert len(draft["definition_hash"]) == 64 and len(draft["input_hash"]) == 64
    image = record["nodes"]["image"]
    assert image["media"] == [store.resolve(image["attributes"]["out"])["image"]]
    assert store.resolve(image["media"][0]).read_bytes() == b"picture Write about rain."
    rounds = [record["nodes"][f"loop[{i}]"] for i in range(2)]
    assert [x["kind"] for x in rounds] == ["round", "round"] and rounds[1]["until"] is False
    assert store.resolve(rounds[1]["carry"]) == {"n": 2}
    assert store.resolve(record["nodes"]["loop"]["attributes"]["rounds"]) == [{"n": 0}, {"n": 1}]
    assert record["nodes"]["skipped"]["status"] == "skipped"
    assert store.resolve(record["nodes"]["skipped"]["attributes"]["out"]) is None
    assert record["counts"]["done"] >= 5 and record["ended_at"]


def test_reused_records_point_at_their_source(tmp_path):
    _, first = _run(tmp_path)
    r, second = _run(tmp_path)
    node = r.store.load_run(second.run_id)["nodes"]["draft"]
    assert node["status"] == "reused" and node["reused_from"] == {"run": first.run_id, "path": "draft"}
    assert node["receipt"] is None
    assert r.store.run_ids() == sorted([first.run_id, second.run_id])
    latest = r.store.latest_records()
    assert latest["draft"][0] == second.run_id


def test_content_store_is_write_once_and_verified(tmp_path):
    store = ContentStore(tmp_path / "v", tmp_path / "b", tmp_path / "m")
    ref = store.put_value({"b": 1, "a": [1.5, None]})
    digest = ref.split(":", 1)[1]
    path = tmp_path / "v" / digest[:2] / f"{digest}.json"
    assert path.read_bytes() == b'{"b":1,"a":[1.5,null]}'          # the value's own key order
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert store.put_value({"b": 1, "a": [1.5, None]}) == ref
    assert store.put_value({"a": [1.5, None], "b": 1}) != ref      # another key order is another value
    assert list(store.get_value(ref)) == ["b", "a"]
    blob = store.put_blob("caf\u00e9")
    assert store.resolve(blob) == "caf\u00e9"
    media = store.put_media(b"PNG", ".png")
    assert ref_kind(media) == "media" and store.media_path(media).read_bytes() == b"PNG"
    path.write_bytes(b'{"tampered":1}')
    with pytest.raises(StoreError, match="does not match its hash"):
        store.get_value(ref)
    store.media_path(media).write_bytes(b"other")
    with pytest.raises(StoreError, match="different content"):
        store.put_media(b"PNG", ".png")
    with pytest.raises(StoreError, match="not a JSON value"):
        store.put_value((1, 2))
    with pytest.raises(StoreError, match="extension"):
        store.put_media(b"x", "png")
    with pytest.raises(StoreError, match="not a store reference"):
        store.resolve("value:abc")


CONFIG = """\
version: 1
graphs: {t.records: main.graph.yaml}
runs:
  dir: work/{item}/graph
  blobs: cache/blobs
"""


def _project(tmp_path, monkeypatch, calls):
    root = tmp_path / "proj"
    write(root / ".llm-kit" / "graphs.yaml", CONFIG)
    write(root / "main.graph.yaml", GRAPH)
    write(root / "draft.md", "Write about {{topic}}.")
    from llm_kit.graph.commands import validate as validate_command
    monkeypatch.setattr(validate_command, "load_registry", lambda config: registry(calls))
    return root


def test_store_paths_from_the_config_and_list_items(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, Calls())
    config = load_config(root)
    store = RunStore.for_item(config, "night-petition")
    assert store.dir == root / "work" / "night-petition" / "graph"
    assert store.blobs_dir == root / "cache" / "blobs" and store.media_dir == store.dir / "media"
    assert list_items(config) == []
    from llm_kit.graph.run import Runner
    Runner.for_project(root, "t.records", "night-petition").run({"topic": "rain"})
    assert list_items(config) == ["night-petition"]
    from llm_kit.graph.model import GraphError
    with pytest.raises(GraphError, match="must match"):
        RunStore.for_item(config, "../escape")


def test_run_and_status_verbs(tmp_path, monkeypatch, capsys):
    from llm_kit.cli import graph as group
    calls = Calls()
    root = _project(tmp_path, monkeypatch, calls)
    base = ["--item", "np", "--project", str(root)]

    assert group.run(["status", "t.records", *base]) == 0
    out = capsys.readouterr().out
    assert "runs: none" in out and "plan: not available" in out and "needs input 'topic'" in out

    assert group.run(["run", "t.records", *base, "--input", 'topic="rain"', "--dry-run"]) == 0
    assert "run     draft (no record)" in capsys.readouterr().out and calls.total() == 0

    assert group.run(["run", "t.records", *base, "--input", 'topic="rain"']) == 0
    out = capsys.readouterr().out
    assert "done     draft" in out and ": done" in out and "record: " in out

    topic = write(tmp_path / "topic.json", '"snow"')
    assert group.run(["run", "t.records", *base, "--input", f"topic=@{topic}", "--target", "draft", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "done" and set(doc["nodes"]) == {"draft"}

    assert group.run(["status", "t.records", *base, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    nodes = {n["path"]: n for n in report["nodes"]}
    assert len(report["runs"]) == 2 and nodes["draft"]["next"] == "reuse"
    assert nodes["image"]["next"] == "run" and nodes["image"]["reason"] == "inputs changed"

    assert group.run(["run", "t.records", *base, "--input", "topic=rain"]) == 1
    assert "not JSON" in capsys.readouterr().err
    assert group.run(["run", "t.records", *base, "--target", "nope"]) == 1
    assert "not top-level nodes" in capsys.readouterr().err


def test_run_verb_exit_codes_for_failed_and_stopped(tmp_path, monkeypatch, capsys):
    from llm_kit.cli import graph as group
    calls = Calls()
    root = _project(tmp_path, monkeypatch, calls)
    write(root / "bad.graph.yaml", "id: t.bad\nnodes:\n  a: {op: t.fail, in: {x: 1}}\n")
    write(root / "stop.graph.yaml", "id: t.stop\nnodes:\n  a: {op: t.stop, in: {x: 1}}\n")
    base = ["--item", "np", "--project", str(root)]
    assert group.run(["run", str(root / "bad.graph.yaml"), *base]) == 1
    assert "failed   a: boom 1" in capsys.readouterr().out
    assert group.run(["run", str(root / "stop.graph.yaml"), *base]) == 3
    assert "stopped  a: halted: usage limit" in capsys.readouterr().out


def test_public_surface_lists_the_runtime_names():
    from pathlib import Path
    import importlib

    surface = Path(__file__).resolve().parents[3] / "packages" / "katsoft-llm-kit" / "public-surface.json"
    names = {e["name"] for e in json.loads(surface.read_text(encoding="utf-8"))["entries"]}
    for module in ("run", "records", "freshness", "media"):
        for name in importlib.import_module(f"llm_kit.graph.{module}").__all__:
            assert f"llm_kit.graph.{module}.{name}" in names, name
