"""Spend admission and settlement: the ledger, guard order, settle, release, unknown, overbilled,
unreserved, missing estimates and llm.model estimates by catalogue row. No paid calls."""

from __future__ import annotations

import pytest

from llm_kit.graph.model import GraphError, Op, Port
from llm_kit.graph.ops import OpFailed, OpResult, builtin_ops
from llm_kit.graph.spend import SpendRefused, SpendService, cost_from_receipt
from llm_kit.graph.testing import make_project, op_context
from llm_kit.graph.loader import load_graph
from llm_kit.graph.records import RunStore
from llm_kit.graph.run import Runner
from test_run_support import Calls, registry, write
from test_services_support import make_ledger

GRAPH = """\
id: t.spend
nodes:
  a: {op: t.echo, in: {x: 1}}
  p: {op: t.cost, in: {x: a.out}}
  after: {op: t.echo, in: {x: p.out}}
"""


def cost_op(calls: Calls, behaviour, estimate=0.25) -> Op:
    def fn(ctx, x):
        calls.hit(ctx)
        return behaviour(ctx)
    return Op("t.cost", "1", fn, inputs={"x": Port({})}, paid=True,
              estimate=(lambda ctx, x: estimate) if estimate is not None else None)


def runner(tmp_path, calls, op, services):
    path = write(tmp_path / "proj" / "main.graph.yaml", GRAPH)
    return Runner(load_graph(path), registry(calls, op), RunStore(tmp_path / "runs" / "it"), item="it",
                  project_root=tmp_path / "proj", services=services)


def run(tmp_path, behaviour, *, estimate=0.25, cap=1.0, guards=(), ledger=True):
    calls = Calls()
    led = make_ledger(tmp_path, cap) if ledger else None
    service = SpendService(ledger=led, guards=guards)
    r = runner(tmp_path, calls, cost_op(calls, behaviour, estimate), {"spend": service})
    result = r.run()
    record = r.store.load_run(result.run_id)
    return result, record, led, calls


def test_a_returned_call_settles_with_the_receipt_cost(tmp_path):
    result, record, ledger, _ = run(tmp_path, lambda ctx: OpResult(out=2, receipt={"cost": {"usd": 0.2}}))
    assert result.status == "done"
    assert record["nodes"]["p"]["spend"] == {"estimate_usd": 0.25, "estimate_source": "op t.cost", "model": None,
                                             "reservation": record["nodes"]["p"]["spend"]["reservation"],
                                             "guards": [], "state": "settled", "cost_usd": 0.2}
    status = ledger.status()
    assert status.settled_usd == 0.2 and status.reservations_open == 0


def test_a_call_that_sent_nothing_releases_and_one_that_raised_holds_the_estimate(tmp_path):
    def refused(ctx):
        raise OpFailed("refused before dispatch", code="refused-selection")
    result, record, ledger, _ = run(tmp_path / "a", refused)
    assert result.status == "failed" and record["nodes"]["p"]["spend"]["state"] == "released"
    assert ledger.status().outstanding_usd == 0

    def boom(ctx):
        raise ValueError("lost the response")
    result, record, ledger, _ = run(tmp_path / "b", boom)
    assert result.status == "failed" and record["nodes"]["p"]["spend"]["state"] == "unknown"
    assert ledger.status().unknown_usd == 0.25 and record["nodes"]["after"]["status"] == "gated"

    result, record, ledger, _ = run(tmp_path / "c", lambda ctx: OpResult(out=1, receipt={"model": "x"}))
    assert result.status == "done" and record["nodes"]["p"]["spend"]["state"] == "unknown"


def test_an_overbilled_call_keeps_its_result_and_stops_the_run(tmp_path):
    result, record, ledger, calls = run(tmp_path, lambda ctx: OpResult(out=1, receipt={"cost": {"usd": 0.9}}))
    assert result.status == "stopped" and result.stop["reason"] == "spend_overbilled"
    assert result.nodes["p"] == "done" and result.nodes["after"] == "pending"
    assert record["nodes"]["p"]["spend"]["state"] == "overbilled" and ledger.status().halted


def test_a_cost_on_a_call_estimated_at_zero_halts_the_ledger(tmp_path):
    result, record, ledger, _ = run(tmp_path, lambda ctx: OpResult(out=1, receipt={"cost": {"usd": 0.1}}),
                                    estimate=0)
    assert result.stop["reason"] == "spend_unreserved" and record["nodes"]["p"]["spend"]["state"] == "unreserved"
    assert ledger.status().halted and ledger.status().halt_reason == "unreserved"
    result, record, _, _ = run(tmp_path / "free", lambda ctx: OpResult(out=1, receipt={"cost": {"class": "subscription"}}),
                               estimate=0, ledger=False)
    assert result.status == "done" and record["nodes"]["p"]["spend"]["state"] == "free"


def test_refusals_happen_before_the_call(tmp_path):
    for name, kwargs, reason in (("cap", {"cap": 0.1}, "spend_cap"), ("noledger", {"ledger": False}, "no_ledger"),
                                 ("noestimate", {"estimate": None}, "no_estimate")):
        result, record, _, calls = run(tmp_path / name, lambda ctx: 1, **kwargs)
        assert result.status == "stopped" and result.stop["reason"] == reason, name
        assert result.nodes["p"] == "refused" and result.nodes["after"] == "pending" and "p" not in calls.paths


