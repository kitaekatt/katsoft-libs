"""Interface types for the agent-session Claude backend (unit I0).

This module FIXES the interfaces that the agent-session leaves implement
against: the request and outcome of one agent session, how a session ended,
the per-attempt session directory, the mailbox record and state shapes, the
shutdown bounds and their validation, the injected session channel, the
workspace finalization hook, and the ledger schema 14 columns. It holds frozen
values, enums, Protocols and pure validation only. It performs no I/O: no file
is read or written, and no path is resolved against the filesystem.

Why it lives in ``llm_kit.completion`` and not in ``llm_kit.jobs``: the signed
component graph (``tests/llm-kit/test_import_graph.py``, ``ALLOWED``) lets
``adapters`` import only ``completion`` and ``registry``, and lets ``jobs``
import ``completion`` but not ``adapters``. The session backend
(``adapters/claude_session.py``) and its caller (``jobs/run.py``) must both
import these types, so ``completion`` is the one component both may reach.
The same pattern holds :mod:`llm_kit.completion.factory_protocols`. A LEAF
module: it imports the standard library and :mod:`llm_kit.completion.types`.

Source of truth for the semantics: the agent-runs design, sections 5.1-5.5,
6.1, 6.2 and 11.1-11.5. Each invariant below cites the section it comes from.

Ledger schema 14 (design 11.2): :data:`SESSION_LEDGER_COLUMNS` is the column
list; each column is added to BOTH :data:`SESSION_LEDGER_TABLES`, and the
model attribute on ``Attempt`` and ``AttemptReservation`` carries the column's
name exactly. A reader of a ledger older than :data:`SESSION_LEDGER_SCHEMA`
supplies each column's ``pre_v14`` value (``LedgerReader`` never migrates).
"""
from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

from llm_kit.completion.types import BackendOptions, LLMResponse

# ---------------------------------------------------------------------------
# Session mode and job options (design 11.1, 11.3)
# ---------------------------------------------------------------------------


class SessionMode(str, Enum):
    """Which Claude backend runs a job (job option ``session``).

    ``completion`` is today's one-shot ``claude -p --output-format json``
    backend and the default. ``agent`` is the stream-json agent session.
    """

    COMPLETION = "completion"
    AGENT = "agent"


#: Job option naming the session mode. Job-level only (design 11.1).
OPTION_SESSION = "session"
#: Job option naming a Claude session id to resume. Agent mode only.
OPTION_RESUME_SESSION = "resume_session"
#: Job option for Q, the quiescence window in seconds (design 5.5, 11.3).
OPTION_QUIESCENCE_S = "session_quiescence_s"
#: Job option for B_res, the wait for a result after an interrupt.
OPTION_RESULT_WAIT_S = "session_result_wait_s"
#: Job option for B_eof, the wait for exit after stdin is closed.
OPTION_EOF_WAIT_S = "session_eof_wait_s"
#: Job option for B_int, the wait for exit after SIGINT.
OPTION_SIGINT_WAIT_S = "session_sigint_wait_s"

#: The four bound option keys, in Q, B_res, B_eof, B_int order.
BOUND_OPTION_KEYS: tuple[str, ...] = (
    OPTION_QUIESCENCE_S,
    OPTION_RESULT_WAIT_S,
    OPTION_EOF_WAIT_S,
    OPTION_SIGINT_WAIT_S,
)

#: Every job option this module owns. All are job-level only: none may appear
#: in a per-model options mapping.
SESSION_OPTION_KEYS = frozenset(
    {OPTION_SESSION, OPTION_RESUME_SESSION, *BOUND_OPTION_KEYS}
)

#: Q default, seconds (design 11.3).
DEFAULT_QUIESCENCE_S = 5.0
#: B_res default, seconds.
DEFAULT_RESULT_WAIT_S = 30.0
#: B_eof default, seconds.
DEFAULT_EOF_WAIT_S = 30.0
#: B_int default, seconds.
DEFAULT_SIGINT_WAIT_S = 10.0

#: ``system_prompt_mode`` default per session mode (design 3, 11.1): agent
#: sessions append to Claude Code's own system prompt; completion keeps replace.
DEFAULT_SYSTEM_PROMPT_MODE: Mapping[SessionMode, str] = {
    SessionMode.COMPLETION: "replace",
    SessionMode.AGENT: "append",
}

#: The permission mode every agent session runs with and its init event must
#: report (design 5, 9).
AGENT_PERMISSION_MODE = "bypassPermissions"


