"""scripts/npm_registry.py: publish once, wait for the registry, compare integrity.

No test calls npm or sleeps: `view`, `publish`, the clock and `sleep` are fakes.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import npm_registry as reg

NAME, VERSION, TARBALL, SUM = "@katsoft/x", "1.0.0", "/t/x.tgz", "sha512-aaa"


class Fake:
    """A registry that lists the version after `lag` seconds and a virtual clock."""

    def __init__(self, listed: str | None = None, lag: float = 0, publish_result=(0, "")):
        self.now = 0.0
        self.listed, self.lag = listed, lag
        self.publish_result = publish_result
        self.published = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def view(self, spec: str) -> str | None:
        assert spec == f"{NAME}@{VERSION}"
        return self.listed if self.now >= self.lag else None

    def publish(self, tarball: str) -> tuple[int, str]:
        assert tarball == TARBALL
        self.published += 1
        return self.publish_result

    def run(self, **kw) -> str:
        return reg.release(NAME, VERSION, TARBALL, SUM, view=self.view, publish=self.publish,
                           sleep=self.sleep, clock=self.clock, log=lambda _m: None, **kw)


def test_publish_then_wait_for_a_slow_registry():
    fake = Fake(listed=SUM, lag=400)
    assert fake.run(timeout=600, interval=15) == "published"
    assert fake.published == 1


def test_listed_version_is_not_published_again():
    fake = Fake(listed=SUM)
    assert fake.run() == "already on the registry"
    assert fake.published == 0


def test_409_after_a_slow_listing_counts_as_already_published():
    # First view saw nothing (registry lag), publish answered 409, the registry lists it later.
    fake = Fake(listed=SUM, lag=30, publish_result=(1, "npm error 409 Conflict - cannot publish over the previously published versions"))
    assert fake.run() == "already on the registry"
    assert fake.published == 1


def test_other_publish_failure_is_loud():
    fake = Fake(listed=SUM, lag=1, publish_result=(1, "npm error 401 Unauthorized"))
    with pytest.raises(reg.ReleaseError, match="401 Unauthorized"):
        fake.run()


def test_integrity_mismatch_fails_loudly():
    with pytest.raises(reg.ReleaseError, match="sha512-bbb.*sha512-aaa"):
        Fake(listed="sha512-bbb").run()


def test_timeout_reports_the_last_answer():
    fake = Fake(listed=SUM, lag=10_000)
    with pytest.raises(reg.ReleaseError, match="does not list.*after 600 s"):
        fake.run(timeout=600, interval=15)
    assert fake.now >= 600
