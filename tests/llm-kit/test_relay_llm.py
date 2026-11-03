"""Tests for llm_kit.relay.llm (unit L4).

Every test uses tmp directories. The double-backend tests make no network call.
One test serves a fake OpenAI-compatible endpoint on a loopback port to drive
the real default backend factory; the live test runs only when the local
llama-swap server answers.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_kit.completion.types import LLMResponse
from llm_kit.registry import consent
from llm_kit.registry import model_endpoints as ME
from llm_kit.relay import config as C
from llm_kit.relay import lifecycle as L
from llm_kit.relay import llm as LL
from llm_kit.relay import mailbox as M
from llm_kit.relay import protocol as P

MACHINE = "m5pro"
ENTRY = "qwen38-m5-32k-local"
ADDRESS = f"{MACHINE}/llm/{ENTRY}"
ENV_JSON = {"machines": {"m5pro.local": {"os": "macos", "tailscale_hostname": "m5pro"}}}


# --------------------------------------------------------------------------
# fixtures and doubles
# --------------------------------------------------------------------------

class FakeBackend:
    """Records every completion; answers from a script, else with a fixed reply."""

    name = "fake"

    def __init__(self, replies=None) -> None:
        self.calls: list[dict] = []
        self._replies = list(replies or [])

    def complete(self, system, user, *, model, options=None):
        self.calls.append({"system": system, "user": user, "model": model, "options": options})
        text = self._replies.pop(0) if self._replies else "ok"
        return LLMResponse(text=text, model=model, finish_reason="stop", input_tokens=7, output_tokens=3)

    def classify_halt(self, exc):  # pragma: no cover - protocol member
        return None


class Factory:
    """An injectable backend factory that records the entries it was asked to build for."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.built: list[str] = []

    def __call__(self, entry):
        self.built.append(entry.id)
        return SimpleNamespace(backend=self.backend, model=entry.model)


@pytest.fixture
def state(tmp_path: Path):
    env_json = tmp_path / "env.json"
    env_json.write_text(json.dumps(ENV_JSON), encoding="utf-8")
    config = C.load_config(
        env_json=env_json, environ={}, hostname=lambda: "m5pro.local",
        config_path=tmp_path / "config" / "relay.yaml", state_root=tmp_path / "state",
    )
    return L.prepare_state(config)


@pytest.fixture
def box(state, tmp_path: Path) -> M.Mailbox:
    return M.Mailbox(M.mailbox_db_path(state.config.paths), machine=MACHINE)


def consent_loader(tmp_path: Path, **classes: bool):
    """A policy loader over a tmp consent file stating the global ``classes`` (none: no file)."""
    path = tmp_path / "consent.json"
    if classes:
        path.write_text(json.dumps({"schema": consent.SCHEMA, **classes}), encoding="utf-8")
    return lambda scope: consent.policy(scope, path=path)


def make_service(state, box, tmp_path, *, replies=None, registry=None, loader=None):
    factory = Factory(FakeBackend(replies))
    service = LL.LlmService(
        machine=MACHINE,
        registry=registry or C.load_loopback_registry(state.registry_file),
        mailbox=box,
        backend_factory=factory,
        policy_loader=loader or consent_loader(tmp_path),
    )
    return service, factory


def refused(service, params, code):
    with pytest.raises(P.RelayError) as caught:
        service.ask(params)
    assert caught.value.code == code, caught.value
    return caught.value


# --------------------------------------------------------------------------
# happy path and conversations
# --------------------------------------------------------------------------