def _bound(name: str, value: object) -> float:
    """Return ``value`` as a float, or raise ``ValueError`` naming ``name``.

    A bound is a real number of seconds that is finite and strictly positive.
    ``bool`` is refused although it is an ``int`` subclass.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if number <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return number


@dataclass(frozen=True)
class SessionBounds:
    """The four shutdown bounds of one agent session, in seconds (design 11.3).

    - ``quiescence_s`` (Q): an attempt ends when a ``result`` has been seen and
      no new ``system:init`` arrives within Q. Q follows the LATEST completed
      turn: a later init cancels the earlier result's timer (design 5.5).
    - ``result_wait_s`` (B_res): after an interrupt, the wait for a result not
      already observed.
    - ``eof_wait_s`` (B_eof): after stdin is closed, the wait for exit.
    - ``sigint_wait_s`` (B_int): after SIGINT, the wait for exit before the
      process group is killed.

    Invariant: every bound is finite and strictly positive. Construction with
    any other value raises ``ValueError``; values are stored as ``float``.
    """

    quiescence_s: float = DEFAULT_QUIESCENCE_S
    result_wait_s: float = DEFAULT_RESULT_WAIT_S
    eof_wait_s: float = DEFAULT_EOF_WAIT_S
    sigint_wait_s: float = DEFAULT_SIGINT_WAIT_S

    def __post_init__(self) -> None:
        object.__setattr__(self, "quiescence_s", _bound("quiescence_s", self.quiescence_s))
        object.__setattr__(self, "result_wait_s", _bound("result_wait_s", self.result_wait_s))
        object.__setattr__(self, "eof_wait_s", _bound("eof_wait_s", self.eof_wait_s))
        object.__setattr__(self, "sigint_wait_s", _bound("sigint_wait_s", self.sigint_wait_s))

    @property
    def shutdown_allowance_s(self) -> float:
        """B_res + B_eof + B_int: the worst case from Stop to a forced kill.

        It excludes Q, which applies once per completed turn. How this
        allowance relates to the job timeout and the supervisor deadline is an
        OPEN design point (design 11.3); this property only measures it.
        """
        return self.result_wait_s + self.eof_wait_s + self.sigint_wait_s

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> "SessionBounds":
        """Read the bound option keys from a job options mapping.

        A missing key takes its default. Keys other than the four bound keys
        are ignored. Raises ``ValueError`` for an invalid value.
        """
        return cls(
            quiescence_s=options.get(OPTION_QUIESCENCE_S, DEFAULT_QUIESCENCE_S),  # type: ignore[arg-type]
            result_wait_s=options.get(OPTION_RESULT_WAIT_S, DEFAULT_RESULT_WAIT_S),  # type: ignore[arg-type]
            eof_wait_s=options.get(OPTION_EOF_WAIT_S, DEFAULT_EOF_WAIT_S),  # type: ignore[arg-type]
            sigint_wait_s=options.get(OPTION_SIGINT_WAIT_S, DEFAULT_SIGINT_WAIT_S),  # type: ignore[arg-type]
        )


def check_session_id(value: object, *, name: str = "session id") -> str:
    """Return ``value`` when it is a canonical lowercase hyphenated UUID.

    Agent sessions are started with ``--session-id <uuid>`` (design 5.1), so a
    session id this library assigns or resumes is a UUID. The canonical form
    also keeps an id from being read as a command-line flag. Raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {value!r}")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        raise ValueError(f"{name} must be a UUID, got {value!r}") from None
    if canonical != value:
        raise ValueError(f"{name} must be the canonical UUID form {canonical!r}, got {value!r}")
    return value


@dataclass(frozen=True)
class SessionOptions:
    """The validated session options of one job (design 11.1, 11.3).

    Invariants: ``resume_session`` is set only in agent mode and is a
    canonical UUID; ``bounds`` is the defaults in completion mode, because a
    bound option given with completion mode is refused by
    :meth:`from_job_options`.
    """

    mode: SessionMode = SessionMode.COMPLETION
    resume_session: Optional[str] = None
    bounds: SessionBounds = SessionBounds()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", SessionMode(self.mode))
        if self.resume_session is not None:
            if self.mode is not SessionMode.AGENT:
                raise ValueError(f"{OPTION_RESUME_SESSION} requires {OPTION_SESSION}: agent")
            check_session_id(self.resume_session, name=OPTION_RESUME_SESSION)
        if not isinstance(self.bounds, SessionBounds):
            raise ValueError("bounds must be a SessionBounds")

    @property
    def default_system_prompt_mode(self) -> str:
        """The ``system_prompt_mode`` a job of this mode gets when it sets none."""
        return DEFAULT_SYSTEM_PROMPT_MODE[self.mode]

    @classmethod
    def from_job_options(cls, options: Mapping[str, object]) -> "SessionOptions":
        """Validate the session keys of a job-level options mapping.

        Other keys are ignored (the job model validates them). Raises
        ``ValueError`` for an unknown session mode, a resume id outside agent
        mode or not a UUID, a bound option outside agent mode, or an invalid
        bound.
        """
        raw_mode = options.get(OPTION_SESSION, SessionMode.COMPLETION.value)
        try:
            mode = SessionMode(raw_mode)
        except ValueError:
            allowed = ", ".join(m.value for m in SessionMode)
            raise ValueError(f"{OPTION_SESSION} must be one of: {allowed}; got {raw_mode!r}") from None
        if mode is not SessionMode.AGENT:
            given = sorted(key for key in BOUND_OPTION_KEYS if key in options)
            if given:
                raise ValueError(f"{given} require {OPTION_SESSION}: agent")
        return cls(
            mode=mode,
            resume_session=options.get(OPTION_RESUME_SESSION),  # type: ignore[arg-type]
            bounds=SessionBounds.from_options(options),
        )


