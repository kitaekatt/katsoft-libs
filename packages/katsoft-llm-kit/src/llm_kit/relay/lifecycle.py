"""Relay lifecycle: state directory, singleton lock, tokens, pause flags, registry file.

Everything here lives under the state directory resolved by
:mod:`llm_kit.relay.config` (``$XDG_STATE_HOME/llm-kit/relay``). The directory
is created ``0700``; every file this module writes is ``0600``.

Security posture. Under alternative A (owner ruling 2026-11-03) the relay
token and the hub bearer are a GUARDRAIL against accidental calls, not
protection against a same-user model: any process of the owning user can read
these files and call the relay. The modes are checked so a world- or
group-readable credential is refused, not so the credential is secret from the
user's own processes.

Singleton. A role (``relay`` or ``hub``) runs at most once per state
directory. The holder owns an exclusive ``flock`` on ``<role>.pid`` for its
whole life and writes its pid there; the kernel releases the lock when the
process dies, so a stale pidfile never blocks a start. A second holder is
refused with the protocol error ``already_running``. POSIX only.

Pause. ``pause.json`` holds the persisted ``relay`` and ``hub`` pause flags
(protocol section 16): ``paused``, ``paused_since`` (epoch seconds, set when a
flag goes from clear to paused) and ``reason``. Every read goes to disk, so
the relay and the hub processes see each other's changes and a restart keeps
the flags.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from llm_kit.relay import protocol as P
from llm_kit.relay.config import (
    RelayConfig,
    RelayConfigError,
    ROLES,
    StatePaths,
    load_loopback_registry,
    render_registry_yaml,
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_TOKEN_BYTES = 32


# --------------------------------------------------------------------------
# state directory and private files
# --------------------------------------------------------------------------

def _refuse_loose(path: Path, *, kind: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RelayConfigError(f"{kind} {path} has mode {mode:04o}; it must not be accessible by group or others (chmod 0700/0600)")
    if path.stat().st_uid != os.getuid():
        raise RelayConfigError(f"{kind} {path} is not owned by the current user")


def ensure_state_dir(paths: StatePaths) -> Path:
    """Create the state directory ``0700`` (parents default) and refuse a loose or foreign one."""
    root = paths.root
    root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    _refuse_loose(root, kind="relay state directory")
    return root


def write_private_file(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` at mode ``0600``."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def ensure_token(path: Path) -> str:
    """The credential stored at ``path``, generating it ``0600`` when absent.

    Creation is atomic and race-safe (write a private temp file, then
    hard-link it into place; the loser of a race reads the winner's file). An
    existing file with group/other access, a foreign owner or empty content is
    refused rather than repaired.
    """
    if not path.exists():
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secrets.token_urlsafe(_TOKEN_BYTES) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, _FILE_MODE)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    return read_token(path)


def read_token(path: Path) -> str:
    """The stored credential, refusing a loose mode, a foreign owner or empty content."""
    _refuse_loose(path, kind="credential file")
    value = path.read_text(encoding="ascii").strip()
    if not value:
        raise RelayConfigError(f"credential file {path} is empty")
    return value


def token_matches(presented: Optional[str], expected: str) -> bool:
    """Constant-time comparison; ``None`` or empty never matches."""
    import hmac

    return bool(presented) and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------
# singleton per role
# --------------------------------------------------------------------------

class SingletonHeld(P.RelayError):
    """Another process already serves this role (``already_running``)."""

    def __init__(self, role: str, pidfile: Path, holder_pid: Optional[int]) -> None:
        super().__init__(
            "already_running",
            f"a {role} is already running (pidfile {pidfile}, pid {holder_pid if holder_pid else 'unknown'})",
            details={"role": role, "pid": holder_pid},
        )
        self.role = role
        self.holder_pid = holder_pid


class RoleLock:
    """The exclusive per-role lock: ``flock`` on ``<role>.pid`` plus the pid inside.

    Use as a context manager or call :meth:`acquire` / :meth:`release`. The
    lock is held for the life of the open file description, so two
    ``RoleLock`` objects for one role conflict even in one process, and a
    crashed holder frees it.
    """

    def __init__(self, paths: StatePaths, role: str) -> None:
        if role not in ROLES:
            raise RelayConfigError(f"unknown relay role {role!r}")
        self.role = role
        self.pidfile = paths.pidfile(role)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "RoleLock":
        if self._fd is not None:
            raise RelayConfigError(f"this process already holds the {self.role} lock")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - POSIX only
            raise RelayConfigError("the relay singleton lock needs fcntl (POSIX)") from None
        fd = os.open(self.pidfile, os.O_RDWR | os.O_CREAT, _FILE_MODE)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SingletonHeld(self.role, self.pidfile, _read_pid(fd)) from None
            os.fchmod(fd, _FILE_MODE)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        """Release the lock (only while held).

        The pidfile is emptied, not unlinked: a waiter that opened the old
        inode could otherwise lock a file no later starter sees.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        os.close(fd)  # closing the descriptor releases the flock

    def __enter__(self) -> "RoleLock":
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _read_pid(fd: int) -> Optional[int]:
    try:
        raw = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
        return int(raw) if raw.isdigit() else None
    except OSError:
        return None


# --------------------------------------------------------------------------
# pause flags
# --------------------------------------------------------------------------

def _clear_flag() -> P.PauseFlag:
    return {"paused": False, "paused_since": None, "reason": None}


class PauseStore:
    """The persisted relay and hub pause flags, re-read from disk on every call.

    ``clock`` returns epoch seconds (injectable for tests). Changes are
    serialized by an ``flock`` on ``pause.lock`` and written atomically, so
    two processes cannot lose each other's update. A corrupt file is an error,
    never read as "not paused".
    """

    def __init__(self, paths: StatePaths, *, clock: Callable[[], float] = time.time) -> None:
        self._file = paths.pause_file
        self._lock = paths.root / "pause.lock"
        self._clock = clock

    def _load(self) -> dict[str, P.PauseFlag]:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {scope: _clear_flag() for scope in P.PAUSE_SCOPES}
        except (OSError, ValueError) as exc:
            raise RelayConfigError(f"pause file {self._file} is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != set(P.PAUSE_SCOPES):
            raise RelayConfigError(f"pause file {self._file} must hold exactly the scopes {', '.join(P.PAUSE_SCOPES)}")
        flags: dict[str, P.PauseFlag] = {}
        for scope in P.PAUSE_SCOPES:
            flag = raw[scope]
            paused = flag.get("paused") if isinstance(flag, dict) else None
            since = flag.get("paused_since") if isinstance(flag, dict) else None
            reason = flag.get("reason") if isinstance(flag, dict) else None
            valid = (
                isinstance(paused, bool)
                and (since is None or (isinstance(since, (int, float)) and not isinstance(since, bool)))
                and (reason is None or isinstance(reason, str))
                and (paused == (since is not None))
            )
            if not valid:
                raise RelayConfigError(f"pause file {self._file} has an invalid flag for scope {scope!r}")
            flags[scope] = {"paused": paused, "paused_since": since, "reason": reason}
        return flags

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        fd = os.open(self._lock, os.O_RDWR | os.O_CREAT, _FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _require_scope(scope: str) -> str:
        if scope not in P.PAUSE_SCOPES:
            raise P.RelayError("bad_request", f"pause scope {scope!r} is not one of {', '.join(P.PAUSE_SCOPES)}")
        return scope

    def state(self) -> P.PauseState:
        """The ``PauseState`` object of protocol section 16."""
        flags = self._load()
        return {"relay": flags["relay"], "hub": flags["hub"]}

    def is_paused(self, scope: str) -> bool:
        return self._load()[self._require_scope(scope)]["paused"]

    def pause(self, scope: str, reason: Optional[str] = None) -> P.PauseState:
        """Pause ``scope``. ``paused_since`` is set on the clear-to-paused change and kept while paused."""
        self._require_scope(scope)
        with self._locked():
            flags = self._load()
            current = flags[scope]
            since = current["paused_since"] if current["paused"] else float(self._clock())
            flags[scope] = {"paused": True, "paused_since": since, "reason": reason if reason is not None else current["reason"]}
            self._write(flags)
        return self.state()

    def resume(self, scope: str) -> P.PauseState:
        """Clear ``scope``'s flag."""
        self._require_scope(scope)
        with self._locked():
            flags = self._load()
            flags[scope] = _clear_flag()
            self._write(flags)
        return self.state()

    def require_not_paused(self, scope: str) -> None:
        """Raise the protocol's ``paused`` error (with ``details.scope`` and ``paused_since``) when paused."""
        flag = self._load()[self._require_scope(scope)]
        if flag["paused"]:
            raise P.RelayError(
                "paused",
                f"the {scope} is paused" + (f": {flag['reason']}" if flag["reason"] else ""),
                details={"scope": scope, "paused_since": flag["paused_since"]},
            )

    def _write(self, flags: dict[str, P.PauseFlag]) -> None:
        write_private_file(self._file, json.dumps({scope: flags[scope] for scope in P.PAUSE_SCOPES}, indent=2) + "\n")


# --------------------------------------------------------------------------
# preparing a start
# --------------------------------------------------------------------------

class PreparedState:
    """What a role needs once its state is ready: credentials and the registry path."""

    def __init__(self, config: RelayConfig, relay_token: str, hub_bearer: str, registry_file: Path) -> None:
        self.config = config
        self.relay_token = relay_token
        self.hub_bearer = hub_bearer
        self.registry_file = registry_file

    def registry_environ(self) -> dict[str, str]:
        """The environment entry that points the registry reader at the relay's own file."""
        from llm_kit.registry.model_endpoints import REGISTRY_ENV

        return {REGISTRY_ENV: str(self.registry_file)}

    def pause_store(self, *, clock: Callable[[], float] = time.time) -> PauseStore:
        return PauseStore(self.config.paths, clock=clock)


def prepare_state(config: RelayConfig) -> PreparedState:
    """Create the state directory, both credentials and the loopback registry.

    Idempotent. The registry file is rewritten from ``config.llm_endpoints``
    (validated loopback) and then read back through the registry reader, so a
    file the reader would refuse never reaches a running role. Call before
    taking a :class:`RoleLock`; it does not take one.
    """
    ensure_state_dir(config.paths)
    relay_token = ensure_token(config.paths.relay_token)
    hub_bearer = ensure_token(config.paths.hub_bearer)
    write_private_file(config.paths.registry_file, render_registry_yaml(config.llm_endpoints))
    load_loopback_registry(config.paths.registry_file)
    return PreparedState(config, relay_token, hub_bearer, config.paths.registry_file)


__all__ = [
    "PauseStore",
    "PreparedState",
    "RoleLock",
    "SingletonHeld",
    "ensure_state_dir",
    "ensure_token",
    "prepare_state",
    "read_token",
    "token_matches",
    "write_private_file",
]
