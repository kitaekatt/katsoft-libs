"""Session discovery for the relay: registrations merged with scans of the harness stores.

Serves ``list_sessions`` and ``read_session`` of ``relay-protocol/1``
(``docs/reference/llm-kit-relay-protocol.md``, sections 7, 9 and 13) for ONE
machine. Two sources are merged:

* the :class:`RegistrationTable`, an in-memory store of channel registrations
  (``register`` / ``heartbeat`` / ``deregister``, keyed by session id, with the
  ``previous_session_id`` rename of section 9). A registered session is the
  only kind that can be ``live-accepting``.
* scans of the harness stores, read only: Claude Code's per-process session
  files (``<claude_home>/sessions/<pid>.json``) and transcripts
  (``<claude_home>/projects/<slug>/<session id>.jsonl``), and Codex rollouts
  (``<codex_home>/sessions/**/rollout-*.jsonl``). A scanned session is
  ``registered: false``, accepts nothing and is never ``live-accepting``.

Session states (protocol section 13):

``live-accepting``
    A registered session whose lease is unexpired, whose process is alive with
    the recorded start time, and whose ``accepts`` is not empty.
``live-read-only``
    Process alive with the recorded start time but not accepting: a scanned
    Claude session, or a registered session with ``accepts == []``.
``idle``
    The process is gone (or its liveness cannot be proved) and a transcript
    exists whose last write is within ``idle_window_s``: resumable.
``ended``
    Process gone and the transcript is older than ``idle_window_s`` or absent,
    or the session id was replaced after ``/clear`` (``previous_session_id``)
    and no process shows it alive.

Liveness is ``pid alive AND process start time consistent``: a pid that was
reused by another process after the session died has a different start time,
so it does not revive the session. For a scanned Claude session the recorded
start is the session file's ``startedAt`` (epoch milliseconds; observed to
agree with the process creation time within a second); for a registration it
is the process creation time read when ``register`` arrived. A scanned Claude
file with no usable ``startedAt`` cannot be verified and is not live.

Codex. Liveness of a Codex session is NOT detected: a Codex process does not
hold its rollout file open between writes (checked with ``psutil.open_files``
on a machine running Codex; no process listed an open rollout), and the
processes found are shared app-servers that serve many sessions. Scanned Codex
sessions are therefore ``idle`` when the rollout was written within
``idle_window_s`` and ``ended`` otherwise; never ``live-*``.

The Claude session-file format is undocumented. Each file is parsed
defensively (unknown fields ignored); a file that does not parse, or whose
fields are unusable, is recorded in the scan's ``skipped`` list with its path
and reason and discovery continues. Nothing here reads a ``*.key`` file in the
sessions directory.

Selectors (``latest``, ``task:<project>/<stub>``, ``cwd:<project-or-path>``)
resolve against LIVE sessions only; a session that is not live is addressed
by its full address. ``task:`` uses the task recorded on a registration.

Reading a session returns a capped, redacted tail of its transcript
(:mod:`llm_kit.relay.transcript`). All filesystem roots, the clock and the
process probe are injectable; the default roots are the user's real harness
homes.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

from llm_kit.relay import protocol as P
from llm_kit.relay import transcript as T

DEFAULT_IDLE_WINDOW_S = 7 * 24 * 3600
DEFAULT_MAX_TRANSCRIPTS = 400
#: Allowed gap between a recorded process start and the process creation time.
START_TOLERANCE_S = 10.0
DEFAULT_TOMBSTONE_S = 3600
_HEAD_BYTES = 64 * 1024
_CODEX_HEAD_BYTES = 2 * 1024 * 1024
_NONCE_MIN, _NONCE_MAX = 16, 128

_SESSION_ID_RE = re.compile(P.SESSION_ID)
_CODEX_ID_RE = re.compile(
    r"rollout-.+-([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\.jsonl"
)

LIVE_STATES = ("live-accepting", "live-read-only")


# --------------------------------------------------------------------------
# roots and process probe
# --------------------------------------------------------------------------

def _claude_home() -> Path:
    return Path.home() / ".claude"


@dataclass(frozen=True)
class DiscoveryRoots:
    """Where the harness stores live; every one injectable."""

    claude_home: Path
    codex_home: Path

    @classmethod
    def default(cls) -> "DiscoveryRoots":
        """The current user's real harness homes (never used by the tests)."""
        return cls(_claude_home(), Path.home() / ".codex")

    @property
    def claude_sessions(self) -> Path:
        return self.claude_home / "sessions"

    @property
    def claude_projects(self) -> Path:
        return self.claude_home / "projects"

    @property
    def codex_sessions(self) -> Path:
        return self.codex_home / "sessions"


