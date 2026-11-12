"""Single-user OAuth 2.1 sign-in for the relay hub (unit hub-oauth).

Why. The claude.ai custom connector signs in with OAuth (MCP authorization,
revision 2025-11-25). This module is the hub's own authorization server and
its access-token check, for one user: the owner. It is on only when the hub
has a public URL (``hub.public_url`` in relay.yaml or ``llm-kit relay hub
--public-url``: the https origin a tunnel in front of the hub serves it at).
Without one the hub accepts its bearer only, as before. Either way the hub
binds loopback only; the tunnel is the only way in from outside.

Endpoints. Every URL the hub publishes is built from the public URL, never
from a request header.

====================================================  =========================================
``GET  /.well-known/oauth-protected-resource[/mcp]``  protected resource metadata (RFC 9728)
``GET  /.well-known/oauth-authorization-server``       authorization server metadata (RFC 8414)
``GET  /.well-known/openid-configuration``             the same document
``POST /register``                                     dynamic client registration (RFC 7591)
``GET  /authorize``                                    the consent page
``POST /authorize``                                    the owner's decision
``POST /token``                                        ``authorization_code`` and ``refresh_token``
====================================================  =========================================

Clients. A client is either a Client ID Metadata Document (an https
``client_id`` URL the hub fetches: claude.ai's recommended style) or a client
the hub registered through ``/register``. Every client is public: no secret,
PKCE ``S256`` required, and its redirect URI must be exactly one of
:data:`ALLOWED_REDIRECT_URIS`, the claude.ai connector callbacks. A metadata
document is fetched over https from public addresses only, with a size and
time cap and no redirects (:class:`MetadataFetcher`); its ``client_id`` must
equal its URL and its ``redirect_uris`` must hold the requested one.

Approval. The consent page names the client and the host it returns to and
asks for a one-time approval code. The hub prints the code on its own stderr
(its terminal) when it shows the page; the code is bound to that one request,
lives :data:`APPROVAL_TTL_S` and allows :data:`MAX_APPROVAL_ATTEMPTS` wrong
tries. No long-lived passphrase exists in any file, so a stranger who adds the
public URL to their own connector cannot approve it without the owner's
terminal. The form also carries a per-request CSRF token and a ``__Host-``
cookie that must both match.

Tokens. An access token lives :data:`ACCESS_TOKEN_TTL_S` and its audience is
the canonical MCP URL (``<public URL>/mcp``). A refresh token lives
:data:`REFRESH_TOKEN_TTL_S` and rotates on every use. Presenting a rotated
refresh token, or an authorization code a second time, revokes every token of
that sign-in. Only SHA-256 hashes of tokens are stored, in ``hub-oauth.json``
in the state directory (``0600``), so the file holds nothing a caller could
present; deleting it while the hub is stopped signs every client out.
Authorization codes (:data:`AUTH_CODE_TTL_S`) and pending consents live in
memory only. Nothing logs a token, a code or a verifier; the approval code is
printed on purpose, because printing it is how the owner sees it.

Authority. A request with a valid access token is served exactly as one with
the hub bearer: the hub calls its relays as origin ``claude.ai/<hub user>``.

Pause. While the hub is paused, ``/register``, ``/authorize`` and ``/token``
refuse (``temporarily_unavailable``); the metadata documents still answer, and
``/mcp`` keeps its own pause rule (tool calls refused, listing answered).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from llm_kit.relay.config import RelayConfigError, parse_public_url
from llm_kit.relay.lifecycle import PauseStore, read_private_file, write_private_file

#: The only redirect URIs a client may use: the claude.ai connector callbacks.
ALLOWED_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
#: Browser origins the connector may send on /mcp (besides the public origin and loopback).
CLIENT_ORIGINS: tuple[str, ...] = ("https://claude.ai", "https://claude.com")
GRANT_TYPES: tuple[str, ...] = ("authorization_code", "refresh_token")

ACCESS_TOKEN_TTL_S = 3600
REFRESH_TOKEN_TTL_S = 30 * 24 * 3600
AUTH_CODE_TTL_S = 120
APPROVAL_TTL_S = 600
MAX_APPROVAL_ATTEMPTS = 5
MAX_PENDING = 16
MAX_CLIENTS = 32
MAX_BODY_BYTES = 16 * 1024
MAX_CLIENT_NAME = 200
MAX_CLIENT_ID_URL = 2048
CIMD_TIMEOUT_S = 5.0
CIMD_MAX_BYTES = 64 * 1024

STORE_VERSION = 1
CONSENT_COOKIE = "__Host-llm-kit-consent"
PRM_PATH = "/.well-known/oauth-protected-resource"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
OIDC_METADATA_PATH = "/.well-known/openid-configuration"
REGISTER_PATH = "/register"
AUTHORIZE_PATH = "/authorize"
TOKEN_PATH = "/token"

#: Crockford base32 without I, L, O, U: what an approval code and a request label are made of.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_COOKIE_VALUE = re.compile(r"[A-Za-z0-9_-]{20,64}")


class OAuthError(Exception):
    """An OAuth error response: ``error`` code, description and HTTP status."""

    def __init__(self, error: str, description: str, *, status: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status = status


class ClientMetadataError(Exception):
    """A client metadata document could not be fetched or is not usable."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _same(presented: Optional[str], expected: str) -> bool:
    """Constant-time equality; ``None`` or empty never matches."""
    return bool(presented) and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def s256(verifier: str) -> str:
    """The PKCE S256 challenge of ``verifier`` (RFC 7636 section 4.2)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _random_code(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def normalize_approval_code(value: Optional[str]) -> str:
    """An approval code as typed, without spaces or dashes, upper case (``abcd-1234`` -> ``ABCD1234``)."""
    return re.sub(r"[\s-]", "", value or "").upper()


def canonical_resource(value: str) -> str:
    """A resource URL compared the RFC 8707 way: lower-case scheme and host, no default port, no trailing slash."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return value
    if parts.query or parts.fragment or parts.username is not None or not parts.hostname:
        return value
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = {"https": 443, "http": 80}.get(scheme)
    netloc = host + (f":{port}" if port is not None and port != default else "")
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _stderr_log(line: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} llm-kit hub oauth: {line}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# client metadata documents
# --------------------------------------------------------------------------

