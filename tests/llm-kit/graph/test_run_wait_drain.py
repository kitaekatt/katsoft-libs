"""A human wait stops only what reads the waiting node; the independent rest of the run finishes.

Also: a run record names no machine path (the project root is relative, the home directory is ``~``).
"""

from __future__ import annotations

import json
from pathlib import Path

from llm_kit.graph.portable import PathScrubber
from llm_kit.graph.records import RunStore
from test_services_support import cli_json, write

OPS = '''\
import time

from llm_kit.graph.ops import OpResult, op


@op("w.make", version="1", inputs={"x": {}})
def make(ctx, x):
    return x


@op("w.slow", version="1", inputs={"x": {}})
def slow(ctx, x):
    time.sleep(0.4)  # the human node has stopped by now
    return x


@op("w.noisy", version="1", inputs={"x": {}})
def noisy(ctx, x):
    here = str(ctx.project_root)
    home = str(__import__("pathlib").Path.home())
    return OpResult(out=x, receipt={
        "cwd": here, "argv": ["node", here + "/tools/run.mjs", home + "/.cache/x"],
        "stderr": "warn: " + here + "/a.txt and " + home + "/b.txt (" + home + ")",
    })
'''

SIDE = """\
id: w.side
inputs:
  x: {schema: {}}
outputs:
  done: {schema: {}}
nodes:
  first: {op: w.slow, in: {x: inputs.x}}
  second: {op: w.make, in: {x: first.out}}
outputs_from:
  done: second.out
"""

MAIN = """\
id: w.main
outer: true
inputs:
  topic: {schema: {type: string}}
outputs:
  after: {schema: {}}
nodes:
  make: {op: w.make, in: {x: inputs.topic}}
  played:
    human:
      kind: played
      schema: {type: object, required: [ok], additionalProperties: false, properties: {ok: {type: boolean}}}
      payload: {made: make.out}
  after: {op: w.make, in: {x: played.out}}
  side:
    graph: ./side.graph.yaml
    in: {x: make.out}
  loud: {op: w.noisy, in: {x: make.out}}
outputs_from:
  after: after.out
"""

CONFIG = """\
version: 1
namespaces:
  w: ops
graphs:
  w.main: main.graph.yaml
runs:
  dir: work/{item}/graph
  state: state
"""


def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root / ".llm-kit" / "graphs.yaml", CONFIG)
    write(root / "ops" / "__init__.py", "")
    write(root / "ops" / "w.py", OPS)
    write(root / "main.graph.yaml", MAIN)
    write(root / "side.graph.yaml", SIDE)
    return root.resolve()


def test_a_human_wait_lets_the_independent_subgraph_finish(tmp_path):
    root = project(tmp_path)
    code, result = cli_json("run", "w.main", "--item", "it", "--input", 'topic="rain"', "--project", str(root))
    assert code == 3 and result["stop"]["reason"] == "waiting" and result["stop"]["node"] == "played"
    nodes = result["nodes"]
    assert nodes["played"] == "stopped"
    assert nodes["side"] == "done" and nodes["side/first"] == "done" and nodes["side/second"] == "done"
    assert nodes["loud"] == "done"
    assert nodes["after"] == "pending"  # reads the waiting node: it waits

    code, result = cli_json("answer", "w.main", "--item", "it", "--node", "played", "--value", '{"ok": true}',
                            "--project", str(root))
    code, result = cli_json("resume", "w.main", "--item", "it", "--project", str(root))
    assert code == 0 and result["status"] == "done"
    assert result["nodes"]["side"] == "reused" and result["nodes"]["after"] == "done"


def test_a_run_record_names_no_machine_path(tmp_path, monkeypatch):
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root = project(tmp_path)
    code, result = cli_json("run", "w.main", "--item", "it", "--input", 'topic="rain"', "--project", str(root))
    assert result["nodes"]["loud"] == "done"
    store = RunStore(root / "work" / "it" / "graph")
    text = store.run_path(result["run_id"]).read_text(encoding="utf-8")
    assert str(root) not in text and str(home) not in text and str(tmp_path) not in text
    receipt = store.load_run(result["run_id"])["nodes"]["loud"]["receipt"]
    assert receipt["cwd"] == "."
    assert receipt["argv"] == ["node", "tools/run.mjs", "~/.cache/x"]
    assert receipt["stderr"] == "warn: a.txt and ~/b.txt (~)"
    assert json.loads(text)["stop"]["reason"] == "waiting"


def test_scrubber_replaces_only_at_a_path_boundary(tmp_path):
    root, home = tmp_path / "proj", tmp_path / "home"
    scrub = PathScrubber(root, home)
    assert scrub.text(f"{root}") == "."
    assert scrub.text(f"in {root}/a/b.py:3") == "in a/b.py:3"
    assert scrub.text(f"{home}/x") == "~/x" and scrub.text(f"{home}") == "~"
    assert scrub.text(f"{root}2/a") == f"{root}2/a"
    assert scrub.text(f"/mnt{root}/a") == f"/mnt{root}/a"
    assert scrub.scrub({"a": [f"{root}/z", 1, None]}) == {"a": ["z", 1, None]}
