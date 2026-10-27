"""Shared helpers for the runtime services tests (test_services_*.py); holds no tests.

:func:`make_project` writes a graph project with a ``demo`` op namespace: a
plain op, a paid op, an op that halts once, a commit adapter and a cap guard,
plus two graphs (``demo.main`` with a halt, a human wait, a paid call and a
commit; ``demo.paid`` with one paid call).
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Optional

from llm_kit.cli.graph import run as graph_cli
from llm_kit.pipeline.runtime.spend_ledger import create_ledger

OPS = '''\
import json
import os
import time

from llm_kit.graph.ops import OpHalted, OpResult, op
from llm_kit.graph.spend import SpendRefused, cap_guard


@op("demo.make", version="1", inputs={"x": {}})
def make(ctx, x):
    return x


@op("demo.sound", version="1", inputs={"x": {}}, paid=True, limit="sound",
    estimate=lambda ctx, x: float(os.environ.get("DEMO_ESTIMATE", "0.6")))
def sound(ctx, x):
    time.sleep(float(os.environ.get("DEMO_SLEEP", "0")))
    with open(ctx.project_root / "sound.calls", "a", encoding="ascii") as stream:
        stream.write(ctx.run_id + "\\n")
    return OpResult(out=x, receipt={"cost": {"usd": 0.5}})


@op("demo.flaky", version="1", inputs={"x": {}})
def flaky(ctx, x):
    flag = ctx.project_root / "halted.flag"
    if not flag.exists():
        flag.write_text("1", encoding="ascii")
        raise OpHalted("usage limit reached", halt={"kind": "usage_limit"})
    return x


@op("demo.ship", version="1", inputs={"bundle": {}}, outputs={"type": "object"}, commit=True)
def ship(ctx, bundle):
    path = ctx.project_root / "shipped.json"
    count = json.loads(path.read_text())["count"] if path.exists() else 0
    path.write_text(json.dumps({"count": count + 1, "bundle": bundle}), encoding="ascii")
    return {"shipped": True}


class Guard:
    def __init__(self, setup):
        self.setup = setup

    def admit(self, charge):
        if os.environ.get("DEMO_GUARD_REFUSE"):
            raise SpendRefused("the moment cap is spent")
        return "token"

    def settle(self, token, charge, cost_usd):
        with open(self.setup.project_root / "guard.log", "a", encoding="ascii") as stream:
            stream.write(f"settle {charge.node_path} {cost_usd}\\n")

    def release(self, token, charge):
        with open(self.setup.project_root / "guard.log", "a", encoding="ascii") as stream:
            stream.write(f"release {charge.node_path}\\n")


@cap_guard("demo.cap")
def cap(setup):
    return Guard(setup)
'''

MAIN = """\
id: demo.main
outer: true
inputs:
  topic: {schema: {type: string}}
nodes:
  make: {op: demo.make, in: {x: inputs.topic}}
  flaky: {op: demo.flaky, in: {x: make.out}}
  played:
    human:
      kind: played
      schema: {type: object, required: [ok], additionalProperties: false, properties: {ok: {type: boolean}}}
      payload: {made: flaky.out}
  sound: {op: demo.sound, in: {x: played.out}}
  ship:
    commit:
      contract: {type: object, required: [topic, played]}
      bundle: {topic: make.out, played: sound.out}
      adapter: demo.ship
"""

PAID = """\
id: demo.paid
inputs:
  topic: {schema: {type: string}}
nodes:
  make: {op: demo.make, in: {x: inputs.topic}}
  sound: {op: demo.sound, in: {x: make.out}}
  after: {op: demo.make, in: {x: sound.out}}
"""

CONFIG = """\
version: 1
namespaces:
  demo: ops
graphs:
  demo.main: main.graph.yaml
  demo.paid: paid.graph.yaml
runs:
  dir: work/{item}/graph
  state: state
services:
  concurrency: {sound: 1, model-text: 2}
  retries: none
  spend:
    ledger_env: DEMO_LEDGER
    guards: [demo.cap]
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_project(tmp_path: Path, *, config: str = CONFIG) -> Path:
    root = tmp_path / "proj"
    write(root / ".llm-kit" / "graphs.yaml", config)
    write(root / "ops" / "__init__.py", "")
    write(root / "ops" / "demo.py", OPS)
    write(root / "main.graph.yaml", MAIN)
    write(root / "paid.graph.yaml", PAID)
    return root.resolve()


def make_ledger(tmp_path: Path, cap_usd: float, name: str = "spend.sqlite"):
    return create_ledger(tmp_path / name, cap_usd=cap_usd, run_id="test-campaign")


def cli(*argv: str) -> tuple[int, str, str]:
    """Run ``llm-kit graph <argv>`` in this process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = graph_cli(list(argv))
    return code, out.getvalue(), err.getvalue()


def cli_json(*argv: str) -> tuple[int, Optional[dict]]:
    code, out, err = cli(*argv, "--json")
    return code, (json.loads(out) if out.strip() else None)
