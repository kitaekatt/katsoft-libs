"""llm_kit.graph freshness: reuse by definition and input hash, force, seeds, dry runs."""

from __future__ import annotations

import pytest

from llm_kit.graph.freshness import forced
from llm_kit.graph.model import GraphError
from test_run_support import Calls, runner, top, write

PROMPTED = """\
id: t.prompted
inputs:
  topic: {type: string}
  other: {schema: {type: string}, default: "x"}
outputs:
  final: {}
nodes:
  draft:
    op: t.write
    prompt: ./draft.md
    slots: {topic: inputs.topic}
  review: {op: t.echo, in: {x: draft.out.draft}}
  side: {op: t.echo, in: {x: inputs.other}}
outputs_from:
  final: review.out
"""


def _prompted(tmp_path, calls, text="header\n---PROMPT---\nWrite about {{topic}}.\n"):
    write(tmp_path / "proj" / "draft.md", text)
    return runner(tmp_path, PROMPTED, calls)


def test_a_second_run_reuses_every_fresh_record(tmp_path):
    calls = Calls()
    first = _prompted(tmp_path, calls).run({"topic": "rain"})
    assert first.outputs == {"final": "Write about rain.\n"} and calls.total() == 3
    calls.reset()
    second = _prompted(tmp_path, calls).run({"topic": "rain"})
    assert calls.total() == 0 and set(top(second).values()) == {"reused"}
    assert second.outputs == first.outputs
    calls.reset()
    third = _prompted(tmp_path, calls).run({"topic": "rain"})    # a reused record is reusable again
    assert calls.total() == 0 and third.outputs == first.outputs


def test_a_changed_input_reruns_only_the_nodes_that_read_it(tmp_path):
    calls = Calls()
    _prompted(tmp_path, calls).run({"topic": "rain"})
    calls.reset()
    result = _prompted(tmp_path, calls).run({"topic": "snow"})
    assert dict(calls.paths) == {"draft": 1, "review": 1}
    assert result.nodes["side"] == "reused"


def test_a_changed_prompt_reruns_the_node_and_what_reads_its_new_output(tmp_path):
    calls = Calls()
    _prompted(tmp_path, calls).run({"topic": "rain"})
    calls.reset()
    _prompted(tmp_path, calls, "other header\n---PROMPT---\nWrite about {{topic}}.\n").run({"topic": "rain"})
    assert calls.total() == 0                                 # a header edit does not count
    _prompted(tmp_path, calls, "---PROMPT---\nWrite a poem about {{topic}}.\n").run({"topic": "rain"})
    assert dict(calls.paths) == {"draft": 1, "review": 1}


def test_inputs_not_given_are_inherited_from_the_newest_record(tmp_path):
    calls = Calls()
    _prompted(tmp_path, calls).run({"topic": "rain"})
    calls.reset()
    r = _prompted(tmp_path, calls)
    result = r.run()
    assert calls.total() == 0 and result.outputs == {"final": "Write about rain.\n"}
    record = r.store.load_run(result.run_id)
    assert set(record["inputs_from"]) == {"topic", "other"}


def test_force_reruns_a_path_and_everything_above_and_below_it(tmp_path):
    calls = Calls()
    _prompted(tmp_path, calls).run({"topic": "rain"})
    calls.reset()
    _prompted(tmp_path, calls).run({"topic": "rain"}, force=["draft"])
    assert dict(calls.paths) == {"draft": 1}                  # same output, so review is reused
    assert forced("loop[1]/write", ["loop"]) and forced("loop", ["loop[1]/write"])
    assert not forced("loop2", ["loop"]) and not forced("a/b", ["a/bc"])
    with pytest.raises(GraphError, match="does not start with a top-level node"):
        _prompted(tmp_path, calls).run({"topic": "rain"}, force=["nope"])


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
          one: {op: t.picture, in: {x: item.text}}
        out: {image: one.out.image}
outputs_from:
  images: draw.out
"""


def test_map_one_item_rerun_by_changed_input_or_force(tmp_path):
    calls = Calls()
    poses = [{"id": "a", "text": "1"}, {"id": "b", "text": "2"}, {"id": "c", "text": "3"}]
    runner(tmp_path, MAP, calls).run({"poses": poses})
    assert calls.total() == 3
    calls.reset()
    changed = [poses[0], {"id": "b", "text": "2b"}, poses[2]]
    result = runner(tmp_path, MAP, calls).run({"poses": changed})
    assert dict(calls.paths) == {"draw[b]/one": 1}
    assert result.nodes["draw[a]/one"] == "reused" and result.nodes["draw[c]/one"] == "reused"
    calls.reset()
    runner(tmp_path, MAP, calls).run({"poses": list(reversed(changed))})   # stable keys: order does not matter
    assert calls.total() == 0
    runner(tmp_path, MAP, calls).run({"poses": changed}, force=["draw[c]"])
    assert dict(calls.paths) == {"draw[c]/one": 1}


LOOP = """\
id: t.loop
inputs:
  rounds: {schema: {type: integer}, default: 3}
outputs:
  rounds: {type: array}
nodes:
  loop:
    repeat:
      max: inputs.rounds
      carry:
        last: {init: 0, next: round.score}
      body:
        nodes:
          judge: {op: t.score, in: {i: index}}
          note: {op: t.echo, in: {x: "carry.last"}}
        out: {score: judge.out.score}
