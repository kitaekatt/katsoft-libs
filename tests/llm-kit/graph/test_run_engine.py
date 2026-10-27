"""llm_kit.graph.run: DAG order, when, map, repeat, subgraphs, failure gating, stops."""

from __future__ import annotations

import threading

import pytest

from llm_kit.graph.model import GraphError, Op, Port
from llm_kit.graph.ops import OpFailed, OpHalted
from llm_kit.graph.spend import SpendService
from test_run_support import Calls, runner, top, write

DIAMOND = """\
id: t.diamond
inputs:
  n: {type: number}
outputs:
  total: {type: number}
nodes:
  a: {op: t.add, in: {a: inputs.n, b: 1}}
  b: {op: t.wait, in: {x: a.out}}
  c: {op: t.wait, in: {x: "a.out + 10"}}
  d: {op: t.add, in: {a: b.out, b: c.out}}
outputs_from:
  total: d.out
"""


def test_diamond_runs_branches_in_parallel_and_joins(tmp_path):
    calls = Calls()
    calls.barrier = threading.Barrier(2, timeout=5)   # b and c must be running at the same time
    result = runner(tmp_path, DIAMOND, calls).run({"n": 1})
    assert result.status == "done"
    assert result.outputs == {"total": 2 + 12}
    assert calls.order[0] == "a" and calls.order[-1] == "d"
    assert set(calls.order[1:3]) == {"b", "c"}


def test_inputs_are_checked_against_their_ports(tmp_path):
    r = runner(tmp_path, DIAMOND, Calls())
    with pytest.raises(GraphError, match="needs input 'n'"):
        r.run({})
    with pytest.raises(GraphError, match="does not match its schema"):
        r.run({"n": "one"})
    with pytest.raises(GraphError, match="no inputs"):
        r.run({"n": 1, "m": 2})


WHEN = """\
id: t.when
inputs:
  enabled: {type: boolean}
outputs:
  value: {}
nodes:
  maybe: {op: t.echo, when: inputs.enabled, in: {x: "'made'"}}
  after: {op: t.echo, in: {x: "maybe.out ?? 'fallback'"}}
outputs_from:
  value: after.out
"""


def test_when_false_skips_the_node_with_null_attributes(tmp_path):
    calls = Calls()
    r = runner(tmp_path, WHEN, calls)
    off = r.run({"enabled": False})
    assert off.nodes["maybe"] == "skipped" and off.outputs == {"value": "fallback"}
    assert "maybe" not in calls.paths
    on = r.run({"enabled": True})
    assert on.nodes["maybe"] == "done" and on.outputs == {"value": "made"}


MAP = """\
id: t.map
inputs:
  poses: {type: array}
outputs:
  images: {type: array}
nodes:
  draw:
    map:
      over: inputs.poses
      key: item.id
      body:
        nodes:
          one: {op: t.echo, in: {x: "item.text + '!'"}}
        out: {image: one.out, at: index}
outputs_from:
  images: draw.out
"""


def test_map_runs_each_item_with_its_key_and_keeps_order(tmp_path):
    calls = Calls()
    poses = [{"id": "p1", "text": "a"}, {"id": "p/2", "text": "b"}, {"id": "p3", "text": "c"}]
    result = runner(tmp_path, MAP, calls).run({"poses": poses})
    assert result.outputs == {"images": [{"image": "a!", "at": 0}, {"image": "b!", "at": 1},
                                         {"image": "c!", "at": 2}]}
    assert set(calls.paths) == {"draw[p1]/one", "draw[p%2F2]/one", "draw[p3]/one"}
    assert result.nodes["draw[p3]"] == "done"


def test_map_refuses_duplicate_and_non_scalar_keys(tmp_path):
    r = runner(tmp_path, MAP, Calls())
    result = r.run({"poses": [{"id": "x", "text": "a"}, {"id": "x", "text": "b"}]})
    assert result.status == "failed" and "not unique" in result.errors["draw"]
    result = r.run({"poses": [{"id": True, "text": "a"}]})
    assert "non-empty string or an integer" in result.errors["draw"]


