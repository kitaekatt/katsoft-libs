"""Services config, concurrency, halt policy, commit contracts and portable record paths."""

from __future__ import annotations

import json
import threading
import time

import pytest

from llm_kit.graph.commands.validate import load_registry
from llm_kit.graph.config import load_config
from llm_kit.graph.human import WAITS_FILE, WaitService
from llm_kit.graph.model import GraphError, Op, Port
from llm_kit.graph.ops import OpHalted, builtin_ops
from llm_kit.graph.records import record_path, resolve_record_path
from llm_kit.graph.run import NodeStopped
from llm_kit.graph.services import Limits, build_services, check_services, op_limit
from llm_kit.graph.spend import SpendService
from test_run_support import Calls, registry, runner, write
from test_services_support import make_ledger, make_project


@pytest.mark.parametrize("section, message", [
    ({"nope": 1}, "unknown services keys"),
    ({"concurrency": {"sound": 0}}, "not an integer of at least 1"),
    ({"concurrency": {"9x": 1}}, "must be a name"),
    ({"retries": 3}, "is not supported"),
    ({"pause_on_halt": "yes"}, "true or false"),
    ({"spend": {"cap": 5}}, "unknown spend keys"),
    ({"spend": {"ledger_env": "A B"}}, "environment variable name"),
    ({"spend": {"guards": ["nodot"]}}, "dotted guard names"),
    ({"spend": {"guards": ["a.b", "a.b"]}}, "twice"),
    ({"spend": {"models": {"row": -1}}}, "at least 0"),
])
def test_bad_services_sections_are_refused(section, message):
    with pytest.raises(GraphError, match=message):
        check_services(section, "services")


