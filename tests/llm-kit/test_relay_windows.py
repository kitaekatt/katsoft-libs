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
    """Records provision/validate/create calls with the file size at that moment (0 = no secret byte yet)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, bool, int]] = []
        self.refuse: dict[str, str] = {}  # operation -> message to refuse with
        self.fds: list[tuple[str, int, int]] = []
        self.after_validate_fd = None  # called after a handle was judged: the point a pathname could be re-pointed

    def _note(self, kind: str, path, directory: bool = False) -> None:
        if kind in self.refuse:
            raise C.RelayConfigError(self.refuse[kind])
        self.events.append((kind, str(path), directory, os.path.getsize(path) if os.path.isfile(path) else -1))

    def create_private_dir(self, path) -> None:
        os.mkdir(path)  # FileExistsError like the real one
        self._note("create_dir", path, True)

    def create_private_file(self, path) -> int:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)  # FileExistsError like the real one
        self._note("create_file", path)
        return fd

    def refuse_writable_ancestors(self, path) -> None:
        self._note("ancestors", path, True)

    def provision(self, path, directory: bool = False) -> None:
        self._note("provision", path, directory)

    def validate(self, path) -> None:
        self._note("validate", path)

    def provision_fd(self, fd: int, path) -> None:
        self.fds.append(("provision_fd", os.fstat(fd).st_ino, os.stat(path).st_ino))
        self._note("provision_fd", path)

    def validate_fd(self, fd: int, path) -> None:
        # which object was judged: the open handle's inode and the pathname's inode at this moment
        self.fds.append(("validate_fd", os.fstat(fd).st_ino, os.stat(path).st_ino))
        self._note("validate_fd", path)
        if self.after_validate_fd is not None:
            self.after_validate_fd(path)

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
    # the root is created with its descriptor (not mkdir-then-provision), after its ancestors were checked
    kinds = [k for k, p, *_ in nt.acl.events if p == str(paths.root)]
    assert kinds[:2] == ["ancestors", "create_dir"]
    assert (str(paths.root), True) in [(p, d) for p, d, _ in nt.acl.of("provision")]

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
    # born private (create_file), normalized and checked while still empty: no secret byte had been written
    assert tmp_events[:3] == [("create_file", 0), ("provision_fd", 0), ("validate_fd", 0)]
    assert str(paths.relay_token) in [p for k, p, *_ in nt.acl.events if k == "validate_fd"]  # read back by handle
    assert L.read_token(paths.relay_token) == token

    nt.acl.events.clear()
    L.write_private_file(paths.pause_file, "{}\n")
    kinds = [(k, Path(p).name.startswith(".pause.json."), size) for k, p, _d, size in nt.acl.events]
    assert kinds[:3] == [("create_file", True, 0), ("provision_fd", True, 0), ("validate_fd", True, 0)]
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
# the descriptor policy (pure): fails closed
# --------------------------------------------------------------------------

def ace(sid: str, *, kind: int = 0, flags: int = 0, mask: int = 0x1F01FF) -> winacl.Ace:
    return winacl.Ace(kind, flags, mask, sid)


def desc(*aces: winacl.Ace, owner: str = USER, protected: bool = True, present: bool = True) -> winacl.Descriptor:
    return winacl.Descriptor(owner, protected, present, list(aces))


GOOD = desc(ace(USER), ace("S-1-5-18"))


@pytest.mark.parametrize("d", [
    GOOD,
    desc(ace(USER)),
    desc(ace(USER), ace("S-1-1-0", kind=1)),  # a deny ACE grants nothing
])
def test_descriptor_policy_accepts_what_provision_writes(d):
    winacl.check_descriptor(d, USER, "p")


@pytest.mark.parametrize(
    "d, why",
    [
        (desc(ace(USER), owner="S-1-5-32-544"), "not owned"),
        (desc(ace(USER), owner=None), "not owned"),
        (desc(ace(USER), protected=False), "unprotected"),
        (desc(present=False), "null or missing"),
        (desc(ace(USER), ace("S-1-1-0")), "S-1-1-0"),
        (desc(ace(USER), ace("S-1-5-32-545")), "S-1-5-32-545"),
        (desc(ace(USER), ace("S-1-5-11")), "S-1-5-11"),
        (desc(ace(USER), ace("S-1-5-32-544")), "S-1-5-32-544"),
        (desc(ace("S-1-5-18")), "no access"),
        (desc(), "no access"),
    ],
)
def test_descriptor_policy_refuses_broad_or_foreign(d, why):
    with pytest.raises(C.RelayConfigError, match=why):
        winacl.check_descriptor(d, USER, "p")


@pytest.mark.parametrize("ace_type", [2, 3, 5, 6, 7, 8, 9, 10, 11, 17, 19, 21, 200])
def test_descriptor_policy_refuses_an_ace_type_it_does_not_understand(ace_type):
    """A conditional allow (type 9, SDDL XA) for Users once slipped past a text parser as 'not an ACE';
    here every non-allow/deny type is refused, never skipped, so a grant cannot hide in a shape we do not read."""
    hidden = winacl.Ace(ace_type, 0, 0, None)
    with pytest.raises(C.RelayConfigError, match=f"type {ace_type} .*does not understand"):
        winacl.check_descriptor(desc(ace(USER), ace("S-1-5-18"), hidden), USER, "p")


def test_descriptor_policy_refuses_a_conditional_grant_even_with_the_rest_correct():
    conditional = winacl.Ace(9, 0, 0x1F01FF, None)  # ACCESS_ALLOWED_CALLBACK_ACE: (XA;;FA;;;WD;(Member_of {...}))
    with pytest.raises(C.RelayConfigError, match="does not understand"):
        winacl.check_descriptor(desc(ace(USER), ace("S-1-5-18"), conditional), USER, "p")


# --------------------------------------------------------------------------
# ancestors
# --------------------------------------------------------------------------

HOME_ACL = desc(ace(USER), ace("S-1-5-18"), ace("S-1-5-32-544"))
TI = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
USERS = "S-1-5-32-545"
AUTH = "S-1-5-11"
ADD_FILE_SUBDIR = 0x2 | 0x4
MODIFY = 0x1301BF  # FILE_GENERIC_READ|WRITE|EXECUTE + DELETE: what Authenticated Users hold on a Modify ACE


def test_ancestor_check_accepts_a_stock_profile_chain():
    for strict in (True, False):
        winacl.check_ancestor(HOME_ACL, USER, "home", strict=strict)
        winacl.check_ancestor(desc(ace(USER), ace(TI), owner=TI), USER, "x", strict=strict)
        winacl.check_ancestor(desc(ace(USER), ace("S-1-1-0", mask=0x1200A9), owner="S-1-5-18"), USER, "x", strict=strict)
        winacl.check_ancestor(desc(ace(USER), ace(USERS, flags=0x0B, mask=0x1F01FF)), USER, "x", strict=strict)  # inherit-only


@pytest.mark.parametrize("mask", [0x2, 0x4, 0x40, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000, 0x1F01FF, MODIFY])
def test_strict_ancestor_refuses_another_trustee_that_can_write(mask):
    with pytest.raises(C.RelayConfigError, match="S-1-5-32-545 write to it"):
        winacl.check_ancestor(desc(ace(USER), ace(USERS, mask=mask)), USER, "parent", strict=True)


def test_lenient_ancestor_allows_adding_entries_but_not_replacing_one():
    """Above the state root's own container, Authenticated Users may create entries (as on C:\\) -- that cannot
    move the existing directory the path runs through -- but may not delete it, delete its children or re-ACL it."""
    winacl.check_ancestor(desc(ace(USER), ace(AUTH, mask=ADD_FILE_SUBDIR)), USER, "C:/", strict=False)
    winacl.check_ancestor(desc(ace(USER), ace(AUTH, mask=0x1200A9 | 0x4)), USER, "C:/", strict=False)
    for mask in (0x40, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000, 0x1F01FF, MODIFY):
        with pytest.raises(C.RelayConfigError, match="S-1-5-11 write to it"):
            winacl.check_ancestor(desc(ace(USER), ace(AUTH, mask=mask)), USER, "high", strict=False)
    with pytest.raises(C.RelayConfigError, match="S-1-5-11 write to it"):  # the same ACE, in the strict zone
        winacl.check_ancestor(desc(ace(USER), ace(AUTH, mask=ADD_FILE_SUBDIR)), USER, "near", strict=True)


@pytest.mark.parametrize("strict", [True, False])
def test_ancestor_check_refuses_an_untrusted_owner_even_with_a_clean_dacl(strict):
    """The owner holds WRITE_DAC implicitly, so a DACL granting only the user and SYSTEM is no protection."""
    clean = desc(ace(USER), ace("S-1-5-18"), owner="S-1-5-21-9-9-9-1002")
    with pytest.raises(C.RelayConfigError, match="owned by S-1-5-21-9-9-9-1002"):
        winacl.check_ancestor(clean, USER, "parent", strict=strict)
    with pytest.raises(C.RelayConfigError, match="owned by None"):
        winacl.check_ancestor(desc(ace(USER), owner=None), USER, "parent", strict=strict)
    for owner in (USER, "S-1-5-18", "S-1-5-32-544", TI):
        winacl.check_ancestor(desc(ace(USER), owner=owner), USER, "parent", strict=strict)


def test_ancestor_check_trusts_owner_rights_only_for_a_trusted_owner():
    """Python 3.13 gives os.mkdir(mode=0o700) directories an OWNER RIGHTS (S-1-3-4) full-control ACE, as pytest's tmp dirs have."""
    owner_rights = ace("S-1-3-4")
    winacl.check_ancestor(desc(ace(USER), owner_rights, ace("S-1-5-18")), USER, "pytest-tmp")
    with pytest.raises(C.RelayConfigError, match="owned by"):
        winacl.check_ancestor(desc(ace(USER), owner_rights, owner="S-1-5-21-9-9-9-1002"), USER, "foreign-owned")


