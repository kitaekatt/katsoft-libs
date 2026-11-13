"""``RelayClient`` over HTTP (unit S3): one connection to one relay.

:class:`HttpRelayClient` implements :class:`llm_kit.relay.protocol.RelayClient`.
Every verb method sends one ``POST /v1/<verb>`` request envelope with the
relay token (``X-Relay-Token``), the optional grant reference
(``X-Relay-Grant``) and, for a hub-origin call, the hub bearer
(``Authorization: Bearer``), and returns the result or raises
:class:`llm_kit.relay.protocol.RelayError` with the error envelope's code. A
transport failure (refused connection, timeout) is ``machine_unreachable``.

A loopback URL must be ``http`` (this machine's relay). A non-loopback URL is
a PEER relay on another machine: it must be ``https`` (TLS verified against
the system trust roots, never disabled) and the caller must give that peer's
own relay token and hub bearer explicitly. This machine's credentials are
never sent to a peer URL and there is no fallback to them. Redirects are never
followed and proxy variables in the environment are ignored, for peers and
loopback alike. A peer connection that cannot be opened within 3 seconds
(``PEER_CONNECT_TIMEOUT_S``) is ``machine_unreachable``, and so is a gateway
answer (HTTP 502, 503 or 504) that carries no relay envelope, which is what a
tunnel in front of a relay that is down returns.

Sent or not (amendment 2). Every error this client makes up itself, rather
than reads from the relay's error envelope, is a :class:`ClientRelayError`
whose ``sent`` says whether the request may have reached the relay: ``False``
only when the connection was never opened (name resolution failed, the
connection was refused, the connect timed out, no pooled connection became
free), ``True`` for everything after (a read or write timeout, a dropped
connection, a gateway answer, a reply that is not a relay envelope). Its
``details`` carry ``sent`` and, when sent, ``outcome: "unknown"``. A
forwarding relay refunds a grant use only for ``sent: False``.

Forwarding (amendment 2). ``peer_key`` is sent as ``X-Relay-Peer-Key`` on
every call whose origin is a ``relay:`` identity, and ``call`` takes
``on_behalf_of`` for the request envelope.

:meth:`HttpRelayClient.local` builds a client for this machine's relay from
the relay configuration: the bound address from ``RelayConfig.bind("relay")``
and the token read (not created) from the state directory.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Mapping, Optional

from llm_kit.relay import protocol as P
from llm_kit.relay.config import RelayConfig, is_loopback_url
from llm_kit.relay.lifecycle import read_token

DEFAULT_TIMEOUT_S = 30.0
#: ``ask_llm`` waits for a completion; a cold local model load is slow (L4 caps a call at 600 s).
DEFAULT_LLM_TIMEOUT_S = 660.0
_LONG_POLL_VERBS = frozenset({"poll", "get_replies"})
#: A peer relay that does not accept a connection within this long is unreachable.
PEER_CONNECT_TIMEOUT_S = 3.0
#: HTTP statuses a gateway (a tunnel, a proxy) answers with, as text, when the relay behind it is down.
_GATEWAY_STATUSES = frozenset({502, 503, 504})


class ClientRelayError(P.RelayError):
    """An error this client made up (no relay error envelope); ``sent`` is whether the request may have arrived."""

    def __init__(self, code: str, message: str, *, sent: bool, details: Optional[Mapping[str, Any]] = None) -> None:
        merged = dict(details or {})
        merged["sent"] = sent
        if sent:
            merged["outcome"] = "unknown"
        super().__init__(code, message, details=merged)
        self.sent = sent


def base_url(host: str, port: int) -> str:
    """``http://host:port`` with an IPv6 host bracketed."""
    address = host.strip().strip("[]")
    return f"http://[{address}]:{port}" if ":" in address else f"http://{address}:{port}"


