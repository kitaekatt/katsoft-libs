"""Front-door model preference lists: ``"model": "a,b"``.

A caller names several door names a request may use, in preference order. On
one swapper host (deployments sharing a ``base_url``), a host busy with one of
the listed models keeps serving it; an idle host gets the first listed model.
The other listed models on that host stand by right after the chosen one: they
serve when it fails, never because it is full (that would swap a busy host).

Run: ``uv run --extra dev pytest tests/llm-kit/test_frontdoor_preference.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from llm_kit.registry.model_endpoints import load_endpoint_registry

REGISTRY = """
version: 1
models:
  gpu-27b:
    base_url: http://gpu.invalid/v1
    model: m27b
    routing: {group: q38, order: 1, max_parallel: 4}
  mac-27b:
    base_url: http://mac.invalid/v1
    model: mac27b
    routing: {group: q38, order: 2, max_parallel: 1}
  gpu-fn:
    base_url: http://gpu.invalid/v1
    model: mfn
    routing: {group: q38fn, order: 1, max_parallel: 4}
"""


def _load(tmp_path: Path):
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    return load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})


def _deployments(tmp_path: Path) -> dict[str, Any]:
    from llm_kit.frontdoor import server

    groups = server._groups(_load(tmp_path))  # noqa: SLF001
    return {d.entry.id: d for ds in groups.values() for d in ds}


def _plan(tmp_path: Path, names: list[str], busy: dict[str, float] = {}):
    from llm_kit.frontdoor import server

    deployments = _deployments(tmp_path)
    for entry_id, since in busy.items():
        deployments[entry_id].in_flight = 1
        deployments[entry_id].busy_since = since
    pools = {
        "q38": [deployments["gpu-27b"], deployments["mac-27b"]],
        "q38fn": [deployments["gpu-fn"]],
    }
    tiers, standby = server._preference_tiers([pools[name] for name in names])  # noqa: SLF001
    return [[d.entry.id for d in tier] for tier in tiers], standby


def _walk(tmp_path: Path, names: list[str], busy: dict[str, float] = {}) -> list[list[str]]:
    return _plan(tmp_path, names, busy)[0]


# --------------------------------------------------------------------------
# The selection rule (pure)
# --------------------------------------------------------------------------


def test_idle_host_honors_the_first_listed_model(tmp_path):
    walk, standby = _plan(tmp_path, ["q38fn", "q38"])
    assert walk == [["gpu-fn"], ["gpu-27b"], ["mac-27b"]]
    assert standby == {"gpu-27b": "gpu-fn"}


def test_host_busy_with_a_listed_model_keeps_serving_it(tmp_path):
    walk, standby = _plan(tmp_path, ["q38fn", "q38"], busy={"gpu-27b": 1.0})
    assert walk == [["gpu-27b"], ["gpu-fn"], ["mac-27b"]]
    assert standby == {"gpu-fn": "gpu-27b"}


def test_busy_with_the_first_choice_still_uses_it(tmp_path):
    walk = _walk(tmp_path, ["q38", "q38fn"], busy={"gpu-fn": 1.0})
    assert walk == [["gpu-fn"], ["gpu-27b"], ["mac-27b"]]


def test_two_listed_models_in_flight_prefer_the_one_busy_longest(tmp_path):
    # The later one is queued behind a swap; the resident one keeps the host.
    walk = _walk(tmp_path, ["q38fn", "q38"], busy={"gpu-27b": 1.0, "gpu-fn": 2.0})
    assert walk[0] == ["gpu-27b"]


def test_busy_on_another_host_does_not_steer_this_host(tmp_path):
    walk = _walk(tmp_path, ["q38fn", "q38"], busy={"mac-27b": 1.0})
    assert walk[0] == ["gpu-fn"]


def test_a_standby_serves_only_after_its_host_choice_is_excluded(tmp_path):
    from llm_kit.frontdoor import server

    deployments = _deployments(tmp_path)
    tiers, standby = server._preference_tiers(  # noqa: SLF001
        [[deployments["gpu-fn"]], [deployments["gpu-27b"], deployments["mac-27b"]]]
    )
    active = server._active_tiers(tiers, set(), standby)  # noqa: SLF001
    assert [[d.entry.id for d in tier] for tier in active] == [["gpu-fn"], ["mac-27b"]]
    active = server._active_tiers(tiers, {"gpu-fn"}, standby)  # noqa: SLF001
    assert [[d.entry.id for d in tier] for tier in active] == [["gpu-27b"], ["mac-27b"]]


# --------------------------------------------------------------------------
# The real door app
# --------------------------------------------------------------------------


class Upstream:
    """Fake ``httpx.AsyncClient``: records POSTs; listed models answer 503."""

    calls: list[dict[str, Any]]
    failing: set[str]

    class Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.content = b'{"choices": []}'
            self.headers = {"content-type": "application/json"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def post(self, url: str, json: Any = None, headers: Any = None) -> "Upstream.Response":
        type(self).calls.append(json)
        return Upstream.Response(503 if json["model"] in type(self).failing else 200)

    async def aclose(self) -> None:
        pass


@pytest.fixture()
def door(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from llm_kit.frontdoor import server

    Upstream.calls = []
    Upstream.failing = set()
    monkeypatch.setattr("httpx.AsyncClient", Upstream)
    return TestClient(server.create_app(_load(tmp_path), access_log=tmp_path / "access.jsonl"))


def _chat(client: Any, name: str) -> Any:
    return client.post("/v1/chat/completions", json={"model": name, "messages": []})


def test_list_request_on_an_idle_door_uses_the_first_model(door):
    assert _chat(door, "q38fn, q38").status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["mfn"]


def test_failed_choice_falls_back_through_the_list(door):
    # The GPU's standby (the 27B) serves before another host when Flash-Next fails.
    Upstream.failing = {"mfn"}
    assert _chat(door, "q38fn,q38").status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["mfn", "m27b"]


def test_unknown_or_empty_item_in_a_list_is_refused(door):
    assert _chat(door, "q38fn,nope").status_code == 404
    assert _chat(door, "q38fn,,q38").status_code == 400
    assert Upstream.calls == []


# --------------------------------------------------------------------------
# Size steering: a list skips a name that cannot hold the estimated request
# --------------------------------------------------------------------------

SIZED = REGISTRY.replace(
    "    model: m27b\n", "    model: m27b\n    context_window: 240000\n"
).replace(
    "    model: mfn\n", "    model: mfn\n    context_window: 65536\n"
).replace(
    "    model: mac27b\n", "    model: mac27b\n    context_window: 32768\n"
)


@pytest.fixture()
def sized_door(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from llm_kit.frontdoor import server

    Upstream.calls = []
    Upstream.failing = set()
    monkeypatch.setattr("httpx.AsyncClient", Upstream)
    path = tmp_path / "sized.yaml"
    path.write_text(SIZED, encoding="utf-8")
    registry = load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})
    return TestClient(server.create_app(registry, access_log=tmp_path / "access.jsonl"))


def _sized_chat(client: Any, name: str, chars: int, max_tokens: int = 0) -> Any:
    body: dict[str, Any] = {"model": name, "messages": [{"role": "user", "content": "x" * chars}]}
    if max_tokens:
        body["max_tokens"] = max_tokens
    return client.post("/v1/chat/completions", json=body)


def test_small_list_request_keeps_the_first_name(sized_door):
    assert _sized_chat(sized_door, "q38fn,q38", 40_000).status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["mfn"]


def test_request_too_big_for_the_first_name_goes_to_the_next(sized_door):
    # ~100k tokens cannot fit Flash-Next's 64k seat; the 27B is preferred, the Mac dropped.
    assert _sized_chat(sized_door, "q38fn,q38", 300_000).status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["m27b"]


def test_max_tokens_counts_toward_the_size(sized_door):
    assert _sized_chat(sized_door, "q38fn,q38", 40_000, max_tokens=60_000).status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["m27b"]


def test_the_estimate_never_refuses_a_request(sized_door):
    # Bigger than every window by the estimate: the door still sends it, as listed.
    assert _sized_chat(sized_door, "q38fn,q38", 2_000_000).status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["mfn"]
