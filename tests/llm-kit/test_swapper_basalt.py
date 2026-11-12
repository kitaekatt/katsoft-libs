"""Basalt (Flash-Next) processes are recognised as model servers.

The engine has process name ``basalt``; the server is the venv Python running
``-m serve.server --engine-bin <dir>/bin/basalt``. Both match by argv anchored
at argv[0], so a shell or wrapper that only mentions the strings never matches.
"""

from __future__ import annotations

from llm_kit.adapters import swapper
from llm_kit.adapters._swapper_process import ProcessRecord

BIN = "/home/u/Dev/basalt/bin/basalt"
PACK = "/home/u/Dev/basalt/models/Qwen3.8-FN-UD-IQ3_XXS.basalt"


def _rec(pid, name, cmdline, ppid=1):
    return ProcessRecord(
        pid=pid, ppid=ppid, name=name, owner="u", create_time=1.0, cmdline=tuple(cmdline)
    )


def test_engine_is_recognised():
    rec = _rec(10, "basalt", [BIN, "--serve", "--model", PACK])
    assert swapper.classify_server(rec) is True


def test_server_is_recognised():
    rec = _rec(
        11,
        "python3",
        ["/home/u/Dev/basalt/.venv/bin/python", "-m", "serve.server", "--engine-bin", BIN],
    )
    assert swapper.classify_server(rec) is True


def test_wrappers_that_mention_basalt_are_not_recognised():
    shell = _rec(12, "bash", ["bash", "-c", f"{BIN} --serve --model {PACK}"])
    grep = _rec(13, "pgrep", ["pgrep", "-f", f"{BIN} --serve"])
    other_python = _rec(14, "python3", ["python3", "-m", "http.server", "--engine-bin", BIN])
    engine_no_serve = _rec(15, "basalt", [BIN, "--version"])
    for rec in (shell, grep, other_python, engine_no_serve):
        assert swapper.classify_server(rec) is False


def test_unreadable_python_stays_indeterminate():
    rec = ProcessRecord(
        pid=16, ppid=1, name="python3", owner="u", create_time=1.0, cmdline=None
    )
    assert swapper.classify_server(rec) is None


class _Inspector:
    def __init__(self, procs):
        self._procs = tuple(procs)

    def current_owner(self):
        return "u"

    def processes(self):
        return self._procs

    def process(self, pid):
        for p in self._procs:
            if p.pid == pid:
                return p
        raise swapper.ProcessNotFound(str(pid))


def test_find_strays_reports_basalt_without_swapper_ancestor():
    swap = _rec(1, "llama-swap", ["llama-swap"], ppid=0)
    child_engine = _rec(20, "basalt", [BIN, "--serve"], ppid=1)
    stray_engine = _rec(30, "basalt", [BIN, "--serve"], ppid=0)
    stray_server = _rec(
        31, "python3", ["python", "-m", "serve.server", "--engine-bin", BIN], ppid=0
    )
    strays = swapper.find_strays(
        inspector=_Inspector([swap, child_engine, stray_engine, stray_server])
    )
    assert {r.pid for r in strays} == {30, 31}
