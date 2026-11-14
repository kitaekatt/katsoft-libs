"""Remote resume (amendment 3, unit R1): the relay runs ONE turn in an idle session for ``continue_session``.

This is the one place the relay starts a process. A ``continue_session`` call
that the service accepted has already stored its message and claimed it in
the same mailbox transaction (``claimed_by`` plus a ``resume_leases`` row with
phase ``claimed``, :meth:`llm_kit.relay.mailbox.Mailbox.send_message`). The
:class:`ResumeRunner` then, on a thread of its own:

1. sets the lease phase ``spawning`` and starts the child in a new session
   (its own process group) with ``LLM_KIT_RESUME_LEASE=<process tag>`` in its
   environment. The tag is persisted on the lease (``proc_tag``) and, unlike
   the lease id (the fencing token), never replaced, so any later relay finds
   the child and its descendants, which inherit the tag;
2. records the child's pid, create time and process group (phase
   ``running``); every lease write is fenced by the lease id;
3. renews the lease every ``renew_s`` until the run's deadline, and stops
   renewing after it, so a hung run lets the lease expire;
4. judges the run (below) and ends the claim with
   :meth:`~llm_kit.relay.mailbox.Mailbox.finish_claim`: the reply
   (``answered`` or ``declined``), the terminal state, the cleared claim and
   the deleted lease in one transaction. A runner whose lease was taken over
   (:class:`~llm_kit.relay.mailbox.LeaseLost`) stops its run's processes and
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
relay owns it, or when its run ended with termination unconfirmed (phase
``stopping``) and no runner owns it. The orphan is fenced first
(:meth:`Mailbox.take_lease`), then every process of its run is stopped
(:func:`stop_processes`). The tag never selects a process by itself: a process
is the run's only with this user's uid and membership proven through the
recorded child (:func:`_membership`). Each is signalled on its own
identity-bound psutil handle (SIGTERM, a grace period, SIGKILL for every
survivor, whether or not the leader already exited); no group is ever
signalled by number, so a reused pid or group is never touched. Termination
must be confirmed (no member left and no unconfirmed process) before the claim
is cleared; a run whose own final stop is not confirmed keeps its claim in
phase ``stopping`` and a sweep later writes its decided outcome through the fenced
:meth:`~llm_kit.relay.mailbox.Mailbox.finish_claim`; every other orphan is
declined with its reason. A surviving child is
never adopted: its stdout belonged to the dead relay, so its output cannot be
judged.
"""
from __future__ import annotations

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

from llm_kit.adapters.claude_stream import StreamVerdict, parse_stream_line
from llm_kit.relay.mailbox import LeaseLost, Mailbox

#: The environment variable that tags a resume child with its lease id.
LEASE_ENV = "LLM_KIT_RESUME_LEASE"
DEFAULT_LEASE_S = 90.0
DEFAULT_RENEW_S = 30.0
#: Seconds between SIGTERM and SIGKILL when a child group is stopped.
TERM_GRACE_S = 3.0
#: Seconds a stopped child may take to disappear before recovery waits for a later sweep.
GONE_WAIT_S = 5.0
_STDERR_TAIL = 2000

REASON_BEFORE_START = "relay restarted before the run started"
REASON_STOPPED = "relay restarted during the run; it was stopped"
REASON_LOST = "the run's result was lost; for Codex the turn may have run, read_session shows it"
REASON_EXPIRED = "the run stopped renewing its lease past its deadline; it was stopped"
REASON_RELAY_STOP = "the relay stopped during the run; it was stopped"
REASON_UNSTOPPED = "the run ended but termination of its processes was not confirmed until a later sweep; its outcome was lost"


def new_lease_id() -> str:
    return "lse_" + secrets.token_hex(12)


def new_proc_tag() -> str:
    """The immutable tag a run's processes carry (``resume_leases.proc_tag``); never replaced by a takeover."""
    return "rpt_" + secrets.token_hex(12)


def new_relay_instance() -> str:
    return "rin_" + secrets.token_hex(8)


def child_env(tag: str, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The child's environment: ``base`` (default: this process's) without ``CLAUDECODE`` and
    ``CLAUDE_CODE_*``, plus the run's immutable process tag."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env[LEASE_ENV] = tag
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
    """Whether ``pid`` is alive AND its create time is exactly ``start`` (the value psutil reported when the
    child was recorded; a reused pid, even one created a moment later, is a different process)."""
    if pid is None or start is None:
        return False
    actual = create_time(pid)
    return actual is not None and actual == start


