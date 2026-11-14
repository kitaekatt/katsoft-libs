"""The Claude CLI as a stream-json agent session (design 5, 11.2).

:class:`ClaudeAgentSession` runs one attempt of an agent-session job::

    claude -p --input-format stream-json --output-format stream-json --verbose
      --model <m> --append-system-prompt <system> --permission-mode bypassPermissions
      [--allowedTools <a>] [--disallowedTools <d>] [--effort <e>] [--json-schema <s>]
      (--session-id <uuid> | --resume <uuid>)

The session is persisted (no ``--no-session-persistence``), so it can be
resumed later with ``--resume``. The backend owns the child's three pipes:

- **stdin** is written by one driver thread. The job's user prompt is the first
  stream-json user message. The thread then polls the mailbox
  (:class:`~llm_kit.completion.session_types.SessionChannel`) and forwards each
  message record as a user message, marking it ``forwarded`` after the write.
  A ``stop`` record ends forwarding and is handed to the control loop. The
  thread also writes the interrupt ``control_request`` and, at shutdown, closes
  the mailbox and then stdin.
- **stdout** is copied byte for byte to ``events.jsonl`` by a reader thread,
  which also parses each line (:func:`~llm_kit.adapters.claude_stream
  .parse_stream_line`) into a :class:`~llm_kit.adapters.claude_stream
  .StreamVerdict`.
- **stderr** is kept as a bounded tail for failure messages.

Control rules (design 5.5, 11.3):

- An attempt ends **quiescent** when a ``result`` has been seen, no later
  ``system:init`` arrived, and Q (``bounds.quiescence_s``) has passed since the
  later of that result and the last forwarded mailbox message. A later init
  voids the timer.
- The attempt timeout (``options.timeout_s``) bounds session activity,
  including every Q window. At the timeout an open Q window ends the attempt
  quiescent with its result; otherwise the session is stopped in interrupt
  mode with stop kind ``timeout``.
- **Stop** (a mailbox ``stop`` record, or the timeout): idle (a result after
  the last init and nothing forwarded since) -> shut down; busy and mode
  ``interrupt`` -> write the interrupt ``control_request``, wait B_res for a
  result not already observed, then shut down; mode ``finish_turn`` -> shut
  down without the interrupt. A result racing the stop is accepted as the
  result, whatever its subtype.
- **Shutdown**: close the mailbox, close stdin, wait B_eof for the leader to
  exit; then SIGINT (Windows: CTRL_BREAK_EVENT), wait B_int; then kill every
  owned process by identity. An init whose session id, cwd or permission mode
  does not match the request is not shut down gracefully: the mailbox and
  stdin are closed and every owned process is killed at once.
- After the leader exits, every remaining ATTACHED process is killed, stdout
  is drained to EOF, and only then is the final result chosen: the LAST result
  event. ``shutdown_confirmed`` (session shutdown) is true only when the
  leader and every attached member are gone and stdout is drained.
  DETACHED processes are never signalled: the shutdown waits up to B_int for
  them, and each one still alive is a survivor, persisted in ``cleanup.json``
  with its cwd and printed as a warning. Survivors make the outcome not
  ``workspace_quiescent``; they do not fail the attempt.

Process identity (design 5.5, 11.2): the child gets
:data:`~llm_kit.completion.session_types.SESSION_TAG_ENV` set to the request's
``process_tag``. ``cleanup.json`` is written right after the spawn and again
whenever a new descendant is seen. A process is stopped only when its
``(pid, create_time)`` is recorded, or (POSIX) when it is inside the leader's
original session while the unreaped leader keeps that session id reserved; it
is then recorded. A recorded member stays attached even if it later calls
``setsid()``, so polling timing never turns a recorded process into a detached
one. DETACHED: proven ours (tagged, or seen as a descendant of the leader),
created after the leader, never recorded, and outside the leader's original
session; a different process group alone is not detachment. A tagged process
neither rule decides is never signalled and makes the shutdown unconfirmed.
Without session ids (Windows) only recorded identities and tagged descendants
of them are attached. Signals go through psutil handles bound to the creation time, so
a reused pid is never touched. psutil is required (the ``local`` extra).

Public names: :class:`ClaudeAgentSession`, :func:`user_message_line`,
:func:`interrupt_request_line`.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional

from llm_kit.completion import halt
from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.contract import OutputContractViolation, finalize_contract
from llm_kit.completion.results import check_applied_controls, utc_now_iso
from llm_kit.completion.session_types import (
    AGENT_PERMISSION_MODE,
    SESSION_HALT_KINDS,
    SESSION_TAG_ENV,
    CleanupRecord,
    DetachedSurvivor,
    MailboxRecordKind,
    ProcessIdentity,
    SessionFailure,
    SessionFailureKind,
    SessionOutcome,
    SessionRequest,
    StopKind,
    StopMode,
    StopRequest,
)
from llm_kit.completion.types import ERROR, LLMResponse, ResponseError

from .backends import (
    _check_system_prompt_mode,
    _claude_envelope_response,
    _claude_option_args,
    _claude_prepare,
    _claudx_plugin_args,
    _resolve_claude_executable,
)
from .capabilities import _CLAUDE_SYSTEM_PROMPT_FLAGS, CLAUDE_AGENT_CAPABILITIES
from .claude_stream import StreamVerdict, init_problem, parse_stream_line

#: Bytes of stderr kept for failure messages.
_STDERR_TAIL = 8192
#: Seconds the backend waits for killed leftovers to disappear.
_KILL_WAIT_S = 5.0
#: Seconds the backend waits for stdout EOF once every owned process is gone.
_DRAIN_WAIT_S = 5.0
_CLOSE = object()


def user_message_line(text: str) -> bytes:
    """One stream-json user message carrying ``text``, as a newline-terminated line.

    Args:
        text: the message text (the job prompt or a mailbox message).

    Returns:
        ``{"type":"user","message":{"role":"user","content":[{"type":"text",
        "text":...}]}}`` as compact ASCII JSON plus ``\\n``.

    Raises:
        TypeError: ``text`` is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a string, not {type(text).__name__}")
    document = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def interrupt_request_line(request_id: str) -> bytes:
    """The stream-json interrupt ``control_request`` (measured on Claude Code 2.1.325, design 5.5).

    Args:
        request_id: the request id Claude echoes in its ``control_response``.

    Returns:
        ``{"type":"control_request","request_id":...,"request":{"subtype":
        "interrupt"}}`` plus ``\\n``.

    Raises:
        ValueError: ``request_id`` is empty or not a string.
    """
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a nonempty string")
    document = {"type": "control_request", "request_id": request_id, "request": {"subtype": "interrupt"}}
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def _psutil() -> Any:
    try:
        import psutil  # noqa: PLC0415 -- the local extra, needed only to run a session
    except ImportError as exc:
        raise RuntimeError(
            "agent sessions need psutil for identity-bound process cleanup; "
            "install katsoft-llm-kit[local]"
        ) from exc
    return psutil


