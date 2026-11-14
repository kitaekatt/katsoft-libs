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
module: it imports the standard library, :mod:`llm_kit.completion.types` and
the halt-kind constants of :mod:`llm_kit.completion.halt`.

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
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

from llm_kit.completion.halt import (
    HALT_AUTH,
    HALT_BACKPRESSURE,
    HALT_INSUFFICIENT_CREDIT,
    HALT_QUOTA,
    HALT_RATE_LIMIT,
)
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
#: Job option for the finalization budget: the end-of-run commit (design 6.1).
OPTION_FINALIZE_S = "session_finalize_s"

#: The five bound option keys, in Q, B_res, B_eof, B_int, finalize order.
BOUND_OPTION_KEYS: tuple[str, ...] = (
    OPTION_QUIESCENCE_S,
    OPTION_RESULT_WAIT_S,
    OPTION_EOF_WAIT_S,
    OPTION_SIGINT_WAIT_S,
    OPTION_FINALIZE_S,
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
#: Finalization budget default, seconds. The end-of-run commit is a
#: ``git status`` / ``git add -A`` / ``git commit`` in one worktree, plus any
#: commit hooks. Two minutes covers a large worktree and ordinary hooks; a
#: commit that overruns it fails safe (the worktree is marked ``dirty-held``,
#: nothing is lost), so a larger default would only inflate every attempt's
#: share of the supervisor budget. ``jobs/workspace.py``'s 900 s
#: ``GIT_TREE_TIMEOUT_S`` covers ``worktree add``, a different operation.
DEFAULT_FINALIZE_S = 120.0

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
    - ``finalize_s``: the budget for the end-of-run commit (design 6.1).

    Deadline rule (design 11.3): Q counts INSIDE the attempt timeout; the
    shutdown allowance and ``finalize_s`` are added per attempt ON TOP of it
    (:class:`AttemptBudget`).

    Invariant: every bound is finite and strictly positive. Construction with
    any other value raises ``ValueError``; values are stored as ``float``.
    """

    quiescence_s: float = DEFAULT_QUIESCENCE_S
    result_wait_s: float = DEFAULT_RESULT_WAIT_S
    eof_wait_s: float = DEFAULT_EOF_WAIT_S
    sigint_wait_s: float = DEFAULT_SIGINT_WAIT_S
    finalize_s: float = DEFAULT_FINALIZE_S

    def __post_init__(self) -> None:
        object.__setattr__(self, "quiescence_s", _bound("quiescence_s", self.quiescence_s))
        object.__setattr__(self, "result_wait_s", _bound("result_wait_s", self.result_wait_s))
        object.__setattr__(self, "eof_wait_s", _bound("eof_wait_s", self.eof_wait_s))
        object.__setattr__(self, "sigint_wait_s", _bound("sigint_wait_s", self.sigint_wait_s))
        object.__setattr__(self, "finalize_s", _bound("finalize_s", self.finalize_s))

    @property
    def shutdown_allowance_s(self) -> float:
        """B_res + B_eof + B_int: the worst case from Stop to a forced kill.

        It excludes Q (inside the attempt timeout) and ``finalize_s``.
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
            finalize_s=options.get(OPTION_FINALIZE_S, DEFAULT_FINALIZE_S),  # type: ignore[arg-type]
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
#: The :class:`CleanupRecord` JSON object, replaced atomically: the durable
#: identity evidence for stopping the session's processes after a crash.
#: Single writer: ``llm_kit.jobs.session`` through
#: :meth:`SessionChannel.record_cleanup`.
CLEANUP_FILE = "cleanup.json"
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

    @property
    def cleanup(self) -> Path:
        """``cleanup.json``."""
        return self.attempt_dir / CLEANUP_FILE


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
#: ``cleanup.json`` (:class:`CleanupRecord`).
CLEANUP_RECORD_SCHEMA_FILE = "cleanup_record.json"
#: Every schema file, for a test that each exists and is loadable.
SESSION_SCHEMA_FILES: tuple[str, ...] = (
    MAILBOX_RECORD_SCHEMA_FILE,
    MAILBOX_STATE_SCHEMA_FILE,
    SESSION_RECORD_SCHEMA_FILE,
    EVENT_LINE_SCHEMA_FILE,
    CLEANUP_RECORD_SCHEMA_FILE,
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


# ---------------------------------------------------------------------------
# Durable cleanup identity (design 5.5, 11.2)
# ---------------------------------------------------------------------------

#: Environment variable carrying the session's membership tag into Claude and
#: every descendant it starts. A tag alone never selects a process to stop:
#: a process is the session's when its (pid, create_time) is recorded, or when
#: it carries the tag AND descends from a recorded identity (the relay rule,
#: ``relay/resume.py`` module docstring).
SESSION_TAG_ENV = "LLM_KIT_SESSION_TAG"

_TAG = re.compile(r"[A-Za-z0-9_-]{8,128}")


def check_process_tag(value: object) -> str:
    """Return ``value`` when it is a valid membership tag (8-128 of ``[A-Za-z0-9_-]``).

    Raises ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or _TAG.fullmatch(value) is None:
        raise ValueError(f"process tag must match [A-Za-z0-9_-]{{8,128}}, got {value!r}")
    return value


@dataclass(frozen=True)
class ProcessIdentity:
    """A process as recorded for later identity-bound cleanup.

    The same shape as ``llm_kit.jobs._supervisor_process.ProcessIdentity``
    (``{"pid", "create_time"}``), which adapters cannot import. A pid alone is
    never evidence: a live process matches only when its creation time equals
    ``create_time``. Invariants: ``pid`` is a positive int; ``create_time`` is
    a finite positive number (seconds since the epoch, as psutil reports it).
    """

    pid: int
    create_time: float

    def __post_init__(self) -> None:
        _positive_int("pid", self.pid)
        if isinstance(self.create_time, bool) or not isinstance(self.create_time, (int, float)):
            raise ValueError(f"create_time must be a number, got {self.create_time!r}")
        created = float(self.create_time)
        if not math.isfinite(created) or created <= 0:
            raise ValueError(f"create_time must be finite and positive, got {self.create_time!r}")
        object.__setattr__(self, "create_time", created)


@dataclass(frozen=True)
class CleanupRecord:
    """``cleanup.json``: what a later sweep needs to stop this session's processes.

    JSON shape: ``{"schema": 1, "tag": str, "leader": {"pid", "create_time"},
    "process_group": int|null, "members": [{"pid", "create_time"}, ...],
    "started_at": str, "updated_at": str, "confirmed_stopped_at": str|null}``.

    - ``tag``: the value of :data:`SESSION_TAG_ENV` in the child; equal to
      ``SessionRecord.process_tag``, which is written BEFORE the spawn, so a
      crash between spawn and the first cleanup record still leaves the tag.
    - ``leader``: the Claude process. ``process_group``: its POSIX process
      group (``start_new_session=True``), ``None`` on Windows.
    - ``members``: every descendant identity observed so far, leader excluded.
    - ``confirmed_stopped_at``: set once the leader and every member were
      confirmed gone by identity. ``SessionOutcome.shutdown_confirmed`` reports
      the conclusion; this record is the evidence a later sweep re-checks.

    Invariants: no duplicate member; the leader is not a member; successive
    writes for one attempt keep ``tag``, ``leader``, ``process_group`` and
    ``started_at``, and ``members`` only grows (the writer refuses otherwise).
    The session backend writes the first record immediately after spawn and a
    new one whenever it observes a new descendant.
    """

    tag: str
    leader: ProcessIdentity
    started_at: str
    updated_at: str
    members: tuple[ProcessIdentity, ...] = ()
    process_group: Optional[int] = None
    confirmed_stopped_at: Optional[str] = None

    def __post_init__(self) -> None:
        check_process_tag(self.tag)
        if not isinstance(self.leader, ProcessIdentity):
            raise ValueError("leader must be a ProcessIdentity")
        _nonempty_str("started_at", self.started_at)
        _nonempty_str("updated_at", self.updated_at)
        members = tuple(self.members)
        if not all(isinstance(member, ProcessIdentity) for member in members):
            raise ValueError("members must be ProcessIdentity values")
        if len(set(members)) != len(members):
            raise ValueError("members must not repeat")
        if self.leader in members:
            raise ValueError("the leader is not a member")
        object.__setattr__(self, "members", members)
        if self.process_group is not None:
            _positive_int("process_group", self.process_group)
        if self.confirmed_stopped_at is not None:
            _nonempty_str("confirmed_stopped_at", self.confirmed_stopped_at)


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

    def record_cleanup(self, record: CleanupRecord) -> None:
        """Replace ``cleanup.json`` with ``record``, durably (fsync, atomic).

        Raises ``ValueError`` when ``record`` breaks the :class:`CleanupRecord`
        succession invariants or its tag differs from the session record's.
        """
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
    before the session backend spawns (identity and ``process_tag``), by the
    channel's ``record_first_init``, and once after the outcome. Process
    identities live in ``cleanup.json`` (:class:`CleanupRecord`), not here.

    Invariants: ``session_id`` is a canonical UUID and is the id the process
    was started or resumed with; ``resumed`` is true exactly when the process
    was started with ``--resume``; ``cwd`` is absolute; ``process_tag`` is a
    valid tag; the end fields (``ended_at``, ``stop_kind``,
    ``shutdown_confirmed``) are all set or all unset.
    """

    run_id: str
    job_id: str
    attempt_no: int
    session_id: str
    resumed: bool
    cwd: Path
    process_tag: str
    created_at: str
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
        check_process_tag(self.process_tag)
        _nonempty_str("created_at", self.created_at)
        end =(self.ended_at, self.stop_kind, self.shutdown_confirmed)
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
    - ``process_tag``: the membership tag, already in ``session.json``; the
      backend sets :data:`SESSION_TAG_ENV` to it in the child environment.
    - ``paths``: the attempt's session directory, already created.
    - ``bounds``: Q, B_res, B_eof, B_int, finalize. ``options.timeout_s`` is
      the attempt timeout; the backend's activity deadline is that timeout,
      and the shutdown it then performs is bounded by
      ``bounds.shutdown_allowance_s`` on top of it (design 11.3).
    - ``channel``: the mailbox, session record and cleanup record port.

    Validation raises ``ValueError``; it does not check that paths exist.
    """

    system: str
    user: str
    model: str
    options: BackendOptions
    cwd: Path
    session_id: str
    resume: bool
    process_tag: str
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
        check_process_tag(self.process_tag)
        if not isinstance(self.paths, SessionPaths):
            raise ValueError("paths must be SessionPaths")
        if not isinstance(self.bounds, SessionBounds):
            raise ValueError("bounds must be SessionBounds")
        if not isinstance(self.channel, SessionChannel):
            raise ValueError("channel must implement SessionChannel")


class SessionFailureKind(str, Enum):
    """What went wrong in a session, independent of :class:`StopKind`.

    - ``timeout``: the attempt timeout was reached before quiescence.
    - ``init_mismatch``: an init event's session id, cwd or permission mode was
      missing, invalid or different from the request.
    - ``no_init``: the process ended without any init event.
    - ``no_result``: an init arrived but no result event did.
    - ``nonzero_exit``: the process exited with a nonzero status.
    - ``result_error``: the chosen result reports ``is_error`` (for example
      ``error_during_execution`` or ``error_max_turns``).

    When several apply, the backend reports the first in
    :data:`FAILURE_PRECEDENCE`, the order the relay already decides in
    (``relay/resume.py``: timeout, mismatch, no init, no result, exit
    status, error result).
    """

    TIMEOUT = "timeout"
    INIT_MISMATCH = "init_mismatch"
    NO_INIT = "no_init"
    NO_RESULT = "no_result"
    NONZERO_EXIT = "nonzero_exit"
    RESULT_ERROR = "result_error"


#: The order in which the backend picks the one reported failure kind.
FAILURE_PRECEDENCE: tuple[SessionFailureKind, ...] = (
    SessionFailureKind.TIMEOUT,
    SessionFailureKind.INIT_MISMATCH,
    SessionFailureKind.NO_INIT,
    SessionFailureKind.NO_RESULT,
    SessionFailureKind.NONZERO_EXIT,
    SessionFailureKind.RESULT_ERROR,
)

#: Halt kinds a session failure may carry: the vocabulary of
#: :mod:`llm_kit.completion.halt`, which ``jobs/run.py`` already classifies,
#: records and acts on (persistent halts, quota write-back, backpressure).
SESSION_HALT_KINDS = frozenset(
    {HALT_AUTH, HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT, HALT_QUOTA, HALT_BACKPRESSURE}
)


@dataclass(frozen=True)
class SessionFailure:
    """Why a session did not produce a clean success, with its evidence.

    - ``kind``: :class:`SessionFailureKind`.
    - ``message``: human detail (the stderr tail, the result text or the
      init problem); nonempty.
    - ``halt_kind``: the provider halt the failure shows, classified by the
      backend with the existing Claude classifiers (``auth`` for a logged-out
      CLI, ``rate_limit`` for a usage cap, ...), or ``None``. Independent of
      ``kind``: an auth failure may be a ``nonzero_exit`` or a
      ``result_error``.
    - ``exit_code``: the process exit status when it was reaped (negative for
      a POSIX signal), else ``None``.
    - ``retry_after_s``: a provider retry hint for ``backpressure``.
    - ``observed_session_id`` / ``observed_cwd``: the init event's raw
      values (verbatim when a string, ``repr()`` otherwise) when they were
      missing, invalid or mismatched; ``None`` otherwise. The outcome itself
      then carries the REQUESTED identity (:class:`SessionOutcome`).

    This lets ``jobs/run.py`` keep its existing diagnostic and retry
    decisions (halt recording, TIMEOUT status, backpressure waits) from an
    outcome, without an exception after spawn and without a fabricated
    result.
    """

    kind: SessionFailureKind
    message: str
    halt_kind: Optional[str] = None
    exit_code: Optional[int] = None
    retry_after_s: Optional[float] = None
    observed_session_id: Optional[str] = None
    observed_cwd: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", SessionFailureKind(self.kind))
        _nonempty_str("failure message", self.message)
        if self.halt_kind is not None and self.halt_kind not in SESSION_HALT_KINDS:
            allowed = ", ".join(sorted(SESSION_HALT_KINDS))
            raise ValueError(f"halt_kind must be one of: {allowed}; got {self.halt_kind!r}")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError(f"exit_code must be an integer, got {self.exit_code!r}")
        if self.kind is SessionFailureKind.NONZERO_EXIT and not self.exit_code:
            raise ValueError("a nonzero_exit failure requires a nonzero exit_code")
        if self.retry_after_s is not None:
            if self.halt_kind != HALT_BACKPRESSURE:
                raise ValueError("retry_after_s is only carried with halt_kind backpressure")
            if isinstance(self.retry_after_s, bool) or not isinstance(self.retry_after_s, (int, float)):
                raise ValueError("retry_after_s must be a number")
            if not math.isfinite(self.retry_after_s) or self.retry_after_s < 0:
                raise ValueError("retry_after_s must be finite and non-negative")
        for name in ("observed_session_id", "observed_cwd"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")


@dataclass(frozen=True)
class SessionOutcome:
    """What one agent session returned (design 11.2).

    - ``response``: built from the LAST ``result`` event after stdout was
      drained through shutdown, with structured-output extraction and
      contract finalization applied and truthful
      ``execution_controls_applied`` (never ``no-session-persistence``).
      ``None`` when no result event arrived: a result is never fabricated.
    - ``session_id`` / ``cwd``: the init event's values when the init was
      valid; otherwise (no init, or a missing, invalid or mismatched field)
      the REQUESTED values, with the observed ones kept in
      ``failure.observed_*``. So both are always a valid identity.
    - ``stop_kind``: why the process was shut down.
    - ``result_subtype``: the chosen result's ``subtype``
      (``success``, ``error_during_execution``, ...).
    - ``shutdown_confirmed``: the process and every owned writer were
      confirmed stopped (leader exit alone is not enough, design 5.5). The
      evidence is ``cleanup.json``, not this flag.
    - ``failure``: :class:`SessionFailure`, or ``None`` for a clean success.

    Invariants: ``result_subtype`` is set exactly when ``response`` is;
    ``quiescent`` implies a response; ``init_mismatch`` stop implies no
    response and a failure of kind ``init_mismatch`` (and that kind only with
    that stop); ``timeout`` stop implies a failure of kind ``timeout``; an
    outcome without a response has a failure unless an operator stopped it.
    A caller must not run the contract, accept, or commit the worktree for an
    outcome with ``shutdown_confirmed`` false; it keeps unit ownership
    instead.
    """

    response: Optional[LLMResponse]
    session_id: str
    cwd: Path
    stop_kind: StopKind
    result_subtype: Optional[str]
    shutdown_confirmed: bool
    failure: Optional[SessionFailure] = None

    def __post_init__(self) -> None:
        if self.response is not None and not isinstance(self.response, LLMResponse):
            raise ValueError("response must be an LLMResponse or None")
        check_session_id(self.session_id)
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
        if not isinstance(self.shutdown_confirmed, bool):
            raise ValueError("shutdown_confirmed must be a boolean")
        failure = self.failure
        if failure is not None and not isinstance(failure, SessionFailure):
            raise ValueError("failure must be a SessionFailure or None")
        failure_kind = failure.kind if failure is not None else None
        if kind is StopKind.QUIESCENT and self.response is None:
            raise ValueError("a quiescent outcome requires a response")
        if kind is StopKind.INIT_MISMATCH and self.response is not None:
            raise ValueError("an init_mismatch outcome carries no response")
        if (kind is StopKind.INIT_MISMATCH) != (failure_kind is SessionFailureKind.INIT_MISMATCH):
            raise ValueError("stop_kind init_mismatch and failure kind init_mismatch go together")
        if kind is StopKind.TIMEOUT and failure_kind is not SessionFailureKind.TIMEOUT:
            raise ValueError("a timeout outcome requires a timeout failure")
        if self.response is None and failure is None and not kind.is_operator:
            raise ValueError("an outcome without a response requires a failure")


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
# Deadlines and the supervisor budget (design 11.3)
# ---------------------------------------------------------------------------


def _seconds(name: str, value: object, *, positive: bool) -> float:
    """Return ``value`` as finite seconds; positive or non-negative as asked."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if number < 0 or (positive and number == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'non-negative'}, got {value!r}")
    return number


class SupervisorBudgetError(ValueError):
    """The explicit supervisor budget does not cover the run's worst case.

    Raised by :func:`check_supervisor_budget` before any reservation, so a
    run that the run-wide supervisor deadline could cut off mid-shutdown or
    mid-commit never starts. ``budget_s`` and ``required_s`` are attributes.
    """

    def __init__(self, budget_s: float, required_s: float) -> None:
        super().__init__(
            f"supervisor budget {budget_s:g} s does not cover the run's worst case "
            f"{required_s:g} s (attempt timeouts, shutdown allowances, finalization, "
            "backpressure and overhead across all jobs, attempts and max_parallel)"
        )
        self.budget_s = budget_s
        self.required_s = required_s


@dataclass(frozen=True)
class AttemptBudget:
    """The worst-case wall time of one attempt (design 11.3).

    - ``timeout_s``: the attempt timeout (``jobs run --timeout``). Session
      activity INCLUDING every Q window, and the contract, run inside it;
      the contract gets the timeout minus the session's counted elapsed time,
      as today (``jobs/run.py``, ``contract_timeout_s``).
    - ``shutdown_allowance_s``: added on top; ``SessionBounds
      .shutdown_allowance_s`` for an agent job, 0 for a completion job.
    - ``finalize_s``: added on top; ``SessionBounds.finalize_s`` for a job
      whose workspace makes an end-of-run commit, else 0. Counted per attempt
      (conservative: the commit runs once per job run).
    - ``backpressure_cap_s``: the ``BackpressurePolicy.cap_s`` a seam call may
      wait out, added on top (conservative).
    - ``overhead_s``: per-attempt setup the caller budgets (workspace
      creation, project setup); 0 when none.

    Invariants: ``timeout_s`` is finite and positive; every other field is
    finite and non-negative.
    """

    timeout_s: float
    shutdown_allowance_s: float = 0.0
    finalize_s: float = 0.0
    backpressure_cap_s: float = 0.0
    overhead_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeout_s", _seconds("timeout_s", self.timeout_s, positive=True))
        for name in ("shutdown_allowance_s", "finalize_s", "backpressure_cap_s", "overhead_s"):
            object.__setattr__(self, name, _seconds(name, getattr(self, name), positive=False))

    @classmethod
    def for_session(
        cls,
        timeout_s: float,
        mode: SessionMode,
        bounds: SessionBounds,
        *,
        finalizes: bool,
        backpressure_cap_s: float = 0.0,
        overhead_s: float = 0.0,
    ) -> "AttemptBudget":
        """The budget of one attempt of a job in ``mode`` with ``bounds``.

        ``finalizes`` is true when the job's workspace makes an end-of-run
        commit (branch mode, item 2); it adds ``bounds.finalize_s``.
        """
        agent = SessionMode(mode) is SessionMode.AGENT
        return cls(
            timeout_s=timeout_s,
            shutdown_allowance_s=bounds.shutdown_allowance_s if agent else 0.0,
            finalize_s=bounds.finalize_s if finalizes else 0.0,
            backpressure_cap_s=backpressure_cap_s,
            overhead_s=overhead_s,
        )

    @property
    def worst_case_s(self) -> float:
        """The sum of every component: the longest one attempt can take."""
        return (
            self.timeout_s + self.shutdown_allowance_s + self.finalize_s
            + self.backpressure_cap_s + self.overhead_s
        )


@dataclass(frozen=True)
class JobBudget:
    """The worst case of one job: ``max_attempts`` attempts of ``attempt``.

    Invariant: ``max_attempts`` is a positive int.
    """

    attempt: AttemptBudget
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptBudget):
            raise ValueError("attempt must be an AttemptBudget")
        _positive_int("max_attempts", self.max_attempts)

    @property
    def worst_case_s(self) -> float:
        """``max_attempts`` times the attempt's worst case."""
        return self.max_attempts * self.attempt.worst_case_s


