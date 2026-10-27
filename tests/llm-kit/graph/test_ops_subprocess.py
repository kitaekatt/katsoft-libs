"""llm.subprocess: JSON on stdin, JSON on stdout, bounded, killed as a process group."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from llm_kit.graph.loader import resolve_schema
from llm_kit.graph.ops import OpFailed
from llm_kit.graph.ops.subprocess import STDERR_TAIL_BYTES, llm_subprocess
from llm_kit.graph.testing import op_context

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups need POSIX")


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def run(tmp_path, code: str, payload=None, **params):
    return llm_subprocess(op_context(tmp_path, params={"argv": py(code), **params}), payload)


ECHO = "import json,sys; print(json.dumps(json.load(sys.stdin)))"


def test_json_round_trip_with_unicode(tmp_path):
    payload = {"name": "caf\u00e9 \u2603", "n": [1, 2.5, None, True], "nested": {"k": "v"}}
    result = run(tmp_path, ECHO, payload)
    assert result.out == payload
    assert result.receipt["exit_code"] == 0
    assert result.receipt["stdin_bytes"] == len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def test_the_default_payload_is_null(tmp_path):
    assert run(tmp_path, ECHO, None).out is None


def test_large_json_output_is_intact(tmp_path):
    code = "import json; print(json.dumps(['x' * 100] * 50000))"
    result = run(tmp_path, code)
    assert result.out == ["x" * 100] * 50000
    assert result.receipt["stdout_bytes"] > 5_000_000


def test_large_stdin_does_not_deadlock(tmp_path):
    payload = ["y" * 100] * 50000
    result = run(tmp_path, "import json,sys; d=json.load(sys.stdin); print(json.dumps(len(d)))", payload)
    assert result.out == 50000


def test_output_over_the_limit_fails_fast(tmp_path):
    code = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
    started = time.monotonic()
    with pytest.raises(OpFailed) as caught:
        run(tmp_path, code, max_output_bytes=100_000)
    assert caught.value.code == "output-too-large"
    assert time.monotonic() - started < 10


def test_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    with pytest.raises(OpFailed) as caught:
        run(tmp_path, code, timeout_s=1.5)
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 20
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:  # pragma: no cover - the failure being tested
        os.kill(pid, 9)
        pytest.fail("the grandchild survived the timeout")


def test_a_straggler_that_holds_the_pipes_does_not_hang_a_finished_call(tmp_path):
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print('7')\n"
    )
    started = time.monotonic()
    result = run(tmp_path, code, timeout_s=60)
    assert result.out == 7
    assert time.monotonic() - started < 20


def test_a_nonzero_exit_fails_with_stderr(tmp_path):
    code = "import sys; sys.stderr.write('boom happened'); print('1'); sys.exit(3)"
    with pytest.raises(OpFailed) as caught:
        run(tmp_path, code)
    assert caught.value.code == "exit-status"
    assert "exit status 3" in str(caught.value) and "boom happened" in str(caught.value)
    assert caught.value.receipt["exit_code"] == 3
    assert caught.value.receipt["stderr"] == "boom happened"


def test_stderr_is_kept_on_success_and_truncated_to_the_tail(tmp_path):
    code = "import sys; sys.stderr.write('a' * 200000 + 'END'); print('1')"
    result = run(tmp_path, code)
    stderr = result.receipt["stderr"]
    assert len(stderr) == STDERR_TAIL_BYTES and stderr.endswith("END")
    assert result.receipt["stderr_truncated"] is True
    small = run(tmp_path, "import sys; sys.stderr.write('note'); print('1')")
    assert (small.receipt["stderr"], small.receipt["stderr_truncated"]) == ("note", False)


@pytest.mark.parametrize("code", ["pass", "print('not json')", "print('1'); print('2')", "print('NaN')",
                                  "print('{\"a\": 1, \"a\": 2}')",
                                  "import sys; sys.stdout.buffer.write(b'\\xff')"])
def test_stdout_must_be_exactly_one_json_value(tmp_path, code):
    with pytest.raises(OpFailed) as caught:
        run(tmp_path, code)
    assert caught.value.code == "invalid-json"


def test_output_schema_is_enforced(tmp_path):
    schema = resolve_schema({"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}, ".")
    assert run(tmp_path, "print('{\"a\": 1}')", output_schema=schema).out == {"a": 1}
    with pytest.raises(OpFailed) as caught:
        run(tmp_path, "print('{\"a\": \"x\"}')", output_schema=schema)
    assert caught.value.code == "schema-mismatch"


def test_only_allowlisted_environment_reaches_the_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_SECRET", "s3cret")
    monkeypatch.setenv("GRAPH_SHARED", "shared")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    default = run(tmp_path, code).out
    assert "GRAPH_SECRET" not in default and "PATH" in default
    custom = run(tmp_path, code, env_allow=["GRAPH_SHARED"], env={"EXTRA": "x"}).out
    assert custom["GRAPH_SHARED"] == "shared" and custom["EXTRA"] == "x" and "PATH" not in custom
    receipt = run(tmp_path, code, env={"EXTRA": "x"}).receipt
    assert "EXTRA" in receipt["env_names"] and "s3cret" not in json.dumps(receipt)


def test_cwd_is_under_the_project_root(tmp_path):
    (tmp_path / "work").mkdir()
    code = "import json, os; print(json.dumps(os.path.realpath(os.getcwd())))"
    assert run(tmp_path, code, cwd="work").out == str((tmp_path / "work").resolve())
    assert run(tmp_path, code).out == str(tmp_path.resolve())
    with pytest.raises(ValueError, match="outside the project root"):
        run(tmp_path, code, cwd="..")
    with pytest.raises(ValueError, match="not a directory"):
        run(tmp_path, code, cwd="nope")


def test_a_missing_executable_is_a_spawn_failure(tmp_path):
    with pytest.raises(OpFailed) as caught:
        llm_subprocess(op_context(tmp_path, params={"argv": ["/no/such/command-xyz"]}), None)
    assert caught.value.code == "spawn-failed"


@pytest.mark.parametrize("params", [
    {"argv": []}, {"argv": "node x"}, {"argv": ["a", 3]}, {"argv": ["a"], "timeout_s": 0},
    {"argv": ["a"], "timeout_s": True}, {"argv": ["a"], "max_output_bytes": 1.5},
    {"argv": ["a"], "env": {"A": 1}}, {"argv": ["a"], "env_allow": "PATH"},
])
def test_bad_parameters_fail_loudly(tmp_path, params):
    with pytest.raises(ValueError, match="llm.subprocess"):
        llm_subprocess(op_context(tmp_path, params=params), None)


def test_a_child_that_ignores_stdin_is_not_an_error(tmp_path):
    result = run(tmp_path, "print('1')", ["z" * 100] * 100000)
    assert result.out == 1
