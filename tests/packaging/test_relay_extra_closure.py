"""Closure of the ``relay`` extra (harness-messaging unit F1).

A fresh venv gets katsoft-contracts and the katsoft-llm-kit wheel built from
the working tree, installed with ONLY the ``relay`` extra. In that venv every
module under ``llm_kit.relay``, ``llm_kit.dispatch.factory`` (which the relay
reaches model backends through) and each third-party package the extra names
(mcp, uvicorn, httpx, psutil, openai, yaml) must import. Anything the relay
imports that the extra does not declare fails here, not on a user's machine.

Opt-in (marker ``wheel_closure``, see conftest.py). The wheel is always built
locally: a released wheel has no ``relay`` extra until the library release that
adds it.

    uv run --extra dev pytest tests/packaging/test_relay_extra_closure.py --wheel-source local -v

The ``test_closure_*_is_detected`` tests break the closure on purpose, by
removing one package from the extra in the wheel's metadata, and require the
same judging function to report it.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

import closure_support as support

pytestmark = pytest.mark.wheel_closure

RELAY_SOURCE = support.REPO / "packages" / support.KIT_DIST / "src" / "llm_kit" / "relay"
THIRD_PARTY = ("mcp", "uvicorn", "httpx", "psutil", "openai", "yaml")


def expected_modules() -> set[str]:
    """llm_kit.relay modules, read from the source tree (a module the wheel drops is a failure)."""
    names = {"llm_kit.relay"}
    for path in RELAY_SOURCE.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        rel = path.relative_to(RELAY_SOURCE).with_suffix("")
        names.add(".".join(["llm_kit.relay", *rel.parts]))
    return names


def relay_problems(report: dict) -> list[str]:
    """Why `report` (probes/relay_closure.py) is not a closed relay install; empty if closed."""
    problems = [f"{name}: {error}" for name, error in sorted(report["errors"].items())]
    missing = (expected_modules() | {"llm_kit.dispatch.factory", *THIRD_PARTY}) - set(report["imported"])
    problems += [f"not imported: {name}" for name in sorted(missing)
                 if not any(name in p for p in problems)]
    return problems


def _probe(venv, tmp_path) -> dict:
    return support.run_probe(venv, "relay_closure.py", home=tmp_path / "home", cwd=tmp_path)


def _relay_venv(local_wheels, root, wheel=None):
    kit = wheel or local_wheels["kit"]
    return support.make_venv(
        root, [str(local_wheels["contracts"]), f"{support.KIT_DIST}[relay] @ {kit.as_uri()}"])


@pytest.fixture(scope="module")
def relay_venv(local_wheels, tmp_path_factory):
    return _relay_venv(local_wheels, tmp_path_factory.mktemp("closure-relay"))


def test_the_extra_is_the_documented_set(local_wheels):
    import zipfile
    with zipfile.ZipFile(local_wheels["kit"]) as zf:
        meta = next(zf.read(n) for n in zf.namelist() if n.endswith(".dist-info/METADATA")).decode()
    names = sorted(re.match(r"[A-Za-z0-9_.-]+", spec).group(0).lower()
                   for spec in re.findall(r"^Requires-Dist: (.+?); extra == '(?:relay)'$", meta, re.M))
    assert names == sorted(["mcp", "uvicorn", "httpx", "psutil", "openai", "pyyaml"])


def test_the_wheel_carries_every_relay_module_and_schema(local_wheels):
    import zipfile
    with zipfile.ZipFile(local_wheels["kit"]) as zf:
        members = set(zf.namelist())
    for module in expected_modules():
        stem = module.replace(".", "/")
        assert f"{stem}.py" in members or f"{stem}/__init__.py" in members, module
    assert any(m.startswith("llm_kit/relay/schemas/") and m.endswith(".json") for m in members)


def test_relay_extra_installs_a_closed_relay(relay_venv, tmp_path):
    report = _probe(relay_venv, tmp_path)
    assert relay_problems(report) == []
    assert {"llm_kit.relay.protocol", "llm_kit.dispatch.factory"} <= set(report["imported"])


def _without(extra_member: str):
    """A mutate() for rewrite_wheel that drops `extra_member` from the relay extra."""
    pattern = re.compile(rf"^Requires-Dist: {extra_member}\b.*extra == 'relay'.*\r?\n", re.M | re.I)

    def mutate(members):
        key = next(n for n in members if n.endswith(".dist-info/METADATA"))
        text = members[key].decode("utf-8")
        edited, count = pattern.subn("", text)
        assert count == 1, f"{extra_member} is not declared once in the relay extra"
        members[key] = edited.encode("utf-8")

    return mutate


@pytest.mark.parametrize("package, module", [("openai", "openai"), ("pyyaml", "yaml")])
def test_closure_missing_package_is_detected(package, module, local_wheels, tmp_path):
    broken = support.rewrite_wheel(local_wheels["kit"], tmp_path / "wheel", _without(package))
    venv = _relay_venv(local_wheels, tmp_path / "venv-root", wheel=broken)
    problems = relay_problems(_probe(venv, tmp_path))
    assert any(module in p for p in problems), problems