class ProbeError(Exception):
    """A process could not be examined (permission, platform), as opposed to not existing."""


class ProcessProbe(Protocol):
    def start_time(self, pid: int) -> Optional[float]:
        """Creation time (epoch seconds) of the live process ``pid``; None when none exists
        (or it is a zombie). Raises :class:`ProbeError` when the answer cannot be had."""


class PsutilProbe:
    """:class:`ProcessProbe` over psutil (the ``relay`` extra of katsoft-llm-kit)."""

    def __init__(self) -> None:
        try:
            import psutil  # noqa: WPS433 - optional dependency, absence reported loudly
        except ImportError as exc:  # pragma: no cover - environment specific
            raise RuntimeError(
                "session discovery needs psutil; install katsoft-llm-kit[relay] (or [local])"
            ) from exc
        self._psutil = psutil

    def start_time(self, pid: int) -> Optional[float]:
        psutil = self._psutil
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
            return float(proc.create_time())
        except psutil.NoSuchProcess:
            return None
        except psutil.Error as exc:
            raise ProbeError(f"cannot examine pid {pid}: {exc}") from exc


def process_matches(
    probe: ProcessProbe,
    pid: int,
    expected_start: Optional[float],
    tolerance: float = START_TOLERANCE_S,
) -> bool:
    """Pid alive AND its creation time within ``tolerance`` seconds of ``expected_start``.

    No recorded start means the match cannot be proved, so it is False.
    """
    if expected_start is None:
        return False
    actual = probe.start_time(pid)
    if actual is None:
        return False
    return abs(actual - expected_start) <= tolerance


# --------------------------------------------------------------------------
# registration table
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Registration:
    registration_id: str
    session_id: str
    harness: str
    pid: int
    pid_start: float
    cwd: str
    version: str
    accepts: tuple[str, ...]
    channel_nonce: str
    title: Optional[str]
    task: Optional[str]
    transcript_path: Optional[str]
    registered_at: float
    last_seen_at: float
    lease_expires_at: float


@dataclass(frozen=True)
class EndEvent:
    """A registration ended. ``reason`` is ``deregistered``, ``lease_lapsed`` or ``session_changed``."""

    address: str
    session_id: str
    registration_id: str
    reason: str
    at: float


@dataclass(frozen=True)
class RegisterOutcome:
    registration: Registration
    replaced: bool


@dataclass(frozen=True)
class HeartbeatOutcome:
    registration: Registration
    session_changed: bool
    old_address: Optional[str]


