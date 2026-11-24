"""Labelling a catalog: calls, follow-ups, the cache, concurrency and the v2 result.

A fake :class:`Labeller` answers every call from the keys it was asked for, so
no model is involved. The fixture project ``fixtures/viewer/project`` has three
graph files (``night``, ``pose``, ``story``; the story file holds a repeat with
an inline body).
"""

from __future__ import annotations

import copy
import json
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

import pytest

from llm_kit.graph.config import load_config
from llm_kit.graph.render import labels, project_catalog
from llm_kit.graph.render.label_types import (
    LABELLED_CATALOG_FORMAT,
    LABELS_PROMPT_ID,
    LabelCall,
    LabelError,
    LabelFailed,
    LabelReply,
)
from llm_kit.graph.render.labels import DEFAULT_JOBS, LabelCache, default_cache_dir, label_catalog
from llm_kit.schema.formats import GRAPH_CATALOG_V2, GRAPH_LABELS_V1, load_schema
from llm_kit.schema.json_schema import validate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "viewer" / "project"
NIGHT = "graphs/night.graph.yaml"
POSE = "graphs/stages/pose.graph.yaml"
STORY = "graphs/stages/story.graph.yaml"


class Fake:
    """A labeller that answers from the keys of each call; ``omit`` and ``hook`` shape its behaviour."""

    def __init__(self, *, entry="alpha", model="alpha-model", declaration=("alpha",),
                 omit: Optional[Callable[[LabelCall], dict]] = None,
                 extra: Optional[Callable[[LabelCall], dict]] = None,
                 hook: Optional[Callable[[LabelCall], None]] = None,
                 followup_entry: Optional[str] = None, title: Optional[str] = None):
        self.declaration = tuple(declaration)
        self.entry, self.model = entry, model
        self.omit, self.extra, self.hook = omit, extra, hook
        self.followup_entry, self.title = followup_entry, title
        self.calls: list[LabelCall] = []
        self._lock = threading.Lock()

    def __call__(self, call: LabelCall) -> LabelReply:
        with self._lock:
            self.calls.append(call)
        if self.hook is not None:
            self.hook(call)
        answer = {
            "nodes": {p: {"title": self.title or f"T {p}", "description": f"D {p}"} for p in call.nodes},
            "edges": {e: f"E {e}" for e in call.edges},
        }
        if call.header:
            answer["graph"] = {"title": f"G {call.file}", "purpose": f"P {call.file}"}
            answer["inputs"] = {n: f"I {n}" for n in call.inputs}
            answer["outputs"] = {n: f"O {n}" for n in call.outputs}
        for key, names in (self.omit(call) if self.omit else {}).items():
            if key == "graph":
                answer.pop("graph", None)
            else:
                for name in names:
                    answer[key].pop(name, None)
        for key, value in (self.extra(call) if self.extra else {}).items():
            answer[key].update(value)
        if call.followup:
            return LabelReply(answer, self.followup_entry or call.entry, self.model + "-followup")
        return LabelReply(answer, self.entry, self.model)

    @property
    def files(self) -> list:
        return [c.file for c in self.calls]


class Boom(Fake):
    def __call__(self, call):  # pragma: no cover -- the test asserts it is never reached
        raise AssertionError(f"no call was expected, got {call.file}")


@pytest.fixture(scope="module")
def catalog() -> dict:
    return project_catalog(load_config(FIXTURE))


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns("state", "work"))
    return dest


def _cat(root: Path) -> dict:
    return project_catalog(load_config(root))


def _valid(v2: dict) -> None:
    assert validate(load_schema(GRAPH_CATALOG_V2), json.loads(json.dumps(v2))) == ()


def _paths(nodes: list, prefix: str = "") -> list:
    out = []
    for node in nodes:
        out.append(prefix + node["id"])
        if "body" in node:
            out += _paths(node["body"]["nodes"], prefix + node["id"] + "/")
    return out


