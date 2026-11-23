"""Front-door model preference lists: ``"model": "a,b"``.

A caller names several door names a request may use, in preference order. On
one swapper host (deployments sharing a ``base_url``), a host busy with one of
the listed models keeps serving it; an idle host gets the first listed model.
The other listed models on that host are tried only after the chosen one fails.

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


def _walk(tmp_path: Path, names: list[str], busy: dict[str, float] = {}) -> list[list[str]]:
    from llm_kit.frontdoor import server

    deployments = _deployments(tmp_path)
    for entry_id, since in busy.items():
        deployments[entry_id].in_flight = 1
        deployments[entry_id].busy_since = since
    pools = {
        "q38": [deployments["gpu-27b"], deployments["mac-27b"]],
        "q38fn": [deployments["gpu-fn"]],
    }
    tiers = server._preference_tiers([pools[name] for name in names])  # noqa: SLF001
    return [[d.entry.id for d in tier] for tier in tiers]


# --------------------------------------------------------------------------
# The selection rule (pure)
# --------------------------------------------------------------------------


def test_idle_host_honors_the_first_listed_model(tmp_path):
    assert _walk(tmp_path, ["q38fn", "q38"]) == [["gpu-fn"], ["mac-27b"], ["gpu-27b"]]


def test_host_busy_with_a_listed_model_keeps_serving_it(tmp_path):
    walk = _walk(tmp_path, ["q38fn", "q38"], busy={"gpu-27b": 1.0})
    assert walk == [["gpu-27b"], ["mac-27b"], ["gpu-fn"]]


def test_busy_with_the_first_choice_still_uses_it(tmp_path):
    walk = _walk(tmp_path, ["q38", "q38fn"], busy={"gpu-fn": 1.0})
    assert walk == [["gpu-fn"], ["mac-27b"], ["gpu-27b"]]


def test_two_listed_models_in_flight_prefer_the_one_busy_longest(tmp_path):
    # The later one is queued behind a swap; the resident one keeps the host.
    walk = _walk(tmp_path, ["q38fn", "q38"], busy={"gpu-27b": 1.0, "gpu-fn": 2.0})
    assert walk[0] == ["gpu-27b"]


def test_busy_on_another_host_does_not_steer_this_host(tmp_path):
    walk = _walk(tmp_path, ["q38fn", "q38"], busy={"mac-27b": 1.0})
    assert walk[0] == ["gpu-fn"]


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
    Upstream.failing = {"mfn", "mac27b"}
    assert _chat(door, "q38fn,q38").status_code == 200
    assert [call["model"] for call in Upstream.calls] == ["mfn", "mac27b", "m27b"]


def test_unknown_or_empty_item_in_a_list_is_refused(door):
    assert _chat(door, "q38fn,nope").status_code == 404
    assert _chat(door, "q38fn,,q38").status_code == 400
    assert Upstream.calls == []
