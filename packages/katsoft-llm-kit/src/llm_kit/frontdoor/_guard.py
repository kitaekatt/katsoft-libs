"""Request guard for the llm-kit front door.

The front door attaches the owner's provider keys to every completion it
forwards, so any page that can reach its socket can spend them. A web page
reaches a loopback-bound server by DNS rebinding (the page's name resolves to
127.0.0.1 and the browser then treats the server as same-origin), and a
cross-origin ``text/plain`` POST needs no CORS preflight. The guard closes both
at the boundary, before routing sees the request, and adds an optional bearer
token for binds that other machines reach.

Checks, in order, on every HTTP request:

1. Host (only when the app was given its bind): 421 unless the Host names the
   bind. A loopback bind answers only loopback names on its own port, plus any
   names the owner listed. A non-loopback bind answers any Host unless the
   owner listed names, because LAN clients reach it by IP or machine name.
2. Bearer token (only when the owner set one): 401 unless
   ``Authorization: Bearer <token>`` matches in constant time.
3. Content type: a POST under ``/v1/`` must be ``application/json`` (parameters
   such as ``charset`` allowed), else 415.

The guard is a plain ASGI wrapper, so response streaming is untouched.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
from typing import Any, Awaitable, Callable, Iterable, Optional

_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
_JSON_TYPE = "application/json"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def error_body(message: str) -> dict[str, Any]:
    """The front door's error envelope: ``{"error": {"message", "type"}}``."""
    return {"error": {"message": message, "type": "invalid_request_error"}}


def is_loopback_bind(host: str) -> bool:
    """True when ``host`` (a ``--host`` value) is ``localhost`` or a loopback IP literal.

    Inputs: the bind host, with or without IPv6 brackets. Returns False for a
    wildcard (``0.0.0.0``, ``::``), another address, a machine name, or an empty
    string. Never raises.
    """
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def split_host(value: str) -> tuple[str, Optional[str]]:
    """Split a Host header value into a lower-case host and an optional port string.

    ``"Example.com:80"`` gives ``("example.com", "80")``; ``"[::1]:4000"`` gives
    ``("::1", "4000")``; a value with no port gives ``None`` for the port. A bare
    (unbracketed) IPv6 value is not a valid Host and is returned whole as the host.
    """
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            rest = text[end + 1:]
            return text[1:end], (rest[1:] if rest.startswith(":") else None)
        return text, None
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, port
    return text, None


def check_options(
    bind_host: Optional[str],
    port: Optional[int],
    allowed_hosts: Iterable[str],
    token: Optional[str],
) -> tuple[str, ...]:
    """Validate the guard options and return the allow-list as a tuple.

    Raises ``ValueError`` when ``bind_host`` and ``port`` are not given together,
    when ``allowed_hosts`` is given without a bind, or when ``token`` is empty.
    """
    hosts = tuple(allowed_hosts)
    if port is not None and bind_host is None:
        raise ValueError("port requires bind_host")
    if bind_host is not None and port is None:
        raise ValueError("bind_host requires port")
    if hosts and bind_host is None:
        raise ValueError("allowed_hosts requires bind_host")
    if token is not None and not token.strip():
        raise ValueError("token must not be empty (leave it unset to disable authentication)")
    return hosts


def host_permitted(
    header: Optional[str], bind_host: str, port: int, allowed_hosts: Iterable[str] = ()
) -> bool:
    """Whether a Host header value (``None`` if absent or repeated) may reach this bind.

    A loopback bind permits ``127.0.0.1``, ``localhost`` and ``[::1]`` with the
    bound ``port``, the bind host itself, and any ``allowed_hosts`` name. A
    non-loopback bind permits every Host when ``allowed_hosts`` is empty, else
    only the listed names. An allow-list entry with a port matches that host and
    port exactly; an entry without one matches the host on any port.
    """
    allowed = tuple(entry.strip().lower() for entry in allowed_hosts if entry.strip())
    loopback = is_loopback_bind(bind_host)
    if not loopback and not allowed:
        return True
    if header is None:
        return False
    value = header.strip().lower()
    if loopback:
        names = {f"{name}:{port}" for name in _LOOPBACK_NAMES}
        bound = bind_host.strip().strip("[]").lower()
        names.add(f"[{bound}]:{port}" if ":" in bound else f"{bound}:{port}")
        if value in names:
            return True
    host, host_port = split_host(value)
    for entry in allowed:
        entry_host, entry_port = split_host(entry)
        if entry_host == host and entry_port in (None, host_port):
            return True
    return False


def _single(headers: list[tuple[bytes, bytes]], name: bytes) -> Optional[bytes]:
    values = [value for key, value in headers if key.lower() == name]
    return values[0] if len(values) == 1 else None


async def _reply(send: Send, status: int, message: str, extra: tuple[tuple[bytes, bytes], ...] = ()) -> None:
    body = json.dumps(error_body(message)).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii")), *extra],
    })
    await send({"type": "http.response.body", "body": body})


class GuardMiddleware:
    """ASGI wrapper that applies the checks in the module docstring.

    ``bind_host`` and ``port`` switch the Host check on (both or neither);
    ``allowed_hosts`` are extra permitted names; ``token`` switches bearer
    authentication on. Build it through :func:`check_options` so bad options
    fail at construction, not at the first request.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        bind_host: Optional[str] = None,
        port: Optional[int] = None,
        allowed_hosts: tuple[str, ...] = (),
        token: Optional[str] = None,
    ) -> None:
        self.app = app
        self.bind_host = bind_host
        self.port = port
        self.allowed_hosts = allowed_hosts
        self._expected = token.encode("utf-8") if token is not None else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: list[tuple[bytes, bytes]] = list(scope["headers"])
        if self.bind_host is not None and self.port is not None:
            raw = _single(headers, b"host")
            header = raw.decode("latin-1") if raw is not None else None
            if not host_permitted(header, self.bind_host, self.port, self.allowed_hosts):
                await _reply(send, 421, "the front door does not answer to this Host header")
                return
        if self._expected is not None and not self._authorized(_single(headers, b"authorization")):
            await _reply(send, 401, "a valid bearer token is required", ((b"www-authenticate", b"Bearer"),))
            return
        if scope["method"] == "POST" and scope["path"].startswith("/v1/"):
            raw_type = _single(headers, b"content-type")
            media = raw_type.decode("latin-1").split(";", 1)[0].strip().lower() if raw_type is not None else ""
            if media != _JSON_TYPE:
                await _reply(send, 415, f"Content-Type must be {_JSON_TYPE}")
                return
        await self.app(scope, receive, send)

    def _authorized(self, value: Optional[bytes]) -> bool:
        if value is None or self._expected is None:
            return False
        scheme, _, credentials = value.partition(b" ")
        return scheme.lower() == b"bearer" and hmac.compare_digest(credentials, self._expected)


__all__ = ["GuardMiddleware", "check_options", "error_body", "host_permitted", "is_loopback_bind", "split_host"]
