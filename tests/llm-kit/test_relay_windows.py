"""Portable tests for the Windows support of the relay lifecycle (unit A1).

Run on every platform. Windows behaviour is exercised through fakes: a fake
``msvcrt`` (a lock table keyed by file and offset), a fake ``winacl`` that
records what it was asked to do and when, and a fake socket. The real
Win32 calls are measured by ``test_relay_windows_native.py`` on a Windows host.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
import types
from pathlib import Path

import pytest

from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import lifecycle as L
from llm_kit.relay import server as S
from llm_kit.relay import winacl

USER = "S-1-5-21-1111111111-2222222222-3333333333-1001"


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------

class FakeMsvcrt(types.ModuleType):
    """``msvcrt.locking`` over a lock table: one byte at one offset of one file, held until unlocked."""

    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self) -> None:
        super().__init__("msvcrt")
        self.calls: list[tuple[str, int, int]] = []
        self.held: set[tuple[int, int, int]] = set()  # (inode, offset, nbytes)
        self.fail_with: int = errno.EACCES

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append(("unlock" if mode == self.LK_UNLCK else "lock", offset, nbytes))
        key = (os.fstat(fd).st_ino, offset, nbytes)
        if mode == self.LK_UNLCK:
            self.held.discard(key)
            return
        assert mode == self.LK_NBLCK, f"the relay may only take non-blocking locks, got mode {mode}"
        if key in self.held:
            raise OSError(self.fail_with, "locked")
        self.held.add(key)


class FakeWinacl:
    """Records provision/validate calls with the file size at that moment (0 = no secret byte yet)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, bool, int]] = []

    def provision(self, path, directory: bool = False) -> None:
        self.events.append(("provision", str(path), directory, os.path.getsize(path) if os.path.isfile(path) else -1))

    def validate(self, path) -> None:
        self.events.append(("validate", str(path), False, os.path.getsize(path) if os.path.isfile(path) else -1))

    def of(self, kind: str) -> list[tuple[str, bool, int]]:
        return [(p, d, s) for k, p, d, s in self.events if k == kind]


