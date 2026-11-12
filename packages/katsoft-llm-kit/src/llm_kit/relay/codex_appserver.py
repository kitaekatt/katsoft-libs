"""Stdio client for ``codex app-server`` (relay Codex channel, unit C2).

The client owns one app-server child (its own process group), speaks the
newline-delimited JSON-RPC dialect measured in plan-codex.md E-K0..E-K12 (no
``jsonrpc`` field), and exposes only what the Codex channel needs: start or
resume a thread under a verified policy, run one turn at a time, interrupt,
close.  It is standard library only.

Safety stance (a remotely triggered turn never runs unsandboxed and never waits
on an approval nobody can answer):

* ``approvalPolicy`` is always ``never`` and ``approvalsReviewer`` always
  ``user``.  ``thread/start`` and ``thread/resume`` report the effective
  configuration; any difference from the request raises
  :class:`CodexConfigMismatch` and the thread is refused for turns.
* An approval-type server request is answered ``cancel`` where its schema
  offers it, otherwise with a JSON-RPC error, and the client ALWAYS sends
  ``turn/interrupt`` too (a plain ``decline`` lets the turn run on).  The turn
  result is ``ok=False`` with status ``approval_requested`` whatever the turn
  then does.
* Any other server request, and any unknown method, gets a JSON-RPC error.
* A child exit, timeout, JSON-RPC error or protocol violation raises or
  yields ``ok=False`` with the cause; never an empty success.
* A turn is judged only by the ``turn/completed`` whose ``turn.id`` equals the
  id from this client's ``turn/start`` response.

Threading.  One reader thread parses stdout and runs every event callback, so
callbacks are ordered; a second thread forwards stderr lines as ``stderr``
events (serialized with the others, but not ordered against them).  An
exception raised by ``on_event`` is kept and re-raised by the next API call
(``close`` raises it after cleanup).  No callback runs after ``close`` returns.

POSIX only: process-group handling is not measured on Windows, so ``start``
refuses there.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence

__all__ = [
    "AppServerEvent",
    "AppServerInfo",
    "CodexAppServer",
    "CodexAppServerError",
    "CodexChildExited",
    "CodexConfigMismatch",
    "CodexNoRollout",
    "CodexProtocolError",
    "CodexRequestTimeout",
    "CodexRpcError",
    "CodexThreadBusy",
    "ThreadInfo",
    "ThreadPolicy",
    "TurnHandle",
    "TurnInProgress",
    "TurnResult",
]

EventKind = Literal[
    "turn_accepted", "turn_started", "agent_message", "item_started", "item_completed",
    "turn_completed", "codex_error", "hook", "approval_cancelled",
    "request_refused", "thread_status", "stderr", "exited",
]

_ALLOWED_SANDBOXES = ("read-only", "workspace-write")
_STDERR_TAIL = 50
_RPC_REFUSED = -32000
_RPC_METHOD_NOT_FOUND = -32601


# ---------------------------------------------------------------- errors

class CodexAppServerError(RuntimeError):
    """Base class of every error this module raises about the app-server."""


class CodexChildExited(CodexAppServerError):
    """The app-server child exited (or its pipes closed)."""


class CodexRequestTimeout(CodexAppServerError):
    """A JSON-RPC request got no response within ``request_timeout_s``."""


class CodexProtocolError(CodexAppServerError):
    """The child sent something this client cannot interpret."""


class CodexRpcError(CodexAppServerError):
    """The child answered a request with a JSON-RPC error."""

    def __init__(self, method: str, code: Any, message: str, data: Any = None) -> None:
        super().__init__(f"{method}: JSON-RPC error {code}: {message}")
        self.method = method
        self.code = code
        self.rpc_message = message
        self.data = data


class CodexConfigMismatch(CodexAppServerError):
    """Codex reported an effective configuration different from the request."""

    def __init__(self, requested: Mapping[str, Any], effective: Mapping[str, Any],
                 thread_id: Optional[str] = None) -> None:
        fields = ", ".join(sorted(requested))
        super().__init__(
            f"effective thread configuration differs from the request in: {fields} "
            f"(requested {dict(requested)!r}, effective {dict(effective)!r})"
        )
        self.requested = dict(requested)
        self.effective = dict(effective)
        self.thread_id = thread_id


class CodexNoRollout(CodexAppServerError):
    """Resume of a thread that never ran a turn (no rollout file yet)."""


class CodexThreadBusy(CodexAppServerError):
    """Another process holds the thread's writer lock."""