class RegistrationTable:
    """In-memory channel registrations, keyed by session id (protocol section 9).

    Thread-safe. State is committed before end listeners run; a listener that
    raises propagates its exception after every listener has run.
    Lapsed leases are swept at the start of every call and by
    :meth:`expire_lapsed`. A lease lapse ends the registration but does not
    look at the process; the discovery layer combines both.
    """

    def __init__(
        self,
        machine: str,
        *,
        probe: Optional[ProcessProbe] = None,
        clock: Callable[[], float] = time.time,
        lease_s: int = P.REGISTRATION_LEASE_S,
        tombstone_s: int = DEFAULT_TOMBSTONE_S,
        id_factory: Callable[[], str] = lambda: "reg_" + secrets.token_hex(8),
        start_tolerance_s: float = START_TOLERANCE_S,
    ) -> None:
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        self.machine = machine
        self._probe = probe if probe is not None else PsutilProbe()
        self._clock = clock
        self._lease_s = lease_s
        self._tombstone_s = tombstone_s
        self._id_factory = id_factory
        self._tolerance = start_tolerance_s
        self._lock = threading.RLock()
        self._by_session: dict[str, Registration] = {}
        self._tombstones: dict[str, tuple[str, float]] = {}
        self._listeners: list[Callable[[EndEvent], None]] = []

    # -- listeners --------------------------------------------------------

    def add_end_listener(self, listener: Callable[[EndEvent], None]) -> None:
        """Call ``listener(event)`` whenever a registration ends (the mailbox moves messages)."""
        self._listeners.append(listener)

    def _emit(self, events: Sequence[EndEvent]) -> None:
        error: Optional[BaseException] = None
        for event in events:
            for listener in list(self._listeners):
                try:
                    listener(event)
                except BaseException as exc:  # noqa: BLE001 - re-raised below, after all listeners ran
                    error = error or exc
        if error is not None:
            raise error

    # -- helpers ----------------------------------------------------------

    def address(self, session_id: str) -> str:
        return f"{self.machine}/claude/{session_id}"

    def _end(self, registration: Registration, reason: str, now: float) -> EndEvent:
        self._by_session.pop(registration.session_id, None)
        self._tombstones[registration.session_id] = (reason, now)
        return EndEvent(self.address(registration.session_id), registration.session_id,
                        registration.registration_id, reason, now)

    def _sweep(self, now: float) -> list[EndEvent]:
        events = [
            self._end(r, "lease_lapsed", now)
            for r in list(self._by_session.values())
            if r.lease_expires_at <= now
        ]
        for session_id, (_, at) in list(self._tombstones.items()):
            if now - at > self._tombstone_s:
                del self._tombstones[session_id]
        return events

    def _by_id(self, registration_id: str, nonce: str) -> Registration:
        for registration in self._by_session.values():
            if registration.registration_id == registration_id:
                if registration.channel_nonce != nonce:
                    raise P.RelayError("nonce_mismatch", "channel nonce does not match the registration")
                return registration
        raise P.RelayError("registration_unknown", "unknown or lapsed registration; register again")

    def _find(self, registration_id: str, session_id: str, nonce: str) -> Registration:
        registration = self._by_id(registration_id, nonce)
        if registration.session_id != session_id:
            raise P.RelayError(
                "registration_unknown", "the registration belongs to another session id; register again"
            )
        return registration

    @staticmethod
    def _check_accepts(accepts: Iterable[str]) -> tuple[str, ...]:
        values = tuple(accepts)
        for kind in values:
            if kind not in P.MESSAGE_KINDS:
                raise P.RelayError("bad_request", f"accepts has unknown message kind {kind!r}")
        return values

    def _alive(self, registration: Registration) -> bool:
        return process_matches(self._probe, registration.pid, registration.pid_start, self._tolerance)

    # -- verbs ------------------------------------------------------------

    def register(
        self,
        *,
        session_id: str,
        pid: int,
        cwd: str,
        version: str,
        accepts: Sequence[str],
        channel_nonce: str,
        harness: str = "claude",
        title: Optional[str] = None,
        task: Optional[str] = None,
        transcript_path: Optional[str] = None,
    ) -> RegisterOutcome:
        """Register a session. A same-session same-pid register replaces the old registration
        (``replaced``); one from another live pid with an unexpired lease is ``registration_conflict``."""
        if harness != "claude":
            raise P.RelayError("bad_request", "only harness claude registers")
        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id) or session_id == "latest":
            raise P.RelayError("bad_request", f"{session_id!r} is not a session id")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            raise P.RelayError("bad_request", "pid must be a positive integer")
        if not isinstance(channel_nonce, str) or not _NONCE_MIN <= len(channel_nonce) <= _NONCE_MAX:
            raise P.RelayError("bad_request", f"channel_nonce must be {_NONCE_MIN}-{_NONCE_MAX} characters")
        kinds = self._check_accepts(accepts)
        try:
            pid_start = self._probe.start_time(pid)
        except ProbeError as exc:
            raise P.RelayError("bad_request", str(exc)) from exc
        if pid_start is None:
            raise P.RelayError("bad_request", f"pid {pid} is not a live process")
        now = self._clock()
        events: list[EndEvent] = []
        with self._lock:
            events += self._sweep(now)
            replaced = False
            existing = self._by_session.get(session_id)
            if existing is not None:
                if existing.pid != pid and self._alive(existing):
                    raise P.RelayError(
                        "registration_conflict",
                        "a live registration from another process holds this session",
                        details={"pid": existing.pid},
                    )
                del self._by_session[session_id]
                replaced = True
            for other in list(self._by_session.values()):
                if other.pid == pid and other.session_id != session_id:
                    # one process, one registration: the process moved on to another session id
                    events.append(self._end(other, "session_changed", now))
            self._tombstones.pop(session_id, None)
            registration = Registration(
                registration_id=self._id_factory(),
                session_id=session_id,
                harness="claude",
                pid=pid,
                pid_start=pid_start,
                cwd=cwd,
                version=version,
                accepts=kinds,
                channel_nonce=channel_nonce,
                title=title,
                task=task,
                transcript_path=transcript_path,
                registered_at=now,
                last_seen_at=now,
                lease_expires_at=now + self._lease_s,
            )
            self._by_session[session_id] = registration
        self._emit(events)
        return RegisterOutcome(registration, replaced)

    def heartbeat(
        self,
        registration_id: str,
        session_id: str,
        channel_nonce: str,
        *,
        previous_session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        title: Optional[str] = None,
        task: Optional[str] = None,
        accepts: Optional[Sequence[str]] = None,
    ) -> HeartbeatOutcome:
        """Renew the lease and update metadata; with ``previous_session_id`` rename the session.

        The registration keeps its id and takes the new address; the old address is tombstoned
        ``session_changed`` and an :class:`EndEvent` for it is emitted (the mailbox then ends
        that address's open messages). A heartbeat repeated after a successful rename (the new
        id already registered, ``previous_session_id`` stale) is an ordinary heartbeat.
        """
        kinds = self._check_accepts(accepts) if accepts is not None else None
        now = self._clock()
        events: list[EndEvent] = []
        with self._lock:
            events += self._sweep(now)
            current = self._by_id(registration_id, channel_nonce)
            if (
                previous_session_id is not None
                and previous_session_id != session_id
                and current.session_id == previous_session_id
            ):
                registration = current
                if not _SESSION_ID_RE.fullmatch(session_id) or session_id == "latest":
                    raise P.RelayError("bad_request", f"{session_id!r} is not a session id")
                clash = self._by_session.get(session_id)
                if clash is not None:
                    raise P.RelayError("registration_conflict", "another registration holds the new session id")
                del self._by_session[previous_session_id]
                self._tombstones[previous_session_id] = ("session_changed", now)
                self._tombstones.pop(session_id, None)
                events.append(
                    EndEvent(self.address(previous_session_id), previous_session_id,
                             registration.registration_id, "session_changed", now)
                )
                changed, old = True, self.address(previous_session_id)
                registration = replace(registration, session_id=session_id)
            else:
                registration = self._find(registration_id, session_id, channel_nonce)
                changed, old = False, None
            registration = replace(
                registration,
                cwd=cwd if cwd is not None else registration.cwd,
                title=title if title is not None else registration.title,
                task=task if task is not None else registration.task,
                accepts=kinds if kinds is not None else registration.accepts,
                last_seen_at=now,
                lease_expires_at=now + self._lease_s,
            )
            self._by_session[registration.session_id] = registration
        self._emit(events)
        return HeartbeatOutcome(registration, changed, old)

    def touch(self, registration_id: str, session_id: str, channel_nonce: str) -> Registration:
        """Renew the lease (``poll`` does this)."""
        now = self._clock()
        events: list[EndEvent] = []
        with self._lock:
            events += self._sweep(now)
            registration = replace(
                self._find(registration_id, session_id, channel_nonce),
                last_seen_at=now,
                lease_expires_at=now + self._lease_s,
            )
            self._by_session[session_id] = registration
        self._emit(events)
        return registration

    def deregister(self, registration_id: str, session_id: str, channel_nonce: str) -> EndEvent:
        """End a registration on a clean exit."""
        now = self._clock()
        events: list[EndEvent] = []
        with self._lock:
            events += self._sweep(now)
            registration = self._find(registration_id, session_id, channel_nonce)
            event = self._end(registration, "deregistered", now)
        self._emit(events + [event])
        return event

    # -- queries ----------------------------------------------------------

    def expire_lapsed(self) -> list[EndEvent]:
        """End registrations whose lease lapsed; the events (also sent to listeners)."""
        with self._lock:
            events = self._sweep(self._clock())
        self._emit(events)
        return events

    def active(self) -> list[Registration]:
        """Unexpired registrations, after a sweep."""
        self.expire_lapsed()
        with self._lock:
            return list(self._by_session.values())

    def get(self, session_id: str) -> Optional[Registration]:
        self.expire_lapsed()
        with self._lock:
            return self._by_session.get(session_id)

    def ended_reason(self, session_id: str) -> Optional[str]:
        """Why ``session_id`` ended, while its tombstone is retained."""
        self.expire_lapsed()
        with self._lock:
            tomb = self._tombstones.get(session_id)
            return tomb[0] if tomb else None

    def is_alive(self, registration: Registration) -> bool:
        return self._alive(registration)


