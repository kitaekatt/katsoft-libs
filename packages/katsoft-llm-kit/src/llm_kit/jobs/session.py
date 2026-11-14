"""The per-attempt session directory of an agent session: mailbox, state and records.

One agent-session attempt owns a directory (:class:`SessionPaths`) holding the
mailbox an operator appends to, the mailbox state, ``session.json`` and
``cleanup.json``. This module is the only writer of those files except
``events.jsonl``, which the session backend writes. It implements the
:class:`~llm_kit.completion.session_types.SessionChannel` the backend is
handed (:class:`FileSessionChannel`) and the operator-side calls
(:func:`append_message`, :func:`request_stop`, :func:`read_state`,
:func:`read_records`). The layout, record shapes and invariants are fixed by
``llm_kit.completion.session_types``; the JSON Schema of each file is in
``llm_kit/jobs/schemas/``.

Synchronization (design 5.3): one lock file, ``mailbox.lock``, serializes every
mutation and every read of the other files, through
:func:`llm_kit.storage.file_lock.file_lock`, so the protocol is the same in
every process and on every platform. A mailbox append is one ``O_APPEND``
write of one JSON line, then ``fsync``, under the lock; ``seq`` is assigned
under the same lock. State, session and cleanup files are replaced atomically
(temporary file, ``fsync``, rename). A final line without a newline is a torn
write: it is not a record, it is never counted for ``seq``, and the next
append removes it before writing.

Every reader refuses a file that does not match its format (unknown or missing
keys, a wrong ``schema`` value, a malformed or non-consecutive mailbox line)
with ``ValueError`` naming the file. A mutating call on an attempt directory
that does not exist raises ``FileNotFoundError``; only
:func:`create_attempt_dir` creates it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Optional
import uuid

from llm_kit.completion.session_types import (
    SESSION_FILES_SCHEMA_VERSION,
    CleanupRecord,
    DetachedSurvivor,
    MailboxRecord,
    MailboxRecordKind,
    MailboxState,
    ProcessIdentity,
    SessionPaths,
    SessionRecord,
    StopKind,
    StopMode,
    StopRequest,
    StopRequestKind,
)
from llm_kit.storage import atomic_json
from llm_kit.storage.file_lock import file_lock

_BINARY = getattr(os, "O_BINARY", 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Locking and file plumbing
# ---------------------------------------------------------------------------


def _require_dir(paths: SessionPaths) -> None:
    if not isinstance(paths, SessionPaths):
        raise TypeError(f"paths must be a SessionPaths, got {type(paths).__name__}")
    if not paths.attempt_dir.is_dir():
        raise FileNotFoundError(f"session directory does not exist: {paths.attempt_dir}")


@contextmanager
def _locked(paths: SessionPaths) -> Iterator[None]:
    _require_dir(paths)
    with file_lock(paths.mailbox_lock):
        yield


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # a directory cannot be opened for fsync on Windows
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_object(text: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _exact(document: Mapping[str, Any], keys: Sequence[str], label: str) -> None:
    schema = document.get("schema")
    if type(schema) is not int or schema != SESSION_FILES_SCHEMA_VERSION:
        raise ValueError(f"{label} has unsupported schema {schema!r}; expected {SESSION_FILES_SCHEMA_VERSION}")
    expected = {"schema", *keys}
    missing = sorted(expected - document.keys())
    extra = sorted(document.keys() - expected)
    if missing or extra:
        raise ValueError(f"{label} has missing keys {missing} and unknown keys {extra}")


def _string_or_none(document: Mapping[str, Any], key: str, label: str) -> Optional[str]:
    value = document[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label}: {key} must be a string or null, got {value!r}")
    return value


def _read_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{label} is not UTF-8 text: {exc}") from None
    return _parse_object(text, f"{label} ({path})")


def _write_json_file(path: Path, document: dict[str, Any]) -> None:
    atomic_json.write(path, document)
    _fsync_directory(path.parent)


# ---------------------------------------------------------------------------
# Record <-> JSON
# ---------------------------------------------------------------------------

_RECORD_KEYS = ("seq", "id", "at", "kind", "text", "stop")
_STATE_KEYS = ("forwarded_through_seq", "closed", "closed_at", "closed_through_seq", "close_reason")
_SESSION_KEYS = (
    "run_id", "job_id", "attempt_no", "session_id", "resumed", "cwd", "process_tag", "created_at",
    "first_init_at", "ended_at", "stop_kind", "result_subtype", "shutdown_confirmed",
)
_CLEANUP_KEYS = (
    "tag", "leader", "process_group", "members", "started_at", "updated_at", "confirmed_stopped_at",
    "detached_survivors",
)


def _record_document(record: MailboxRecord) -> dict[str, Any]:
    return {
        "schema": SESSION_FILES_SCHEMA_VERSION,
        "seq": record.seq,
        "id": record.id,
        "at": record.at,
        "kind": record.kind.value,
        "text": record.text,
        "stop": None if record.stop is None else {"kind": record.stop.kind.value, "mode": record.stop.mode.value},
    }


def _record_line(record: MailboxRecord) -> bytes:
    text = json.dumps(_record_document(record), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return text.encode("ascii") + b"\n"


def _record_from_document(document: Mapping[str, Any], label: str) -> MailboxRecord:
    _exact(document, _RECORD_KEYS, label)
    stop = document["stop"]
    if stop is not None:
        if not isinstance(stop, dict) or set(stop) != {"kind", "mode"}:
            raise ValueError(f"{label}: stop must be an object with kind and mode")
        stop = StopRequest(kind=StopRequestKind(stop["kind"]), mode=StopMode(stop["mode"]))
    return MailboxRecord(
        seq=document["seq"], id=document["id"], at=document["at"], kind=MailboxRecordKind(document["kind"]),
        text=document["text"], stop=stop,
    )


def _state_document(state: MailboxState) -> dict[str, Any]:
    return {
        "schema": SESSION_FILES_SCHEMA_VERSION,
        "forwarded_through_seq": state.forwarded_through_seq,
        "closed": state.closed,
        "closed_at": state.closed_at,
        "closed_through_seq": state.closed_through_seq,
        "close_reason": None if state.close_reason is None else state.close_reason.value,
    }


def _state_from_document(document: Mapping[str, Any], label: str) -> MailboxState:
    _exact(document, _STATE_KEYS, label)
    reason = document["close_reason"]
    return MailboxState(
        forwarded_through_seq=document["forwarded_through_seq"],
        closed=document["closed"],
        closed_at=document["closed_at"],
        closed_through_seq=document["closed_through_seq"],
        close_reason=None if reason is None else StopKind(reason),
    )


def _session_document(record: SessionRecord) -> dict[str, Any]:
    return {
        "schema": SESSION_FILES_SCHEMA_VERSION,
        "run_id": record.run_id,
        "job_id": record.job_id,
        "attempt_no": record.attempt_no,
        "session_id": record.session_id,
        "resumed": record.resumed,
        "cwd": str(record.cwd),
        "process_tag": record.process_tag,
        "created_at": record.created_at,
        "first_init_at": record.first_init_at,
        "ended_at": record.ended_at,
        "stop_kind": None if record.stop_kind is None else record.stop_kind.value,
        "result_subtype": record.result_subtype,
        "shutdown_confirmed": record.shutdown_confirmed,
    }


def _session_from_document(document: Mapping[str, Any], label: str) -> SessionRecord:
    _exact(document, _SESSION_KEYS, label)
    for key in ("cwd", "first_init_at", "ended_at", "result_subtype"):
        _string_or_none(document, key, label)
    if not isinstance(document["cwd"], str):
        raise ValueError(f"{label}: cwd must be a string")
    confirmed = document["shutdown_confirmed"]
    if confirmed is not None and not isinstance(confirmed, bool):
        raise ValueError(f"{label}: shutdown_confirmed must be a boolean or null, got {confirmed!r}")
    stop_kind = document["stop_kind"]
    return SessionRecord(
        run_id=document["run_id"], job_id=document["job_id"], attempt_no=document["attempt_no"],
        session_id=document["session_id"], resumed=document["resumed"], cwd=Path(document["cwd"]),
        process_tag=document["process_tag"], created_at=document["created_at"],
        first_init_at=document["first_init_at"], ended_at=document["ended_at"],
        stop_kind=None if stop_kind is None else StopKind(stop_kind),
        result_subtype=document["result_subtype"], shutdown_confirmed=confirmed,
    )


def _identity_document(identity: ProcessIdentity) -> dict[str, Any]:
    return {"pid": identity.pid, "create_time": identity.create_time}


def _identity_from_document(value: object, label: str) -> ProcessIdentity:
    if not isinstance(value, dict) or set(value) != {"pid", "create_time"}:
        raise ValueError(f"{label}: a process identity must be an object with pid and create_time")
    return ProcessIdentity(pid=value["pid"], create_time=value["create_time"])


def _survivor_document(survivor: DetachedSurvivor) -> dict[str, Any]:
    return {**_identity_document(survivor.identity), "cwd": survivor.cwd}


def _survivor_from_document(value: object, label: str) -> DetachedSurvivor:
    if not isinstance(value, dict) or set(value) != {"pid", "create_time", "cwd"}:
        raise ValueError(f"{label}: a detached survivor must be an object with pid, create_time and cwd")
    identity = ProcessIdentity(pid=value["pid"], create_time=value["create_time"])
    return DetachedSurvivor(identity=identity, cwd=value["cwd"])


def _cleanup_document(record: CleanupRecord) -> dict[str, Any]:
    return {
        "schema": SESSION_FILES_SCHEMA_VERSION,
        "tag": record.tag,
        "leader": _identity_document(record.leader),
        "process_group": record.process_group,
        "members": [_identity_document(member) for member in record.members],
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "confirmed_stopped_at": record.confirmed_stopped_at,
        "detached_survivors": [_survivor_document(item) for item in record.detached_survivors],
    }


def _cleanup_from_document(document: Mapping[str, Any], label: str) -> CleanupRecord:
    _exact(document, _CLEANUP_KEYS, label)
    members = document["members"]
    if not isinstance(members, list):
        raise ValueError(f"{label}: members must be a list")
    survivors = document["detached_survivors"]
    if not isinstance(survivors, list):
        raise ValueError(f"{label}: detached_survivors must be a list")
    return CleanupRecord(
        tag=document["tag"],
        leader=_identity_from_document(document["leader"], label),
        started_at=document["started_at"],
        updated_at=document["updated_at"],
        members=tuple(_identity_from_document(member, label) for member in members),
        process_group=document["process_group"],
        confirmed_stopped_at=document["confirmed_stopped_at"],
        detached_survivors=tuple(_survivor_from_document(item, label) for item in survivors),
    )


# ---------------------------------------------------------------------------
# Unlocked readers and writers (the caller holds mailbox.lock)
# ---------------------------------------------------------------------------


def _parse_records(data: bytes, label: str) -> tuple[MailboxRecord, ...]:
    """Parse the complete lines of a mailbox file; a torn final line is ignored."""
    complete = data[: data.rfind(b"\n") + 1]
    records: list[MailboxRecord] = []
    for number, raw in enumerate(complete.split(b"\n")[:-1], start=1):
        where = f"{label} line {number}"
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{where} is not UTF-8 text: {exc}") from None
        record = _record_from_document(_parse_object(text, where), where)
        if record.seq != number:
            raise ValueError(f"{where} has seq {record.seq}; mailbox lines must be numbered 1, 2, 3, ...")
        records.append(record)
    return tuple(records)


def _load_records(paths: SessionPaths) -> tuple[MailboxRecord, ...]:
    return _parse_records(paths.mailbox.read_bytes(), str(paths.mailbox))


def _load_state(paths: SessionPaths) -> MailboxState:
    label = f"mailbox state ({paths.mailbox_state})"
    return _state_from_document(_read_json_file(paths.mailbox_state, "mailbox state"), label)


def _store_state(paths: SessionPaths, state: MailboxState) -> None:
    _write_json_file(paths.mailbox_state, _state_document(state))


def _load_session(paths: SessionPaths) -> SessionRecord:
    label = f"session record ({paths.session_record})"
    return _session_from_document(_read_json_file(paths.session_record, "session record"), label)


def _store_session(paths: SessionPaths, record: SessionRecord) -> None:
    _write_json_file(paths.session_record, _session_document(record))


def _load_cleanup(paths: SessionPaths) -> Optional[CleanupRecord]:
    if not paths.cleanup.exists():
        return None
    label = f"cleanup record ({paths.cleanup})"
    return _cleanup_from_document(_read_json_file(paths.cleanup, "cleanup record"), label)


def _append_record(paths: SessionPaths, kind: MailboxRecordKind, *, text: Optional[str],
                   stop: Optional[StopRequest], message_id: Optional[str], at: Optional[str]) -> MailboxRecord:
    data = paths.mailbox.read_bytes()
    existing = _parse_records(data, str(paths.mailbox))
    record = MailboxRecord(
        seq=len(existing) + 1,
        id=uuid.uuid4().hex if message_id is None else message_id,
        at=_now() if at is None else at,
        kind=kind,
        text=text,
        stop=stop,
    )
    line = _record_line(record)
    complete = data.rfind(b"\n") + 1
    descriptor = os.open(paths.mailbox, os.O_RDWR | os.O_APPEND | _BINARY, 0o600)
    try:
        if complete != len(data):
            os.ftruncate(descriptor, complete)  # a torn tail is not a record
        view = memoryview(line)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def create_attempt_dir(paths: SessionPaths) -> None:
    """Create the attempt directory with an empty mailbox and an open, empty state.

    Idempotent: an existing mailbox and state are kept, never reset. The
    directory is private (``0700`` on POSIX) because messages carry prompts.
    Call it before the session backend starts. ``events.jsonl``, ``session.json``
    and ``cleanup.json`` are written later by their owners. Raises ``TypeError``
    for a non-:class:`SessionPaths` and ``OSError`` for a filesystem failure.
    """
    if not isinstance(paths, SessionPaths):
        raise TypeError(f"paths must be a SessionPaths, got {type(paths).__name__}")
    paths.attempt_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        paths.attempt_dir.chmod(0o700)
    with file_lock(paths.mailbox_lock):
        descriptor = os.open(paths.mailbox, os.O_WRONLY | os.O_CREAT | _BINARY, 0o600)
        os.close(descriptor)
        if not paths.mailbox_state.exists():
            _store_state(paths, MailboxState())
    _fsync_directory(paths.attempt_dir)


def append_message(paths: SessionPaths, text: str, *, message_id: Optional[str] = None,
                   at: Optional[str] = None) -> MailboxRecord:
    """Append a user message to the mailbox and return its record.

    ``text`` must be a nonempty string. ``message_id`` defaults to a new UUID
    hex; ``at`` defaults to the current UTC time as ISO-8601. ``seq`` is the
    next number, assigned under the mailbox lock, so concurrent appends from
    any number of processes get unique consecutive numbers. The line is
    written with one ``O_APPEND`` write and ``fsync``. A message appended
    after the mailbox closed is still recorded; ``read_state(paths).delivery
    (record.seq)`` is then ``undelivered``. Raises ``ValueError`` for an
    invalid ``text``, ``message_id`` or ``at`` (nothing is written), and
    ``FileNotFoundError`` when the attempt directory does not exist.
    """
    with _locked(paths):
        return _append_record(paths, MailboxRecordKind.MESSAGE, text=text, stop=None,
                              message_id=message_id, at=at)


def request_stop(paths: SessionPaths, kind: StopRequestKind, mode: StopMode, *,
                 at: Optional[str] = None) -> MailboxRecord:
    """Append an operator stop or pause request to the mailbox and return its record.

    ``kind`` is :class:`StopRequestKind` (``stop`` or ``pause``) and ``mode``
    :class:`StopMode` (``interrupt`` or ``finish_turn``). The request travels
    through the mailbox like a message, with the same ordering, locking and
    durability, and is subject to the same rule after closure (recorded,
    ``undelivered``). Raises ``ValueError`` for an invalid ``kind``, ``mode``
    or ``at``, and ``FileNotFoundError`` when the attempt directory does not exist.
    """
    request = StopRequest(kind=kind, mode=mode)
    with _locked(paths):
        return _append_record(paths, MailboxRecordKind.STOP, text=None, stop=request,
                              message_id=None, at=at)


def read_state(paths: SessionPaths) -> MailboxState:
    """Return the current :class:`MailboxState`.

    Raises ``FileNotFoundError`` when the attempt directory or its state file
    does not exist, and ``ValueError`` when the file does not match its format.
    """
    with _locked(paths):
        return _load_state(paths)


def read_records(paths: SessionPaths) -> tuple[MailboxRecord, ...]:
    """Return every complete mailbox record in ``seq`` order.

    A torn final line is not a record and is omitted. Raises
    ``FileNotFoundError`` when the attempt directory or mailbox does not exist
    and ``ValueError`` for a malformed or non-consecutive line.
    """
    with _locked(paths):
        return _load_records(paths)


def write_session_record(paths: SessionPaths, record: SessionRecord) -> None:
    """Write ``session.json`` atomically (``fsync``, rename).

    The identity fields (``run_id``, ``job_id``, ``attempt_no``,
    ``session_id``, ``resumed``, ``cwd``, ``process_tag``, ``created_at``) of
    an existing record cannot change, and a recorded ``first_init_at`` cannot
    be dropped or changed, so a caller that replaces the record after the
    outcome must start from :func:`read_session_record`. Raises ``ValueError``
    for a record that breaks those rules, ``TypeError`` for a non-
    :class:`SessionRecord`, and ``FileNotFoundError`` when the attempt
    directory does not exist.
    """
    if not isinstance(record, SessionRecord):
        raise TypeError(f"record must be a SessionRecord, got {type(record).__name__}")
    with _locked(paths):
        if paths.session_record.exists():
            _check_session_succession(_load_session(paths), record)
        _store_session(paths, record)


def read_session_record(paths: SessionPaths) -> SessionRecord:
    """Return ``session.json`` as a :class:`SessionRecord`.

    Raises ``FileNotFoundError`` when it does not exist and ``ValueError``
    when it does not match its format.
    """
    with _locked(paths):
        return _load_session(paths)


def read_cleanup_record(paths: SessionPaths) -> Optional[CleanupRecord]:
    """Return ``cleanup.json`` as a :class:`CleanupRecord`, or ``None`` when absent.

    Raises ``FileNotFoundError`` when the attempt directory does not exist and
    ``ValueError`` when the file does not match its format.
    """
    with _locked(paths):
        return _load_cleanup(paths)


_IDENTITY_FIELDS = ("run_id", "job_id", "attempt_no", "session_id", "resumed", "cwd", "process_tag", "created_at")


def _check_session_succession(old: SessionRecord, new: SessionRecord) -> None:
    for name in _IDENTITY_FIELDS:
        if getattr(old, name) != getattr(new, name):
            raise ValueError(f"session record {name} cannot change ({getattr(old, name)!r} -> {getattr(new, name)!r})")
    if old.first_init_at is not None and new.first_init_at != old.first_init_at:
        raise ValueError("session record first_init_at, once recorded, cannot be dropped or changed")


def _check_cleanup_succession(old: CleanupRecord, new: CleanupRecord) -> None:
    for name in ("tag", "leader", "process_group", "started_at"):
        if getattr(old, name) != getattr(new, name):
            raise ValueError(f"cleanup record {name} cannot change ({getattr(old, name)!r} -> {getattr(new, name)!r})")
    dropped = [member for member in old.members if member not in new.members]
    if dropped:
        raise ValueError(f"cleanup record cannot drop tracked members: {dropped}")


class FileSessionChannel:
    """The :class:`~llm_kit.completion.session_types.SessionChannel` over a session directory.

    Built over the :class:`SessionPaths` of an attempt directory that
    :func:`create_attempt_dir` created. Every method takes the mailbox lock, so
    one channel and any number of operator processes may use the directory at
    once. ``poll``, ``mark_forwarded`` and ``close`` serve the mailbox,
    ``record_cleanup`` writes ``cleanup.json`` and ``record_first_init`` stamps
    ``session.json``.
    """

    def __init__(self, paths: SessionPaths) -> None:
        if not isinstance(paths, SessionPaths):
            raise TypeError(f"paths must be a SessionPaths, got {type(paths).__name__}")
        self.paths = paths

    def poll(self, after_seq: int) -> Sequence[MailboxRecord]:
        """Return the records with ``seq > after_seq`` in order; empty once the mailbox is closed.

        Raises ``ValueError`` for a negative or non-integer ``after_seq``.
        """
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError(f"after_seq must be a non-negative integer, got {after_seq!r}")
        with _locked(self.paths):
            if _load_state(self.paths).closed:
                return ()
            return tuple(record for record in _load_records(self.paths) if record.seq > after_seq)

    def mark_forwarded(self, seq: int) -> None:
        """Record that ``seq`` was written to stdin.

        Raises ``ValueError`` unless the mailbox is open, the record exists, and
        ``seq`` is ``forwarded_through_seq + 1``.
        """
        with _locked(self.paths):
            state = _load_state(self.paths)
            if state.closed:
                raise ValueError(f"cannot forward seq {seq}: the mailbox is closed")
            if seq != state.forwarded_through_seq + 1:
                raise ValueError(
                    f"cannot forward seq {seq}: the next record to forward is {state.forwarded_through_seq + 1}")
            if seq > len(_load_records(self.paths)):
                raise ValueError(f"cannot forward seq {seq}: no such mailbox record")
            _store_state(self.paths, MailboxState(forwarded_through_seq=seq))

    def close(self, reason: StopKind, at: str) -> MailboxState:
        """Close the mailbox and return the final state; closing again returns the first closure.

        ``closed_through_seq`` is the highest ``seq`` present now. Records
        appended later are recorded and ``undelivered``. Raises ``ValueError``
        for an invalid ``reason`` or ``at``.
        """
        reason = StopKind(reason)
        with _locked(self.paths):
            state = _load_state(self.paths)
            if state.closed:
                return state
            closed = MailboxState(
                forwarded_through_seq=state.forwarded_through_seq, closed=True, closed_at=at,
                closed_through_seq=len(_load_records(self.paths)), close_reason=reason,
            )
            _store_state(self.paths, closed)
            return closed

    def record_cleanup(self, record: CleanupRecord) -> None:
        """Replace ``cleanup.json`` with ``record``, durably (``fsync``, atomic rename).

        Raises ``ValueError`` when ``record`` changes ``tag``, ``leader``,
        ``process_group`` or ``started_at`` of the existing record, drops a
        member, or has a ``tag`` different from ``session.json``'s
        ``process_tag``; ``FileNotFoundError`` when ``session.json`` has not
        been written (it carries the tag and precedes the spawn).
        """
        if not isinstance(record, CleanupRecord):
            raise ValueError(f"record must be a CleanupRecord, got {type(record).__name__}")
        with _locked(self.paths):
            session = _load_session(self.paths)
            if record.tag != session.process_tag:
                raise ValueError(
                    f"cleanup record tag {record.tag!r} differs from the session record's {session.process_tag!r}")
            existing = _load_cleanup(self.paths)
            if existing is not None:
                _check_cleanup_succession(existing, record)
            _write_json_file(self.paths.cleanup, _cleanup_document(record))

    def record_first_init(self, at: str) -> None:
        """Record in ``session.json`` when the first ``system:init`` arrived.

        Only the first call has an effect. Raises ``ValueError`` for an empty
        ``at`` and ``FileNotFoundError`` when ``session.json`` has not been written.
        """
        if not isinstance(at, str) or not at.strip():
            raise ValueError(f"at must be a nonempty string, got {at!r}")
        with _locked(self.paths):
            session = _load_session(self.paths)
            if session.first_init_at is None:
                _store_session(self.paths, replace(session, first_init_at=at))


__all__ = [
    "FileSessionChannel",
    "append_message",
    "create_attempt_dir",
    "read_cleanup_record",
    "read_records",
    "read_session_record",
    "read_state",
    "request_stop",
    "write_session_record",
]
