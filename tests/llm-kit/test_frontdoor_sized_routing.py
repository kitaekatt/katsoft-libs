"""Context-aware front-door routing: sized names, the size filter, size-only tiers.

A front-door entry's ``context_window`` is the size its name promises; the door
drops tiers that cannot hold it, uses a ``routing.size_only`` tier only when no
ordinary tier can, and answers 400 when nothing fits. No test opens a socket:
upstream calls hit a fake ``httpx.AsyncClient`` that records the URL and body.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_kit.registry.model_endpoints import (
    EndpointEntry,
    EndpointRegistryError,
    RoutingConfig,
    frontdoor_group_members,
    frontdoor_group_of,
    load_endpoint_registry,
    tiers_for_size,
)

REGISTRY = """
version: 1
default: door-32k
models:
  mac-32k:
    base_url: http://mac.invalid/v1
    model: mac-model
    context_window: 32768
    routing: {group: g, order: 2, max_parallel: 1}
  gpu-240k:
    base_url: http://gpu.invalid/v1
    model: gpu-model
    context_window: 240000
    routing: {group: g, order: 1, max_parallel: 4}
  cloud:
    base_url: http://cloud.invalid/v1
    model: cloud-model
    context_window: 1048576
    routing: {group: g, order: 3}
  fn-64k:
    base_url: http://gpu.invalid/v1
    model: fn-model
    context_window: 65536
    routing: {group: fn, order: 1, max_parallel: 4}
  fn-240k:
    base_url: http://gpu.invalid/v1
    model: fn-model-240k
    context_window: 240000
    routing: {group: fn, order: 2, max_parallel: 1, size_only: true}
  door-32k:
    base_url: http://door.invalid/v1
    model: g
    frontdoor: true
    context_window: 32768
  door-240k:
    base_url: http://door.invalid/v1
    model: g-240k
    frontdoor: true
    frontdoor_group: g
    context_window: 240000
  door-fn-240k:
    base_url: http://door.invalid/v1
    model: fn-240k-name
    frontdoor: true
    frontdoor_group: fn
    context_window: 240000
"""


def _load(tmp_path: Path, text: str = REGISTRY):
    path = tmp_path / "registry.yaml"
    path.write_text(text, encoding="utf-8")
    return load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})


# --------------------------------------------------------------------------
# Registry parsing and the size rule
# --------------------------------------------------------------------------


def test_registry_parses_size_only_and_frontdoor_group(tmp_path):
    registry = _load(tmp_path)
    assert registry.notes == []
    assert registry.entries["fn-240k"].routing.size_only is True
    assert registry.entries["fn-64k"].routing.size_only is False
    assert registry.entries["door-240k"].frontdoor_group == "g"
    assert registry.entries["door-32k"].frontdoor_group is None
    assert frontdoor_group_of(registry.entries["door-240k"]) == "g"
    assert frontdoor_group_of(registry.entries["door-32k"]) == "g"  # plain: model is the group
    assert frontdoor_group_of(registry.entries["gpu-240k"]) is None  # not a door


def test_invalid_size_only_and_frontdoor_group_are_noted_and_ignored(tmp_path):
    text = """
version: 1
models:
  a:
    base_url: http://a.invalid/v1
    model: a
    routing: {group: g, size_only: "yes"}
  b:
    base_url: http://b.invalid/v1
    model: b
    frontdoor_group: g
  c:
    base_url: http://c.invalid/v1
    model: c
    frontdoor: true
    frontdoor_group: ""