def required_supervisor_budget_s(
    jobs: Sequence[JobBudget], max_parallel: int, *, run_overhead_s: float = 0.0
) -> float:
    """The worst-case wall time of a supervised ``jobs run`` (design 11.3).

    The supervisor deadline is run-wide (``jobs/_supervisor_worker.py``, the
    ``spec.timeout_s`` loop), so it must cover every job that may still run.
    Jobs are independent and driven by a pool of ``max_parallel`` workers
    that never idles while a job is pending, so the greedy list-scheduling
    bound holds: with ``m = min(max_parallel, len(jobs))`` workers, the run
    takes at most ``sum / m + (1 - 1/m) * max`` of the job worst cases
    (``sum`` when ``m`` is 1). ``run_overhead_s`` (startup, selection, ledger
    work) is added once. Raises ``ValueError`` for invalid input.
    """
    _positive_int("max_parallel", max_parallel)
    overhead = _seconds("run_overhead_s", run_overhead_s, positive=False)
    budgets = tuple(jobs)
    if not all(isinstance(job, JobBudget) for job in budgets):
        raise ValueError("jobs must be JobBudget values")
    if not budgets:
        return overhead
    costs = [job.worst_case_s for job in budgets]
    workers = min(max_parallel, len(costs))
    total = sum(costs)
    if workers == 1:
        return overhead + total
    return overhead + total / workers + (1 - 1 / workers) * max(costs)


