"""Front-door failover: R39-R41 (plugins-kit
docs/planning/quota-resilient-dispatch/requirements.md, "LLM front-door failover").

R39  a declared front-door entry expands into its routing group's members, in
     ``routing.order``, and a unit can complete on a member with the front door down.
R40  a refused connection, a DNS failure or a timeout moves the unit to the next
     member, and past the group to the next declared entry.
R41  a busy endpoint (``classify_backpressure``) is skipped at once: no SDK retry
     wait, no quota halt recorded.

Transport failures are REAL OpenAI-SDK exceptions: a real ``openai.OpenAI`` client
over an ``httpx2.MockTransport`` raises them through a real ``OpenRouterBackend``
and its real halt classifier, so no test opens a socket. The three dispatch paths
covered are ``declaration.run``, the pipeline's ``route`` (``DeclarationBackend``)
and the code-review lane.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import httpx2
import openai
import pytest

from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

from llm_kit import _runtime
from llm_kit.adapters.backends import OpenRouterBackend
from llm_kit.completion import halt
from llm_kit.completion.types import BackendOptions, LLMResponse
from llm_kit.dispatch import declaration as d
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.dispatch.reachability import Reachability
from llm_kit.registry.conserve import ConserveSpec
from llm_kit.registry.model_endpoints import (
    HARNESS_KIND,
    TRANSPORT_KIND,
    EndpointEntry,
    RoutingConfig,
    frontdoor_group_members,
)


# --------------------------------------------------------------------------
# Registry builders
# --------------------------------------------------------------------------


def front_door(entry_id="door", group="g"):
    return EndpointEntry(id=entry_id, base_url=f"http://{entry_id}.invalid/v1", model=group, frontdoor=True)


def member(entry_id, order, *, local=True, group="g", spec=None):
    return EndpointEntry(
        id=entry_id, base_url=f"http://{entry_id}.invalid/v1", model=f"{entry_id}-model",
        routing=RoutingConfig(group=group, order=order),
        billing_mode="unmetered" if local else None,
        billing_owner="self" if local else None,
        conserve_usage=spec,
    )


def harness(entry_id, name="claude", spec=None):
    return EndpointEntry(
        id=entry_id, base_url=None, model=f"{entry_id}-model", kind=HARNESS_KIND, harness=name,
        conserve_usage=spec,
    )


def fleet(**overrides):
    """The live shape: a front door over two local tiers and a cloud spill, plus Opus.

    Inserted out of walk order, and with a member of another group, so ordering
    and group membership are both exercised.
    """
    entries = {
        "cloud": member("cloud", 3, local=False),
        "door": front_door(),
        "m2": member("m2", 2),
        "other": member("other", 1, group="h"),
        "m1": member("m1", 1),
        "opus": harness("opus"),
    }
    entries.update(overrides)
    return entries


class Probes:
    """Stands in for ``check_many``: records every probe set; ``down`` ids are unreachable."""

    def __init__(self, down=()):
        self.down = set(down)
        self.calls = []

    def __call__(self, entries, **_kw):
        self.calls.append(sorted(entries))
        return {
            name: Reachability(
                "unreachable" if name in self.down else "reachable", "test",
                "probe: down" if name in self.down else "ok",
            )
            for name in entries
        }


@pytest.fixture()
def probes(monkeypatch):
    fake = Probes()
    monkeypatch.setattr(d, "check_many", fake)
    return fake


# --------------------------------------------------------------------------
# A real SDK client over a mock transport
# --------------------------------------------------------------------------


def _completion_body(text="answer"):
    return {
        "id": "c", "object": "chat.completion", "created": 0, "model": "served",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class Endpoint:
    """One mock HTTP endpoint. ``behaviour`` decides every request it receives."""

    def __init__(self, behaviour="ok", text="answer"):
        self.behaviour = behaviour
        self.text = text
        self.requests = 0

    def handler(self, request):
        self.requests += 1
        b = self.behaviour
        if b == "ok":
            return httpx2.Response(200, json=_completion_body(self.text))
        if b == "refused":
            raise httpx2.ConnectError("[Errno 61] Connection refused", request=request)
        if b == "dns":
            raise httpx2.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)
        if b == "connect-timeout":
            raise httpx2.ConnectTimeout("timed out", request=request)
        if b == "read-timeout":
            raise httpx2.ReadTimeout("timed out", request=request)
        if b == "busy503":
            return httpx2.Response(503, json={"error": {"message": "all slots busy", "code": "server_busy"}})
        if b == "busy429":
            return httpx2.Response(429, headers={"retry-after": "7"},
                                   json={"error": {"message": "request queue full", "code": "queue_full"}})
        if b == "error500":
            return httpx2.Response(500, json={"error": {"message": "internal error"}})
        if b == "bad400":
            return httpx2.Response(400, json={"error": {"message": "bad request"}})
        if b == "unsupported":
            raise httpx2.UnsupportedProtocol("Request URL is missing an 'http://' or 'https://' protocol.", request=request)
        if b == "local-protocol":
            raise httpx2.LocalProtocolError("Illegal header value", request=request)
        if b == "dropped":
            raise httpx2.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        raise AssertionError(f"unknown behaviour {b!r}")

    def client(self):
        return openai.OpenAI(
            api_key="test", base_url="http://endpoint.invalid/v1",
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)),
        )

    def backend(self):
        return OpenRouterBackend(client=self.client())


def sdk_error(behaviour):
    """The exception a real SDK call raises for ``behaviour`` (no retries)."""
    client = Endpoint(behaviour).client().with_options(max_retries=0)
    with pytest.raises(Exception) as caught:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    return caught.value


class Scripted:
    """A completion backend replaying scripted outcomes; records every call."""

    def __init__(self, name, outcomes, classify=None):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []
        self._classify = classify

    def complete(self, system, user, *, model, options=None):
        self.calls.append({"model": model, "options": options})
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def classify_halt(self, exc):
        if self._classify is not None:
            return self._classify(exc)
        return getattr(exc, "kind", None)


def ok(text="ok"):
    return LLMResponse(text=text, model="m", input_tokens=1, output_tokens=1)


def factory_for(backends, calls=None):
    def factory(name, **_kw):
        if calls is not None:
            calls.append(name)
        kind = HARNESS_KIND if name == "opus" else TRANSPORT_KIND
        return BackendSelection(endpoint=name, kind=kind, backend=backends[name], model=f"vendor/{name}")

    return factory


def request():
    return d.RunRequest(system="s", prompt="p")


# --------------------------------------------------------------------------
# R39: expansion
# --------------------------------------------------------------------------


def test_group_members_are_the_front_doors_group_in_walk_order():
    entries = fleet()
    assert frontdoor_group_members(entries["door"], entries) == ["m1", "m2", "cloud"]
    # Only a front door expands (D3), and nothing expands into a front door.
    assert frontdoor_group_members(entries["m1"], entries) == []
    assert frontdoor_group_members(entries["opus"], entries) == []
    assert frontdoor_group_members(front_door("lonely", group="none"), entries) == []


def test_router_down_unit_completes_on_group_member(home, probes):
    """R39: the front door is down; the unit completes on its first member."""
    probes.down = {"door"}
    endpoints = {name: Endpoint() for name in ("door", "m1", "m2", "cloud")}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_COMPLETED
    assert result.entry == "m1"
    assert result.response.text == "answer"
    assert [e.requests for e in endpoints.values()] == [0, 1, 0, 0]
    # Staged probing: the declared entry first, its members only once it is down.
    assert probes.calls == [["door"], ["cloud", "m1", "m2"]]


def test_members_follow_the_front_door_in_routing_order_marked_via(probes):
    probes.down = {"door"}
    ranking = d.describe(["door", "opus"], entries=fleet(), caller="process")
    assert [e.id for e in ranking.rendered_entries] == ["door", "m1", "m2", "cloud", "opus"]
    assert [e.via for e in ranking.rendered_entries] == [None, "door", "door", "door", None]
    assert [e.declared_index for e in ranking.rendered_entries] == [0, 0, 0, 0, 1]
    assert ranking.default.id == "m1"
    assert ranking.expanded_names == ("door", "m1", "m2", "cloud", "opus")
    rendered = ranking.render()
    assert "via door" in rendered.splitlines()[2]
    assert ranking.to_json()["rendered_entries"][1]["via"] == "door"
    assert "via" not in ranking.to_json()["rendered_entries"][0]


def test_router_up_sends_to_router_members_unprobed(home, probes):
    """D1: a usable front door keeps the unit, and no member is probed or resolved."""
    endpoints = {name: Endpoint() for name in ("door", "m1", "m2", "cloud")}
    backends = {name: e.backend() for name, e in endpoints.items()}
    backends["opus"] = Scripted("claude-cli", [ok()])
    resolved = []
    ranking = d.describe(["door", "opus"], entries=fleet(), caller="process",
                         backend_factory=factory_for(backends, resolved))
    assert [e.id for e in ranking.rendered_entries] == ["door", "opus"]
    assert [x.id for x in ranking.dispositions] == ["door", "opus"]
    assert probes.calls == [["door", "opus"]]
    assert resolved == ["door", "opus"]
    probes.calls.clear()
    resolved.clear()
    result = d.run(["door", "opus"], request(), entries=fleet(),
                   backend_factory=factory_for(backends, resolved))
    assert result.entry == "door"
    assert [e.requests for e in endpoints.values()] == [1, 0, 0, 0]
    assert probes.calls == [["door", "opus"]]
    assert "m1" not in resolved and "m2" not in resolved and "cloud" not in resolved


def test_singleton_without_listed_alternatives_has_no_fallback(home, probes):
    """No one else listed, no fallback: the other group's endpoint is never tried."""
    entries = {"solo": member("solo", 1, group="lonely"), "other": member("other", 1, group="h")}
    endpoints = {"solo": Endpoint("refused"), "other": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["solo"], request(), entries=entries, backend_factory=factory_for(backends))
    assert result.status == d.RUN_ATTEMPT_LIMIT
    assert [a.entry for a in result.attempts] == ["solo"]
    assert endpoints["other"].requests == 0
    # A front door with no group members expands to itself only.
    lonely = {"door": front_door(group="nobody"), "other": member("other", 1, group="h")}
    assert d.describe(["door"], entries=lonely, caller="process").expanded_names == ("door",)