"""
    registry = _load(tmp_path, text)
    assert registry.entries["a"].routing.size_only is False
    assert registry.entries["b"].frontdoor_group is None
    assert registry.entries["c"].frontdoor_group is None
    joined = "\n".join(registry.notes)
    assert "invalid 'size_only'" in joined
    assert "entry 'b' frontdoor_group ignored" in joined
    assert "invalid 'frontdoor_group'" in joined


def _tier(entry_id: str, window: Any, *, size_only: bool = False) -> EndpointEntry:
    return EndpointEntry(
        id=entry_id, base_url="http://x.invalid/v1", model=entry_id, context_window=window,
        routing=RoutingConfig(group="g", size_only=size_only),
    )


def test_tiers_for_size_filters_by_window_and_keeps_unknown_windows():
    small, big, unknown = _tier("small", 32768), _tier("big", 240000), _tier("unknown", None)
    assert [t.id for t in tiers_for_size([small, big, unknown], None)] == ["small", "big", "unknown"]
    assert [t.id for t in tiers_for_size([small, big, unknown], 32768)] == ["small", "big", "unknown"]
    assert [t.id for t in tiers_for_size([small, big, unknown], 100000)] == ["big", "unknown"]
    assert tiers_for_size([small, big], 300000) == []


def test_size_only_tier_serves_only_when_no_ordinary_tier_fits():
    ordinary, swap = _tier("ordinary", 65536), _tier("swap", 240000, size_only=True)
    assert [t.id for t in tiers_for_size([ordinary, swap], None)] == ["ordinary"]
    assert [t.id for t in tiers_for_size([ordinary, swap], 65536)] == ["ordinary"]
    assert [t.id for t in tiers_for_size([ordinary, swap], 65537)] == ["swap"]
    assert tiers_for_size([ordinary, swap], 240001) == []


def test_frontdoor_members_follow_the_door_size(tmp_path):
    entries = _load(tmp_path).entries
    assert frontdoor_group_members(entries["door-32k"], entries) == ["gpu-240k", "mac-32k", "cloud"]
    assert frontdoor_group_members(entries["door-240k"], entries) == ["gpu-240k", "cloud"]
    assert frontdoor_group_members(entries["door-fn-240k"], entries) == ["fn-240k"]


# --------------------------------------------------------------------------
# The real door app
# --------------------------------------------------------------------------


class Upstream:
    """Fake ``httpx.AsyncClient``: records POSTs; chosen hosts answer 503."""

    calls: list[tuple[str, dict[str, Any]]]
    failing: set[str]

    class Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.content = b'{"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}'
            self.headers = {"content-type": "application/json"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def post(self, url: str, json: Any = None, headers: Any = None) -> "Upstream.Response":
        type(self).calls.append((url, json))
        host = url.split("//", 1)[1].split("/", 1)[0]
        return Upstream.Response(503 if host in type(self).failing else 200)

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
    log = tmp_path / "access.jsonl"
    app = server.create_app(_load(tmp_path), access_log=log)
    client = TestClient(app)
    client.log = log  # type: ignore[attr-defined]
    return client


def _chat(client: Any, name: str) -> Any:
    return client.post("/v1/chat/completions", json={"model": name, "messages": []})


def _models_hit() -> list[str]:
    return [body["model"] for _, body in Upstream.calls]


def test_models_lists_groups_and_sized_names(door):
    ids = [item["id"] for item in door.get("/v1/models").json()["data"]]
    assert ids == sorted({"g", "fn", "g-240k", "fn-240k-name"})


def test_plain_name_keeps_meaning_32k_and_spills_to_every_tier(door):
    Upstream.failing = {"gpu.invalid"}
    assert _chat(door, "g").status_code == 200
    assert _models_hit() == ["gpu-model", "mac-model"]


def test_240k_name_never_reaches_the_small_tier(door):
    Upstream.failing = {"gpu.invalid"}
    assert _chat(door, "g-240k").status_code == 200
    assert _models_hit() == ["gpu-model", "cloud-model"]


def test_240k_name_uses_the_gpu_first(door):
    assert _chat(door, "g-240k").status_code == 200
    assert _models_hit() == ["gpu-model"]


def test_name_larger_than_every_server_is_a_400_naming_the_largest_size(tmp_path, monkeypatch, door):
    from llm_kit.frontdoor import server

    registry = _load(tmp_path, REGISTRY.replace("model: cloud-model\n    context_window: 1048576",
                                                "model: cloud-model\n    context_window: 262144"))
    entries = dict(registry.entries)
    entries["door-300k"] = EndpointEntry(
        id="door-300k", base_url="http://door.invalid/v1", model="g-300k", context_window=300000,
        frontdoor=True, frontdoor_group="g",
    )
    from llm_kit.registry.model_endpoints import EndpointRegistry
    from fastapi.testclient import TestClient

    client = TestClient(server.create_app(EndpointRegistry(entries=entries), access_log=tmp_path / "x.jsonl"))
    answer = _chat(client, "g-300k")
    assert answer.status_code == 400
    message = answer.json()["error"]["message"]
    assert "no g server holds 300,000 tokens" in message
    assert "largest size g holds is 262,144" in message
    assert Upstream.calls == []


def test_flash_next_plain_group_never_swaps_to_the_size_only_tier(door):
    Upstream.failing = {"gpu.invalid"}
    answer = _chat(door, "fn")
    assert answer.status_code == 502  # the 64k tier failed; the size-only tier is no spill target
    assert _models_hit() == ["fn-model"]


def test_flash_next_240k_name_goes_to_the_size_only_tier(door):
    assert _chat(door, "fn-240k-name").status_code == 200
    assert _models_hit() == ["fn-model-240k"]


def test_unknown_name_is_404(door):
    assert _chat(door, "nope").status_code == 404
    assert Upstream.calls == []


def test_access_log_records_the_requested_name_and_size(door):
    assert _chat(door, "g-240k").status_code == 200
    record = json.loads(door.log.read_text(encoding="utf-8").splitlines()[-1])
    assert (record["group"], record["requested"], record["size"]) == ("g", "g-240k", 240000)
    assert record["deployment"] == "gpu-240k"


def test_health_reports_window_and_size_only(door):
    rows = {row["id"]: row for row in door.get("/health").json()["deployments"]}
    assert rows["fn-240k"]["size_only"] is True and rows["fn-240k"]["context_window"] == 240000
    assert rows["gpu-240k"]["size_only"] is False


def test_two_entries_giving_one_name_different_sizes_fail_loudly(tmp_path):
    pytest.importorskip("fastapi")
    from llm_kit.frontdoor import server

    text = REGISTRY + """
  door-dup:
    base_url: http://door.invalid/v1
    model: g-240k
    frontdoor: true
    frontdoor_group: g
    context_window: 100000
"""
    with pytest.raises(EndpointRegistryError, match="both name 'g-240k'"):
        server.create_app(_load(tmp_path, text), access_log=tmp_path / "x.jsonl")


def test_check_text_lists_sizes_and_names(tmp_path):
    from llm_kit.frontdoor import server

    text = server._check_text(_load(tmp_path))  # noqa: SLF001
    assert "size_only fn-240k" in text
    assert "g-240k -> g (240,000)" in text
    assert "fn -> fn (unsized)" in text