def _reader_rules(v2: dict) -> None:
    """The rules the schema subset cannot state (docs/reference/llm-kit-formats.md)."""
    for file, block in v2["labels"].items():
        graph = v2["files"][file]
        node_list = {}

        def collect(nodes, prefix=""):
            for n in nodes:
                node_list[prefix + n["id"]] = n
                if "body" in n:
                    collect(n["body"]["nodes"], prefix + n["id"] + "/")
        collect(graph["nodes"])
        count = len(block["sources"])
        assert block["graph"]["source"] < count
        assert set(block["graph"]["inputs"]) <= {i["name"] for i in graph["inputs"]}
        assert set(block["graph"]["outputs"]) <= set(graph["outputs"])
        for path, label in block["nodes"].items():
            assert path in node_list and label["source"] < count
            assert set(label["reads"]) <= set(node_list[path]["deps"])


def _label(catalog, labeller, root=FIXTURE, **options):
    return label_catalog(copy.deepcopy(catalog), labeller, root=root, **options)


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------


def test_every_node_edge_and_port_is_labelled_and_the_result_is_a_valid_v2_catalog(catalog):
    before = json.dumps(catalog)
    fake = Fake()
    v2 = label_catalog(catalog, fake, root=FIXTURE)
    assert json.dumps(catalog) == before, "the v1 catalog was changed"
    assert v2["format"] == LABELLED_CATALOG_FORMAT
    assert {k: v for k, v in v2.items() if k not in ("format", "labels")} == {
        k: v for k, v in catalog.items() if k != "format"}
    _valid(v2)
    _reader_rules(v2)
    assert list(v2["labels"]) == list(v2["files"]) == [NIGHT, POSE, STORY]
    assert sorted(fake.files) == [NIGHT, POSE, STORY]
    for file, graph in v2["files"].items():
        block = v2["labels"][file]
        assert list(block["nodes"]) == _paths(graph["nodes"])
        assert block["graph"] == {"title": f"G {file}", "purpose": f"P {file}",
                                  "inputs": {i["name"]: f"I {i['name']}" for i in graph["inputs"]},
                                  "outputs": {o: f"O {o}" for o in graph["outputs"]}, "source": 0}
        assert [s["prompt"] for s in block["sources"]] == [LABELS_PROMPT_ID]
        assert block["sources"][0]["calls"] == [{"entry": "alpha", "model": "alpha-model"}]
    story = v2["labels"][STORY]["nodes"]
    assert story["loop/repair"]["reads"] == {"check": "E loop/check->loop/repair", "write": "E loop/write->loop/repair"}
    assert story["loop/write"]["reads"] == {} and story["loop/write"]["title"] == "T loop/write"
    assert v2["labels"][NIGHT]["nodes"]["encode"]["reads"] == {
        "approve": "E approve->encode", "bundle": "E bundle->encode"}


def test_a_catalog_that_is_not_v1_or_a_bad_option_is_refused(catalog):
    labelled = _label(catalog, Fake())
    with pytest.raises(ValueError, match="graph-catalog/v1"):
        _label(labelled, Fake())
    for bad in (0, -1, 1.5):
        with pytest.raises(ValueError, match="jobs"):
            _label(catalog, Fake(), jobs=bad)
    with pytest.raises(ValueError, match="nodes_per_call"):
        _label(catalog, Fake(), nodes_per_call=0)
    assert DEFAULT_JOBS == 4


def test_batched_calls_merge_into_one_labelled_file_with_one_source_each(catalog, tmp_path):
    fake = Fake()
    v2 = _label(catalog, fake, nodes_per_call=4, cache=LabelCache(tmp_path / "c"))
    _valid(v2)
    _reader_rules(v2)
    story = v2["labels"][STORY]
    assert len(story["sources"]) == 2 and len({s["key"] for s in story["sources"]}) == 2
    assert story["graph"]["source"] == 0
    sources = {path: label["source"] for path, label in story["nodes"].items()}
    assert sources == {"loop": 0, "loop/write": 0, "loop/check": 0, "loop/repair": 0, "loop/evaluate": 1, "keep": 1}
    assert story["nodes"]["loop/evaluate"]["reads"] == {"write": "E loop/write->loop/evaluate"}
    assert len(fake.calls) == 2 + 1 + 2  # night 6 nodes = 2 calls, pose 1, story 2
    assert len(list((tmp_path / "c").glob("*.json"))) == 5


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