def tagged_processes(tag: str) -> list[tuple[int, float]]:
    """``(pid, create time)`` of every process of this user whose environment carries the run's tag."""
    return [(proc.pid, start) for proc, start in _tagged(tag)]


def _tagged(tag: str) -> list[tuple[Any, float]]:
    psutil = _psutil()
    found: list[tuple[Any, float]] = []
    for proc in psutil.process_iter():
        try:
            if proc.environ().get(LEASE_ENV) == tag:
                found.append((proc, float(proc.create_time())))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except SystemError:
            # psutil 7.2 on macOS raises SystemError from proc_environ under load
            # (KERN_PROCARGS2 failing with errno 0): unreadable, as AccessDenied.
            continue
    return found


def _own_uid(proc: Any) -> bool:
    psutil = _psutil()
    try:
        return proc.uids().real == os.getuid()
    except psutil.Error:
        return False


def _live(proc: Any) -> bool:
    """Whether ``proc`` (a bound handle) still runs: not gone and not a zombie (a zombie runs nothing)."""
    psutil = _psutil()
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _membership(tag: str, leader: Optional[tuple[int, float, int]]) -> tuple[list[Any], list[Any]]:
    """``(members, unconfirmed)`` of one run, as psutil handles bound to their exact create times.

    The tag never selects a process by itself: an environment variable can be copied by any process of this
    user. A process is a member only with this user's uid AND independent evidence from the recorded
    ``leader`` = ``(pid, create time, pgid)``:

    * the leader's identity is confirmed (its pid has exactly the recorded create time, alive or an unreaped
      zombie): the leader while it runs, its descendants, and every member of its process group, tagged or not;
    * the leader is gone and its pid is free (``pgid == pid``; a pid is not reused while a process group of that
      id has members): the members of that group that also carry the tag.

    ``unconfirmed`` are the live processes of this user that may belong to the run but are not proven to:
    tagged processes outside the membership, and untagged members of a group whose leader is gone. They are
    never signalled; they only keep the run's claim held (:func:`stop_processes`). With no leader recorded (a
    crash before the child's pid was written) no process is a member.
    """
    psutil = _psutil()
    me = os.getpid()
    tagged = {proc.pid: proc for proc, _ in _tagged(tag) if proc.pid != me and _own_uid(proc) and _live(proc)}
    members: dict[int, Any] = {}
    loose: dict[int, Any] = {}
    if leader is not None:
        pid, start, pgid = leader
        lead = None
        try:
            candidate = psutil.Process(pid)
            if float(candidate.create_time()) == start and _own_uid(candidate):
                lead = candidate  # the recorded leader, alive or a zombie its parent has not reaped
        except psutil.Error:
            lead = None
        if lead is not None:
            if _live(lead):
                members[pid] = lead
            try:
                for child in lead.children(recursive=True):
                    if _own_uid(child) and _live(child):
                        members[child.pid] = child
            except psutil.Error:
                pass  # the leader ended meanwhile: its group is still enumerated below
        if lead is not None or (pgid == pid and not psutil.pid_exists(pid)):
            for proc in psutil.process_iter():
                try:
                    if proc.pid in members or proc.pid == me or os.getpgid(proc.pid) != pgid:
                        continue
                    proc.create_time()  # binds the handle to this process's identity
                    if not (_own_uid(proc) and _live(proc)):
                        continue
                    if lead is not None or proc.pid in tagged:
                        members[proc.pid] = proc
                    else:
                        loose[proc.pid] = proc
                except (ProcessLookupError, PermissionError, psutil.Error):
                    continue
    members.pop(me, None)  # never this relay
    unconfirmed = [proc for pid, proc in tagged.items() if pid not in members] + list(loose.values())
    return list(members.values()), unconfirmed


def run_processes(tag: str, leader: Optional[tuple[int, float, int]] = None) -> list[Any]:
    """Every live process proven to belong to one run (:func:`_membership`), as psutil handles."""
    return _membership(tag, leader)[0]


def _wait_exited(pid: int, timeout_s: float) -> bool:
    """Wait until this relay's child ``pid`` has exited WITHOUT reaping it (a zombie keeps its identity, so its
    process group stays provably the run's while the rest of the run is stopped); False on timeout."""
    psutil = _psutil()
    end = time.monotonic() + timeout_s
    delay = 0.001
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    while True:
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.05)


