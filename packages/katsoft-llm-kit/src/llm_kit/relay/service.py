"""Relay dispatch (unit S3): the relay-protocol/1 checks and verbs, independent of the transport.

:class:`RelayService` takes one request envelope plus the credentials its
transport carried and returns ``(http_status, response_envelope)``. The HTTP
server (:mod:`llm_kit.relay.server`) is one transport; the hub can hand the
same envelopes to a service in process. The wire contract is
``docs/reference/llm-kit-relay-protocol.md``; section 20 of that document
records the decisions this module makes where the contract leaves room.

Check order (protocol section 6), each stage refusing with the first failing
code and no effect:

1. envelope: UTF-8 JSON object, at most ``MAX_REQUEST_BYTES``, the protocol
   literal, a well-formed request id and origin identity;
2. auth: ``X-Relay-Token`` on every request (constant time); a hub-origin
   request also needs the hub bearer (``Authorization: Bearer``);
3. verb: known, equal to the path verb, and served by this build;
4. params: the envelope and ``<verb>.request`` JSON Schemas;
5. origin: the verb's origin kinds, this machine only, and a channel origin
   that names the harness and the session in its params;
6. pause: a hub-origin call while the hub is paused, and ``send_message`` while
   the relay is paused (``poll`` returns empty instead, in its own handler);
7. grant: a covering grant for the origin kinds the verb table names (the
   ``direct`` origin; a hub call needs none);
8. the verb's own refusals. A hub ``send_message`` is accepted only for a
   session that opted in through a channel (``not_opted_in`` and
   ``session_ended`` otherwise, as for every origin); a hub
   ``answer_question`` answers any question, with the same refusals a
   ``/direct`` answer meets (owner rulings 2026-11-11, protocol section 3).

Forwarding (amendment 2, protocol sections 3 and 20 "Forwarding"). A relay
with forwarding peers (``peers``: machine -> client carrying that peer's
peer key) sends a ``/direct`` session's read, llm and write calls that name
a peer to that peer, as origin ``relay:<this machine>`` with ``on_behalf_of``
set to the session, and fans ``list_machines``, ``list_sessions`` and
``list_questions`` out to every peer. The order is fixed: the local grant
check (no I/O), the cached capability probe (``status.forwarding.receive``),
reserving one grant use, then the forwarded requests. A use is refunded only
on a certain pre-effect outcome.

Principals (protocol section 3). The credential a request presents, never the
origin it claims, decides who is calling: the relay token is the local
principal; a peer key this relay issued to ``<M>`` (``accept_keys``,
``X-Relay-Peer-Key``, never together with the relay token) is the principal
``peer:<M>``, admitted only for ``<M>`` in ``accept_from``. ``peer:<M>`` may
claim only ``relay:<M>`` (``on_behalf_of`` a ``direct:<M>/...`` session) or a
hub origin (``claude.ai/...``, the hub on ``<M>``; no hub bearer); a
``relay:`` origin needs ``peer:<M>``. Neither origin is ever forwarded, so a
peer's call is answered from this machine only (one hop).

A grant use is reserved before the effect, so two concurrent calls cannot
share one remaining use: ``send_message`` consumes in the mailbox transaction
that inserts the message; every other granted verb reserves its use
(:meth:`Mailbox.consume_grant`) before its handler runs and gives it back
(:meth:`Mailbox.refund_grant`) only when the handler failed before any effect
(:func:`failed_before_effect`). Every result is validated against its
``<verb>.response`` schema before it is returned; a result that does not
conform is an ``internal`` error. Every request whose origin and verb are known
gets one ``call`` audit row.

Security posture (alternative A, owner ruling 2026-11-03): the relay token,
the hub bearer, origins and grants are a guardrail against accidental calls.
A same-user process can read the token and the bearer, claim any origin and
mint a grant for its own session. None of it is a security boundary.
"""
from __future__ import annotations

import concurrent.futures
import hmac
import importlib.metadata
import json
import secrets
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from llm_kit.relay import protocol as P
from llm_kit.relay import routing
from llm_kit.relay.client import ClientRelayError
from llm_kit.relay.discovery import Discovery, EndEvent, RegistrationTable, SessionInfo
from llm_kit.relay.lifecycle import PauseStore, token_matches
from llm_kit.relay.mailbox import STORED_REFUSALS, GrantUse, Mailbox
from llm_kit.relay.questions import QuestionService
from llm_kit.schema import json_schema as JS

#: Verbs the frozen contract defines that this build does not serve. They are
#: refused ``unknown_verb`` with ``details.not_implemented`` true. Every verb of
#: relay-protocol/1 is served, so the set is empty; a verb added to the verb
#: table before its handler exists is listed here until the handler lands.
NOT_SERVED: frozenset[str] = frozenset()

#: Verbs whose target param names a session (a selector there is resolved).
_SESSION_TARGET_PARAMS = frozenset({("read_session", "address"), ("send_message", "to"), ("get_replies", "address")})

#: Mutating verbs that may change what a waiting poll or get_replies sees.
_WAKES = frozenset({"send_message", "ack", "reply", "poll", "deregister", "heartbeat", "register"})

#: Default and cap of get_replies.limit.
DEFAULT_REPLIES_LIMIT = 50

_ERROR_OUTCOMES = frozenset({"internal", "upstream_unavailable", "machine_unreachable"})
_MAX_SCHEMA_ERRORS = 20

#: Failed-authentication audit bounds (protocol section 20, "Audit"). At most
#: one ``unauthenticated`` call row per origin per window (the requests it stands for are
#: counted in one closing row), at most ``AUTH_AUDIT_MAX_ROWS_PER_WINDOW`` such rows per window
#: over all origins, and at most ``AUTH_AUDIT_MAX_TOTAL_ROWS`` in the whole audit table. Audit
#: rows are never deleted, so the total cap is a stop, not a rotation.
AUTH_AUDIT_WINDOW_S = 60.0
AUTH_AUDIT_MAX_ROWS_PER_WINDOW = 60
AUTH_AUDIT_MAX_TOTAL_ROWS = 10_000

#: Error codes of a handler that ran an effect: a use reserved for such a call stays spent.
_AFTER_EFFECT_CODES = frozenset({"too_large", "internal"})

#: Seconds a peer's capability probe answer (``status.forwarding``) is reused; a transport failure is never cached.
PEER_PROBE_TTL_S = 60.0
#: The fan-out verbs (protocol section 4a of the forwarding plan): local plus every forwarding peer.
FAN_OUT_VERBS = frozenset({"list_machines", "list_sessions", "list_questions"})


def failed_before_effect(exc: BaseException) -> bool:
    """Whether a handler failure proves the operation had no effect, so its reserved grant use is refunded.

    Only a :class:`~llm_kit.relay.protocol.RelayError` can; any other exception
    is a defect after which the effect is unknown. ``upstream_unavailable``
    counts only when its cause chain shows the connection was never made
    (``ConnectionRefusedError`` or a name-resolution failure): a timeout or a
    dropped connection may have followed a request the endpoint processed.
    ``too_large`` and ``internal`` come from a handler that already ran.
    """
    if not isinstance(exc, P.RelayError):
        return False
    if exc.code == "upstream_unavailable":
        seen: set[int] = set()
        cause: Optional[BaseException] = exc.__cause__ or exc.__context__
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, (ConnectionRefusedError, socket.gaierror)):
                return True
            seen.add(id(cause))
            cause = cause.__cause__ or cause.__context__
        return False
    return exc.code not in _AFTER_EFFECT_CODES


