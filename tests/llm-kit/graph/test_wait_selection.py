"""Human waits: a new wait supersedes the older open waits of its node path; ``answer`` and ``resume``
choose one wait by unit id or run; a stale wait is dismissed. No paid or live calls."""

from __future__ import annotations

import pytest
from llm_kit.graph.config import load_config
from llm_kit.graph.human import WaitService, resolution_kind
from llm_kit.graph.records import RunStore
from test_services_support import cli, cli_json, make_ledger, make_project

ITEM = "it"


def _setup(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("DEMO_LEDGER", str(make_ledger(tmp_path, 5.0).path))
    (root / "halted.flag").write_text("1")          # the halting op passes
    return root, ("--project", str(root))


def _wait_for(root, topic):
    """Run the graph with ``topic`` until the human node waits; return the run id."""
    code, result = cli_json("run", "demo.main", "--item", ITEM, "--input", f'topic="{topic}"',
                            "--project", str(root))
    assert code == 3 and result["stop"]["reason"] == "waiting"
    return result["run_id"]


def _service(root):
    return WaitService.for_item(load_config(root), ITEM)


def _open(root):
    return {w.unit: w for w in _service(root).open_waits(ITEM)}


def _closed(root):
    return {c["unit"]: c for c in _service(root).closed_waits(ITEM)}


def _stale_second_wait(root, monkeypatch, topic):
    """A second open wait for the node, as left by a build that did not supersede."""
    with monkeypatch.context() as patch:
        patch.setattr(WaitService, "_supersede", lambda self, *args, **kwargs: [])
        return _wait_for(root, topic)


def test_a_new_wait_for_the_node_supersedes_the_older_one(tmp_path, monkeypatch):
    root, project = _setup(tmp_path, monkeypatch)
    first = _wait_for(root, "rain")
    (old_unit,) = _open(root)
    second = _wait_for(root, "snow")                 # other inputs: another digest, another unit
    (new_unit,) = _open(root)
    assert new_unit != old_unit

    closed = _closed(root)
    assert closed[old_unit]["resolution"] == "superseded" and new_unit in closed[old_unit]["reason"]
    assert closed[old_unit]["run"] == first and _open(root)[new_unit].run == second

    # recorded in the store, not deleted
    service = _service(root)
    old = service.store.list_interrupts(ITEM, old_unit)
    assert len(old) == 1 and old[0].resolution.outcome == "rejected"
    assert resolution_kind("rejected", old[0].resolution.reason) == "superseded"

    # answer with no selector now finds exactly the current wait
    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--node", "played", "--value", '{"ok": true}',
                         *project)
    assert code == 0, err
    assert _closed(root)[new_unit]["resolution"] == "answered"


def test_the_same_question_does_not_supersede_itself(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch)
    _wait_for(root, "rain")
    (unit,) = _open(root)
    _wait_for(root, "rain")                          # same inputs, same digest: the same wait
    assert list(_open(root)) == [unit] and _closed(root) == {}


def test_a_superseded_wait_that_comes_back_asks_again(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch)
    _wait_for(root, "rain")
    (rain_unit,) = _open(root)
    _wait_for(root, "snow")
    _wait_for(root, "rain")                          # the old question again: the next unit number
    (again,) = _open(root)
    assert again.endswith("#2") and again.rsplit("#", 1)[0] == rain_unit.rsplit("#", 1)[0]
    assert {c["resolution"] for c in _closed(root).values()} == {"superseded"}


def test_answer_with_several_open_waits_picks_none_and_lists_them(tmp_path, monkeypatch):
    root, project = _setup(tmp_path, monkeypatch)
    first = _wait_for(root, "rain")
    second = _stale_second_wait(root, monkeypatch, "snow")
    units = _open(root)
    assert len(units) == 2

    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--node", "played", "--value", '{"ok": true}',
                         *project)
    assert code == 1 and "2 open waits" in err and "--wait UNIT" in err and "--dismiss UNIT" in err
    for unit, wait in units.items():
        assert unit in err and wait.run in err
    assert "rain" in err and "snow" in err
    assert len(_open(root)) == 2 and _closed(root) == {}      # nothing was recorded

    code, listed = cli_json("answer", "demo.main", "--item", ITEM, *project)
    assert code == 0 and {w["unit"] for w in listed} == set(units)
    assert {w["run"] for w in listed} == {first, second}


