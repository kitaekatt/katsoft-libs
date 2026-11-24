"""The production labeller (``llm_kit.graph.render.label_model``): consent, one gate, text-only calls.

No model runs. The seams of ``model_labeller`` are faked: the consent policy, the harness
authentication, the registry entries and the backend factory. The backends are the real
``ClaudeCliBackend`` over a fake ``runner`` (so the argv and the working directory are the shipped
ones) and a scripted transport for the front-door case; reachability probes are replaced.

Properties pinned here, each with the check that shows it can fail:

- S4  consent before any backend: a refusal sends nothing and names the exact command.
- S5  every call is armed text-only (the backend that completes is in text-only mode).
- S10 a fallback cannot reach an entry the consent gate refused (a front door's unapproved members).
- finding 6  a follow-up is served only by the entry that served the primary call.
- finding 7  every call runs in, and probes authentication in, the project root, whatever the cwd.
- the labeller is safe to call from four threads and runs its gate once.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.capabilities import _CLAUDE_TEXT_ONLY_ARGS
from llm_kit.completion import halt
from llm_kit.completion.types import LLMResponse
from llm_kit.dispatch import declaration as d
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.dispatch.reachability import Reachability
from llm_kit.graph.render import label_model
from llm_kit.graph.render.label_model import (
    CONSENT_SCOPE,
    DEFAULT_LABEL_MODELS,
    DEFAULT_TIMEOUT_S,
    model_labeller,
)
from llm_kit.graph.render.label_types import LabelCall, LabelError, LabelFailed, LabelRefused
from llm_kit.registry import consent
from llm_kit.registry.cost import METERED, SUBSCRIPTION, HarnessAuth
from llm_kit.registry.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry, RoutingConfig

SCHEMA = {
    "type": "object",
    "required": ["nodes", "edges"],
    "additionalProperties": False,
    "properties": {"nodes": {"type": "object"}, "edges": {"type": "object"}},
}
GOOD = {"nodes": {"n": {"title": "T", "description": "D"}}, "edges": {}}
SUBSCRIBED = {"claude": HarnessAuth(SUBSCRIPTION, "test subscription")}


def make_call(index: int = 0, *, followup: bool = False, entry: Optional[str] = None, file: str = "graphs/a.graph.yaml") -> LabelCall:
    return LabelCall(
        file=file, index=index, header=True, nodes=("n",), edges=(), inputs=(), outputs=(),
        system="You write JSON only.", prompt=f"label {file} call {index}", schema=SCHEMA,
        key="k" * 64, followup=followup, entry=entry,
    )


# --------------------------------------------------------------------------
# Fakes for the dispatch seams
# --------------------------------------------------------------------------


class Runner:
    """The ``runner`` seam of ``ClaudeCliBackend``: records argv, prompt and cwd; answers from a script."""

    def __init__(self, answer: Any = GOOD, *, model: str = "claude-haiku-5-5", barrier: Optional[threading.Barrier] = None,
                 quota_halt: bool = False) -> None:
        self.answer = answer
        self.model = model
        self.barrier = barrier
        self.quota_halt = quota_halt
        self.calls: list = []
        self._lock = threading.Lock()

    def __call__(self, cmd, request, cwd, **_kw):
        with self._lock:
            self.calls.append({"cmd": list(cmd), "request": request, "cwd": cwd})
        if self.barrier is not None:
            self.barrier.wait(timeout=20)
        if self.quota_halt:
            return json.dumps({"result": "You have hit your limit", "is_error": True, "api_error_status": 429}), "", 0
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return json.dumps({"result": text, "is_error": False, "usage": {}, "modelUsage": {self.model: {}}}), "", 0


@dataclass
class RecordingClaude(ClaudeCliBackend):
    """A real ``ClaudeCliBackend`` that notes the mode it completes in (``replace`` keeps the shared list)."""

    seen: list = field(default_factory=list)

    def complete(self, system, user, *, model, options=None):
        self.seen.append({"text_only": self.text_only, "model": model, "options": options})
        return super().complete(system, user, model=model, options=options)


class Scripted:
    """A transport backend with scripted outcomes; no tool surface, so a guarantee needs no arming."""

    name = "openai-compat"

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, system, user, *, model, options=None):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def classify_halt(self, exc):
        return getattr(exc, "kind", None)


class Halted(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"halted: {kind}")
        self.kind = kind


def claude_entry(entry_id: str, model: str) -> EndpointEntry:
    return EndpointEntry(id=entry_id, base_url=None, model=model, kind=HARNESS_KIND, harness="claude")


def policy_for(**classes: bool):
    """A ``policy_loader`` over a stated scope; ``loads`` counts how often the labeller reads consent."""
    config = consent.ConsentConfig(globals_={}, scopes={CONSENT_SCOPE: dict(classes)}, source="file", path=Path("consent.json"))
    loads: list = []

    def loader(scope):
        loads.append(scope)
        return consent.effective(config, scope)

    loader.loads = loads  # type: ignore[attr-defined]
    return loader


class World:
    """The seams of one labeller, and a record of what reached them."""

    def __init__(self, monkeypatch, tmp_path: Path, *, entries=None, runners=None, policy=None, auth=SUBSCRIBED) -> None:
        self.root = tmp_path / "project"
        self.root.mkdir()
        self.elsewhere = tmp_path / "elsewhere"
        self.elsewhere.mkdir()
        self.entries = entries if entries is not None else {"haiku": claude_entry("haiku", "haiku")}
        self.runners = runners if runners is not None else {"haiku": Runner()}
        self.policy = policy if policy is not None else policy_for(subscription=True)
        self.auth = auth
        self.backends: dict = {}
        self.built: list = []        # (entry id, project_root kwarg) of every backend the factory made
        self.probes: list = []       # (ids, project_root) of every reachability probe
        monkeypatch.setattr(d, "check_many", self._check_many)
        monkeypatch.chdir(self.elsewhere)

    def _check_many(self, entries, **kw):
        self.probes.append((sorted(entries), kw.get("project_root")))
        return {name: Reachability("reachable", "test", "ok") for name in entries}

    def factory(self, name, **kw):
        self.built.append((name, kw.get("project_root")))
        if name in self.backends:
            return self.backends[name]
        entry = self.entries[name]
        backend = self.runner_backend(name) if entry.kind == HARNESS_KIND else self.runners[name]
        selection = BackendSelection(endpoint=name, kind=entry.kind, backend=backend, model=entry.model)
        self.backends[name] = selection
        return selection

    def runner_backend(self, name):
        return RecordingClaude(runner=self.runners[name], executable="claude")

    def labeller(self, declaration=DEFAULT_LABEL_MODELS, **overrides):
        kw = dict(project_root=self.root, policy_loader=self.policy, harness_auth=self.auth,
                  entries=self.entries, backend_factory=self.factory)
        kw.update(overrides)
        return model_labeller(declaration, **kw)

    def completed(self) -> int:
        return sum(len(r.calls) for r in self.runners.values() if isinstance(r, Runner))


@pytest.fixture()
def world(monkeypatch, tmp_path):
    return World(monkeypatch, tmp_path)


# --------------------------------------------------------------------------
# The constructor and the happy path
# --------------------------------------------------------------------------


def test_the_defaults_are_the_documented_ones(world):
    assert CONSENT_SCOPE == "graph-labels"
    assert DEFAULT_LABEL_MODELS == ("haiku",)
    assert DEFAULT_TIMEOUT_S == 120.0
    assert world.labeller().declaration == ("haiku",)
    assert world.labeller(["haiku", "sonnet"]).declaration == ("haiku", "sonnet")


@pytest.mark.parametrize("bad", [(), [], ("haiku", "haiku"), ("haiku", ""), ("haiku", 3), "haiku"])
def test_a_malformed_declaration_is_refused_when_the_labeller_is_built(world, bad):
    with pytest.raises(ValueError):
        world.labeller(bad)


def test_building_the_labeller_reads_no_consent_and_calls_nothing(world):
    world.labeller()
    assert world.policy.loads == [] and world.built == [] and world.probes == [] and world.completed() == 0


def test_a_call_returns_the_parsed_answer_the_serving_entry_and_the_model_that_answered(world):
    reply = world.labeller()(make_call())
    assert reply.answer == GOOD
    assert reply.entry == "haiku"
    assert reply.model == "claude-haiku-5-5"
    assert world.policy.loads == [CONSENT_SCOPE]
    # the call carried the plan's own system and prompt, and the requested model
    (call,) = world.runners["haiku"].calls
    assert call["request"] == "label graphs/a.graph.yaml call 0"
    assert call["cmd"][call["cmd"].index("--system-prompt") + 1] == "You write JSON only."
    assert call["cmd"][call["cmd"].index("--model") + 1] == "haiku"


# --------------------------------------------------------------------------
# S5: text-only
# --------------------------------------------------------------------------


def test_label_calls_are_armed_text_only(world):
    """S5. Revert check: drop GUARANTEES from ``run`` and the backend completes in the unarmed mode."""
    world.labeller()(make_call())
    (seen,) = world.backends["haiku"].backend.seen
    assert seen["text_only"] is True
    (call,) = world.runners["haiku"].calls
    assert call["cmd"][-len(_CLAUDE_TEXT_ONLY_ARGS):] == list(_CLAUDE_TEXT_ONLY_ARGS)
    assert call["cmd"][call["cmd"].index("--tools") + 1] == ""


# --------------------------------------------------------------------------
# S4: consent before any backend
# --------------------------------------------------------------------------


def test_consent_refusal_dispatches_nothing(monkeypatch, tmp_path):
    """S4. Revert check: skip the gate (no ``consent_policy`` in ``describe``) and a model call is made."""
    w = World(monkeypatch, tmp_path, policy=policy_for(subscription=False))
    with pytest.raises(LabelRefused) as caught:
        w.labeller()(make_call())
    text = str(caught.value)
    assert "llm-kit consent set --scope graph-labels --subscription on" in text
    assert "--no-labels" in text
    assert "nothing was sent" in text
    assert w.completed() == 0


def test_a_metered_login_is_refused_with_the_metered_command(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, auth={"claude": HarnessAuth(METERED, "api key")}, policy=policy_for(subscription=True, metered=False))
    with pytest.raises(LabelRefused) as caught:
        w.labeller()(make_call())
    assert "llm-kit consent set --scope graph-labels --metered on" in str(caught.value)
    assert w.completed() == 0


@pytest.mark.parametrize("error", [consent.ConsentConfigError("consent.json: not valid JSON"), OSError("consent.json is locked")])
def test_an_unreadable_consent_file_refuses_and_says_so(monkeypatch, tmp_path, error):
    def loader(scope):
        raise error

    w = World(monkeypatch, tmp_path, policy=loader)
    with pytest.raises(LabelRefused) as caught:
        w.labeller()(make_call())
    assert str(error) in str(caught.value)
    assert "--no-labels" in str(caught.value)
    assert w.completed() == 0


def test_nothing_usable_without_a_consent_cause_still_refuses_with_the_way_out(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)

    def down(entries, **_kw):
        return {name: Reachability("unreachable", "test", "claude is not installed") for name in entries}

    monkeypatch.setattr(d, "check_many", down)
    with pytest.raises(LabelRefused) as caught:
        w.labeller()(make_call())
    assert "claude is not installed" in str(caught.value)
    assert "--no-labels" in str(caught.value)
    assert w.completed() == 0


def test_a_refused_gate_stays_refused_and_is_asked_once(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, policy=policy_for(subscription=False))
    labeller = w.labeller()
    for _ in range(2):
        with pytest.raises(LabelRefused):
            labeller(make_call())
    assert len(w.policy.loads) == 1


# --------------------------------------------------------------------------
# One gate, four threads
# --------------------------------------------------------------------------


def test_the_gate_runs_once_for_four_concurrent_calls(monkeypatch, tmp_path):
    """Four calls are inside the backend at the same moment (a barrier proves they overlap), one gate served them all."""
    barrier = threading.Barrier(4)
    w = World(monkeypatch, tmp_path, runners={"haiku": Runner(barrier=barrier)})
    labeller = w.labeller()
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda i: labeller(make_call(i)), range(4)))
    assert [r.entry for r in replies] == ["haiku"] * 4
    assert w.completed() == 4
    assert len(w.policy.loads) == 1
    # the gate probed the entry once; the four runs reused its verdict from the shared cache
    assert w.probes == [(["haiku"], str(w.root.resolve()))]


def test_a_refusal_reaches_every_concurrent_caller_after_one_consent_read(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, policy=policy_for(subscription=False))
    labeller = w.labeller()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(labeller, make_call(i)) for i in range(4)]
    for future in futures:
        assert isinstance(future.exception(), LabelRefused)
    assert len(w.policy.loads) == 1 and w.completed() == 0


# --------------------------------------------------------------------------
# Finding 7: the root, whatever the working directory
# --------------------------------------------------------------------------


def test_every_call_runs_and_probes_in_the_project_root_from_another_directory(monkeypatch, tmp_path):
    """Revert checks: drop ``cwd=root`` from the options, and the runner's cwd is the caller's directory."""
    seen_auth: list = []

    class RecordingAuth(d._LazyHarnessAuth):
        def __init__(self, cwd):
            super().__init__(cwd)
            seen_auth.append(cwd)

        def ensure(self, harness):
            self._auth.setdefault(harness, HarnessAuth(SUBSCRIPTION, "test"))

    w = World(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "_LazyHarnessAuth", RecordingAuth)
    assert Path.cwd().resolve() != w.root.resolve()
    w.labeller(harness_auth=None)(make_call())
    root = w.root.resolve()
    (call,) = w.runners["haiku"].calls
    assert Path(call["cwd"]).resolve() == root
    assert seen_auth and all(Path(cwd).resolve() == root for cwd in seen_auth)
    assert w.built and all(Path(project_root).resolve() == root for _name, project_root in w.built)
    assert w.probes and all(Path(project_root).resolve() == root for _ids, project_root in w.probes)


# --------------------------------------------------------------------------
# Finding 6: the follow-up is pinned
# --------------------------------------------------------------------------


def two_models(monkeypatch, tmp_path, **runners):
    entries = {"haiku": claude_entry("haiku", "haiku"), "sonnet": claude_entry("sonnet", "sonnet")}
    return World(monkeypatch, tmp_path, entries=entries, runners={"haiku": Runner(), "sonnet": Runner(model="claude-sonnet-5-5"), **runners})


def test_a_follow_up_is_served_only_by_the_pinned_entry(monkeypatch, tmp_path):
    w = two_models(monkeypatch, tmp_path)
    labeller = w.labeller(["haiku", "sonnet"])
    reply = labeller(make_call(followup=True, entry="sonnet"))
    assert (reply.entry, reply.model) == ("sonnet", "claude-sonnet-5-5")
    assert len(w.runners["sonnet"].calls) == 1 and w.runners["haiku"].calls == []


def test_a_pinned_entry_that_halts_does_not_fall_back_to_another(monkeypatch, tmp_path):
    w = two_models(monkeypatch, tmp_path, sonnet=Runner(quota_halt=True))
    with pytest.raises(LabelFailed):
        w.labeller(["haiku", "sonnet"])(make_call(followup=True, entry="sonnet"))
    assert w.runners["haiku"].calls == []


def test_a_primary_call_falls_back_inside_the_approved_set_and_reports_who_served(monkeypatch, tmp_path):
    w = two_models(monkeypatch, tmp_path, haiku=Runner(quota_halt=True))
    reply = w.labeller(["haiku", "sonnet"])(make_call())
    assert (reply.entry, reply.model) == ("sonnet", "claude-sonnet-5-5")
    assert len(w.runners["haiku"].calls) == 1 and len(w.runners["sonnet"].calls) == 1


@pytest.mark.parametrize("entry", [None, "opus"])
def test_a_follow_up_that_names_no_approved_entry_is_refused_unsent(monkeypatch, tmp_path, entry):
    w = two_models(monkeypatch, tmp_path)
    with pytest.raises(LabelRefused):
        w.labeller(["haiku", "sonnet"])(make_call(followup=True, entry=entry))
    assert w.completed() == 0


# --------------------------------------------------------------------------
# S10: a fallback cannot reach a refused entry
# --------------------------------------------------------------------------


def front_door_world(monkeypatch, tmp_path):
    """A front door (metered, because its group spills to a paid member) over two local members, and a paid one.

    Consent allows metered and refuses local, so the door is approved and its local members are not. The
    door's halt must end the call: the members are the entries consent refused.
    """
    entries = {
        "door": EndpointEntry(id="door", base_url="http://door.invalid/v1", model="g", frontdoor=True),
        "m1": EndpointEntry(id="m1", base_url="http://m1.invalid/v1", model="m1-model", routing=RoutingConfig(group="g", order=1),
                            billing_mode="unmetered", billing_owner="self"),
        "m2": EndpointEntry(id="m2", base_url="http://m2.invalid/v1", model="m2-model", routing=RoutingConfig(group="g", order=2),
                            billing_mode="unmetered", billing_owner="self"),
        "cloud": EndpointEntry(id="cloud", base_url="http://cloud.invalid/v1", model="cloud-model", routing=RoutingConfig(group="g", order=3)),
    }
    answer = LLMResponse(text=json.dumps(GOOD), model="served")
    runners = {
        "door": Scripted([Halted(halt.HALT_BACKPRESSURE)]),
        "m1": Scripted([answer]), "m2": Scripted([answer]), "cloud": Scripted([answer]),
    }
    return World(monkeypatch, tmp_path, entries=entries, runners=runners, policy=policy_for(local=False, metered=True, subscription=False))


def test_fallback_cannot_reach_a_refused_entry(monkeypatch, tmp_path):
    """S10. Revert check: pass the full entries map to ``run`` and the door's local members answer the call."""
    w = front_door_world(monkeypatch, tmp_path)
    with pytest.raises(LabelFailed):
        w.labeller(["door"])(make_call())
    assert w.runners["door"].calls == 1
    assert [w.runners[name].calls for name in ("m1", "m2", "cloud")] == [0, 0, 0]
    assert {name for name, _root in w.built} == {"door"}