def test_ancestor_check_fails_closed_on_null_dacl_and_unknown_ace():
    for strict in (True, False):
        with pytest.raises(C.RelayConfigError, match="null DACL"):
            winacl.check_ancestor(desc(present=False), USER, "parent", strict=strict)
        with pytest.raises(C.RelayConfigError, match="does not understand"):
            winacl.check_ancestor(desc(ace(USER), winacl.Ace(9, 0, 0x2, None)), USER, "parent", strict=strict)


def test_ancestor_chain_walks_to_the_root_and_splits_at_the_profile(tmp_path):
    home = tmp_path / "home"
    inside = home / "AppData" / "state" / "relay"
    strict, lenient = winacl.ancestor_chain(inside, home)
    assert strict == [home / "AppData" / "state", home / "AppData", home]
    assert lenient[0] == tmp_path and lenient[-1].parent == lenient[-1]  # on to the volume root
    outside = tmp_path / "elsewhere" / "deep" / "relay"
    strict, lenient = winacl.ancestor_chain(outside, home)
    assert strict == [tmp_path / "elsewhere" / "deep"]  # no profile: only the immediate container is strict
    assert lenient[0] == tmp_path / "elsewhere" and lenient[-1].parent == lenient[-1] and home not in strict + lenient


def test_nt_state_dir_is_refused_when_an_ancestor_is_writable(tmp_path, nt):
    nt.acl.refuse["ancestors"] = "ancestor parent lets S-1-5-32-545 write to it"
    root = tmp_path / "state"
    with pytest.raises(C.RelayConfigError, match="lets S-1-5-32-545"):
        L.ensure_state_dir(C.StatePaths(root))
    assert not root.exists()  # nothing was created under a parent another user controls


