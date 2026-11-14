"""Real Git repositories for the branch-mode workspace and unit registry tests.

``hermetic_git`` points Git at an empty global configuration and no system
configuration, so a developer's hooks, ``core.hooksPath`` or signing settings
never reach the repositories under test. ``Repo`` is a clone with an ``origin``
remote whose ``dev`` branch is the merge target.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


def git(*args: str, cwd: Path, check: bool = True) -> str:
    """Run Git in ``cwd`` and return its stripped stdout."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {result.stderr}")
    return result.stdout.strip()


@pytest.fixture
def hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system Git configuration for the test or the code under test."""
    empty = tmp_path / "gitconfig-global"
    empty.write_text("", encoding="ascii")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)


@dataclass
class Repo:
    """A clone (``root``) of a bare ``remote`` with ``origin/dev`` fetched."""

    root: Path
    remote: Path

    @property
    def common_dir(self) -> Path:
        return (self.root / ".git").resolve()

    def head(self, ref: str = "HEAD") -> str:
        return git("rev-parse", ref, cwd=self.root)

    def log(self, ref: str) -> list[str]:
        return git("log", "--format=%s", ref, cwd=self.root).splitlines()

    def show(self, ref: str, path: str) -> str:
        return git("show", f"{ref}:{path}", cwd=self.root)

    def advance_remote(self, name: str, text: str) -> str:
        """Commit ``name`` on a scratch clone's dev, push it, fetch it here; return the sha."""
        scratch = self.root.parent / f"scratch-{name}"
        git("clone", "-q", "-b", "dev", str(self.remote), str(scratch), cwd=self.root.parent)
        configure(scratch)
        (scratch / name).write_text(text, encoding="utf-8")
        git("add", name, cwd=scratch)
        git("commit", "-q", "-m", f"add {name}", cwd=scratch)
        git("push", "-q", "origin", "dev", cwd=scratch)
        git("fetch", "-q", "origin", cwd=self.root)
        return git("rev-parse", "origin/dev", cwd=self.root)


def configure(path: Path) -> None:
    git("config", "user.name", "Unit Test", cwd=path)
    git("config", "user.email", "unit@example.invalid", cwd=path)
    git("config", "commit.gpgsign", "false", cwd=path)


def make_repo(base: Path, name: str = "project") -> Repo:
    """A bare remote with one commit on ``dev`` and a clone of it with ``origin/dev``."""
    remote = base / f"{name}-remote.git"
    seed = base / f"{name}-seed"
    root = base / name
    git("init", "-q", "--bare", "-b", "dev", str(remote), cwd=base)
    git("init", "-q", "-b", "dev", str(seed), cwd=base)
    configure(seed)
    (seed / "README.txt").write_text("seed\n", encoding="ascii")
    (seed / "pkg").mkdir()
    (seed / "pkg" / "module.txt").write_text("module\n", encoding="ascii")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-q", "origin", "dev", cwd=seed)
    git("clone", "-q", str(remote), str(root), cwd=base)
    configure(root)
    return Repo(root=root.resolve(), remote=remote.resolve())
