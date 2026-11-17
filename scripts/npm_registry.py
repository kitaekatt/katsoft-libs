#!/usr/bin/env python3
"""Publish one npm tarball, wait for the registry, compare integrity.

`python3 scripts/npm_registry.py <name> <version> <tarball> <integrity>` (used by
release-npm.yml). The registry lists a new version only minutes after `npm
publish` succeeds. So the command:

1. skips the publish when the registry already lists the version;
2. publishes otherwise, and treats a "previously published" refusal (409/403) as
   "already published", so a rerun after a slow listing is safe;
3. polls `npm view <name>@<version> dist.integrity` up to --timeout seconds,
   and fails with the last answer when the registry never lists it;
4. fails when the registry's integrity differs from the packed integrity.

Prints `MODE=<published|already on the registry>` for $GITHUB_ENV.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Callable

ALREADY_PUBLISHED = ("previously published", "409", "E403")


class ReleaseError(Exception):
    """The release cannot be confirmed; the message says why."""


def npm_view(spec: str) -> str | None:
    """Return dist.integrity of `spec` from the registry, or None when it is not listed."""
    done = subprocess.run(["npm", "view", spec, "dist.integrity"], capture_output=True, text=True)
    return done.stdout.strip() or None


def npm_publish(tarball: str) -> tuple[int, str]:
    """Run `npm publish`; return its exit code and its combined output."""
    done = subprocess.run(["npm", "publish", tarball, "--provenance", "--access", "public"],
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def release(name: str, version: str, tarball: str, integrity: str, *,
            view: Callable[[str], str | None] = npm_view,
            publish: Callable[[str], tuple[int, str]] = npm_publish,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic,
            log: Callable[[str], None] = print,
            timeout: float = 600, interval: float = 15) -> str:
    """Publish when needed, wait for the listing, compare integrity; return the mode.

    Raises ReleaseError on a publish failure, a timeout, or an integrity mismatch.
    """
    spec = f"{name}@{version}"
    mode = "already on the registry"
    if view(spec) is None:
        code, output = publish(tarball)
        if code == 0:
            mode = "published"
        elif any(marker in output for marker in ALREADY_PUBLISHED):
            log(f"{spec} was already published; waiting for the registry listing")
        else:
            raise ReleaseError(f"npm publish of {spec} failed:\n{output}")
    deadline = clock() + timeout
    while True:
        listed = view(spec)
        if listed is not None:
            break
        if clock() >= deadline:
            raise ReleaseError(f"the registry does not list {spec} after {timeout:g} s (last answer: nothing)")
        log(f"the registry does not list {spec} yet; waiting {interval:g} s")
        sleep(interval)
    if listed != integrity:
        raise ReleaseError(f"{spec}: registry integrity {listed} differs from packed integrity {integrity}")
    return mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name")
    parser.add_argument("version")
    parser.add_argument("tarball")
    parser.add_argument("integrity")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args(argv)
    try:
        mode = release(args.name, args.version, args.tarball, args.integrity,
                       log=lambda m: print(m, file=sys.stderr, flush=True),
                       timeout=args.timeout, interval=args.interval)
    except ReleaseError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"MODE={mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