def test_happy_path_returns_the_completion(state, box, tmp_path):
    service, factory = make_service(state, box, tmp_path, replies=["blue"])
    result = service.ask({"endpoint": ADDRESS, "prompt": "Name a colour.", "max_tokens": 16, "temperature": 0.5})
    assert result == {
        "endpoint": ADDRESS, "text": "blue", "finish_reason": "stop",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    assert factory.built == [ENTRY]
    call = factory.backend.calls[0]
    assert (call["system"], call["user"], call["model"]) == ("", "Name a colour.", "qwen3.8-27b-unleashed")
    assert (call["options"].max_tokens, call["options"].temperature) == (16, 0.5)


def test_explicit_messages_are_rendered_and_nothing_is_stored(state, box, tmp_path):
    service, factory = make_service(state, box, tmp_path)
    service.ask({"endpoint": ADDRESS, "messages": [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Again"},
    ]})
    call = factory.backend.calls[0]
    assert call["system"] == "Be brief."
    assert call["user"] == "User: Hi\n\nAssistant: Hello\n\nUser: Again\n\nAssistant:"
    assert box.load_conversation(ADDRESS, "anything") == []


def test_conversation_is_carried_across_two_calls(state, box, tmp_path):
    service, factory = make_service(state, box, tmp_path, replies=["Paris", "about two million"])
    first = service.ask({"endpoint": ADDRESS, "prompt": "Capital of France?", "conversation": "geo"})
    assert (first["conversation"], first["turn"]) == ("geo", 2)
    second = service.ask({"endpoint": ADDRESS, "prompt": "Population?", "conversation": "geo"})
    assert (second["text"], second["turn"]) == ("about two million", 4)
    assert factory.backend.calls[0]["user"] == "Capital of France?"
    assert factory.backend.calls[1]["user"] == (
        "User: Capital of France?\n\nAssistant: Paris\n\nUser: Population?\n\nAssistant:"
    )
    assert box.load_conversation(ADDRESS, "geo") == [
        {"role": "user", "content": "Capital of France?"}, {"role": "assistant", "content": "Paris"},
        {"role": "user", "content": "Population?"}, {"role": "assistant", "content": "about two million"},
    ]
    # another conversation name and another endpoint start empty
    assert box.load_conversation(ADDRESS, "other") == []


def test_a_failed_call_stores_nothing(state, box, tmp_path):
    service, factory = make_service(state, box, tmp_path)

    def boom(system, user, *, model, options=None):
        raise ConnectionError("connection refused")

    factory.backend.complete = boom
    error = refused(service, {"endpoint": ADDRESS, "prompt": "hi", "conversation": "c"}, "upstream_unavailable")
    assert error.retryable and "ConnectionError" in error.message
    assert box.load_conversation(ADDRESS, "c") == []


def test_history_over_the_context_budget_drops_the_oldest_turns(state, box, tmp_path, monkeypatch):
    monkeypatch.setattr(LL, "MAX_CONTEXT_CHARS", 50)
    box.append_turns(ADDRESS, "c", [("user", "a" * 30), ("assistant", "b" * 30), ("user", "c" * 10), ("assistant", "d" * 10)])
    service, factory = make_service(state, box, tmp_path)
    result = service.ask({"endpoint": ADDRESS, "prompt": "e" * 20, "conversation": "c"})
    assert result["dropped_turns"] == 2
    user = factory.backend.calls[0]["user"]
    assert "a" * 30 not in user and "b" * 30 not in user and "c" * 10 in user and "e" * 20 in user


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------

def test_consent_off_refuses_before_a_backend_is_built(state, box, tmp_path):
    loader = consent_loader(tmp_path, local=False)
    service, factory = make_service(state, box, tmp_path, loader=loader)
    error = refused(service, {"endpoint": ADDRESS, "prompt": "hi", "conversation": "c"}, "consent_refused")
    assert factory.built == [] and factory.backend.calls == []
    assert "llm-kit consent set --scope relay --local on" in error.message
    assert error.details["commands"] == ["llm-kit consent set --scope relay --local on"]
    assert error.details["cost_class"] == "local"
    assert box.load_conversation(ADDRESS, "c") == []


def test_scope_relay_on_allows_what_the_global_policy_refuses(state, box, tmp_path):
    path = tmp_path / "consent.json"
    path.write_text(json.dumps({"schema": consent.SCHEMA, "local": False, "scopes": {"relay": {"local": True}}}), encoding="utf-8")
    service, factory = make_service(state, box, tmp_path, loader=lambda scope: consent.policy(scope, path=path))
    assert service.ask({"endpoint": ADDRESS, "prompt": "hi"})["text"] == "ok"
    assert factory.built == [ENTRY]


def test_an_unverified_billing_entry_is_metered_and_refused_by_default(state, box, tmp_path):
    path = tmp_path / "plain.yaml"
    path.write_text("version: 1\nmodels:\n  plain:\n    base_url: http://127.0.0.1:9/v1\n    model: m\n", encoding="utf-8")
    registry = ME.load_endpoint_registry({ME.REGISTRY_ENV: str(path)})
    service, factory = make_service(state, box, tmp_path, registry=registry)
    error = refused(service, {"endpoint": f"{MACHINE}/llm/plain", "prompt": "hi"}, "consent_refused")
    assert "llm-kit consent set --scope relay --metered on" in error.message
    assert factory.built == []


def test_a_malformed_consent_file_refuses(state, box, tmp_path):
    (tmp_path / "consent.json").write_text("{not json", encoding="utf-8")
    service, factory = make_service(state, box, tmp_path, loader=consent_loader(tmp_path))
    error = refused(service, {"endpoint": ADDRESS, "prompt": "hi"}, "consent_refused")
    assert "could not be read" in error.message and factory.built == []


def test_a_non_loopback_registry_entry_is_refused(state, box, tmp_path):
    path = tmp_path / "remote.yaml"
    path.write_text(
        "version: 1\nmodels:\n  remote:\n    base_url: http://m5pro.localdomain:8000/v1\n    model: m\n"
        "    billing: {mode: unmetered, owner: self}\n", encoding="utf-8",
    )
    registry = ME.load_endpoint_registry({ME.REGISTRY_ENV: str(path)})  # bypasses load_loopback_registry on purpose
    service, factory = make_service(state, box, tmp_path, registry=registry)
    refused(service, {"endpoint": f"{MACHINE}/llm/remote", "prompt": "hi"}, "endpoint_refused")
    assert factory.built == [] and factory.backend.calls == []


@pytest.mark.parametrize("endpoint", [None, "", "   "])
def test_no_named_endpoint_is_refused_not_defaulted(state, box, tmp_path, endpoint):
    service, factory = make_service(state, box, tmp_path)
    params = {"prompt": "hi"} if endpoint is None else {"endpoint": endpoint, "prompt": "hi"}
    refused(service, params, "endpoint_refused")
    assert factory.built == []


@pytest.mark.parametrize("endpoint", [
    f"{MACHINE}/llm/not-in-registry",     # unknown name
    f"{MACHINE}/claude/{ENTRY}",          # not an llm address
    f"other/llm/{ENTRY}",                 # another machine
])
def test_unknown_or_foreign_endpoints_are_refused(state, box, tmp_path, endpoint):
    service, factory = make_service(state, box, tmp_path)
    refused(service, {"endpoint": endpoint, "prompt": "hi"}, "endpoint_refused")
    assert factory.built == []


def test_a_bare_entry_name_is_not_an_address(state, box, tmp_path):
    service, _ = make_service(state, box, tmp_path)
    refused(service, {"endpoint": ENTRY, "prompt": "hi"}, "target_invalid")


@pytest.mark.parametrize("params", [
    {"endpoint": ADDRESS},                                                          # neither form
    {"endpoint": ADDRESS, "prompt": "x", "messages": [{"role": "user", "content": "x"}]},
    {"endpoint": ADDRESS, "prompt": ""},
    {"endpoint": ADDRESS, "messages": [{"role": "user", "content": "x"}], "conversation": "c"},
    {"endpoint": ADDRESS, "messages": []},
    {"endpoint": ADDRESS, "messages": [{"role": "system", "content": "only system"}]},
    {"endpoint": ADDRESS, "messages": [{"role": "tool", "content": "x"}]},
    {"endpoint": ADDRESS, "messages": [{"role": "user", "content": "x"}] * 65},
    {"endpoint": ADDRESS, "prompt": "x", "conversation": "bad name!"},
    {"endpoint": ADDRESS, "prompt": "x", "max_tokens": 0},
    {"endpoint": ADDRESS, "prompt": "x", "max_tokens": 32769},
    {"endpoint": ADDRESS, "prompt": "x", "max_tokens": True},
    {"endpoint": ADDRESS, "prompt": "x", "temperature": 2.5},
    {"endpoint": ADDRESS, "prompt": "x", "stream": True},
])
def test_malformed_params_are_bad_requests(state, box, tmp_path, params):
    service, factory = make_service(state, box, tmp_path)
    refused(service, params, "bad_request")
    assert factory.built == []


def test_a_prompt_over_the_limit_is_too_large(state, box, tmp_path):
    service, factory = make_service(state, box, tmp_path)
    refused(service, {"endpoint": ADDRESS, "prompt": "x" * (LL.MAX_PROMPT_CHARS + 1)}, "too_large")
    assert factory.built == []


def test_a_completion_over_the_limit_is_too_large_and_not_stored(state, box, tmp_path):
    service, _ = make_service(state, box, tmp_path, replies=["x" * (LL.MAX_RESPONSE_CHARS + 1)])
    refused(service, {"endpoint": ADDRESS, "prompt": "hi", "conversation": "c"}, "too_large")
    assert box.load_conversation(ADDRESS, "c") == []


def test_a_service_without_a_factory_needs_the_registry_environment(state, box):
    with pytest.raises(ValueError):
        LL.LlmService(machine=MACHINE, registry=C.load_loopback_registry(state.registry_file), mailbox=box)


# --------------------------------------------------------------------------
# the default factory, against a fake loopback OpenAI-compatible endpoint
# --------------------------------------------------------------------------

class _Server:
    """A loopback HTTP server counting POSTs and answering chat completions."""

    def __init__(self, answer: str) -> None:
        self.hits: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.hits.append({"path": self.path, "body": body})
                payload = json.dumps({
                    "id": "c1", "object": "chat.completion", "created": 0, "model": body.get("model", "m"),
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": answer}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_default_factory_reaches_only_the_relay_registry_and_restores_the_environment(tmp_path, monkeypatch):
    pytest.importorskip("openai")
    env_json = tmp_path / "env.json"
    env_json.write_text(json.dumps(ENV_JSON), encoding="utf-8")
    with _Server("from-relay") as relay_server, _Server("from-default") as decoy:
        # The user's fleet registry names a DEFAULT endpoint; the relay must never touch it.
        xdg = tmp_path / "xdg"
        (xdg / "llm-kit").mkdir(parents=True)
        (xdg / "llm-kit" / "model-endpoints.yaml").write_text(
            f"version: 1\ndefault: decoy\nmodels:\n  decoy:\n    base_url: http://127.0.0.1:{decoy.port}/v1\n"
            "    model: decoy-model\n    billing: {mode: unmetered, owner: self}\n", encoding="utf-8",
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.delenv(ME.REGISTRY_ENV, raising=False)
        config = C.load_config(
            env_json=env_json, environ={}, hostname=lambda: "m5pro.local",
            config_path=tmp_path / "config" / "relay.yaml", state_root=tmp_path / "state",
            llm_endpoints={"fake": {
                "base_url": f"http://127.0.0.1:{relay_server.port}/v1", "model": "fake-model",
                "billing": {"mode": "unmetered", "owner": "self"},
            }},
        )
        prepared = L.prepare_state(config)
        box = M.Mailbox(M.mailbox_db_path(config.paths), machine=MACHINE)
        service = LL.LlmService.from_state(prepared, box, policy_loader=consent_loader(tmp_path), timeout_s=30)

        refused(service, {"prompt": "hi"}, "endpoint_refused")
        assert decoy.hits == [] and relay_server.hits == []

        result = service.ask({"endpoint": f"{MACHINE}/llm/fake", "prompt": "hello", "conversation": "c"})
        assert (result["text"], result["turn"]) == ("from-relay", 2)
        assert result["usage"] == {"input_tokens": 5, "output_tokens": 2}
        assert len(relay_server.hits) == 1 and relay_server.hits[0]["body"]["model"] == "fake-model"
        assert decoy.hits == []
        assert ME.REGISTRY_ENV not in os.environ  # the process environment is restored


def test_default_factory_refuses_a_layered_config_that_shadows_the_entry(state, monkeypatch):
    import llm_kit.dispatch.factory as F
    import llm_kit.registry.models as RM

    entry = C.load_loopback_registry(state.registry_file).entries[ENTRY]
    monkeypatch.setattr(F, "create_backend", lambda name, **kw: SimpleNamespace(kind="transport", backend=None, model="m"))
    monkeypatch.setattr(RM, "resolve_endpoint", lambda name, **kw: {"base_url": "https://openrouter.ai/api/v1"})
    with pytest.raises(P.RelayError) as caught:
        LL.default_backend_factory(entry)
    assert caught.value.code == "endpoint_refused"


def test_registry_environment_restores_a_previous_value(monkeypatch):
    monkeypatch.setenv("L4_SCOPE_TEST", "before")
    with LL.registry_environment({"L4_SCOPE_TEST": "inside", "L4_SCOPE_NEW": "x"}):
        assert os.environ["L4_SCOPE_TEST"] == "inside" and os.environ["L4_SCOPE_NEW"] == "x"
    assert os.environ["L4_SCOPE_TEST"] == "before" and "L4_SCOPE_NEW" not in os.environ


# --------------------------------------------------------------------------
# live: the local llama-swap server (skipped unless it answers within 2 s)
# --------------------------------------------------------------------------

def _llama_swap_answers() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


# Slow: the first call waits for the model to load (timeout 900 s). No `slow` marker is
# registered in this repository, so the test is named and documented rather than marked.
@pytest.mark.skipif(not _llama_swap_answers(), reason="llama-swap is not answering on 127.0.0.1:8000")
def test_live_one_word_answer_from_the_local_model(state, box, tmp_path, monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.delenv(ME.REGISTRY_ENV, raising=False)
    service = LL.LlmService.from_state(state, box, policy_loader=consent_loader(tmp_path), timeout_s=900)
    result = service.ask({
        "endpoint": ADDRESS,
        "prompt": "Reply with exactly one word: the colour of the sky on a clear day.",
        "max_tokens": 2048,
        "conversation": "live-l4",
    })
    assert result["endpoint"] == ADDRESS and result["text"].strip(), result
    assert result["turn"] == 2 and result["usage"]["output_tokens"] > 0
    assert ME.REGISTRY_ENV not in os.environ
