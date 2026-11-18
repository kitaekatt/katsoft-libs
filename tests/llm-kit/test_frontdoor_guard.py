"""Front-door request guard: Host validation, JSON content type, optional bearer token.

A web page can reach a loopback-bound front door by DNS rebinding and spend the
owner's provider keys. The guard answers 421 for a Host the bind does not own,
415 for a POST that is not JSON (a cross-origin ``text/plain`` POST needs no CORS
preflight), and 401 when the owner set a token and the request lacks it. No test
opens a socket: the app is driven by a starlette ``TestClient`` and upstream calls
hit a fake ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from llm_kit.registry.model_endpoints import EndpointEntry, EndpointRegistry, RoutingConfig

TOKEN = "s3cret-token-value"
CHAT = {"model": "g", "messages": []}


class Upstream:
    """Fake ``httpx.AsyncClient``: records POSTs, answers 200 or a two-chunk stream."""

    calls: list[dict[str, Any]] = []

    class Response:
        status_code = 200
        content = b'{"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}'
        headers = {"content-type": "application/json"}

    class Stream:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self) -> "Upstream.Stream":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def aiter_bytes(self):
            yield b"data: one\n\n"
            yield b"data: two\n\n"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def post(self, url: str, json: Any = None, headers: Any = None) -> "Upstream.Response":
        type(self).calls.append(json)
        return Upstream.Response()

    def stream(self, method: str, url: str, json: Any = None, headers: Any = None) -> "Upstream.Stream":
        type(self).calls.append(json)
        return Upstream.Stream()

    async def aclose(self) -> None:
        return None


def _registry() -> EndpointRegistry:
    entry = EndpointEntry(
        id="gpu", base_url="http://gpu.invalid/v1", model="gpu-model",
        routing=RoutingConfig(group="g", order=1),
    )
    return EndpointRegistry(entries={"gpu": entry})


@pytest.fixture()
def make(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from llm_kit.frontdoor import server

    Upstream.calls = []
    monkeypatch.setattr("httpx.AsyncClient", Upstream)

    def build(**options: Any) -> Any:
        app = server.create_app(_registry(), access_log=tmp_path / "access.jsonl", **options)
        return TestClient(app)

    return build


def _loopback(make: Any, **options: Any) -> Any:
    return make(bind_host="127.0.0.1", port=4000, **options)


def _error_message(response: Any) -> str:
    return response.json()["error"]["message"]


# --------------------------------------------------------------------------
# Host validation
# --------------------------------------------------------------------------

ROUTES = [("GET", "/health"), ("GET", "/v1/models"), ("GET", "/who")]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("host", ["evil.example", "evil.example:4000", "127.0.0.1.evil.example:4000", "attacker.test:80"])
def test_loopback_bind_rejects_a_rebound_host_with_421(make, method, path, host):
    client = _loopback(make)
    response = client.request(method, path, headers={"Host": host})
    assert response.status_code == 421
    assert "host" in _error_message(response).lower()
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_loopback_bind_rejects_a_rebound_host_on_completions_and_never_calls_upstream(make):
    client = _loopback(make)
    response = client.post("/v1/chat/completions", json=CHAT, headers={"Host": "evil.example:4000"})
    assert response.status_code == 421
    assert Upstream.calls == []


@pytest.mark.parametrize("host", ["127.0.0.1:4000", "localhost:4000", "LocalHost:4000", "[::1]:4000"])
def test_loopback_bind_accepts_loopback_host_variants(make, host):
    client = _loopback(make)
    assert client.get("/health", headers={"Host": host}).status_code == 200
    assert client.post("/v1/chat/completions", json=CHAT, headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost:4001", "[::1]", "127.0.0.1:80"])
def test_loopback_bind_rejects_a_loopback_name_on_another_port(make, host):
    assert _loopback(make).get("/health", headers={"Host": host}).status_code == 421


def test_loopback_bind_rejects_a_request_with_two_host_headers(make):
    client = _loopback(make)
    response = client.get("/health", headers=[("Host", "127.0.0.1:4000"), ("Host", "evil.example")])
    assert response.status_code == 421


@pytest.mark.parametrize("bind", ["localhost", "::1", "127.0.0.1"])
def test_every_loopback_spelling_of_the_bind_host_is_a_loopback_bind(make, bind):
    client = make(bind_host=bind, port=4000)
    assert client.get("/health", headers={"Host": "evil.example:4000"}).status_code == 421
    assert client.get("/health", headers={"Host": "localhost:4000"}).status_code == 200


def test_non_loopback_bind_without_an_allow_list_accepts_a_machine_name_host(make):
    client = make(bind_host="0.0.0.0", port=4000)
    for host in ("studio.lan:4000", "192.168.1.20:4000", "studio:4000", "anything.example"):
        assert client.get("/health", headers={"Host": host}).status_code == 200


def test_non_loopback_bind_with_an_allow_list_enforces_it(make):
    client = make(bind_host="0.0.0.0", port=4000, allowed_hosts=["studio.lan", "192.168.1.20:4000"])
    assert client.get("/health", headers={"Host": "studio.lan:4000"}).status_code == 200
    assert client.get("/health", headers={"Host": "STUDIO.lan:9999"}).status_code == 200  # a bare name matches any port
    assert client.get("/health", headers={"Host": "192.168.1.20:4000"}).status_code == 200
    assert client.get("/health", headers={"Host": "192.168.1.20:4001"}).status_code == 421  # a host:port entry is exact
    assert client.get("/health", headers={"Host": "evil.example:4000"}).status_code == 421
    assert client.get("/health", headers={"Host": "localhost:4000"}).status_code == 421
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 421


def test_loopback_bind_with_an_allow_list_adds_the_listed_names_to_loopback(make):
    # A reverse proxy on the same machine forwards a public name to a loopback bind.
    client = _loopback(make, allowed_hosts=["door.example"])
    assert client.get("/health", headers={"Host": "door.example"}).status_code == 200
    assert client.get("/health", headers={"Host": "localhost:4000"}).status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 421


def test_host_is_checked_before_the_token(make):
    client = _loopback(make, token=TOKEN)
    response = client.get("/health", headers={"Host": "evil.example", "Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 421


# --------------------------------------------------------------------------
# JSON content type
# --------------------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["text/plain", "text/plain;charset=UTF-8", "multipart/form-data; boundary=x",
                                           "application/x-www-form-urlencoded", "application/jsonx", "application/json2"])
def test_a_non_json_post_is_415_and_never_reaches_upstream(make, content_type):
    client = _loopback(make)
    response = client.post(
        "/v1/chat/completions", content=json.dumps(CHAT),
        headers={"Host": "127.0.0.1:4000", "Content-Type": content_type},
    )
    assert response.status_code == 415
    assert "application/json" in _error_message(response)
    assert Upstream.calls == []


def test_a_post_without_a_content_type_is_415(make):
    client = _loopback(make)
    request = client.build_request(
        "POST", "/v1/chat/completions", content=json.dumps(CHAT), headers={"Host": "127.0.0.1:4000"}
    )
    request.headers.pop("content-type", None)
    assert client.send(request).status_code == 415
    assert Upstream.calls == []


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"])
def test_a_json_post_with_parameters_or_any_case_passes(make, content_type):
    client = _loopback(make)
    response = client.post(
        "/v1/chat/completions", content=json.dumps(CHAT),
        headers={"Host": "127.0.0.1:4000", "Content-Type": content_type},
    )
    assert response.status_code == 200
    assert Upstream.calls == [{"model": "gpu-model", "messages": []}]


def test_get_routes_need_no_content_type(make):
    assert _loopback(make).get("/v1/models", headers={"Host": "127.0.0.1:4000"}).status_code == 200


def test_the_content_type_check_applies_without_host_options(make):
    # A guard-less embedding of the app (no bind_host) still rejects a non-JSON POST.
    response = make().post("/v1/chat/completions", content=json.dumps(CHAT), headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


# --------------------------------------------------------------------------
# Bearer token
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", ROUTES + [("POST", "/v1/chat/completions")])
def test_a_token_makes_a_request_without_it_401(make, method, path):
    client = _loopback(make, token=TOKEN)
    response = client.request(method, path, headers={"Host": "127.0.0.1:4000"}, json=CHAT if method == "POST" else None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text
    assert Upstream.calls == []


@pytest.mark.parametrize("authorization", [
    "Bearer wrong-token", f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", "Bearer ", "Bearer", TOKEN,
    f"Basic {TOKEN}", f"Token {TOKEN}", f"Bearer  {TOKEN}",
])
def test_a_token_makes_a_wrong_credential_401(make, authorization):
    client = _loopback(make, token=TOKEN)
    response = client.get("/health", headers={"Host": "127.0.0.1:4000", "Authorization": authorization})
    assert response.status_code == 401
    assert TOKEN not in response.text


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_the_right_token_passes_on_every_route(make, scheme):
    client = _loopback(make, token=TOKEN)
    headers = {"Host": "127.0.0.1:4000", "Authorization": f"{scheme} {TOKEN}"}
    for _, path in ROUTES:
        assert client.get(path, headers=headers).status_code == 200
    assert client.post("/v1/chat/completions", json=CHAT, headers=headers).status_code == 200


def test_the_token_is_compared_in_constant_time(make, monkeypatch):
    seen: list[tuple[bytes, bytes]] = []
    import hmac

    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr("llm_kit.frontdoor._guard.hmac.compare_digest", spy)
    client = _loopback(make, token=TOKEN)
    assert client.get("/health", headers={"Host": "127.0.0.1:4000", "Authorization": "Bearer nope"}).status_code == 401
    assert seen == [(b"nope", TOKEN.encode())]


def test_without_a_token_behaviour_is_unchanged(make):
    client = _loopback(make)
    assert client.get("/health", headers={"Host": "127.0.0.1:4000", "Authorization": "Bearer whatever"}).status_code == 200
    assert client.post("/v1/chat/completions", json=CHAT, headers={"Host": "127.0.0.1:4000"}).status_code == 200


def test_the_token_applies_on_a_non_loopback_lan_bind(make):
    client = make(bind_host="0.0.0.0", port=4000, token=TOKEN)
    assert client.get("/health", headers={"Host": "studio.lan:4000"}).status_code == 401
    assert client.get("/health", headers={"Host": "studio.lan:4000", "Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_the_token_is_checked_before_the_content_type(make):
    client = _loopback(make, token=TOKEN)
    response = client.post("/v1/chat/completions", content="x", headers={"Host": "127.0.0.1:4000", "Content-Type": "text/plain"})
    assert response.status_code == 401


def test_an_empty_token_is_refused_at_construction(make):
    for token in ("", "   "):
        with pytest.raises(ValueError, match="token"):
            make(token=token)


def test_host_options_must_come_together(make):
    with pytest.raises(ValueError, match="bind_host"):
        make(port=4000)
    with pytest.raises(ValueError, match="port"):
        make(bind_host="127.0.0.1")
    with pytest.raises(ValueError, match="bind_host"):
        make(allowed_hosts=["door.example"])


# --------------------------------------------------------------------------
# Streaming and routing are untouched
# --------------------------------------------------------------------------


def test_streaming_passes_through_the_guard_unchanged(make):
    client = _loopback(make, token=TOKEN)
    headers = {"Host": "127.0.0.1:4000", "Authorization": f"Bearer {TOKEN}"}
    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True}, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"data: one\n\ndata: two\n\n"
    assert response.headers["x-frontdoor-deployment"] == "gpu"


def test_an_unknown_route_still_answers_404_for_an_allowed_request(make):
    assert _loopback(make).get("/nope", headers={"Host": "127.0.0.1:4000"}).status_code == 404


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

REGISTRY_YAML = """
version: 1
default: gpu
models:
  gpu:
    base_url: http://gpu.invalid/v1
    model: gpu-model
    routing: {group: g, order: 1}