def test_nt_private_dirs_are_created_with_their_descriptor_not_mkdir_then_provision(tmp_path, nt):
    paths = C.StatePaths(tmp_path / "state")
    L.ensure_state_dir(paths)
    nt.acl.events.clear()
    L.ensure_private_dir(paths.peers_dir, kind="peers directory")
    kinds = [k for k, p, *_ in nt.acl.events if p == str(paths.peers_dir)]
    assert kinds[:2] == ["ancestors", "create_dir"]  # ancestors checked, then born private
    assert kinds[-1] == "validate"
    nt.acl.events.clear()
    L.ensure_private_dir(paths.peers_dir, kind="peers directory")  # existing: validated, nothing created or re-provisioned
    assert [k for k, *_ in nt.acl.events if k in ("create_dir", "provision")] == []
    assert "validate" in [k for k, *_ in nt.acl.events]
    # an ancestor another user can write refuses before anything is created
    nt.acl.refuse["ancestors"] = "ancestor parent lets S-1-5-32-545 write to it"
    with pytest.raises(C.RelayConfigError, match="lets S-1-5-32-545"):
        L.ensure_private_dir(paths.peer_dir("m5pro"), kind="peer directory")
    assert not paths.peer_dir("m5pro").exists()


# --------------------------------------------------------------------------
# validation and reads use the handle, not the name
# --------------------------------------------------------------------------