def test_explicit_member_keeps_its_slot_and_members_are_not_repeated(probes):
    probes.down = {"door", "door2"}
    entries = fleet(door2=front_door("door2"))
    ranking = d.describe(["door", "opus", "m2", "door2"], entries=entries, caller="process")
    # m2 is the caller's own, after opus; door2 adds nothing door did not.
    assert ranking.expanded_names == ("door", "m1", "cloud", "opus", "m2", "door2")
    assert [e.id for e in ranking.rendered_entries] == ["door", "m1", "cloud", "opus", "m2", "door2"]
    assert [e.via for e in ranking.rendered_entries] == [None, "door", "door", None, None, None]


def test_members_pass_the_filters_themselves(probes):
    """D4 + cross-check 2: allow, exclude and requirements judge each member on its own entry."""
    entries = fleet()
    # The healthy front door is metered (its group spills to the cloud), so
    # allow=["local"] switches it off -- and its local members remain usable.
    ranking = d.describe(["door", "opus"], entries=entries, caller="process", allow=["local"],
                         harness_auth={"claude": d._cost.HarnessAuth("api-key", "metered key")})
    assert [e.id for e in ranking.rendered_entries] == ["m1", "m2"]
    assert [x.id for x in ranking.switched_off] == ["door", "cloud", "opus"]
    assert ranking.default.id == "m1"
    # A caller-excluded front door still expands; a caller-excluded member does not run.
    ranking = d.describe(["door"], entries=entries, caller="process", exclude=["door", "m1"])
    assert [e.id for e in ranking.rendered_entries] == ["m2", "cloud"]
    excluded = {x.id: x.disposition for x in ranking.dispositions}
    assert excluded == {"door": "excluded", "m1": "excluded", "m2": "usable", "cloud": "usable"}


