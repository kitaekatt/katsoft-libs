"""The relay's HTTP server (unit S3): relay-protocol/1 on a loopback socket.

:class:`RelayServer` is one machine's relay process. Constructing or starting it refuses unless this machine's relay.yaml holds
``enabled: true`` (:func:`llm_kit.relay.config.require_enabled`; the file is
read again at start). Starting it:

1. refuses a bind host that is not a literal loopback address (before any
   state is touched, and again on the address the socket actually bound);
2. prepares the state directory, the relay token, the hub bearer and the
   loopback model registry (:func:`llm_kit.relay.lifecycle.prepare_state`);
3. takes the relay role's singleton lock (:class:`RoleLock`); a second relay
   on the same state directory is refused ``already_running``;
4. builds the mailbox, the registration table, session discovery and the
   ``ask_llm`` service, and the transport-independent
   :class:`llm_kit.relay.service.RelayService` over them;
5. binds ``RelayConfig.bind("relay")`` (``127.0.0.1:4710`` by default) and
   serves ``POST /v1/<verb>`` with uvicorn.

The ASGI application here is a minimal callable rather than a framework: it
checks the method, the path and ``X-Relay-Token`` (constant time) BEFORE it
reads anything, then the content type and the declared body size; it takes one
of a bounded number of in-flight slots, reads the body under a deadline,
reads the other two auth headers, and hands the body to the service on a
worker thread (a long poll waits up to 25 seconds there). A maintenance thread ends lapsed
registrations and lapses expired messages every few seconds; if it fails, the
server stops and the failure is raised.

Security posture (alternative A): loopback only, and the relay token is a
guardrail, not a boundary against a same-user process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional

from llm_kit.relay import protocol as P
from llm_kit.relay.client import base_url
from llm_kit.relay.config import (
    RelayConfig,
    RelayConfigError,
    is_loopback_host,
    load_loopback_registry,
    require_enabled,
)
from llm_kit.relay.discovery import Discovery, DiscoveryRoots, ProcessProbe, PsutilProbe, RegistrationTable
from llm_kit.relay.lifecycle import PreparedState, RoleLock, prepare_state
from llm_kit.relay.mailbox import Mailbox, MailboxLimits, mailbox_db_path
from llm_kit.relay.service import RelayService

ROLE = "relay"
PATH_PREFIX = "/v1/"
DEFAULT_MAINTAIN_INTERVAL_S = 5.0
DEFAULT_WORKERS = 32
#: Seconds a request body may take to arrive in full (the body is at most ``MAX_REQUEST_BYTES``).
DEFAULT_BODY_DEADLINE_S = 10.0
#: Requests past the relay token check that may be reading a body or running at once; one more is
#: refused ``rate_limited``. Twice the worker count: a full set of workers plus an equal queue.
DEFAULT_MAX_IN_FLIGHT = 2 * DEFAULT_WORKERS
_STARTUP_TIMEOUT_S = 15.0
_JSON_TYPES = ("application/json",)
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def require_loopback(host: object, *, what: str = "relay") -> str:
    """``host`` when it is a literal loopback address; :class:`RelayConfigError` otherwise.

    The one bind guard of the relay. It runs on the configured host before any
    state is created and on the address the socket bound to.
    """
    if not is_loopback_host(host):
        raise RelayConfigError(f"{what} bind host {host!r} is not a loopback address; the relay binds loopback only")
    return str(host).strip().strip("[]")


def _on_nt() -> bool:
    return os.name == "nt"


def bind_loopback(host: str, port: int) -> socket.socket:
    """A listening TCP socket on loopback ``host``:``port`` (``port`` 0 picks a free one)."""
    address = require_loopback(host)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if _on_nt():
            # SO_REUSEADDR on Windows lets a second process bind a port that is in use.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        require_loopback(sock.getsockname()[0], what="bound relay")
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock


def _bearer(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != P.BEARER_SCHEME.lower() or not token.strip():
        return None
    return token.strip()


class RelayApp:
    """The ASGI callable: ``POST /v1/<verb>`` -> :meth:`RelayService.handle` on a worker thread."""

    def __init__(
        self,
        service: RelayService,
        executor: concurrent.futures.Executor,
        *,
        body_deadline_s: float = DEFAULT_BODY_DEADLINE_S,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    ) -> None:
        if body_deadline_s <= 0:
            raise ValueError("body_deadline_s must be positive")
        if isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or max_in_flight < 1:
            raise ValueError("max_in_flight must be a positive integer")
        self._service = service
        self._executor = executor
        self._body_deadline_s = body_deadline_s
        self._max_in_flight = max_in_flight
        self._in_flight = 0
        self._slots = threading.Lock()

    def _take_slot(self) -> bool:
        with self._slots:
            if self._in_flight >= self._max_in_flight:
                return False
            self._in_flight += 1
            return True

    def _free_slot(self, _future: Any = None) -> None:
        with self._slots:
            self._in_flight -= 1

    async def _read_body(self, receive: Callable[..., Any]) -> "bytes | tuple[int, dict[str, Any]]":
        service = self._service
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return service.transport_error("bad_request", "the client disconnected before the body was read")
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > P.MAX_REQUEST_BYTES:
                return service.transport_error("too_large", f"the request is over {P.MAX_REQUEST_BYTES} bytes",
                                               limit=P.MAX_REQUEST_BYTES, what="request")
        return bytes(body)

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        status, envelope = await self._answer(scope, receive)
        payload = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", P.CONTENT_TYPE.encode("ascii")),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": payload})

    async def _answer(self, scope: dict[str, Any], receive: Callable[..., Any]) -> tuple[int, dict[str, Any]]:
        service = self._service
        path = scope.get("path", "")
        verb = path[len(PATH_PREFIX):] if path.startswith(PATH_PREFIX) else ""
        if not verb or "/" in verb:
            return service.transport_error("unknown_verb", f"no relay verb at {path!r}; every verb is POST /v1/<verb>")
        if scope.get("method") != P.HTTP_METHOD:
            return service.transport_error("bad_request", f"relay verbs are {P.HTTP_METHOD} only")
        headers: dict[str, str] = {}
        for raw_name, raw_value in scope.get("headers", []):
            headers[raw_name.decode("latin-1").lower()] = raw_value.decode("latin-1")
        # Authenticate before reading a byte of the body: a caller without the relay token holds nothing open.
        if not service.relay_token_ok(headers.get(P.HEADER_RELAY_TOKEN.lower())):
            return service.transport_error("unauthenticated", f"missing or wrong {P.HEADER_RELAY_TOKEN}")
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in _JSON_TYPES:
            return service.transport_error("bad_request", f"Content-Type must be {P.CONTENT_TYPE}")
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > P.MAX_REQUEST_BYTES:
            return service.transport_error("too_large", f"the request is over {P.MAX_REQUEST_BYTES} bytes",
                                           limit=P.MAX_REQUEST_BYTES, what="request")
        if not self._take_slot():
            return service.transport_error("rate_limited", f"the relay has {self._max_in_flight} requests in flight; retry shortly",
                                           limit=self._max_in_flight, what="in_flight")
        handed_off = False
        try:
            try:
                body = await asyncio.wait_for(self._read_body(receive), timeout=self._body_deadline_s)
            except asyncio.TimeoutError:
                return service.transport_error(
                    "bad_request", f"the request body did not arrive within {self._body_deadline_s:g} s")
            if isinstance(body, tuple):
                return body
            call = functools.partial(
                service.handle, body, path_verb=verb,
                relay_token=headers.get(P.HEADER_RELAY_TOKEN.lower()),
                hub_bearer=_bearer(headers.get(P.HEADER_AUTHORIZATION.lower())),
                grant=headers.get(P.HEADER_GRANT.lower()) or None,
                ingress="relay",
            )
            work = self._executor.submit(call)
            work.add_done_callback(self._free_slot)  # the slot is held until the worker is done, even if the client leaves
            handed_off = True
            return await asyncio.wrap_future(work)
        finally:
            if not handed_off:
                self._free_slot()


class RelayServer:
    """One machine's relay: state, singleton lock, service and a loopback HTTP listener.

    ``port`` overrides the configured port (0 picks a free one; tests use it).
    ``roots`` and ``probe`` steer session discovery (default: the user's real
    harness homes, read only, and psutil). ``backend_factory`` and
    ``policy_loader`` pass through to :class:`llm_kit.relay.llm.LlmService`.

    Use :meth:`serve_forever` in the foreground (SIGINT or SIGTERM stops it) or
    :meth:`start` / :meth:`stop` (or ``with``) to serve on a background thread.
    """

    def __init__(
        self,
        config: RelayConfig,
        *,
        port: Optional[int] = None,
        roots: Optional[DiscoveryRoots] = None,
        probe: Optional[ProcessProbe] = None,
        clock: Callable[[], float] = time.time,
        backend_factory: Optional[Callable[[Any], Any]] = None,
        policy_loader: Optional[Callable[[str], Any]] = None,
        mailbox_limits: Optional[MailboxLimits] = None,
        poll_interval_s: float = 1.0,
        maintain_interval_s: float = DEFAULT_MAINTAIN_INTERVAL_S,
        workers: int = DEFAULT_WORKERS,
        body_deadline_s: float = DEFAULT_BODY_DEADLINE_S,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    ) -> None:
        require_enabled(ROLE, config.config_path)
        self.config = config
        host, configured_port = config.bind(ROLE)
        self._host = host
        self._port = configured_port if port is None else port
        self._roots = roots
        self._probe = probe
        self._clock = clock
        self._backend_factory = backend_factory
        self._policy_loader = policy_loader
        self._limits = mailbox_limits
        self._poll_interval_s = poll_interval_s
        self._maintain_interval_s = maintain_interval_s
        self._workers = workers
        self._body_deadline_s = body_deadline_s
        self._max_in_flight = max_in_flight
        self.prepared: Optional[PreparedState] = None
        self.service: Optional[RelayService] = None
        self.mailbox: Optional[Mailbox] = None
        self.table: Optional[RegistrationTable] = None
        self._lock: Optional[RoleLock] = None
        self._sock: Optional[socket.socket] = None
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._uvicorn: Any = None
        self._thread: Optional[threading.Thread] = None
        self._maint_thread: Optional[threading.Thread] = None
        self._stop_maint = threading.Event()
        self._failure: Optional[BaseException] = None

    # -- addresses --------------------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        """The bound ``(host, port)``; only while open."""
        if self._sock is None:
            raise RuntimeError("the relay server is not open")
        name = self._sock.getsockname()
        return name[0], name[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return base_url(host, port)

    # -- opening and closing --------------------------------------------------------

    def _open(self) -> None:
        if self._sock is not None:
            raise RuntimeError("the relay server is already open")
        require_enabled(ROLE, self.config.config_path)
        require_loopback(self._host)
        self.prepared = prepare_state(self.config)
        lock = RoleLock(self.config.paths, ROLE).acquire()
        try:
            machine = self.config.machine_label
            mailbox = Mailbox(mailbox_db_path(self.config.paths), machine=machine, clock=self._clock,
                              limits=self._limits)
            probe = self._probe if self._probe is not None else PsutilProbe()
            table = RegistrationTable(machine, probe=probe, clock=self._clock)
            discovery = Discovery(machine, self._roots if self._roots is not None else DiscoveryRoots.default(),
                                  table=table, probe=probe, clock=self._clock)
            from llm_kit.relay.llm import LlmService

            llm_kw: dict[str, Any] = {}
            if self._backend_factory is not None:
                llm_kw["backend_factory"] = self._backend_factory
            if self._policy_loader is not None:
                llm_kw["policy_loader"] = self._policy_loader
            llm = LlmService.from_state(self.prepared, mailbox, **llm_kw)
            endpoints = sorted(load_loopback_registry(self.prepared.registry_file).entries)
            self.service = RelayService(
                machine=machine, relay_token=self.prepared.relay_token, hub_bearer=self.prepared.hub_bearer,
                mailbox=mailbox, table=table, discovery=discovery, llm=llm,
                pause=self.prepared.pause_store(clock=self._clock), endpoints=endpoints,
                ledgers=self.config.ledgers, clock=self._clock, poll_interval_s=self._poll_interval_s,
            )
            self.mailbox, self.table = mailbox, table
            self._sock = bind_loopback(self._host, self._port)
        except BaseException:
            lock.release()
            raise
        self._lock = lock
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self._workers,
                                                               thread_name_prefix="llm-kit-relay-call")
        import uvicorn

        app = RelayApp(self.service, self._executor, body_deadline_s=self._body_deadline_s,
                       max_in_flight=self._max_in_flight)
        self._uvicorn = uvicorn.Server(uvicorn.Config(
            app, lifespan="off", http="h11", ws="none", log_config=None, log_level="warning",
            access_log=False, proxy_headers=False, server_header=False, timeout_graceful_shutdown=5,
        ))
        self._stop_maint.clear()
        self._maint_thread = threading.Thread(target=self._maintain_loop, name="llm-kit-relay-maintain", daemon=True)
        self._maint_thread.start()

    def _maintain_loop(self) -> None:
        while not self._stop_maint.wait(self._maintain_interval_s):
            try:
                assert self.service is not None
                self.service.maintain()
            except BaseException as exc:  # noqa: BLE001 -- stop the server loudly with this failure
                traceback.print_exc(file=sys.stderr)
                self._failure = exc
                if self._uvicorn is not None:
                    self._uvicorn.should_exit = True
                return

    def _close(self) -> None:
        self._stop_maint.set()
        if self.service is not None:
            self.service.close()
        if self._maint_thread is not None:
            self._maint_thread.join(timeout=10)
            self._maint_thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def _raise_failure(self) -> None:
        failure, self._failure = self._failure, None
        if failure is not None:
            raise RuntimeError(f"the relay stopped: maintenance failed: {failure!r}") from failure

    # -- serving -------------------------------------------------------------------

    def serve_forever(self, on_ready: Optional[Callable[["RelayServer"], None]] = None) -> None:
        """Serve until SIGINT or SIGTERM, then stop gracefully; call ``on_ready(self)`` once serving.

        Must run on the main thread (it installs the two signal handlers and
        restores the previous ones on return). The HTTP server runs on a
        background thread, exactly as :meth:`start` runs it. A server that
        stops without a signal is an error.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("serve_forever installs signal handlers and must run on the main thread")
        stop = threading.Event()
        previous = {sig: signal.signal(sig, lambda signum, frame: stop.set()) for sig in _STOP_SIGNALS}
        try:
            self.start()
            if on_ready is not None:
                on_ready(self)
            assert self._thread is not None
            while not stop.is_set() and self._thread.is_alive():
                stop.wait(0.5)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.stop()
        if not stop.is_set():
            raise RuntimeError("the relay HTTP server stopped without a signal")

    def start(self) -> "RelayServer":
        """Open and serve on a background thread; returns once requests are being answered."""
        self._open()
        assert self._sock is not None
        sock = self._sock
        self._thread = threading.Thread(target=lambda: self._uvicorn.run(sockets=[sock]),
                                        name="llm-kit-relay-http", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while not self._uvicorn.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("the relay HTTP server did not start")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        """Stop serving, release the lock, and raise a maintenance failure if one stopped the server."""
        if self.service is not None:
            self.service.close()
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                raise RuntimeError("the relay HTTP server did not stop within 15 s")
            self._thread = None
        self._close()
        self._raise_failure()

    def __enter__(self) -> "RelayServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


__all__ = [
    "DEFAULT_BODY_DEADLINE_S",
    "DEFAULT_MAINTAIN_INTERVAL_S",
    "DEFAULT_MAX_IN_FLIGHT",
    "PATH_PREFIX",
    "RelayApp",
    "RelayServer",
    "bind_loopback",
    "require_loopback",
]