def _swap_in_other_content(path: str) -> None:
    other = Path(path).with_name("attacker.tmp")
    other.write_text("attacker-value\n")
    os.replace(other, path)


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to replace a file that is open: the protection itself")
def test_nt_credential_read_judges_and_reads_the_same_handle(tmp_path, nt):
    paths = C.StatePaths(tmp_path)
    L.ensure_state_dir(paths)
    token = L.ensure_token(paths.relay_token)
    nt.acl.fds.clear()
    # the name is re-pointed at another file right after the handle was judged
    nt.acl.after_validate_fd = _swap_in_other_content
    assert L.read_token(paths.relay_token) == token  # the validated file's content, not the swapped-in file's
    (kind, handle_ino, path_ino), = nt.acl.fds
    assert kind == "validate_fd" and handle_ino == path_ino  # at judging time the handle was the named file
    assert paths.relay_token.read_text().strip() == "attacker-value"  # and the swap really happened


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to replace a file that is open: the protection itself")
def test_nt_private_file_read_judges_and_reads_the_same_handle(tmp_path, nt):
    paths = C.StatePaths(tmp_path)
    L.ensure_state_dir(paths)
    L.write_private_file(paths.registry_file, "kept: yes\n")
    nt.acl.after_validate_fd = _swap_in_other_content
    assert L.read_private_file(paths.registry_file) == "kept: yes\n"
    nt.acl.refuse["validate_fd"] = "grants access to S-1-5-32-545"
    with pytest.raises(C.RelayConfigError, match="private state file .*grants access"):
        L.read_private_file(paths.registry_file)
    with pytest.raises(C.RelayConfigError, match="credential file .*grants access"):
        L.read_token(paths.relay_token if paths.relay_token.exists() else paths.registry_file)


def test_nt_temp_file_is_provisioned_and_judged_through_its_creating_handle(tmp_path, nt):
    paths = C.StatePaths(tmp_path)
    L.ensure_state_dir(paths)
    nt.acl.fds.clear()
    L.write_private_file(paths.pause_file, "{}\n")
    kinds = [k for k, *_ in nt.acl.fds]
    assert kinds == ["provision_fd", "validate_fd"]
    assert all(handle == named for _k, handle, named in nt.acl.fds)  # the handle is the file that was created
    assert "provision" not in [k for k, p, *_ in nt.acl.events if Path(p).name.startswith(".pause.json.")]


# --------------------------------------------------------------------------
# pause.json is validated before it is read
# --------------------------------------------------------------------------

def test_nt_existing_pause_file_is_validated_against_the_open_handle_before_it_is_read(tmp_path, nt):
    paths = C.StatePaths(tmp_path)
    L.ensure_state_dir(paths)
    store = L.PauseStore(paths, clock=lambda: 5.0)
    assert store.is_paused("relay") is False  # absent file: no validation, no error
    assert [p for p, *_ in nt.acl.of("validate_fd") if p == str(paths.pause_file)] == []
    store.pause("relay", "why")
    nt.acl.events.clear()
    assert store.is_paused("relay") is True
    assert [p for p, *_ in nt.acl.of("validate_fd")] == [str(paths.pause_file)]  # judged the open file

    # a pause file another user can write is refused, and its contents are never used
    nt.acl.refuse["validate_fd"] = f"{paths.pause_file} grants access to S-1-5-32-545"
    with pytest.raises(C.RelayConfigError, match="pause file .* grants access to S-1-5-32-545"):
        store.is_paused("relay")
    with pytest.raises(C.RelayConfigError, match="grants access"):
        store.require_not_paused("relay")
    with pytest.raises(C.RelayConfigError, match="grants access"):
        store.pause("hub", "x")  # a writer reads before it rewrites: it refuses too
    assert nt.msvcrt.held == set()


@pytest.mark.skipif(os.name == "nt", reason="the POSIX path")
def test_posix_pause_read_does_not_call_winacl(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "winacl", types.SimpleNamespace())  # any attribute access would raise
    store = L.PauseStore(C.StatePaths(tmp_path))
    store.pause("relay", "why")
    assert store.is_paused("relay") is True


def test_winacl_refuses_to_run_off_windows(tmp_path):
    if os.name == "nt":
        pytest.skip("the real Win32 calls are exercised natively")
    with pytest.raises(C.RelayConfigError, match="only available on Windows"):
        winacl.validate(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="the POSIX client starts normally")
def test_the_app_server_client_refuses_to_start_on_windows():
    from llm_kit.relay.codex_appserver import CodexAppServer, CodexAppServerError

    with pytest.raises(CodexAppServerError, match="supports POSIX only"):
        CodexAppServer([sys.executable], cwd=os.getcwd()).start()
