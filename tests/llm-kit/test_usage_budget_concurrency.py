"""The verdict cache stays whole when threads of one process share it.

Both writers of the cache (``pinned_evaluate`` on a miss, ``record_observed_halt``)
read the file, change one mapping and write it back. Each of those sequences
must happen inside one hold of the module lock, and each write must go through
a temp file no other writer can pick. A cross-process race is out of scope: the
lock orders threads of one process only.

No test sleeps or starts a thread. A recording lock and recording wrappers show
the order of events, and a nested write shows what a shared temp name costs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List

import pytest

from llm_kit.dispatch import usage_budget as ub
from llm_kit.registry.conserve import POOL_SEVEN_DAY, STATUS_AVAILABLE, Budget, ConserveSpec

SESSION = {"CLAUDE_CODE_SESSION_ID": "session-under-test"}
SPEC = ConserveSpec(pool=POOL_SEVEN_DAY)


class RecordingLock:
    """A lock that writes each acquire and release into a shared event list."""

    def __init__(self, events: List[str]) -> None:
        self.events = events

    def __enter__(self) -> "RecordingLock":
        self.events.append("acquire")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.events.append("release")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """The event list, with the lock, the cache calls and ``evaluate`` recording into it."""
    log: List[str] = []
    real_load, real_store = ub._load_cache, ub._store_cache

    def load(path: Path, key: str) -> Any:
        log.append("load")
        return real_load(path, key)

    def store(path: Path, key: str, verdicts: Any) -> None:
        log.append("store")
        real_store(path, key, verdicts)

    def evaluate(spec: ConserveSpec, harness: Any, *, now: Any = None) -> Budget:
        log.append("evaluate")
        return Budget(status=STATUS_AVAILABLE, pool=spec.pool, detail="stub")

    monkeypatch.setattr(ub, "_VERDICT_LOCK", RecordingLock(log), raising=False)
    monkeypatch.setattr(ub, "_load_cache", load)
    monkeypatch.setattr(ub, "_store_cache", store)
    monkeypatch.setattr(ub, "evaluate", evaluate)
    return log


def test_a_miss_reads_evaluates_and_writes_inside_one_hold(events: List[str], tmp_path: Path) -> None:
    ub.pinned_evaluate("haiku", SPEC, "claude", cache_path=tmp_path / "v.json", environ=SESSION)
    assert events == ["acquire", "load", "evaluate", "store", "release"]


def test_a_hit_reads_inside_one_hold(events: List[str], tmp_path: Path) -> None:
    cache = tmp_path / "v.json"
    ub.pinned_evaluate("haiku", SPEC, "claude", cache_path=cache, environ=SESSION)
    events.clear()
    ub.pinned_evaluate("haiku", SPEC, "claude", cache_path=cache, environ=SESSION)
    assert events == ["acquire", "load", "release"]


def test_an_observed_halt_reads_and_writes_inside_one_hold(events: List[str], tmp_path: Path) -> None:
    ub.record_observed_halt("haiku", SPEC, cache_path=tmp_path / "v.json", environ=SESSION)
    assert events == ["acquire", "load", "store", "release"]


def test_two_writes_use_different_temp_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sources: List[str] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        sources.append(os.fspath(src))
        real_replace(src, dst)

    monkeypatch.setattr(ub.os, "replace", replace)
    cache = tmp_path / "v.json"
    ub._store_cache(cache, "k", {"a": 1})
    ub._store_cache(cache, "k", {"b": 2})
    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert all(Path(source).parent == tmp_path for source in sources)


def test_a_write_in_the_middle_of_another_write_loses_neither_file_nor_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second writer lands between the first writer's write and its replace.

    With one temp name for both, the second replace consumes the shared temp
    file and the first replace finds nothing, so the first update is lost.
    With a temp file each, both complete and the file holds the later replace.
    """
    cache = tmp_path / "v.json"
    real_replace = os.replace
    nested: List[bool] = []

    def replace(src: Any, dst: Any) -> None:
        if not nested:
            nested.append(True)
            ub._store_cache(cache, "k", {"inner": 2})
        real_replace(src, dst)

    monkeypatch.setattr(ub.os, "replace", replace)
    ub._store_cache(cache, "k", {"outer": 1})
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "session_key": "k", "verdicts": {"outer": 1},
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v.json"]


def test_a_failed_write_gives_up_quietly_and_leaves_no_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(src: Any, dst: Any) -> None:
        raise PermissionError("replace refused")

    monkeypatch.setattr(ub.os, "replace", refuse)
    ub._store_cache(tmp_path / "v.json", "k", {"a": 1})
    assert list(tmp_path.iterdir()) == []