@dataclass
class _AuthSummary:
    origin: str
    verb: str
    suppressed: int
    overflow: bool


class _AuthAuditLimiter:
    """Decides which ``unauthenticated`` requests get an audit row (bounds in the constants above)."""

    def __init__(self, machine: str) -> None:
        self._machine = machine
        self._lock = threading.Lock()
        self._window_start: Optional[float] = None
        self._rows = 0
        self._seen: dict[str, list[Any]] = {}  # origin -> [verb, suppressed]
        self._overflow = 0
        self._overflow_verb: Optional[str] = None
        self._total: Optional[int] = None
        self._capped_reported = False

    def _roll(self, now: float) -> list[_AuthSummary]:
        if self._window_start is None:
            self._window_start = now
            return []
        if 0 <= now - self._window_start < AUTH_AUDIT_WINDOW_S:
            return []
        summaries = [_AuthSummary(origin, entry[0], entry[1], False) for origin, entry in self._seen.items() if entry[1]]
        if self._overflow and self._overflow_verb is not None:
            summaries.append(_AuthSummary(f"relay:{self._machine}", self._overflow_verb, self._overflow, True))
        room = max(0, AUTH_AUDIT_MAX_TOTAL_ROWS - (self._total or 0))
        summaries = summaries[:room]
        self._total = (self._total or 0) + len(summaries)
        self._window_start, self._rows = now, 0
        self._seen.clear()
        self._overflow, self._overflow_verb = 0, None
        return summaries

    def admit(self, now: float, origin: str, verb: str, load_total: Callable[[], int]) -> tuple[bool, list[_AuthSummary]]:
        """``(write this request's row, closing rows now due)``."""
        with self._lock:
            if self._total is None:
                self._total = load_total()
            summaries = self._roll(now)
            entry = self._seen.get(origin)
            if entry is not None:
                entry[1] += 1
                return False, summaries
            if self._rows >= AUTH_AUDIT_MAX_ROWS_PER_WINDOW or self._total >= AUTH_AUDIT_MAX_TOTAL_ROWS:
                if self._total >= AUTH_AUDIT_MAX_TOTAL_ROWS and not self._capped_reported:
                    self._capped_reported = True
                    print(f"llm-kit relay: the audit table holds {self._total} unauthenticated call rows, the cap; "
                          "further failed-authentication requests are not recorded", file=sys.stderr)
                self._overflow += 1
                self._overflow_verb = self._overflow_verb or verb
                return False, summaries
            self._seen[origin] = [verb, 0]
            self._rows += 1
            self._total += 1
            return True, summaries

    def due(self, now: float) -> list[_AuthSummary]:
        """Closing rows for a window that has ended with no later failed request to close it."""
        with self._lock:
            if self._window_start is None or self._total is None:
                return []
            return self._roll(now)


def _bad(message: str, **details: Any) -> P.RelayError:
    return P.RelayError("bad_request", message, details=details or None)


def _same(a: str, b: str) -> bool:
    """Constant-time equality of two strings (any characters)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def library_version() -> Optional[str]:
    """The installed katsoft-llm-kit version; None in a source run with no distribution metadata."""
    try:
        return importlib.metadata.version("katsoft-llm-kit")
    except importlib.metadata.PackageNotFoundError:
        return None


class _Schemas:
    """The composed, checked relay schemas, loaded once each."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> dict[str, Any]:
        with self._lock:
            schema = self._cache.get(name)
            if schema is None:
                schema = P.load_schema(name)
                JS.check_schema(schema)
                self._cache[name] = schema
            return schema

    def errors(self, name: str, value: Any) -> list[list[str]]:
        return [list(e) for e in JS.validate(self.get(name), value)][:_MAX_SCHEMA_ERRORS]


@dataclass
class _Call:
    """What one request carries through the stages (also read by the end listener)."""

    verb: str
    origin: P.Identity
    ended: dict[str, int] = field(default_factory=dict)
    #: The direct session a relay origin acts for (amendment 2); None for every other origin.
    on_behalf_of: Optional[P.Identity] = None
    #: What the call row's ``detail`` records for an ok forwarded call.
    note: Optional[str] = None


@dataclass(frozen=True)
class _Granted:
    """The grant stage's outcome: the resolved target and the grant use to consume."""

    target: Optional[str]
    use: Optional[GrantUse]


class _PeerLink:
    """One forwarding peer: its client (None when no peer key is stored) and the cached capability probe."""

    def __init__(self, machine: str, client: Any) -> None:
        self.machine = machine
        self.client = client
        self.lock = threading.Lock()
        self.probed: Optional[tuple[float, Optional[P.RelayError]]] = None


@dataclass(frozen=True)
class _Plan:
    """Where a ``/direct`` call goes: ``single`` (one peer), ``fanout`` or ``message_id`` (probe every relay)."""

    kind: str
    machine: Optional[str] = None