@pytest.fixture
def nt(monkeypatch):
    """Windows dispatch on any host: nt flag, fake msvcrt, fake winacl, no fcntl, no fchmod."""
    msvcrt = FakeMsvcrt()
    acl = FakeWinacl()
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` raises ImportError on the nt path
    monkeypatch.setattr(L, "_on_nt", lambda: True)
    monkeypatch.setattr(L, "winacl", acl)

    def no_fchmod(*_a, **_k):
        raise AssertionError("os.fchmod must not be called on nt")

    monkeypatch.setattr(os, "fchmod", no_fchmod, raising=False)
    return types.SimpleNamespace(msvcrt=msvcrt, acl=acl)


# --------------------------------------------------------------------------
# portable pid read
# --------------------------------------------------------------------------

def test_read_pid_without_pread(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "pread", raising=False)  # Windows has no os.pread
    path = tmp_path / "relay.pid"
    path.write_bytes(b"4242\n")
    fd = os.open(path, os.O_RDWR)
    try:
        os.lseek(fd, 0, os.SEEK_END)  # the descriptor position must not matter
        assert L._read_pid(fd) == 4242
    finally:
        os.close(fd)


def test_second_role_lock_reports_the_holder_pid_without_pread(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "pread", raising=False)
    paths = C.StatePaths(tmp_path)
    holder = L.RoleLock(paths, "relay").acquire()
    try:
        with pytest.raises(L.SingletonHeld) as caught:
            L.RoleLock(paths, "relay").acquire()
        assert caught.value.holder_pid == os.getpid() and caught.value.code == "already_running"
    finally:
        holder.release()


# --------------------------------------------------------------------------
# exclusive bind on nt
# --------------------------------------------------------------------------

class FakeSocket:
    instances: list["FakeSocket"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.options: list[tuple[int, int, int]] = []
        self.bound = None
        self.listening = False
        FakeSocket.instances.append(self)

    def setsockopt(self, level, option, value) -> None:
        self.options.append((level, option, value))

    def bind(self, address) -> None:
        self.bound = address

    def getsockname(self):
        return ("127.0.0.1", 4321)

    def listen(self, _backlog) -> None:
        self.listening = True

    def close(self) -> None:
        pass


def _bind_with_fake_socket(monkeypatch, *, nt_flag: bool) -> FakeSocket:
    FakeSocket.instances = []
    monkeypatch.setattr(S.socket, "socket", FakeSocket)
    monkeypatch.setattr(S.socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)  # absent off Windows
    monkeypatch.setattr(S, "_on_nt", lambda: nt_flag)
    S.bind_loopback("127.0.0.1", 0)
    (sock,) = FakeSocket.instances
    return sock


def test_bind_exclusive_on_nt(monkeypatch):
    sock = _bind_with_fake_socket(monkeypatch, nt_flag=True)
    assert sock.options == [(socket.SOL_SOCKET, -5, 1)]  # SO_EXCLUSIVEADDRUSE only; REUSEADDR would allow a second bind
    assert sock.bound == ("127.0.0.1", 0) and sock.listening


def test_bind_keeps_reuseaddr_off_nt(monkeypatch):
    sock = _bind_with_fake_socket(monkeypatch, nt_flag=False)
    assert sock.options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


# --------------------------------------------------------------------------
# cwd selector with a Windows cwd
# --------------------------------------------------------------------------

def _discovery_with_cwds(monkeypatch, cwds: list[str]) -> D.Discovery:
    sessions = tuple(
        D.SessionInfo("claude", f"s{i}", "live-accepting", True, ("note",), cwd=cwd, last_activity_at=float(i))
        for i, cwd in enumerate(cwds)
    )
    monkeypatch.setattr(D.Discovery, "scan", lambda self: D.ScanResult(sessions, ()))
    return D.Discovery("winbox", D.DiscoveryRoots(Path("/nonexistent-claude"), Path("/nonexistent-codex")))


def test_cwd_selector_windows_path(monkeypatch):
    discovery = _discovery_with_cwds(monkeypatch, ["C:\\Users\\u\\Dev\\app", "D:\\work\\other", "/Users/u/Dev/posix-app"])
    assert discovery.resolve("cwd:app").session_id == "s0"                  # final component of a drive-letter cwd
    assert discovery.resolve("cwd:APP").session_id == "s0"                  # Windows paths compare case-insensitively
    assert discovery.resolve("cwd:/C:/Users/u/Dev/app").session_id == "s0"  # the absolute form of the same cwd
    assert discovery.resolve("cwd:other").session_id == "s1"
    assert discovery.resolve("cwd:posix-app").session_id == "s2"             # POSIX cwds keep the POSIX rule
    assert discovery.resolve("cwd:/Users/u/Dev/posix-app").session_id == "s2"
    with pytest.raises(Exception) as caught:
        discovery.resolve("cwd:missing")
    assert getattr(caught.value, "code", None) == "target_not_found"


# --------------------------------------------------------------------------
# nt dispatch: winacl and msvcrt
# --------------------------------------------------------------------------

def test_nt_dispatch_uses_winacl_and_msvcrt(tmp_path, nt):
    paths = C.StatePaths(tmp_path / "state")
    L.ensure_state_dir(paths)
    assert (str(paths.root), True) in [(p, d) for p, d, _ in nt.acl.of("provision")]  # the root is provisioned as a directory

    first = L.RoleLock(paths, "relay").acquire()
    try:
        # the lock is one byte far past the pid text, taken without blocking
        assert nt.msvcrt.calls == [("lock", L.LOCK_OFFSET, 1)]
        assert paths.pidfile("relay").read_text().strip() == str(os.getpid())  # pid at offset 0 stays readable
        assert str(paths.pidfile("relay")) in [p for p, *_ in nt.acl.of("validate")]
        # a second acquire is refused and names the holder, read from offset 0
        with pytest.raises(L.SingletonHeld) as caught:
            L.RoleLock(paths, "relay").acquire()
        assert caught.value.holder_pid == os.getpid()
        # a different role has its own lock
        L.RoleLock(paths, "hub").acquire().release()
    finally:
        first.release()
    assert nt.msvcrt.held == set()  # release unlocks before the handle closes
    L.RoleLock(paths, "relay").acquire().release()


def test_nt_lock_error_other_than_contention_is_not_already_running(tmp_path, nt):
    nt.msvcrt.fail_with = errno.EBADF
    paths = C.StatePaths(tmp_path)
    holder = L.RoleLock(paths, "relay").acquire()
    try:
        with pytest.raises(OSError) as caught:
            L.RoleLock(paths, "relay").acquire()
        assert not isinstance(caught.value, L.SingletonHeld) and caught.value.errno == errno.EBADF
    finally:
        holder.release()


def test_nt_temp_files_are_private_before_the_first_secret_byte(tmp_path, nt):
    paths = C.StatePaths(tmp_path)
    L.ensure_state_dir(paths)
    nt.acl.events.clear()

    token = L.ensure_token(paths.relay_token)
    tmp_events = [(k, size) for k, p, _d, size in nt.acl.events if Path(p).name.startswith(".relay.token.")]
    assert tmp_events[:2] == [("provision", 0), ("validate", 0)]  # empty file: no secret byte yet
    assert ("validate", len(token) + 1) not in tmp_events[:2]
    assert nt.acl.events[-1][:2] == ("validate", str(paths.relay_token)) or str(paths.relay_token) in [
        p for k, p, *_ in nt.acl.events if k == "validate"
    ]
    assert L.read_token(paths.relay_token) == token

    nt.acl.events.clear()
    L.write_private_file(paths.pause_file, "{}\n")
    kinds = [(k, Path(p).name.startswith(".pause.json."), size) for k, p, _d, size in nt.acl.events]
    assert kinds[0] == ("provision", True, 0) and kinds[1] == ("validate", True, 0)
    assert kinds[-1][:2] == ("validate", False) and kinds[-1][2] > 0  # the replacement target is validated at its final path, written


def test_nt_pause_store_serializes_on_msvcrt_and_times_out_loudly(tmp_path, nt, monkeypatch):
    paths = C.StatePaths(tmp_path)
    L.ensure_state_dir(paths)
    store = L.PauseStore(paths, clock=lambda: 100.0)
    store.pause("relay", "maintenance")
    assert store.state()["relay"] == {"paused": True, "paused_since": 100.0, "reason": "maintenance"}
    assert nt.msvcrt.held == set() and ("lock", L.LOCK_OFFSET, 1) in nt.msvcrt.calls

    # a reader takes the same lock: Windows refuses a replace over an open file and a read during one
    before = len(nt.msvcrt.calls)
    assert store.is_paused("relay") is True
    assert [c[0] for c in nt.msvcrt.calls[before:]] == ["lock", "unlock"] and nt.msvcrt.held == set()

    # a holder that never lets go: the poll gives up with a loud config error
    holder = os.open(paths.root / "pause.lock", os.O_RDWR)
    os.lseek(holder, L.LOCK_OFFSET, os.SEEK_SET)
    nt.msvcrt.locking(holder, nt.msvcrt.LK_NBLCK, 1)
    monkeypatch.setattr(L, "_PAUSE_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(L, "_PAUSE_LOCK_POLL_SECONDS", 0.01)
    try:
        with pytest.raises(C.RelayConfigError, match="pause lock .* was not free"):
            store.resume("relay")
    finally:
        os.close(holder)


# --------------------------------------------------------------------------
# the SDDL policy (pure)
# --------------------------------------------------------------------------

GOOD_DIR = f"O:{USER}D:P(A;OICI;FA;;;{USER})(A;OICI;FA;;;SY)"
GOOD_FILE = f"O:{USER}D:P(A;;FA;;;{USER})(A;;FA;;;SY)"


@pytest.mark.parametrize("sddl", [GOOD_DIR, GOOD_FILE, f"O:{USER}G:{USER}D:P(A;;FA;;;{USER})", f"O:{USER}D:P(A;;FA;;;{USER})(D;;FA;;;WD)", f"O:{USER}D:PAI(A;;FA;;;{USER})"])
def test_sddl_policy_accepts_what_provision_writes(sddl):
    winacl.check_sddl(sddl, USER, "p")


@pytest.mark.parametrize(
    "sddl, why",
    [
        (f"O:BAD:P(A;;FA;;;{USER})", "not owned"),
        (f"O:{USER}D:AI(A;ID;FA;;;{USER})(A;ID;FA;;;SY)", "unprotected"),
        (f"O:{USER}D:AR(A;ID;FA;;;{USER})", "unprotected"),
        (f"O:{USER}D:(A;;FA;;;{USER})", "unprotected"),
        (f"O:{USER}", "null or missing"),
        (f"O:{USER}D:NO_ACCESS_CONTROL", "null or missing"),
        (f"O:{USER}D:P(A;;FA;;;{USER})(A;;FR;;;WD)", "WD"),
        (f"O:{USER}D:P(A;;FA;;;{USER})(A;;FR;;;BU)", "BU"),
        (f"O:{USER}D:P(A;;FA;;;{USER})(A;;FR;;;AU)", "AU"),
        (f"O:{USER}D:P(A;;FA;;;{USER})(A;;FA;;;BA)", "BA"),
        (f"O:{USER}D:P(A;;FA;;;{USER})(A;;FA;;;S-1-5-21-9-9-9-1002)", "S-1-5-21-9-9-9-1002"),
        (f"O:{USER}D:P(A;;FA;;;SY)", "no access"),
        ("garbage", "cannot read"),
    ],
)
def test_sddl_policy_refuses_broad_or_foreign(sddl, why):
    with pytest.raises(C.RelayConfigError, match=why):
        winacl.check_sddl(sddl, USER, "p")


def test_winacl_refuses_to_run_off_windows(tmp_path):
    if os.name == "nt":
        pytest.skip("the real Win32 calls are exercised natively")
    with pytest.raises(C.RelayConfigError, match="only available on Windows"):
        winacl.validate(tmp_path)