def check_client_id_url(url: object) -> str:
    """``url`` when it can be a client metadata document URL; :class:`ClientMetadataError` otherwise.

    An https URL with a host and a path, no user info, no fragment and no dot
    segments, at most :data:`MAX_CLIENT_ID_URL` characters.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_CLIENT_ID_URL:
        raise ClientMetadataError("the client_id URL is empty or too long")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError as exc:
        raise ClientMetadataError(f"the client_id {url!r} is not a URL: {exc}") from exc
    if parts.scheme != "https":
        raise ClientMetadataError(f"the client_id {url!r} is not an https URL")
    if not parts.hostname or parts.username is not None or "@" in parts.netloc:
        raise ClientMetadataError(f"the client_id {url!r} needs a host and no user info")
    if parts.fragment or url.endswith("#"):
        raise ClientMetadataError(f"the client_id {url!r} must not have a fragment")
    segments = parts.path.split("/")
    if parts.path in ("", "/") or any(segment in (".", "..") for segment in segments):
        raise ClientMetadataError(f"the client_id {url!r} needs a path without dot segments")
    return url


def is_public_address(address: str) -> bool:
    """True for a globally routable IP address (an IPv4-mapped IPv6 address is judged as IPv4)."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ``host`` (SNI and certificate checked against it) over a socket to the checked ``address``."""

    def __init__(self, host: str, address: str, port: int, *, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class MetadataFetcher:
    """Fetch a client metadata document: https, public addresses only, capped, no redirects.

    The host is resolved once; every address must be public
    (:func:`is_public_address`), and the connection goes to the checked
    address, so a second resolution cannot point it elsewhere. Any status but
    200 is refused (a redirect is not followed), as is a body over
    ``max_bytes``, a fetch over ``timeout_s`` in total, or a body that is not
    a JSON object. ``resolver``, ``address_allowed`` and ``ssl_context`` are
    injectable for tests.
    """

    def __init__(
        self,
        *,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        address_allowed: Callable[[str], bool] = is_public_address,
        timeout_s: float = CIMD_TIMEOUT_S,
        max_bytes: int = CIMD_MAX_BYTES,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self._resolver = resolver
        self._address_allowed = address_allowed
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._ssl_context = ssl_context

    def __call__(self, url: str) -> dict[str, Any]:
        check_client_id_url(url)
        parts = urlsplit(url)
        host = parts.hostname
        assert host is not None
        port = parts.port or 443
        deadline = time.monotonic() + self._timeout_s
        try:
            infos = self._resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ClientMetadataError(f"cannot resolve {host}: {exc}") from exc
        addresses = sorted({str(info[4][0]) for info in infos})
        if not addresses:
            raise ClientMetadataError(f"{host} resolves to no address")
        refused = [a for a in addresses if not self._address_allowed(a)]
        if refused:
            raise ClientMetadataError(
                f"{host} resolves to a non-public address ({', '.join(refused)}); "
                "client metadata is fetched from public addresses only"
            )
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection = self._connection(host, addresses[0], port)
        try:
            try:
                connection.request("GET", target, headers={"Accept": "application/json", "User-Agent": "llm-kit-hub"})
                response = connection.getresponse()
                if response.status != 200:
                    raise ClientMetadataError(
                        f"{url} answered HTTP {response.status}; only a 200 is used and no redirect is followed"
                    )
                declared = response.getheader("content-length")
                if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                    raise ClientMetadataError(f"{url} is larger than {self._max_bytes} bytes")
                body = self._read(connection, response, deadline)
            except (OSError, http.client.HTTPException) as exc:
                raise ClientMetadataError(f"cannot fetch {url}: {type(exc).__name__}: {exc}") from exc
        finally:
            connection.close()
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ClientMetadataError(f"{url} is not a JSON document: {exc}") from exc
        if not isinstance(document, dict):
            raise ClientMetadataError(f"{url} is not a JSON object")
        return document

    def _connection(self, host: str, address: str, port: int) -> http.client.HTTPConnection:
        context = self._ssl_context if self._ssl_context is not None else ssl.create_default_context()
        return _PinnedHTTPSConnection(host, address, port, timeout=self._timeout_s, context=context)

    def _read(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse,
              deadline: float) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientMetadataError(f"the client metadata fetch took longer than {self._timeout_s} s")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(8192) if hasattr(response, "read1") else response.read(8192)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > self._max_bytes:
                raise ClientMetadataError(f"the client metadata document is larger than {self._max_bytes} bytes")
            chunks.append(chunk)


def check_client_document(document: Mapping[str, Any], client_id: str, redirect_uri: str) -> str:
    """The display name of a usable client metadata document; :class:`ClientMetadataError` otherwise.

    Its ``client_id`` must equal the URL it was fetched from and its
    ``redirect_uris`` must hold ``redirect_uri``.
    """
    if document.get("client_id") != client_id:
        raise ClientMetadataError(
            f"the document's client_id {document.get('client_id')!r} is not the URL it was fetched from"
        )
    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not all(isinstance(u, str) for u in redirect_uris):
        raise ClientMetadataError("the document has no redirect_uris list")
    if redirect_uri not in redirect_uris:
        raise ClientMetadataError(f"the document does not list the redirect_uri {redirect_uri}")
    name = document.get("client_name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:MAX_CLIENT_NAME]
    return urlsplit(client_id).hostname or client_id


# --------------------------------------------------------------------------
# persisted clients and token hashes
# --------------------------------------------------------------------------

class TokenStore:
    """Registered clients and token hashes in one ``0600`` JSON file; the hub is its only writer.

    The hub is a singleton per state directory, so this process owns the file;
    every change rewrites it atomically. Expired tokens are dropped on every
    write. A file that is not a version-:data:`STORE_VERSION` store, or has a
    loose mode, is refused loudly.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._clients, self._access, self._refresh = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not self._path.exists():
            return {}, {}, {}
        try:
            raw = json.loads(read_private_file(self._path))
        except ValueError as exc:
            raise RelayConfigError(f"hub OAuth store {self._path} is not JSON: {exc}") from exc
        sections = ("clients", "access_tokens", "refresh_tokens")
        if (not isinstance(raw, dict) or raw.get("version") != STORE_VERSION
                or not all(isinstance(raw.get(key), dict) for key in sections)):
            raise RelayConfigError(f"hub OAuth store {self._path} is not a version-{STORE_VERSION} store")
        return raw["clients"], raw["access_tokens"], raw["refresh_tokens"]

    def _save_locked(self) -> None:
        now = self._clock()
        self._access = {k: v for k, v in self._access.items() if v["expires_at"] > now}
        self._refresh = {k: v for k, v in self._refresh.items() if v["expires_at"] > now}
        payload = {"version": STORE_VERSION, "clients": self._clients, "access_tokens": self._access,
                   "refresh_tokens": self._refresh}
        write_private_file(self._path, json.dumps(payload, indent=1, sort_keys=True) + "\n")

    # -- clients ----------------------------------------------------------------------

    def client(self, client_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            record = self._clients.get(client_id)
            return dict(record) if record is not None else None

    def add_client(self, record: Mapping[str, Any]) -> str:
        """Store a registered client and return its new id; the oldest go past :data:`MAX_CLIENTS`."""
        with self._lock:
            client_id = "llm-kit-" + secrets.token_urlsafe(16)
            self._clients[client_id] = dict(record)
            while len(self._clients) > MAX_CLIENTS:
                oldest = min(self._clients, key=lambda key: self._clients[key]["issued_at"])
                del self._clients[oldest]
            self._save_locked()
            return client_id

    # -- tokens -----------------------------------------------------------------------

    def _issue_locked(self, *, client_id: str, resource: str, scope: Optional[str], family: str) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self._clock()
        base = {"client_id": client_id, "resource": resource, "scope": scope, "family": family}
        self._access[_digest(access)] = {**base, "expires_at": now + ACCESS_TOKEN_TTL_S}
        self._refresh[_digest(refresh)] = {**base, "expires_at": now + REFRESH_TOKEN_TTL_S, "rotated": False}
        self._save_locked()
        response: dict[str, Any] = {"access_token": access, "token_type": "Bearer",
                                    "expires_in": ACCESS_TOKEN_TTL_S, "refresh_token": refresh}
        if scope:
            response["scope"] = scope
        return response

    def issue(self, *, client_id: str, resource: str, scope: Optional[str], family: str) -> dict[str, Any]:
        """A new access and refresh token pair for one sign-in (``family``); the token response body."""
        with self._lock:
            return self._issue_locked(client_id=client_id, resource=resource, scope=scope, family=family)

    def access(self, token: str) -> Optional[dict[str, Any]]:
        """The live access token record for ``token``, or None."""
        with self._lock:
            record = self._access.get(_digest(token))
            if record is None or record["expires_at"] <= self._clock():
                return None
            return dict(record)

    def rotate(self, token: str, *, check: Callable[[Mapping[str, Any]], None]) -> dict[str, Any]:
        """Exchange a refresh token for a new pair; ``check`` may refuse (it consumes nothing).

        A refresh token that was already rotated revokes its whole sign-in.
        """
        with self._lock:
            record = self._refresh.get(_digest(token))
            if record is None or record["expires_at"] <= self._clock():
                raise OAuthError("invalid_grant", "the refresh token is unknown or expired")
            if record["rotated"]:
                self._revoke_locked(record["family"])
                self._save_locked()
                raise OAuthError("invalid_grant", "the refresh token was already used; this sign-in is revoked")
            check(record)
            record["rotated"] = True
            return self._issue_locked(client_id=record["client_id"], resource=record["resource"],
                                      scope=record["scope"], family=record["family"])

    def _revoke_locked(self, family: str) -> None:
        self._access = {k: v for k, v in self._access.items() if v["family"] != family}
        self._refresh = {k: v for k, v in self._refresh.items() if v["family"] != family}

    def revoke_family(self, family: str) -> None:
        """Remove every token of one sign-in."""
        with self._lock:
            self._revoke_locked(family)
            self._save_locked()


# --------------------------------------------------------------------------
# ASGI plumbing
# --------------------------------------------------------------------------

class _BodyTooLarge(Exception):
    pass


@dataclass
class Request:
    """One HTTP request as the OAuth endpoints see it."""

    method: str
    path: str
    query: bytes
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())

    def cookie(self, name: str) -> Optional[str]:
        for part in (self.header("cookie") or "").split(";"):
            key, eq, value = part.strip().partition("=")
            if eq and key == name:
                return value
        return None


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str
    headers: list[tuple[str, str]]


def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name not in out:
            out[name] = value
        elif name == "cookie":  # several Cookie headers are one list; for every other header the first wins
            out[name] = f"{out[name]}; {value}"
    return out


async def _read_body(receive: Callable[..., Any], limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        body = message.get("body", b"")
        size += len(body)
        if size > limit:
            raise _BodyTooLarge()
        chunks.append(body)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


async def send_response(send: Callable[..., Any], response: Response) -> None:
    headers = [(b"content-type", response.content_type.encode("latin-1")),
               (b"content-length", str(len(response.body)).encode("ascii")),
               (b"cache-control", b"no-store")]
    headers += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers]
    await send({"type": "http.response.start", "status": response.status, "headers": headers})
    await send({"type": "http.response.body", "body": response.body})


def _json(status: int, payload: Mapping[str, Any], headers: Optional[list[tuple[str, str]]] = None) -> Response:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return Response(status, body, "application/json", list(headers or []))


def _oauth_error(exc: OAuthError) -> Response:
    return _json(exc.status, {"error": exc.error, "error_description": exc.description}, [("pragma", "no-cache")])


def _params(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Request parameters; a repeated one is refused and an empty one counts as absent (RFC 6749 3.1)."""
    out: dict[str, str] = {}
    for key, value in pairs:
        if key in out:
            raise OAuthError("invalid_request", f"parameter {key!r} is repeated")
        if value != "":
            out[key] = value
    return out


def _query(request: Request) -> dict[str, str]:
    try:
        return _params(parse_qsl(request.query.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        raise OAuthError("invalid_request", "the query string is not UTF-8") from None


def _form(request: Request) -> dict[str, str]:
    content_type = (request.header("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise OAuthError("invalid_request", "the body must be application/x-www-form-urlencoded")
    try:
        return _params(parse_qsl(request.body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        raise OAuthError("invalid_request", "the body is not UTF-8") from None


def _need(params: Mapping[str, str], name: str) -> str:
    value = params.get(name)
    if value is None:
        raise OAuthError("invalid_request", f"{name} is required")
    return value


def _basic_client_id(header: Optional[str]) -> Optional[str]:
    """The client id in an ``Authorization: Basic`` header (any secret is ignored: every client is public)."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise OAuthError("invalid_client", "the Basic authorization header is malformed", status=401) from None
    return unquote(decoded.partition(":")[0]) or None


# --------------------------------------------------------------------------
# the consent page
# --------------------------------------------------------------------------

_PAGE_STYLE = (
    "body{font-family:-apple-system,system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;"
    "line-height:1.45;color:#1d1d1f;background:#fff}h1{font-size:1.35rem}dt{font-weight:600;margin-top:.5rem}"
    "dd{margin:0;word-break:break-all}input[name=approval_code]{font:1.4rem ui-monospace,monospace;"
    "letter-spacing:.15em;width:12ch;padding:.3rem;text-transform:uppercase}button{font-size:1rem;"
    "padding:.45rem 1.1rem;margin:.8rem .5rem 0 0}.error{color:#b00020;font-weight:600}"
    "@media (prefers-color-scheme:dark){body{color:#eee;background:#111}}"
)
_PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
             "form-action 'self' " + " ".join(CLIENT_ORIGINS))


def _page(status: int, title: str, body_html: str, headers: Optional[list[tuple[str, str]]] = None) -> Response:
    text = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)} - llm-kit hub</title><style>{_PAGE_STYLE}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body_html}</main></body></html>"
    )
    page_headers = [("content-security-policy", _PAGE_CSP), ("x-frame-options", "DENY"),
                    ("referrer-policy", "no-referrer"), ("x-content-type-options", "nosniff")]
    return Response(status, text.encode("utf-8"), "text/html; charset=utf-8", page_headers + list(headers or []))


def _notice(status: int, title: str, message: str) -> Response:
    return _page(status, title, f"<p>{html.escape(message)}</p>")


@dataclass
class _Pending:
    """One consent page waiting for the owner's decision (memory only)."""

    request_id: str
    label: str
    csrf: str
    binding: str
    approval_code: str
    client_id: str
    client_name: str
    redirect_uri: str
    challenge: str
    resource: str
    scope: Optional[str]
    state: Optional[str]
    created_at: float
    expires_at: float
    attempts: int = 0


@dataclass
class _Code:
    """One issued authorization code (memory only, keyed by its hash)."""

    client_id: str
    redirect_uri: str
    challenge: str
    resource: str
    scope: Optional[str]
    expires_at: float
    used: bool = False
    family: Optional[str] = None


# --------------------------------------------------------------------------
# the authorization server
# --------------------------------------------------------------------------

class HubOAuth:
    """The hub's authorization server and access-token check, for one owner.

    ``public_url`` is the hub's public https origin; ``store_path`` the
    ``hub-oauth.json`` file; ``pause`` the hub's pause flags. The client
    metadata fetcher, the log line sink (default: the hub's stderr) and the
    clock are injectable.
    """

    def __init__(
        self,
        public_url: str,
        *,
        store_path: Path,
        pause: PauseStore,
        mcp_path: str = "/mcp",
        fetch_client_metadata: Optional[Callable[[str], Mapping[str, Any]]] = None,
        log: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.time,
        pause_scope: str = "hub",
    ) -> None:
        self.issuer = parse_public_url(public_url)
        self.resource = self.issuer + mcp_path
        self.resource_metadata_url = self.issuer + PRM_PATH + mcp_path
        self.public_host = urlsplit(self.issuer).netloc
        self._store = TokenStore(store_path, clock=clock)
        self._pause = pause
        self._pause_scope = pause_scope
        self._fetch = fetch_client_metadata if fetch_client_metadata is not None else MetadataFetcher()
        self.log = log if log is not None else _stderr_log
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, _Code] = {}
        self._routes: dict[str, dict[str, Callable[[Request], Any]]] = {
            PRM_PATH: {"GET": self._protected_resource_metadata},
            PRM_PATH + mcp_path: {"GET": self._protected_resource_metadata},
            AS_METADATA_PATH: {"GET": self._authorization_server_metadata},
            OIDC_METADATA_PATH: {"GET": self._authorization_server_metadata},
            REGISTER_PATH: {"POST": self._register},
            AUTHORIZE_PATH: {"GET": self._authorize_page, "POST": self._authorize_decision},
            TOKEN_PATH: {"POST": self._token},
        }

    @property
    def store(self) -> TokenStore:
        return self._store

    # -- the resource server side ----------------------------------------------------------

    def verify_access(self, token: str) -> bool:
        """True when ``token`` is a live access token whose audience is this hub's MCP URL."""
        record = self._store.access(token)
        return record is not None and record["resource"] == self.resource

    def www_authenticate(self, *, invalid_token: bool) -> str:
        """The ``WWW-Authenticate`` value of a 401 from ``/mcp`` (RFC 9728 section 5.1)."""
        value = f'Bearer resource_metadata="{self.resource_metadata_url}"'
        if invalid_token:
            value += ', error="invalid_token", error_description="the access token is unknown, expired or for another resource"'
        return value

    # -- dispatch -------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return path in self._routes

    async def handle(self, scope: Mapping[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        """Serve one OAuth endpoint request (``scope['path']`` must be one :meth:`handles` names)."""
        methods = self._routes[scope["path"]]
        handler = methods.get(scope["method"])
        if handler is None:
            await send_response(send, _json(405, {"error": "invalid_request",
                                                  "error_description": f"{scope['method']} is not allowed here"},
                                            [("allow", ", ".join(methods))]))
            return
        try:
            body = await _read_body(receive, MAX_BODY_BYTES) if scope["method"] == "POST" else b""
        except _BodyTooLarge:
            await send_response(send, _json(413, {"error": "invalid_request",
                                                  "error_description": "the request body is too large"}))
            return
        request = Request(scope["method"], scope["path"], scope.get("query_string", b""), _headers(scope), body)
        await send_response(send, await handler(request))

    def _paused(self) -> bool:
        return self._pause.is_paused(self._pause_scope)

    # -- metadata -------------------------------------------------------------------------

    async def _protected_resource_metadata(self, request: Request) -> Response:
        self.log(f"served protected resource metadata ({request.path}) to {request.header('user-agent')!r}")
        return _json(200, {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "resource_name": "llm-kit hub",
        })

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + AUTHORIZE_PATH,
            "token_endpoint": self.issuer + TOKEN_PATH,
            "registration_endpoint": self.issuer + REGISTER_PATH,
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": list(GRANT_TYPES),
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }

    async def _authorization_server_metadata(self, request: Request) -> Response:
        self.log(f"served authorization server metadata ({request.path}) to {request.header('user-agent')!r}")
        return _json(200, self.authorization_server_metadata())

    # -- dynamic client registration -----------------------------------------------------------

    async def _register(self, request: Request) -> Response:
        if self._paused():
            return _json(503, {"error": "temporarily_unavailable", "error_description": "the hub is paused"})

        def refuse(error: str, description: str) -> Response:
            self.log(f"registration refused: {error}: {description}")
            return _json(400, {"error": error, "error_description": description})

        content_type = (request.header("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return refuse("invalid_client_metadata", "the registration body must be application/json")
        try:
            metadata = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return refuse("invalid_client_metadata", "the registration body is not JSON")
        if not isinstance(metadata, dict):
            return refuse("invalid_client_metadata", "the registration body is not a JSON object")
        redirect_uris = metadata.get("redirect_uris")
        if (not isinstance(redirect_uris, list) or not redirect_uris
                or not all(isinstance(uri, str) for uri in redirect_uris)):
            return refuse("invalid_redirect_uri", "redirect_uris must be a non-empty list of URLs")
        foreign = [uri for uri in redirect_uris if uri not in ALLOWED_REDIRECT_URIS]
        if foreign:
            return refuse("invalid_redirect_uri",
                          f"only the claude.ai connector callbacks are allowed, not {', '.join(foreign)}")
        grant_types = metadata.get("grant_types", ["authorization_code"])
        if not isinstance(grant_types, list) or not set(grant_types) <= set(GRANT_TYPES) or not grant_types:
            return refuse("invalid_client_metadata", f"grant_types must be drawn from {', '.join(GRANT_TYPES)}")
        response_types = metadata.get("response_types", ["code"])
        if response_types != ["code"]:
            return refuse("invalid_client_metadata", "response_types must be [\"code\"]")
        name = metadata.get("client_name")
        if name is not None and (not isinstance(name, str) or len(name) > MAX_CLIENT_NAME):
            return refuse("invalid_client_metadata", f"client_name must be a string of at most {MAX_CLIENT_NAME} characters")
        method = metadata.get("token_endpoint_auth_method", "none")
        now = int(self._clock())
        record = {"client_name": name, "redirect_uris": list(dict.fromkeys(redirect_uris)), "issued_at": now}
        client_id = self._store.add_client(record)
        note = "" if method == "none" else f" (asked for {method!r}; registered as a public client, auth method none)"
        self.log(f"registered client {client_id} {name!r} for {', '.join(record['redirect_uris'])}{note}")
        response = {"client_id": client_id, "client_id_issued_at": now, "redirect_uris": record["redirect_uris"],
                    "grant_types": list(GRANT_TYPES), "response_types": ["code"],
                    "token_endpoint_auth_method": "none"}
        if name is not None:
            response["client_name"] = name
        return _json(201, response)

    # -- authorization -------------------------------------------------------------------------

    async def _resolve_client(self, client_id: str, redirect_uri: str) -> str:
        """The client's display name, once it is known and ``redirect_uri`` is one of its own."""
        if client_id.startswith("https://"):
            try:
                check_client_id_url(client_id)
                loop = asyncio.get_running_loop()
                document = await loop.run_in_executor(None, self._fetch, client_id)
                return check_client_document(document, client_id, redirect_uri)
            except ClientMetadataError as exc:
                raise OAuthError("invalid_client", f"client metadata document {client_id}: {exc}") from exc
        record = self._store.client(client_id)
        if record is None:
            raise OAuthError("invalid_client", f"unknown client_id {client_id!r}; register it or use a "
                                               "client metadata document URL")
        if redirect_uri not in record["redirect_uris"]:
            raise OAuthError("invalid_request", f"redirect_uri {redirect_uri} is not registered for this client")
        return record.get("client_name") or client_id

    def _redirect(self, redirect_uri: str, params: Mapping[str, Optional[str]]) -> Response:
        query = {key: value for key, value in params.items() if value is not None}
        query["iss"] = self.issuer
        location = redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(query)
        return Response(302, b"", "text/plain; charset=utf-8", [("location", location), ("referrer-policy", "no-referrer")])

    async def _authorize_page(self, request: Request) -> Response:
        if self._paused():
            return _notice(503, "The hub is paused", "Resume it (llm-kit relay resume --scope hub), then connect again.")
        try:
            params = _query(request)
        except OAuthError as exc:
            return _notice(400, "Sign-in request refused", exc.description)
        client_id, redirect_uri = params.get("client_id"), params.get("redirect_uri")
        self.log(f"authorize request: client_id={client_id!r} redirect_uri={redirect_uri!r} "
                 f"user_agent={request.header('user-agent')!r}")
        if not client_id or not redirect_uri:
            return _notice(400, "Sign-in request refused", "The request names no client_id or no redirect_uri.")
        if redirect_uri not in ALLOWED_REDIRECT_URIS:
            self.log(f"authorize refused: redirect_uri {redirect_uri!r} is not a claude.ai connector callback")
            return _notice(400, "Sign-in request refused",
                           f"{redirect_uri} is not a claude.ai connector callback; this hub signs in claude.ai only.")
        try:
            client_name = await self._resolve_client(client_id, redirect_uri)
        except OAuthError as exc:
            self.log(f"authorize refused: {exc.error}: {exc.description}")
            return _notice(400, "Sign-in request refused", exc.description)

        # The redirect URI is now trusted: every later refusal goes back to the client.
        state = params.get("state")

        def back(error: str, description: str) -> Response:
            self.log(f"authorize refused for {client_id}: {error}: {description}")
            return self._redirect(redirect_uri, {"error": error, "error_description": description, "state": state})

        if params.get("response_type") != "code":
            return back("unsupported_response_type", "response_type must be code")
        challenge, method = params.get("code_challenge"), params.get("code_challenge_method")
        if not challenge:
            return back("invalid_request", "code_challenge is required (PKCE S256)")
        if method != "S256":
            return back("invalid_request", "code_challenge_method must be S256")
        if not _CHALLENGE.fullmatch(challenge):
            return back("invalid_request", "code_challenge is not a base64url SHA-256 value")
        resource = params.get("resource")
        if not resource:
            return back("invalid_target", f"resource is required and must be {self.resource}")
        if canonical_resource(resource) != self.resource:
            return back("invalid_target", f"resource must be {self.resource}")

        now = self._clock()
        binding = request.cookie(CONSENT_COOKIE)
        if binding is None or not _COOKIE_VALUE.fullmatch(binding):
            binding = secrets.token_urlsafe(24)
        pending = _Pending(
            request_id=secrets.token_urlsafe(16), label="R-" + _random_code(4), csrf=secrets.token_urlsafe(24),
            binding=binding, approval_code=_random_code(8), client_id=client_id, client_name=client_name,
            redirect_uri=redirect_uri, challenge=challenge, resource=self.resource, scope=params.get("scope"),
            state=state, created_at=now, expires_at=now + APPROVAL_TTL_S,
        )
        with self._lock:
            self._prune_locked(now)
            while len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
                del self._pending[oldest]
            self._pending[pending.request_id] = pending
        code = pending.approval_code
        self.log(f"sign-in request {pending.label}: {client_name!r} ({client_id}) returning to "
                 f"{urlsplit(redirect_uri).hostname}")
        self.log(f">>> approval code for {pending.label}: {code[:4]}-{code[4:]} "
                 f"(valid {APPROVAL_TTL_S // 60} minutes, one use) <<<")
        cookie = (f"{CONSENT_COOKIE}={binding}; Path=/; Secure; HttpOnly; SameSite=Lax; "
                  f"Max-Age={APPROVAL_TTL_S}")
        return self._consent_page(pending, status=200, error=None, headers=[("set-cookie", cookie)])

    def _consent_page(self, pending: _Pending, *, status: int, error: Optional[str],
                      headers: Optional[list[tuple[str, str]]] = None) -> Response:
        e = html.escape
        started = time.strftime("%H:%M:%S UTC", time.gmtime(pending.created_at))
        error_html = f"<p class=\"error\">{e(error)}</p>" if error else ""
        body = (
            f"<p><b>{e(pending.client_name)}</b> asks to connect to this hub as you. Once approved it can list and "
            "read your coding sessions, send messages to sessions that accept them, and answer their questions, "
            "and the sessions treat its messages as yours.</p>"
            f"<dl><dt>Client</dt><dd>{e(pending.client_id)}</dd>"
            f"<dt>Returns to</dt><dd>{e(urlsplit(pending.redirect_uri).hostname or pending.redirect_uri)}</dd>"
            f"<dt>Request</dt><dd>{e(pending.label)}, started {e(started)}</dd></dl>"
            "<p>Approve only if you clicked Connect yourself just now. Type the approval code the hub printed "
            f"next to <b>{e(pending.label)}</b> in its terminal.</p>"
            f"{error_html}"
            f"<form method=\"post\" action=\"{AUTHORIZE_PATH}\">"
            f"<input type=\"hidden\" name=\"request_id\" value=\"{e(pending.request_id)}\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{e(pending.csrf)}\">"
            "<p><label>Approval code<br><input name=\"approval_code\" autocomplete=\"one-time-code\" "
            "autocapitalize=\"characters\" spellcheck=\"false\" maxlength=\"16\" autofocus></label></p>"
            "<button name=\"decision\" value=\"approve\">Approve</button>"
            "<button name=\"decision\" value=\"deny\">Deny</button></form>"
        )
        return _page(status, "Approve sign-in to your llm-kit hub", body, headers)

    def _prune_locked(self, now: float) -> None:
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}

    async def _authorize_decision(self, request: Request) -> Response:
        try:
            form = _form(request)
        except OAuthError as exc:
            return _notice(400, "Sign-in request refused", exc.description)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            pending = self._pending.get(form.get("request_id", ""))
        if pending is None:
            return _notice(400, "This sign-in request has expired",
                           "It timed out or was already decided. Start again from Connect in claude.ai.")
        if not (_same(form.get("csrf"), pending.csrf) and _same(request.cookie(CONSENT_COOKIE), pending.binding)):
            self.log(f"sign-in request {pending.label}: refused a decision without its CSRF token and cookie")
            return _notice(403, "Sign-in request refused", "This form did not come from the hub's own consent page.")
        if self._paused():
            return _notice(503, "The hub is paused", "Resume it (llm-kit relay resume --scope hub), then try again.")
        decision = form.get("decision")
        if decision == "deny":
            self._drop(pending)
            self.log(f"sign-in request {pending.label}: denied by the owner")
            return self._redirect(pending.redirect_uri, {"error": "access_denied",
                                                         "error_description": "the owner denied the sign-in",
                                                         "state": pending.state})
        if decision != "approve":
            return _notice(400, "Sign-in request refused", "The form named no decision.")
        if not _same(normalize_approval_code(form.get("approval_code")), pending.approval_code):
            with self._lock:
                pending.attempts += 1
                attempts = pending.attempts
            if attempts >= MAX_APPROVAL_ATTEMPTS:
                self._drop(pending)
                self.log(f"sign-in request {pending.label}: refused after {attempts} wrong approval codes")
                return self._redirect(pending.redirect_uri, {"error": "access_denied",
                                                             "error_description": "too many wrong approval codes",
                                                             "state": pending.state})
            self.log(f"sign-in request {pending.label}: wrong approval code ({attempts} of {MAX_APPROVAL_ATTEMPTS})")
            return self._consent_page(pending, status=400, error=(
                f"That is not the approval code for {pending.label}. "
                f"{MAX_APPROVAL_ATTEMPTS - attempts} tries left."))
        self._drop(pending)
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._codes[_digest(code)] = _Code(
                client_id=pending.client_id, redirect_uri=pending.redirect_uri, challenge=pending.challenge,
                resource=pending.resource, scope=pending.scope, expires_at=now + AUTH_CODE_TTL_S,
            )
        self.log(f"sign-in request {pending.label}: approved; authorization code issued to {pending.client_id}")
        return self._redirect(pending.redirect_uri, {"code": code, "state": pending.state})

    def _drop(self, pending: _Pending) -> None:
        with self._lock:
            self._pending.pop(pending.request_id, None)

    # -- tokens --------------------------------------------------------------------------

    async def _token(self, request: Request) -> Response:
        try:
            if self._paused():
                raise OAuthError("temporarily_unavailable", "the hub is paused", status=503)
            form = _form(request)
            basic = _basic_client_id(request.header("authorization"))
            client_id = form.get("client_id")
            if basic is not None and client_id is not None and basic != client_id:
                raise OAuthError("invalid_client", "client_id differs from the Basic authorization header", status=401)
            client_id = client_id or basic
            grant_type = _need(form, "grant_type")
            self.log(f"token request: grant_type={grant_type!r} client_id={client_id!r}")
            if grant_type == "authorization_code":
                tokens = self._grant_code(form, client_id)
            elif grant_type == "refresh_token":
                tokens = self._grant_refresh(form, client_id)
            else:
                raise OAuthError("unsupported_grant_type", f"grant_type {grant_type!r} is not supported")
        except OAuthError as exc:
            self.log(f"token request refused: {exc.error}: {exc.description}")
            return _oauth_error(exc)
        return _json(200, tokens, [("pragma", "no-cache")])

    def _grant_code(self, form: Mapping[str, str], client_id: Optional[str]) -> dict[str, Any]:
        code = _need(form, "code")
        redirect_uri = _need(form, "redirect_uri")
        verifier = _need(form, "code_verifier")
        if client_id is None:
            raise OAuthError("invalid_request", "client_id is required")
        with self._lock:  # a code is spent by its first presentation, whatever the outcome
            self._prune_locked(self._clock())
            record = self._codes.get(_digest(code))
            reused = record is not None and record.used
            if record is not None:
                record.used = True
        if record is None:
            raise OAuthError("invalid_grant", "the authorization code is unknown or expired")
        if reused:
            if record.family is not None:
                self._store.revoke_family(record.family)
            raise OAuthError("invalid_grant", "the authorization code was already used; its tokens are revoked")
        if record.client_id != client_id:
            raise OAuthError("invalid_grant", "the authorization code was issued to another client")
        if record.redirect_uri != redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri differs from the authorization request")
        if not _VERIFIER.fullmatch(verifier) or not _same(s256(verifier), record.challenge):
            raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge")
        resource = form.get("resource")
        if resource is not None and canonical_resource(resource) != record.resource:
            raise OAuthError("invalid_target", f"resource must be {record.resource}")
        family = secrets.token_urlsafe(12)
        record.family = family
        tokens = self._store.issue(client_id=client_id, resource=record.resource, scope=record.scope, family=family)
        self.log(f"tokens issued to {client_id} (sign-in {family[:6]})")
        return tokens

    def _grant_refresh(self, form: Mapping[str, str], client_id: Optional[str]) -> dict[str, Any]:
        token = _need(form, "refresh_token")
        resource = form.get("resource")

        def check(record: Mapping[str, Any]) -> None:
            if client_id is not None and record["client_id"] != client_id:
                raise OAuthError("invalid_grant", "the refresh token was issued to another client")
            if record["resource"] != self.resource:
                raise OAuthError("invalid_grant", "the refresh token is for another resource")
            if resource is not None and canonical_resource(resource) != record["resource"]:
                raise OAuthError("invalid_target", f"resource must be {record['resource']}")

        tokens = self._store.rotate(token, check=check)
        self.log(f"tokens refreshed for {client_id!r}")
        return tokens


__all__ = [
    "ACCESS_TOKEN_TTL_S",
    "ALLOWED_REDIRECT_URIS",
    "APPROVAL_TTL_S",
    "AUTH_CODE_TTL_S",
    "CLIENT_ORIGINS",
    "CONSENT_COOKIE",
    "ClientMetadataError",
    "HubOAuth",
    "MAX_APPROVAL_ATTEMPTS",
    "MetadataFetcher",
    "OAuthError",
    "REFRESH_TOKEN_TTL_S",
    "TokenStore",
    "canonical_resource",
    "check_client_document",
    "check_client_id_url",
    "is_public_address",
    "normalize_approval_code",
    "s256",
]