def test_pace_never_reorders_across_a_group_boundary(probes, monkeypatch):
    """Cross-check 1: a paced member cannot overtake its front door's block, nor a
    later declared entry overtake a member; declared entries still pace-sort."""
    probes.down = {"door"}
    spec = ConserveSpec(pool="seven_day")
    entries = fleet(m2=member("m2", 2, spec=spec), opus=harness("opus", spec=spec),
                    sonnet=harness("sonnet", spec=spec))
    paces = {"m2": 0.5, "opus": 2.0, "sonnet": 3.0}
    monkeypatch.setattr(d, "pinned_evaluate", lambda name, spec, harness: SimpleNamespace(
        usable=True, status=d.STATUS_AVAILABLE, detail="", resets_at=None))
    monkeypatch.setattr(d, "_fresh_reading", lambda name, spec, harness: SimpleNamespace(
        remaining=paces[name] * 0.5, window_remaining=0.5))
    ranking = d.describe(["door", "opus", "sonnet"], entries=entries, caller="process")
    # Without blocks the global pace sort would move opus/sonnet into m2's slot.
    assert [e.id for e in ranking.rendered_entries] == ["door", "m1", "m2", "cloud", "sonnet", "opus"]


def test_floor_itemises_expanded_members_via_the_front_door(probes):
    probes.down = {"door", "m1", "m2", "cloud"}
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        d.describe(["door"], entries=fleet(), caller="process")
    floor = caught.value
    assert [(x.id, x.disposition, x.via) for x in floor.dispositions] == [
        ("door", "unreachable", None), ("m1", "unreachable", "door"),
        ("m2", "unreachable", "door"), ("cloud", "unreachable", "door"),
    ]
    assert "m1: unreachable (probe: down) [via door]" in str(floor)
    assert floor.to_json()["dispositions"][1]["via"] == "door"
    assert "via" not in floor.to_json()["dispositions"][0]


# --------------------------------------------------------------------------
# R40: transport failures move the unit on
# --------------------------------------------------------------------------


