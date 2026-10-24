#!/usr/bin/env python3
"""Release the registered libraries independently of plugin publication."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import _packages

ROOT = Path(__file__).resolve().parents[1]


def prepare(repo: _packages.Repo) -> tuple[str, _packages.Plan]:
    """Freeze a clean dev commit and validate its standalone package plan."""
    if repo.git("branch", "--show-current") != _packages.DEV_BRANCH:
        raise _packages.Refusal("Run the release command on the dev branch.")
    if repo.git("status", "--porcelain"):
        raise _packages.Refusal("Commit the working changes before releasing packages.")
    base = repo.sha("HEAD")
    registry = _packages.read_registry(repo, base)
    if registry.repository != "https://github.com/kitaekatt/katsoft-libs":
        raise _packages.Refusal("The registry must identify kitaekatt/katsoft-libs.")
    plan = _packages.plan(repo, base, only=None, shipping_plugins=set(), master=base)
    return base, plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Push release tags and record verified PyPI releases (requires publication approval)")
    args = parser.parse_args(argv)
    repo = _packages.Repo(ROOT)
    try:
        base, plan = prepare(repo)
        for note in plan.notes:
            print(note)
        if not plan.release_set:
            print("No package versions need release.")
            return 0
        for dist in plan.release_set:
            print(f"release {dist} {plan.versions[dist]}")
        if not args.execute:
            print("Plan only. After publication approval, run with --execute.")
            return 0
        repo.fetch()
        if repo.sha(repo.remote_dev()) != base:
            raise _packages.Refusal("Push the reviewed dev commit before releasing packages.")
        # GitHub dispatch needs the workflow on the default branch. dev is the
        # standalone repository's default branch; no plugin projection is needed.
        _packages.Release(repo, plan, base, _packages.Services.real(ROOT)).run()
        return 0
    except _packages.PackageError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
