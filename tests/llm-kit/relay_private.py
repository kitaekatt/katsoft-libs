"""Platform-correct assertions for the relay's private-file promise (shared by the relay tests).

POSIX keeps a state directory ``0700`` and a credential ``0600``; Windows keeps the same promise with a
protected owner-plus-SYSTEM ACL (:mod:`llm_kit.relay.winacl`), which ``os.stat`` modes do not show.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX modes; the Windows ACL refusals are measured in test_relay_windows_native.py",
)


def assert_private(path: Path, *, directory: bool = False) -> None:
    """Assert ``path`` is private to the current user: mode 0600/0700 on POSIX, a validated ACL on Windows."""
    if os.name == "nt":
        from llm_kit.relay import winacl

        winacl.validate(path)
        return
    assert stat.S_IMODE(path.stat().st_mode) == (0o700 if directory else 0o600), path


def set_home(monkeypatch: pytest.MonkeyPatch, home: "os.PathLike[str] | str") -> None:
    """Point ``Path.home()`` at ``home`` on every platform (HOME on POSIX, USERPROFILE on Windows)."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