def test_connection_refused_moves_to_next_member(home, probes):
    """R40: the front door refuses the connection; the next endpoint is called."""
    endpoints = {"door": Endpoint("refused"), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_COMPLETED
    assert result.entry == "m1"
    assert [(a.entry, a.halt, a.outcome) for a in result.attempts] == [
        ("door", "launch", "halted"), ("m1", None, "completed"),
    ]
    # One request each: the SDK's own retries never ran (D2).
    assert [e.requests for e in endpoints.values()] == [1, 1, 0, 0]


def test_group_exhausted_falls_to_next_declaration_entry(home, probes):
    """R40: the front door fails, every member fails, the declared next entry serves."""
    endpoints = {
        "door": Endpoint("refused"), "m1": Endpoint("dns"),
        "m2": Endpoint("connect-timeout"), "cloud": Endpoint("read-timeout"),
    }
    backends = {name: e.backend() for name, e in endpoints.items()}
    backends["opus"] = Scripted("claude-cli", [ok("from opus")])
    result = d.run(["door", "opus"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_COMPLETED
    assert result.entry == "opus"
    assert result.response.text == "from opus"
    assert [a.entry for a in result.attempts] == ["door", "m1", "m2", "cloud", "opus"]
    # Each endpoint was asked exactly once: a failed one stays ruled out.
    assert [e.requests for e in endpoints.values()] == [1, 1, 1, 1]
    assert backends["opus"].calls and backends["opus"].calls[0]["options"].max_retries is None


def test_explicit_max_attempts_is_preserved(home, probes):
    endpoints = {"door": Endpoint("refused"), "m1": Endpoint("refused"), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    backends["opus"] = Scripted("claude-cli", [ok()])
    result = d.run(["door", "opus"], request(), entries=fleet(), backend_factory=factory_for(backends),
                   max_attempts=2)
    assert result.status == d.RUN_ATTEMPT_LIMIT
    assert result.detail == "attempt limit reached (2)"
    assert [a.entry for a in result.attempts] == ["door", "m1"]
    assert endpoints["m2"].requests == 0 and backends["opus"].calls == []


def test_cli_default_attempts_are_sized_by_run(monkeypatch, capsys):
    """The CLI leaves the limit to run(), which counts the expanded declaration."""
    from llm_kit.cli import completion as completion_cli

    seen = {}

    def fake_run(declared, req, **kw):
        seen.update(kw)
        raise d.NoUsableRoutingTarget(declared, (), "process")

    monkeypatch.setattr(completion_cli, "declaration_run", fake_run)
    from llm_kit import cli

    try:
        cli.main(["completion", "complete", "--models", "door,opus", "--prompt", "x"])
    except (SystemExit, d.NoUsableRoutingTarget):
        pass
    assert "max_attempts" in seen and seen["max_attempts"] is None


@pytest.mark.parametrize("behaviour", ["unsupported", "local-protocol"])
def test_client_configuration_error_does_not_fall_through(home, probes, behaviour):
    endpoints = {"door": Endpoint(behaviour), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_FAILED
    assert result.detail.startswith("task error:")
    assert [e.requests for e in endpoints.values()] == [1, 0, 0, 0]


@pytest.mark.parametrize("behaviour", ["error500", "bad400", "dropped"])
def test_bad_response_does_not_fall_through(home, probes, behaviour):
    endpoints = {"door": Endpoint(behaviour), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_FAILED
    assert [e.requests for e in endpoints.values()] == [1, 0, 0, 0]


@pytest.mark.parametrize("error", [ValueError("model produced garbage"), KeyError("choices")])
def test_task_and_programming_errors_do_not_fall_through(probes, error):
    backends = {name: Scripted("openrouter", [ok()], halt.classify_openai_exception)
                for name in ("m1", "m2", "cloud")}
    backends["door"] = Scripted("openrouter", [error], halt.classify_openai_exception)
    result = d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_FAILED
    assert [len(backends[n].calls) for n in ("door", "m1", "m2", "cloud")] == [1, 0, 0, 0]


# --------------------------------------------------------------------------
# R41: a busy endpoint is skipped at once, with no quota halt
# --------------------------------------------------------------------------


@pytest.mark.parametrize("behaviour", ["busy503", "busy429"])
def test_busy_member_skipped_without_halt_or_wait(home, probes, monkeypatch, behaviour):
    """R41: a busy front door hands the unit on; no SDK backoff, no recorded halt."""
    def no_halt(*_a, **_kw):
        raise AssertionError("a busy endpoint must not record a quota halt")

    monkeypatch.setattr(d, "record_observed_halt", no_halt)
    spec = ConserveSpec(pool="seven_day")
    entries = fleet(door=EndpointEntry(id="door", base_url="http://door.invalid/v1", model="g",
                                       frontdoor=True, conserve_usage=spec))
    monkeypatch.setattr(d, "pinned_evaluate", lambda name, spec, harness: SimpleNamespace(
        usable=True, status=d.STATUS_AVAILABLE, detail="", resets_at=None))
    monkeypatch.setattr(d, "_fresh_reading", lambda name, spec, harness: None)
    endpoints = {"door": Endpoint(behaviour), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], request(), entries=entries, backend_factory=factory_for(backends))
    assert result.status == d.RUN_COMPLETED
    assert result.entry == "m1"
    assert [(a.entry, a.halt) for a in result.attempts] == [("door", "backpressure"), ("m1", None)]
    assert endpoints["door"].requests == 1  # no SDK retry wait on the busy endpoint


def test_explicit_max_retries_is_sent_as_given(probes):
    backends = {"door": Scripted("openrouter", [ok()])}
    result = d.run(["door"], d.RunRequest(system="s", prompt="p", options=BackendOptions(max_retries=3)),
                   entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_COMPLETED
    assert backends["door"].calls[0]["options"].max_retries == 3
    backends = {"door": Scripted("openrouter", [ok()])}
    d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert backends["door"].calls[0]["options"].max_retries == 0


# --------------------------------------------------------------------------
# The classifier, on real SDK exceptions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("behaviour", "verdict"), [
    ("refused", halt.ENDPOINT_UNREACHABLE),
    ("dns", halt.ENDPOINT_UNREACHABLE),
    ("connect-timeout", halt.ENDPOINT_UNREACHABLE),
    ("read-timeout", halt.ENDPOINT_TIMEOUT),
    ("unsupported", None),
    ("local-protocol", None),
    ("dropped", None),
    ("error500", None),
    ("bad400", None),
    ("busy503", None),
])
def test_endpoint_failure_classification_of_real_sdk_errors(behaviour, verdict):
    exc = sdk_error(behaviour)
    assert isinstance(exc, openai.OpenAIError)
    assert halt.classify_endpoint_failure(exc) == verdict


@pytest.mark.parametrize(("behaviour", "reason"), [
    ("refused", d.FALLTHROUGH_UNREACHABLE),
    ("read-timeout", d.FALLTHROUGH_TIMEOUT),
    ("busy503", d.FALLTHROUGH_BUSY),
    ("busy429", d.FALLTHROUGH_BUSY),
    ("error500", None),
    ("unsupported", None),
])
def test_fallthrough_reason(behaviour, reason):
    assert d.fallthrough_reason(sdk_error(behaviour)) == reason


def test_unrelated_error_raised_while_handling_a_connection_failure_is_not_one():
    refused = sdk_error("refused")
    try:
        try:
            raise refused
        except openai.APIConnectionError:
            raise KeyError("bug in the handler")
    except KeyError as exc:
        assert exc.__context__ is refused
        assert halt.classify_endpoint_failure(exc) is None
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = refused
    assert halt.classify_endpoint_failure(wrapped) is None


def test_cause_chain_walk_is_bounded_and_cycle_safe():
    request_ = httpx2.Request("POST", "http://x.invalid/v1")
    a = openai.APIConnectionError(request=request_)
    b = openai.APIConnectionError(request=request_)
    a.__cause__, b.__cause__ = b, a
    assert halt.classify_endpoint_failure(a) is None
    deep = httpx2.ConnectError("refused", request=request_)
    for _ in range(10):
        outer = openai.APIConnectionError(request=request_)
        outer.__cause__ = deep
        deep = outer
    assert halt.classify_endpoint_failure(deep) is None  # beyond the bound
    near = openai.APIConnectionError(request=request_)
    near.__cause__ = httpx2.ConnectError("refused", request=request_)
    assert halt.classify_endpoint_failure(near) == halt.ENDPOINT_UNREACHABLE


# --------------------------------------------------------------------------
# The pipeline: route() -> DeclarationBackend
# --------------------------------------------------------------------------

_REGISTRY = """\
version: 1
default: door
models:
  cloud: {base_url: http://cloud.invalid/v1, model: cloud-model, routing: {group: g, order: 3}}
  door: {base_url: http://door.invalid/v1, model: g, frontdoor: true}
  m2: {base_url: http://m2.invalid/v1, model: m2-model, billing: {mode: unmetered, owner: self}, routing: {group: g, order: 2}}
  m1: {base_url: http://m1.invalid/v1, model: m1-model, billing: {mode: unmetered, owner: self}, routing: {group: g, order: 1}}
  solo: {base_url: http://solo.invalid/v1, model: solo-model}
"""


class PipelineFake:
    """A pipeline-side backend: scripted outcomes, the pipeline response type."""

    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, system, user, *, model, options=None):
        self.calls.append({"model": model, "options": options})
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def classify_halt(self, exc):
        from llm_kit.pipeline.runtime import backends

        return backends.platform.HALT_UNREACHABLE if backends._is_connection_error(exc) else None


@pytest.fixture()
def pipeline(home, tmp_path, monkeypatch, probes):
    from llm_kit.pipeline.runtime import backends

    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    fakes = {}
    monkeypatch.setattr(backends, "_backend_for_entry", lambda entry: fakes[entry.id])
    backends.reset_declared_entry_cache()
    yield SimpleNamespace(backends=backends, fakes=fakes, probes=probes)
    backends.reset_declared_entry_cache()


def _presp(text):
    from llm_kit.pipeline.runtime.platform import LLMResponse as PipelineResponse

    return PipelineResponse(text=text, model="served")


def test_pipeline_route_falls_through_to_group_member_in_call(pipeline):
    """R40 on the pipeline path: the front door refuses, the call completes on m1,
    and the next call goes straight to m1 (an unreachable endpoint stays ruled out)."""
    b = pipeline.backends
    pipeline.fakes.update({
        "door": PipelineFake("model-endpoint", [sdk_error("refused")]),
        "m1": PipelineFake("model-endpoint", [_presp("first"), _presp("second")]),
    })
    models = ["door", "opus"]
    backend = b.route(models=models)
    assert isinstance(backend, b.DeclarationBackend)
    model = b.routed_model("", models=models)
    assert model == "g"
    response = b.platform.call_llm(backend, "s", "u", model=model)
    assert response.text == "first"
    assert pipeline.fakes["m1"].calls[0]["model"] == "m1-model"
    assert pipeline.fakes["door"].calls[0]["options"].max_retries == 0
    assert pipeline.fakes["m1"].calls[0]["options"].max_retries == 0
    again = b.route(models=models).complete("s", "u", model=model)
    assert again.text == "second"
    assert len(pipeline.fakes["door"].calls) == 1


def test_pipeline_busy_endpoint_is_skipped_for_that_call_only(pipeline):
    """R41 on the pipeline path: busy is not remembered, so the next call tries the front door again."""
    b = pipeline.backends
    pipeline.fakes.update({
        "door": PipelineFake("model-endpoint", [sdk_error("busy503"), _presp("door later")]),
        "m1": PipelineFake("model-endpoint", [_presp("m1 now")]),
    })
    backend = b.route(models=["door"])
    assert backend.complete("s", "u", model="g").text == "m1 now"
    assert b.route(models=["door"]).complete("s", "u", model="g").text == "door later"


def test_pipeline_exhausted_declaration_raises_the_last_endpoints_failure(pipeline):
    b = pipeline.backends
    pipeline.fakes.update({name: PipelineFake("model-endpoint", [sdk_error("refused")])
                           for name in ("door", "m1", "m2", "cloud")})
    backend = b.route(models=["door"])
    with pytest.raises(b.platform.PipelineHaltError) as caught:
        b.platform.call_llm(backend, "s", "u", model="g")
    assert caught.value.kind == b.platform.HALT_UNREACHABLE
    assert [len(pipeline.fakes[n].calls) for n in ("door", "m1", "m2", "cloud")] == [1, 1, 1, 1]


def test_pipeline_non_fallthrough_error_propagates_unchanged(pipeline):
    b = pipeline.backends
    boom = ValueError("bad answer")
    pipeline.fakes.update({"door": PipelineFake("model-endpoint", [boom]),
                           "m1": PipelineFake("model-endpoint", [_presp("never")])})
    with pytest.raises(ValueError) as caught:
        b.route(models=["door"]).complete("s", "u", model="g")
    assert caught.value is boom
    assert pipeline.fakes["m1"].calls == []


def test_pipeline_single_entry_declaration_returns_the_plain_backend(pipeline):
    b = pipeline.backends
    solo = PipelineFake("openrouter", [_presp("solo")])
    pipeline.fakes["solo"] = solo
    assert b.route(models=["solo"]) is solo


# --------------------------------------------------------------------------
# The code-review lane
# --------------------------------------------------------------------------

_LANE = "lane_x"


@pytest.fixture()
def review_lane(home, tmp_path, monkeypatch, probes):
    """``llm_kit.dispatch.review_lane`` imported over stub ``bootstrap_lib`` lane modules."""
    import json

    class LaneOutputError(Exception):
        pass

    prompts = types.ModuleType("bootstrap_lib.code_review.lane_prompts")
    prompts.ENDPOINT_ELIGIBLE_LANES = frozenset({_LANE})
    prompts.KNOWN_LANES = frozenset({_LANE})
    prompts.LANE_PROMPTS = {_LANE: SimpleNamespace(system="review")}
    prompts.LANES_REQUIRING_AGENT_LOOP = frozenset()
    prompts.PROMPT_VERSION = "test"
    prompts.LaneOutputError = LaneOutputError
    prompts.build_user_message = lambda lane, **_kw: "diff"
    prompts.is_agent_alias = lambda model: False
    prompts.parse_issue_array = lambda text, **_kw: json.loads(text)
    output = types.ModuleType("bootstrap_lib.code_review.lane_output")
    output.claude_mds_by_file = lambda *_a, **_kw: {}
    output.load_bundle = lambda *_a, **_kw: {}
    for name, module in (
        ("bootstrap_lib", types.ModuleType("bootstrap_lib")),
        ("bootstrap_lib.code_review", types.ModuleType("bootstrap_lib.code_review")),
        ("bootstrap_lib.code_review.lane_prompts", prompts),
        ("bootstrap_lib.code_review.lane_output", output),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(_runtime, "fleet", lambda: True)
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    sys.modules.pop("llm_kit.dispatch.review_lane", None)
    module = importlib.import_module("llm_kit.dispatch.review_lane")
    fakes = {}
    delivers = {"door", "m1", "m2", "cloud"}

    def create_backend(name, **_kw):
        if name not in fakes:
            raise module.EndpointResolveError(f"unknown endpoint {name!r}")
        params = {"effort": SimpleNamespace(emits="top-level")} if name in delivers else {}
        return BackendSelection(endpoint=name, kind=TRANSPORT_KIND, backend=fakes[name],
                                model=f"{name}-model", capabilities=SimpleNamespace(params=params))

    monkeypatch.setattr(module, "create_backend", create_backend)
    yield SimpleNamespace(module=module, fakes=fakes, delivers=delivers)
    sys.modules.pop("llm_kit.dispatch.review_lane", None)


def _lane(module):
    return module.run_lane(lane=_LANE, model="door", effort="high", diff_text="d")


def test_review_lane_falls_through_to_group_member(review_lane):
    review_lane.fakes.update({"door": Scripted("openrouter", [sdk_error("refused")]),
                              "m1": Scripted("openrouter", [ok("[]")])})
    envelope = _lane(review_lane.module)
    assert envelope["endpoint"] == "m1"
    assert envelope["configured_model"] == "door"
    assert review_lane.fakes["m1"].calls[0]["options"].max_retries == 0


def test_review_lane_busy_endpoint_hands_the_call_on(review_lane):
    review_lane.fakes.update({"door": Scripted("openrouter", [sdk_error("busy503")]),
                              "m1": Scripted("openrouter", [ok("[]")])})
    assert _lane(review_lane.module)["endpoint"] == "m1"
    assert len(review_lane.fakes["door"].calls) == 1


def test_review_lane_skips_a_member_that_cannot_serve_the_lane(review_lane):
    review_lane.delivers.discard("m1")  # m1 would drop the stated effort
    review_lane.fakes.update({"door": Scripted("openrouter", [sdk_error("refused")]),
                              "m1": Scripted("openrouter", [ok("[]")]),
                              "m2": Scripted("openrouter", [ok("[]")])})
    assert _lane(review_lane.module)["endpoint"] == "m2"
    assert review_lane.fakes["m1"].calls == []


def test_review_lane_task_error_does_not_fall_through(review_lane):
    review_lane.fakes.update({"door": Scripted("openrouter", [sdk_error("error500")]),
                              "m1": Scripted("openrouter", [ok("[]")])})
    with pytest.raises(review_lane.module.LaneRunError, match="'door' failed"):
        _lane(review_lane.module)
    assert review_lane.fakes["m1"].calls == []


def test_pipeline_options_carry_max_retries_across_the_seam():
    from llm_kit.pipeline.runtime import backends
    from llm_kit.pipeline.runtime.platform import BackendOptions as PipelineOptions

    assert backends._to_completion_options(PipelineOptions(max_retries=0)).max_retries == 0
    assert backends._to_completion_options(PipelineOptions()).max_retries is None


def test_caller_deadline_read_timeout_does_not_fall_through(home, probes):
    """A request that set its own timeout_s owns a post-connect timeout; a connect
    timeout still says the endpoint is unreachable."""
    options = BackendOptions(timeout_s=30)
    endpoints = {"door": Endpoint("read-timeout"), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], d.RunRequest(system="s", prompt="p", options=options), entries=fleet(),
                   backend_factory=factory_for(backends))
    assert result.status == d.RUN_FAILED
    assert [e.requests for e in endpoints.values()] == [1, 0, 0, 0]
    endpoints = {"door": Endpoint("connect-timeout"), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    result = d.run(["door"], d.RunRequest(system="s", prompt="p", options=options), entries=fleet(),
                   backend_factory=factory_for(backends))
    assert result.entry == "m1"


def test_fallthrough_reason_respects_a_caller_deadline():
    assert d.fallthrough_reason(sdk_error("read-timeout"), caller_deadline=True) is None
    assert d.fallthrough_reason(sdk_error("connect-timeout"), caller_deadline=True) == d.FALLTHROUGH_UNREACHABLE
    assert d.fallthrough_reason(sdk_error("busy503"), caller_deadline=True) == d.FALLTHROUGH_BUSY


def test_busy_falls_through_when_the_backend_does_not_classify_it(probes):
    backends = {name: Scripted("openrouter", [ok(name)]) for name in ("m1", "m2", "cloud")}
    backends["door"] = Scripted("openrouter", [sdk_error("busy503")])  # its classify_halt says None
    result = d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.entry == "m1"
    assert [(a.entry, a.halt) for a in result.attempts] == [("door", "backpressure"), ("m1", None)]


def test_review_lane_own_deadline_read_timeout_does_not_fall_through(review_lane):
    review_lane.fakes.update({"door": Scripted("openrouter", [sdk_error("read-timeout")]),
                              "m1": Scripted("openrouter", [ok("[]")])})
    with pytest.raises(review_lane.module.LaneRunError, match="'door' failed"):
        _lane(review_lane.module)
    assert review_lane.fakes["m1"].calls == []


def test_pipeline_own_deadline_read_timeout_does_not_fall_through(pipeline):
    from llm_kit.pipeline.runtime.platform import BackendOptions as PipelineOptions

    b = pipeline.backends
    timeout = sdk_error("read-timeout")
    pipeline.fakes.update({"door": PipelineFake("model-endpoint", [timeout]),
                           "m1": PipelineFake("model-endpoint", [_presp("never")])})
    with pytest.raises(openai.APITimeoutError):
        b.route(models=["door"]).complete("s", "u", model="g", options=PipelineOptions(timeout_s=30))
    assert pipeline.fakes["m1"].calls == []


# --------------------------------------------------------------------------
# u3b item 1: the front door's own group-exhausted 502
# --------------------------------------------------------------------------


class FrontDoorApp:
    """The REAL front-door server app (``llm_kit.frontdoor.server.create_app``) over a
    group whose every deployment fails, reached by a real OpenAI client.

    ``members`` is ``"raise"`` (each upstream call raises a connect error) or
    ``"503"`` (each upstream answers 503); either way the server walks the whole
    group and answers its exhaustion response. No socket is opened: upstream
    calls hit a fake ``httpx.AsyncClient``, and the OpenAI client's ``httpx2``
    transport forwards into a starlette ``TestClient``.
    """

    def __init__(self, monkeypatch, tmp_path, members="raise"):
        pytest.importorskip("fastapi")
        import httpx
        from fastapi.testclient import TestClient

        from llm_kit.frontdoor import server
        from llm_kit.registry.model_endpoints import EndpointRegistry

        self.upstream = []
        upstream = self.upstream

        class Upstream503:
            status_code = 503
            content = b'{"error": {"message": "overloaded upstream"}}'
            headers = {"content-type": "application/json"}

        class FailingAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def post(self, url, json=None, headers=None):
                upstream.append(url)
                if members == "raise":
                    raise httpx.ConnectError("[Errno 61] Connection refused")
                return Upstream503()

            async def aclose(self):
                pass

        monkeypatch.setattr("httpx.AsyncClient", FailingAsyncClient)
        registry = EndpointRegistry(entries={
            "m1": member("m1", 1), "m2": member("m2", 2), "cloud": member("cloud", 3, local=False),
        })
        self.client = TestClient(server.create_app(registry, access_log=tmp_path / "access.jsonl"))
        self.requests = 0

    def handler(self, request):
        import json

        self.requests += 1
        body = json.loads(request.content)
        body["model"] = "g"  # the group the app serves, whatever slug the caller sent
        answer = self.client.post(request.url.path, json=body)
        return httpx2.Response(answer.status_code, headers=dict(answer.headers), content=answer.content)

    def backend(self):
        client = openai.OpenAI(api_key="test", base_url="http://door.invalid/v1",
                               http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)))
        return OpenRouterBackend(client=client)

    def error(self):
        client = openai.OpenAI(api_key="test", base_url="http://door.invalid/v1", max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)))
        with pytest.raises(openai.APIStatusError) as caught:
            client.chat.completions.create(model="g", messages=[{"role": "user", "content": "x"}])
        return caught.value


