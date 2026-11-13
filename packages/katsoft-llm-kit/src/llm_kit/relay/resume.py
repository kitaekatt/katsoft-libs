"""Remote resume (amendment 3, unit R1): the relay runs ONE turn in an idle session for ``continue_session``.

This is the one place the relay starts a process. A ``continue_session`` call
that the service accepted has already stored its message and claimed it in
the same mailbox transaction (``claimed_by`` plus a ``resume_leases`` row with
phase ``claimed``, :meth:`llm_kit.relay.mailbox.Mailbox.send_message`). The
:class:`ResumeRunner` then, on a thread of its own:

1. sets the lease phase ``spawning`` and starts the child in a new session
   (its own process group) with ``LLM_KIT_RESUME_LEASE=<lease id>`` in its
   environment, so a relay that dies before it records the pid can still find
   the child;
2. records the child's pid, create time and process group (phase
   ``running``); every lease write is fenced by the lease id;
3. renews the lease every ``renew_s`` until the run's deadline, and stops
   renewing after it, so a hung run lets the lease expire;
4. judges the run (below) and ends the claim with
   :meth:`~llm_kit.relay.mailbox.Mailbox.finish_claim`: the reply
   (``answered`` or ``declined``), the terminal state, the cleared claim and
   the deleted lease in one transaction. A runner whose lease was taken over
   (:class:`~llm_kit.relay.mailbox.LeaseLost`) kills its own child's group and
   writes nothing.

Codex. :func:`llm_kit.adapters.codex_command.build_codex_app_server_argv`
(stdio, hooks and notify off, approval ``never``, ``workspace-write`` rooted at
the rollout's recorded cwd, network off) and the C2 client
(:mod:`llm_kit.relay.codex_appserver`): ``thread/resume`` under a verified
policy with ``/tmp`` and ``$TMPDIR`` excluded (:class:`CodexConfigMismatch`
declines), one ``turn/start`` whose text is the Codex channel's two-line
header then the body, ``wait``, ``close``. An approval request is cancelled and
the turn interrupted by C2; the message is declined. Judged by the
:class:`TurnResult` only.

Claude. ``claude -p --resume <id> --output-format stream-json --verbose
--permission-mode <mode>`` (``default`` only, relay.yaml), the prompt on stdin,
cwd = the transcript's cwd, ``CLAUDECODE`` and ``CLAUDE_CODE_*`` removed from
the environment. Success needs an ``init`` event whose ``session_id``, cwd and
``permissionMode`` equal the request (otherwise the child is killed at once
and the message declined ``config_mismatch``) and, after it, a ``result``
event of the same session with ``is_error`` false and a non-empty ``result``.
Measured with Claude Code 2.1.317 on 2026-11-13 (plan-codex H16): ``-p
--resume`` keeps the session id, and its ``init`` event names ``cwd`` and
``permissionMode``. Nothing answers a permission prompt: in ``-p`` with mode
``default`` a tool that needs permission is refused, not asked.

Orphan recovery (:meth:`ResumeRunner.recover`, at relay start and on every
maintenance sweep). A lease is an orphan when another relay instance took it,
when it expired, or when its recorded child is gone and no runner of this
relay owns it. A recorded child that is alive with the same pid AND create
time is terminated (SIGTERM to its group, a grace period, SIGKILL) and must be
gone before the claim is cleared; a pid whose create time differs is never
signalled. A ``spawning`` lease with no pid is matched to this user's
processes by the environment tag (psutil ``environ``). Every orphan is
declined with the reason through the fenced
:meth:`~llm_kit.relay.mailbox.Mailbox.recover_lease`. A surviving child is
never adopted: its stdout belonged to the dead relay, so its output cannot be
judged.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from llm_kit.relay.mailbox import LeaseLost, Mailbox

#: The environment variable that tags a resume child with its lease id.
LEASE_ENV = "LLM_KIT_RESUME_LEASE"
DEFAULT_LEASE_S = 90.0
DEFAULT_RENEW_S = 30.0
#: Seconds between SIGTERM and SIGKILL when a child group is stopped.
TERM_GRACE_S = 3.0
#: Seconds a stopped child may take to disappear before recovery waits for a later sweep.
GONE_WAIT_S = 5.0
#: A recorded create time matches the live process within this many seconds.
START_TOLERANCE_S = 1.0
_STDERR_TAIL = 2000

REASON_BEFORE_START = "relay restarted before the run started"
REASON_STOPPED = "relay restarted during the run; it was stopped"
REASON_LOST = "the run's result was lost; for Codex the turn may have run, read_session shows it"
REASON_EXPIRED = "the run stopped renewing its lease past its deadline; it was stopped"
REASON_RELAY_STOP = "the relay stopped during the run; it was stopped"


def new_lease_id() -> str:
    return "lse_" + secrets.token_hex(12)


def new_relay_instance() -> str:
    return "rin_" + secrets.token_hex(8)


def child_env(lease_id: str, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The child's environment: ``base`` (default: this process's) without ``CLAUDECODE`` and
    ``CLAUDE_CODE_*``, plus the lease tag."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env[LEASE_ENV] = lease_id
    return env


# --------------------------------------------------------------------------
# process identity and termination
# --------------------------------------------------------------------------

def _psutil() -> Any:
    try:
        import psutil  # noqa: WPS433 - the relay extra
    except ImportError as exc:  # pragma: no cover - environment specific
        raise RuntimeError("remote resume needs psutil; install katsoft-llm-kit[relay]") from exc
    return psutil


def create_time(pid: int) -> Optional[float]:
    """The live process ``pid``'s create time, or None when there is none (or it is a zombie)."""
    psutil = _psutil()
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return float(proc.create_time())
    except psutil.NoSuchProcess:
        return None