def test_a_failed_map_item_fails_the_map_and_keeps_the_others(tmp_path):
    calls = Calls()
    calls.fail_keys = {"b"}
    text = MAP.replace("t.echo, in: {x: \"item.text + '!'\"}", "t.fail_some, in: {x: item.text}")
    poses = [{"id": "p1", "text": "a"}, {"id": "p2", "text": "b"}]
    r = runner(tmp_path, text, calls)
    first = r.run({"poses": poses})
    assert first.status == "failed"
    assert first.nodes["draw[p1]"] == "done" and first.nodes["draw[p2]"] == "failed"
    assert first.nodes["draw[p2]/one"] == "failed" and "boom b" in first.errors["draw[p2]/one"]
    calls.fail_keys = set()
    calls.reset()
    second = r.run({"poses": poses})
    assert second.status == "done"
    assert second.nodes["draw[p1]/one"] == "reused" and dict(calls.paths) == {"draw[p2]/one": 1}


REPEAT = """\
id: t.repeat
inputs:
  rounds: {schema: {type: integer}, default: 4}
outputs:
  rounds: {type: array}
  best: {}
nodes:
  loop:
    repeat:
      max: inputs.rounds
      carry:
        best: {init: null, next: "max(carry.best ?? 0, round.score)"}
      until: round.score >= 8 or (carry.best != null and round.score <= carry.best)
      body:
        nodes:
          judge: {op: t.score, in: {i: index}}
        out: {score: judge.out.score, before: carry.best}
outputs_from:
  rounds: loop.rounds
  best: loop.carry.best
"""


def test_repeat_stops_on_until_and_keeps_every_round(tmp_path):
    calls = Calls()
    calls.scores = [5, 7, 6, 9]
    result = runner(tmp_path, REPEAT, calls).run()
    assert result.outputs == {"rounds": [{"score": 5, "before": None}, {"score": 7, "before": 5},
                                         {"score": 6, "before": 7}], "best": 7}
    assert [result.nodes[f"loop[{i}]"] for i in range(3)] == ["done"] * 3
    assert "loop[3]" not in result.nodes


def test_repeat_stops_at_max(tmp_path):
    calls = Calls()
    calls.scores = [1, 2, 3, 4, 5]
    result = runner(tmp_path, REPEAT, calls).run({"rounds": 2})
    assert [r["score"] for r in result.outputs["rounds"]] == [1, 2]
    assert result.outputs["best"] == 2


def test_repeat_max_must_be_a_positive_integer(tmp_path):
    result = runner(tmp_path, REPEAT, Calls()).run({"rounds": 0})
    assert result.status == "failed" and "at least 1" in result.errors["loop"]


SUBGRAPH = """\
id: t.outer
inputs:
  n: {type: number}
outputs:
  out: {}
nodes:
  inner: {graph: ./inner.graph.yaml, in: {n: inputs.n}}
  each:
    map: {over: "[1, 2]", key: item, graph: ./inner.graph.yaml, in: {n: item, step: 100}}
  again:
    repeat: {max: 2, graph: ./inner.graph.yaml, in: {n: "index + 0"}}
outputs_from:
  out: "[inner.out.sum, each.out, again.rounds]"
"""

INNER = """\
id: t.inner
inputs:
  n: {type: number}
  step: {schema: {type: number}, default: 1}
outputs:
  sum: {type: number}
nodes:
  add: {op: t.add, in: {a: inputs.n, b: inputs.step}}
outputs_from:
  sum: add.out
"""


def test_subgraph_files_for_graph_map_and_repeat_nodes(tmp_path):
    write(tmp_path / "proj" / "inner.graph.yaml", INNER)
    calls = Calls()
    result = runner(tmp_path, SUBGRAPH, calls).run({"n": 5})
    assert result.outputs == {"out": [6, [{"sum": 101}, {"sum": 102}], [{"sum": 1}, {"sum": 2}]]}
    assert {"inner/add", "each[1]/add", "each[2]/add", "again[0]/add", "again[1]/add"} == set(calls.paths)


def test_failure_gates_dependents_and_unrelated_nodes_finish(tmp_path):
    text = """\
id: t.fail
nodes:
  bad: {op: t.fail, in: {x: 1}}
  after: {op: t.echo, in: {x: bad.out}}
  later: {op: t.echo, in: {x: after.out}}
  free: {op: t.echo, in: {x: 2}}
"""
    calls = Calls()
    result = runner(tmp_path, text, calls).run()
    assert result.status == "failed"
    assert top(result) == {"bad": "failed", "after": "gated", "later": "gated", "free": "done"}
    assert result.errors["bad"] == "boom 1"


def test_op_output_and_attributes_are_checked(tmp_path):
    text = "id: t.bad\nnodes:\n  x: {op: t.bad_out, in: {x: 1}}\n"
    result = runner(tmp_path, text, Calls()).run()
    assert result.status == "failed" and "does not match its output schema" in result.errors["x"]


