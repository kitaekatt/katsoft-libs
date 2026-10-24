"""The standalone command plans without publishing and freezes reviewed input."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

import test_package_release_states as states

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import _packages
import release


@pytest.fixture
def repo(tmp_path: Path) -> _packages.Repo:
    root = states.make_repo(tmp_path)
    registry = root / "packages" / "registry.toml"
    registry.write_text(registry.read_text().replace(states.REPOSITORY,
                        "https://github.com/kitaekatt/katsoft-libs"))
    states.git(root, "add", "packages/registry.toml")
    states.git(root, "commit", "-qm", "set library repository identity")
    return _packages.Repo(root)


def test_plan_only_never_builds_uploads_or_pushes(repo: _packages.Repo,
                                                monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(release, "ROOT", repo.root)
    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("a plan reached publication")
    monkeypatch.setattr(_packages.Release, "run", forbid)
    monkeypatch.setattr(_packages.Services, "real", forbid)
    before = states.git(repo.root, "rev-parse", "HEAD")
    assert release.main([]) == 0
    assert "Plan only" in capsys.readouterr().out
    assert states.git(repo.root, "rev-parse", "HEAD") == before
    assert states.git(repo.root, "tag", "--list") == ""


def test_dirty_source_is_refused(repo: _packages.Repo) -> None:
    (repo.root / "pending.txt").write_text("pending\n")
    with pytest.raises(_packages.Refusal, match="Commit the working changes"):
        release.prepare(repo)


def test_other_branch_is_refused(repo: _packages.Repo,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    real_git = repo.git
    def fake_git(*args: str, **kwargs: object) -> str:
        if args == ("branch", "--show-current"):
            return "other"
        return real_git(*args, **kwargs)
    monkeypatch.setattr(repo, "git", fake_git)
    with pytest.raises(_packages.Refusal, match="dev branch"):
        release.prepare(repo)


def test_historical_surface_path_is_used_after_a_move(repo: _packages.Repo) -> None:
    root = repo.root
    registry = root / "packages" / "registry.toml"
    before = states.git(root, "rev-parse", "HEAD")
    moved = root / "contracts" / "alpha.py"
    moved.parent.mkdir()
    moved.write_text('__all__ = ["one"]\n')
    registry.write_text(registry.read_text().replace('all = "libs/alpha/__init__.py"',
                                                  'all = "contracts/alpha.py"'))
    states.git(root, "add", ".")
    states.git(root, "commit", "-qm", "move surface and remove a public name")
    after = states.git(root, "rev-parse", "HEAD")
    entry = _packages.read_registry(repo, after).entry(states.DIST)
    with pytest.raises(_packages.Refusal, match="without a major bump"):
        _packages._require_major_for_removed_names(repo, after, entry, "1.1.0",
                                                  {"1.0.0": {"commit": before}})
