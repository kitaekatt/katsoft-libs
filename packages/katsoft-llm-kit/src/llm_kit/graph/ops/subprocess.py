"""``llm.subprocess``: run a command with JSON on stdin and JSON on stdout.

::

    encode:
      op: llm.subprocess
      in: {payload: bundle.out}
      argv: [node, pipeline/moments/beta/encode/stdio.mjs]
      timeout_s: 120

The command gets ``payload`` (the ``payload`` input, default ``null``) as one
UTF-8 JSON document on stdin and must print exactly one JSON value on stdout and
exit with status 0. The result is that value (checked against ``output_schema``
when given). Rules:

* ``argv`` is a literal list (no shell). ``cwd`` is a directory under the
  project root (default: the root).
* The child starts a new session and gets only the environment variables named
  by ``env_allow`` (default :data:`DEFAULT_ENV_ALLOW`) that are set, plus the
  literal ``env`` mapping. Nothing else is inherited.
* ``timeout_s`` (default 300) bounds the whole call, readers included. A
  timeout, an output over ``max_output_bytes`` (default 64 MiB), and the end of
  the call all kill the child's whole process group.
* stderr is kept: the last 64 KiB, in the receipt (``stderr``) and in the
  message of a failure. Environment values never enter the receipt.

Failures raise :class:`OpFailed`: ``spawn-failed``, ``timeout``,
``output-too-large``, ``exit-status``, ``invalid-json``, ``schema-mismatch``.
POSIX only (process groups); other platforms raise ``unsupported-platform``.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from llm_kit.schema.json_schema import validate as validate_value

from ..model import OpContext, OpResult, ParamSpec, Port
from .registry import OpFailed, op

__all__ = [
    "DEFAULT_ENV_ALLOW",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S",
    "STDERR_TAIL_BYTES",
    "llm_subprocess",
]

DEFAULT_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
STDERR_TAIL_BYTES = 64 * 1024
_CHUNK = 64 * 1024


def _fault(ctx: OpContext, message: str) -> ValueError:
    return ValueError(f"llm.subprocess {ctx.node_path}: {message}")


def _strings(ctx: OpContext, name: str, value: Any, *, empty: bool) -> list[str]:
    if (not isinstance(value, list) or (not value and not empty)
            or not all(isinstance(item, str) and item for item in value)):
        raise _fault(ctx, f"{name} must be a list of non-empty strings" + ("" if empty else " (at least one)"))
    return list(value)


def _number(ctx: OpContext, name: str, value: Any, *, integer: bool) -> float:
    ok = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok or not value > 0:
        raise _fault(ctx, f"{name} must be a positive {'integer' if integer else 'number'}, not {value!r}")
    return value


def _directory(ctx: OpContext, value: Any) -> Path:
    if value is None:
        return ctx.project_root
    if not isinstance(value, str) or not value:
        raise _fault(ctx, "cwd must be a path string")
    root = ctx.project_root.resolve()
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise _fault(ctx, f"cwd {value!r} resolves outside the project root")
    if not path.is_dir():
        raise _fault(ctx, f"cwd {value!r} is not a directory")
    return path


def _environment(ctx: OpContext, params: Any) -> dict[str, str]:
    allow = _strings(ctx, "env_allow", params.get("env_allow", list(DEFAULT_ENV_ALLOW)), empty=True)
    extra = params.get("env", {})
    if (not isinstance(extra, dict)
            or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in extra.items())):
        raise _fault(ctx, "env must be a mapping of names to string values")
    env = {name: os.environ[name] for name in allow if name in os.environ}
    env.update(extra)
    return env


def _refuse_constant(token: str) -> Any:
    raise ValueError(f"non-JSON constant {token}")


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate object key {key!r}")
        out[key] = value
    return out


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # the group is already gone: the normal case after a clean exit


def _tail(stderr: "deque[bytes]", dropped: bool) -> tuple[str, bool]:
    data = b"".join(stderr)
    return data[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace"), dropped or len(data) > STDERR_TAIL_BYTES


@op(
    "llm.subprocess",
    version="1",
    inputs={"payload": Port({}, default=None, description="the JSON document written to stdin")},
    params={
        "argv": ParamSpec("literal", required=True, description="the command as a list of strings"),
        "cwd": ParamSpec("literal", description="a directory under the project root"),
        "env_allow": ParamSpec("literal", description="names of environment variables passed through"),
        "env": ParamSpec("literal", description="extra environment variables (name -> string)"),
        "timeout_s": ParamSpec("literal", description="wall-clock cap in seconds"),
        "max_output_bytes": ParamSpec("literal", description="stdout cap in bytes"),
        "output_schema": ParamSpec("schema", description="the stdout JSON value must match it"),
    },
    output_schema_param="output_schema",
    description="Run a command: JSON on stdin, one JSON value on stdout.",
)
def llm_subprocess(ctx: OpContext, payload: Any) -> OpResult:
    params = ctx.params
    argv = _strings(ctx, "argv", params.get("argv"), empty=False)
    timeout_s = float(_number(ctx, "timeout_s", params.get("timeout_s", DEFAULT_TIMEOUT_S), integer=False))
    limit = int(_number(ctx, "max_output_bytes", params.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES),
                        integer=True))
    cwd = _directory(ctx, params.get("cwd"))
    env = _environment(ctx, params)
    stdin = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    where = f"llm.subprocess {ctx.node_path} ({argv[0]})"
    receipt: dict[str, Any] = {
        "argv": argv, "cwd": str(cwd), "env_names": sorted(env), "timeout_s": timeout_s,
        "max_output_bytes": limit, "stdin_bytes": len(stdin),
    }
    if os.name != "posix":
        raise OpFailed(f"{where}: process groups need a POSIX platform", code="unsupported-platform",
                       receipt=receipt)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd, env=env, start_new_session=True)
    except OSError as exc:
        raise OpFailed(f"{where}: cannot start the command: {exc}", code="spawn-failed", receipt=receipt) from None

    stdout = bytearray()
    stderr: deque[bytes] = deque()
    stderr_bytes = [0]
    stderr_dropped = [False]
    overflow = threading.Event()

    def feed() -> None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the command exited or never reads stdin; its exit status and output decide the result

    def read_stdout() -> None:
        while chunk := proc.stdout.read(_CHUNK):
            stdout.extend(chunk)
            if len(stdout) > limit:
                overflow.set()
                _kill_group(proc.pid)
                return

    def read_stderr() -> None:
        while chunk := proc.stderr.read(_CHUNK):
            stderr.append(chunk)
            stderr_bytes[0] += len(chunk)
            while stderr_bytes[0] - len(stderr[0]) >= STDERR_TAIL_BYTES:
                stderr_bytes[0] -= len(stderr.popleft())
                stderr_dropped[0] = True

    threads = [threading.Thread(target=target, daemon=True) for target in (feed, read_stdout, read_stderr)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _kill_group(proc.pid)  # the child, a timeout, or stragglers that kept the pipes open
        proc.wait()
        for thread in threads:
            thread.join(timeout=5.0)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
    receipt["wall_ms"] = int((time.monotonic() - started) * 1000)
    receipt["exit_code"] = proc.returncode
    receipt["stdout_bytes"] = len(stdout)
    receipt["stdout_sha256"] = hashlib.sha256(bytes(stdout)).hexdigest()
    receipt["stderr"], receipt["stderr_truncated"] = _tail(stderr, stderr_dropped[0])
    detail = f"; stderr: {receipt['stderr'].strip()!r}" if receipt["stderr"].strip() else ""

    if timed_out:
        raise OpFailed(f"{where}: no result within {timeout_s} s{detail}", code="timeout", receipt=receipt)
    if overflow.is_set():
        raise OpFailed(f"{where}: stdout is over {limit} bytes", code="output-too-large", receipt=receipt)
    if proc.returncode != 0:
        raise OpFailed(f"{where}: exit status {proc.returncode}{detail}", code="exit-status", receipt=receipt)
    out = _parse(where, bytes(stdout), receipt)
    if "output_schema" in params:
        errors = validate_value(params["output_schema"], out)
        if errors:
            faults = "; ".join(f"{pointer or '/'} {keyword}" for pointer, keyword in errors)
            raise OpFailed(f"{where}: the output does not match output_schema: {faults}", code="schema-mismatch",
                           receipt=receipt)
    return OpResult(out=out, receipt=receipt)


def _parse(where: str, data: bytes, receipt: dict) -> Any:
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_refuse_constant, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise OpFailed(f"{where}: stdout is not one JSON value: {exc}", code="invalid-json",
                       receipt=receipt) from None
