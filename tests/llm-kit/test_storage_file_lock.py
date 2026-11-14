"""The shared cross-process file lock (llm_kit.storage.file_lock), moved out of the job supervisor.

The POSIX tests use real ``flock``; the Windows tests run the ``msvcrt`` path on any host
through a fake ``msvcrt`` that holds a lock table, so the blocking and nonblocking modes and
the one-byte seed write are pinned without Windows.
"""
from __future__ import annotations

import errno
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from llm_kit.jobs import _supervisor_worker as worker
from llm_kit.jobs import supervisor
from llm_kit.storage import file_lock as fl
from llm_kit.storage.file_lock import file_lock

def acquire_in_thread(path: Path, *, blocking: bool, hold: threading.Event, held: threading.Event,
                      errors: list) -> threading.Thread:
    def run() -> None:
        try:
            with file_lock(path, blocking=blocking):
                held.set()
                hold.wait(10)
        except BaseException as exc:  # reported to the test
            errors.append(exc)
            held.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def try_lock(path: Path, *, blocking: bool, wait_s: float = 5.0):
    """Enter the lock from another thread: the exception it raised, "entered", or "hung" if it did not return."""
    outcome: list = []

    def run() -> None:
        try:
            with file_lock(path, blocking=blocking):
                outcome.append("entered")
        except BaseException as exc:  # reported to the test
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(wait_s)
    return outcome[0] if outcome else "hung"


def test_a_held_lock_refuses_a_nonblocking_second_holder_and_frees_on_exit(tmp_path):
    path = tmp_path / "a" / "b" / "x.lock"  # parents are created
    hold, held, errors = threading.Event(), threading.Event(), []
    thread = acquire_in_thread(path, blocking=True, hold=hold, held=held, errors=errors)
    assert held.wait(10) and not errors
    attempt = acquire_in_thread(path, blocking=False, hold=threading.Event(), held=threading.Event(), errors=errors)
    attempt.join(5)
    assert not attempt.is_alive(), "a nonblocking acquire must not wait"
    assert len(errors) == 1 and isinstance(errors[0], BlockingIOError)
    hold.set()
    thread.join(5)
    with file_lock(path, blocking=False):
        pass  # free again
    assert path.exists()


def test_a_blocking_acquire_waits_for_the_holder(tmp_path):
    path = tmp_path / "x.lock"
    hold, held, errors = threading.Event(), threading.Event(), []
    thread = acquire_in_thread(path, blocking=True, hold=hold, held=held, errors=errors)
    assert held.wait(10)
    entered = threading.Event()

    def waiter() -> None:
        with file_lock(path):
            entered.set()

    second = threading.Thread(target=waiter, daemon=True)
    second.start()
    assert not entered.wait(0.3), "the second holder must wait while the first holds the lock"
    hold.set()
    assert entered.wait(10)
    thread.join(5)
    second.join(5)
    assert not errors


def test_the_lock_is_released_when_the_block_raises(tmp_path):
    path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with file_lock(path):
            raise RuntimeError("boom")
    with file_lock(path, blocking=False):
        pass


def test_the_lock_serializes_a_read_modify_write(tmp_path):
    path, counter = tmp_path / "x.lock", tmp_path / "n.txt"
    counter.write_text("0")

    def bump() -> None:
        for _ in range(25):
            with file_lock(path):
                value = int(counter.read_text())
                time.sleep(0.0005)
                counter.write_text(str(value + 1))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert counter.read_text() == "100"


def test_the_supervisor_and_its_worker_use_this_one_lock():
    assert supervisor.file_lock is file_lock
    assert worker.file_lock is file_lock
    assert not hasattr(supervisor, "_lock")


# -- the Windows path on any host --------------------------------------------


class FakeMsvcrt(types.ModuleType):
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self) -> None:
        super().__init__("msvcrt")
        self.calls: list[tuple[str, int, int, int]] = []  # (action, offset, nbytes, size)
        self.held: set[tuple[int, int, int]] = set()
        self.fail_with = errno.EACCES
        self.guard = threading.Lock()

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        key = (os.fstat(fd).st_ino, offset, nbytes)
        with self.guard:
            self.calls.append(("unlock" if mode == self.LK_UNLCK else "lock", offset, nbytes, os.fstat(fd).st_size))
            if mode == self.LK_UNLCK:
                self.held.discard(key)
                return
            assert mode == self.LK_NBLCK, f"only the nonblocking primitive may be used, got mode {mode}"
            if key in self.held:
                raise OSError(self.fail_with, "locked")
            self.held.add(key)


@pytest.fixture
def nt(monkeypatch):
    if os.name != "posix":
        pytest.skip("the fake msvcrt tests patch os.name on a POSIX host")
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` would raise on the nt path
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(fl, "POLL_S", 0.001)
    return fake


def test_windows_seeds_one_byte_then_locks_byte_zero_nonblocking_and_unlocks(tmp_path, nt):
    path = tmp_path / "x.lock"
    with file_lock(path):
        assert path.read_bytes() == b"0"
        assert nt.calls == [("lock", 0, 1, 1)]  # the seed precedes the lock; the lock is at offset 0
    assert nt.calls[-1][:3] == ("unlock", 0, 1) and nt.held == set()
    with file_lock(path):
        pass
    assert path.read_bytes() == b"0"  # an existing file is not reseeded or grown


def test_windows_nonblocking_refuses_a_held_lock_for_every_conflict_errno(tmp_path, nt):
    path = tmp_path / "x.lock"
    for code in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
        nt.fail_with = code
        with file_lock(path):
            outcome = try_lock(path, blocking=False, wait_s=3)
        assert isinstance(outcome, BlockingIOError) and "lock held" in str(outcome), (code, outcome)
    assert nt.held == set()


def test_windows_blocking_retries_until_the_holder_releases(tmp_path, nt):
    path = tmp_path / "x.lock"
    hold, held, errors = threading.Event(), threading.Event(), []
    thread = acquire_in_thread(path, blocking=True, hold=hold, held=held, errors=errors)
    assert held.wait(10)
    entered = threading.Event()

    def waiter() -> None:
        with file_lock(path):
            entered.set()

    second = threading.Thread(target=waiter, daemon=True)
    second.start()
    assert not entered.wait(0.2)
    hold.set()
    assert entered.wait(10)
    thread.join(5)
    second.join(5)
    assert not errors and nt.held == set()
    assert sum(1 for call in nt.calls if call[0] == "lock") > 2  # the wait was a retry loop


def test_windows_an_unrelated_oserror_propagates_and_unlock_runs_after_a_failing_block(tmp_path, nt):
    path = tmp_path / "x.lock"
    nt.fail_with = errno.EBADF
    with file_lock(path):
        outcome = try_lock(path, blocking=True, wait_s=3)
    assert isinstance(outcome, OSError) and not isinstance(outcome, BlockingIOError), outcome
    assert outcome.errno == errno.EBADF
    with pytest.raises(RuntimeError):
        with file_lock(path):
            raise RuntimeError("boom")
    assert nt.held == set()
