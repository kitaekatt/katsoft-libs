"""``RelayClient`` over HTTP (unit S3): one connection to one relay.

:class:`HttpRelayClient` implements :class:`llm_kit.relay.protocol.RelayClient`.
Every verb method sends one ``POST /v1/<verb>`` request envelope with the
relay token (``X-Relay-Token``), the optional grant reference
(``X-Relay-Grant``) and, for a hub-origin call, the hub bearer
(``Authorization: Bearer``), and returns the result or raises
:class:`llm_kit.relay.protocol.RelayError` with the error envelope's code. A
transport failure (refused connection, timeout) is ``machine_unreachable``.

The URL must be a loopback ``http`` URL: the relay token is this machine's and
is never sent elsewhere (cross-machine reach is a later phase). Proxy
variables in the environment are ignored for the same reason.

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


def base_url(host: str, port: int) -> str:
    """``http://host:port`` with an IPv6 host bracketed."""
    address = host.strip().strip("[]")
    return f"http://[{address}]:{port}" if ":" in address else f"http://{address}:{port}"


class HttpRelayClient:
    """A :class:`~llm_kit.relay.protocol.RelayClient` for one relay over loopback HTTP.

    ``machine`` is the relay's machine label (reported by :attr:`machine` and
    named in ``machine_unreachable``). ``hub_bearer`` is sent only on
    hub-origin calls. ``transport`` is an optional ``httpx`` transport.
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
    ) -> None:
        if not is_loopback_url(url) or not url.startswith("http://"):
            raise ValueError(f"relay URL {url!r} is not a loopback http URL; the relay token never leaves this machine")
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        if not relay_token:
            raise ValueError("the relay token is empty")
        import httpx

        self._httpx = httpx
        self._url = url.rstrip("/")
        self._machine = machine
        self._token = relay_token
        self._bearer = hub_bearer
        self._timeout_s = timeout_s
        self._llm_timeout_s = llm_timeout_s
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

    def _timeout(self, verb: str, params: Mapping[str, Any]) -> float:
        if verb == "ask_llm":
            return self._llm_timeout_s
        if verb in _LONG_POLL_VERBS:
            wait = params.get("wait_s", 0)
            return self._timeout_s + (wait if isinstance(wait, (int, float)) else 0)
        return self._timeout_s

    def call(self, verb: str, params: Mapping[str, Any], *, origin: str,
             grant: Optional[str] = None) -> dict[str, Any]:
        """Send ``verb`` as ``origin`` and return its result; raise ``RelayError`` on an error envelope."""
        request_id = "req-" + secrets.token_hex(8)
        envelope = P.make_request(verb, params, origin=origin, request_id=request_id)
        headers = {"Content-Type": P.CONTENT_TYPE, P.HEADER_RELAY_TOKEN: self._token}
        if grant is not None:
            headers[P.HEADER_GRANT] = grant
        if origin.startswith("claude.ai/") and self._bearer:
            headers[P.HEADER_AUTHORIZATION] = f"{P.BEARER_SCHEME} {self._bearer}"
        url = self._url + P.HTTP_PATH_TEMPLATE.format(verb=verb)
        body = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        try:
            response = self._http.post(url, content=body, headers=headers, timeout=self._timeout(verb, params))
        except self._httpx.TransportError as exc:
            raise P.RelayError(
                "machine_unreachable", f"relay {self._machine} at {self._url} did not answer: {type(exc).__name__}: {exc}",
                details={"machine": self._machine},
            ) from exc
        try:
            reply = response.json()
        except ValueError as exc:
            raise P.RelayError("internal", f"relay {self._machine} answered HTTP {response.status_code} with a body that is not JSON") from exc
        if not isinstance(reply, dict) or reply.get("protocol") != P.PROTOCOL:
            raise P.RelayError("internal", f"relay {self._machine} answered HTTP {response.status_code} without a {P.PROTOCOL} envelope")
        if reply.get("id") not in (request_id, None):
            raise P.RelayError("internal", f"relay {self._machine} answered request {reply.get('id')!r}, not {request_id!r}")
        if reply.get("ok") is True:
            result = reply.get("result")
            if not isinstance(result, dict):
                raise P.RelayError("internal", f"relay {self._machine} sent an ok response without a result object")
            return result
        try:
            raise P.RelayError.from_error_object(reply.get("error") or {})
        except ValueError as exc:
            raise P.RelayError("internal", f"relay {self._machine} sent a malformed error object: {exc}") from exc

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


__all__ = ["DEFAULT_LLM_TIMEOUT_S", "DEFAULT_TIMEOUT_S", "HttpRelayClient", "base_url"]