class RelayService:
    """One machine's relay: the verb handlers over the mailbox, registrations, discovery and LLM service.

    ``table`` must be the registration table ``discovery`` merges; the service
    subscribes the mailbox's ``end_session`` to its end events. ``endpoints``
    are the loopback registry entry names ``list_machines`` advertises.
    ``ledgers`` are the watched job-ledger paths (``RelayConfig.ledgers``) the
    questions verbs read and answer; the service builds one
    :class:`~llm_kit.relay.questions.QuestionService` over them, with this
    relay's clock and reachability. ``poll_interval_s`` bounds how long a waiting ``poll`` or ``get_replies``
    sleeps between mailbox reads when no local write wakes it (a write by
    another process is seen within that interval).
    """

    def __init__(
        self,
        *,
        machine: str,
        relay_token: str,
        hub_bearer: str,
        mailbox: Mailbox,
        table: RegistrationTable,
        discovery: Discovery,
        llm: Any,
        pause: PauseStore,
        endpoints: Sequence[str] = (),
        ledgers: Sequence[Path] = (),
        clock: Callable[[], float] = time.time,
        poll_interval_s: float = 1.0,
        version: Optional[str] = None,
        peers: Optional[Mapping[str, Any]] = None,
        accept_from: Sequence[str] = (),
        accept_keys: Optional[Mapping[str, str]] = None,
        probe_ttl_s: float = PEER_PROBE_TTL_S,
    ) -> None:
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        if not relay_token or not hub_bearer:
            raise ValueError("the relay token and the hub bearer must be non-empty")
        if discovery.table is not table:
            raise ValueError("discovery must merge the same registration table the service serves")
        for name in list(peers or {}) + list(accept_from) + list(accept_keys or {}):
            if not P.is_machine_label(name) or name == machine:
                raise ValueError(f"{name!r} is not another machine's label")
        keys = list((accept_keys or {}).values())
        if len(set(keys)) != len(keys) or relay_token in keys or hub_bearer in keys:
            raise ValueError("every peer key must be distinct, and distinct from the relay token and the hub bearer")
        for name, peer in (peers or {}).items():
            if peer is not None and peer.machine != name:
                raise ValueError(f"the forwarding client for {name!r} names machine {peer.machine!r}")
        self.machine = machine
        self._relay_token = relay_token
        self._hub_bearer = hub_bearer
        self._mailbox = mailbox
        self._table = table
        self._discovery = discovery
        self._llm = llm
        self._pause = pause
        self._endpoints = tuple(endpoints)
        self._clock = clock
        self._questions = QuestionService(machine=machine, ledgers=ledgers, clock=clock, reach=self._reach)
        self._poll_interval_s = poll_interval_s
        self._version = version if version is not None else library_version()
        self._schemas = _Schemas()
        self._auth_audit = _AuthAuditLimiter(machine)
        self._peers: dict[str, _PeerLink] = {name: _PeerLink(name, peer) for name, peer in (peers or {}).items()}
        self._known = frozenset({machine, *self._peers})
        self._accept_from = frozenset(accept_from)
        self._accept_keys = dict(accept_keys or {})
        self._probe_ttl_s = probe_ttl_s
        self._fan = (concurrent.futures.ThreadPoolExecutor(max_workers=max(4, 2 * len(self._peers)),
                                                           thread_name_prefix="llm-kit-relay-fan")
                     if self._peers else None)
        self._local = threading.local()
        self._wake = threading.Condition()
        self._generation = 0
        self._closing = threading.Event()
        self._handlers: dict[str, Callable[[_Call, Mapping[str, Any], _Granted, str], dict[str, Any]]] = {
            "register": self._register,
            "heartbeat": self._heartbeat,
            "deregister": self._deregister,
            "poll": self._poll,
            "ack": self._ack,
            "reply": self._reply,
            "list_machines": self._list_machines,
            "list_sessions": self._list_sessions,
            "read_session": self._read_session,
            "ask_llm": self._ask_llm,
            "send_message": self._send_message,
            "get_replies": self._get_replies,
            "list_questions": self._list_questions,
            "answer_question": self._answer_question,
            "grant_mint": self._grant_mint,
            "grant_check": self._grant_check,
            "pause": self._pause_verb,
            "resume": self._resume_verb,
            "status": self._status,
        }
        missing = set(P.VERBS) - set(self._handlers) - NOT_SERVED
        if missing:
            raise AssertionError(f"relay verbs without a handler: {sorted(missing)}")
        table.add_end_listener(self._on_end)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Wake every waiting poll so it returns at once (the server is stopping)."""
        self._closing.set()
        self._notify()
        if self._fan is not None:
            self._fan.shutdown(wait=False, cancel_futures=True)

    @property
    def forwarding_peers(self) -> tuple[str, ...]:
        """The peers this relay forwards ``/direct`` calls to (amendment 2), in configuration order."""
        return tuple(self._peers)

    def maintain(self) -> None:
        """End lapsed registrations and lapse expired messages (the server calls this periodically)."""
        self._table.expire_lapsed()
        if self._mailbox.expire():
            self._notify()
        self._write_auth_summaries(self._auth_audit.due(self._clock()), "relay")

    def _notify(self) -> None:
        with self._wake:
            self._generation += 1
            self._wake.notify_all()

    def _seen(self) -> int:
        """The wake generation; read BEFORE looking at the mailbox, so a wake in between is not lost."""
        with self._wake:
            return self._generation

    def _wait(self, seconds: float, seen: int) -> None:
        with self._wake:
            if self._generation == seen:
                self._wake.wait(seconds)

    # -- the end listener ------------------------------------------------------

    def _on_end(self, event: EndEvent) -> None:
        """A registration ended: its unfinished messages become session-ended (protocol section 9)."""
        call: Optional[_Call] = getattr(self._local, "call", None)
        origin: Optional[P.Identity] = None
        if event.reason == "lease_lapsed":
            verb = "expire"
        else:
            verb = "deregister" if event.reason == "deregistered" else "heartbeat"
            if call is not None and call.verb in ("register", "heartbeat", "deregister"):
                verb = call.verb
                origin = call.origin
        moved = self._mailbox.end_session(event.address, reason=event.reason, verb=verb, origin=origin)
        if call is not None:
            call.ended[event.address] = call.ended.get(event.address, 0) + moved
        if moved:
            self._notify()

    # -- the entry point -------------------------------------------------------

    def handle(
        self,
        body: Union[bytes, bytearray, Mapping[str, Any]],
        *,
        path_verb: Optional[str] = None,
        relay_token: Optional[str] = None,
        hub_bearer: Optional[str] = None,
        grant: Optional[str] = None,
        ingress: str = "relay",
        peer_key: Optional[str] = None,
    ) -> tuple[int, dict[str, Any]]:
        """Answer one request; returns ``(http_status, response_envelope)``. Never raises a RelayError.

        ``body`` is the raw request bytes (HTTP) or an already parsed envelope
        (in process). ``relay_token``, ``hub_bearer``, ``grant`` and ``peer_key``
        are what the transport carried as ``X-Relay-Token``, the
        ``Authorization`` bearer, ``X-Relay-Grant`` and ``X-Relay-Peer-Key``.
        ``path_verb`` is the verb the transport addressed (``/v1/<verb>``),
        which must equal the envelope's.
        """
        request_id: Optional[str] = None
        identity: Optional[P.Identity] = None
        delegate: Optional[P.Identity] = None
        verb: Optional[str] = None
        target: Optional[str] = None
        granted: Optional[_Granted] = None
        call: Optional[_Call] = None
        try:
            envelope = self._parse(body)
            request_id = envelope["id"]
            identity = P.parse_identity(envelope["origin"])
            body_verb = envelope["verb"]
            if body_verb in P.VERBS:
                verb = body_verb  # known: the call row can be written whatever happens next
            if "on_behalf_of" in envelope and identity.kind != "relay":
                raise _bad("on_behalf_of is valid only with a relay origin; a client never supplies its own delegation")
            principal = self._authenticate(identity, relay_token, hub_bearer, peer_key)
            spec = self._verb(body_verb, path_verb)
            params = self._validate_request(envelope)
            target = self._target_param(spec, params)
            delegate = self._check_origin(spec, identity, params, envelope.get("on_behalf_of"), principal)
            self._check_pause(spec, identity)
            call = _Call(spec.name, identity, on_behalf_of=delegate)
            self._local.call = call
            try:
                granted = self._grant_stage(spec, identity, params, grant)
                target = granted.target if granted.target is not None else target
                plan = self._forward_plan(spec, identity, params, granted)
                if plan is not None:
                    result = self._forward(call, spec, params, granted, plan, ingress)
                else:
                    reserved: Optional[str] = None
                    if granted.use is not None and spec.name != "send_message":
                        # Reserve the use before the effect: of two concurrent calls on the last use, one is refused here.
                        reserved = self._reserve(spec, granted)
                    try:
                        result = self._handlers[spec.name](call, params, granted, ingress)
                    except BaseException as failure:
                        if reserved is not None and failed_before_effect(failure):
                            self._mailbox.refund_grant(reserved)
                        raise
            finally:
                self._local.call = None
            if spec.name in _WAKES:
                self._notify()
            self._validate_result(spec.name, result)
            status, response, outcome, code, detail = 200, P.make_ok(request_id, result), "ok", None, call.note
        except P.RelayError as exc:
            status, response = exc.http_status, P.make_error(request_id, exc)
            outcome = "error" if exc.code in _ERROR_OUTCOMES else "refused"
            code, detail = exc.code, exc.message
        except Exception as exc:  # noqa: BLE001 -- a relay defect: logged in full, answered as internal
            traceback.print_exc(file=sys.stderr)
            error = P.RelayError("internal", f"relay defect: {type(exc).__name__}: {str(exc)[:500]}")
            status, response = error.http_status, P.make_error(request_id, error)
            outcome, code, detail = "error", "internal", error.message
        if identity is not None and verb is not None:
            try:
                if code != "unauthenticated" or self._admit_auth_failure(identity, verb, ingress):
                    self._mailbox.append_audit(
                        event="call", origin=identity, verb=verb, outcome=outcome, ingress=ingress,
                        request_id=request_id, target=target[:1100] if isinstance(target, str) else None,
                        grant_id=granted.use.grant_id if granted is not None and granted.use is not None else None,
                        code=code, detail=detail, on_behalf_of=delegate,
                    )
            except Exception as exc:  # noqa: BLE001 -- an unaudited call is a relay defect, answered as internal
                traceback.print_exc(file=sys.stderr)
                error = P.RelayError("internal", f"relay defect: the call audit row was not written: {type(exc).__name__}")
                status, response = error.http_status, P.make_error(request_id, error)
        return status, response

    def _admit_auth_failure(self, identity: P.Identity, verb: str, ingress: str) -> bool:
        """Whether this failed-authentication request gets its own row; closes due windows as a side effect."""
        write, due = self._auth_audit.admit(
            self._clock(), str(identity), verb,
            lambda: len(self._mailbox.audit_rows(code="unauthenticated", limit=AUTH_AUDIT_MAX_TOTAL_ROWS)),
        )
        self._write_auth_summaries(due, ingress)
        return write

    def _write_auth_summaries(self, summaries: Sequence[_AuthSummary], ingress: str) -> None:
        for item in summaries:
            what = ("unauthenticated requests from further origins" if item.overflow
                    else "further unauthenticated requests from this origin")
            self._mailbox.append_audit(
                event="call", origin=item.origin, verb=item.verb, outcome="refused", ingress=ingress,
                code="unauthenticated",
                detail=f"{item.suppressed} {what} in the last {AUTH_AUDIT_WINDOW_S:g} s window were not recorded individually",
            )

    def relay_token_ok(self, presented: Optional[str]) -> bool:
        """Whether ``presented`` is the relay token (constant time); the transport asks before it reads a body."""
        return token_matches(presented, self._relay_token)

    def peer_principal(self, presented: Optional[str]) -> Optional[str]:
        """The machine whose peer key ``presented`` is (principal ``peer:<M>``), or None; constant time per key."""
        if not presented:
            return None
        found = None
        for machine, key in self._accept_keys.items():
            if token_matches(presented, key):
                found = machine
        return found

    def transport_error(self, code: str, message: str, **details: Any) -> tuple[int, dict[str, Any]]:
        """``(status, envelope)`` for a refusal the transport makes before a body is read."""
        error = P.RelayError(code, message, details=details or None)
        return error.http_status, P.make_error(None, error)

    # -- stages ---------------------------------------------------------------

    def _parse(self, body: Union[bytes, bytearray, Mapping[str, Any]]) -> dict[str, Any]:
        if isinstance(body, (bytes, bytearray)):
            if len(body) > P.MAX_REQUEST_BYTES:
                raise P.RelayError(
                    "too_large", f"the request is over {P.MAX_REQUEST_BYTES} bytes",
                    details={"limit": P.MAX_REQUEST_BYTES, "what": "request"},
                )
            try:
                envelope = json.loads(bytes(body).decode("utf-8"), parse_constant=_reject_constant)
            except (UnicodeDecodeError, ValueError) as exc:
                raise _bad(f"the request body is not UTF-8 JSON: {exc}") from exc
        elif isinstance(body, Mapping):
            envelope = dict(body)
        else:
            raise _bad("the request is not a JSON object")
        if not isinstance(envelope, dict):
            raise _bad("the request envelope is not a JSON object")
        if "protocol" not in envelope:
            raise _bad("the request envelope has no protocol")
        if envelope["protocol"] != P.PROTOCOL:
            raise P.RelayError(
                "unsupported_protocol", f"protocol {envelope['protocol']!r} is not {P.PROTOCOL}",
                details={"supported": [P.PROTOCOL]},
            )
        if not P.is_valid_id("request", envelope.get("id")):
            raise _bad("the request id is missing or not [A-Za-z0-9._:-]{1,128}")
        if not isinstance(envelope.get("origin"), str):
            raise _bad("the request has no origin identity")
        if not isinstance(envelope.get("verb"), str):
            raise _bad("the request has no verb")
        if not isinstance(envelope.get("params"), dict):
            raise _bad("params must be a JSON object")
        return envelope

    def _authenticate(self, identity: P.Identity, relay_token: Optional[str], hub_bearer: Optional[str],
                      peer_key: Optional[str]) -> Optional[str]:
        """The principal: None for the relay token (local), else the machine ``<M>`` of ``peer:<M>``.

        Chosen by the credential presented, never by the claimed origin. A peer key is the whole credential of
        a peer: it is never accepted together with the relay token, and a hub origin under it needs no bearer.
        """
        if peer_key:
            if relay_token:
                raise P.RelayError("unauthenticated",
                                   f"a request carries {P.HEADER_RELAY_TOKEN} or {P.HEADER_PEER_KEY}, never both")
            principal = self.peer_principal(peer_key)
            if principal is None:
                raise P.RelayError("unauthenticated", f"{P.HEADER_PEER_KEY} is not a peer key this relay issued")
            return principal
        if not token_matches(relay_token, self._relay_token):
            raise P.RelayError("unauthenticated", f"missing or wrong {P.HEADER_RELAY_TOKEN}")
        if identity.kind == "hub" and not token_matches(hub_bearer, self._hub_bearer):
            raise P.RelayError("unauthenticated", "a hub-origin request needs the hub bearer (Authorization: Bearer)")
        if identity.kind == "relay":
            raise P.RelayError(
                "unauthenticated",
                f"a relay origin is authenticated only by the {P.HEADER_PEER_KEY} this relay issued to "
                f"{identity.subject!r}, never by the relay token",
            )
        return None

    def _verb(self, body_verb: str, path_verb: Optional[str]) -> P.VerbSpec:
        for name in (path_verb, body_verb):
            if name is not None and name not in P.VERBS:
                raise P.RelayError("unknown_verb", f"no relay verb {name!r}")
        if path_verb is not None and path_verb != body_verb:
            raise _bad(f"the path names verb {path_verb!r} but the envelope names {body_verb!r}")
        if body_verb in NOT_SERVED:
            raise P.RelayError(
                "unknown_verb", f"{body_verb!r} is not served by this relay build",
                details={"verb": body_verb, "not_implemented": True},
            )
        return P.VERBS[body_verb]

    def _validate_request(self, envelope: dict[str, Any]) -> dict[str, Any]:
        errors = self._schemas.errors("envelope.request", envelope)
        if errors:
            raise _bad("the request envelope fails its schema", schema_errors=errors)
        params = envelope["params"]
        errors = self._schemas.errors(P.request_schema_name(envelope["verb"]), params)
        if errors:
            raise _bad(f"{envelope['verb']} params fail their schema", schema_errors=errors)
        return params

    def _validate_result(self, verb: str, result: Any) -> None:
        errors = self._schemas.errors(P.response_schema_name(verb), result)
        if errors:
            print(f"llm-kit relay: {verb} result fails its schema: {errors}", file=sys.stderr)
            raise P.RelayError("internal", f"relay defect: the {verb} result fails its schema",
                               details={"schema_errors": errors})

    @staticmethod
    def _target_param(spec: P.VerbSpec, params: Mapping[str, Any]) -> Optional[str]:
        for name in spec.target_params:
            if name in params:
                return params[name]
        return None

    def _check_origin(self, spec: P.VerbSpec, identity: P.Identity, params: Mapping[str, Any],
                      on_behalf_of: Optional[str] = None, principal: Optional[str] = None) -> Optional[P.Identity]:
        """Refuse an origin the verb, this machine or the principal does not allow; returns a relay origin's delegate."""
        if principal is not None:
            if principal == self.machine or principal not in self._accept_from:
                raise P.RelayError(
                    "origin_not_allowed",
                    f"this relay accepts no calls from peer {principal!r} (relay.accept_from)",
                )
            if not (identity.kind == "hub" or (identity.kind == "relay" and identity.subject == principal)):
                raise P.RelayError(
                    "origin_not_allowed",
                    f"a peer key of {principal!r} may claim only relay:{principal} or a hub origin, not {identity}",
                )
        if identity.kind not in spec.origins:
            raise P.RelayError(
                "origin_not_allowed", f"a {identity.kind} origin may not call {spec.name}",
                details={"origins": sorted(spec.origins)},
            )
        if identity.kind == "relay":
            delegate = P.delegated_identity(identity, on_behalf_of)
            if delegate is None and spec.name != "status":
                raise P.RelayError("origin_not_allowed",
                                   f"a relay origin calls {spec.name} only on behalf of a direct session (on_behalf_of)")
            return delegate
        if identity.kind in ("channel", "direct"):
            machine = identity.subject.split("/", 1)[0]
        elif identity.kind == "operator":
            machine = identity.subject
        else:
            machine = None
        if machine is not None and machine != self.machine:
            raise P.RelayError("origin_not_allowed", f"origin {identity} names machine {machine!r}; this relay is {self.machine!r}")
        if identity.kind == "channel":
            session = identity.subject.split("/", 2)[2]
            allowed = {params.get("session_id")}
            if spec.name == "heartbeat" and params.get("previous_session_id"):
                allowed.add(params["previous_session_id"])
            if session not in allowed:
                raise P.RelayError("origin_not_allowed", f"origin {identity} does not name the session in the params")
            if spec.name == "register" and identity.subject.split("/", 2)[1] != params.get("harness"):
                raise P.RelayError("origin_not_allowed", f"origin {identity} does not name the harness in the params")
        return None

    def _check_pause(self, spec: P.VerbSpec, identity: P.Identity) -> None:
        if identity.kind in P.HUB_PAUSE_REFUSES_ORIGINS:
            self._pause.require_not_paused("hub")
        if spec.name in P.RELAY_PAUSE_REFUSES:
            self._pause.require_not_paused("relay")

    def _resolve_session_target(self, target: str) -> str:
        """A selector resolved to the one live session address; any other target as given."""
        try:
            parsed = P.parse_target(target)
        except P.RelayError:
            return target
        if isinstance(parsed, P.Selector):
            if parsed.machine is not None and parsed.machine in self._peers:
                raise P.RelayError(
                    "target_invalid",
                    f"{target!r} is a selector on another machine; a selector is resolved here only, so name the "
                    f"session's full address from list_sessions",
                )
            return self._discovery.resolve(target).address(self.machine)
        return target

    def _grant_stage(self, spec: P.VerbSpec, identity: P.Identity, params: Mapping[str, Any],
                     grant: Optional[str]) -> _Granted:
        if identity.kind not in spec.grant_for:
            return _Granted(None, None)
        target = self._target_param(spec, params)
        name = next((n for n in spec.target_params if n in params), None)
        if target is not None and (spec.name, name) in _SESSION_TARGET_PARAMS:
            target = self._resolve_session_target(target)
        session = identity.subject
        check = self._mailbox.check_grant(session=session, verb=spec.name, target=target, grant_id=grant)
        if not check["covered"]:
            code = check["code"] or "grant_required"
            raise P.RelayError(code, f"no grant of {session} covers {spec.name}" + (f" on {target}" if target else ""))
        return _Granted(target, GrantUse(session, check["grant_id"]))

    def _reserve(self, spec: P.VerbSpec, granted: _Granted) -> Optional[str]:
        """Consume one use of the covering grant now, before any effect; the grant id to refund, or None."""
        if granted.use is None:
            return None
        self._mailbox.consume_grant(session=granted.use.session, verb=spec.name, target=granted.target,
                                    grant_id=granted.use.grant_id)
        return granted.use.grant_id

    # -- forwarding (amendment 2) --------------------------------------------------------

    def _forward_plan(self, spec: P.VerbSpec, identity: P.Identity, params: Mapping[str, Any],
                      granted: _Granted) -> Optional[_Plan]:
        """Where a call goes when it is not this machine's alone; None to serve it locally. No I/O.

        Only a ``/direct`` origin is ever forwarded: a channel, operator, hub or relay origin is answered
        here (a hub call naming another machine keeps today's local answer, and a relay origin is never
        forwarded or fanned out again).
        """
        if identity.kind != "direct" or spec.tier not in P.FORWARDED_TIERS or not self._peers:
            return None
        verb = spec.name
        if verb in FAN_OUT_VERBS:
            machine = params.get("machine")
            if machine is None:
                return _Plan("fanout")
            return _Plan("single", machine) if machine in self._peers else None
        if verb == "get_replies" and "message_id" in params:
            return _Plan("message_id")
        machine = self._target_machine(verb, params, granted)
        return _Plan("single", machine) if machine is not None and machine in self._peers else None

    @staticmethod
    def _target_machine(verb: str, params: Mapping[str, Any], granted: _Granted) -> Optional[str]:
        """The machine a single-target call names: an address's, an endpoint's or a question id's."""
        if verb == "answer_question":
            try:
                return routing.question_machine(params["id"])
            except P.RelayError:
                return None
        name = {"read_session": "address", "ask_llm": "endpoint", "send_message": "to", "get_replies": "address"}[verb]
        value = granted.target if granted.target is not None else params.get(name)
        try:
            parsed = P.parse_target(value)
        except P.RelayError:
            return None
        return parsed.machine if isinstance(parsed, P.Address) else None

    def _forward(self, call: _Call, spec: P.VerbSpec, params: Mapping[str, Any], granted: _Granted, plan: _Plan,
                 ingress: str) -> dict[str, Any]:
        if plan.kind == "single":
            assert plan.machine is not None
            call.note = f"forwarded to relay {plan.machine}"
            return self._forward_single(call, spec, params, granted, plan.machine)
        if plan.kind == "fanout":
            call.note = f"fanned out to relays {', '.join(self._peers)}"
            return self._forward_fanout(call, spec, params, granted, ingress)
        call.note = f"probed relays {', '.join(self._peers)}"
        return self._forward_message_id(call, spec, params, granted, ingress)

    def _capability(self, link: _PeerLink) -> Optional[P.RelayError]:
        """None when ``link``'s relay accepts forwarded calls, else the error to report; never sends the call itself.

        The answer (``status.forwarding.receive``, or the probe's refusal) is cached for ``probe_ttl_s``; a
        transport failure is not. A peer without a stored peer key is never probed.
        """
        if link.client is None:
            return P.RelayError(
                "machine_unreachable",
                f"no peer key is stored for relay {link.machine!r}; import one with "
                f"`llm-kit relay peer export --for {self.machine}` on {link.machine}, piped into "
                f"`llm-kit relay peer import {link.machine}` here",
                details={"machine": link.machine, "sent": False},
            )
        with link.lock:  # one probe per peer at a time: concurrent calls share its answer
            now = time.monotonic()
            if link.probed is not None and now - link.probed[0] < self._probe_ttl_s:
                return link.probed[1]
            try:
                status = link.client.call("status", {}, origin=f"relay:{self.machine}")
            except ClientRelayError as exc:
                return P.RelayError(
                    "machine_unreachable", f"relay {link.machine} did not answer the capability probe: {exc.message}",
                    details={"machine": link.machine, "sent": False},
                )
            except P.RelayError as exc:
                answer: Optional[P.RelayError] = P.RelayError(
                    "not_implemented",
                    f"relay {link.machine} does not accept forwarded calls from {self.machine}: {exc.code}",
                    details={"machine": link.machine, "sent": False, "probe": exc.to_error_object()},
                )
            else:
                forwarding = status.get("forwarding")
                if status.get("machine") != link.machine:
                    return P.RelayError(
                        "internal", f"the relay configured as machine {link.machine!r} reports machine "
                        f"{status.get('machine')!r} in status; check its URL and the label in relay.yaml",
                        details={"machine": link.machine, "sent": False},
                    )
                if isinstance(forwarding, Mapping) and forwarding.get("receive") is True:
                    answer = None
                else:
                    answer = P.RelayError(
                        "not_implemented",
                        f"relay {link.machine} does not accept forwarded calls (no status.forwarding.receive; "
                        "katsoft-llm-kit 1.21.0 or later is needed there)",
                        details={"machine": link.machine, "sent": False},
                    )
            link.probed = (now, answer)
            return answer

    def _capabilities(self, links: Sequence[_PeerLink]) -> dict[str, Optional[P.RelayError]]:
        """The capability of every link, probed in parallel."""
        assert self._fan is not None
        return {link.machine: outcome for link, outcome in
                ((link, future.result()) for link, future in
                 [(link, self._fan.submit(self._capability, link)) for link in links])}

    def _call_peer(self, call: _Call, link: _PeerLink, verb: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """One forwarded request as ``relay:<this machine>`` on behalf of the ``/direct`` session; result checked."""
        result = link.client.call(verb, dict(params), origin=f"relay:{self.machine}", on_behalf_of=str(call.origin))
        routing.check_result(verb, result, machine=link.machine, known=self._known)
        if verb == "send_message":
            message = result.get("message") if isinstance(result.get("message"), Mapping) else {}
            stamp = f"relay:{self.machine}"
            if message.get("sender") != str(call.origin) or message.get("forwarded_by") != stamp:
                raise P.RelayError(
                    "internal",
                    f"relay {link.machine} stored the forwarded send as sender {message.get('sender')!r} forwarded_by "
                    f"{message.get('forwarded_by')!r}; expected {str(call.origin)!r} and {stamp!r}",
                    details={"machine": link.machine, "field": "send_message.message.sender"},
                )
        return result

    def _forward_single(self, call: _Call, spec: P.VerbSpec, params: Mapping[str, Any], granted: _Granted,
                        machine: str) -> dict[str, Any]:
        """The call on peer ``machine`` alone: probe, reserve, send; refund only on a certain pre-effect outcome."""
        link = self._peers[machine]
        refusal = self._capability(link)
        if refusal is not None:
            raise refusal  # nothing reserved, nothing sent
        send = spec.name == "send_message"
        params = dict(params)
        if send and "id" not in params:
            params["id"] = "msg_" + secrets.token_hex(12)  # a retry after an unknown outcome then delivers once
        extra = {"message_id": params["id"]} if send else {}
        reserved = self._reserve(spec, granted)
        try:
            result = self._call_peer(call, link, spec.name, params)
        except ClientRelayError as exc:
            if not exc.sent:
                self._refund(reserved)
                raise P.RelayError(
                    "machine_unreachable", f"relay {machine} could not be reached; the call was not sent: {exc.message}",
                    details={"machine": machine, "sent": False, **extra},
                ) from exc
            raise P.RelayError(
                "machine_unreachable",
                f"relay {machine} did not answer after the call was sent, so its outcome is unknown and the grant use "
                f"stays spent: {exc.message}",
                details={"machine": machine, "sent": True, "outcome": "unknown", **extra},
            ) from exc
        except P.RelayError as exc:
            if exc.code != "internal" and (failed_before_effect(exc) or (send and exc.code in STORED_REFUSALS)):
                self._refund(reserved)
            if extra:
                raise P.RelayError(exc.code, exc.message, details={**(exc.details or {}), **extra}) from exc
            raise
        if send and result.get("replayed") is True:
            self._refund(reserved)  # a replayed send consumes nothing, as a local one
        return result

    def _refund(self, grant_id: Optional[str]) -> None:
        if grant_id is not None:
            self._mailbox.refund_grant(grant_id)

    def _forward_fanout(self, call: _Call, spec: P.VerbSpec, params: Mapping[str, Any], granted: _Granted,
                        ingress: str) -> dict[str, Any]:
        """This machine plus every forwarding peer; a peer that cannot answer is listed unreachable."""
        verb = spec.name
        links = list(self._peers.values())
        refusals = self._capabilities(links)
        reserved = self._reserve(spec, granted)
        try:
            local = self._handlers[verb](call, params, granted, ingress)
        except BaseException as failure:
            if reserved is not None and failed_before_effect(failure):
                self._refund(reserved)  # the call failed before any peer request
            raise
        ready = [link for link in links if refusals[link.machine] is None]

        def ask(link: _PeerLink) -> dict[str, Any]:
            forwarded = dict(params)
            if verb != "list_machines":
                forwarded["machine"] = link.machine
            return self._call_peer(call, link, verb, forwarded)

        assert self._fan is not None
        answers = dict((link.machine, outcome) for link, outcome in routing.fan_out(self._fan, ready, ask))
        merged: dict[str, Any] = {key: list(value) for key, value in local.items() if isinstance(value, list)}
        merged.setdefault("machines", [])
        if verb == "list_questions":
            merged["ledgers"] = [{**entry, "machine": self.machine} for entry in local.get("ledgers", [])]
        for link in links:
            outcome = refusals[link.machine] or answers[link.machine]
            if isinstance(outcome, P.RelayError):
                merged["machines"].append({"machine": link.machine, "status": "unreachable",
                                           "error": outcome.to_error_object()})
                continue
            for key, value in outcome.items():
                if key == "ledgers":
                    merged["ledgers"].extend({**entry, "machine": link.machine} for entry in value)
                elif key in merged and isinstance(value, list):
                    merged[key].extend(value)
        if verb == "list_questions":
            merged["questions"].sort(key=lambda q: (q["created_at"], q["id"]))
        return {**local, **merged}

    def _forward_message_id(self, call: _Call, spec: P.VerbSpec, params: Mapping[str, Any], granted: _Granted,
                            ingress: str) -> dict[str, Any]:
        """``get_replies(message_id)``: probe this relay and every peer without ``wait_s``; one may hold it."""
        links = list(self._peers.values())
        refusals = self._capabilities(links)
        reserved = self._reserve(spec, granted)
        probe = {k: v for k, v in params.items() if k != "wait_s"}
        ready = [link for link in links if refusals[link.machine] is None]
        assert self._fan is not None
        peer_outcomes = routing.fan_out(self._fan, ready, lambda link: self._call_peer(call, link, spec.name, probe))
        try:
            local: Any = self._get_replies(call, probe, granted, ingress)
        except P.RelayError as exc:
            local = exc
        outcomes: list[tuple[str, Any]] = [(self.machine, local)] + [(link.machine, o) for link, o in peer_outcomes]
        holders = [(m, o) for m, o in outcomes if not isinstance(o, P.RelayError)]
        uncertain = [m for m, error in refusals.items() if error is not None]
        if not holders:
            certain = not uncertain and all(
                isinstance(o, P.RelayError) and not isinstance(o, ClientRelayError) and o.code == "message_not_found"
                for _, o in outcomes)
            if certain:
                self._refund(reserved)
                raise P.RelayError("message_not_found", f"no relay holds message {params['message_id']}",
                                   details={"machines": [m for m, _ in outcomes]})
            if uncertain:
                raise P.RelayError(
                    "machine_unreachable",
                    f"no reachable relay holds message {params['message_id']}; not asked: {', '.join(uncertain)}",
                    details={"machines": uncertain},
                )
            # a holder may have been missed: report the error that says so, not another relay's message_not_found
            outcomes.sort(key=lambda item: isinstance(item[1], P.RelayError) and item[1].code == "message_not_found")
        machine, result = routing.resolve_one(outcomes, params["message_id"])
        if params.get("wait_s", 0) == 0:
            return result
        if machine == self.machine:
            return self._get_replies(call, params, granted, ingress)
        return self._call_peer(call, self._peers[machine], spec.name, params)

    # -- channel verbs ------------------------------------------------------------

    @staticmethod
    def _harness(call: _Call) -> str:
        """The harness a channel origin names (``channel:<machine>/<harness>/<session>``)."""
        return call.origin.subject.split("/", 2)[1]

    def _registration(self, call: _Call, params: Mapping[str, Any]):
        """The live registration named by the params, without renewing its lease (ack, reply)."""
        registration = self._table.get(params["session_id"], self._harness(call))
        if registration is None or not _same(registration.registration_id, params["registration_id"]):
            raise P.RelayError("registration_unknown", "unknown or lapsed registration; register again")
        if not _same(registration.channel_nonce, params["channel_nonce"]):
            raise P.RelayError("nonce_mismatch", "channel nonce does not match the registration")
        return registration

    def _register(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        outcome = self._table.register(
            session_id=params["session_id"], pid=params["pid"], cwd=params["cwd"], version=params["version"],
            accepts=params["accepts"], channel_nonce=params["channel_nonce"], harness=params["harness"],
            title=params.get("title"), task=params.get("task"), transcript_path=params.get("transcript_path"),
        )
        registration = outcome.registration
        return {
            "registration_id": registration.registration_id,
            "address": self._table.address(registration.session_id, registration.harness),
            "machine": self.machine,
            "heartbeat_s": P.HEARTBEAT_INTERVAL_S,
            "lease_s": int(round(registration.lease_expires_at - registration.last_seen_at)),
            "lease_expires_at": registration.lease_expires_at,
            "ack_timeout_s": self._mailbox.ack_timeout_s,
            "max_wait_s": P.MAX_POLL_WAIT_S,
            "paused": self._pause.is_paused("relay"),
            "replaced": outcome.replaced,
        }

    def _heartbeat(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        outcome = self._table.heartbeat(
            params["registration_id"], params["session_id"], params["channel_nonce"],
            previous_session_id=params.get("previous_session_id"), cwd=params.get("cwd"),
            title=params.get("title"), task=params.get("task"), accepts=params.get("accepts"),
            harness=self._harness(call),
        )
        registration = outcome.registration
        ended = call.ended.get(outcome.old_address, 0) if outcome.old_address else 0
        return {
            "registration_id": registration.registration_id,
            "address": self._table.address(registration.session_id, registration.harness),
            "lease_expires_at": registration.lease_expires_at,
            "paused": self._pause.is_paused("relay"),
            "session_changed": outcome.session_changed,
            "ended_messages": ended,
        }

    def _deregister(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        event = self._table.deregister(params["registration_id"], params["session_id"], params["channel_nonce"],
                                       self._harness(call))
        return {
            "registration_id": event.registration_id,
            "address": event.address,
            "ended_messages": call.ended.get(event.address, 0),
        }

    def _poll(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        registration = self._table.touch(params["registration_id"], params["session_id"], params["channel_nonce"],
                                         self._harness(call))
        address = self._table.address(registration.session_id, registration.harness)
        max_messages = params.get("max_messages", P.MAX_POLL_BATCH)
        deadline = time.monotonic() + params["wait_s"]
        while True:
            seen = self._seen()
            if self._pause.is_paused("relay"):
                return {"messages": [], "paused": True, "lease_expires_at": registration.lease_expires_at}
            messages = self._mailbox.poll(address, max_messages=max_messages)
            remaining = deadline - time.monotonic()
            if messages or remaining <= 0 or self._closing.is_set():
                return {"messages": messages, "paused": False, "lease_expires_at": registration.lease_expires_at}
            self._wait(min(remaining, self._poll_interval_s), seen)

    def _ack(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        registration = self._registration(call, params)
        return dict(self._mailbox.ack(self._table.address(registration.session_id, registration.harness),
                                      params["message_id"]))

    def _reply(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        registration = self._registration(call, params)
        return dict(self._mailbox.reply(
            self._table.address(registration.session_id, registration.harness), params["in_reply_to"], params["state"], params["body"],
            id=params.get("id"),
        ))

    # -- read and llm verbs -----------------------------------------------------------

    def _reach(self) -> dict[str, Any]:
        return {"machine": self.machine, "status": "paused" if self._pause.is_paused("relay") else "up"}

    def _list_machines(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        pause = self._pause.state()
        return {"machines": [{
            "machine": self.machine,
            "status": "paused" if pause["relay"]["paused"] else "up",
            "protocol": P.PROTOCOL,
            "last_contact_at": self._clock(),
            "pause": pause,
            "endpoints": [f"{self.machine}/llm/{name}" for name in self._endpoints],
        }]}

    def _list_sessions(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        machine = params.get("machine")
        if machine is not None and machine != self.machine:
            error = P.RelayError(
                "machine_unreachable",
                f"this relay serves {self.machine!r} only; relay-protocol/1 defines no forwarding to {machine!r}",
            )
            return {"sessions": [], "machines": [{"machine": machine, "status": "unreachable",
                                                   "error": error.to_error_object()}]}
        result = self._discovery.list_sessions(params.get("harness"), params.get("state"))
        return {"sessions": [s.to_session(self.machine) for s in result.sessions], "machines": [self._reach()]}

    def _read_session(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        address = granted.target if granted.target is not None else params["address"]
        return self._discovery.read_session(address, since=params.get("since"),
                                            limit=params.get("limit", P.DEFAULT_READ_TURNS))

    def _ask_llm(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        return self._llm.ask(params)

    # -- messaging --------------------------------------------------------------------

    @staticmethod
    def _refusal(info: SessionInfo, kind: str) -> Optional[str]:
        """The caller-detected refusal for a send to ``info`` (protocol section 13), or None to queue."""
        if info.state in ("idle", "ended"):
            return "session_ended"
        if info.state == "live-accepting" and kind in info.accepts:
            return None
        return "not_opted_in"

    def _send_message(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        target = granted.target if granted.target is not None else params["to"]
        try:
            info = self._discovery.resolve(target)
            address, refuse = info.address(self.machine), self._refusal(info, params["kind"])
        except P.RelayError as exc:
            # A registration that ended (deregistered, lapsed, renamed) is remembered by the
            # table even when no transcript or session file shows the session: session_ended.
            ended = self._ended_address(target) if exc.code == "target_not_found" else None
            if ended is None:
                raise
            address, refuse = ended, "session_ended"
        forwarded = call.origin.kind == "relay"  # authenticated peer (auth stage), its delegate checked (origin stage)
        return dict(self._mailbox.send_message(
            origin=call.on_behalf_of if forwarded else call.origin, to=address, kind=params["kind"], body=params["body"],
            id=params.get("id"), expires_in_s=params.get("expires_in_s"), task=params.get("task"),
            hop=params.get("hop", 0), grant=granted.use, refuse=refuse, ingress=ingress,
            forwarded_by=call.origin if forwarded else None,
        ))

    def _ended_address(self, target: str) -> Optional[str]:
        """``target`` when it is an address of this machine whose registration ended; else None."""
        try:
            parsed = P.parse_target(target)
        except P.RelayError:
            return None
        if not isinstance(parsed, P.Address) or parsed.machine != self.machine or parsed.harness not in P.CHANNEL_HARNESSES:
            return None
        return str(parsed) if self._table.ended_reason(parsed.session, parsed.harness) is not None else None

    def _replies_snapshot(self, params: Mapping[str, Any], address: Optional[str], limit: int) -> list[dict[str, Any]]:
        if "message_id" in params:
            return [dict(self._mailbox.get_message(params["message_id"]))]
        assert address is not None
        return [dict(m) for m in self._mailbox.messages_to(address, limit=limit)]

    def _get_replies(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        limit = params.get("limit", DEFAULT_REPLIES_LIMIT)
        address = None
        if "address" in params:
            address = granted.target if granted.target is not None else self._resolve_session_target(params["address"])
            P.parse_address(address)
        seen = self._seen()
        records = self._replies_snapshot(params, address, limit)
        signature = [(m["id"], m["state"], m["updated_at"]) for m in records]
        deadline = time.monotonic() + params.get("wait_s", 0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._closing.is_set():
                return {"messages": records}
            self._wait(min(remaining, self._poll_interval_s), seen)
            seen = self._seen()
            records = self._replies_snapshot(params, address, limit)
            if [(m["id"], m["state"], m["updated_at"]) for m in records] != signature:
                return {"messages": records}

    # -- questions ----------------------------------------------------------------------

    def _list_questions(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        return self._questions.list_questions(params)

    def _answer_question(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        # A hub answer needs no grant (owner ruling 2026-11-11, "allow answering any question": a message
        # from claude.ai counts as the owner's own words); the hub bearer and the relay token still apply.
        return self._questions.answer_question(params)

    # -- grants -------------------------------------------------------------------------

    def _own_session(self, call: _Call, session: str) -> None:
        if call.origin.subject != session:
            raise P.RelayError("origin_not_allowed", "a /direct session mints and checks grants only for itself")

    def _grant_mint(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        self._own_session(call, params["session"])
        targets = [self._resolve_session_target(t) for t in params["targets"]]
        grant = self._mailbox.mint_grant(
            origin=call.origin, session=params["session"], verb=params["verb"], targets=targets,
            expires_in_s=params["expires_in_s"], uses=params["uses"], origin_proof=params["origin_proof"],
            command=params.get("command"),
        )
        return {"grant": dict(grant)}

    def _grant_check(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        self._own_session(call, params["session"])
        target = params.get("target")
        if target is not None:
            target = self._resolve_session_target(target)
        return dict(self._mailbox.check_grant(
            session=params["session"], verb=params["verb"], target=target, grant_id=params.get("grant_id"),
        ))

    # -- control ------------------------------------------------------------------------

    def _pause_verb(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        return {"pause": self._pause.pause(params["scope"], params.get("reason"))}

    def _resume_verb(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        state = self._pause.resume(params["scope"])
        self._notify()
        return {"pause": state}

    def _status(self, call: _Call, params: Mapping[str, Any], granted: _Granted, ingress: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol": P.PROTOCOL,
            "machine": self.machine,
            "now": self._clock(),
            "pause": self._pause.state(),
            "registrations": len(self._table.active()),
            "harnesses": list(P.CHANNEL_HARNESSES),
            "forwarding": {"receive": True},
        }
        if self._version is not None:
            result["library_version"] = self._version
        return result


__all__ = ["DEFAULT_REPLIES_LIMIT", "NOT_SERVED", "RelayService", "library_version"]