def test_answer_by_wait_resolves_only_the_chosen_wait(tmp_path, monkeypatch):
    root, project = _setup(tmp_path, monkeypatch)
    first = _wait_for(root, "rain")
    second = _stale_second_wait(root, monkeypatch, "snow")
    by_run = {w.run: w.unit for w in _open(root).values()}

    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--wait", by_run[first], "--value", '{"ok": true}',
                         *project)
    assert code == 0, err
    assert [c["unit"] for c in _closed(root).values()] == [by_run[first]]
    assert list(_open(root)) == [by_run[second]]

    # --node with --run chooses the other one
    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--node", "played", "--run", second,
                         "--value", '{"ok": false}', *project)
    assert code == 0, err
    assert _open(root) == {} and _closed(root)[by_run[second]]["resolution"] == "answered"

    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--wait", "nope@x#1", "--value", "{}", *project)
    assert code == 1 and "no open wait" in err


def test_a_bad_value_for_the_chosen_wait_records_nothing(tmp_path, monkeypatch):
    root, project = _setup(tmp_path, monkeypatch)
    _wait_for(root, "rain")
    (unit,) = _open(root)
    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--wait", unit, "--value", '{"ok": "yes"}',
                         *project)
    assert code == 1 and "does not match the node schema" in err
    assert list(_open(root)) == [unit]


def test_dismiss_closes_an_older_wait_and_keeps_the_record(tmp_path, monkeypatch):
    root, project = _setup(tmp_path, monkeypatch)
    first = _wait_for(root, "rain")
    _stale_second_wait(root, monkeypatch, "snow")
    stale = next(u for u, w in _open(root).items() if w.run == first)
    current = next(u for u in _open(root) if u != stale)
    waits = {u: w for u, w in _open(root).items()}

    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--dismiss", stale, "--reason", "old build",
                         *project)
    assert code == 0, err
    assert list(_open(root)) == [current]
    closed = _closed(root)[stale]
    assert closed["resolution"] == "dismissed" and closed["reason"] == "dismissed: old build"
    assert closed["run"] == waits[stale].run

    # the remaining wait needs no selector now
    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--node", "played", "--value", '{"ok": true}',
                         *project)
    assert code == 0, err
    assert _closed(root)[current]["resolution"] == "answered" and _closed(root)[stale]["resolution"] == "dismissed"

    code, out, err = cli("answer", "demo.main", "--item", ITEM, "--dismiss", stale, *project)
    assert code == 1 and "no open wait" in err           # already closed


def test_resume_continues_the_run_that_owns_the_answered_wait(tmp_path, monkeypatch):
    root, project = _setup(tmp_path, monkeypatch)
    first = _wait_for(root, "rain")
    second = _stale_second_wait(root, monkeypatch, "snow")
    by_run = {w.run: w.unit for w in _open(root).values()}

    assert cli("answer", "demo.main", "--item", ITEM, "--wait", by_run[first], "--value", '{"ok": true}',
               *project)[0] == 0

    # the newest run (second) still waits on its own unit: it is refused, and names that unit
    code, out, err = cli("resume", "demo.main", "--item", ITEM, *project)
    assert code == 1 and "still waits for an answer" in err and by_run[second] in err

    code, result = cli_json("resume", "demo.main", "--item", ITEM, "--wait", by_run[first], *project)
    assert code == 0 and result["status"] == "done", result
    record = RunStore(root / "work" / ITEM / "graph").load_run(result["run_id"])
    assert record["resumed_from"] == first
    assert record["nodes"]["played"]["wait"]["unit"] == by_run[first]
    assert list(_open(root)) == [by_run[second]]                # the other wait stays open

    code, out, err = cli("resume", "demo.main", "--item", ITEM, "--wait", "none@x#1", *project)
    assert code == 1 and "no stopped run" in err
    with pytest.raises(SystemExit) as exit_info:                 # --run and --wait exclude each other
        cli("resume", "demo.main", "--item", ITEM, "--wait", by_run[first], "--run", first, *project)
    assert exit_info.value.code == 2