def stop_processes(tag: str, leader: Optional[tuple[int, float, int]] = None, *, grace_s: float = TERM_GRACE_S,
                   gone_wait_s: float = GONE_WAIT_S, rounds: int = 3) -> bool:
    """Terminate every member of one run (:func:`_membership`); True only once no member is left AND no
    process that may belong to the run is unconfirmed. False is loud (stderr) and means the claim stays held.

    Each member is signalled on its own psutil handle, which refuses to signal a pid whose create time
    changed, so no reused pid and no process group by number is ever signalled. SIGTERM, a grace period, then
    SIGKILL for every survivor, whether or not the leader already exited; then the run is enumerated again
    (a descendant may have started meanwhile), up to ``rounds`` times. An unconfirmed process is never
    signalled.
    """
    psutil = _psutil()
    procs: list[Any] = []
    for _ in range(rounds):
        procs = run_processes(tag, leader)
        if not procs:
            break
        for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, gone_wait_s)):
            for proc in procs:
                try:
                    proc.send_signal(sig)
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    pass  # already gone (or the pid now names another process, which psutil refuses)
            _, procs = psutil.wait_procs(procs, timeout=max(wait, 0.05))
            procs = [proc for proc in procs if _live(proc)]  # a killed child its parent has not reaped yet
            if not procs:
                break
        if procs:
            break
    members, unconfirmed = _membership(tag, leader)
    if members or unconfirmed:
        print(f"llm-kit relay: resume run {tag}: termination not confirmed; still running: "
              f"{sorted(p.pid for p in members)}, unconfirmed (never signalled): "
              f"{sorted(p.pid for p in unconfirmed)}", file=sys.stderr)
        return False
    return True