"""


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from llm_kit.frontdoor import server

    registry = tmp_path / "registry.yaml"
    registry.write_text(REGISTRY_YAML, encoding="utf-8")
    for name in ("FRONTDOOR_HOST", "FRONTDOOR_PORT", "FRONTDOOR_ALLOWED_HOSTS", "FRONTDOOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    captured: dict[str, Any] = {}

    def fake_create_app(reg: Any, **options: Any) -> str:
        captured["app_options"] = options
        return "app"

    def fake_run(app: Any, **options: Any) -> None:
        captured["run"] = (app, options)

    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", fake_run)
    return server, str(registry), captured


def test_main_defaults_bind_loopback_and_pass_the_bind_to_the_guard(cli):
    server, registry, captured = cli
    assert server.main(["--registry", registry]) == 0
    options = captured["app_options"]
    assert (options["bind_host"], options["port"]) == ("127.0.0.1", 4000)
    assert options["allowed_hosts"] == ()
    assert options["token"] is None
    assert captured["run"][1]["host"] == "127.0.0.1" and captured["run"][1]["port"] == 4000


def test_main_passes_the_flag_host_port_and_allow_list(cli):
    server, registry, captured = cli
    server.main(["--registry", registry, "--host", "0.0.0.0", "--port", "4100",
                 "--allowed-hosts", "studio.lan, 192.168.1.20:4100"])
    options = captured["app_options"]
    assert (options["bind_host"], options["port"]) == ("0.0.0.0", 4100)
    assert options["allowed_hosts"] == ("studio.lan", "192.168.1.20:4100")


def test_main_reads_the_allow_list_and_token_from_the_environment(cli, monkeypatch):
    server, registry, captured = cli
    monkeypatch.setenv("FRONTDOOR_ALLOWED_HOSTS", "studio.lan")
    monkeypatch.setenv("FRONTDOOR_TOKEN", TOKEN)
    server.main(["--registry", registry, "--host", "0.0.0.0"])
    options = captured["app_options"]
    assert options["allowed_hosts"] == ("studio.lan",)
    assert options["token"] == TOKEN


def test_main_refuses_a_set_but_empty_token(cli, monkeypatch, capsys):
    server, registry, captured = cli
    monkeypatch.setenv("FRONTDOOR_TOKEN", "")
    assert server.main(["--registry", registry]) == 2
    assert "FRONTDOOR_TOKEN" in capsys.readouterr().err
    assert "app_options" not in captured


def test_the_token_is_never_a_command_line_argument(cli):
    server, registry, _ = cli
    with pytest.raises(SystemExit):
        server.main(["--registry", registry, "--token", TOKEN])


def test_print_config_reports_the_allow_list_and_whether_a_token_is_set_but_never_the_token(cli, monkeypatch, capsys):
    server, registry, _ = cli
    monkeypatch.setenv("FRONTDOOR_TOKEN", TOKEN)
    assert server.main(["--registry", registry, "--print-config", "--allowed-hosts", "studio.lan"]) == 0
    out = capsys.readouterr().out
    config = json.loads(out)
    assert config["allowed_hosts"] == ["studio.lan"]
    assert config["token_set"] is True
    assert TOKEN not in out


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("LOCALHOST", True), ("::1", True), ("[::1]", True), ("127.0.0.2", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.20", False), ("studio.lan", False), ("", False),
])
def test_is_loopback_bind(host, expected):
    from llm_kit.frontdoor._guard import is_loopback_bind

    assert is_loopback_bind(host) is expected


@pytest.mark.parametrize("value,expected", [
    ("example.com", ("example.com", None)), ("Example.COM:80", ("example.com", "80")),
    ("[::1]:4000", ("::1", "4000")), ("[::1]", ("::1", None)), ("127.0.0.1:4000", ("127.0.0.1", "4000")),
])
def test_split_host_header(value, expected):
    from llm_kit.frontdoor._guard import split_host

    assert split_host(value) == expected


# --------------------------------------------------------------------------
# Startup warning for the one mode the guard cannot protect
# --------------------------------------------------------------------------


def test_main_warns_once_on_stderr_for_a_non_loopback_bind_with_no_allow_list_and_no_token(cli, capsys):
    server, registry, captured = cli
    assert server.main(["--registry", registry, "--host", "0.0.0.0"]) == 0
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) == 1
    for remedy in ("--allowed-hosts", "FRONTDOOR_ALLOWED_HOSTS", "FRONTDOOR_TOKEN"):
        assert remedy in lines[0]
    assert lines[0].startswith("warning:")
    assert "run" in captured  # the server still starts


@pytest.mark.parametrize("argv,env", [
    (["--host", "127.0.0.1"], {}),                                    # loopback bind
    (["--host", "localhost"], {}),
    (["--host", "0.0.0.0", "--allowed-hosts", "studio.lan"], {}),     # allow-list set
    (["--host", "0.0.0.0"], {"FRONTDOOR_ALLOWED_HOSTS": "studio.lan"}),
    (["--host", "0.0.0.0"], {"FRONTDOOR_TOKEN": TOKEN}),              # token set
    (["--host", "0.0.0.0", "--print-config"], {}),                    # not starting a server
    (["--host", "0.0.0.0", "--check"], {}),
])
def test_main_does_not_warn_in_any_other_mode(cli, monkeypatch, capsys, argv, env):
    server, registry, _ = cli
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert server.main(["--registry", registry, *argv]) == 0
    assert capsys.readouterr().err == ""
