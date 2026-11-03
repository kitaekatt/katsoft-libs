"""Closure probe: can the interpreter this script runs under import the relay?

Run by tests/packaging/test_relay_extra_closure.py with a venv's own interpreter
in isolated mode (-I). It imports every module under llm_kit.relay, the module
the relay reaches model backends through (llm_kit.dispatch.factory) and each
third-party package the relay extra names, and prints one JSON object. It
asserts nothing; the test judges the report.
"""
import importlib
import json
import pkgutil

THIRD_PARTY = ("mcp", "uvicorn", "httpx", "psutil", "openai", "yaml")
report = {"relay_modules": [], "imported": [], "errors": {}}


def attempt(name):
    try:
        importlib.import_module(name)
        report["imported"].append(name)
    except BaseException as exc:  # noqa: BLE001 - the report names it
        report["errors"][name] = f"{type(exc).__name__}: {exc}"


try:
    import llm_kit.relay as relay
    report["relay_modules"] = ["llm_kit.relay"] + sorted(
        m.name for m in pkgutil.walk_packages(relay.__path__, "llm_kit.relay."))
except BaseException as exc:  # noqa: BLE001
    report["errors"]["llm_kit.relay"] = f"{type(exc).__name__}: {exc}"

for name in (*report["relay_modules"], "llm_kit.dispatch.factory", *THIRD_PARTY):
    attempt(name)

print(json.dumps(report))