def _child_env(tag: str) -> dict[str, str]:
    """This process's environment without ``CLAUDECODE``, plus the membership tag.

    ``CLAUDECODE`` marks a process as running inside Claude Code; a session
    started from a Claude Code tool must not inherit it.
    """
    env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
    env[SESSION_TAG_ENV] = tag
    return env


def _same_user(proc: Any) -> bool:
    """Whether ``proc`` runs as this user (always true on Windows, where uids do not exist)."""
    if os.name != "posix":
        return True
    try:
        return proc.uids().real == os.getuid()
    except Exception:  # noqa: BLE001 -- psutil.Error; an unreadable process is not ours
        return False


def _is_init(event: Mapping[str, Any]) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


def _observed(value: object) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else repr(value)


@dataclass
class ClaudeAgentSession:
    """``claude -p`` as a stream-json agent session; implements
    :class:`~llm_kit.completion.session_types.AgentSessionBackend`.

    Attributes:
        executable: explicit path to the ``claude`` binary. ``None`` resolves
            it per run: ``LLM_KIT_CLAUDE_EXECUTABLE`` when set, else ``claude``
            on PATH.
        poll_interval_s: how often the stdin driver polls the mailbox and the
            control loop re-checks its rules, in seconds.
        member_scan_interval_s: how often the leader's descendants are
            enumerated and newly seen ones added to ``cleanup.json``.

    ``name`` is ``claude-cli``, the adapter the ledger records; ``capabilities``
    is :data:`~llm_kit.adapters.capabilities.CLAUDE_AGENT_CAPABILITIES`.
    """

    executable: Optional[str] = None
    poll_interval_s: float = 0.1
    member_scan_interval_s: float = 0.25
    name: str = field(default="claude-cli", init=False)
    capabilities: ClassVar[Capabilities] = CLAUDE_AGENT_CAPABILITIES

    def build_argv(self, request: SessionRequest) -> list[str]:
        """The argv :meth:`run` would spawn for ``request``, without spawning anything.

        Raises the same errors as :meth:`run` raises before the spawn: an
        unsupported output contract, any skill context, an effort outside the
        model's menu, an unknown ``system_prompt_mode``, or a missing CLI.
        """
        argv, _, _ = self._prepare(request)
        return argv

    def applied_controls(self, request: SessionRequest) -> tuple[str, ...]:
        """The execution controls the argv for ``request`` carries.

        ``permission-bypass`` always; ``allowed-tools`` and ``disallowed-tools``
        only when the request sets them. Never ``no-session-persistence``: the
        agent session keeps its session. Raises ``ValueError`` when an id is
        not advertised by :attr:`capabilities`.
        """
        options = request.options
        applied = ["permission-bypass"]
        if options.allowed_tools is not None:
            applied.append("allowed-tools")
        if options.disallowed_tools is not None:
            applied.append("disallowed-tools")
        return check_applied_controls(self.capabilities, tuple(applied))

    def _prepare(self, request: SessionRequest) -> tuple[list[str], Any, tuple[str, ...]]:
        if not isinstance(request, SessionRequest):
            raise TypeError(f"request must be a SessionRequest, not {type(request).__name__}")
        options = request.options
        contract_plan, system = _claude_prepare(
            self.capabilities, options, request.system, request.model, subject=self.name
        )
        _check_system_prompt_mode(options)
        executable = self.executable or _resolve_claude_executable()
        argv = [
            executable,
            *_claudx_plugin_args(),
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            request.model,
            _CLAUDE_SYSTEM_PROMPT_FLAGS[options.system_prompt_mode],
            system,
            "--permission-mode",
            AGENT_PERMISSION_MODE,
        ]
        if options.allowed_tools is not None:
            argv.extend(["--allowedTools", options.allowed_tools])
        argv.extend(_claude_option_args(options, contract_plan))
        argv.extend(["--resume" if request.resume else "--session-id", request.session_id])
        return argv, contract_plan, self.applied_controls(request)

    def run(self, request: SessionRequest) -> SessionOutcome:
        """Run one attempt to its end and return its :class:`SessionOutcome`.

        Raises before the spawn for a request it cannot start (see
        :meth:`build_argv`), when psutil is missing, when ``events.jsonl``
        cannot be opened, or when the process cannot be started. After the
        spawn every end of the process is an outcome; a failure of the session
        channel (the mailbox or record files) stops every owned process and is
        then raised.
        """
        argv, contract_plan, controls = self._prepare(request)
        timeout_s = request.options.timeout_s
        if timeout_s is None:
            raise ValueError("an agent session needs options.timeout_s (the attempt timeout)")
        psutil = _psutil()
        sink = open(request.paths.events, "ab")  # noqa: SIM115 -- closed by the reader thread
        try:
            started_at = utc_now_iso()
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(  # noqa: S603 -- argv built above, no shell
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(request.cwd),
                env=_child_env(request.process_tag),
                **kwargs,
            )
        except BaseException:
            sink.close()
            raise
        run = _SessionRun(
            self, request, process, sink, psutil,
            contract_plan=contract_plan, controls=controls, started_at=started_at,
            timeout_s=float(timeout_s),
        )
        return run.execute()