class HttpRelayClient:
    """A :class:`~llm_kit.relay.protocol.RelayClient` for one relay over loopback HTTP.

    ``machine`` is the relay's machine label (reported by :attr:`machine` and
    named in ``machine_unreachable``). ``hub_bearer`` is sent only on
    hub-origin calls and is required for a peer (non-loopback) URL.
    ``transport`` is an optional ``httpx`` transport. ``connect_timeout_s``
    bounds opening a connection; it defaults to
    :data:`PEER_CONNECT_TIMEOUT_S` for a peer URL and to the call timeout
    for a loopback one.
    """

    def __init__(
        self,
        url: str,
        *,
        machine: str,
        relay_token: str,
        hub_bearer: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
        transport: Any = None,
        connect_timeout_s: Optional[float] = None,
        peer_key: Optional[str] = None,
    ) -> None:
        peer = not is_loopback_url(url)
        if peer:
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError(f"relay URL {url!r} is not a loopback URL, so it must be an https URL; "
                                 "a credential never crosses a network in clear text")
            if not hub_bearer:
                raise ValueError(f"relay URL {url!r} is a peer; its own hub bearer must be given explicitly")
        elif not url.startswith("http://"):
            raise ValueError(f"relay URL {url!r} is not a loopback http URL (a peer relay takes an https URL)")
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        if not relay_token:
            raise ValueError("the relay token is empty")
        if peer_key is not None and not peer_key:
            raise ValueError("the forward key is empty")
        import httpx

        self._httpx = httpx
        self._url = url.rstrip("/")
        self._machine = machine
        self._token = relay_token
        self._bearer = hub_bearer
        self._peer_key = peer_key
        self._timeout_s = timeout_s
        self._llm_timeout_s = llm_timeout_s
        self._connect_timeout_s = connect_timeout_s if connect_timeout_s is not None else (
            PEER_CONNECT_TIMEOUT_S if peer else None)
        # verify defaults to True (system trust roots); no redirects, no proxy variables, for every URL
        self._http = httpx.Client(transport=transport, trust_env=False, follow_redirects=False)

    @classmethod
    def local(cls, config: RelayConfig, *, hub: bool = False, **kw: Any) -> "HttpRelayClient":
        """A client for this machine's relay. ``hub=True`` also reads the hub bearer (for hub-origin calls)."""
        host, port = config.bind("relay")
        bearer = read_token(config.paths.hub_bearer) if hub else None
        return cls(base_url(host, port), machine=config.machine_label,
                   relay_token=read_token(config.paths.relay_token), hub_bearer=bearer, **kw)

    @property
    def machine(self) -> str:
        return self._machine

    @property
    def url(self) -> str:
        return self._url

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HttpRelayClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the one call ------------------------------------------------------------

    def _timeout(self, verb: str, params: Mapping[str, Any]) -> Any:
        if verb == "ask_llm":
            total = self._llm_timeout_s
        elif verb in _LONG_POLL_VERBS:
            wait = params.get("wait_s", 0)
            total = self._timeout_s + (wait if isinstance(wait, (int, float)) else 0)
        else:
            total = self._timeout_s
        if self._connect_timeout_s is None:
            return total
        return self._httpx.Timeout(total, connect=min(total, self._connect_timeout_s))

    def call(self, verb: str, params: Mapping[str, Any], *, origin: str,
             grant: Optional[str] = None, on_behalf_of: Optional[str] = None) -> dict[str, Any]:
        """Send ``verb`` as ``origin`` and return its result; raise ``RelayError`` on an error envelope.

        An error the relay did not answer with (transport, gateway, malformed reply) is a
        :class:`ClientRelayError` saying whether the request may have been sent.
        """
        request_id = "req-" + secrets.token_hex(8)
        envelope = P.make_request(verb, params, origin=origin, request_id=request_id, on_behalf_of=on_behalf_of)
        headers = {"Content-Type": P.CONTENT_TYPE, P.HEADER_RELAY_TOKEN: self._token}
        if grant is not None:
            headers[P.HEADER_GRANT] = grant
        if origin.startswith("claude.ai/") and self._bearer:
            headers[P.HEADER_AUTHORIZATION] = f"{P.BEARER_SCHEME} {self._bearer}"
        if origin.startswith("relay:") and self._peer_key is not None:
            headers[P.HEADER_PEER_KEY] = self._peer_key
        url = self._url + P.HTTP_PATH_TEMPLATE.format(verb=verb)
        body = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        httpx = self._httpx
        try:
            response = self._http.post(url, content=body, headers=headers, timeout=self._timeout(verb, params))
        except httpx.TransportError as exc:
            # Only a connection that was never opened proves the request was not sent.
            sent = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            raise ClientRelayError(
                "machine_unreachable", f"relay {self._machine} at {self._url} did not answer: {type(exc).__name__}: {exc}",
                sent=sent, details={"machine": self._machine},
            ) from exc
        try:
            reply = response.json()
        except ValueError as exc:
            if response.status_code in _GATEWAY_STATUSES:
                raise self._gateway_error(response.status_code) from exc
            raise ClientRelayError("internal", f"relay {self._machine} answered HTTP {response.status_code} with a body that is not JSON",
                                   sent=True) from exc
        if not isinstance(reply, dict) or reply.get("protocol") != P.PROTOCOL:
            if response.status_code in _GATEWAY_STATUSES:
                raise self._gateway_error(response.status_code)
            raise ClientRelayError("internal", f"relay {self._machine} answered HTTP {response.status_code} without a {P.PROTOCOL} envelope",
                                   sent=True)
        if reply.get("id") not in (request_id, None):
            raise ClientRelayError("internal", f"relay {self._machine} answered request {reply.get('id')!r}, not {request_id!r}",
                                   sent=True)
        if reply.get("ok") is True:
            result = reply.get("result")
            if not isinstance(result, dict):
                raise ClientRelayError("internal", f"relay {self._machine} sent an ok response without a result object", sent=True)
            return result
        try:
            error = P.RelayError.from_error_object(reply.get("error") or {})
        except ValueError as exc:
            raise ClientRelayError("internal", f"relay {self._machine} sent a malformed error object: {exc}", sent=True) from exc
        raise error

    def _gateway_error(self, status: int) -> P.RelayError:
        return ClientRelayError(
            "machine_unreachable",
            f"relay {self._machine} at {self._url} is not answering: a gateway answered HTTP {status} without a relay envelope",
            sent=True, details={"machine": self._machine},
        )

    # -- one method per verb (protocol.RelayClient) ------------------------------------

    def register(self, params, *, origin, grant=None):
        return self.call("register", params, origin=origin, grant=grant)

    def heartbeat(self, params, *, origin, grant=None):
        return self.call("heartbeat", params, origin=origin, grant=grant)

    def deregister(self, params, *, origin, grant=None):
        return self.call("deregister", params, origin=origin, grant=grant)

    def poll(self, params, *, origin, grant=None):
        return self.call("poll", params, origin=origin, grant=grant)

    def ack(self, params, *, origin, grant=None):
        return self.call("ack", params, origin=origin, grant=grant)

    def reply(self, params, *, origin, grant=None):
        return self.call("reply", params, origin=origin, grant=grant)

    def list_machines(self, params, *, origin, grant=None):
        return self.call("list_machines", params, origin=origin, grant=grant)

    def list_sessions(self, params, *, origin, grant=None):
        return self.call("list_sessions", params, origin=origin, grant=grant)

    def read_session(self, params, *, origin, grant=None):
        return self.call("read_session", params, origin=origin, grant=grant)

    def ask_llm(self, params, *, origin, grant=None):
        return self.call("ask_llm", params, origin=origin, grant=grant)

    def send_message(self, params, *, origin, grant=None):
        return self.call("send_message", params, origin=origin, grant=grant)

    def get_replies(self, params, *, origin, grant=None):
        return self.call("get_replies", params, origin=origin, grant=grant)

    def list_questions(self, params, *, origin, grant=None):
        return self.call("list_questions", params, origin=origin, grant=grant)

    def answer_question(self, params, *, origin, grant=None):
        return self.call("answer_question", params, origin=origin, grant=grant)

    def grant_mint(self, params, *, origin, grant=None):
        return self.call("grant_mint", params, origin=origin, grant=grant)

    def grant_check(self, params, *, origin, grant=None):
        return self.call("grant_check", params, origin=origin, grant=grant)

    def pause(self, params, *, origin, grant=None):
        return self.call("pause", params, origin=origin, grant=grant)

    def resume(self, params, *, origin, grant=None):
        return self.call("resume", params, origin=origin, grant=grant)

    def status(self, params, *, origin, grant=None):
        return self.call("status", params, origin=origin, grant=grant)


__all__ = ["ClientRelayError", "DEFAULT_LLM_TIMEOUT_S", "DEFAULT_TIMEOUT_S", "HttpRelayClient", "PEER_CONNECT_TIMEOUT_S",
           "base_url"]