class TurnInProgress(CodexAppServerError):
    """A turn of this thread is already running (started by any client)."""


# ---------------------------------------------------------------- frozen types

@dataclass(frozen=True)
class AppServerInfo:
    pid: int
    pgid: int
    codex_version: Optional[str]    # from the initialize userAgent
    started_at: float


@dataclass(frozen=True)
class ThreadPolicy:
    """What the caller asks for. Approval policy ``never`` and approvals
    reviewer ``user`` are constants, not parameters."""
    cwd: str
    sandbox: str
    network: bool
    exclude_tmp: bool


@dataclass(frozen=True)
class ThreadInfo:
    """What Codex reports. ``sandbox`` is normalized (workspaceWrite ->
    workspace-write). A readOnly sandbox does not report the /tmp exclusions;
    for it they carry the requested values and are not compared."""
    thread_id: str
    cwd: str
    sandbox: str
    approval_policy: str
    approvals_reviewer: str
    network_access: bool
    exclude_slash_tmp: bool
    exclude_tmpdir_env_var: bool
    rollout_path: Optional[str]
    model: Optional[str]


@dataclass(frozen=True)
class AppServerEvent:
    kind: EventKind
    thread_id: Optional[str]
    turn_id: Optional[str]
    at: float
    data: Mapping[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """status: completed | failed | interrupted | approval_requested | timeout | exited.
    ``completed`` with ``ok=False`` means the turn ended without a final answer."""
    ok: bool
    status: str
    turn_id: Optional[str]
    final_message: Optional[str]
    error: Optional[str]


# ---------------------------------------------------------------- internals

class _Pending:
    def __init__(self, method: str, hook: Optional[Callable[["_Pending", Any], None]] = None) -> None:
        self.method = method
        self.hook = hook
        self.event = threading.Event()
        self.message: Optional[dict] = None
        self.exc: Optional[BaseException] = None
        self.meta: Dict[str, Any] = {}


class _Turn:
    def __init__(self, thread_id: str, turn_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.done = threading.Event()
        self.result: Optional[TurnResult] = None
        self.approval: Optional[Dict[str, Any]] = None
        self.interrupt_error: Optional[str] = None
        self.codex_error: Optional[str] = None
        self.timeout_note: Optional[str] = None   # set before the timeout interrupt is sent


class TurnHandle:
    """One accepted turn. ``wait`` returns its :class:`TurnResult`."""

    def __init__(self, client: "CodexAppServer", turn: _Turn) -> None:
        self._client = client
        self._turn = turn
        self.turn_id = turn.turn_id
        self.thread_id = turn.thread_id

    def wait(self, timeout_s: float) -> TurnResult:
        """Block until the turn ends. On timeout, interrupt it and return
        ``ok=False`` with status ``timeout``."""
        client, turn = self._client, self._turn
        client._raise_pending()
        if not turn.done.wait(timeout_s):
            # Mark first: the interrupt's own turn/completed must read as a timeout.
            turn.timeout_note = f"turn did not finish within {timeout_s}s; interrupt requested"
            note = ""
            try:
                client.interrupt(turn.thread_id, turn.turn_id)
            except CodexAppServerError as exc:
                note = f"; interrupt failed: {exc}"
            client._finish(turn, TurnResult(False, "timeout", turn.turn_id, None, turn.timeout_note + note))
        client._raise_pending()
        assert turn.result is not None
        return turn.result


def _kebab(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", camel).lower()


def _version_from_user_agent(agent: Any) -> Optional[str]:
    if not isinstance(agent, str):
        return None
    match = re.match(r"^[^/\s]+/(\S+)", agent)
    return match.group(1) if match else None


def _cancel_command(params: Mapping[str, Any]) -> Optional[dict]:
    offered = params.get("availableDecisions")
    if isinstance(offered, list) and "cancel" not in offered:
        return None
    return {"decision": "cancel"}


def _cancel_file(_params: Mapping[str, Any]) -> Optional[dict]:
    return {"decision": "cancel"}


def _cancel_elicitation(_params: Mapping[str, Any]) -> Optional[dict]:
    return {"action": "cancel"}


def _no_cancel(_params: Mapping[str, Any]) -> Optional[dict]:
    return None


# method -> builder of the cancel result, or None when the schema offers no
# cancel (the client then answers with a JSON-RPC error). The seven of E-K4.
_APPROVAL_METHODS: Dict[str, Callable[[Mapping[str, Any]], Optional[dict]]] = {
    "item/commandExecution/requestApproval": _cancel_command,
    "item/fileChange/requestApproval": _cancel_file,
    "mcpServer/elicitation/request": _cancel_elicitation,
    "item/permissions/requestApproval": _no_cancel,
    "item/tool/requestUserInput": _no_cancel,
    "applyPatchApproval": _no_cancel,
    "execCommandApproval": _no_cancel,
}


class CodexAppServer:
    """One ``codex app-server`` child over stdio. See the module docstring."""

    def __init__(self, argv: Sequence[str], *, cwd: str, env: Optional[Mapping[str, str]] = None,
                 on_event: Optional[Callable[[AppServerEvent], None]] = None,
                 request_timeout_s: float = 60.0, client_name: str = "llm-kit-relay") -> None:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_event = on_event
        self._request_timeout_s = request_timeout_s
        self._client_name = client_name

        self._proc: Optional[subprocess.Popen] = None
        self._pgid: Optional[int] = None
        self._reader: Optional[threading.Thread] = None
        self._err_reader: Optional[threading.Thread] = None
        self._lock = threading.RLock()          # state
        self._write_lock = threading.Lock()     # stdin
        self._emit_lock = threading.RLock()     # callbacks + closed flag
        self._closed = False
        self._exited = False
        self._exit_error: Optional[str] = None
        self._fatal: Optional[BaseException] = None
        self._cb_error: Optional[BaseException] = None
        self._next_id = 1
        self._pending: Dict[int, _Pending] = {}
        self._async_hooks: Dict[Any, Callable[[dict], None]] = {}
        self._thread_status: Dict[str, str] = {}
        self._refused_threads: Dict[str, CodexConfigMismatch] = {}
        self._turns: Dict[str, _Turn] = {}              # turn id -> turn this client started
        self._foreign_active: Dict[str, set] = {}       # thread id -> turn ids started by others
        self._stderr_tail: deque = deque(maxlen=_STDERR_TAIL)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> AppServerInfo:
        if os.name != "posix":
            raise CodexAppServerError(
                "codex app-server client supports POSIX only (process groups are not measured on Windows)")
        if self._proc is not None:
            raise CodexAppServerError("start() was already called")
        self._proc = subprocess.Popen(
            self._argv, cwd=self._cwd, env=self._env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, start_new_session=True)
        self._pgid = os.getpgid(self._proc.pid)
        self._reader = threading.Thread(target=self._read_loop, name="codex-appserver-reader", daemon=True)
        self._err_reader = threading.Thread(target=self._err_loop, name="codex-appserver-stderr", daemon=True)
        self._reader.start()
        self._err_reader.start()
        try:
            result = self._request("initialize", {
                "clientInfo": {"name": self._client_name, "version": "0"},
                "capabilities": {"experimentalApi": True},
            })
            self._send({"method": "initialized"})
        except BaseException:
            self.close(grace_s=1.0)
            raise
        return AppServerInfo(
            pid=self._proc.pid, pgid=self._pgid,
            codex_version=_version_from_user_agent(result.get("userAgent") if isinstance(result, dict) else None),
            started_at=time.time())

    def close(self, grace_s: float = 5.0) -> None:
        """Close stdin, wait up to ``grace_s``, then kill the whole group."""
        proc = self._proc
        if proc is not None and not self._closed:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass  # pipe already broken: the child is gone, which is the goal
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                pass
            if self._pgid is not None:
                try:
                    os.killpg(self._pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # nothing left in the group
            proc.wait()
            for thread in (self._reader, self._err_reader):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=5.0)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        with self._emit_lock:
            self._closed = True
        self._raise_pending()

    # ------------------------------------------------------------ threads

    def start_thread(self, policy: ThreadPolicy, *, developer_instructions: str) -> ThreadInfo:
        if not isinstance(developer_instructions, str) or not developer_instructions.strip():
            raise ValueError("developer_instructions must be a non-empty string")
        params = self._thread_params(policy)
        params["developerInstructions"] = developer_instructions
        result = self._request("thread/start", params, hook=self._on_thread_response)
        return self._accept_thread(result, policy)

    def resume_thread(self, thread_id: str, policy: ThreadPolicy) -> ThreadInfo:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be a non-empty string")
        params = self._thread_params(policy)
        params["threadId"] = thread_id
        try:
            result = self._request("thread/resume", params, hook=self._on_thread_response)
        except CodexRpcError as exc:
            if exc.code == -32600 and "no rollout found" in exc.rpc_message:
                raise CodexNoRollout(f"thread {thread_id} never ran a turn; it cannot be resumed") from exc
            if exc.code == -32600 and "already has an active writer" in exc.rpc_message:
                raise CodexThreadBusy(f"thread {thread_id} is open in another Codex process") from exc
            raise
        return self._accept_thread(result, policy)

    def thread_status(self, thread_id: str) -> str:
        """idle | active | systemError (| notLoaded), from the latest report."""
        self._raise_pending()
        with self._lock:
            status = self._thread_status.get(thread_id)
        if status is None:
            raise CodexAppServerError(f"no status known for thread {thread_id}")
        return status

    @staticmethod
    def _thread_params(policy: ThreadPolicy) -> dict:
        if policy.sandbox not in _ALLOWED_SANDBOXES:
            raise ValueError(f"policy.sandbox must be one of {_ALLOWED_SANDBOXES}, got {policy.sandbox!r}")
        if not isinstance(policy.cwd, str) or not os.path.isabs(policy.cwd):
            raise ValueError(f"policy.cwd must be an absolute path, got {policy.cwd!r}")
        return {
            "cwd": policy.cwd,
            "sandbox": policy.sandbox,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "config": {"sandbox_workspace_write": {
                "network_access": bool(policy.network),
                "exclude_slash_tmp": bool(policy.exclude_tmp),
                "exclude_tmpdir_env_var": bool(policy.exclude_tmp),
            }},
        }

    def _accept_thread(self, result: Any, policy: ThreadPolicy) -> ThreadInfo:
        try:
            thread = result["thread"]
            sandbox = result["sandbox"]
            sandbox_type = str(sandbox["type"])
            kind = _kebab(sandbox_type)
            if kind == "workspace-write":
                exclude_slash = bool(sandbox.get("excludeSlashTmp", False))
                exclude_env = bool(sandbox.get("excludeTmpdirEnvVar", False))
            else:
                exclude_slash = exclude_env = bool(policy.exclude_tmp)
            info = ThreadInfo(
                thread_id=str(thread["id"]),
                cwd=str(result["cwd"]),
                sandbox=kind,
                approval_policy=str(result["approvalPolicy"]),
                approvals_reviewer=str(result["approvalsReviewer"]),
                network_access=bool(sandbox.get("networkAccess", False)),
                exclude_slash_tmp=exclude_slash,
                exclude_tmpdir_env_var=exclude_env,
                rollout_path=thread.get("path"),
                model=result.get("model"),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise CodexProtocolError(f"unusable thread response: {exc!r}") from exc
        self._verify(info, policy)
        return info

    def _on_thread_response(self, _pending: _Pending, result: Any) -> None:
        """Reader thread: record the reported status before any later line (a
        status notification that follows the response must not be overwritten)."""
        try:
            thread = result["thread"]
            status = thread["status"]["type"]
            thread_id = str(thread["id"])
        except (KeyError, TypeError):
            return  # _accept_thread reports the malformed response
        with self._lock:
            self._thread_status[thread_id] = status

    def _verify(self, info: ThreadInfo, policy: ThreadPolicy) -> None:
        requested: Dict[str, Any] = {}
        effective: Dict[str, Any] = {}

        def check(name: str, want: Any, got: Any) -> None:
            if want != got:
                requested[name] = want
                effective[name] = got

        check("cwd", os.path.realpath(policy.cwd), os.path.realpath(info.cwd))
        check("sandbox", policy.sandbox, info.sandbox)
        check("approval_policy", "never", info.approval_policy)
        check("approvals_reviewer", "user", info.approvals_reviewer)
        check("network_access", bool(policy.network), info.network_access)
        if info.sandbox == "workspace-write":
            check("exclude_slash_tmp", bool(policy.exclude_tmp), info.exclude_slash_tmp)
            check("exclude_tmpdir_env_var", bool(policy.exclude_tmp), info.exclude_tmpdir_env_var)
        if requested:
            mismatch = CodexConfigMismatch(requested, effective, info.thread_id)
            with self._lock:
                self._refused_threads[info.thread_id] = mismatch
            raise mismatch

    # ------------------------------------------------------------ turns

    def start_turn(self, thread_id: str, text: str) -> TurnHandle:
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        self._raise_pending()
        with self._lock:
            if self._exited:
                raise CodexChildExited(self._exit_error or "codex app-server has exited")
            refused = self._refused_threads.get(thread_id)
            status = self._thread_status.get(thread_id)
        if refused is not None:
            raise CodexConfigMismatch(refused.requested, refused.effective, thread_id)
        if status == "active":
            raise TurnInProgress(f"thread {thread_id} has a turn running")
        pending_meta = {"thread_id": thread_id}
        self._request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
                      hook=self._on_turn_start_response, meta=pending_meta)
        turn = pending_meta.get("turn")
        if not isinstance(turn, _Turn):
            raise CodexProtocolError("turn/start produced no turn")
        return TurnHandle(self, turn)

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def _on_turn_start_response(self, pending: _Pending, result: Any) -> None:
        """Runs on the reader thread, before any later line is handled, so
        ``turn_accepted`` precedes every other event of the turn."""
        thread_id = pending.meta["thread_id"]
        try:
            turn_id = str(result["turn"]["id"])
            status = result["turn"].get("status")
        except (KeyError, TypeError, AttributeError) as exc:
            pending.exc = CodexProtocolError(f"unusable turn/start response: {exc!r}")
            return
        with self._lock:
            known = self._turns.get(turn_id)
            foreign = turn_id in self._foreign_active.get(thread_id, ())
            if foreign or (known is not None and not known.done.is_set()):
                pending.exc = TurnInProgress(
                    f"turn {turn_id} of thread {thread_id} is already running; the text was not started as its own turn")
                return
            turn = _Turn(thread_id, turn_id)
            self._turns[turn_id] = turn
            pending.meta["turn"] = turn
        self._emit("turn_accepted", thread_id, turn_id, {"status": status})

    # ------------------------------------------------------------ transport

    def _raise_pending(self) -> None:
        err = self._cb_error
        if err is not None:
            self._cb_error = None
            raise err
        if self._fatal is not None:
            raise self._fatal

    def _send(self, obj: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CodexAppServerError("client not started")
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CodexChildExited(f"codex app-server is not accepting input: {exc!r}") from exc

    def _request(self, method: str, params: Mapping[str, Any], *,
                 hook: Optional[Callable[[_Pending, Any], None]] = None,
                 meta: Optional[Dict[str, Any]] = None) -> Any:
        self._raise_pending()
        pending = _Pending(method, hook)
        if meta is not None:
            pending.meta = meta
        with self._lock:
            if self._exited:
                raise CodexChildExited(self._exit_error or "codex app-server has exited")
            rid = self._next_id
            self._next_id += 1
            self._pending[rid] = pending
        try:
            self._send({"id": rid, "method": method, "params": dict(params)})
        except CodexAppServerError:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        if not pending.event.wait(self._request_timeout_s):
            with self._lock:
                self._pending.pop(rid, None)
            raise CodexRequestTimeout(f"{method}: no response within {self._request_timeout_s}s")
        if pending.exc is not None:
            raise pending.exc
        message = pending.message or {}
        if "error" in message:
            err = message["error"] if isinstance(message["error"], dict) else {}
            raise CodexRpcError(method, err.get("code"), str(err.get("message")), err.get("data"))
        return message.get("result")

    def _request_nowait(self, method: str, params: Mapping[str, Any],
                        on_response: Callable[[dict], None]) -> None:
        """Send a request from the reader thread; the response hook runs on it too."""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._async_hooks[rid] = on_response
        self._send({"id": rid, "method": method, "params": dict(params)})

    # ------------------------------------------------------------ reader

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("not a JSON object")
                except ValueError as exc:
                    self._fail(CodexProtocolError(f"unparseable line from codex app-server: {line[:200]!r} ({exc})"))
                    continue
                try:
                    self._dispatch(message)
                except Exception as exc:  # noqa: BLE001 - a client bug must surface, not vanish
                    self._fail(CodexProtocolError(f"client failed handling {str(message)[:200]!r}: {exc!r}"))
        finally:
            self._on_eof()

    def _err_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for raw in proc.stderr:
            line = raw.rstrip("\n")
            with self._lock:
                self._stderr_tail.append(line)
            self._emit("stderr", None, None, {"line": line})

    def _fail(self, exc: CodexAppServerError) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = exc
            pendings = list(self._pending.values())
            self._pending.clear()
            turns = [t for t in self._turns.values() if not t.done.is_set()]
        for pending in pendings:
            pending.exc = exc
            pending.event.set()
        for turn in turns:
            self._finish(turn, TurnResult(False, "failed", turn.turn_id, None, str(exc)))
        self._emit("codex_error", None, None, {"stage": "protocol", "message": str(exc)})

    def _on_eof(self) -> None:
        proc = self._proc
        assert proc is not None
        if self._err_reader is not None and self._err_reader is not threading.current_thread():
            self._err_reader.join(timeout=2.0)
        try:
            code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            code = None
        with self._lock:
            tail = list(self._stderr_tail)[-5:]
            reason = f"codex app-server exited (code {code})" + (f"; stderr tail: {tail}" if tail else "")
            self._exited = True
            self._exit_error = reason
            pendings = list(self._pending.values())
            self._pending.clear()
            turns = [t for t in self._turns.values() if not t.done.is_set()]
        exc = CodexChildExited(reason)
        for pending in pendings:
            pending.exc = exc
            pending.event.set()
        for turn in turns:
            self._finish(turn, TurnResult(False, "exited", turn.turn_id, None,
                                          f"{reason} before the turn completed"))
        self._emit("exited", None, None, {"returncode": code, "stderr_tail": tail})

    def _finish(self, turn: _Turn, result: TurnResult) -> None:
        with self._lock:
            if turn.result is None:
                turn.result = result
        turn.done.set()

    def _emit(self, kind: str, thread_id: Optional[str], turn_id: Optional[str],
              data: Optional[Mapping[str, Any]] = None) -> None:
        event = AppServerEvent(kind=kind, thread_id=thread_id, turn_id=turn_id,  # type: ignore[arg-type]
                               at=time.time(), data=dict(data or {}))
        with self._emit_lock:
            callback = self._on_event
            if self._closed or callback is None:
                return
            try:
                callback(event)
            except BaseException as exc:  # noqa: BLE001 - kept and re-raised by the next API call
                if self._cb_error is None:
                    self._cb_error = exc

    # ------------------------------------------------------------ dispatch

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        has_id = "id" in message
        if method is None:
            if not has_id:
                raise CodexProtocolError("message with neither method nor id")
            self._on_response(message)
        elif has_id:
            self._on_server_request(message)
        else:
            self._on_notification(method, message.get("params") or {})

    def _on_response(self, message: dict) -> None:
        rid = message["id"]
        with self._lock:
            pending = self._pending.pop(rid, None)
            hook = self._async_hooks.pop(rid, None)
        if pending is None:
            if hook is not None:
                hook(message)
                return
            raise CodexProtocolError(f"response for unknown request id {rid!r}")
        if pending.hook is not None and "result" in message:
            pending.hook(pending, message["result"])
        pending.message = message
        pending.event.set()

    def _on_server_request(self, message: dict) -> None:
        method = str(message["method"])
        rid = message["id"]
        params = message.get("params") or {}
        builder = _APPROVAL_METHODS.get(method)
        if builder is None:
            self._send({"id": rid, "error": {"code": _RPC_METHOD_NOT_FOUND,
                                             "message": "not supported by this client"}})
            self._emit("request_refused", params.get("threadId"), params.get("turnId"),
                       {"method": method, "id": rid})
            return
        thread_id = params.get("threadId") or params.get("conversationId")
        turn_id = params.get("turnId")
        cancel = builder(params)
        if cancel is not None:
            self._send({"id": rid, "result": cancel})
            response = "cancel"
        else:
            self._send({"id": rid, "error": {"code": _RPC_REFUSED,
                                             "message": "approval refused by this client (never answered remotely)"}})
            response = "error"
        with self._lock:
            targets = self._interrupt_targets(thread_id, turn_id)
            for turn in targets:
                if turn.approval is None:
                    turn.approval = {"method": method, "reason": params.get("reason")}
        # Always interrupt: a refusal alone lets the turn run on.
        for target_thread, target_turn in self._interrupt_pairs(thread_id, turn_id, targets):
            self._request_nowait("turn/interrupt", {"threadId": target_thread, "turnId": target_turn},
                                 self._interrupt_hook(target_turn))
        self._emit("approval_cancelled", thread_id, turn_id,
                   {"method": method, "id": rid, "response": response, "reason": params.get("reason"),
                    "interrupted": [t for _, t in self._interrupt_pairs(thread_id, turn_id, targets)]})

    def _interrupt_targets(self, thread_id: Optional[str], turn_id: Optional[str]) -> List[_Turn]:
        if turn_id is not None:
            turn = self._turns.get(turn_id)
            return [turn] if turn is not None else []
        return [t for t in self._turns.values() if t.thread_id == thread_id and not t.done.is_set()]

    @staticmethod
    def _interrupt_pairs(thread_id: Optional[str], turn_id: Optional[str], targets: List[_Turn]):
        if turn_id is not None and thread_id is not None:
            return [(thread_id, turn_id)]
        return [(t.thread_id, t.turn_id) for t in targets]

    def _interrupt_hook(self, turn_id: str) -> Callable[[dict], None]:
        def hook(message: dict) -> None:
            if "error" in message:
                err = message["error"] if isinstance(message["error"], dict) else {}
                text = f"turn/interrupt failed: {err.get('message')}"
                with self._lock:
                    turn = self._turns.get(turn_id)
                    if turn is not None:
                        turn.interrupt_error = text
                self._emit("codex_error", None, turn_id, {"stage": "interrupt", "message": text})
        return hook

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        if method == "thread/status/changed":
            status = (params.get("status") or {}).get("type")
            if thread_id and isinstance(status, str):
                with self._lock:
                    self._thread_status[thread_id] = status
            self._emit("thread_status", thread_id, None, {"status": status})
        elif method == "turn/started":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            with self._lock:
                ours = turn_id in self._turns
                if not ours and thread_id and turn_id:
                    self._foreign_active.setdefault(thread_id, set()).add(turn_id)
            self._emit("turn_started", thread_id, turn_id, {"foreign": not ours})
        elif method == "turn/completed":
            self._on_turn_completed(thread_id, params.get("turn") or {})
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            turn_id = params.get("turnId")
            if method == "item/completed" and item.get("type") == "agentMessage":
                self._emit("agent_message", thread_id, turn_id,
                           {"text": item.get("text"), "phase": item.get("phase"), "item_id": item.get("id")})
            else:
                kind = "item_started" if method == "item/started" else "item_completed"
                self._emit(kind, thread_id, turn_id, {"item": item})
        elif method == "error":
            error = params.get("error") or {}
            message = error.get("message")
            turn_id = params.get("turnId")
            if params.get("willRetry") is False and turn_id:
                with self._lock:
                    turn = self._turns.get(turn_id)
                    if turn is not None:
                        turn.codex_error = message
            self._emit("codex_error", thread_id, turn_id,
                       {"message": message, "will_retry": params.get("willRetry")})
        elif method.startswith("hook/"):
            self._emit("hook", thread_id, params.get("turnId"), {"method": method, "params": dict(params)})
        # every other notification is ignored by design

    def _on_turn_completed(self, thread_id: Optional[str], turn: Mapping[str, Any]) -> None:
        turn_id = turn.get("id")
        with self._lock:
            ours = self._turns.get(turn_id) if turn_id else None
            if thread_id and turn_id:
                self._foreign_active.get(thread_id, set()).discard(turn_id)
        self._emit("turn_completed", thread_id, turn_id,
                   {"status": turn.get("status"), "error": turn.get("error"), "foreign": ours is None})
        if ours is not None:
            self._finish(ours, self._result_from_completed(ours, turn))

    @staticmethod
    def _result_from_completed(ours: _Turn, turn: Mapping[str, Any]) -> TurnResult:
        status = turn.get("status")
        final: Optional[str] = None
        for item in turn.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                final = item.get("text")
        tid = ours.turn_id
        if ours.timeout_note is not None and status == "interrupted":
            return TurnResult(False, "timeout", tid, None, ours.timeout_note)
        if ours.approval is not None:
            note = f"; {ours.interrupt_error}" if ours.interrupt_error else ""
            return TurnResult(False, "approval_requested", tid, None,
                              f"Codex requested approval ({ours.approval['method']}); it was cancelled "
                              f"and the turn interrupted (turn ended {status}){note}")
        if status == "completed":
            if isinstance(final, str) and final.strip():
                return TurnResult(True, "completed", tid, final, None)
            return TurnResult(False, "completed", tid, None, "turn completed without a final answer")
        if status == "failed":
            err = turn.get("error")
            message = err.get("message") if isinstance(err, dict) else None
            return TurnResult(False, "failed", tid, None, message or ours.codex_error or "turn failed")
        if status == "interrupted":
            return TurnResult(False, "interrupted", tid, None, "turn interrupted")
        return TurnResult(False, "failed", tid, None, f"unexpected turn status {status!r}")