class _ChannelFailure(RuntimeError):
    """The session channel failed after the spawn; raised once every process is stopped."""


class _SessionRun:
    """The state of one running session: threads, verdict, timers and process identities."""

    def __init__(
        self,
        backend: ClaudeAgentSession,
        request: SessionRequest,
        process: subprocess.Popen,
        sink: Any,
        psutil: Any,
        *,
        contract_plan: Any,
        controls: tuple[str, ...],
        started_at: str,
        timeout_s: float,
    ) -> None:
        self.backend = backend
        self.request = request
        self.channel = request.channel
        self.bounds = request.bounds
        self.process = process
        self.sink = sink
        self.psutil = psutil
        self.contract_plan = contract_plan
        self.controls = controls
        self.started_at = started_at
        self.timeout_s = timeout_s
        self.spawned = time.monotonic()
        self.verdict = StreamVerdict(
            session_id=request.session_id, cwd=request.cwd, permission_mode=AGENT_PERMISSION_MODE
        )
        self.cond = threading.Condition()
        self.event_times: dict[int, float] = {}
        self.last_forward: Optional[float] = None
        self.stop_request: Optional[StopRequest] = None
        self.close_reason: Optional[StopKind] = None
        self.errors: list[BaseException] = []
        self.stderr_tail: deque[bytes] = deque()
        self.stderr_size = 0
        self.writes: "queue.Queue[object]" = queue.Queue()
        self.stdout_done = threading.Event()
        self.escalated = False
        self.problem: Optional[tuple[Mapping[str, Any], str]] = None
        self.checked_inits = 0
        self.leader: Optional[ProcessIdentity] = None
        self.leader_handle: Any = None
        self.group: Optional[int] = None
        self.sid: Optional[int] = None
        self.members: dict[tuple[int, float], ProcessIdentity] = {}
        self.outside: dict[tuple[int, float], ProcessIdentity] = {}
        self.cleanup: Optional[CleanupRecord] = None
        self.last_scan = 0.0

    # -- threads -----------------------------------------------------------

    def _note_error(self, exc: BaseException) -> None:
        with self.cond:
            self.errors.append(exc)
            self.cond.notify_all()

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        try:
            first_init = True
            while True:
                line = stream.readline()
                if not line:
                    break
                self.sink.write(line)
                self.sink.flush()  # line-buffered: a reader sees every complete line at once
                event = parse_stream_line(line)
                if event is None:
                    continue
                index = self.verdict.observe(event)
                if _is_init(event) and first_init:
                    first_init = False
                    try:
                        self.channel.record_first_init(utc_now_iso())
                    except Exception as exc:  # noqa: BLE001 -- surfaced after shutdown
                        self._note_error(exc)
                with self.cond:
                    self.event_times[index] = time.monotonic()
                    self.cond.notify_all()
        except Exception as exc:  # noqa: BLE001 -- a sink failure is surfaced after shutdown
            self._note_error(exc)
        finally:
            self.sink.close()
            self.stdout_done.set()
            with self.cond:
                self.cond.notify_all()

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                return
            self.stderr_tail.append(chunk)
            self.stderr_size += len(chunk)
            while self.stderr_size > _STDERR_TAIL and len(self.stderr_tail) > 1:
                self.stderr_size -= len(self.stderr_tail.popleft())

    def _drive_stdin(self) -> None:
        stdin = self.process.stdin
        broken = False

        def write(data: bytes) -> bool:
            nonlocal broken
            if broken:
                return False
            try:
                stdin.write(data)
                stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                broken = True
                return False

        accepting = True
        after = 0
        try:
            write(user_message_line(self.request.user))
            while True:
                try:
                    item = self.writes.get(timeout=self.backend.poll_interval_s)
                except queue.Empty:
                    item = None
                if item is _CLOSE:
                    break
                if isinstance(item, bytes):
                    write(item)
                if not accepting or broken:
                    continue
                for record in self.channel.poll(after):
                    if record.kind is MailboxRecordKind.MESSAGE:
                        if not write(user_message_line(record.text or "")):
                            break  # left pending; undelivered at closure
                        self.channel.mark_forwarded(record.seq)
                        after = record.seq
                        with self.cond:
                            self.last_forward = time.monotonic()
                            self.cond.notify_all()
                    else:
                        self.channel.mark_forwarded(record.seq)
                        after = record.seq
                        accepting = False
                        with self.cond:
                            self.stop_request = record.stop
                            self.cond.notify_all()
                        break
        except Exception as exc:  # noqa: BLE001 -- surfaced after shutdown
            self._note_error(exc)
            while self.writes.get() is not _CLOSE:
                pass
        finally:
            try:
                self.channel.close(self.close_reason or StopKind.PROCESS_EXIT, utc_now_iso())
            except Exception as exc:  # noqa: BLE001 -- surfaced after shutdown
                self._note_error(exc)
            try:
                stdin.close()
            except (BrokenPipeError, OSError):
                pass

    # -- process identity ----------------------------------------------------

    def _record_cleanup(
        self, *, confirmed: bool = False, survivors: Optional[tuple[DetachedSurvivor, ...]] = None,
    ) -> None:
        if self.leader is None:
            return
        now = utc_now_iso()
        members = tuple(self.members.values())
        if self.cleanup is None:
            record = CleanupRecord(
                tag=self.request.process_tag, leader=self.leader, started_at=self.started_at,
                updated_at=now, members=members, process_group=self.group,
                confirmed_stopped_at=now if confirmed else None,
                detached_survivors=survivors or (),
            )
        else:
            record = replace(
                self.cleanup, updated_at=now, members=members,
                confirmed_stopped_at=now if confirmed else None,
                detached_survivors=self.cleanup.detached_survivors if survivors is None else survivors,
            )
        try:
            self.channel.record_cleanup(record)
        except Exception as exc:  # noqa: BLE001 -- surfaced after shutdown
            self._note_error(exc)
            return
        self.cleanup = record

    def _identify_leader(self) -> None:
        psutil = self.psutil
        try:
            handle = psutil.Process(self.process.pid)
            self.leader = ProcessIdentity(pid=self.process.pid, create_time=handle.create_time())
            self.leader_handle = handle
        except psutil.Error:
            return  # it already ended and was reaped; nothing can be recorded for it
        if os.name == "posix":
            try:
                self.group = os.getpgid(self.process.pid)
                self.sid = os.getsid(self.process.pid)
            except OSError:
                self.group = None
                self.sid = None
        self._record_cleanup()

    def _add_members(self, found: list[ProcessIdentity]) -> None:
        new = [item for item in found
               if (item.pid, item.create_time) not in self.members and item != self.leader]
        if not new:
            return
        for item in new:
            self.members[(item.pid, item.create_time)] = item
        self._record_cleanup()

    def _session_proof(self) -> bool:
        """Whether the leader's original session id still proves membership (POSIX).

        The session id is the leader's pid (``start_new_session=True``); while
        the leader is unreaped no other process can take that pid, so no other
        session can carry that id.
        """
        return self.sid is not None and self.leader is not None and self.process.returncode is None

    def _session_of(self, pid: int) -> Optional[int]:
        try:
            return os.getsid(pid)
        except OSError:
            return None

    def _scan_descendants(self) -> None:
        """Record every descendant of the leader that is ATTACHED when observed.

        On POSIX a descendant is recorded only while it is inside the leader's
        original session; one already outside it is noted as a detachment
        candidate instead (:meth:`_classify` decides). A recorded member stays
        attached even if it later calls ``setsid()``.
        """
        if self.leader_handle is None:
            return
        psutil = self.psutil
        found = []
        try:
            children = self.leader_handle.children(recursive=True)
        except psutil.Error:
            return
        session_rule = os.name == "posix" and self._session_proof()
        for child in children:
            try:
                identity = ProcessIdentity(pid=child.pid, create_time=child.create_time())
            except psutil.Error:
                continue
            key = (identity.pid, identity.create_time)
            if key in self.members:
                continue
            if session_rule and self._session_of(child.pid) != self.sid:
                self.outside[key] = identity
                continue
            found.append(identity)
        self._add_members(found)

    def _bound(self, identity: ProcessIdentity) -> Any:
        """A live psutil handle for ``identity``, or None when it is gone (or a zombie)."""
        psutil = self.psutil
        try:
            proc = psutil.Process(identity.pid)
            if proc.create_time() != identity.create_time:
                return None
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
            return proc
        except psutil.Error:
            return None

    def _leader_running(self) -> bool:
        if self.leader is None:
            return self.process.poll() is None
        return self._bound(self.leader) is not None

    def _classify(self) -> tuple[list[Any], list[Any], list[Any]]:
        """``(attached, detached, unconfirmed)``: live processes of this session, leader excluded.

        - ATTACHED (signalled at shutdown): a recorded member, by identity,
          whatever its session now is; or (POSIX, while the unreaped leader
          reserves its session id) any process inside the leader's original
          session, which is then recorded; or (no session proof) a tagged
          process that descends from a recorded identity.
        - DETACHED (never signalled): proven ours (it carries the tag, or it
          was seen as a descendant of the leader), created after the leader,
          never recorded, and outside the leader's original session. A
          different process group alone is not detachment.
        - UNCONFIRMED (never signalled; blocks shutdown confirmation): a
          tagged process none of the rules above decides.

        An unreadable environment (``AccessDenied``, or the ``SystemError``
        psutil 7.2 raises on macOS under load) counts as untagged. Only a
        process with no other tie depends on the tag: one inside the leader's
        session, recorded, or seen as a descendant is classified without it.
        """
        psutil = self.psutil
        me = os.getpid()
        attached: dict[int, Any] = {}
        for identity in self.members.values():
            proc = self._bound(identity)
            if proc is not None:
                attached[proc.pid] = proc
        self._scan_descendants()
        for identity in self.members.values():
            proc = self._bound(identity)
            if proc is not None:
                attached[proc.pid] = proc
        session_rule = os.name == "posix" and self._session_proof()
        recorded = set(self.members) | ({(self.leader.pid, self.leader.create_time)} if self.leader else set())
        leader_created = self.leader.create_time if self.leader is not None else None
        detached: dict[int, Any] = {}
        unconfirmed: dict[int, Any] = {}
        for proc in psutil.process_iter():
            try:
                if proc.pid == me or proc.pid in attached or (self.leader and proc.pid == self.leader.pid):
                    continue
                sid = self._session_of(proc.pid) if session_rule else None
                in_session = session_rule and sid is not None and sid == self.sid
                tagged = False
                if in_session or _same_user(proc):
                    try:
                        tagged = proc.environ().get(SESSION_TAG_ENV) == self.request.process_tag
                    except (psutil.AccessDenied, psutil.ZombieProcess, SystemError):
                        # SystemError: psutil 7.2 on macOS, proc_environ under load
                        # (KERN_PROCARGS2 failing with errno 0); unreadable, as AccessDenied.
                        tagged = False
                created = proc.create_time()
                seen_outside = (proc.pid, created) in self.outside
                if not (in_session or tagged or seen_outside):
                    continue
                if proc.status() == psutil.STATUS_ZOMBIE:
                    continue
                identity = ProcessIdentity(pid=proc.pid, create_time=created)
                if in_session:
                    attached[proc.pid] = proc
                    self._add_members([identity])
                    continue
                if session_rule:
                    if (sid is not None and leader_created is not None and created >= leader_created):
                        detached[proc.pid] = proc
                    else:
                        unconfirmed[proc.pid] = proc
                    continue
                descends = False
                for parent in proc.parents():
                    try:
                        if (parent.pid, parent.create_time()) in recorded:
                            descends = True
                            break
                    except psutil.Error:
                        continue
                if tagged and descends:
                    attached[proc.pid] = proc
                    self._add_members([identity])
                else:
                    unconfirmed[proc.pid] = proc
            except psutil.Error:
                continue
        return list(attached.values()), list(detached.values()), list(unconfirmed.values())

    def _kill_attached(self) -> tuple[bool, list[Any], list[Any]]:
        """Kill every attached process; return (none left, detached, unconfirmed).

        Detached and unconfirmed processes are never signalled.
        """
        psutil = self.psutil
        attached, detached, unconfirmed = self._classify()
        for _ in range(3):
            if not attached:
                break
            for proc in attached:
                try:
                    proc.kill()  # psutil refuses a pid whose create time changed
                except psutil.Error:
                    pass
            psutil.wait_procs(attached, timeout=_KILL_WAIT_S)
            attached, detached, unconfirmed = self._classify()
        return not attached, detached, unconfirmed

    def _wait_detached(self, detached: list[Any], deadline: float) -> tuple[DetachedSurvivor, ...]:
        """Wait until ``deadline`` for detached processes to exit; return those still alive.

        Nothing is signalled. The survivors are re-classified once at the end
        (while the leader still reserves its session id), so a process a
        survivor started meanwhile is reported too.
        """
        psutil = self.psutil
        if detached:
            psutil.wait_procs(detached, timeout=max(0.0, deadline - time.monotonic()))
        _, still, _ = self._classify()
        survivors = []
        for proc in still:
            try:
                identity = ProcessIdentity(pid=proc.pid, create_time=proc.create_time())
            except psutil.Error:
                continue
            try:
                cwd: Optional[str] = proc.cwd() or None
            except psutil.Error:
                cwd = None
            if cwd is not None and not os.path.isabs(cwd):
                cwd = None
            survivors.append(DetachedSurvivor(identity=identity, cwd=cwd))
        return tuple(sorted(survivors, key=lambda item: item.identity.pid))

    # -- leader signals ------------------------------------------------------

    def _wait_leader(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for the leader to exit, without reaping it."""
        end = time.monotonic() + timeout_s
        while self._leader_running():
            if time.monotonic() >= end:
                return False
            time.sleep(min(self.backend.poll_interval_s, 0.05))
        return True

    def _signal_leader(self, sig: int) -> None:
        handle = self.leader_handle if self.leader is not None else None
        if handle is not None:
            try:
                handle.send_signal(sig)
            except self.psutil.Error:
                pass
            return
        if self.process.poll() is None:
            try:
                self.process.send_signal(sig)
            except OSError:
                pass

    def _interrupt_leader(self) -> None:
        self.escalated = True
        self._signal_leader(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)

    def _kill_leader(self) -> None:
        self.escalated = True
        self._signal_leader(signal.SIGKILL if os.name != "nt" else signal.SIGTERM)

    # -- control loop ----------------------------------------------------------

    def _window(self) -> tuple[bool, bool, Optional[float]]:
        """``(open, idle, started)`` of the quiescence window of the latest result."""
        last = self.verdict.last_result
        if last is None:
            return False, False, None
        index = last[0]
        later_init = self.verdict.last_init_index
        if later_init is not None and later_init > index:
            return False, False, None
        with self.cond:
            seen = self.event_times.get(index)
            forward = self.last_forward
        if seen is None:
            return False, False, None
        idle = forward is None or forward <= seen
        return True, idle, max(seen, forward) if forward is not None else seen

    def _check_inits(self) -> bool:
        inits = self.verdict.inits
        while self.checked_inits < len(inits):
            _, event = inits[self.checked_inits]
            self.checked_inits += 1
            problem = init_problem(
                event, session_id=self.request.session_id, cwd=self.request.cwd,
                permission_mode=AGENT_PERMISSION_MODE,
            )
            if problem is not None and self.problem is None:
                self.problem = (event, problem)
        return self.problem is not None

    def _decide(self) -> tuple[StopKind, str]:
        """Run the control loop until the session must end; return (stop kind, how)."""
        deadline = self.spawned + self.timeout_s
        quiet = self.bounds.quiescence_s
        while True:
            with self.cond:
                self.cond.wait(timeout=self.backend.poll_interval_s)
                stop_request = self.stop_request
                errors = bool(self.errors)
            now = time.monotonic()
            if now - self.last_scan >= self.backend.member_scan_interval_s:
                self.last_scan = now
                self._scan_descendants()
            if errors:
                return StopKind.PROCESS_EXIT, "kill"
            if self._check_inits():
                return StopKind.INIT_MISMATCH, "kill"
            if stop_request is not None:
                return stop_request.kind.stop_kind, stop_request.mode.value
            if not self._leader_running():
                return StopKind.PROCESS_EXIT, "exited"
            open_, _, started = self._window()
            if open_ and started is not None and now - started >= quiet:
                return StopKind.QUIESCENT, StopMode.FINISH_TURN.value
            if now >= deadline:
                if open_:
                    return StopKind.QUIESCENT, StopMode.FINISH_TURN.value
                return StopKind.TIMEOUT, StopMode.INTERRUPT.value

    def _interrupt(self) -> None:
        """Send the interrupt and wait B_res for a result not already observed."""
        mark = self.verdict.events_seen
        self.writes.put(interrupt_request_line(f"llm-kit-stop-{uuid.uuid4().hex}"))
        end = time.monotonic() + self.bounds.result_wait_s
        while time.monotonic() < end:
            if self.verdict.has_result_after(mark - 1):
                return
            if not self._leader_running():
                return
            with self.cond:
                self.cond.wait(timeout=self.backend.poll_interval_s)

    def _shutdown(self, stop_kind: StopKind, how: str) -> tuple[bool, tuple[DetachedSurvivor, ...]]:
        """Stop the session; return ``(shutdown confirmed, detached survivors)``.

        Confirmed: the leader and every attached member are gone by identity
        and stdout was drained. Detached processes are never signalled; they
        get up to B_int (capped so the whole shutdown stays within the
        shutdown allowance) to exit, and those still alive are the survivors,
        persisted in ``cleanup.json``.
        """
        began = time.monotonic()
        self.close_reason = stop_kind
        if how == StopMode.INTERRUPT.value:
            _, idle, _ = self._window()
            if not idle and self._leader_running():
                self._interrupt()
        self.writes.put(_CLOSE)
        if how == "kill":
            self._kill_leader()
        elif how != "exited" and not self._wait_leader(self.bounds.eof_wait_s):
            self._interrupt_leader()
            if not self._wait_leader(self.bounds.sigint_wait_s):
                self._kill_leader()
        if not self._wait_leader(_KILL_WAIT_S):
            self._kill_leader()
            self._wait_leader(_KILL_WAIT_S)
        leader_gone = not self._leader_running()
        none_left, detached, unconfirmed = self._kill_attached()
        deadline = min(time.monotonic() + self.bounds.sigint_wait_s, began + self.bounds.shutdown_allowance_s)
        survivors = self._wait_detached(detached, deadline)
        if survivors:
            listed = ", ".join(f"{item.identity.pid} (cwd {item.cwd})" for item in survivors)
            print(
                f"llm-kit agent session {self.request.session_id}: WARNING: detached processes carrying "
                f"its tag are still alive and were not signalled; the workspace is not quiescent and "
                f"must not be finalized, reused, merged or reaped until they are gone: {listed}",
                file=sys.stderr,
            )
        if unconfirmed:
            print(
                f"llm-kit agent session {self.request.session_id}: processes carrying its tag "
                f"could not be proven its own and were not signalled: {sorted(p.pid for p in unconfirmed)}",
                file=sys.stderr,
            )
        try:
            self.process.wait(timeout=_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            leader_gone = False
        drained = self.stdout_done.wait(_DRAIN_WAIT_S)
        confirmed = leader_gone and none_left and not unconfirmed and drained
        if confirmed or survivors:
            self._record_cleanup(confirmed=confirmed, survivors=survivors)
        return confirmed, survivors

    def execute(self) -> SessionOutcome:
        threads = [
            threading.Thread(target=self._read_stdout, name="claude-session-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="claude-session-stderr", daemon=True),
            threading.Thread(target=self._drive_stdin, name="claude-session-stdin", daemon=True),
        ]
        self._identify_leader()
        for thread in threads:
            thread.start()
        try:
            stop_kind, how = self._decide()
        except BaseException:
            # KeyboardInterrupt or an unexpected error in the control loop: no
            # process of this session may outlive the caller's exception.
            self._shutdown(StopKind.PROCESS_EXIT, "kill")
            raise
        counted_s = time.monotonic() - self.spawned
        confirmed, survivors = self._shutdown(stop_kind, how)
        for thread in threads:
            thread.join(timeout=_DRAIN_WAIT_S)
        if any(thread.is_alive() for thread in threads):
            confirmed = False
        if self.errors:
            raise _ChannelFailure(
                f"the agent session's channel failed after the spawn: {self.errors[0]!r}; "
                f"every owned process was stopped (confirmed: {confirmed})"
            ) from self.errors[0]
        return self._outcome(stop_kind, counted_s, confirmed, survivors)

    # -- the outcome ---------------------------------------------------------

    def _stderr_text(self) -> str:
        return b"".join(self.stderr_tail).decode("utf-8", errors="replace").strip()

    def _halt_kind(self, text: str) -> Optional[str]:
        kind = halt.classify_claude_exception(RuntimeError(text))
        return kind if kind in SESSION_HALT_KINDS else None

    def _outcome(
        self, stop_kind: StopKind, counted_s: float, confirmed: bool,
        survivors: tuple[DetachedSurvivor, ...],
    ) -> SessionOutcome:
        self._check_inits()
        request = self.request
        inits = self.verdict.inits
        last = self.verdict.last_result
        exit_code = self.process.returncode
        if self.problem is not None and not (stop_kind is StopKind.TIMEOUT or stop_kind.is_operator):
            stop_kind = StopKind.INIT_MISMATCH
        session_id, cwd = request.session_id, request.cwd
        valid = self.verdict.first_valid_init
        if self.problem is None and valid is not None:
            seen_cwd = valid[1].get("cwd")
            if isinstance(seen_cwd, str) and os.path.isabs(seen_cwd):
                cwd = Path(seen_cwd)
        response: Optional[LLMResponse] = None
        subtype: Optional[str] = None
        result_error = False
        result_text = ""
        if last is not None and self.problem is None:
            event = last[1]
            subtype = str(event.get("subtype") or ("error" if event.get("is_error") else "success"))
            response = _claude_envelope_response(
                dict(event), model=request.model, capabilities=self.backend.capabilities,
                options=request.options, contract_plan=self.contract_plan, controls=self.controls,
                wall_ms=int(counted_s * 1000), attempts=1, started_at=self.started_at,
            )
            result_error = bool(event.get("is_error"))
            body = event.get("result") if isinstance(event.get("result"), str) else ""
            status = event.get("api_error_status")
            result_text = f'subtype {subtype} ("api_error_status":{status}): {body}' if status else f"subtype {subtype}: {body}"
            if result_error:
                code = self._halt_kind(result_text + "\n" + self._stderr_text()) or SessionFailureKind.RESULT_ERROR.value
                response = replace(response, status=ERROR, error=ResponseError(code, result_text[:2000]))
            else:
                try:
                    response = finalize_contract(self.contract_plan, response)
                except OutputContractViolation as violation:
                    response = violation.response
        failure = None
        if not stop_kind.is_operator:
            failure = self._failure(stop_kind, inits, last, exit_code, result_error, result_text)
        return SessionOutcome(
            response=response, session_id=session_id, cwd=cwd, stop_kind=stop_kind,
            result_subtype=subtype if response is not None else None,
            shutdown_confirmed=confirmed, failure=failure, detached_survivors=survivors,
        )

    def _failure(
        self, stop_kind: StopKind, inits: tuple, last: Any, exit_code: Optional[int],
        result_error: bool, result_text: str,
    ) -> Optional[SessionFailure]:
        stderr = self._stderr_text()
        tail = f"; stderr: {stderr[-2000:]}" if stderr else ""
        own_exit = exit_code is not None and exit_code != 0 and not self.escalated
        if stop_kind is StopKind.TIMEOUT:
            return SessionFailure(
                kind=SessionFailureKind.TIMEOUT,
                message=f"the attempt timeout of {self.timeout_s:g} s was reached before the session was quiescent",
                exit_code=exit_code,
            )
        if self.problem is not None:
            event, problem = self.problem
            return SessionFailure(
                kind=SessionFailureKind.INIT_MISMATCH, message=f"init event mismatch: {problem}",
                exit_code=exit_code, observed_session_id=_observed(event.get("session_id")),
                observed_cwd=_observed(event.get("cwd")),
            )
        if not inits:
            message = f"claude ended without a system:init event (exit {exit_code}){tail}"
            return SessionFailure(kind=SessionFailureKind.NO_INIT, message=message,
                                  halt_kind=self._halt_kind(stderr), exit_code=exit_code)
        if last is None:
            message = f"claude ended without a result event (exit {exit_code}){tail}"
            return SessionFailure(kind=SessionFailureKind.NO_RESULT, message=message,
                                  halt_kind=self._halt_kind(stderr), exit_code=exit_code)
        if own_exit:
            message = f"claude exited with status {exit_code}{tail}"
            return SessionFailure(kind=SessionFailureKind.NONZERO_EXIT, message=message,
                                  halt_kind=self._halt_kind(stderr), exit_code=exit_code)
        if result_error:
            return SessionFailure(
                kind=SessionFailureKind.RESULT_ERROR, message=result_text[:2000] or "error result",
                halt_kind=self._halt_kind(result_text + "\n" + stderr), exit_code=exit_code,
            )
        return None


__all__ = ["ClaudeAgentSession", "interrupt_request_line", "user_message_line"]
