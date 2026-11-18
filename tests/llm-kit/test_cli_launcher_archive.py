"""The console-script launcher must not stay open for the life of the process.

A console script (``llm-kit.exe`` on Windows) is a zip archive with a ``__main__.py``,
and the interpreter puts it at ``sys.path[0]``. ``importlib.metadata`` builds a path
object for every ``sys.path`` entry on its first scan and caches it; for a zip that
object holds the archive open. Windows then refuses to delete or rename the launcher
while the process lives, so ``uv sync`` cannot upgrade a virtual environment that a
long-running ``llm-kit relay serve`` uses. The CLI drops the launcher from
``sys.path`` before the first metadata scan (``llm_kit.cli.main``, console path).

Each case runs in a child interpreter, so the metadata caches and ``sys.path`` of the
test process stay untouched, and so the child starts with no scan behind it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "packages" / "katsoft-llm-kit" / "src"

#: Plays the console script: put the launcher archive first on ``sys.path`` (where the
#: interpreter puts it), call ``main`` the way the script does, run the metadata scans
#: the relay and the runtime check run, then report what still holds the archive.
_CHILD = textwrap.dedent(
    """
    import gc, json, os, sys, zipfile

    launcher = sys.argv[1]
    argv = sys.argv[2:]
    sys.path.insert(0, launcher)
    sys.argv = ["llm-kit", *argv]

    from llm_kit.cli import main
    try:
        main()
    except SystemExit:
        pass
    # What `llm-kit relay serve` does at start, and the scan behind every
    # distribution lookup.
    import importlib.metadata
    from llm_kit.relay.service import library_version
    library_version()
    list(importlib.metadata.distributions())

    def same(a, b):
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))

    open_archives = [
        z.filename for z in gc.get_objects()
        if isinstance(z, zipfile.ZipFile) and getattr(z, "fp", None) is not None
        and z.filename and same(z.filename, launcher)
    ]
    moved = None
    if os.name == "nt":
        target = launcher + ".moved"
        try:
            os.rename(launcher, target)
            os.rename(target, launcher)
            moved = True
        except OSError as exc:
            moved = f"winerror={getattr(exc, 'winerror', None)}"
    print(json.dumps({
        "open_archives": open_archives,
        "rename": moved,
        "launcher_on_path": any(isinstance(p, str) and p and same(p, launcher) for p in sys.path),
        "path0": sys.path[0],
    }))
    """
)


def _launcher(tmp_path: Path) -> Path:
    """An archive shaped like a console-script launcher: a stub, then a zip with ``__main__.py``."""
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("__main__.py", "raise SystemExit(0)\n")
    exe = tmp_path / "llm-kit-launcher.exe"
    exe.write_bytes(b"MZ" + b"\0" * 62 + inner.read_bytes())
    return exe


def _run_child(entry: Path, *, script: str = _CHILD, argv: tuple[str, ...] = ("--help",)) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(LIB), str(REPO / "packages" / "katsoft-contracts" / "src")])
    env["_BOOTSTRAP_GUARD_SKIP_REEXEC"] = "1"
    for name in ("CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PLUGIN_TEST", "PYTHONSTARTUP"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(entry), *argv],
        capture_output=True, text=True, env=env, timeout=120, cwd=entry.parent,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_launcher_archive_is_not_held_open_after_a_metadata_scan(tmp_path: Path) -> None:
    # Fails before the fix: the first metadata scan opens the archive and the
    # cached path object keeps it open (on Windows the rename fails, os error 32).
    report = _run_child(_launcher(tmp_path))
    assert report["open_archives"] == []
    assert report["rename"] in (None, True)
    assert report["launcher_on_path"] is False


def test_a_library_call_with_argv_keeps_sys_path_as_it_was(tmp_path: Path) -> None:
    # main(argv) is a library call in the caller's interpreter; it must not edit
    # the caller's sys.path. Only the console path (argv omitted) drops the launcher.
    script = _CHILD.replace("main()\n", 'main(["--help"])\n', 1)
    assert script != _CHILD
    report = _run_child(_launcher(tmp_path), script=script)
    assert report["launcher_on_path"] is True


def test_a_directory_at_the_front_of_sys_path_is_kept(tmp_path: Path) -> None:
    # `python -m llm_kit.cli` puts a directory first; only a file (the launcher) goes.
    directory = tmp_path / "front"
    directory.mkdir()
    report = _run_child(directory)
    assert report["launcher_on_path"] is True
    assert report["path0"] == str(directory)
