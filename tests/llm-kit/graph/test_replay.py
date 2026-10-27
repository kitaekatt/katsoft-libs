"""Replay one node from a saved run against the recorded graph version (llm_kit.graph.replay).

llm.model runs on a scripted MockBackend: no network, no spend.
"""

from __future__ import annotations

import json

import pytest

from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import GraphError, OpRegistry
from llm_kit.graph.ops import builtin_ops
from llm_kit.graph.records import RunStore
from llm_kit.graph.replay import replay_node
from llm_kit.graph.run import Runner
from llm_kit.graph.spend import SpendService
from llm_kit.graph.testing import MockBackendFactory, make_project
from test_services_support import cli_json, write

SELECTION = "draft.write"
GRAPH = """\
id: t.replay
inputs:
  topic: {schema: {type: string}}
nodes:
  draft:
    op: llm.model
    model: draft.write
    prompt: prompts/draft.md
    slots: {topic: inputs.topic}
  polish:
    op: llm.model
    model: draft.write
    prompt: prompts/polish.md
    slots: {text: draft.out}
"""
DRAFT = "head\n---PROMPT---\nDraft about {{topic}}.\n"
POLISH = "head\n---PROMPT---\nPolish: {{text}}\n"
FREE = SpendService(models={"mock-row": 0})  # the mock row costs nothing
CONFIG = """\
version: 1
graphs:
  t.replay: main.graph.yaml
runs:
  dir: work/{item}/graph
services:
  spend: {models: {mock-row: 0}}
"""


@pytest.fixture()
def setup(tmp_path):
    project = make_project(tmp_path / "mock", [SELECTION])
    root = project.root
    write(root / "main.graph.yaml", GRAPH)
    write(root / "prompts" / "draft.md", DRAFT)
    write(root / "prompts" / "polish.md", POLISH)
    write(root / ".llm-kit" / "graphs.yaml", CONFIG)
    registry = OpRegistry({op.name: op for op in builtin_ops()})
    store = RunStore(root / "work" / "it" / "graph")
    with project.environment():
        source = Runner(load_graph(root / "main.graph.yaml"), registry, store, item="it", project_root=root,
                        services={"backend_factory": MockBackendFactory(responses=["draft one", "polish one"]), "spend": FREE})
        first = source.run({"topic": "tea"})
        assert first.status == "done", first.errors
        yield root, registry, store, first


def replay(setup, responses, node="polish", run_id=None):
    root, registry, store, first = setup
    factory = MockBackendFactory(responses=responses)
    result = replay_node(store, run_id or first.run_id, node, registry=registry, project_root=root,
                         services={"backend_factory": factory, "spend": FREE})
    return result, factory


def test_replays_one_node_from_the_recorded_version_into_a_new_run(setup):
    root, _, store, first = setup
    source_bytes = store.run_path(first.run_id).read_bytes()
    # the live files move on; the replay must use the recorded version
    write(root / "prompts" / "polish.md", "head\n---PROMPT---\nCHANGED {{text}}\n")
    write(root / "main.graph.yaml", GRAPH + "  extra: {op: llm.model, model: draft.write, prompt: prompts/draft.md, slots: {topic: inputs.topic}}\n")

    result, factory = replay(setup, ["polish two"])
    assert result.run_id != first.run_id and result.status == "done"
    assert result.nodes["draft"] == "reused" and result.nodes["polish"] == "done"
    assert [c["user"] for c in factory.calls] == ["Polish: draft one\n"]  # recorded template, saved input
    record = store.load_run(result.run_id)
    assert record["replay"] == {"run": first.run_id, "node": "polish", "graph": record["graph"]["hash"],
                                "graph_file": "main.graph.yaml"}
    assert record["target"] == ["draft", "polish"] and record["force"] == ["polish"]
    assert store.get_attribute(record["nodes"]["polish"]["attributes"]["text"]) == "polish two"
    assert store.run_path(first.run_id).read_bytes() == source_bytes  # the source run is untouched
    assert not list((root / ".llm-kit").glob("replay-*"))  # the scratch copy is gone


def test_the_verb_replays_the_newest_run(setup):
    root, _, store, first = setup
    from llm_kit.graph.ops.model import set_backend_factory
    set_backend_factory(MockBackendFactory(responses=["polish three"]))
    try:
        code, out = cli_json("replay", "t.replay", "--item", "it", "--node", "polish", "--project", str(root))
    finally:
        set_backend_factory(None)
    assert code == 0 and out["run_id"] != first.run_id
    assert out["nodes"]["draft"] == "reused" and out["nodes"]["polish"] == "done"
    assert len(store.run_ids()) == 2


def test_a_missing_recorded_file_fails_loudly(setup):
    root, _, store, first = setup
    record = store.load_run(first.run_id)
    ref = record["files"]["prompts/polish.md"]
    digest = ref.split(":", 1)[1]
    (store.blobs_dir / digest[:2] / digest).unlink()
    with pytest.raises(GraphError, match="recorded version of prompts/polish.md is not available"):
        replay(setup, ["x"])
    assert len(store.run_ids()) == 1


def test_a_run_with_no_snapshots_fails_loudly(setup):
    _, _, store, first = setup
    record = store.load_run(first.run_id)
    record["files"] = {}
    store.run_path(first.run_id).write_text(json.dumps(record), encoding="ascii")
    with pytest.raises(GraphError, match="no file snapshots"):
        replay(setup, ["x"])


def test_a_changed_op_version_means_the_recorded_version_is_not_available(setup):
    root, registry, store, first = setup
    op = registry.get("llm.model")
    changed = OpRegistry({**{o.name: o for o in builtin_ops()}, "llm.model": type(op)(**{**op.__dict__, "version": "99"})})
    with pytest.raises(GraphError, match="not the recorded"):
        replay_node(store, first.run_id, "polish", registry=changed, project_root=root,
                    services={"backend_factory": MockBackendFactory(responses=["x"]), "spend": FREE})
    assert len(store.run_ids()) == 1


@pytest.mark.parametrize("node,message", [("nope", "no record of node 'nope'"),
                                          ("polish/inner", "not a top-level node")])
def test_a_node_that_cannot_be_replayed_fails_loudly(setup, node, message):
    with pytest.raises(GraphError, match=message):
        replay(setup, ["x"], node=node)


def test_a_dependency_without_a_reusable_record_is_not_run(setup):
    _, _, store, first = setup
    record = store.load_run(first.run_id)
    record["nodes"]["draft"]["status"] = "failed"
    store.run_path(first.run_id).write_text(json.dumps(record), encoding="ascii")
    result_error = None
    try:
        replay(setup, ["x", "y"])
    except GraphError as exc:
        result_error = str(exc)
    assert result_error and "node 'draft'" in result_error and "replay runs only the one node" in result_error
    assert len(store.run_ids()) == 1


def test_the_verb_fails_loudly_for_a_missing_run(setup, capsys):
    root, _, _, _ = setup
    from llm_kit.cli.graph import run as graph_cli
    assert graph_cli(["replay", "t.replay", "--item", "it", "--node", "polish", "--run",
                      "20200101T000000000000Z-0000", "--project", str(root)]) == 1
    assert "does not exist" in capsys.readouterr().err
