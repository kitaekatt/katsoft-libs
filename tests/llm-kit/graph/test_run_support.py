"""Shared helpers for the llm_kit.graph runtime tests (test_run_*.py); holds no tests.

Ops are built in the test (the generic ops of llm_kit.graph.ops are a separate
unit): :func:`registry` returns an :class:`OpRegistry` of small ``t.*`` ops
that count their calls in a :class:`Calls`.
"""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from llm_kit.graph.loader import load_graph, prompt_body
from llm_kit.graph.model import Op, OpRegistry, OpResult, ParamSpec, Port
from llm_kit.graph.records import RunStore
from llm_kit.graph.run import NodeStopped, Runner

NUMBER = {"type": "number"}
ANY: dict = {}


class Calls:
    """Op calls by node path, thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.paths: Counter = Counter()
        self.order: list[str] = []
        self.barrier: Optional[threading.Barrier] = None
        self.scores: list[Any] = []
        self.fail_keys: set = set()

    def hit(self, ctx) -> None:
        with self.lock:
            self.paths[ctx.node_path] += 1
            self.order.append(ctx.node_path)

    def total(self) -> int:
        return sum(self.paths.values())

    def reset(self) -> None:
        with self.lock:
            self.paths.clear()
            self.order.clear()


def registry(calls: Calls, *extra: Op) -> OpRegistry:
    def add(ctx, a, b):
        calls.hit(ctx)
        return a + b

    def echo(ctx, x):
        calls.hit(ctx)
        return x

    def wait(ctx, x):
        calls.hit(ctx)
        calls.barrier.wait()
        return x

    def fail(ctx, x):
        calls.hit(ctx)
        raise ValueError(f"boom {x}")

    def fail_some(ctx, x):
        calls.hit(ctx)
        if x in calls.fail_keys:
            raise ValueError(f"boom {x}")
        return x

    def score(ctx, i):
        calls.hit(ctx)
        return {"score": calls.scores[i]}

    def stop(ctx, x):
        calls.hit(ctx)
        raise NodeStopped("halted", "usage limit")

    def write(ctx, slots):
        calls.hit(ctx)
        text = prompt_body(Path(ctx.params["prompt"]).read_text(encoding="utf-8"))
        for name, value in slots.items():
            text = text.replace("{{" + name + "}}", str(value))
        return OpResult(out={"draft": text}, attributes={"sent": text, "text": text.upper()},
                        receipt={"model": "fake", "cost": {"usd": 0}})

    def picture(ctx, x):
        calls.hit(ctx)
        ref = ctx.store.put_media(f"picture {x}".encode(), ".png")
        return OpResult(out={"image": ref}, media=(ref,))

    def bad_out(ctx, x):
        calls.hit(ctx)
        return {"n": "not a number"}

    def write_slots(ctx, **_):
        raise AssertionError("not called")

    ops = [
        Op("t.add", "1", add, inputs={"a": Port(NUMBER), "b": Port(NUMBER, default=0)}, outputs=NUMBER),
        Op("t.echo", "1", echo, inputs={"x": Port(ANY)}),
        Op("t.wait", "1", wait, inputs={"x": Port(ANY)}),
        Op("t.fail", "1", fail, inputs={"x": Port(ANY)}),
        Op("t.fail_some", "1", fail_some, inputs={"x": Port(ANY)}),
        Op("t.score", "1", score, inputs={"i": Port({"type": "integer"})},
           outputs={"type": "object", "properties": {"score": {"type": "integer"}}}),
        Op("t.stop", "1", stop, inputs={"x": Port(ANY)}),
        Op("t.write", "1", lambda ctx, **kw: write(ctx, ctx.params.get("slots", {})),
           params={"prompt": ParamSpec("template", required=True, slots="slots"), "slots": ParamSpec("exprs")},
           attributes=frozenset({"out", "sent", "text"})),
        Op("t.picture", "1", picture, inputs={"x": Port(ANY)}),
        Op("t.bad_out", "1", bad_out, inputs={"x": Port(ANY)},
           outputs={"type": "object", "properties": {"n": {"type": "number"}}}),
        Op("t.paid", "1", echo, inputs={"x": Port(ANY)}, paid=True),
        Op("t.adapter", "1", echo, inputs={"bundle": Port(ANY)}, commit=True),
        *extra,
    ]
    return OpRegistry({op.name: op for op in ops})


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def runner(tmp_path: Path, graph_text: str, calls: Calls, *, item: str = "it", name: str = "main.graph.yaml",
           services=None, max_parallel: int = 8, store: Optional[RunStore] = None) -> Runner:
    """A runner for ``graph_text`` written under ``tmp_path/proj``; records under ``tmp_path/runs/<item>``."""
    root = tmp_path / "proj"
    path = write(root / name, graph_text)
    store = store or RunStore(tmp_path / "runs" / item)
    return Runner(load_graph(path), registry(calls), store, item=item, project_root=root, services=services,
                  max_parallel=max_parallel)


def top(result) -> dict:
    """Top-level node statuses of a RunResult."""
    return {p: s for p, s in result.nodes.items() if "/" not in p and "[" not in p}
