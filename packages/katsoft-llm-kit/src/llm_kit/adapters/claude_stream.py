"""Parsing and verdicts for the Claude CLI ``stream-json`` output.

One owner for the three questions every consumer of a ``claude -p
--output-format stream-json`` stdout asks: is this line an event
(:func:`parse_stream_line`), does a ``system:init`` event describe the session
that was asked for (:func:`init_problem`), and what has the stream shown so far
(:class:`StreamVerdict`). The relay's resume runner and the agent-session
backend share them, so both read init and result events by the same rules.

Public names: :func:`parse_stream_line`, :func:`init_problem`,
:class:`StreamVerdict`.

The module is stdlib-only and performs no I/O.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Union


def parse_stream_line(line: Union[str, bytes]) -> Optional[dict]:
    """Return the JSON object on one stdout line, or None when the line is not one.

    Args:
        line: one line of CLI stdout, as text or UTF-8 bytes (undecodable bytes
            are replaced). Surrounding whitespace, including the newline, is
            ignored.

    Returns:
        The decoded event when the line is a JSON object; None for a blank
        line, malformed JSON, or JSON that is not an object (list, string,
        number). A line is never an exception.

    Raises:
        TypeError: ``line`` is neither ``str`` nor ``bytes``.
    """
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("utf-8", errors="replace")
    elif not isinstance(line, str):
        raise TypeError(f"line must be str or bytes, not {type(line).__name__}")
    try:
        event = json.loads(line)
    except ValueError:  # JSONDecodeError and the embedded-NUL / lone-surrogate cases
        return None
    return event if isinstance(event, dict) else None


def _is_init(event: Mapping[str, Any]) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


def _is_result(event: Mapping[str, Any]) -> bool:
    return event.get("type") == "result"


def _realpath(value: Union[str, Path]) -> Optional[str]:
    """``os.path.realpath`` of a non-empty path, or None when it cannot be resolved."""
    try:
        text = os.fspath(value)  # TypeError for a non-path, e.g. a missing (None) or numeric cwd
        if not isinstance(text, str) or not text:
            return None
        return os.path.realpath(text)
    except (TypeError, ValueError, OSError):
        return None


def init_problem(
    event: Mapping[str, Any],
    *,
    session_id: str,
    cwd: Union[str, Path],
    permission_mode: str,
) -> Optional[str]:
    """Return why a ``system:init`` event does not match the expected session, or None.

    The checks run in order and the first failure is reported: the event's
    ``session_id`` must equal ``session_id``; its ``cwd`` must resolve (by
    ``os.path.realpath``) to the same directory as ``cwd``; its
    ``permissionMode`` must equal ``permission_mode``.

    Args:
        event: the init event, as returned by :func:`parse_stream_line`.
        session_id: the session id that was requested.
        cwd: the working directory that was requested.
        permission_mode: the permission mode that was requested.

    Returns:
        None when the event matches; otherwise a one-line description of the
        first mismatch. A missing or non-string ``session_id`` or ``cwd`` (and
        an empty ``cwd``) is a mismatch, never an exception.
    """
    seen_id = event.get("session_id")
    if seen_id != session_id:
        return f"session_id {seen_id!r} is not {session_id!r}"
    seen_cwd = event.get("cwd")
    resolved_seen, resolved_want = _realpath(seen_cwd), _realpath(cwd)
    if resolved_seen is None or resolved_want is None or resolved_seen != resolved_want:
        return f"cwd {seen_cwd!r} is not {cwd!r}"
    mode = event.get("permissionMode")
    if mode != permission_mode:
        return f"permissionMode {mode!r} is not {permission_mode!r}"
    return None


class StreamVerdict:
    """What a Claude ``stream-json`` stream has shown, for one expected session.

    Feed every parsed event to :meth:`observe` in arrival order. The verdict
    keeps all ``system:init`` and ``result`` events with their indexes and
    answers the questions the relay and the agent-session backend ask. An
    event's index is a 0-based count of every event passed to :meth:`observe`
    (all types; lines :func:`parse_stream_line` rejects are never passed and
    never counted), so indexes order inits, results and any other event.

    ``observe`` and the accessors may be called from different threads; each
    accessor returns a consistent snapshot.

    Args:
        session_id: the requested session id.
        cwd: the requested working directory.
        permission_mode: the requested permission mode.
            An init is *valid* exactly when :func:`init_problem` returns None for
            these three expected values.
    """

    def __init__(self, *, session_id: str, cwd: Union[str, Path], permission_mode: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.permission_mode = permission_mode
        self._lock = threading.Lock()
        self._events_seen = 0
        self._inits: list[tuple[int, Mapping[str, Any]]] = []
        self._problems: list[Optional[str]] = []
        self._results: list[tuple[int, Mapping[str, Any]]] = []

    def observe(self, event: Mapping[str, Any]) -> int:
        """Record one event and return its index.

        Args:
            event: a decoded stream event.

        Returns:
            The event's 0-based index among all observed events.

        Raises:
            TypeError: ``event`` is not a mapping.
        """
        if not isinstance(event, Mapping):
            raise TypeError(f"event must be a mapping, not {type(event).__name__}")
        problem = None
        if _is_init(event):
            problem = init_problem(event, session_id=self.session_id, cwd=self.cwd,
                                   permission_mode=self.permission_mode)
        with self._lock:
            index = self._events_seen
            self._events_seen += 1
            if _is_init(event):
                self._inits.append((index, event))
                self._problems.append(problem)
            elif _is_result(event):
                self._results.append((index, event))
            return index

    @property
    def events_seen(self) -> int:
        """The index the next observed event will get (the count observed so far)."""
        with self._lock:
            return self._events_seen

    @property
    def inits(self) -> tuple[tuple[int, Mapping[str, Any]], ...]:
        """Every ``system:init`` event as ``(index, event)``, in arrival order."""
        with self._lock:
            return tuple(self._inits)

    @property
    def results(self) -> tuple[tuple[int, Mapping[str, Any]], ...]:
        """Every ``result`` event as ``(index, event)``, in arrival order."""
        with self._lock:
            return tuple(self._results)

    @property
    def first_init(self) -> Optional[Mapping[str, Any]]:
        """The first init event, valid or not; None before any init."""
        with self._lock:
            return self._inits[0][1] if self._inits else None

    @property
    def first_problem(self) -> Optional[str]:
        """:func:`init_problem` of :attr:`first_init`; None when it is valid or absent."""
        with self._lock:
            return self._problems[0] if self._problems else None

    @property
    def first_valid_init(self) -> Optional[tuple[int, Mapping[str, Any]]]:
        """The first init whose problem is None, as ``(index, event)``; None when there is none."""
        with self._lock:
            for item, problem in zip(self._inits, self._problems):
                if problem is None:
                    return item
            return None

    @property
    def first_result_after_init(self) -> Optional[tuple[int, Mapping[str, Any]]]:
        """The relay's result: the first result after the first valid init for the expected session.

        A result qualifies when its index is greater than :attr:`first_valid_init`'s
        and its ``session_id`` equals the expected one. None when no init is
        valid or no result qualifies.
        """
        with self._lock:
            start = next((i for (i, _), problem in zip(self._inits, self._problems) if problem is None), None)
            if start is None:
                return None
            for index, event in self._results:
                if index > start and event.get("session_id") == self.session_id:
                    return (index, event)
            return None

    @property
    def last_init_index(self) -> Optional[int]:
        """Index of the latest init; None before any init. A result's quiescence timer is void
        when this exceeds the result's index."""
        with self._lock:
            return self._inits[-1][0] if self._inits else None

    @property
    def last_result(self) -> Optional[tuple[int, Mapping[str, Any]]]:
        """The latest result as ``(index, event)``; None before any result."""
        with self._lock:
            return self._results[-1] if self._results else None

    def has_result_after(self, index: int) -> bool:
        """True when some result has an index strictly greater than ``index``.

        At Stop the backend reads :attr:`events_seen` and waits for
        ``has_result_after(events_seen - 1)``, which excludes every result
        already observed.

        Raises:
            TypeError: ``index`` is not an ``int`` (``bool`` is refused).
        """
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"index must be an int, not {type(index).__name__}")
        with self._lock:
            return bool(self._results) and self._results[-1][0] > index