def test_a_warm_cache_makes_no_call_and_gives_identical_bytes(catalog, tmp_path):
    cache = LabelCache(tmp_path / "cache")
    cold = _label(catalog, Fake(), cache=cache)
    assert len(list((tmp_path / "cache").glob("*.json"))) == 3
    warm = _label(catalog, Boom(), cache=cache)
    assert json.dumps(warm) == json.dumps(cold)
    entry = json.loads((tmp_path / "cache" / f"{cold['labels'][NIGHT]['sources'][0]['key']}.json").read_text("utf-8"))
    assert validate(load_schema(GRAPH_LABELS_V1), entry) == ()
    assert entry["declaration"] == ["alpha"] and entry["prompt"] == LABELS_PROMPT_ID


def test_one_edited_file_relabels_only_its_calls(project, tmp_path):
    cache = LabelCache(tmp_path / "cache")
    label_catalog(_cat(project), Fake(), root=project, cache=cache)
    judge = project / "prompts" / "judge.md"
    judge.write_text(judge.read_text(encoding="utf-8").replace("Fixture prompt judge", "Judge harder"), encoding="utf-8")
    fake = Fake()
    label_catalog(_cat(project), fake, root=project, cache=cache)
    assert fake.files == [STORY]


def test_refresh_skips_the_cache_reads_and_rewrites_the_entries(catalog, tmp_path):
    cache = LabelCache(tmp_path / "cache")
    _label(catalog, Fake(), cache=cache)
    stale = _label(catalog, Fake(title="stale"), cache=cache, refresh=True)
    assert stale["labels"][NIGHT]["nodes"]["brief"]["title"] == "stale"
    fake = Fake()
    fresh = _label(catalog, fake, cache=cache, refresh=True)
    assert len(fake.calls) == 3 and fresh["labels"][NIGHT]["nodes"]["brief"]["title"] == "T brief"
    again = _label(catalog, Boom(), cache=cache)
    assert again["labels"][NIGHT]["nodes"]["brief"]["title"] == "T brief"


def test_without_a_cache_nothing_is_written_in_the_project_tree(project):
    before = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))
    label_catalog(_cat(project), Fake(), root=project)
    assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*")) == before


def _damage_json(path: Path):
    path.write_text("{not json", encoding="utf-8")


def _damage_schema(path: Path):
    entry = json.loads(path.read_text("utf-8"))
    entry["calls"] = []
    path.write_text(json.dumps(entry), encoding="utf-8")


def _damage_complete(path: Path):
    entry = json.loads(path.read_text("utf-8"))
    del entry["answer"]["nodes"]["brief"]
    path.write_text(json.dumps(entry), encoding="utf-8")


def _damage_foreign(path: Path):
    entry = json.loads(path.read_text("utf-8"))
    entry["key"] = "0" * 64
    path.write_text(json.dumps(entry), encoding="utf-8")


def _damage_prompt(path: Path):
    entry = json.loads(path.read_text("utf-8"))
    entry["prompt"] = "llm-kit.graph-labels.prompt/0"
    path.write_text(json.dumps(entry), encoding="utf-8")


@pytest.mark.parametrize("damage", [_damage_json, _damage_schema, _damage_complete, _damage_foreign, _damage_prompt])
def test_a_damaged_cache_entry_fails_loudly(catalog, tmp_path, damage):
    cache = LabelCache(tmp_path / "cache")
    cold = _label(catalog, Fake(), cache=cache)
    key = cold["labels"][NIGHT]["sources"][0]["key"]
    target = tmp_path / "cache" / f"{key}.json"
    damage(target)
    fake = Fake()
    with pytest.raises(LabelError) as caught:
        _label(catalog, fake, cache=cache)
    assert target.name in str(caught.value) and "--relabel" in str(caught.value)
    assert fake.calls == [], "a damaged entry must be found before any call is made"
    repaired = _label(catalog, Fake(), cache=cache, refresh=True)
    assert json.dumps(repaired) == json.dumps(cold)