# --------------------------------------------------------------------------
# scan results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SkippedEntry:
    """A store entry discovery could not use; reported, never fatal."""

    path: str
    reason: str


@dataclass(frozen=True)
class SessionInfo:
    harness: str
    session_id: str
    state: str
    registered: bool
    accepts: tuple[str, ...]
    cwd: Optional[str] = None
    title: Optional[str] = None
    task: Optional[str] = None
    pid: Optional[int] = None
    version: Optional[str] = None
    last_activity_at: Optional[float] = None
    transcript: Optional[Path] = None

    def address(self, machine: str) -> str:
        return f"{machine}/{self.harness}/{self.session_id}"

    def to_session(self, machine: str) -> P.Session:
        session: P.Session = {
            "address": self.address(machine),
            "machine": machine,
            "harness": self.harness,
            "session_id": self.session_id,
            "state": self.state,
            "registered": self.registered,
            "accepts": list(self.accepts),
        }
        for key in ("cwd", "title", "task", "pid", "version", "last_activity_at"):
            value = getattr(self, key)
            if value is not None:
                session[key] = value  # type: ignore[literal-required]
        return session


@dataclass(frozen=True)
class ScanResult:
    sessions: tuple[SessionInfo, ...]
    skipped: tuple[SkippedEntry, ...]


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _short(value: Any, limit: int = 200) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