def _stop_confirmed(tag: str, leader: Optional[tuple[int, float, int]], *, grace_s: float) -> bool:
    """:func:`stop_processes`, with any exception (for example psutil.AccessDenied on a signal) logged loudly
    and counted as unconfirmed termination, so the caller keeps the claim held instead of releasing it."""
    try:
        return stop_processes(tag, leader, grace_s=grace_s)
    except Exception:  # noqa: BLE001 -- every cleanup failure keeps the claim; none is swallowed silently
        print(f"llm-kit relay: resume run {tag}: stopping its processes failed; termination is not confirmed",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False


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
    #: The immutable process tag of the lease (``resume_leases.proc_tag``); the lease id when not given.
    proc_tag: Optional[str] = None

    @property
    def tag(self) -> str:
        return self.proc_tag or self.lease_id


@dataclass
class _Active:
    job: ResumeJob
    thread: Optional[threading.Thread] = None
    stop: Callable[[], None] = lambda: None
    #: ``(pid, create time, pgid)`` of the recorded child, once recorded.
    leader: Optional[tuple[int, float, int]] = None
    stopped_by: Optional[str] = None
    lost: threading.Event = field(default_factory=threading.Event)
    #: Set when the run's final stop did not confirm termination: the claim is kept (phase ``stopping``).
    unstopped: bool = False


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
        #: Leases this relay fenced for recovery whose processes have not stopped yet: new lease id -> the
        #: outcome to write once they have (a decline with the orphan's reason, or the outcome a run decided
        #: before its processes failed to stop). The process tag is persisted (``resume_leases.proc_tag``).
        self._taken: dict[str, tuple[str, str]] = {}
        #: Runs that ended with termination unconfirmed (phase ``stopping``): lease id -> their decided outcome.
        self._pending: dict[str, tuple[str, str]] = {}
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
            if active.unstopped:
                # a process of the run may still act in the session: keep the claim held, never release it
                with self._lock:
                    self._pending[job.lease_id] = (state, body)
                self.mailbox.set_lease_phase(job.lease_id, "stopping")
                print(f"llm-kit relay: resume lease {job.lease_id} ({job.address}): its processes did not stop;"
                      " the claim stays held and every maintenance sweep retries the stop", file=sys.stderr)
                return
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
            start = -1.0  # already gone: no process has this create time, so nothing is ever signalled by it
        active.leader = (pid, start, pgid)
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
        app = C2.CodexAppServer(argv, cwd=job.cwd, env=child_env(job.tag), client_name="llm-kit-relay-resume")

        def stop() -> None:
            app.close(grace_s=0.5)
            _stop_confirmed(job.tag, active.leader, grace_s=0.0)

        active.stop = stop
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
                pass  # the outcome is already decided
            except Exception:  # noqa: BLE001 -- a cleanup defect: termination is not confirmed
                traceback.print_exc(file=sys.stderr)
                active.unstopped = True
            if not _stop_confirmed(job.tag, active.leader, grace_s=0.5):
                active.unstopped = True

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
            self.claude_argv(job), cwd=job.cwd, env=child_env(job.tag),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
        pgid = proc.pid  # start_new_session: the child leads its own group

        def kill() -> None:
            # every process of the run, each by its own identity-bound handle: never the group by number,
            # which after the leader is reaped could name a stranger's group
            _stop_confirmed(job.tag, active.leader, grace_s=0.0)

        active.stop = kill
        verdict = StreamVerdict(session_id=job.session_id, cwd=job.cwd, permission_mode=job.claude_permission_mode)
        stderr: list[str] = []

        def read_out() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                event = parse_stream_line(line)
                if event is None:
                    continue
                verdict.observe(event)
                # only the first init decides the run; a mismatch stops the child at once
                if event is verdict.first_init and verdict.first_problem is not None:
                    kill()

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
            if not _wait_exited(proc.pid, max(0.1, deadline - time.monotonic())):
                kill()
                return "declined", f"timeout: the resumed claude turn did not finish within {job.timeout_s:g} s"
        finally:
            # The leader is not reaped yet: as a zombie it keeps its pid and create time, so its whole group,
            # untagged members included, is still provably the run's. Nothing of the run outlives it.
            if not _stop_confirmed(job.tag, active.leader, grace_s=0.5):
                active.unstopped = True
            try:
                code = proc.wait(timeout=GONE_WAIT_S)
            except subprocess.TimeoutExpired:
                code = None  # the leader survived SIGKILL: unstopped is set and the claim stays held
            for reader in readers:
                reader.join(timeout=5.0)
        if verdict.first_problem is not None:
            return "declined", f"config_mismatch: {verdict.first_problem}"
        if verdict.first_init is None:
            return "declined", f"claude exited {code} without an init event: {''.join(stderr)[-500:].strip()}"
        # nothing before this run's init counts (a stale result is never read)
        matched = verdict.first_result_after_init
        if matched is None:
            return "declined", f"claude exited {code} without a result event"
        result = matched[1]
        if code != 0:
            return "declined", f"claude exited {code}: {str(result.get('result') or '')[:500]}"
        if result.get("is_error") is not False:
            return "declined", f"claude reported an error: {str(result.get('result') or result.get('subtype'))[:500]}"
        text = result.get("result")
        if not isinstance(text, str) or not text.strip():
            return "declined", "claude returned an empty result"
        return "answered", fit_reply(text, job.address)

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
            outcome = self._taken.get(lease_id)
            if outcome is None:
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
                    outcome = self._pending.pop(lease_id, None) or ("declined", reason)
                if active is not None:
                    active.lost.set()
                self._taken[taken] = outcome
                lease_id = taken
            if not self._stop_children(lease):
                print(f"llm-kit relay: resume lease {lease_id} ({lease['address']}): termination of its processes"
                      " is not confirmed; the claim stays held until a later sweep confirms it", file=sys.stderr)
                continue
            try:
                self.mailbox.finish_claim(lease_id, *outcome)
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
        if lease["phase"] == "stopping":
            return REASON_UNSTOPPED
        if lease["phase"] == "claimed":
            return REASON_BEFORE_START
        if lease["phase"] == "running" and not child_alive:
            return REASON_LOST
        if not ours:
            return REASON_STOPPED
        if expired:
            return REASON_EXPIRED
        return None  # ours, unexpired, its runner just ending: its own finish_claim ends the claim

    def _stop_children(self, lease: Mapping[str, Any]) -> bool:
        """Stop every process proven to belong to the lease's run (:func:`_membership`: the recorded child,
        its descendants and its group, with the persisted ``proc_tag`` required wherever the child's identity
        can no longer be confirmed); True once termination is confirmed."""
        leader = None
        if lease["child_pid"] is not None and lease["child_start"] is not None and lease["pgid"] is not None:
            leader = (lease["child_pid"], lease["child_start"], lease["pgid"])
        return _stop_confirmed(lease["proc_tag"], leader, grace_s=self._grace)


__all__ = [
    "LEASE_ENV",
    "REASON_BEFORE_START",
    "REASON_EXPIRED",
    "REASON_LOST",
    "REASON_RELAY_STOP",
    "REASON_STOPPED",
    "REASON_UNSTOPPED",
    "ResumeJob",
    "ResumeRunner",
    "child_env",
    "new_lease_id",
    "new_proc_tag",
    "new_relay_instance",
    "same_process",
    "run_processes",
    "stop_processes",
    "tagged_processes",
]