# ---------------------------------------------------------------------------
# How a session ended (design 5.5)
# ---------------------------------------------------------------------------


class StopKind(str, Enum):
    """Why an agent session's process was shut down (ledger ``stop_kind``).

    - ``quiescent``: a result was seen and no new init came within Q; the
      normal end of an attempt.
    - ``operator_stop`` / ``operator_pause``: an operator stop or pause
      (either :class:`StopMode`). Terminal: the job becomes CANCELLED with the
      error code of the same name, independent of backend error
      classification, and is never retried (design 5.5).
    - ``timeout``: the job's deadline was reached before quiescence.
    - ``process_exit``: Claude exited on its own before quiescence.
    - ``init_mismatch``: an init event's session id, cwd or permission mode
      did not match the request, so the process was stopped.

    A hard process-tree cancel kills the recording process, so it never
    produces a StopKind; such an attempt's ``stop_kind`` stays NULL.
    """

    QUIESCENT = "quiescent"
    OPERATOR_STOP = "operator_stop"
    OPERATOR_PAUSE = "operator_pause"
    TIMEOUT = "timeout"
    PROCESS_EXIT = "process_exit"
    INIT_MISMATCH = "init_mismatch"

    @property
    def is_operator(self) -> bool:
        """True for the two operator kinds, which are terminal (CANCELLED)."""
        return self in (StopKind.OPERATOR_STOP, StopKind.OPERATOR_PAUSE)

    @property
    def operator_error_code(self) -> Optional[str]:
        """The attempt error code for an operator kind, else ``None``."""
        return self.value if self.is_operator else None


class StopRequestKind(str, Enum):
    """What an operator asked for. Pause is Stop plus a databench-side mark."""

    STOP = "stop"
    PAUSE = "pause"

    @property
    def stop_kind(self) -> StopKind:
        """The :class:`StopKind` the session records when this request ends it."""
        return StopKind.OPERATOR_STOP if self is StopRequestKind.STOP else StopKind.OPERATOR_PAUSE


class StopMode(str, Enum):
    """How a stop is carried out (design 5.5).

    - ``interrupt``: idle -> close stdin; busy -> send the interrupt
      ``control_request``, wait B_res for a result not already observed, then
      shut down. A success result racing the stop is accepted as the result.
    - ``finish_turn``: shut down without the interrupt, letting the current
      turn finish.
    """

    INTERRUPT = "interrupt"
    FINISH_TURN = "finish_turn"


# ---------------------------------------------------------------------------
# Session directory layout (design 11.1)
# ---------------------------------------------------------------------------

#: Claude's stream-json stdout, verbatim and line-buffered (design 5.2).
#: Single writer: the session backend in the ``jobs run`` process.
EVENTS_FILE = "events.jsonl"
#: Append-only mailbox, one :class:`MailboxRecord` JSON object per line.
MAILBOX_FILE = "mailbox.jsonl"
#: The :class:`MailboxState` JSON object, replaced atomically.
MAILBOX_STATE_FILE = "mailbox-state.json"
#: The lock file serializing every mailbox append, forward mark and closure.
#: Separate from the data files because a Windows byte-range lock also denies
#: reads of the locked file.
MAILBOX_LOCK_FILE = "mailbox.lock"
#: The :class:`SessionRecord` JSON object, replaced atomically. Single writer:
#: ``llm_kit.jobs.session``.
SESSION_RECORD_FILE = "session.json"
#: Prefix of the per-attempt directory name: ``attempt-<n>``.
ATTEMPT_DIR_PREFIX = "attempt-"


