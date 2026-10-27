"""End-to-end: halt and resume, a human wait answered through the CLI, a paid call under the
ledger cap, a commit node, and two processes on one ledger. No paid or live calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from llm_kit.graph.records import RunStore
from llm_kit.pipeline.runtime.spend_ledger import open_ledger
from test_services_support import cli, cli_json, make_ledger, make_project

REPO = Path(__file__).resolve().parents[3]


def test_halt_wait_answer_resume_pay_and_commit(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    ledger = make_ledger(tmp_path, 1.0)
    monkeypatch.setenv("DEMO_LEDGER", str(ledger.path))
    project = ("--project", str(root))

    code, result = cli_json("run", "demo.main", "--item", "it", "--input", 'topic="rain"', *project)
    assert code == 3 and result["stop"]["reason"] == "halted" and result["nodes"]["flaky"] == "stopped"
    assert result["nodes"]["make"] == "done" and result["nodes"]["ship"] == "pending"

    code, result = cli_json("resume", "demo.main", "--item", "it", *project)
    assert code == 3 and result["stop"]["reason"] == "waiting" and result["stop"]["node"] == "played"
    assert result["nodes"]["make"] == "reused" and result["nodes"]["flaky"] == "done"
    assert result["nodes"]["sound"] == "pending" and not (root / "sound.calls").exists()
    waiting_run = result["run_id"]

    code, out, err = cli("resume", "demo.main", "--item", "it", *project)
    assert code == 1 and "still waits for an answer" in err

    code, listed = cli_json("answer", "demo.main", "--item", "it", *project)
    assert code == 0 and [w["node"] for w in listed] == ["played"]
    assert listed[0]["payload"] == {"made": "rain"} and listed[0]["run"] == waiting_run

    code, out, err = cli("answer", "demo.main", "--item", "it", "--node", "played", "--value", '{"ok": "yes"}',
                         *project)
    assert code == 1 and "does not match the node schema" in err
    code, out, err = cli("answer", "demo.main", "--item", "it", "--node", "nope", "--value", "{}", *project)
    assert code == 1 and "no open wait for node 'nope'" in err
    code, out, err = cli("answer", "demo.main", "--item", "it", "--node", "played", "--value", '{"ok": true}',
                         *project)
    assert code == 0 and "llm-kit graph resume" in out

    code, result = cli_json("resume", "demo.main", "--item", "it", *project)
    assert code == 0 and result["status"] == "done"
    assert result["nodes"]["played"] == "done" and result["nodes"]["ship"] == "done"
    store = RunStore(root / "work" / "it" / "graph")
    record = store.load_run(result["run_id"])
    assert record["resumed_from"] == waiting_run
    assert store.get_value(record["nodes"]["played"]["attributes"]["out"]) == {"ok": True}
    assert record["nodes"]["played"]["wait"]["answered"] is True
    spend = record["nodes"]["sound"]["spend"]
    assert spend["state"] == "settled" and spend["estimate_usd"] == 0.6 and spend["cost_usd"] == 0.5
    assert spend["guards"] == ["demo.cap"]
    assert open_ledger(ledger.path).status().settled_usd == 0.5
    assert json.loads((root / "shipped.json").read_text()) == {"count": 1,
                                                               "bundle": {"topic": "rain", "played": {"ok": True}}}
    assert (root / "guard.log").read_text() == "settle sound 0.5\n"

    code, result = cli_json("run", "demo.main", "--item", "it", *project)
    assert code == 0 and set(result["nodes"].values()) == {"reused"}
    assert json.loads((root / "shipped.json").read_text())["count"] == 1
    assert len((root / "sound.calls").read_text().split()) == 1

    code, out, err = cli("resume", "demo.main", "--item", "it", *project)
    assert code == 1 and "only a stopped run resumes" in err


def test_a_forced_human_node_asks_again(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("DEMO_LEDGER", str(make_ledger(tmp_path, 5.0).path))
    (root / "halted.flag").write_text("1")
    project = ("--project", str(root))
    assert cli("run", "demo.main", "--item", "it", "--input", 'topic="rain"', *project)[0] == 3
    assert cli("answer", "demo.main", "--item", "it", "--node", "played", "--value", '{"ok": true}', *project)[0] == 0
    assert cli("resume", "demo.main", "--item", "it", *project)[0] == 0
    code, result = cli_json("run", "demo.main", "--item", "it", "--force", "played", *project)
    assert code == 3 and result["stop"]["reason"] == "waiting"
    assert cli("answer", "demo.main", "--item", "it", "--node", "played", "--value", '{"ok": false}', *project)[0] == 0
    code, result = cli_json("resume", "demo.main", "--item", "it", *project)
    assert code == 0 and result["nodes"]["sound"] == "done"
    store = RunStore(root / "work" / "it" / "graph")
    record = store.load_run(result["run_id"])
    assert store.get_value(record["nodes"]["played"]["attributes"]["out"]) == {"ok": False}
    assert record["nodes"]["played"]["wait"]["unit"].endswith("#2")


def test_a_refusal_stops_dependents_and_reports_done_and_remaining(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    ledger = make_ledger(tmp_path, 0.5)
    monkeypatch.setenv("DEMO_LEDGER", str(ledger.path))
    code, out, err = cli("run", "demo.paid", "--item", "it", "--input", 'topic="rain"', "--project", str(root))
    assert code == 3, err
    assert "refused  sound" in out and "stopped  sound: spend_cap:" in out
    assert "done: make" in out and "remaining: sound, after" in out
    assert "ledger: $0.000000 of $0.50; $0.500000 remaining" in out
    assert not (root / "sound.calls").exists()
    assert open_ledger(ledger.path).status().outstanding_usd == 0

    monkeypatch.setenv("DEMO_ESTIMATE", "0.4")
    monkeypatch.setenv("DEMO_GUARD_REFUSE", "1")
    code, result = cli_json("run", "demo.paid", "--item", "it", "--project", str(root))
    assert code == 3 and result["nodes"]["sound"] == "refused" and result["stop"]["refused_by"] == "demo.cap"
    assert "guard demo.cap refused: the moment cap is spent" in result["stop"]["message"]
    assert open_ledger(ledger.path).status().outstanding_usd == 0 and not (root / "sound.calls").exists()


def test_the_dry_run_lists_paid_nodes_with_their_estimates(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("DEMO_LEDGER", str(make_ledger(tmp_path, 1.0).path))
    code, out, err = cli("run", "demo.paid", "--item", "it", "--input", 'topic="rain"', "--project", str(root),
                         "--dry-run")
    assert code == 0 and "run     make (no record)" in out
    assert "unknown sound" in out and "[paid]" in out
    assert cli("run", "demo.paid", "--item", "it", "--target", "make", "--input", 'topic="rain"', "--project",
               str(root))[0] == 0
    code, out, err = cli("run", "demo.paid", "--item", "it", "--project", str(root), "--dry-run")
    assert "reuse   make" in out and "run     sound (no record) [paid $0.600000]" in out
    assert not (root / "sound.calls").exists()


def test_two_processes_on_one_ledger_admit_only_what_fits(tmp_path):
    root = make_project(tmp_path)
    ledger = make_ledger(tmp_path, 1.0)
    env = {**os.environ, "DEMO_LEDGER": str(ledger.path), "DEMO_SLEEP": "1.5",
           "PYTHONPATH": os.pathsep.join([str(REPO / "packages" / "katsoft-llm-kit" / "src"),
                                          str(REPO / "packages" / "katsoft-contracts" / "src")])}
    code = "import sys; from llm_kit.cli.graph import run; sys.exit(run(sys.argv[1:]))"
    procs = [subprocess.Popen([sys.executable, "-c", code, "run", "demo.paid", "--item", item, "--input",
                               'topic="rain"', "--project", str(root), "--json"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for item in ("a", "b")]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode in (0, 3), err
        results.append(json.loads(out))
    statuses = sorted(r["nodes"]["sound"] for r in results)
    assert statuses == ["done", "refused"]
    status = open_ledger(ledger.path).status()
    assert status.settled_usd == 0.5 and status.outstanding_usd == 0.5 and status.reservations_open == 0


def test_an_item_that_escapes_the_project_is_refused_before_the_wait_store_opens(tmp_path):
    from llm_kit.graph.config import load_config
    from llm_kit.graph.model import GraphError
    from llm_kit.graph.human import WaitService
    from test_services_support import CONFIG

    root = make_project(tmp_path, config=CONFIG.replace("state: state", "state: state/{item}"))
    project = ("--project", str(root))
    code, out, err = cli("answer", "demo.main", "--item", "../../x", "--node", "played", "--value", "{}", *project)
    assert code == 1 and "must match" in err
    assert not (tmp_path.parent / "x").exists() and not list(tmp_path.rglob("waits.sqlite"))
    config = load_config(root)
    try:
        WaitService.for_item(config, "../../x")
    except GraphError as error:
        assert "must match" in str(error)
    else:
        raise AssertionError("for_item accepted an escaping item")