def same_process(pid: Optional[int], start: Optional[float]) -> bool:
    """Whether ``pid`` is alive AND was created at ``start`` (a reused pid is a different process)."""
    if pid is None or start is None:
        return False
    actual = create_time(pid)
    return actual is not None and abs(actual - start) <= START_TOLERANCE_S


def stop_group(pid: int, start: float, pgid: int, *, grace_s: float = TERM_GRACE_S,
               gone_wait_s: float = GONE_WAIT_S) -> bool:
    """Terminate the process group of the identity-checked child ``pid``; True once the child is gone.

    Never signals anything unless ``pid`` is alive with create time ``start`` and still leads ``pgid``.
    A child that is already gone is True at once.
    """
    if not same_process(pid, start):
        return True
    try:
        if os.getpgid(pid) != pgid:
            return False  # the process left its group: not ours to signal by group
    except ProcessLookupError:
        return True
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, gone_wait_s)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if not same_process(pid, start):
                _reap(pid)
                return True
            _reap(pid)
            time.sleep(0.05)
    return not same_process(pid, start)


def _reap(pid: int) -> None:
    """Collect ``pid`` if it is our own exited child (so it is not left a zombie)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # not our child: its parent reaps it


def tagged_processes(lease_id: str) -> list[tuple[int, float]]:
    """``(pid, create time)`` of every process of this user whose environment carries the lease tag."""
    psutil = _psutil()
    found: list[tuple[int, float]] = []
    for proc in psutil.process_iter():
        try:
            if proc.environ().get(LEASE_ENV) == lease_id:
                found.append((proc.pid, float(proc.create_time())))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ResumeJob:
    """One accepted ``continue_session``: the claim and what the run needs."""

    lease_id: str
    message_id: str
    address: str
    harness: str
    session_id: str
    cwd: str
    prompt: str
    timeout_s: float
    claude_permission_mode: str = "default"


@dataclass
class _Active:
    job: ResumeJob
    thread: Optional[threading.Thread] = None
    stop: Callable[[], None] = lambda: None
    stopped_by: Optional[str] = None
    lost: threading.Event = field(default_factory=threading.Event)


class ResumeRunner:
    """Runs claimed ``continue_session`` jobs and recovers orphaned leases. One per relay process.

    ``codex_argv_prefix`` and ``claude_argv`` replace the resolved launchers (tests point them at stubs).
    ``on_finish`` is called after a claim ended (the service wakes waiting ``get_replies`` calls).
    """

    def __init__(
        self,
        mailbox: Mailbox,
        *,
        relay_instance: Optional[str] = None,
        lease_s: float = DEFAULT_LEASE_S,
        renew_s: float = DEFAULT_RENEW_S,
        codex_argv_prefix: Optional[Sequence[str]] = None,
        claude_argv: Optional[Sequence[str]] = None,
        on_finish: Callable[[], None] = lambda: None,
        term_grace_s: float = TERM_GRACE_S,
    ) -> None:
        if renew_s <= 0 or lease_s <= renew_s:
            raise ValueError("lease_s must exceed renew_s, and both must be positive")
        self.mailbox = mailbox
        self.relay_instance = relay_instance or new_relay_instance()
        self.lease_s = lease_s
        self.renew_s = renew_s
        self._codex_prefix = list(codex_argv_prefix) if codex_argv_prefix is not None else None
        self._claude_argv = list(claude_argv) if claude_argv is not None else None
        self._on_finish = on_finish
        self._grace = term_grace_s
        self._lock = threading.Lock()
        self._active: dict[str, _Active] = {}
        #: Leases this relay fenced for recovery whose child has not stopped yet:
        #: new lease id -> (reason, the lease id the child's environment carries).
        self._taken: dict[str, tuple[str, str]] = {}
        self._closing = False

    # -- starting and stopping ------------------------------------------------------

    def start(self, job: ResumeJob) -> threading.Thread:
        """Run ``job`` on a new thread (its claim is already in the mailbox)."""
        active = _Active(job)
        thread = threading.Thread(target=self._run, args=(active,), name=f"llm-kit-resume-{job.lease_id}",
                                  daemon=True)
        active.thread = thread
        with self._lock:
            if self._closing:
                raise RuntimeError("the resume runner is closing")
            self._active[job.lease_id] = active
        thread.start()
        return thread

    def owns(self, lease_id: str) -> bool:
        """Whether a runner thread of this relay is still working on ``lease_id``."""
        with self._lock:
            active = self._active.get(lease_id)
            return active is not None and active.thread is not None and active.thread.is_alive()

    def close(self, timeout_s: float = 15.0) -> None:
        """Relay stop: stop every running child and let each run decline its message."""
        with self._lock:
            self._closing = True
            actives = list(self._active.values())
        for active in actives:
            active.stopped_by = REASON_RELAY_STOP
            active.stop()
        end = time.monotonic() + timeout_s
        for active in actives:
            if active.thread is not None:
                active.thread.join(timeout=max(0.0, end - time.monotonic()))

    # -- one run --------------------------------------------------------------------

    def _run(self, active: _Active) -> None:
        job = active.job
        renewer: Optional[threading.Thread] = None
        done = threading.Event()
        try:
            state, body = "declined", "the run did not finish"
            try:
                self.mailbox.set_lease_phase(job.lease_id, "spawning")
                deadline = time.monotonic() + job.timeout_s
                renewer = threading.Thread(target=self._renew, args=(active, deadline, done),
                                           name=f"llm-kit-resume-renew-{job.lease_id}", daemon=True)
                renewer.start()
                if job.harness == "codex":
                    state, body = self._run_codex(active, deadline)
                else:
                    state, body = self._run_claude(active, deadline)
            except LeaseLost:
                raise
            except Exception as exc:  # noqa: BLE001 -- a run defect still ends the claim, loudly
                traceback.print_exc(file=sys.stderr)
                state, body = "declined", f"the resume run failed: {type(exc).__name__}: {str(exc)[:500]}"
            if active.stopped_by is not None:
                state, body = "declined", active.stopped_by
            done.set()
            if active.lost.is_set():
                raise LeaseLost(job.lease_id)
            self.mailbox.finish_claim(job.lease_id, state, body)
            self._on_finish()
        except LeaseLost:
            active.stop()  # another writer recovered the lease: stop our child, write nothing
        finally:
            done.set()
            with self._lock:
                self._active.pop(job.lease_id, None)

    def _renew(self, active: _Active, deadline: float, done: threading.Event) -> None:
        """Renew every ``renew_s`` until the run ends or its deadline passes (then let the lease expire)."""
        while not done.wait(self.renew_s):
            if time.monotonic() > deadline + self.renew_s:
                return  # a run past its deadline is hung: stop renewing, so the sweep recovers it
            try:
                self.mailbox.renew_lease(active.job.lease_id, self.lease_s)
            except LeaseLost:
                active.lost.set()
                active.stop()
                return

    def _record(self, active: _Active, pid: int, pgid: int) -> None:
        start = create_time(pid)
        if start is None:
            start = time.time()  # already gone: nothing will match it, so nothing is ever signalled
        self.mailbox.set_lease_phase(active.job.lease_id, "running", child_pid=pid, child_start=start, pgid=pgid,
                                     lease_s=self.lease_s)

    # -- Codex ------------------------------------------------------------------------

    def _run_codex(self, active: _Active, deadline: float) -> tuple[str, str]:
        from llm_kit.adapters.codex_command import build_codex_app_server_argv
        from llm_kit.relay import codex_appserver as C2
        from llm_kit.relay.codex_channel import fit_reply

        job = active.job
        argv = build_codex_app_server_argv(root=job.cwd, sandbox="workspace-write", network=False, hooks=False,
                                           argv_prefix=self._codex_prefix)
        app = C2.CodexAppServer(argv, cwd=job.cwd, env=child_env(job.lease_id), client_name="llm-kit-relay-resume")
        active.stop = lambda: app.close(grace_s=0.5)
        try:
            info = app.start()
            self._record(active, info.pid, info.pgid)
            policy = C2.ThreadPolicy(cwd=job.cwd, sandbox="workspace-write", network=False, exclude_tmp=True)
            try:
                app.resume_thread(job.session_id, policy)
            except C2.CodexConfigMismatch as exc:
                return "declined", f"config_mismatch: codex reported another policy than requested: {exc}"
            except C2.CodexThreadBusy:
                return "declined", "thread is open in another Codex process"
            except C2.CodexNoRollout:
                return "declined", "relay defect: an idle thread had no rollout (it never ran a turn)"
            try:
                handle = app.start_turn(job.session_id, job.prompt)
            except C2.TurnInProgress:
                return "declined", "thread is running a turn in another Codex process"
            result = handle.wait(max(0.1, deadline - time.monotonic()))
            if result.ok and result.final_message:
                return "answered", fit_reply(result.final_message, job.address)
            return "declined", f"codex turn {result.status}: {result.error or 'no final answer'}"
        except C2.CodexAppServerError as exc:
            return "declined", f"codex app-server failed: {type(exc).__name__}: {str(exc)[:500]}"
        finally:
            try:
                app.close(grace_s=1.0)
            except C2.CodexAppServerError:
                pass  # the outcome is already decided; close killed the group either way

    # -- Claude -----------------------------------------------------------------------

    def claude_argv(self, job: ResumeJob) -> list[str]:
        if job.claude_permission_mode != "default":
            raise ValueError(f"permission mode {job.claude_permission_mode!r} is not allowed for a resumed turn")
        if self._claude_argv is not None:
            head = list(self._claude_argv)
        else:
            found = shutil.which("claude")
            if found is None:
                raise RuntimeError("claude is not on PATH")
            head = [found]
        return head + ["-p", "--resume", job.session_id, "--output-format", "stream-json", "--verbose",
                       "--permission-mode", job.claude_permission_mode]

    def _run_claude(self, active: _Active, deadline: float) -> tuple[str, str]:
        from llm_kit.relay.codex_channel import fit_reply

        job = active.job
        proc = subprocess.Popen(
            self.claude_argv(job), cwd=job.cwd, env=child_env(job.lease_id),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
        pgid = proc.pid  # start_new_session: the child leads its own group

        def kill() -> None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # the group is already gone

        active.stop = kill
        verdict: dict[str, Any] = {}
        stderr: list[str] = []

        def read_out() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind, subtype = event.get("type"), event.get("subtype")
                if "init" not in verdict:
                    if kind == "system" and subtype == "init":
                        verdict["init"] = event
                        problem = self._init_problem(job, event)
                        if problem is not None:
                            verdict["mismatch"] = problem
                            kill()
                    continue  # nothing before this run's init counts (a stale result is never read)
                if kind == "result" and event.get("session_id") == job.session_id and "result" not in verdict:
                    verdict["result"] = event

        def read_err() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                if sum(map(len, stderr)) < _STDERR_TAIL:
                    stderr.append(line)

        readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
        for reader in readers:
            reader.start()
        try:
            self._record(active, proc.pid, pgid)
            assert proc.stdin is not None
            try:
                proc.stdin.write(job.prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # the child already exited; its exit status decides below
            try:
                code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                kill()
                proc.wait()
                return "declined", f"timeout: the resumed claude turn did not finish within {job.timeout_s:g} s"
        finally:
            kill()  # nothing of the group outlives the run
            for reader in readers:
                reader.join(timeout=5.0)
        if "mismatch" in verdict:
            return "declined", f"config_mismatch: {verdict['mismatch']}"
        if "init" not in verdict:
            return "declined", f"claude exited {code} without an init event: {''.join(stderr)[-500:].strip()}"
        result = verdict.get("result")
        if result is None:
            return "declined", f"claude exited {code} without a result event"
        if code != 0:
            return "declined", f"claude exited {code}: {str(result.get('result') or '')[:500]}"
        if result.get("is_error") is not False:
            return "declined", f"claude reported an error: {str(result.get('result') or result.get('subtype'))[:500]}"
        text = result.get("result")
        if not isinstance(text, str) or not text.strip():
            return "declined", "claude returned an empty result"
        return "answered", fit_reply(text, job.address)

    @staticmethod
    def _init_problem(job: ResumeJob, event: Mapping[str, Any]) -> Optional[str]:
        if event.get("session_id") != job.session_id:
            return f"session_id {event.get('session_id')!r} is not {job.session_id!r}"
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or os.path.realpath(cwd) != os.path.realpath(job.cwd):
            return f"cwd {cwd!r} is not {job.cwd!r}"
        if event.get("permissionMode") != job.claude_permission_mode:
            return f"permissionMode {event.get('permissionMode')!r} is not {job.claude_permission_mode!r}"
        return None

    # -- orphan recovery --------------------------------------------------------------

    def recover(self, now: Optional[float] = None) -> int:
        """Recover every orphaned lease (module docstring); returns how many claims were declined.

        Order per orphan: fence it first (:meth:`Mailbox.take_lease`, so a runner that outlived its relay can
        write nothing more), then stop its child, then decline. A child that does not stop keeps the fenced
        lease for a later sweep.
        """
        recovered = 0
        clock = time.time() if now is None else now
        for lease in self.mailbox.leases():
            lease_id = lease["lease_id"]
            reason, tag = self._taken.get(lease_id, (None, lease_id))
            if reason is None:
                reason = self._orphan_reason(lease, clock)
                if reason is None:
                    continue
                taken = new_lease_id()
                try:
                    self.mailbox.take_lease(lease_id, new_lease_id=taken, relay_instance=self.relay_instance)
                except LeaseLost:
                    continue  # its runner finished first: nothing to recover
                with self._lock:
                    active = self._active.get(lease_id)
                if active is not None:
                    active.lost.set()
                self._taken[taken] = (reason, tag)
                lease_id = taken
            if not self._stop_children(lease, tag):
                print(f"llm-kit relay: a resume child of lease {lease_id} did not stop; "
                      "its claim is kept until a later sweep", file=sys.stderr)
                continue
            try:
                self.mailbox.recover_lease(lease_id, reason)
            except LeaseLost:
                pass  # cannot happen after take_lease (only this relay holds the new id); nothing to write
            else:
                recovered += 1
            self._taken.pop(lease_id, None)
        if recovered:
            self._on_finish()
        return recovered

    def _orphan_reason(self, lease: Mapping[str, Any], clock: float) -> Optional[str]:
        """Why ``lease`` is an orphan, or None when a runner of this relay still owns it."""
        ours = lease["relay_instance"] == self.relay_instance
        owned = ours and self.owns(lease["lease_id"])
        expired = lease["expires_at"] <= clock
        child = lease["child_pid"]
        child_alive = same_process(child, lease["child_start"]) if child is not None else False
        if owned and not expired:
            return None
        if lease["phase"] == "claimed":
            return REASON_BEFORE_START
        if lease["phase"] == "running" and not child_alive:
            return REASON_LOST
        if not ours:
            return REASON_STOPPED
        if expired:
            return REASON_EXPIRED
        return None  # ours, unexpired, its runner just ending: its own finish_claim ends the claim

    def _stop_children(self, lease: Mapping[str, Any], tag: str) -> bool:
        """Stop the lease's recorded child (identity-checked) or, while spawning, every process tagged ``tag``
        (the lease id the child was started with, from before any :meth:`Mailbox.take_lease`)."""
        if lease["phase"] == "running" and lease["child_pid"] is not None:
            return stop_group(lease["child_pid"], lease["child_start"], lease["pgid"], grace_s=self._grace)
        if lease["phase"] == "spawning":
            return all(stop_group(pid, start, _pgid(pid), grace_s=self._grace)
                       for pid, start in tagged_processes(tag))
        return True


def _pgid(pid: int) -> int:
    """``pid``'s process group; ``pid`` itself when it is already gone (stop_group then signals nothing)."""
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid


__all__ = [
    "LEASE_ENV",
    "REASON_BEFORE_START",
    "REASON_EXPIRED",
    "REASON_LOST",
    "REASON_RELAY_STOP",
    "REASON_STOPPED",
    "ResumeJob",
    "ResumeRunner",
    "child_env",
    "new_lease_id",
    "new_relay_instance",
    "same_process",
    "stop_group",
    "tagged_processes",
]
