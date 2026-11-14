"""A cross-process exclusive file lock. Standard library only.

One lock file serializes writers across processes on POSIX (``flock``) and
Windows (``msvcrt`` byte-range locking). Used by the job supervisor for its
lease and ownership locks and by the agent-session directory for its mailbox
lock. A lock is held for the duration of the ``with`` block and released when
it exits, including when the block raises. The lock file is never removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time

#: Seconds between attempts of the Windows blocking wait.
POLL_S = 0.02


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive lock on the file ``path`` while the block runs.

    ``path`` is created, with its parent directories, when absent. The lock is
    advisory and cooperative: it excludes only other holders of the same file
    through this function.

    ``blocking=True`` waits for the holder to release. ``blocking=False``
    raises ``BlockingIOError`` at once when another holder has the lock. An
    ``OSError`` other than a lock conflict propagates. On Windows the lock
    covers byte 0, which is seeded with one byte when the file is empty; the
    file's content is never meant to be read, because a byte-range lock also
    denies reads of the locked range.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            # Windows byte-range locks also deny reads. Check metadata before
            # locking instead of reading a byte another owner can hold.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            # msvcrt's blocking lock has a fixed ten-retry limit. Use the
            # nonblocking primitive and an explicit wait for the blocking case.
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if not blocking:
                        raise BlockingIOError(f"File lock held: {path}") from None
                    time.sleep(POLL_S)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = ["file_lock", "POLL_S"]