def check_supervisor_budget(
    budget_s: float,
    jobs: Sequence[JobBudget],
    max_parallel: int,
    *,
    run_overhead_s: float = 0.0,
) -> float:
    """Refuse a run whose explicit supervisor budget is too small.

    ``budget_s`` is the explicit supervisor-budget input of ``jobs run`` (the
    run-wide deadline the supervisor enforces). ``jobs`` are the run's
    non-terminal jobs. Returns the required budget. Raises
    :class:`SupervisorBudgetError` when ``budget_s`` is smaller, and
    ``ValueError`` for invalid input. Called before any reservation.
    """
    budget = _seconds("budget_s", budget_s, positive=True)
    required = required_supervisor_budget_s(jobs, max_parallel, run_overhead_s=run_overhead_s)
    if budget < required:
        raise SupervisorBudgetError(budget, required)
    return required


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
    "AgentSessionBackend",
    "ATTEMPT_DIR_PREFIX",
    "AttemptBudget",
    "BOUND_OPTION_KEYS",
    "check_process_tag",
    "check_session_id",
    "check_supervisor_budget",
    "CLEANUP_FILE",
    "CLEANUP_RECORD_SCHEMA_FILE",
    "CleanupRecord",
    "DEFAULT_EOF_WAIT_S",
    "DEFAULT_FINALIZE_S",
    "DEFAULT_QUIESCENCE_S",
    "DEFAULT_RESULT_WAIT_S",
    "DEFAULT_SIGINT_WAIT_S",
    "DEFAULT_SYSTEM_PROMPT_MODE",
    "encode_job_id",
    "EVENT_LINE_SCHEMA_FILE",
    "EVENTS_FILE",
    "FAILURE_PRECEDENCE",
    "JobBudget",
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
    "OPTION_FINALIZE_S",
    "OPTION_QUIESCENCE_S",
    "OPTION_RESULT_WAIT_S",
    "OPTION_RESUME_SESSION",
    "OPTION_SESSION",
    "OPTION_SIGINT_WAIT_S",
    "ProcessIdentity",
    "required_supervisor_budget_s",
    "SCHEMA_DIRECTORY",
    "SCHEMA_PACKAGE",
    "SESSION_FILES_SCHEMA_VERSION",
    "SESSION_HALT_KINDS",
    "SESSION_LEDGER_COLUMNS",
    "SESSION_LEDGER_SCHEMA",
    "SESSION_LEDGER_TABLES",
    "SESSION_OPTION_KEYS",
    "SESSION_RECORD_FILE",
    "SESSION_RECORD_SCHEMA_FILE",
    "SESSION_SCHEMA_FILES",
    "SESSION_TAG_ENV",
    "SessionBounds",
    "SessionChannel",
    "SessionFailure",
    "SessionFailureKind",
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
    "SupervisorBudgetError",
    "WorkspaceFinalizeResult",
    "WorkspaceFinalizeStatus",
]