def test_an_op_that_stops_ends_the_run_cleanly(tmp_path):
    text = """\
id: t.stop
nodes:
  first: {op: t.stop, in: {x: 1}}
  next: {op: t.echo, in: {x: first.out}}
"""
    result = runner(tmp_path, text, Calls()).run()
    assert result.status == "stopped"
    assert result.stop == {"reason": "halted", "node": "first", "message": "usage limit"}
    assert top(result) == {"first": "stopped", "next": "pending"}


def test_op_halted_stops_and_op_failed_fails_keeping_receipts(tmp_path):
    def halted(ctx, x):
        raise OpHalted("usage limit reached", halt={"kind": "usage_limit"}, receipt={"model": "m"},
                       attributes={"sent": "prompt"})

    def failed(ctx, x):
        raise OpFailed("bad answer", code="schema", attributes={"text": "raw"})

    extra = (Op("t.halted", "1", halted, inputs={"x": Port({})}, attributes=frozenset({"out", "sent"})),
             Op("t.failed", "1", failed, inputs={"x": Port({})}, attributes=frozenset({"out", "text"})))
    calls = Calls()
    from test_run_support import registry
    from llm_kit.graph.loader import load_graph
    from llm_kit.graph.records import RunStore
    from llm_kit.graph.run import Runner

    path = write(tmp_path / "g.graph.yaml", "id: t.h\nnodes:\n  h: {op: t.halted, in: {x: 1}}\n")
    store = RunStore(tmp_path / "runs")
    result = Runner(load_graph(path), registry(calls, *extra), store, item="i", project_root=tmp_path).run()
    assert result.status == "stopped" and result.stop["reason"] == "halted"
    node = store.load_run(result.run_id)["nodes"]["h"]
    assert node["receipt"] == {"model": "m"} and store.resolve(node["attributes"]["sent"]) == "prompt"
    assert node["error"]["halt"] == {"kind": "usage_limit"}

    path = write(tmp_path / "f.graph.yaml", "id: t.f\nnodes:\n  f: {op: t.failed, in: {x: 1}}\n")
    result = Runner(load_graph(path), registry(calls, *extra), store, item="i", project_root=tmp_path).run()
    node = store.load_run(result.run_id)["nodes"]["f"]
    assert result.status == "failed" and node["error"]["code"] == "schema"
    assert store.resolve(node["attributes"]["text"]) == "raw"


def test_paid_ops_need_the_spend_service(tmp_path):
    text = "id: t.paid\nnodes:\n  p: {op: t.paid, in: {x: 1}}\n"
    calls = Calls()
    result = runner(tmp_path, text, calls).run()
    assert result.status == "failed" and "spend service" in result.errors["p"] and not calls.paths
    assert runner(tmp_path, text, calls, services={"spend": SpendService()}).run().status == "done"


def test_a_human_node_without_the_wait_service_stops_the_run_as_unsupported(tmp_path):
    text = """\
id: t.outer
outer: true
nodes:
  make: {op: t.echo, in: {x: 1}}
  played:
    human: {kind: played, schema: {type: boolean}, payload: {made: make.out}}
  ship:
    commit: {contract: {}, bundle: make.out, adapter: t.adapter}
  after: {op: t.echo, in: {x: played.out}}
"""
    result = runner(tmp_path, text, Calls(), max_parallel=1).run()
    assert result.status == "stopped" and result.stop["reason"] == "unsupported"
    assert result.nodes["make"] == "done"
    assert result.nodes["played"] == "stopped" and result.nodes["ship"] in ("done", "pending")
    assert result.nodes["after"] == "pending"
    assert "durable-wait service" in result.stop["message"]


def test_target_runs_only_the_target_and_what_it_reads(tmp_path):
    calls = Calls()
    calls.barrier = threading.Barrier(1)
    result = runner(tmp_path, DIAMOND, calls).run({"n": 1}, target=["b"])
    assert set(top(result)) == {"a", "b"} and result.outputs is None and result.status == "done"
    with pytest.raises(GraphError, match="not top-level nodes"):
        runner(tmp_path, DIAMOND, calls).run({"n": 1}, target=["zzz"])


def test_an_invalid_graph_is_refused_before_anything_runs(tmp_path):
    with pytest.raises(GraphError, match="unknown op"):
        runner(tmp_path, "id: t.x\nnodes:\n  a: {op: t.nope, in: {}}\n", Calls())