outputs_from:
  rounds: loop.rounds
"""


def test_repeat_rounds_replay_from_records_while_their_carry_is_unchanged(tmp_path):
    calls = Calls()
    calls.scores = [1, 2, 3, 4]
    runner(tmp_path, LOOP, calls).run()
    assert calls.total() == 6
    calls.reset()
    result = runner(tmp_path, LOOP, calls).run({"rounds": 4})      # the loop node reruns; its rounds reuse
    assert dict(calls.paths) == {"loop[3]/judge": 1, "loop[3]/note": 1}
    assert result.nodes["loop[0]/judge"] == "reused"
    calls.reset()
    calls.scores = [1, 5, 3, 4]
    runner(tmp_path, LOOP, calls).run({"rounds": 4}, force=["loop[1]/judge"])
    assert dict(calls.paths) == {"loop[1]/judge": 1, "loop[2]/note": 1}


SEEDED = """\
id: t.seeded
inputs:
  topic: {type: string}
outputs:
  final: {}
nodes:
  brief: {op: t.echo, in: {x: inputs.topic}}
  plot: {graph: ./plot.graph.yaml, in: {brief: brief.out}}
  script: {op: t.echo, in: {x: plot.out.plot}}
outputs_from:
  final: script.out
"""

PLOT = """\
id: t.plot
inputs:
  brief: {}
outputs:
  plot: {type: string}
nodes:
  write: {op: t.echo, in: {x: inputs.brief}}
outputs_from:
  plot: write.out
"""


def _seeded(tmp_path, calls):
    write(tmp_path / "proj" / "plot.graph.yaml", PLOT)
    return runner(tmp_path, SEEDED, calls)


def test_seeded_nodes_are_reused_and_rerun_when_their_inputs_change(tmp_path):
    calls = Calls()
    r = _seeded(tmp_path, calls)
    r.seed("brief", {"out": "adopted brief"}, {"path": "work.json"}, inputs={"topic": "rain"})
    r.seed("plot", {"out": {"plot": "adopted plot"}}, {"path": "work.json"})
    result = r.run()
    assert dict(calls.paths) == {"script": 1}
    assert result.nodes["brief"] == "reused" and result.nodes["plot"] == "reused"
    assert result.outputs == {"final": "adopted plot"}
    calls.reset()
    changed = r.run({"topic": "snow"})                           # brief's input changed: everything after reruns
    assert dict(calls.paths) == {"brief": 1, "plot/write": 1, "script": 1}
    assert changed.outputs == {"final": "snow"}


def test_an_invalid_seed_is_shown_but_never_reused(tmp_path):
    calls = Calls()
    r = _seeded(tmp_path, calls)
    record = r.seed("brief", {"out": "old"}, {"why": "fails today's contract"}, valid=False,
                    inputs={"topic": "rain"})
    assert record["status"] == "seeded" and record["seed"] == {"source": {"why": "fails today's contract"},
                                                               "valid": False}
    plan = {e.path: e for e in r.plan()}
    assert plan["brief"].action == "run" and plan["brief"].reason == "seed invalid"
    r.run()
    assert calls.paths["brief"] == 1


def test_seed_checks_its_node_kind_order_and_schema(tmp_path):
    r = _seeded(tmp_path, Calls())
    with pytest.raises(GraphError, match="seed 'brief' first"):
        r.seed("plot", {"out": {"plot": "p"}}, {}, inputs={"topic": "rain"})
    with pytest.raises(GraphError, match="not a top-level node"):
        r.seed("plot/write", {"out": "p"}, {})
    with pytest.raises(GraphError, match="missing"):
        r.seed("brief", {}, {}, inputs={"topic": "rain"})
    r.seed("brief", {"out": "b"}, {}, inputs={"topic": "rain"})
    with pytest.raises(GraphError, match="does not match"):
        r.seed("plot", {"out": {"plot": 3}}, {})
    r.seed("plot", {"out": {"plot": 3}}, {}, valid=False)          # an invalid seed is kept as it is


def test_dry_run_plan_lists_reuse_run_and_unknown_and_writes_nothing(tmp_path):
    calls = Calls()
    write(tmp_path / "proj" / "plot.graph.yaml", PLOT)
    r = runner(tmp_path, SEEDED.replace("op: t.echo, in: {x: plot.out.plot}", "op: t.paid, in: {x: plot.out.plot}"),
               calls, services={"spend": object()})
    plan = {e.path: e for e in r.plan({"topic": "rain"})}
    assert plan["brief"].action == "run" and plan["brief"].reason == "no record"
    assert plan["plot"].action == "unknown" and plan["plot/write"].action == "unknown"
    assert plan["script"].paid and plan["script"].action == "unknown"
    assert calls.total() == 0 and r.store.run_ids() == []
    done = r.run({"topic": "rain"})
    plan = {e.path: e for e in r.plan()}
    assert {plan[p].action for p in ("brief", "plot", "script")} == {"reuse"}
    assert plan["brief"].run == done.run_id
    plan = {e.path: e for e in r.plan({"topic": "snow"})}
    assert plan["brief"].reason == "inputs changed" and plan["plot"].action == "unknown"