def test_the_config_checks_services_and_builds_them(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    with pytest.raises(GraphError, match="unknown services keys"):
        write(root / ".llm-kit" / "graphs.local.yaml", "version: 1\nservices:\n  cap: 5\n")
        load_config(root)
    (root / ".llm-kit" / "graphs.local.yaml").unlink()
    ledger = make_ledger(tmp_path, 2.0)
    monkeypatch.setenv("DEMO_LEDGER", str(ledger.path))
    config = load_config(root)
    with pytest.raises(GraphError, match="load the op registry first"):
        build_services(config, item="it", store=None)
    load_registry(config)
    services = build_services(config, item="it", store=None)
    assert services["pause_on_halt"] is True and services["concurrency"].sizes == {"sound": 1, "model-text": 2}
    assert services["spend"].ledger.cap_usd == 2.0 and [n for n, _ in services["spend"].guards] == ["demo.cap"]
    assert services["waits"].path == root / "state" / WAITS_FILE
    monkeypatch.delenv("DEMO_LEDGER")
    assert build_services(config, item="it", store=None)["spend"].ledger is None


def test_concurrency_holds_one_slot_per_kind(tmp_path):
    active, peak, lock = [0], [0], threading.Lock()

    def slow(ctx, x):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return x

    text = "id: t.c\nnodes:\n  m:\n    map: {over: '[1, 2, 3, 4]', key: item, body: {nodes: {s: {op: t.slow, in: {x: item}}}, out: {v: s.out}}}\n"
    op = Op("t.slow", "1", slow, inputs={"x": Port({})}, limit="sound")
    from llm_kit.graph.loader import load_graph  # noqa: PLC0415
    from llm_kit.graph.records import RunStore  # noqa: PLC0415
    from llm_kit.graph.run import Runner  # noqa: PLC0415

    path = write(tmp_path / "proj" / "c.graph.yaml", text)
    for sizes, expected in (({"sound": 1}, 1), ({"other": 1}, None)):
        r = Runner(load_graph(path), registry(Calls(), op), RunStore(tmp_path / "runs" / str(sizes)), item="it",
                   project_root=tmp_path / "proj", services={"concurrency": Limits(sizes)})
        result = r.run()
        if expected is None:
            assert result.status == "failed" and any("not configured" in m for m in result.errors.values())
        else:
            assert result.status == "done" and peak[0] == expected


def test_llm_model_takes_a_per_node_concurrency_kind():
    model_op = next(op for op in builtin_ops() if op.name == "llm.model")
    assert op_limit(model_op, {}) == "model-text"
    assert op_limit(model_op, {"limit": "model-image"}) == "model-image"
    with pytest.raises(GraphError, match="not a concurrency kind"):
        op_limit(model_op, {"limit": 3})


def test_a_halt_pauses_the_run_unless_pause_on_halt_is_false(tmp_path):
    def halting(ctx, x):
        raise OpHalted("usage limit", halt={"kind": "usage_limit"})
    op = Op("t.halting", "1", halting, inputs={"x": Port({})})
    from llm_kit.graph.loader import load_graph  # noqa: PLC0415
    from llm_kit.graph.records import RunStore  # noqa: PLC0415
    from llm_kit.graph.run import Runner  # noqa: PLC0415

    path = write(tmp_path / "proj" / "h.graph.yaml", "id: t.h\nnodes:\n  h: {op: t.halting, in: {x: 1}}\n")
    for pause, status in ((True, "stopped"), (False, "failed")):
        r = Runner(load_graph(path), registry(Calls(), op), RunStore(tmp_path / "runs" / str(pause)), item="it",
                   project_root=tmp_path / "proj", services={"pause_on_halt": pause})
        assert r.run().status == status


COMMIT = """\
id: t.outer
outer: true
nodes:
  make: {op: t.echo, in: {x: 1}}
  ship:
    commit: {contract: {type: object, required: [n]}, bundle: {n: make.out}, adapter: t.adapter}
"""


def commit_runner(tmp_path, text, calls):
    from llm_kit.graph.loader import load_graph  # noqa: PLC0415
    from llm_kit.graph.records import RunStore  # noqa: PLC0415
    from llm_kit.graph.run import Runner  # noqa: PLC0415

    def ship(ctx, bundle):
        calls.hit(ctx)
        return {"shipped": bundle}
    op = Op("t.ship", "1", ship, inputs={"bundle": Port({})}, commit=True)
    path = write(tmp_path / "proj" / "o.graph.yaml", text.replace("t.adapter", "t.ship"))
    return Runner(load_graph(path), registry(calls, op), RunStore(tmp_path / "runs"), item="it",
                  project_root=tmp_path / "proj")


def test_a_commit_runs_only_when_the_bundle_meets_the_contract(tmp_path):
    calls = Calls()
    r = commit_runner(tmp_path / "ok", COMMIT, calls)
    result = r.run()
    assert result.status == "done" and calls.paths["ship"] == 1
    node = r.store.load_run(result.run_id)["nodes"]["ship"]
    assert r.store.get_value(node["attributes"]["out"]) == {"shipped": {"n": 1}}
    calls = Calls()
    r = commit_runner(tmp_path / "bad", COMMIT.replace("required: [n]", "required: [n, title]"), calls)
    result = r.run()
    node = r.store.load_run(result.run_id)["nodes"]["ship"]
    assert result.status == "failed" and node["error"]["code"] == "contract" and "ship" not in calls.paths
    assert "/title required" in node["error"]["message"]


def test_a_commit_adapter_in_an_op_node_is_refused(tmp_path):
    with pytest.raises(GraphError, match="commit adapter"):
        runner(tmp_path, "id: t.x\nnodes:\n  a: {op: t.adapter, in: {bundle: 1}}\n", Calls())


def test_node_stopped_takes_only_stopped_or_refused():
    assert NodeStopped("x", "y", status="refused").status == "refused"
    with pytest.raises(ValueError):
        NodeStopped("x", "y", status="failed")


def test_records_hold_only_project_relative_paths(tmp_path):
    root = tmp_path / "proj"
    write(root / "prompts" / "p.md", "Say {{x}}.\n")
    write(root / "stages" / "sub.graph.yaml", """\
id: t.sub
inputs:
  n: {schema: {}}
outputs:
  v: {schema: {}}
outputs_from: {v: w.out}
nodes:
  w: {op: t.write, prompt: ../prompts/p.md, slots: {x: inputs.n}}
""")
    text = """\
id: t.main
nodes:
  a: {op: t.echo, in: {x: 1}}
  s: {graph: ./stages/sub.graph.yaml, in: {n: a.out}}
  loop:
    repeat: {max: 2, carry: {c: {init: 0, next: round.v}}, body: {nodes: {e: {op: t.echo, in: {x: carry.c}}}, out: {v: e.out}}}
"""
    r = runner(tmp_path, text, Calls())
    result = r.run()
    assert result.status == "done", result.errors
    raw = r.store.run_path(result.run_id).read_text()
    record = json.loads(raw)
    assert str(root.resolve()) not in raw and str(tmp_path) not in raw and "/private/" not in raw
    files = {entry["file"] for entry in record["graphs"].values()}
    assert files == {"main.graph.yaml", "stages/sub.graph.yaml"}
    assert set(record["files"]) == {"main.graph.yaml", "stages/sub.graph.yaml", "prompts/p.md"}
    assert resolve_record_path(root, "stages/sub.graph.yaml") == (root / "stages" / "sub.graph.yaml").resolve()


def test_record_path_helpers_refuse_machine_paths(tmp_path):
    assert record_path(tmp_path, tmp_path / "a" / "b.yaml") == "a/b.yaml"
    with pytest.raises(GraphError, match="outside the project root"):
        record_path(tmp_path / "proj", tmp_path / "other.yaml")
    for bad in ("/etc/passwd", "../x", "a/../../x", "C:/x", "a\\b", ""):
        with pytest.raises(GraphError):
            resolve_record_path(tmp_path, bad)


def test_the_viewer_fixture_records_hold_no_machine_paths():
    from pathlib import Path  # noqa: PLC0415

    runs = Path(__file__).resolve().parent / "fixtures" / "viewer" / "project" / "work"
    for path in runs.rglob("*.json"):
        text = path.read_text()
        assert "/Users/" not in text and "/home/" not in text and "/private/" not in text, path


def test_a_wait_service_without_runs_state_is_refused(tmp_path):
    root = make_project(tmp_path, config="version: 1\nruns:\n  dir: work/{item}/graph\n")
    with pytest.raises(GraphError, match="runs.state is not configured"):
        WaitService.for_item(load_config(root), "it")
    assert "waits" not in build_services(load_config(root), item="it", store=None)
    assert SpendService().ledger is None


def test_public_surface_and_lazy_exports_list_the_services_names():
    import importlib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    import llm_kit.graph as graph  # noqa: PLC0415

    surface = Path(__file__).resolve().parents[3] / "packages" / "katsoft-llm-kit" / "public-surface.json"
    names = {e["name"] for e in json.loads(surface.read_text(encoding="utf-8"))["entries"]}
    for module in ("services", "spend", "human", "commit"):
        for name in importlib.import_module(f"llm_kit.graph.{module}").__all__:
            assert f"llm_kit.graph.{module}.{name}" in names, name
            assert getattr(graph, name) is getattr(importlib.import_module(f"llm_kit.graph.{module}"), name)
    assert {"record_path", "resolve_record_path"} <= set(dir(graph))