def test_cache_entries_are_written_atomically_and_keys_are_checked(tmp_path, monkeypatch):
    cache = LabelCache(tmp_path / "cache")
    assert cache.get("a" * 64) is None and not (tmp_path / "cache").exists()
    entry = {"format": GRAPH_LABELS_V1, "key": "a" * 64, "prompt": LABELS_PROMPT_ID, "declaration": ["alpha"],
             "calls": [{"entry": "alpha", "model": "m"}], "answer": {"nodes": {}, "edges": {}}}
    path = cache.put(entry)
    assert path == tmp_path / "cache" / ("a" * 64 + ".json") and cache.get("a" * 64) == entry
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [path.name]
    other = {**entry, "key": "b" * 64}

    def fail(*args, **kwargs):
        raise OSError("disk went away")
    monkeypatch.setattr(labels.os, "replace", fail)
    with pytest.raises(OSError):
        cache.put(other)
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [path.name], "a failed write left a file behind"
    for bad in ("short", "A" * 64, "../" + "a" * 61):
        with pytest.raises(ValueError):
            cache.get(bad)
        with pytest.raises(ValueError):
            cache.put({**entry, "key": bad})


def test_the_default_cache_directory_is_the_neutral_one(tmp_path):
    xdg = tmp_path / "xdg"
    assert default_cache_dir({"XDG_CACHE_HOME": str(xdg)}) == xdg / "llm-kit" / "graph-labels"


# ---------------------------------------------------------------------------
# follow-up and failures
# ---------------------------------------------------------------------------


def _night_gaps(call: LabelCall) -> dict:
    if call.file == NIGHT and not call.followup:
        return {"graph": ["graph"], "nodes": ["plot"], "edges": ["brief->plot"], "outputs": ["bundle"]}
    return {}


def test_a_follow_up_is_pinned_to_the_entry_that_served_the_first_call(catalog, tmp_path):
    fake = Fake(entry="beta", model="beta-model", declaration=("alpha", "beta"), omit=_night_gaps)
    cache = LabelCache(tmp_path / "cache")
    v2 = _label(catalog, fake, cache=cache)
    follow = [c for c in fake.calls if c.followup]
    assert len(follow) == 1 and len(fake.calls) == 4
    [call] = follow
    assert call.entry == "beta" and call.file == NIGHT
    assert (call.nodes, call.edges, call.inputs, call.outputs) == (("plot",), ("brief->plot",), (), ("bundle",))
    _valid(v2)
    block = v2["labels"][NIGHT]
    assert block["sources"][0]["calls"] == [{"entry": "beta", "model": "beta-model"},
                                            {"entry": "beta", "model": "beta-model-followup"}]
    assert block["nodes"]["plot"]["title"] == "T plot" and block["graph"]["outputs"] == {"bundle": "O bundle"}
    assert block["nodes"]["poses"]["reads"] == {"plot": "E plot->poses"}
    assert v2["labels"][POSE]["sources"][0]["calls"] == [{"entry": "beta", "model": "beta-model"}]
    entry = json.loads((cache.directory / f"{block['sources'][0]['key']}.json").read_text("utf-8"))
    assert len(entry["calls"]) == 2 and "plot" in entry["answer"]["nodes"] and entry["declaration"] == ["alpha", "beta"]
    assert validate(load_schema(GRAPH_LABELS_V1), entry) == ()
    warm = _label(catalog, Boom(declaration=("alpha", "beta")), cache=cache)
    assert json.dumps(warm) == json.dumps(v2)


def test_keys_still_missing_after_the_follow_up_fail_and_nothing_is_cached(catalog, tmp_path):
    def stubborn(call):
        return {"nodes": ["plot"]} if call.file == NIGHT else {}
    cache = LabelCache(tmp_path / "cache")
    fake = Fake(omit=stubborn, declaration=("alpha",))
    with pytest.raises(LabelFailed, match="plot"):
        _label(catalog, fake, cache=cache, jobs=1)
    assert fake.files == [NIGHT, NIGHT] and [c.followup for c in fake.calls] == [False, True]
    assert not cache.directory.exists() or not list(cache.directory.glob("*.json"))


def test_an_unknown_key_in_the_answer_fails(catalog):
    fake = Fake(extra=lambda call: {"nodes": {"ghost": {"title": "t", "description": "d"}}} if call.file == POSE else {})
    with pytest.raises(LabelFailed, match="ghost"):
        _label(catalog, fake, jobs=1)


