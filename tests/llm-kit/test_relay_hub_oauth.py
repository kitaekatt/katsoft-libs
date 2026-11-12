"""Tests for llm_kit.relay.hub_oauth: the hub's single-user OAuth 2.1 sign-in (unit hub-oauth).

Every test serves a real relay and a real hub on ephemeral 127.0.0.1 ports
(the Machine helper of test_relay_hub) with the public URL
``https://hub.example.test``; requests go to the loopback socket, as a tunnel
would deliver them. Client metadata documents come from a local HTTP server
through an injected fetcher, so nothing leaves the machine. The hub's clock is
a test double, so expiry is exercised without sleeping.
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest

from llm_kit.relay import config as C
from llm_kit.relay import hub as H
from llm_kit.relay import hub_oauth as OA
from llm_kit.relay import lifecycle as L
from llm_kit.relay import protocol as P

from test_relay_hub import A, Hub, Machine

PUBLIC = "https://hub.example.test"
RESOURCE = PUBLIC + "/mcp"
PRM_URL = PUBLIC + "/.well-known/oauth-protected-resource/mcp"
CALLBACK = OA.ALLOWED_REDIRECT_URIS[0]
CIMD_ID = "https://cimd.example.test/oauth/client.json"
ACCEPT = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, OA.s256(verifier)


def client_document(**over: Any) -> dict[str, Any]:
    document = {"client_id": CIMD_ID, "client_name": "Claude", "redirect_uris": list(OA.ALLOWED_REDIRECT_URIS),
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
    document.update(over)
    return document


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class Logs(list):
    def text(self) -> str:
        return "\n".join(self)

    def approval_code(self, label: str) -> str:
        codes = re.findall(rf"approval code for {re.escape(label)}: ([0-9A-Z]{{4}}-[0-9A-Z]{{4}})", self.text())
        assert len(codes) == 1, self.text()
        return codes[0]


class _Documents(http.server.BaseHTTPRequestHandler):
    """Serves the owning LoopbackMetadata's documents by path; 404 otherwise."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        status, body = self.server.documents.get(self.path, (404, b"{}"))  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class LoopbackMetadata:
    """A fake client-metadata host: ``https://cimd.example.test/<path>`` is served by a local HTTP server.

    It is the hub's injected fetcher: it records each URL it is asked for and
    fetches the path from the local server.
    """

    def __init__(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Documents)
        self.server.documents = {}  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.fetched: list[str] = []

    def serve(self, path: str, document: Any, status: int = 200) -> None:
        self.server.documents[path] = (status, json.dumps(document).encode("utf-8"))  # type: ignore[attr-defined]

    def __call__(self, url: str) -> dict[str, Any]:
        self.fetched.append(url)
        parts = urlsplit(url)
        assert parts.netloc == "cimd.example.test", url
        response = httpx.get(f"http://127.0.0.1:{self.server.server_port}{parts.path}", trust_env=False)
        if response.status_code != 200:
            raise OA.ClientMetadataError(f"{url} answered HTTP {response.status_code}")
        return response.json()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class OAuthHub:
    """A running hub with a public URL, plus an HTTP driver for its sign-in."""

    def __init__(self, server: H.HubServer, logs: Logs, metadata: LoopbackMetadata, clock: Clock) -> None:
        self.server = server
        self.logs = logs
        self.metadata = metadata
        self.clock = clock
        self.base = server.url[: -len(H.MCP_PATH)]
        self.http = httpx.Client(base_url=self.base, trust_env=False, follow_redirects=False, timeout=30)

    def close(self) -> None:
        self.http.close()

    def local(self, url: str) -> str:
        """A public URL's path on the loopback socket (what the tunnel does)."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == PUBLIC, url
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        response = self.http.request(method, path, **kw)
        self.http.cookies.clear()  # cookies are passed by hand: the consent cookie is Secure and __Host-
        return response

    # -- sign-in steps -----------------------------------------------------------------

    def params(self, client_id: str = CIMD_ID, challenge: Optional[str] = None, **over: Any) -> dict[str, Any]:
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
                  "code_challenge": challenge or pkce()[1], "code_challenge_method": "S256",
                  "resource": RESOURCE, "state": "state-123"}
        params.update(over)
        return {k: v for k, v in params.items() if v is not None}

    def authorize(self, params: dict[str, Any], cookie: Optional[str] = None) -> httpx.Response:
        headers = {"Cookie": f"{OA.CONSENT_COOKIE}={cookie}"} if cookie else {}
        return self.request("GET", "/authorize", params=params, headers=headers)

    @staticmethod
    def consent(page: httpx.Response) -> dict[str, str]:
        assert page.status_code == 200, page.text
        cookie = page.headers["set-cookie"]
        match = re.match(rf"{re.escape(OA.CONSENT_COOKIE)}=([^;]+);", cookie)
        assert match, cookie
        return {
            "request_id": re.search(r'name="request_id" value="([^"]+)"', page.text).group(1),
            "csrf": re.search(r'name="csrf" value="([^"]+)"', page.text).group(1),
            "cookie": match.group(1),
            "label": re.search(r"R-[0-9A-Z]{4}", page.text).group(0),
        }

    def decide(self, consent: dict[str, str], *, code: Optional[str] = None, decision: str = "approve",
               csrf: Optional[str] = None, cookie: Optional[str] = "same") -> httpx.Response:
        form = {"request_id": consent["request_id"], "csrf": consent["csrf"] if csrf is None else csrf,
                "decision": decision}
        if code is not None:
            form["approval_code"] = code
        value = consent["cookie"] if cookie == "same" else cookie
        headers = {"Cookie": f"{OA.CONSENT_COOKIE}={value}"} if value else {}
        return self.request("POST", "/authorize", data=form, headers=headers)

    def approve(self, params: dict[str, Any]) -> dict[str, list[str]]:
        """Show the consent page, type the printed code, approve; the redirect's query."""
        consent = self.consent(self.authorize(params))
        response = self.decide(consent, code=self.logs.approval_code(consent["label"]))
        return redirect_query(response)

    def sign_in(self, client_id: str = CIMD_ID) -> tuple[str, str]:
        """A fresh authorization code and its verifier."""
        verifier, challenge = pkce()
        query = self.approve(self.params(client_id, challenge))
        return query["code"][0], verifier

    def token(self, data: dict[str, Any], headers: Optional[dict[str, str]] = None) -> httpx.Response:
        return self.request("POST", "/token", data=data, headers=headers or {})

    def exchange(self, code: str, verifier: str, client_id: str = CIMD_ID, **extra: Any) -> httpx.Response:
        return self.token({"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                           "client_id": client_id, "code_verifier": verifier, **extra})

    def tokens(self, client_id: str = CIMD_ID) -> dict[str, Any]:
        code, verifier = self.sign_in(client_id)
        response = self.exchange(code, verifier, client_id, resource=RESOURCE)
        assert response.status_code == 200, response.text
        return response.json()

    def refresh(self, refresh_token: str, client_id: Optional[str] = CIMD_ID, **extra: Any) -> httpx.Response:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token, **extra}
        if client_id is not None:
            data["client_id"] = client_id
        return self.token(data)

    def register(self, metadata: Any, content_type: str = "application/json") -> httpx.Response:
        body = metadata if isinstance(metadata, (bytes, str)) else json.dumps(metadata)
        return self.request("POST", "/register", content=body, headers={"Content-Type": content_type})

    def mcp_status(self, token: str) -> int:
        response = self.request("POST", "/mcp", json=INITIALIZE,
                                headers={**ACCEPT, "Authorization": f"Bearer {token}"})
        return response.status_code

    def tool_names(self, token: str) -> list[str]:
        return [t.name for t in Hub(self.server).run(lambda c: c.list_tools(), bearer=token).tools]


def redirect_query(response: httpx.Response, redirect_uri: str = CALLBACK) -> dict[str, list[str]]:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(redirect_uri + "?"), location
    return parse_qs(urlsplit(location).query)


def start_hub(machine: Machine, metadata: LoopbackMetadata, logs: Logs, clock: Clock,
              public_url: Optional[str] = PUBLIC) -> H.HubServer:
    return H.HubServer(machine.config, peers=[machine.peer], port=0, client_timeout_s=5.0, clock=clock,
                       public_url=public_url, metadata_fetcher=metadata, oauth_log=logs.append).start()


@pytest.fixture
def machine(tmp_path: Path) -> Iterator[Machine]:
    machine = Machine(A, tmp_path)
    try:
        yield machine
    finally:
        machine.server.stop()


@pytest.fixture
def metadata() -> Iterator[LoopbackMetadata]:
    fake = LoopbackMetadata()
    fake.serve("/oauth/client.json", client_document())
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def oauth(machine: Machine, metadata: LoopbackMetadata) -> Iterator[OAuthHub]:
    logs, clock = Logs(), Clock()
    server = start_hub(machine, metadata, logs, clock)
    hub = OAuthHub(server, logs, metadata, clock)
    try:
        yield hub
    finally:
        hub.close()
        server.stop()


def restart(hub: OAuthHub, machine: Machine, public_url: str = PUBLIC) -> OAuthHub:
    """Stop the hub and start a new one on the same state directory (and clock)."""
    hub.close()
    hub.server.stop()
    server = start_hub(machine, hub.metadata, hub.logs, hub.clock, public_url=public_url)
    return OAuthHub(server, hub.logs, hub.metadata, hub.clock)


# --------------------------------------------------------------------------
# metadata documents
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"])
def test_protected_resource_metadata_names_the_public_mcp_url(oauth, path):
    response = oauth.request("GET", path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document["resource"] == RESOURCE
    assert document["authorization_servers"] == [PUBLIC]
    assert document["bearer_methods_supported"] == ["header"]


@pytest.mark.parametrize("path", ["/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"])
def test_authorization_server_metadata_advertises_what_the_connector_needs(oauth, path):
    document = oauth.request("GET", path).json()
    assert document["issuer"] == PUBLIC
    assert document["authorization_endpoint"] == PUBLIC + "/authorize"
    assert document["token_endpoint"] == PUBLIC + "/token"
    assert document["registration_endpoint"] == PUBLIC + "/register"
    assert document["response_types_supported"] == ["code"]
    assert document["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert document["token_endpoint_auth_methods_supported"] == ["none"]
    assert document["code_challenge_methods_supported"] == ["S256"]
    assert document["client_id_metadata_document_supported"] is True


def test_metadata_urls_come_from_the_public_url_not_the_host_header(oauth):
    document = oauth.request("GET", "/.well-known/oauth-authorization-server",
                             headers={"Host": "hub.example.test", "X-Forwarded-Host": "evil.example"}).json()
    assert document["issuer"] == PUBLIC


def test_the_metadata_documents_answer_only_get(oauth):
    response = oauth.request("POST", "/.well-known/oauth-authorization-server")
    assert response.status_code == 405 and response.headers["allow"] == "GET"


def test_without_a_public_url_no_oauth_endpoint_is_served(machine, metadata):
    logs, clock = Logs(), Clock()
    server = start_hub(machine, metadata, logs, clock, public_url=None)
    try:
        assert server.oauth is None and server.public_mcp_url is None
        with httpx.Client(base_url=server.url[:-4], trust_env=False) as http:
            for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"):
                response = http.get(path)
                assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
            assert http.post("/token", data={"grant_type": "refresh_token"}).status_code == 401
    finally:
        server.stop()
    assert logs == []


def test_the_public_url_argument_overrides_the_configuration(machine, metadata):
    import dataclasses

    config = dataclasses.replace(machine.config, hub_public_url="https://from-config.example.test")
    server = H.HubServer(config, peers=[machine.peer], port=0, public_url="https://Hub.Example.test/",
                         metadata_fetcher=metadata, oauth_log=Logs().append).start()
    try:
        assert server.public_url == PUBLIC and server.public_mcp_url == RESOURCE
        assert server.oauth is not None and server.oauth.resource == RESOURCE
    finally:
        server.stop()
    server = H.HubServer(config, peers=[machine.peer], port=0, oauth_log=Logs().append).start()
    try:
        assert server.public_mcp_url == "https://from-config.example.test/mcp"
    finally:
        server.stop()


# --------------------------------------------------------------------------
# the /mcp gate
# --------------------------------------------------------------------------

def test_an_unauthenticated_mcp_request_is_401_naming_the_resource_metadata(oauth):
    for method in ("POST", "GET"):
        response = oauth.request(method, "/mcp", json=INITIALIZE if method == "POST" else None, headers=ACCEPT)
        assert response.status_code == 401, method
        assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{PRM_URL}"'
        assert response.json()["error"]["code"] == "unauthenticated"
    assert "refused POST /mcp: no bearer token" in oauth.logs.text()


@pytest.mark.parametrize("header", ["Bearer not-a-token", "Bearer " + "x" * 43, "bearer wrong"])
def test_an_unknown_token_is_401_invalid_token(oauth, header):
    response = oauth.request("POST", "/mcp", json=INITIALIZE, headers={**ACCEPT, "Authorization": header})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith(f'Bearer resource_metadata="{PRM_URL}", error="invalid_token"')


def test_the_hub_bearer_still_works_with_the_sign_in_on(oauth):
    hub = Hub(oauth.server)
    assert hub.ok("list_machines")["machines"][0]["status"] == "up"
    assert len(hub.run(lambda c: c.list_tools()).tools) == len(H.SERVED_TOOLS)


def test_the_relay_token_is_not_accepted_at_the_hub(oauth, machine):
    assert oauth.mcp_status(machine.server.prepared.relay_token) == 401


def test_a_foreign_host_header_is_refused_421_and_the_public_host_is_served(oauth):
    bearer = {**ACCEPT, "Authorization": f"Bearer {oauth.server.bearer}"}
    for host in ("evil.example", "hub.example.test.evil.example", "127.0.0.2.nip.io"):
        response = oauth.request("POST", "/mcp", json=INITIALIZE, headers={**bearer, "Host": host})
        assert response.status_code == 421, host
        assert oauth.request("GET", "/.well-known/oauth-authorization-server",
                             headers={"Host": host}).status_code == 421
    for host in ("hub.example.test", "hub.example.test:443", "localhost:4711"):
        response = oauth.request("POST", "/mcp", json=INITIALIZE, headers={**bearer, "Host": host})
        assert response.status_code == 200, (host, response.text)


def test_the_connector_origin_is_accepted_on_mcp(oauth):
    headers = {**ACCEPT, "Authorization": f"Bearer {oauth.server.bearer}", "Host": "hub.example.test"}
    for origin in ("https://claude.ai", "https://claude.com", PUBLIC):
        assert oauth.request("POST", "/mcp", json=INITIALIZE, headers={**headers, "Origin": origin}).status_code == 200
    assert oauth.request("POST", "/mcp", json=INITIALIZE,
                         headers={**headers, "Origin": "https://evil.example"}).status_code == 403


def test_host_allowed_reads_loopback_and_the_public_host():
    assert H.host_allowed("127.0.0.1:4711", None) and H.host_allowed("[::1]:4711", None)
    assert H.host_allowed("localhost", None) and H.host_allowed("127.0.0.1", None)
    assert not H.host_allowed("hub.example.test", None) and not H.host_allowed(None, None)
    assert H.host_allowed("hub.example.test", "hub.example.test")
    assert not H.host_allowed("hub.example.test:8443", "hub.example.test")


# --------------------------------------------------------------------------
# dynamic client registration
# --------------------------------------------------------------------------

def test_registration_makes_a_public_client_for_the_connector_callbacks(oauth):
    response = oauth.register({"client_name": "claudeai", "redirect_uris": [CALLBACK],
                               "grant_types": ["authorization_code", "refresh_token"],
                               "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert response.status_code == 201, response.text
    client = response.json()
    assert client["client_id"].startswith("llm-kit-") and client["redirect_uris"] == [CALLBACK]
    assert client["token_endpoint_auth_method"] == "none" and "client_secret" not in client
    assert client["client_name"] == "claudeai"
    stored = json.loads(oauth.server.config.paths.hub_oauth.read_text())
    assert stored["clients"][client["client_id"]]["redirect_uris"] == [CALLBACK]


@pytest.mark.parametrize("redirect_uris", [
    ["https://evil.example/callback"], [CALLBACK, "https://evil.example/callback"], [],
    "https://claude.ai/api/mcp/auth_callback", None, ["https://claude.ai/api/mcp/auth_callback/"],
    ["http://claude.ai/api/mcp/auth_callback"],
])
def test_registration_refuses_any_redirect_uri_but_the_callbacks(oauth, redirect_uris):
    body = {"client_name": "x"} if redirect_uris is None else {"redirect_uris": redirect_uris}
    response = oauth.register(body)
    assert response.status_code == 400 and response.json()["error"] == "invalid_redirect_uri"
    assert json.loads(oauth.server.config.paths.hub_oauth.read_text())["clients"] == {} if (
        oauth.server.config.paths.hub_oauth.exists()) else True


@pytest.mark.parametrize("body,content_type", [
    ("not json", "application/json"),
    ("[1, 2]", "application/json"),
    (json.dumps({"redirect_uris": [CALLBACK]}), "application/x-www-form-urlencoded"),
    (json.dumps({"redirect_uris": [CALLBACK], "grant_types": ["client_credentials"]}), "application/json"),
    (json.dumps({"redirect_uris": [CALLBACK], "grant_types": []}), "application/json"),
    (json.dumps({"redirect_uris": [CALLBACK], "response_types": ["token"]}), "application/json"),
    (json.dumps({"redirect_uris": [CALLBACK], "client_name": "x" * 201}), "application/json"),
    (json.dumps({"redirect_uris": [CALLBACK], "client_name": 7}), "application/json"),
])
def test_registration_refuses_bad_client_metadata(oauth, body, content_type):
    response = oauth.register(body, content_type)
    assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"


def test_a_client_asking_for_a_secret_is_registered_public(oauth):
    response = oauth.register({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_post"})
    assert response.status_code == 201
    assert response.json()["token_endpoint_auth_method"] == "none" and "client_secret" not in response.json()
    assert "asked for 'client_secret_post'" in oauth.logs.text()


def test_registrations_are_capped(oauth):
    ids = [oauth.register({"redirect_uris": [CALLBACK]}).json()["client_id"] for _ in range(OA.MAX_CLIENTS + 3)]
    clients = json.loads(oauth.server.config.paths.hub_oauth.read_text())["clients"]
    assert len(clients) == OA.MAX_CLIENTS and ids[-1] in clients


def test_an_oversized_body_is_refused_413(oauth):
    response = oauth.register("x" * (OA.MAX_BODY_BYTES + 1))
    assert response.status_code == 413


# --------------------------------------------------------------------------
# /authorize: the client and its redirect URI (refused with a page, never a redirect)
# --------------------------------------------------------------------------

def assert_page_refusal(response: httpx.Response, status: int = 400) -> None:
    assert response.status_code == status, response.text
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")


def test_authorize_refuses_an_unknown_client_with_a_page(oauth):
    assert_page_refusal(oauth.authorize(oauth.params("llm-kit-nobody")))


@pytest.mark.parametrize("redirect_uri", ["https://evil.example/callback", "https://claude.ai/api/mcp/auth_callback/x",
                                          None])
def test_authorize_refuses_a_redirect_uri_that_is_not_a_callback(oauth, redirect_uri):
    client_id = oauth.register({"redirect_uris": [CALLBACK]}).json()["client_id"]
    for cid in (client_id, CIMD_ID):
        assert_page_refusal(oauth.authorize(oauth.params(cid, redirect_uri=redirect_uri)))
    assert oauth.metadata.fetched == []  # refused before any fetch


def test_authorize_refuses_a_registered_client_using_a_callback_it_did_not_register(oauth):
    client_id = oauth.register({"redirect_uris": [CALLBACK]}).json()["client_id"]
    assert_page_refusal(oauth.authorize(oauth.params(client_id, redirect_uri=OA.ALLOWED_REDIRECT_URIS[1])))


def test_authorize_fetches_the_client_metadata_document_and_logs_its_url(oauth):
    page = oauth.authorize(oauth.params(CIMD_ID))
    assert page.status_code == 200
    assert oauth.metadata.fetched == [CIMD_ID]
    assert f"client_id='{CIMD_ID}'" in oauth.logs.text()
    assert "<b>Claude</b>" in page.text and "claude.ai" in page.text


@pytest.mark.parametrize("document", [
    client_document(client_id="https://cimd.example.test/other.json"),
    client_document(redirect_uris=["https://evil.example/callback"]),
    client_document(redirect_uris=None),
    {"client_name": "no id"},
])
def test_authorize_refuses_a_metadata_document_that_does_not_fit(oauth, document):
    oauth.metadata.serve("/oauth/client.json", document)
    assert_page_refusal(oauth.authorize(oauth.params(CIMD_ID)))


def test_authorize_refuses_a_metadata_document_that_cannot_be_fetched(oauth):
    assert_page_refusal(oauth.authorize(oauth.params("https://cimd.example.test/missing.json")))
    assert oauth.metadata.fetched == ["https://cimd.example.test/missing.json"]


@pytest.mark.parametrize("client_id", ["https://cimd.example.test", "https://cimd.example.test/",
                                       "https://cimd.example.test/a/../client.json", "https://u@cimd.example.test/c"])
def test_authorize_refuses_a_client_id_url_that_cannot_be_a_metadata_document(oauth, client_id):
    assert_page_refusal(oauth.authorize(oauth.params(client_id)))
    assert oauth.metadata.fetched == []


def test_the_consent_page_escapes_what_the_client_says(oauth):
    oauth.metadata.serve("/oauth/client.json", client_document(client_name="<script>alert(1)</script>"))
    page = oauth.authorize(oauth.params(CIMD_ID))
    assert page.status_code == 200
    assert "<script>" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_the_consent_page_sets_a_secure_cookie_and_refuses_framing(oauth):
    page = oauth.authorize(oauth.params(CIMD_ID))
    cookie = page.headers["set-cookie"]
    for part in ("Path=/", "Secure", "HttpOnly", "SameSite=Lax"):
        assert part in cookie
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert OA.ALLOWED_REDIRECT_URIS  # the callback origins may receive the form's redirect
    for origin in OA.CLIENT_ORIGINS:
        assert origin in page.headers["content-security-policy"]


def test_the_consent_page_does_not_show_the_approval_code(oauth):
    page = oauth.authorize(oauth.params(CIMD_ID))
    consent = oauth.consent(page)
    code = oauth.logs.approval_code(consent["label"])
    assert code not in page.text and code.replace("-", "") not in page.text


def test_authorize_reuses_the_browsers_binding_cookie(oauth):
    first = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    second = oauth.consent(oauth.authorize(oauth.params(CIMD_ID), cookie=first["cookie"]))
    assert second["cookie"] == first["cookie"]
    # both tabs of one browser can still be decided
    assert oauth.decide(first, code=oauth.logs.approval_code(first["label"])).status_code == 302
    assert oauth.decide(second, code=oauth.logs.approval_code(second["label"])).status_code == 302


# --------------------------------------------------------------------------
# /authorize: the request's other parameters (refused back to the trusted callback)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("over,error", [
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"response_type": None}, "unsupported_response_type"),
    ({"code_challenge": None}, "invalid_request"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge_method": None}, "invalid_request"),
    ({"code_challenge": "short"}, "invalid_request"),
    ({"resource": None}, "invalid_target"),
    ({"resource": "https://hub.example.test/other"}, "invalid_target"),
    ({"resource": "https://evil.example/mcp"}, "invalid_target"),
])
def test_authorize_refusals_after_the_callback_is_trusted_go_back_to_it(oauth, over, error):
    query = redirect_query(oauth.authorize(oauth.params(CIMD_ID, **over)))
    assert query["error"] == [error] and query["state"] == ["state-123"] and query["iss"] == [PUBLIC]
    assert "code" not in query


def test_a_repeated_parameter_is_refused(oauth):
    response = oauth.request("GET", f"/authorize?client_id={CIMD_ID}&client_id=other&redirect_uri={CALLBACK}")
    assert_page_refusal(response)


@pytest.mark.parametrize("resource", ["https://HUB.example.test/mcp", "https://hub.example.test:443/mcp",
                                      "https://hub.example.test/mcp/"])
def test_the_resource_is_compared_canonically(oauth, resource):
    assert oauth.authorize(oauth.params(CIMD_ID, resource=resource)).status_code == 200


def test_authorize_refuses_while_the_hub_is_paused(oauth, machine):
    store = L.PauseStore(machine.config.paths)
    store.pause("hub", "testing")
    try:
        assert_page_refusal(oauth.authorize(oauth.params(CIMD_ID)), 503)
        assert oauth.metadata.fetched == []
    finally:
        store.resume("hub")
    assert oauth.authorize(oauth.params(CIMD_ID)).status_code == 200


def test_pending_requests_are_capped(oauth):
    first = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    for _ in range(OA.MAX_PENDING):
        oauth.authorize(oauth.params(CIMD_ID))
    response = oauth.decide(first, code=oauth.logs.approval_code(first["label"]))
    assert_page_refusal(response)
    assert "expired" in response.text


# --------------------------------------------------------------------------
# /authorize: the owner's decision
# --------------------------------------------------------------------------

def test_approve_redirects_with_a_code_the_state_and_the_issuer(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    typed = oauth.logs.approval_code(consent["label"]).lower().replace("-", " ")
    query = redirect_query(oauth.decide(consent, code=typed))
    assert set(query) == {"code", "state", "iss"}
    assert query["state"] == ["state-123"] and query["iss"] == [PUBLIC] and len(query["code"][0]) >= 40
    assert f"sign-in request {consent['label']}: approved" in oauth.logs.text()


def test_without_a_state_none_is_returned(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID, state=None)))
    query = redirect_query(oauth.decide(consent, code=oauth.logs.approval_code(consent["label"])))
    assert set(query) == {"code", "iss"}


@pytest.mark.parametrize("case", ["wrong csrf", "no csrf", "no cookie", "wrong cookie"])
def test_a_decision_without_its_csrf_token_and_cookie_is_refused(oauth, case):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    code = oauth.logs.approval_code(consent["label"])
    kw = {"wrong csrf": {"csrf": "x" * 32}, "no csrf": {"csrf": ""}, "no cookie": {"cookie": None},
          "wrong cookie": {"cookie": "y" * 32}}[case]
    assert_page_refusal(oauth.decide(consent, code=code, **kw), 403)
    # the request is still pending for its own page
    assert redirect_query(oauth.decide(consent, code=code))["code"]


def test_wrong_approval_codes_re_show_the_page_and_the_fifth_ends_the_request(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    for attempt in range(1, OA.MAX_APPROVAL_ATTEMPTS):
        response = oauth.decide(consent, code="AAAA-AAAA")
        assert response.status_code == 400 and "not the approval code" in response.text, attempt
        assert f"{OA.MAX_APPROVAL_ATTEMPTS - attempt} tries left" in response.text
    query = redirect_query(oauth.decide(consent, code="AAAA-AAAA"))
    assert query["error"] == ["access_denied"]
    response = oauth.decide(consent, code=oauth.logs.approval_code(consent["label"]))
    assert_page_refusal(response)


def test_a_decision_without_a_code_is_a_wrong_code(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    assert oauth.decide(consent, code=None).status_code == 400


def test_an_approval_code_is_bound_to_its_own_request(oauth):
    first = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    second = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    assert oauth.decide(second, code=oauth.logs.approval_code(first["label"])).status_code == 400
    assert redirect_query(oauth.decide(second, code=oauth.logs.approval_code(second["label"])))["code"]


def test_deny_redirects_access_denied_and_ends_the_request(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    query = redirect_query(oauth.decide(consent, decision="deny"))
    assert query["error"] == ["access_denied"] and query["state"] == ["state-123"]
    assert_page_refusal(oauth.decide(consent, code=oauth.logs.approval_code(consent["label"])))


def test_an_unknown_decision_is_refused(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    assert_page_refusal(oauth.decide(consent, decision="maybe", code="x"))


def test_an_approval_code_expires(oauth):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    oauth.clock.now += OA.APPROVAL_TTL_S + 1
    assert_page_refusal(oauth.decide(consent, code=oauth.logs.approval_code(consent["label"])))


def test_an_approval_is_refused_while_the_hub_is_paused(oauth, machine):
    consent = oauth.consent(oauth.authorize(oauth.params(CIMD_ID)))
    store = L.PauseStore(machine.config.paths)
    store.pause("hub")
    try:
        assert_page_refusal(oauth.decide(consent, code=oauth.logs.approval_code(consent["label"])), 503)
    finally:
        store.resume("hub")


# --------------------------------------------------------------------------
# /token
# --------------------------------------------------------------------------

def oauth_error(response: httpx.Response, error: str, status: int = 400) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"] == error, response.text
    assert response.headers["cache-control"] == "no-store"


def test_a_code_exchange_returns_bearer_tokens_quickly(oauth):
    code, verifier = oauth.sign_in()
    started = time.monotonic()
    response = oauth.exchange(code, verifier, resource=RESOURCE)
    assert time.monotonic() - started < 2.0
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == OA.ACCESS_TOKEN_TTL_S
    assert tokens["access_token"] != tokens["refresh_token"]
    assert oauth.mcp_status(tokens["access_token"]) == 200


def test_the_resource_may_be_left_out_of_the_code_exchange(oauth):
    code, verifier = oauth.sign_in()
    assert oauth.exchange(code, verifier).status_code == 200


@pytest.mark.parametrize("data,error", [
    ({}, "invalid_request"),
    ({"grant_type": "client_credentials"}, "unsupported_grant_type"),
    ({"grant_type": "password", "username": "u", "password": "p"}, "unsupported_grant_type"),
    ({"grant_type": "authorization_code"}, "invalid_request"),
    ({"grant_type": "refresh_token"}, "invalid_request"),
])
def test_token_request_shape_errors(oauth, data, error):
    oauth_error(oauth.token(data), error)


def test_the_token_endpoint_needs_a_form_body(oauth):
    response = oauth.request("POST", "/token", json={"grant_type": "refresh_token", "refresh_token": "x"})
    oauth_error(response, "invalid_request")


def test_a_repeated_token_parameter_is_refused(oauth):
    response = oauth.request("POST", "/token", content="grant_type=refresh_token&grant_type=refresh_token",
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    oauth_error(response, "invalid_request")


def test_a_wrong_verifier_is_invalid_grant_and_spends_the_code(oauth):
    code, verifier = oauth.sign_in()
    oauth_error(oauth.exchange(code, pkce()[0]), "invalid_grant")
    oauth_error(oauth.exchange(code, verifier), "invalid_grant")


def test_a_malformed_verifier_is_invalid_grant(oauth):
    code, verifier = oauth.sign_in()
    oauth_error(oauth.exchange(code, "short"), "invalid_grant")


@pytest.mark.parametrize("change,error", [
    ({"redirect_uri": OA.ALLOWED_REDIRECT_URIS[1]}, "invalid_grant"),
    ({"client_id": "https://cimd.example.test/other.json"}, "invalid_grant"),
    ({"resource": "https://hub.example.test/other"}, "invalid_target"),
])
def test_a_code_exchange_must_match_the_authorization_request(oauth, change, error):
    code, verifier = oauth.sign_in()
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": CIMD_ID,
            "code_verifier": verifier, **change}
    oauth_error(oauth.token(data), error)


def test_a_code_exchange_needs_a_client_id(oauth):
    code, verifier = oauth.sign_in()
    oauth_error(oauth.token({"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                             "code_verifier": verifier}), "invalid_request")


def test_an_unknown_code_is_invalid_grant(oauth):
    oauth_error(oauth.exchange("not-a-code", pkce()[0]), "invalid_grant")


def test_an_expired_code_is_invalid_grant(oauth):
    code, verifier = oauth.sign_in()
    oauth.clock.now += OA.AUTH_CODE_TTL_S + 1
    oauth_error(oauth.exchange(code, verifier), "invalid_grant")


def test_a_reused_code_revokes_the_tokens_it_bought(oauth):
    code, verifier = oauth.sign_in()
    tokens = oauth.exchange(code, verifier).json()
    assert oauth.mcp_status(tokens["access_token"]) == 200
    oauth_error(oauth.exchange(code, verifier), "invalid_grant")
    assert oauth.mcp_status(tokens["access_token"]) == 401
    oauth_error(oauth.refresh(tokens["refresh_token"]), "invalid_grant")


def test_a_basic_header_supplies_the_client_id_and_its_secret_is_ignored(oauth):
    code, verifier = oauth.sign_in()
    basic = base64.b64encode(f"{quote(CIMD_ID, safe='')}:ignored".encode()).decode()  # RFC 6749 2.3.1 encoding
    response = oauth.token({"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                            "code_verifier": verifier}, headers={"Authorization": f"Basic {basic}"})
    assert response.status_code == 200, response.text
    other = base64.b64encode(b"someone-else:x").decode()
    oauth_error(oauth.token({"grant_type": "refresh_token", "refresh_token": response.json()["refresh_token"],
                             "client_id": CIMD_ID}, headers={"Authorization": f"Basic {other}"}),
                "invalid_client", 401)


def test_refresh_rotates_the_refresh_token(oauth):
    tokens = oauth.tokens()
    response = oauth.refresh(tokens["refresh_token"], resource=RESOURCE)
    assert response.status_code == 200, response.text
    fresh = response.json()
    assert fresh["refresh_token"] != tokens["refresh_token"] and fresh["access_token"] != tokens["access_token"]
    assert oauth.mcp_status(fresh["access_token"]) == 200
    assert oauth.refresh(fresh["refresh_token"], client_id=None).status_code == 200


def test_a_rotated_refresh_token_presented_again_revokes_the_sign_in(oauth):
    tokens = oauth.tokens()
    fresh = oauth.refresh(tokens["refresh_token"]).json()
    oauth_error(oauth.refresh(tokens["refresh_token"]), "invalid_grant")
    assert oauth.mcp_status(fresh["access_token"]) == 401
    oauth_error(oauth.refresh(fresh["refresh_token"]), "invalid_grant")


def test_another_sign_in_survives_a_revocation(oauth):
    one, two = oauth.tokens(), oauth.tokens()
    oauth.refresh(one["refresh_token"])
    oauth.refresh(one["refresh_token"])  # reuse: revokes sign-in one only
    assert oauth.mcp_status(two["access_token"]) == 200


@pytest.mark.parametrize("change,error", [
    ({"client_id": "https://cimd.example.test/other.json"}, "invalid_grant"),
    ({"resource": "https://hub.example.test/other"}, "invalid_target"),
])
def test_a_refused_refresh_does_not_spend_the_refresh_token(oauth, change, error):
    tokens = oauth.tokens()
    oauth_error(oauth.refresh(tokens["refresh_token"], **change), error)
    assert oauth.refresh(tokens["refresh_token"]).status_code == 200


def test_an_unknown_refresh_token_is_invalid_grant(oauth):
    oauth_error(oauth.refresh("not-a-refresh-token"), "invalid_grant")


def test_an_access_token_expires(oauth):
    tokens = oauth.tokens()
    oauth.clock.now += OA.ACCESS_TOKEN_TTL_S + 1
    assert oauth.mcp_status(tokens["access_token"]) == 401
    assert oauth.refresh(tokens["refresh_token"]).status_code == 200


def test_a_refresh_token_expires(oauth):
    tokens = oauth.tokens()
    oauth.clock.now += OA.REFRESH_TOKEN_TTL_S + 1
    oauth_error(oauth.refresh(tokens["refresh_token"]), "invalid_grant")


def test_the_token_endpoint_refuses_while_the_hub_is_paused(oauth, machine):
    code, verifier = oauth.sign_in()
    store = L.PauseStore(machine.config.paths)
    store.pause("hub")
    try:
        oauth_error(oauth.exchange(code, verifier), "temporarily_unavailable", 503)
        oauth_error(oauth.register({"redirect_uris": [CALLBACK]}), "temporarily_unavailable", 503)
    finally:
        store.resume("hub")
    assert oauth.exchange(code, verifier).status_code == 200


def test_tokens_for_another_public_url_are_refused(oauth, machine):
    tokens = oauth.tokens()
    moved = restart(oauth, machine, public_url="https://moved.example.test")
    try:
        assert moved.mcp_status(tokens["access_token"]) == 401
        oauth_error(moved.refresh(tokens["refresh_token"]), "invalid_grant")
    finally:
        moved.close()
        moved.server.stop()


# --------------------------------------------------------------------------
# what is stored and logged
# --------------------------------------------------------------------------

def test_tokens_and_clients_survive_a_hub_restart(oauth, machine):
    client_id = oauth.register({"redirect_uris": [CALLBACK]}).json()["client_id"]
    tokens = oauth.tokens()
    again = restart(oauth, machine)
    try:
        assert again.mcp_status(tokens["access_token"]) == 200
        assert again.refresh(tokens["refresh_token"]).status_code == 200
        assert again.authorize(again.params(client_id)).status_code == 200
    finally:
        again.close()
        again.server.stop()


def test_the_store_is_private_and_holds_no_presentable_secret(oauth):
    tokens = oauth.tokens()
    code, verifier = oauth.sign_in()
    path = oauth.server.config.paths.hub_oauth
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    for secret in (tokens["access_token"], tokens["refresh_token"], code, verifier):
        assert secret not in text


def test_no_token_code_or_verifier_is_logged(oauth):
    code, verifier = oauth.sign_in()
    tokens = oauth.exchange(code, verifier).json()
    fresh = oauth.refresh(tokens["refresh_token"]).json()
    oauth.mcp_status("Bearer-wrong")
    text = oauth.logs.text()
    for secret in (code, verifier, tokens["access_token"], tokens["refresh_token"], fresh["access_token"],
                   fresh["refresh_token"], oauth.server.bearer):
        assert secret not in text


def test_a_loose_store_is_refused_and_the_lock_released(oauth, machine):
    oauth.tokens()
    oauth.close()
    oauth.server.stop()
    path = machine.config.paths.hub_oauth
    os.chmod(path, 0o644)
    with pytest.raises(C.RelayConfigError, match="0644"):
        start_hub(machine, oauth.metadata, oauth.logs, oauth.clock)
    os.chmod(path, 0o600)
    start_hub(machine, oauth.metadata, oauth.logs, oauth.clock).stop()


@pytest.mark.parametrize("text", ["not json", "[]", json.dumps({"version": 2, "clients": {}, "access_tokens": {},
                                                                 "refresh_tokens": {}})])
def test_a_corrupt_store_is_refused(machine, metadata, text):
    L.ensure_state_dir(machine.config.paths)
    L.write_private_file(machine.config.paths.hub_oauth, text)
    with pytest.raises(C.RelayConfigError, match="OAuth store"):
        start_hub(machine, metadata, Logs(), Clock())


# --------------------------------------------------------------------------
# the client metadata fetcher
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://cimd.example.test/c.json", "https://cimd.example.test",
                                 "https://cimd.example.test/", "https://cimd.example.test/c.json#x",
                                 "https://cimd.example.test/c.json#", "https://u:p@cimd.example.test/c.json",
                                 "https://cimd.example.test/./c.json", "https://cimd.example.test/a/../c.json",
                                 "https:///c.json", "https://cimd.example.test/" + "x" * 2100, "", None])
def test_a_client_id_url_must_be_https_with_a_path(url):
    with pytest.raises(OA.ClientMetadataError):
        OA.check_client_id_url(url)


def test_the_fetcher_checks_the_client_id_url_itself():
    looked_up: list[str] = []

    def resolve(host: str, port: int, type: int = 0) -> list[Any]:
        looked_up.append(host)
        return _resolver("93.184.215.14")(host, port, type)

    for url in ("http://cimd.example.test/c.json", "https://cimd.example.test/", "https://cimd.example.test/c#x"):
        with pytest.raises(OA.ClientMetadataError, match="client_id"):
            OA.MetadataFetcher(resolver=resolve)(url)
    assert looked_up == []


def test_a_good_client_id_url_passes():
    assert OA.check_client_id_url(CIMD_ID) == CIMD_ID
    assert OA.check_client_id_url("https://claude.ai/oauth/mcp-oauth-client-metadata?v=1") is not None


@pytest.mark.parametrize("address,public", [
    ("93.184.215.14", True), ("2606:4700::1111", True), ("127.0.0.1", False), ("10.1.2.3", False),
    ("192.168.1.5", False), ("172.16.0.1", False), ("169.254.169.254", False), ("100.64.1.1", False),
    ("::1", False), ("fd00::1", False), ("fe80::1", False), ("::ffff:127.0.0.1", False), ("0.0.0.0", False),
    ("224.0.0.1", False), ("::ffff:93.184.215.14", True),
])
def test_only_public_addresses_are_fetched_from(address, public):
    assert OA.is_public_address(address) is public


def _resolver(*addresses: str):
    def resolve(host: str, port: int, type: int = 0) -> list[Any]:
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in addresses]
    return resolve


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("10.0.0.7",), ("93.184.215.14", "192.168.1.5"), ("::1",)])
def test_the_fetcher_refuses_a_host_with_any_non_public_address(addresses):
    fetcher = OA.MetadataFetcher(resolver=_resolver(*addresses))
    with pytest.raises(OA.ClientMetadataError, match="non-public"):
        fetcher(CIMD_ID)


def test_the_fetcher_refuses_an_unresolvable_host():
    def fail(*args: Any, **kw: Any) -> Any:
        raise socket.gaierror("no such host")

    with pytest.raises(OA.ClientMetadataError, match="cannot resolve"):
        OA.MetadataFetcher(resolver=fail)(CIMD_ID)


class _Behaviours(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        kind = self.path.strip("/").split(".")[0]
        if kind == "redirect":
            self.send_response(302)
            self.send_header("Location", "/target.json")
            self.end_headers()
            return
        if kind == "declared":
            self.send_response(200)
            self.send_header("Content-Length", str(10 * OA.CIMD_MAX_BYTES))
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        if kind == "big":
            self.wfile.write(b"[" + b"1," * OA.CIMD_MAX_BYTES + b"1]")
        elif kind == "slow":
            for _ in range(30):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.1)
        elif kind == "text":
            self.wfile.write(b"<html>not json</html>")
        elif kind == "array":
            self.wfile.write(b"[1, 2]")
        else:
            self.wfile.write(json.dumps(client_document()).encode())

    def log_message(self, *args: Any) -> None:
        pass


class _LocalFetcher(OA.MetadataFetcher):
    """The real fetcher, with its connection pointed at a local plain-HTTP server (TLS itself is stdlib's)."""

    def __init__(self, port: int, **kw: Any) -> None:
        super().__init__(resolver=_resolver("93.184.215.14"), **kw)
        self._local_port = port

    def _connection(self, host: str, address: str, port: int) -> http.client.HTTPConnection:
        assert (host, address, port) == ("cimd.example.test", "93.184.215.14", 443)
        return http.client.HTTPConnection("127.0.0.1", self._local_port, timeout=self._timeout_s)


@pytest.fixture
def behaviours() -> Iterator[http.server.ThreadingHTTPServer]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Behaviours)
    server.hits = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_the_fetcher_reads_a_json_document(behaviours):
    document = _LocalFetcher(behaviours.server_port)("https://cimd.example.test/ok.json")
    assert document == client_document()


@pytest.mark.parametrize("path,match", [
    ("/redirect.json", "HTTP 302"), ("/declared.json", "larger than"), ("/big.json", "larger than"),
    ("/text.json", "not a JSON document"), ("/array.json", "not a JSON object"),
])
def test_the_fetcher_refuses_what_is_not_a_small_json_object(behaviours, path, match):
    with pytest.raises(OA.ClientMetadataError, match=match):
        _LocalFetcher(behaviours.server_port)("https://cimd.example.test" + path)
    assert behaviours.hits == [path]  # a redirect is never followed


def test_the_fetcher_gives_up_after_its_time_cap(behaviours):
    started = time.monotonic()
    with pytest.raises(OA.ClientMetadataError):
        _LocalFetcher(behaviours.server_port, timeout_s=0.5)("https://cimd.example.test/slow.json")
    assert time.monotonic() - started < 2.5


def test_the_fetchers_connection_goes_to_the_checked_address_not_a_new_lookup():
    """The pinned connection dials the address it was given; TLS then fails against this plain socket."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    seen: list[Any] = []

    def accept() -> None:
        conn, peer = listener.accept()
        seen.append(peer)
        conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    fetcher = OA.MetadataFetcher()
    connection = fetcher._connection("cimd.does-not-resolve.invalid", "127.0.0.1", listener.getsockname()[1])
    with pytest.raises(OSError):
        connection.connect()
    thread.join(timeout=5)
    listener.close()
    assert seen and seen[0][0] == "127.0.0.1"


@pytest.mark.parametrize("document,match", [
    (client_document(client_id="https://x.example/c.json"), "client_id"),
    (client_document(redirect_uris="https://claude.ai/api/mcp/auth_callback"), "redirect_uris"),
    (client_document(redirect_uris=[OA.ALLOWED_REDIRECT_URIS[1]]), "does not list"),
])
def test_check_client_document_refusals(document, match):
    with pytest.raises(OA.ClientMetadataError, match=match):
        OA.check_client_document(document, CIMD_ID, CALLBACK)


def test_a_document_without_a_name_is_shown_by_its_host():
    assert OA.check_client_document(client_document(client_name=None), CIMD_ID, CALLBACK) == "cimd.example.test"
    assert OA.check_client_document(client_document(client_name="  A  "), CIMD_ID, CALLBACK) == "A"


def test_s256_is_base64url_sha256_without_padding():
    # expected value computed outside Python:
    # printf %s <verifier> | openssl dgst -sha256 -binary | base64 | tr '+/' '-_' | tr -d '='
    assert OA.s256("dBjftJeZ4CVP-mJ92K9qzvpvIrAN2Cy0afvjpMBbrFQ") == "ij-W2gKM2MjB1jTD0QYGynL-E5eGBJhynPrtKgWcYJ4"


# --------------------------------------------------------------------------
# end to end: discovery, sign-in, tokens, an authenticated tools/list
# --------------------------------------------------------------------------

def _discover(oauth: OAuthHub) -> dict[str, Any]:
    """What a connector does first: 401 -> protected resource metadata -> authorization server metadata."""
    unauthenticated = oauth.request("POST", "/mcp", json=INITIALIZE, headers=ACCEPT)
    assert unauthenticated.status_code == 401
    prm_url = re.fullmatch(r'Bearer resource_metadata="([^"]+)"', unauthenticated.headers["www-authenticate"]).group(1)
    prm = oauth.request("GET", oauth.local(prm_url)).json()
    assert prm["resource"] == RESOURCE
    issuer = prm["authorization_servers"][0]
    metadata = oauth.request("GET", oauth.local(issuer + "/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == issuer
    return metadata


def _sign_in_through(oauth: OAuthHub, metadata: dict[str, Any], client_id: str) -> dict[str, Any]:
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(8)
    authorize = oauth.local(metadata["authorization_endpoint"])
    page = oauth.request("GET", authorize, params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": RESOURCE, "state": state, "scope": "relay"})
    consent = oauth.consent(page)
    query = redirect_query(oauth.decide(consent, code=oauth.logs.approval_code(consent["label"])))
    assert query["state"] == [state] and query["iss"] == [metadata["issuer"]]
    token = oauth.request("POST", oauth.local(metadata["token_endpoint"]), data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": verifier, "resource": RESOURCE})
    assert token.status_code == 200, token.text
    tokens = token.json()
    assert tokens["scope"] == "relay"
    return tokens


def _use(oauth: OAuthHub, machine: Machine, tokens: dict[str, Any], client_id: str) -> None:
    assert oauth.tool_names(tokens["access_token"]) == list(H.SERVED_TOOLS)
    result = Hub(oauth.server).run(lambda c: c.call_tool("list_machines", {}), bearer=tokens["access_token"])
    assert not result.is_error and result.structured_content["machines"][0]["machine"] == A
    rows = machine.server.mailbox.audit_rows()
    assert any(r["origin"] == "claude.ai/owner" and r["verb"] == "list_machines" for r in rows)
    refreshed = oauth.request("POST", "/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                      "refresh_token": tokens["refresh_token"],
                                                      "resource": RESOURCE})
    assert refreshed.status_code == 200, refreshed.text
    assert oauth.tool_names(refreshed.json()["access_token"]) == list(H.SERVED_TOOLS)


def test_end_to_end_with_a_client_metadata_document(oauth, machine):
    metadata = _discover(oauth)
    assert metadata["client_id_metadata_document_supported"] is True
    tokens = _sign_in_through(oauth, metadata, CIMD_ID)
    assert oauth.metadata.fetched == [CIMD_ID]
    _use(oauth, machine, tokens, CIMD_ID)


def test_end_to_end_with_dynamic_client_registration(oauth, machine):
    metadata = _discover(oauth)
    registered = oauth.request("POST", oauth.local(metadata["registration_endpoint"]), json={
        "client_name": "Claude", "redirect_uris": [CALLBACK], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    tokens = _sign_in_through(oauth, metadata, client_id)
    assert oauth.metadata.fetched == []
    _use(oauth, machine, tokens, client_id)


def test_a_paused_hub_still_lists_tools_for_a_signed_in_client_but_refuses_calls(oauth, machine):
    tokens = oauth.tokens()
    store = L.PauseStore(machine.config.paths)
    store.pause("hub")
    try:
        assert oauth.tool_names(tokens["access_token"]) == list(H.SERVED_TOOLS)
        result = Hub(oauth.server).run(lambda c: c.call_tool("list_machines", {}), bearer=tokens["access_token"])
        assert result.is_error and result.structured_content["error"]["code"] == "paused"
    finally:
        store.resume("hub")


def test_a_tool_call_with_an_oauth_token_logs_the_client_id_and_never_the_token(oauth):
    tokens = oauth.tokens()
    Hub(oauth.server).run(lambda c: c.call_tool("list_machines", {}), bearer=tokens["access_token"])
    calls = [line for line in oauth.logs if line.startswith("llm-kit hub call: ")]
    assert len(calls) == 1 and "tool=list_machines outcome=ok" in calls[0]
    assert f'client_id="{CIMD_ID}"' in calls[0]
    for secret in (tokens["access_token"], tokens["refresh_token"], oauth.server.bearer):
        assert secret not in oauth.logs.text()
