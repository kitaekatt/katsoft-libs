"""llm_kit.graph value store: a stored value keeps its key order; freshness ignores key order."""

from __future__ import annotations

import hashlib
import json

from llm_kit.graph.model import Op, Port
from llm_kit.graph.records import RunStore
from llm_kit.graph.run import Runner
from llm_kit.graph.loader import load_graph
from llm_kit.pipeline.artifacts.freshness.hashing import stable_json
from test_run_support import ANY, Calls, registry, write

GRAPH = """\
id: t.order
inputs:
  seed: {}
outputs:
  final: {}
nodes:
  make: {op: t.shaped, in: {x: inputs.seed}}
  copy: {op: t.echo, in: {x: make.out}}
outputs_from:
  final: copy.out
"""

# Keys are deliberately not sorted at any depth.
SHAPED = {"zeta": 1, "alpha": {"y": [{"b": 2, "a": 1}], "x": "é"}, "mid": None}
REORDERED = {"mid": None, "alpha": {"x": "é", "y": [{"a": 1, "b": 2}]}, "zeta": 1}


def _dump(value) -> str:
    return json.dumps(value)


def _runner(tmp_path, calls, shape):
    def shaped(ctx, x):
        calls.hit(ctx)
        return shape(x)

    root = tmp_path / "proj"
    path = write(root / "main.graph.yaml", GRAPH)
    ops = registry(calls, Op("t.shaped", "1", shaped, inputs={"x": Port(ANY)}))
    return Runner(load_graph(path), ops, RunStore(tmp_path / "runs" / "it"), item="it", project_root=root)


def test_a_value_reads_back_in_its_key_order(tmp_path):
    store = RunStore(tmp_path / "runs" / "it")
    ref = store.put_value(SHAPED)
    back = store.get_value(ref)
    assert back == SHAPED and _dump(back) == _dump(SHAPED) and _dump(back) != _dump(REORDERED)
    path = store.values_dir / ref[len("value:"):][:2] / (ref[len("value:"):] + ".json")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ref[len("value:"):]    # name is the hash of the file


def test_a_value_with_sorted_keys_keeps_its_old_bytes_and_reference(tmp_path):
    store = RunStore(tmp_path / "runs" / "it")
    value = {"a": 1, "b": {"c": [1, 2, {"d": "é", "e": 1.5}]}}
    ref = store.put_value(value)
    assert ref == "value:" + hashlib.sha256(stable_json(value)).hexdigest()
    path = store.values_dir / ref[6:8] / (ref[6:] + ".json")
    assert path.read_bytes() == stable_json(value)


def test_two_key_orders_are_two_references_each_read_back_as_stored(tmp_path):
    store = RunStore(tmp_path / "runs" / "it")
    first, second = store.put_value(SHAPED), store.put_value(REORDERED)
    assert first != second and store.put_value(SHAPED) == first        # same order: same reference, no error
    assert _dump(store.get_value(first)) == _dump(SHAPED)
    assert _dump(store.get_value(second)) == _dump(REORDERED)


def test_run_record_resolve_keeps_the_order_an_op_produced(tmp_path):
    calls = Calls()
    runner = _runner(tmp_path, calls, lambda x: json.loads(_dump(SHAPED)))
    result = runner.run({"seed": 1})
    record = runner.store.load_run(result.run_id)
    assert _dump(result.outputs["final"]) == _dump(SHAPED)
    for node in ("make", "copy"):
        assert _dump(runner.store.resolve(record["nodes"][node]["attributes"]["out"])) == _dump(SHAPED)
    assert _dump(runner.store.resolve(record["outputs"])["final"]) == _dump(SHAPED)
    newest = runner.store.latest_records()["make"][1]
    assert _dump(runner.store.resolve(newest["attributes"]["out"])) == _dump(SHAPED)


def test_reuse_ignores_key_order_and_keeps_the_stored_order(tmp_path):
    calls = Calls()
    first = _runner(tmp_path, calls, lambda x: json.loads(_dump(SHAPED))).run({"seed": {"p": 1, "q": 2}})
    calls.reset()
    # same input with its keys reordered: the input hash is canonical, so every record is reused
    again = _runner(tmp_path, calls, lambda x: json.loads(_dump(REORDERED))).run({"seed": {"q": 2, "p": 1}})
    assert calls.total() == 0 and set(again.nodes.values()) == {"reused"}
    assert _dump(again.outputs["final"]) == _dump(first.outputs["final"]) == _dump(SHAPED)


def test_a_forced_rerun_records_the_order_that_run_produced(tmp_path):
    calls = Calls()
    _runner(tmp_path, calls, lambda x: json.loads(_dump(SHAPED))).run({"seed": 1})
    runner = _runner(tmp_path, calls, lambda x: json.loads(_dump(REORDERED)))
    result = runner.run({"seed": 1}, force=["make", "copy"])
    record = runner.store.load_run(result.run_id)
    assert _dump(runner.store.resolve(record["nodes"]["make"]["attributes"]["out"])) == _dump(REORDERED)
    assert _dump(result.outputs["final"]) == _dump(REORDERED)