def test_a_follow_up_answered_by_another_entry_fails(catalog):
    fake = Fake(omit=_night_gaps, followup_entry="gamma")
    with pytest.raises(LabelFailed, match="gamma|pinned"):
        _label(catalog, fake, jobs=1)


def test_an_answer_that_breaks_the_call_schema_fails(catalog):
    with pytest.raises(LabelFailed, match="schema"):
        _label(catalog, Fake(title="t" * 81), jobs=1)


# ---------------------------------------------------------------------------
# concurrency, failure order, progress
# ---------------------------------------------------------------------------


def test_at_most_jobs_calls_run_at_once(catalog):
    jobs = 3
    lock = threading.Lock()
    state = {"now": 0, "most": 0}
    saturated, extra, release = threading.Event(), threading.Event(), threading.Event()

    def hold(call):
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            now = state["now"]
        if now == jobs:
            saturated.set()
        if now > jobs:
            extra.set()
        release.wait(10)
        with lock:
            state["now"] -= 1

    fake = Fake(hook=hold)
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = _label(catalog, fake, jobs=jobs, nodes_per_call=1)
        except BaseException as exc:  # pragma: no cover -- surfaced below
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert saturated.wait(10), "the calls never reached the bound"
        assert not extra.wait(0.3), "a call started beyond the bound"
    finally:
        release.set()
        worker.join(30)
    assert "error" not in outcome, outcome.get("error")
    assert state["most"] == jobs and len(fake.calls) == 6 + 3 + 6
    _valid(outcome["result"])


def test_the_first_failure_stops_new_calls_and_keeps_completed_ones_cached(catalog, tmp_path):
    def fail_on_pose(call):
        if call.file == POSE:
            raise LabelFailed("pose went wrong")

    cache = LabelCache(tmp_path / "cache")
    fake = Fake(hook=fail_on_pose)
    with pytest.raises(LabelFailed, match="pose went wrong"):
        _label(catalog, fake, jobs=1, cache=cache)
    assert fake.files == [NIGHT, POSE], "a call started after the failure"
    assert len(list(cache.directory.glob("*.json"))) == 1
    retry = Fake()
    _label(catalog, retry, jobs=1, cache=cache)
    assert retry.files == [POSE, STORY], "the completed call was not cached"


def test_the_first_failure_in_plan_order_is_raised_with_the_count_of_the_others(catalog):
    pose_failed = threading.Event()

    def hook(call):
        if call.file == POSE:
            pose_failed.set()
            raise LabelFailed("pose went wrong")
        if call.file == NIGHT:
            assert pose_failed.wait(10)
            raise LabelFailed("night went wrong")

    fake = Fake(hook=hook)
    with pytest.raises(LabelFailed) as caught:
        _label(catalog, fake, jobs=2)
    assert "night went wrong" in str(caught.value) and "1 other" in str(caught.value)
    assert STORY not in fake.files


def test_the_result_does_not_depend_on_the_order_the_calls_finish_in(catalog):
    serial = _label(catalog, Fake(), jobs=1)
    last_done = threading.Event()

    def reverse(call):
        if call.file == NIGHT:
            assert last_done.wait(10)
        elif call.file == STORY:
            last_done.set()

    out_of_order = _label(catalog, Fake(hook=reverse), jobs=3)
    assert json.dumps(out_of_order) == json.dumps(serial)


def test_progress_goes_to_the_callback_and_nothing_is_printed(catalog, tmp_path, capsys):
    cache = LabelCache(tmp_path / "cache")
    lines: list = []
    _label(catalog, Fake(), cache=cache, progress=lines.append)
    assert lines[0].startswith("labels: 3 graph files") and "3 to make" in lines[0] and "alpha" in lines[0]
    assert "(4 at a time)" in lines[0]
    assert sum(NIGHT in line for line in lines[1:]) == 1 and sum(STORY in line for line in lines[1:]) == 1
    assert lines[-1].startswith("labels:") and "3 made" in lines[-1]
    followups: list = []
    _label(catalog, Fake(omit=_night_gaps), jobs=1, progress=followups.append, refresh=True)
    assert any("follow-up" in line and NIGHT in line for line in followups)
    warm: list = []
    _label(catalog, Boom(), cache=cache, progress=warm.append)
    assert "3 cached" in warm[0] and "none to make" in warm[0] and len(warm) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