def encode_job_id(job_id: str) -> str:
    """Return a stable, bounded, filesystem-safe path component for a job id.

    Job ids only need to be nonempty (``Job.__post_init__``), so the id is
    never used raw. The rule is the one ``jobs/workspace.py`` uses for
    worktree paths: ASCII alphanumerics and ``._-`` are kept, every other
    character becomes ``_``, leading and trailing ``.``/``_`` are stripped,
    the result is cut to 80 characters (``job`` when empty), and the first 12
    hex digits of the id's SHA-256 are appended after ``-``. Distinct ids
    therefore map to distinct components. Raises ``ValueError`` for an id
    that is empty after stripping whitespace.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job id must be a nonempty string")
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in job_id
    ).strip("._")
    if not safe:
        safe = "job"
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:80]}-{digest}"


@dataclass(frozen=True)
class SessionPaths:
    """The files of one attempt's session directory.

    ``attempt_dir`` is ``<session-root>/<encode_job_id(job id)>/attempt-<n>``
    (:meth:`for_attempt`). Invariant: ``attempt_dir`` is absolute. The
    directory is created by ``llm_kit.jobs.session`` before the session
    backend runs; this value never touches the filesystem.
    """

    attempt_dir: Path

    def __post_init__(self) -> None:
        path = Path(self.attempt_dir)
        if not path.is_absolute():
            raise ValueError(f"attempt_dir must be absolute, got {path}")
        object.__setattr__(self, "attempt_dir", path)

    @classmethod
    def for_attempt(cls, session_root: Path, job_id: str, attempt_no: int) -> "SessionPaths":
        """Build the paths for attempt ``attempt_no`` of ``job_id``.

        ``session_root`` is the run-specific ``jobs run --session-root``
        directory and must be absolute. ``attempt_no`` is the ledger attempt
        number, a positive ``int``. Raises ``ValueError`` otherwise.
        """
        root = Path(session_root)
        if not root.is_absolute():
            raise ValueError(f"session_root must be absolute, got {root}")
        if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no <= 0:
            raise ValueError(f"attempt_no must be a positive integer, got {attempt_no!r}")
        return cls(root / encode_job_id(job_id) / f"{ATTEMPT_DIR_PREFIX}{attempt_no}")

    @property
    def events(self) -> Path:
        """``events.jsonl``."""
        return self.attempt_dir / EVENTS_FILE

    @property
    def mailbox(self) -> Path:
        """``mailbox.jsonl``."""
        return self.attempt_dir / MAILBOX_FILE

    @property
    def mailbox_state(self) -> Path:
        """``mailbox-state.json``."""
        return self.attempt_dir / MAILBOX_STATE_FILE

    @property
    def mailbox_lock(self) -> Path:
        """``mailbox.lock``."""
        return self.attempt_dir / MAILBOX_LOCK_FILE

    @property
    def session_record(self) -> Path:
        """``session.json``."""
        return self.attempt_dir / SESSION_RECORD_FILE


# ---------------------------------------------------------------------------
# JSON Schema file names (design 11.4). Contents are written by leaf L1b.
# ---------------------------------------------------------------------------

#: Package holding the schema directory.
SCHEMA_PACKAGE = "llm_kit.jobs"
#: Directory under :data:`SCHEMA_PACKAGE`.
SCHEMA_DIRECTORY = "schemas"
#: One ``mailbox.jsonl`` line (:class:`MailboxRecord`).
MAILBOX_RECORD_SCHEMA_FILE = "mailbox_record.json"
#: ``mailbox-state.json`` (:class:`MailboxState`).
MAILBOX_STATE_SCHEMA_FILE = "mailbox_state.json"
#: ``session.json`` (:class:`SessionRecord`).
SESSION_RECORD_SCHEMA_FILE = "session_record.json"
#: One ``events.jsonl`` line: the permissive schema of design 11.4, verbatim.
EVENT_LINE_SCHEMA_FILE = "event_line.json"
#: Every schema file, for a test that each exists and is loadable.
SESSION_SCHEMA_FILES: tuple[str, ...] = (
    MAILBOX_RECORD_SCHEMA_FILE,
    MAILBOX_STATE_SCHEMA_FILE,
    SESSION_RECORD_SCHEMA_FILE,
    EVENT_LINE_SCHEMA_FILE,
)

#: The ``schema`` field value of a mailbox record, the mailbox state and the
#: session record. A reader refuses any other value.
SESSION_FILES_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Mailbox (design 5.3, 5.4)
# ---------------------------------------------------------------------------


class MailboxRecordKind(str, Enum):
    """The kind of one mailbox line."""

    MESSAGE = "message"
    STOP = "stop"


class MessageDelivery(str, Enum):
    """Delivery state of one mailbox record (design 5.3, 5.4).

    ``forwarded``, never ``consumed``: stdout does not echo an injected
    message, so a successful stdin write proves only that it was forwarded.
    ``undelivered``: the mailbox closed before the record was forwarded,
    including a record appended after closure. ``pending``: open, not yet
    forwarded.
    """

    PENDING = "pending"
    FORWARDED = "forwarded"
    UNDELIVERED = "undelivered"


def _nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string, got {value!r}")
    return value


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class StopRequest:
    """An operator stop or pause request carried by a ``stop`` mailbox line."""

    kind: StopRequestKind = StopRequestKind.STOP
    mode: StopMode = StopMode.INTERRUPT

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", StopRequestKind(self.kind))
        object.__setattr__(self, "mode", StopMode(self.mode))


@dataclass(frozen=True)
class MailboxRecord:
    """One ``mailbox.jsonl`` line.

    JSON shape: ``{"schema": 1, "seq": int, "id": str, "at": str, "kind":
    "message"|"stop", "text": str|null, "stop": {"kind", "mode"}|null}``.

    Invariants:

    - ``seq`` starts at 1 and increases by exactly 1 per line; it is assigned
      under the mailbox lock, so it is unique within one attempt.
    - ``id`` is a nonempty caller-visible id (a UUID hex is conventional).
    - ``at`` is an ISO-8601 UTC timestamp string of the append.
    - a ``message`` has nonempty ``text`` and no ``stop``; a ``stop`` has a
      :class:`StopRequest` and no ``text``.
    - the job's initial prompt is NOT a mailbox record: it is the first
      user message the session backend writes to stdin (design 5.3).

    Protocol (design 5.3): every append, every forward mark and the closure
    take :data:`MAILBOX_LOCK_FILE`; an append is one ``O_APPEND`` write of one
    line followed by ``fsync`` before the lock is released. A record appended
    after closure is still written and its delivery is ``undelivered``.
    """

    seq: int
    id: str
    at: str
    kind: MailboxRecordKind
    text: Optional[str] = None
    stop: Optional[StopRequest] = None

    def __post_init__(self) -> None:
        _positive_int("seq", self.seq)
        _nonempty_str("id", self.id)
        _nonempty_str("at", self.at)
        kind = MailboxRecordKind(self.kind)
        object.__setattr__(self, "kind", kind)
        if kind is MailboxRecordKind.MESSAGE:
            _nonempty_str("message text", self.text)
            if self.stop is not None:
                raise ValueError("a message record carries no stop request")
        else:
            if not isinstance(self.stop, StopRequest):
                raise ValueError("a stop record requires a StopRequest")
            if self.text is not None:
                raise ValueError("a stop record carries no text")


@dataclass(frozen=True)
class MailboxState:
    """``mailbox-state.json``: forwarding and closure of one attempt's mailbox.

    JSON shape: ``{"schema": 1, "forwarded_through_seq": int, "closed": bool,
    "closed_at": str|null, "closed_through_seq": int|null, "close_reason":
    StopKind value|null}``.

    Invariants:

    - the session backend forwards records strictly in ``seq`` order, so one
      high-water mark, ``forwarded_through_seq`` (0 = none), records every
      forward.
    - ``closed_at``, ``closed_through_seq`` and ``close_reason`` are set
      exactly when ``closed`` is true; ``closed_through_seq`` is the highest
      ``seq`` present when the mailbox closed, and
      ``forwarded_through_seq <= closed_through_seq``.
    - once closed, nothing is forwarded; the state never reopens.
    """

    forwarded_through_seq: int = 0
    closed: bool = False
    closed_at: Optional[str] = None
    closed_through_seq: Optional[int] = None
    close_reason: Optional[StopKind] = None

    def __post_init__(self) -> None:
        _count("forwarded_through_seq", self.forwarded_through_seq)
        if not isinstance(self.closed, bool):
            raise ValueError("closed must be a boolean")
        closure = (self.closed_at, self.closed_through_seq, self.close_reason)
        if not self.closed:
            if any(value is not None for value in closure):
                raise ValueError("an open mailbox has no closure fields")
            return
        if any(value is None for value in closure):
            raise ValueError("a closed mailbox requires closed_at, closed_through_seq and close_reason")
        _nonempty_str("closed_at", self.closed_at)
        through = _count("closed_through_seq", self.closed_through_seq)
        object.__setattr__(self, "close_reason", StopKind(self.close_reason))
        if self.forwarded_through_seq > through:
            raise ValueError("forwarded_through_seq cannot exceed closed_through_seq")

    def delivery(self, seq: int) -> MessageDelivery:
        """The delivery state of the record with this ``seq``."""
        _positive_int("seq", seq)
        if seq <= self.forwarded_through_seq:
            return MessageDelivery.FORWARDED
        return MessageDelivery.UNDELIVERED if self.closed else MessageDelivery.PENDING


@runtime_checkable
class SessionChannel(Protocol):
    """The session directory as the session backend sees it (injected).

    Implemented by ``llm_kit.jobs.session``; called only by the session
    backend in ``adapters/claude_session.py``, which cannot import ``jobs``.
    Every method follows the mailbox protocol of :class:`MailboxRecord`.
    """

    def poll(self, after_seq: int) -> Sequence[MailboxRecord]:
        """Records with ``seq > after_seq``, in ``seq`` order; empty once closed."""
        ...

    def mark_forwarded(self, seq: int) -> None:
        """Record that ``seq`` was written to stdin.

        Raises ``ValueError`` unless ``seq`` is ``forwarded_through_seq + 1``
        of an open mailbox.
        """
        ...

    def close(self, reason: StopKind, at: str) -> MailboxState:
        """Close the mailbox (idempotent) and return the final state."""
        ...

    def record_started(self, pid: int, at: str) -> None:
        """Record the Claude process id and spawn time in ``session.json``."""
        ...

    def record_first_init(self, at: str) -> None:
        """Record when the first ``system:init`` arrived (design 6.3 metric)."""
        ...


# ---------------------------------------------------------------------------
# session.json (design 11.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """``session.json``: the identity and end of one attempt's session.

    JSON shape: the field names below plus ``"schema": 1``; paths as strings,
    enums as their values. Written by ``llm_kit.jobs.session`` only: once
    before the session backend runs (identity), by the channel's
    ``record_started`` / ``record_first_init``, and once after the outcome.

    Invariants: ``session_id`` is a canonical UUID and is the id the process
    was started or resumed with; ``resumed`` is true exactly when the process
    was started with ``--resume``; ``cwd`` is absolute; the end fields
    (``ended_at``, ``stop_kind``, ``shutdown_confirmed``) are all set or all
    unset.
    """

    run_id: str
    job_id: str
    attempt_no: int
    session_id: str
    resumed: bool
    cwd: Path
    created_at: str
    pid: Optional[int] = None
    started_at: Optional[str] = None
    first_init_at: Optional[str] = None
    ended_at: Optional[str] = None
    stop_kind: Optional[StopKind] = None
    result_subtype: Optional[str] = None
    shutdown_confirmed: Optional[bool] = None

    def __post_init__(self) -> None:
        _nonempty_str("run_id", self.run_id)
        _nonempty_str("job_id", self.job_id)
        _positive_int("attempt_no", self.attempt_no)
        check_session_id(self.session_id)
        if not isinstance(self.resumed, bool):
            raise ValueError("resumed must be a boolean")
        cwd = Path(self.cwd)
        if not cwd.is_absolute():
            raise ValueError(f"cwd must be absolute, got {cwd}")
        object.__setattr__(self, "cwd", cwd)
        _nonempty_str("created_at", self.created_at)
        if self.pid is not None:
            _positive_int("pid", self.pid)
        end = (self.ended_at, self.stop_kind, self.shutdown_confirmed)
        if any(value is None for value in end) and any(value is not None for value in end):
            raise ValueError("ended_at, stop_kind and shutdown_confirmed are set together")
        if self.stop_kind is not None:
            object.__setattr__(self, "stop_kind", StopKind(self.stop_kind))


# ---------------------------------------------------------------------------
# Request and outcome (design 11.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRequest:
    """One agent-session attempt, as ``jobs/run.py`` hands it to the backend.

    - ``system`` / ``user``: the job prompt; ``user`` is the first stdin user
      message.
    - ``model``: the selected model; ``options``: the seam options built for
      the selected entry (effort, disallowed tools, system prompt mode,
      output contract, ``timeout_s``).
    - ``cwd``: the actual execution cwd, the declared repository subdirectory
      inside the workspace included (design 5.1). Absolute. ``options.cwd`` is
      ``None`` or equal to it.
    - ``session_id``: assigned at reservation and recorded on the live
      reservation before the process starts (design 5.1). With ``resume``
      false the process starts with ``--session-id <id>``; with ``resume``
      true it starts with ``--resume <id>`` (the job's ``resume_session``).
    - ``paths``: the attempt's session directory, already created.
    - ``bounds``: Q, B_res, B_eof, B_int.
    - ``channel``: the mailbox and session record port.

    Validation raises ``ValueError``; it does not check that paths exist.
    """

    system: str
    user: str
    model: str
    options: BackendOptions
    cwd: Path
    session_id: str
    resume: bool
    paths: SessionPaths
    bounds: SessionBounds
    channel: SessionChannel

    def __post_init__(self) -> None:
        if not isinstance(self.system, str):
            raise ValueError("system must be a string")
        _nonempty_str("user prompt", self.user)
        if not isinstance(self.model, str):
            raise ValueError("model must be a string")
        if not isinstance(self.options, BackendOptions):
            raise ValueError("options must be BackendOptions")
        cwd = Path(self.cwd)
        if not cwd.is_absolute():
            raise ValueError(f"cwd must be absolute, got {cwd}")
        object.__setattr__(self, "cwd", cwd)
        if self.options.cwd is not None and Path(self.options.cwd) != cwd:
            raise ValueError(f"options.cwd {self.options.cwd} differs from cwd {cwd}")
        check_session_id(self.session_id)
        if not isinstance(self.resume, bool):
            raise ValueError("resume must be a boolean")
        if not isinstance(self.paths, SessionPaths):
            raise ValueError("paths must be SessionPaths")
        if not isinstance(self.bounds, SessionBounds):
            raise ValueError("bounds must be SessionBounds")
        if not isinstance(self.channel, SessionChannel):
            raise ValueError("channel must implement SessionChannel")


@dataclass(frozen=True)
class SessionOutcome:
    """What one agent session returned (design 11.2).

    - ``response``: built from the LAST ``result`` event after stdout was
      drained through shutdown, with structured-output extraction and
      contract finalization applied and truthful
      ``execution_controls_applied`` (never ``no-session-persistence``).
      ``None`` when no result event was observed.
    - ``session_id`` / ``cwd``: as reported by the init event (the requested
      values when no init arrived).
    - ``stop_kind``: why the process was shut down.
    - ``result_subtype``: the chosen result's ``subtype``
      (``success``, ``error_during_execution``, ...).
    - ``shutdown_confirmed``: the process and every owned writer were
      confirmed stopped (leader exit alone is not enough, design 5.5).

    Invariants: ``result_subtype`` is set exactly when ``response`` is;
    ``quiescent`` implies a response; ``init_mismatch`` implies none. A caller
    must not run the contract, accept, or commit the worktree for an outcome
    with ``shutdown_confirmed`` false; it keeps unit ownership instead.
    """

    response: Optional[LLMResponse]
    session_id: str
    cwd: Path
    stop_kind: StopKind
    result_subtype: Optional[str]
    shutdown_confirmed: bool

    def __post_init__(self) -> None:
        if self.response is not None and not isinstance(self.response, LLMResponse):
            raise ValueError("response must be an LLMResponse or None")
        _nonempty_str("session_id", self.session_id)
        cwd = Path(self.cwd)
        if not cwd.is_absolute():
            raise ValueError(f"cwd must be absolute, got {cwd}")
        object.__setattr__(self, "cwd", cwd)
        kind = StopKind(self.stop_kind)
        object.__setattr__(self, "stop_kind", kind)
        if (self.response is None) != (self.result_subtype is None):
            raise ValueError("result_subtype is set exactly when response is")
        if self.result_subtype is not None:
            _nonempty_str("result_subtype", self.result_subtype)
        if kind is StopKind.QUIESCENT and self.response is None:
            raise ValueError("a quiescent outcome requires a response")
        if kind is StopKind.INIT_MISMATCH and self.response is not None:
            raise ValueError("an init_mismatch outcome carries no response")
        if not isinstance(self.shutdown_confirmed, bool):
            raise ValueError("shutdown_confirmed must be a boolean")


@runtime_checkable
class AgentSessionBackend(Protocol):
    """An agent-session backend (``adapters/claude_session.py``).

    ``run`` blocks for the whole attempt and returns a
    :class:`SessionOutcome`. It raises only for a request it cannot start
    (an unsupported contract, effort or option); every process end after the
    spawn is an outcome, never an exception.
    """

    name: str

    def run(self, request: SessionRequest) -> SessionOutcome:
        """Run one attempt to its end (design 5.5)."""
        ...


# ---------------------------------------------------------------------------
# Workspace finalization hook (design 6.1, 11.5)
# ---------------------------------------------------------------------------


class WorkspaceFinalizeStatus(str, Enum):
    """Result of the end-of-run commit (design 6.1).

    - ``clean``: nothing was left to commit.
    - ``committed``: remaining changes were committed to the unit branch.
    - ``dirty_held``: the commit failed or writers were not confirmed
      stopped; the worktree is held for a human.
    - ``not_applicable``: the workspace is not a branch-mode unit.
    """

    CLEAN = "clean"
    COMMITTED = "committed"
    DIRTY_HELD = "dirty_held"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class WorkspaceFinalizeResult:
    """What :meth:`SessionWorkspaceHook.finalize_run` did.

    Invariant: ``commit_sha`` is set exactly when ``status`` is
    ``committed``.
    """

    status: WorkspaceFinalizeStatus
    commit_sha: Optional[str] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        status = WorkspaceFinalizeStatus(self.status)
        object.__setattr__(self, "status", status)
        if (status is WorkspaceFinalizeStatus.COMMITTED) != (self.commit_sha is not None):
            raise ValueError("commit_sha is set exactly when status is committed")
        if self.commit_sha is not None:
            _nonempty_str("commit_sha", self.commit_sha)


@runtime_checkable
class SessionWorkspaceHook(Protocol):
    """The end-of-run hook ``jobs/run.py`` calls; branch mode implements it.

    Called once per job when its run ends for ANY reason (accepted, rejected,
    failed, stopped, timed out), from a ``finally`` reached by every exit
    path, after all attempts and the contract, while unit ownership is still
    held (design 6.1, 11.5). Acquisition is unchanged:
    ``WorkspaceManager.prepare(job, attempt_no) -> WorkspaceResolution``.

    Keyword arguments:

    - ``run_id`` / ``job_id``: the ledger identity.
    - ``working_directory``: the worktree root of the unit (not the
      repository subdirectory).
    - ``writers_stopped``: every owned writer was confirmed stopped. When
      false the hook must not commit; it returns ``dirty_held``.
    - ``final_state``: the job's ``JobState`` value when the run ended, or
      ``None`` when the job is not terminal.
    - ``stop_kind``: the last attempt's :class:`StopKind`, ``None`` for a
      completion-mode or never-started attempt.

    Crash-path finalization from ``jobs cancel`` and supervisor settlement is
    item 2's and is not part of this signature.
    """

    def finalize_run(
        self,
        *,
        run_id: str,
        job_id: str,
        working_directory: Path,
        writers_stopped: bool,
        final_state: Optional[str],
        stop_kind: Optional[StopKind],
    ) -> WorkspaceFinalizeResult:
        """Commit remaining changes to the unit branch, or hold the worktree."""
        ...


# ---------------------------------------------------------------------------
# Ledger schema 14 (design 11.2)
# ---------------------------------------------------------------------------

#: The ledger schema that adds the session columns; ``jobs/store.py`` names
#: its constant for it ``SESSION_SCHEMA``.
SESSION_LEDGER_SCHEMA = 14
#: The tables that each gain every :data:`SESSION_LEDGER_COLUMNS` column.
SESSION_LEDGER_TABLES: tuple[str, ...] = ("attempts", "reservations")


@dataclass(frozen=True)
class LedgerColumn:
    """One ledger schema 14 column.

    ``name`` is the SQL column, the model attribute on ``Attempt`` and
    ``AttemptReservation``, the ``to_mapping`` key, and the
    ``reserve_attempt`` / ``append_attempt`` keyword. ``sql`` is the exact
    ``ALTER TABLE ... ADD COLUMN`` type clause. ``pre_v14`` is the value a
    row written before schema 14 has (the SQL default after migration, and the
    value ``LedgerReader`` supplies for an older ledger); ``None`` is NULL.
    ``python_type`` is the model annotation.
    """

    name: str
    sql: str
    pre_v14: Optional[str]
    python_type: str
    meaning: str


SESSION_LEDGER_COLUMNS: tuple[LedgerColumn, ...] = (
    LedgerColumn(
        "session_mode", "TEXT NOT NULL DEFAULT 'completion'", "completion", "SessionMode",
        "completion or agent. Every other session column is NULL for completion.",
    ),
    LedgerColumn(
        "session_id", "TEXT", None, "Optional[str]",
        "Agent: the Claude session id, set on the reservation at reserve time, "
        "before any attempt row exists, and copied to the attempt row.",
    ),
    LedgerColumn(
        "session_cwd", "TEXT", None, "Optional[Path]",
        "Agent: reservation = the requested execution cwd; attempt = the cwd the "
        "init event reported (equal after validation).",
    ),
    LedgerColumn(
        "session_dir", "TEXT", None, "Optional[Path]",
        "Agent: SessionPaths.attempt_dir, set at reserve time.",
    ),
    LedgerColumn(
        "stop_kind", "TEXT", None, "Optional[StopKind]",
        "Agent: SessionOutcome.stop_kind, written with the attempt row and onto "
        "its reservation in the same transaction; NULL on a live, lost or "
        "pre-invoke-resolved reservation and after a hard cancel.",
    ),
)


__all__ = [
    "AGENT_PERMISSION_MODE",
    "ATTEMPT_DIR_PREFIX",
    "AgentSessionBackend",
    "BOUND_OPTION_KEYS",
    "DEFAULT_EOF_WAIT_S",
    "DEFAULT_QUIESCENCE_S",
    "DEFAULT_RESULT_WAIT_S",
    "DEFAULT_SIGINT_WAIT_S",
    "DEFAULT_SYSTEM_PROMPT_MODE",
    "EVENTS_FILE",
    "EVENT_LINE_SCHEMA_FILE",
    "LedgerColumn",
    "MAILBOX_FILE",
    "MAILBOX_LOCK_FILE",
    "MAILBOX_RECORD_SCHEMA_FILE",
    "MAILBOX_STATE_FILE",
    "MAILBOX_STATE_SCHEMA_FILE",
    "MailboxRecord",
    "MailboxRecordKind",
    "MailboxState",
    "MessageDelivery",
    "OPTION_EOF_WAIT_S",
    "OPTION_QUIESCENCE_S",
    "OPTION_RESULT_WAIT_S",
    "OPTION_RESUME_SESSION",
    "OPTION_SESSION",
    "OPTION_SIGINT_WAIT_S",
    "SCHEMA_DIRECTORY",
    "SCHEMA_PACKAGE",
    "SESSION_FILES_SCHEMA_VERSION",
    "SESSION_LEDGER_COLUMNS",
    "SESSION_LEDGER_SCHEMA",
    "SESSION_LEDGER_TABLES",
    "SESSION_OPTION_KEYS",
    "SESSION_RECORD_FILE",
    "SESSION_RECORD_SCHEMA_FILE",
    "SESSION_SCHEMA_FILES",
    "SessionBounds",
    "SessionChannel",
    "SessionMode",
    "SessionOptions",
    "SessionOutcome",
    "SessionPaths",
    "SessionRecord",
    "SessionRequest",
    "SessionWorkspaceHook",
    "StopKind",
    "StopMode",
    "StopRequest",
    "StopRequestKind",
    "WorkspaceFinalizeResult",
    "WorkspaceFinalizeStatus",
    "check_session_id",
    "encode_job_id",
]