@pytest.mark.parametrize("members", ["raise", "503"])
def test_real_front_door_exhaustion_is_recognised(monkeypatch, tmp_path, members):
    app = FrontDoorApp(monkeypatch, tmp_path, members)
    exc = app.error()
    assert exc.status_code == 502
    assert len(app.upstream) == 3  # the server walked its whole group itself
    assert halt.is_frontdoor_group_exhausted(exc)
    assert d.fallthrough_reason(exc) == d.FALLTHROUGH_GROUP_EXHAUSTED


def test_router_group_exhausted_falls_to_next_declaration_entry(home, probes, monkeypatch, tmp_path):
    """The router is up, every member fails inside it: the unit goes to the next
    DECLARED entry, and no member is called directly (the router already tried them)."""
    app = FrontDoorApp(monkeypatch, tmp_path)
    endpoints = {name: Endpoint() for name in ("m1", "m2", "cloud")}
    backends = {name: e.backend() for name, e in endpoints.items()}
    backends["door"] = app.backend()
    backends["opus"] = Scripted("claude-cli", [ok("from opus")])
    result = d.run(["door", "m2", "opus"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_COMPLETED
    assert result.entry == "opus"
    assert [(a.entry, a.halt) for a in result.attempts] == [("door", "launch"), ("opus", None)]
    assert app.requests == 1
    # m2 is declared on its own, but it is in the exhausted group: skipped too.
    assert [e.requests for e in endpoints.values()] == [0, 0, 0]


def test_router_group_exhausted_with_no_one_else_listed_fails(home, probes, monkeypatch, tmp_path):
    app = FrontDoorApp(monkeypatch, tmp_path)
    endpoints = {name: Endpoint() for name in ("m1", "m2", "cloud")}
    backends = {name: e.backend() for name, e in endpoints.items()}
    backends["door"] = app.backend()
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        d.run(["door"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert [(x.id, x.detail) for x in caught.value.dispositions] == [
        ("door", "ruled out by the caller"),
        ("m1", "door reported its group exhausted"),
        ("m2", "door reported its group exhausted"),
        ("cloud", "door reported its group exhausted"),
    ]
    assert app.requests == 1
    assert [e.requests for e in endpoints.values()] == [0, 0, 0]


def test_pipeline_group_exhausted_goes_to_next_declared_entry(pipeline, monkeypatch, tmp_path):
    b = pipeline.backends
    exhausted = FrontDoorApp(monkeypatch, tmp_path).error()
    pipeline.fakes.update({
        "door": PipelineFake("model-endpoint", [exhausted]),
        "m1": PipelineFake("model-endpoint", [_presp("never")]),
        "solo": PipelineFake("model-endpoint", [_presp("from solo")]),
    })
    assert b.route(models=["door", "solo"]).complete("s", "u", model="g").text == "from solo"
    assert pipeline.fakes["m1"].calls == []


class Plain502(Endpoint):
    def handler(self, request):
        if self.behaviour == "bad502":
            self.requests += 1
            return httpx2.Response(502, json={"error": {"message": "all deployments failed"}})
        if self.behaviour == "header500":
            self.requests += 1
            return httpx2.Response(500, headers={halt.FRONTDOOR_DEPLOYMENT_HEADER: "m1"},
                                   json={"error": {"message": "all deployments failed"}})
        return super().handler(request)


@pytest.mark.parametrize("behaviour", ["bad502", "header500", "error500"])
def test_other_502_and_5xx_stay_bad_responses(home, probes, behaviour):
    """Only the front door's 502 WITH its deployment header moves the unit; the
    same wording without the header, or the header on another status, does not."""
    endpoints = {"door": Plain502(behaviour), "m1": Endpoint(), "m2": Endpoint(), "cloud": Endpoint()}
    backends = {name: e.backend() for name, e in endpoints.items()}
    backends["opus"] = Scripted("claude-cli", [ok()])
    result = d.run(["door", "opus"], request(), entries=fleet(), backend_factory=factory_for(backends))
    assert result.status == d.RUN_FAILED
    assert [e.requests for e in endpoints.values()] == [1, 0, 0, 0]
    assert backends["opus"].calls == []


# --------------------------------------------------------------------------
# u3b item 2: a front door used alone on the CLI single-call path
# --------------------------------------------------------------------------

_CLI_REGISTRY = """\
version: 1
default: door
models:
  cloud: {base_url: http://cloud.invalid/v1, model: cloud-model, routing: {group: g, order: 3}}
  door: {base_url: http://door.invalid/v1, model: g, frontdoor: true}
  m2: {base_url: http://m2.invalid/v1, model: m2-model, billing: {mode: unmetered, owner: self}, routing: {group: g, order: 2}}
  m1: {base_url: http://m1.invalid/v1, model: m1-model, billing: {mode: unmetered, owner: self}, routing: {group: g, order: 1}}
  solo: {base_url: http://solo.invalid/v1, model: solo-model}
"""
_BASE_KEYS = {"protocol", "endpoint", "kind", "backend", "response"}


@pytest.fixture()
def cli(home, tmp_path, monkeypatch, probes, capsys):
    import json

    from llm_kit import cli as cli_module
    from llm_kit.dispatch import factory

    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _CLI_REGISTRY)))
    # Every registry id resolves; one a test does not script fails loudly if called.
    backends = {name: Scripted("openrouter", []) for name in ("door", "m1", "m2", "cloud", "solo")}

    def create_backend(endpoint=None, *, model=None, cheap=False, project_root=None):
        name = endpoint or "door"
        return BackendSelection(endpoint=name, kind=TRANSPORT_KIND, backend=backends[name],
                                model=model or f"{name}-model")

    monkeypatch.setattr(factory, "create_backend", create_backend)

    def complete(*argv):
        capsys.readouterr()
        code = cli_module.main(["completion", "complete", *argv, "--prompt", "x"])
        out = capsys.readouterr().out
        return code, json.loads(out)

    return SimpleNamespace(backends=backends, complete=complete, probes=probes)


def _openai_scripted(outcomes):
    return Scripted("openrouter", outcomes, halt.classify_openai_exception)


@pytest.mark.parametrize("argv", [("--models", "door"), ()])
def test_cli_single_front_door_falls_through_to_member(cli, argv):
    """`complete --models <door>` and the default entry (a front door) fall through."""
    cli.backends.update({"door": _openai_scripted([sdk_error("refused")]),
                         "m1": _openai_scripted([ok("from m1")])})
    code, envelope = cli.complete(*argv)
    assert code == 0
    assert envelope["endpoint"] == "m1"
    assert envelope["response"]["text"] == "from m1"
    assert set(envelope) == _BASE_KEYS | {"fell_through"}
    assert [(f["entry"], f["reason"]) for f in envelope["fell_through"]] == [("door", "unreachable")]
    assert cli.backends["door"].calls[0]["options"].max_retries == 0


def test_cli_healthy_single_front_door_envelope_and_probes_are_unchanged(cli):
    cli.backends.update({"door": _openai_scripted([ok("from door")]), "m1": _openai_scripted([ok()])})
    code, envelope = cli.complete()
    assert code == 0
    assert set(envelope) == _BASE_KEYS
    assert envelope["endpoint"] == "door"
    assert cli.probes.calls == []  # the default path still probes nothing
    assert cli.backends["m1"].calls == []


def test_cli_single_front_door_all_failing_keeps_the_one_call_failure_shape(cli):
    for name in ("door", "m1", "m2", "cloud"):
        cli.backends[name] = _openai_scripted([sdk_error("refused")])
    code, envelope = cli.complete("--models", "door")
    assert code == 1  # EXIT_FAILURE, as a refused one-call request always was
    assert envelope["response"]["error"]["code"] == "execution"
    assert "halt" not in envelope
    assert envelope["endpoint"] == "cloud"
    assert [f["entry"] for f in envelope["fell_through"]] == ["door", "m1", "m2", "cloud"]


def test_cli_single_front_door_group_exhausted_has_no_one_else_to_try(cli, monkeypatch, tmp_path):
    exhausted = FrontDoorApp(monkeypatch, tmp_path).error()
    cli.backends.update({"door": _openai_scripted([exhausted]), "m1": _openai_scripted([ok()])})
    code, envelope = cli.complete("--models", "door")
    assert code == 1
    assert cli.backends["m1"].calls == []
    assert [f["reason"] for f in envelope["fell_through"]] == ["group-exhausted"]


def test_cli_single_non_front_door_keeps_its_one_call(cli):
    cli.backends.update({"solo": _openai_scripted([sdk_error("refused")]), "m1": _openai_scripted([ok()])})
    code, envelope = cli.complete("--models", "solo")
    assert code == 1
    assert set(envelope) == _BASE_KEYS
    assert cli.backends["m1"].calls == []
    assert cli.backends["solo"].calls[0]["options"].max_retries is None


def test_an_exhausted_front_door_is_not_recorded_unreachable(home, monkeypatch, tmp_path):
    """The front door answered: a caller's reachability cache must not mark it down."""
    app = FrontDoorApp(monkeypatch, tmp_path)
    entries = fleet()
    cache = {name: Reachability("reachable", "test", "ok") for name in entries}
    backends = {"door": app.backend(), "opus": Scripted("claude-cli", [ok()])}
    result = d.run(["door", "opus"], request(), entries=entries, backend_factory=factory_for(backends),
                   reachability_cache=cache)
    assert result.entry == "opus"
    assert cache["door"].status == "reachable"