class Discovery:
    """Registrations merged with scans of the Claude and Codex stores for one machine."""

    def __init__(
        self,
        machine: str,
        roots: DiscoveryRoots,
        *,
        table: Optional[RegistrationTable] = None,
        probe: Optional[ProcessProbe] = None,
        clock: Callable[[], float] = time.time,
        idle_window_s: int = DEFAULT_IDLE_WINDOW_S,
        max_transcripts: int = DEFAULT_MAX_TRANSCRIPTS,
        start_tolerance_s: float = START_TOLERANCE_S,
        read_limits: T.ReadLimits = T.ReadLimits(),
    ) -> None:
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        self.machine = machine
        self.roots = roots
        self._probe = probe if probe is not None else (table._probe if table is not None else PsutilProbe())
        self.table = table if table is not None else RegistrationTable(
            machine, probe=self._probe, clock=clock, start_tolerance_s=start_tolerance_s
        )
        self._clock = clock
        self._idle_window_s = idle_window_s
        self._max_transcripts = max_transcripts
        self._tolerance = start_tolerance_s
        self._read_limits = read_limits

    # -- scanning ---------------------------------------------------------

    def _state_without_process(self, last_activity: Optional[float], now: float) -> str:
        if last_activity is not None and now - last_activity <= self._idle_window_s:
            return "idle"
        return "ended"

    def _claude_transcripts(self, skipped: list[SkippedEntry]) -> dict[str, tuple[Path, float]]:
        """Session id -> (newest transcript path, mtime) over every project directory."""
        found: dict[str, tuple[Path, float]] = {}
        projects = self.roots.claude_projects
        if not projects.is_dir():
            return found
        try:
            slugs = sorted(p for p in projects.iterdir() if p.is_dir())
        except OSError as exc:
            skipped.append(SkippedEntry(str(projects), f"unreadable: {exc.strerror or exc}"))
            return found
        for slug in slugs:
            try:
                entries = list(slug.glob("*.jsonl"))
            except OSError as exc:
                skipped.append(SkippedEntry(str(slug), f"unreadable: {exc.strerror or exc}"))
                continue
            for path in entries:
                if not _SESSION_ID_RE.fullmatch(path.stem):
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError as exc:
                    skipped.append(SkippedEntry(str(path), f"unreadable: {exc.strerror or exc}"))
                    continue
                known = found.get(path.stem)
                if known is None or mtime > known[1]:
                    found[path.stem] = (path, mtime)
        return found

    @staticmethod
    def _claude_head(path: Path) -> tuple[Optional[str], Optional[str]]:
        """(cwd, title) from the first bytes of a Claude transcript; (None, None) when absent."""
        cwd = title = None
        try:
            with open(path, "rb") as handle:
                head = handle.read(_HEAD_BYTES)
        except OSError:
            return None, None
        raws = head.split(b"\n")
        if len(head) == _HEAD_BYTES:
            raws = raws[:-1]  # the last line may be cut by the read cap
        for raw in raws:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, Mapping):
                continue
            if cwd is None:
                cwd = _short(obj.get("cwd"), 1000)
            if title is None and obj.get("type") == "custom-title":
                title = _short(obj.get("customTitle"))
            if cwd and title:
                break
        return cwd, title

    def _scan_claude(self, now: float, skipped: list[SkippedEntry]) -> list[SessionInfo]:
        transcripts = self._claude_transcripts(skipped)
        sessions: dict[str, SessionInfo] = {}
        directory = self.roots.claude_sessions
        files: list[Path] = []
        if directory.is_dir():
            try:
                files = sorted(directory.glob("*.json"))
            except OSError as exc:
                skipped.append(SkippedEntry(str(directory), f"unreadable: {exc.strerror or exc}"))
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, Mapping):
                    raise ValueError("not a JSON object")
                pid = data.get("pid")
                if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
                    raise ValueError("pid missing or not a positive integer")
                if path.stem != str(pid):
                    raise ValueError(f"file name does not match pid {pid}")
                session_id = data.get("sessionId")
                if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id) or session_id == "latest":
                    raise ValueError("sessionId missing or not a valid session id")
                started_ms = _number(data.get("startedAt"))
                live = process_matches(
                    self._probe, pid, started_ms / 1000.0 if started_ms is not None else None, self._tolerance
                )
            except (OSError, ValueError, ProbeError) as exc:
                skipped.append(SkippedEntry(str(path), str(exc) or type(exc).__name__))
                continue
            tpath, tmtime = transcripts.get(session_id, (None, None))
            updated = _number(data.get("updatedAt"))
            last = tmtime if tmtime is not None else (updated / 1000.0 if updated is not None else None)
            state = "live-read-only" if live else self._state_without_process(last, now)
            cwd = _short(data.get("cwd"), 1000)
            title = _short(data.get("name"))
            if (cwd is None or title is None) and tpath is not None:
                head_cwd, head_title = self._claude_head(tpath)
                cwd, title = cwd or head_cwd, title or head_title
            info = SessionInfo(
                "claude", session_id, state, False, (), cwd, title, None, pid,
                _short(data.get("version"), 64), last, tpath,
            )
            known = sessions.get(session_id)
            if known is None or (info.state in LIVE_STATES and known.state not in LIVE_STATES):
                sessions[session_id] = info

        rest = sorted(
            ((sid, tp, mt) for sid, (tp, mt) in transcripts.items() if sid not in sessions),
            key=lambda item: item[2],
            reverse=True,
        )[: self._max_transcripts]
        for sid, tpath, mtime in rest:
            cwd, title = self._claude_head(tpath)
            sessions[sid] = SessionInfo(
                "claude", sid, self._state_without_process(mtime, now), False, (), cwd, title,
                None, None, None, mtime, tpath,
            )
        return list(sessions.values())

    @staticmethod
    def _codex_head(path: Path) -> Optional[Mapping[str, Any]]:
        try:
            with open(path, "rb") as handle:
                head = handle.read(_CODEX_HEAD_BYTES)
        except OSError:
            return None
        first = head.split(b"\n", 1)
        if len(first) < 2 and len(head) == _CODEX_HEAD_BYTES:
            return None
        try:
            obj = json.loads(first[0])
        except ValueError:
            return None
        payload = obj.get("payload") if isinstance(obj, Mapping) else None
        return payload if isinstance(payload, Mapping) else None

    def _scan_codex(self, now: float, skipped: list[SkippedEntry]) -> list[SessionInfo]:
        root = self.roots.codex_sessions
        if not root.is_dir():
            return []
        collected: list[Path] = []

        def walk(directory: Path) -> None:
            if len(collected) >= self._max_transcripts:
                return
            try:
                entries = sorted(directory.iterdir(), key=lambda p: p.name, reverse=True)
            except OSError as exc:
                skipped.append(SkippedEntry(str(directory), f"unreadable: {exc.strerror or exc}"))
                return
            for entry in entries:
                if len(collected) >= self._max_transcripts:
                    return
                if entry.is_dir():
                    walk(entry)
                elif entry.name.startswith("rollout-") and entry.suffix == ".jsonl":
                    collected.append(entry)

        walk(root)
        sessions: list[SessionInfo] = []
        for path in collected:
            match = _CODEX_ID_RE.fullmatch(path.name)
            if match is None:
                skipped.append(SkippedEntry(str(path), "file name carries no session id"))
                continue
            session_id = match.group(1).lower()
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                skipped.append(SkippedEntry(str(path), f"unreadable: {exc.strerror or exc}"))
                continue
            meta = self._codex_head(path)
            cwd = None
            if meta is not None:
                declared = meta.get("id")
                if isinstance(declared, str) and declared.lower() != session_id:
                    skipped.append(SkippedEntry(str(path), "session_meta id differs from the file name"))
                    continue
                cwd = _short(meta.get("cwd"), 1000)
            version = _short(meta.get("cli_version"), 64) if meta is not None else None
            sessions.append(
                SessionInfo("codex", session_id, self._state_without_process(mtime, now), False, (),
                            cwd, None, None, None, version, mtime, path)
            )
        return sessions

    def _merge_registrations(self, scanned: list[SessionInfo], now: float) -> list[SessionInfo]:
        by_id = {(s.harness, s.session_id): s for s in scanned}
        for registration in self.table.active():
            try:
                alive = self.table.is_alive(registration)
            except ProbeError:
                alive = False
            base = by_id.get(("claude", registration.session_id))
            tpath = Path(registration.transcript_path) if registration.transcript_path else (base.transcript if base else None)
            last = base.last_activity_at if base and base.last_activity_at is not None else registration.last_seen_at
            if alive:
                state = "live-accepting" if registration.accepts else "live-read-only"
            else:
                state = self._state_without_process(last, now)
            by_id[("claude", registration.session_id)] = SessionInfo(
                "claude", registration.session_id, state, True, registration.accepts,
                registration.cwd or (base.cwd if base else None),
                registration.title or (base.title if base else None),
                registration.task, registration.pid,
                registration.version or (base.version if base else None),
                last, tpath,
            )
        merged: list[SessionInfo] = []
        for info in by_id.values():
            if info.harness == "claude" and info.state not in LIVE_STATES and not info.registered:
                if self.table.ended_reason(info.session_id) == "session_changed":
                    info = replace(info, state="ended")
            merged.append(info)
        return merged

    def scan(self) -> ScanResult:
        """All sessions of this machine, newest activity first, plus the entries that were skipped."""
        now = self._clock()
        skipped: list[SkippedEntry] = []
        scanned = self._scan_claude(now, skipped) + self._scan_codex(now, skipped)
        merged = self._merge_registrations(scanned, now)
        merged.sort(key=lambda s: (s.last_activity_at is None, -(s.last_activity_at or 0.0), s.session_id))
        return ScanResult(tuple(merged), tuple(skipped))

    # -- protocol verbs ---------------------------------------------------

    def list_sessions(self, harness: Optional[str] = None, state: Optional[str] = None) -> ScanResult:
        """``list_sessions``: the scan filtered by ``harness`` and ``state``."""
        if harness is not None and harness not in P.SESSION_HARNESSES:
            raise P.RelayError("bad_request", f"harness must be one of {P.SESSION_HARNESSES}")
        if state is not None and state not in P.SESSION_STATES:
            raise P.RelayError("bad_request", f"state must be one of {P.SESSION_STATES}")
        result = self.scan()
        sessions = tuple(
            s for s in result.sessions
            if (harness is None or s.harness == harness) and (state is None or s.state == state)
        )
        return ScanResult(sessions, result.skipped)

    def resolve(self, target: str) -> SessionInfo:
        """The one session ``target`` (an address or a selector) names."""
        parsed = P.parse_target(target)
        result = self.scan()
        if isinstance(parsed, P.Address):
            if parsed.machine != self.machine:
                raise P.RelayError("target_not_found", f"{parsed.machine!r} is not this machine")
            if parsed.harness not in P.SESSION_HARNESSES:
                raise P.RelayError("target_invalid", f"harness {parsed.harness!r} has no sessions to read")
            for info in result.sessions:
                if info.harness == parsed.harness and info.session_id == parsed.session:
                    return info
            raise P.RelayError("target_not_found", f"no session {parsed}")
        if parsed.machine not in (None, self.machine):
            raise P.RelayError("target_not_found", f"{parsed.machine!r} is not this machine")
        candidates = [
            s for s in result.sessions
            if s.state in LIVE_STATES and (parsed.harness is None or s.harness == parsed.harness)
        ]
        if parsed.kind == "task":
            matches = [s for s in candidates if s.task == parsed.value]
        elif parsed.kind == "cwd":
            if parsed.value.startswith("/"):
                matches = [s for s in candidates if s.cwd is not None and s.cwd.rstrip("/") == parsed.value.rstrip("/")]
            else:
                matches = [
                    s for s in candidates
                    if s.cwd is not None and os.path.basename(s.cwd.rstrip("/")) == parsed.value
                ]
        else:
            matches = candidates
            if matches:
                return max(matches, key=lambda s: s.last_activity_at or 0.0)
        if not matches:
            raise P.RelayError("target_not_found", f"no live session matches {parsed}")
        if len(matches) > 1:
            raise P.RelayError(
                "target_ambiguous", f"{parsed} matches {len(matches)} live sessions",
                details={"candidates": [s.address(self.machine) for s in matches]},
            )
        return matches[0]

    def read_session(
        self, address: str, since: Optional[str] = None, limit: int = P.DEFAULT_READ_TURNS
    ) -> dict[str, Any]:
        """``read_session``: a capped, redacted transcript tail as the protocol result object."""
        info = self.resolve(address)
        if info.transcript is None:
            raise P.RelayError("target_not_found", f"session {info.address(self.machine)} has no transcript")
        return T.read_transcript(
            info.transcript, info.harness, info.address(self.machine),
            since=since, limit=limit, limits=self._read_limits,
        ).to_result()