# --------------------------------------------------------------------------
# What a bad answer does
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, why",
    [
        ("this is not json", "not JSON"),
        ("", "empty"),
        ('["nodes"]', "object"),
        ({"nodes": {}, "edges": {}, "extra": 1}, "schema"),
        ({"nodes": {}}, "schema"),
    ],
)
def test_an_unusable_answer_fails_loudly_naming_the_call(monkeypatch, tmp_path, answer, why):
    w = World(monkeypatch, tmp_path, runners={"haiku": Runner(answer=answer)})
    with pytest.raises(LabelFailed) as caught:
        w.labeller()(make_call(2, file="graphs/b.graph.yaml"))
    assert "graphs/b.graph.yaml" in str(caught.value) and "call 2" in str(caught.value)
    assert isinstance(caught.value, LabelError)


def test_a_failed_model_call_is_a_failed_label_not_a_refusal(monkeypatch, tmp_path):
    class Broken(Runner):
        def __call__(self, cmd, request, cwd, **kw):
            self.calls.append({"cmd": cmd, "request": request, "cwd": cwd})
            return "", "boom", 1

    w = World(monkeypatch, tmp_path, runners={"haiku": Broken()})
    with pytest.raises(LabelFailed) as caught:
        w.labeller()(make_call())
    assert "boom" in str(caught.value)
    assert w.completed() == 1


def test_the_options_carry_the_timeout(world):
    world.labeller(timeout_s=33.0)(make_call())
    (seen,) = world.backends["haiku"].backend.seen
    assert seen["options"].timeout_s == 33.0
    assert Path(seen["options"].cwd).resolve() == world.root.resolve()


def test_the_module_imports_dispatch_only_inside_functions():
    """The library stays usable without the dispatch layer until a labeller is asked to call a model."""
    import ast

    tree = ast.parse(Path(label_model.__file__).read_text(encoding="utf-8"))
    eager = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and "dispatch" in (node.module or ""):
            eager.append(node.module)
        if isinstance(node, ast.Import):
            eager.extend(a.name for a in node.names if "dispatch" in a.name)
    assert eager == []