class Recorder:
    def __init__(self, name, log, ledger, refuse=False):
        self.name, self.log, self.ledger, self.refuse = name, log, ledger, refuse

    def admit(self, charge):
        self.log.append(("admit", self.name, self.ledger.status().reserved_usd))
        if self.refuse:
            raise SpendRefused("no", detail={"moment": "night"})
        return self.name

    def settle(self, token, charge, cost):
        self.log.append(("settle", token, cost))

    def release(self, token, charge):
        self.log.append(("release", token))


def test_the_ledger_admits_first_then_the_guards_in_order(tmp_path):
    log: list = []
    ledger = make_ledger(tmp_path, 1.0, "guards.sqlite")
    guards = [("one", Recorder("one", log, ledger)), ("two", Recorder("two", log, ledger, refuse=True))]
    calls = Calls()
    r = runner(tmp_path, calls, cost_op(calls, lambda ctx: 1), {"spend": SpendService(ledger=ledger, guards=guards)})
    result = r.run()
    assert log == [("admit", "one", 0.25), ("admit", "two", 0.25), ("release", "one")]
    assert result.stop["refused_by"] == "two" and result.stop["moment"] == "night"
    assert ledger.status().outstanding_usd == 0 and "p" not in calls.paths

    log.clear()
    guards[1][1].refuse = False
    r = runner(tmp_path / "again", calls, cost_op(calls, lambda ctx: OpResult(out=1, receipt={"cost": {"usd": 0.1}})),
               {"spend": SpendService(ledger=ledger, guards=guards)})
    assert r.run().status == "done"
    assert log[-2:] == [("settle", "one", 0.1), ("settle", "two", 0.1)]


def test_llm_model_estimates_come_from_the_catalogue_row(tmp_path):
    project = make_project(tmp_path, ["draft.write"])
    model_op = next(op for op in builtin_ops() if op.name == "llm.model")
    ctx = op_context(project.root, params={"model": "draft.write"})
    with project.environment():
        assert SpendService(models={"mock-row": 0.01}).estimate(model_op, ctx, {}) == \
            (0.01, "services.spend.models.mock-row", "mock-row")
        with pytest.raises(SpendRefused, match="no estimate for model row 'mock-row'") as refused:
            SpendService().estimate(model_op, ctx, {})
        assert refused.value.reason == "no_estimate" and refused.value.status == "refused"
        with pytest.raises(GraphError, match="not in the project's model file"):
            SpendService(models={"mock-row": 0}).estimate(model_op, op_context(project.root, params={"model": "x"}),
                                                          {})


def test_receipt_costs():
    assert cost_from_receipt({"cost": {"usd": 0.3}}) == 0.3
    assert cost_from_receipt({"cost": {"class": "local", "usd": None}}) == 0.0
    assert cost_from_receipt({"cost": {"class": "metered", "usd": None}}) is None
    assert cost_from_receipt(None) is None
    with pytest.raises(GraphError, match="not a USD amount"):
        cost_from_receipt({"cost": {"usd": -1}})


def test_a_bad_estimate_fails_the_node(tmp_path):
    result, record, _, calls = run(tmp_path, lambda ctx: 1, estimate=float("nan"))
    assert result.status == "failed" and "not a USD amount" in result.errors["p"] and "p" not in calls.paths


def test_spend_service_must_be_a_spend_service(tmp_path):
    calls = Calls()
    result = runner(tmp_path, calls, cost_op(calls, lambda ctx: 1), {"spend": object()}).run()
    assert result.status == "failed" and "must be a SpendService" in result.errors["p"]


def test_a_spend_refusal_after_a_wait_is_the_run_stop_reason(tmp_path):
    """A wait stops the run first; a drained node then refused by the cap must still report the spend stop."""
    import time

    from llm_kit.graph.run import NodeStopped

    def wait(ctx, x):
        raise NodeStopped("waiting", "waits for an answer")

    def slow(ctx, x):
        time.sleep(0.3)  # the wait node has stopped by now
        return x

    graph = ("id: t.waitspend\nnodes:\n"
             "  w: {op: t.wait, in: {x: 1}}\n"
             "  s: {op: t.slow, in: {x: 1}}\n"
             "  p: {op: t.cost, in: {x: s.out}}\n")
    calls = Calls()
    ops = registry(calls, cost_op(calls, lambda ctx: 1),
                   Op("t.wait", "1", wait, inputs={"x": Port({})}),
                   Op("t.slow", "1", slow, inputs={"x": Port({})}))
    path = write(tmp_path / "proj" / "main.graph.yaml", graph)
    service = SpendService(ledger=make_ledger(tmp_path, 0.1))
    r = Runner(load_graph(path), ops, RunStore(tmp_path / "runs" / "it"), item="it",
               project_root=tmp_path / "proj", services={"spend": service})
    result = r.run()
    assert result.nodes["w"] == "stopped" and result.nodes["p"] == "refused"
    assert result.status == "stopped"
    assert result.stop["reason"] == "spend_cap" and result.stop["node"] == "p"
    assert r.store.load_run(result.run_id)["stop"]["reason"] == "spend_cap"
