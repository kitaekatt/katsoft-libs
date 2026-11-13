#!/usr/bin/env python3
"""Stand-in for ``claude -p --resume <id> --output-format stream-json --verbose --permission-mode M`` (stdlib only).

It reads the prompt from stdin, appends one JSON line describing the call to ``FAKE_CLAUDE_LOG`` (argv, cwd,
pid, whether CLAUDECODE is set, the lease tag, the prompt), then prints stream-json events shaped as Claude
Code 2.1.317 prints them (measured 2026-11-13): an ``init`` system event naming session_id, cwd and
permissionMode, then a ``result`` event. A ``MODE=<name>`` token in the prompt picks the behaviour:

  reply (default)  init, then result "answer from claude"
  mismatch-cwd     init names another cwd (the run must be killed before a result)
  mismatch-mode    init names permissionMode bypassPermissions
  mismatch-session init names another session id
  noresult         init only, exit 0
  iserror          result with is_error true
  exit1            init and an ok result, then exit 1
  empty            result with an empty string
  stale            a result of THIS session printed before the init, then init and result "fresh"
  hang             init, then sleep (until killed)
"""
import json
import os
import re
import sys
import time

argv = sys.argv[1:]
session = argv[argv.index("--resume") + 1]
mode_flag = argv[argv.index("--permission-mode") + 1]
prompt = sys.stdin.read()
match = re.search(r"MODE=(\S+)", prompt)
mode = match.group(1) if match else "reply"
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({
            "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "pgid": os.getpgid(0),
            "claudecode": "CLAUDECODE" in os.environ,
            "claude_code_vars": sorted(k for k in os.environ if k.startswith("CLAUDE_CODE_")),
            "lease": os.environ.get("LLM_KIT_RESUME_LEASE"), "prompt": prompt,
        }) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(text, is_error=False, sid=None):
    emit({"type": "result", "subtype": "error_during_execution" if is_error else "success", "is_error": is_error,
          "result": text, "session_id": sid or session, "permission_denials": []})


cwd = os.getcwd()
init = {"type": "system", "subtype": "init", "session_id": session, "cwd": cwd, "permissionMode": mode_flag}
if mode == "stale":
    result("STALE-ANSWER")
emit({"type": "system", "subtype": "hook_started", "session_id": session})
if mode == "mismatch-cwd":
    init["cwd"] = "/somewhere/else"
elif mode == "mismatch-mode":
    init["permissionMode"] = "bypassPermissions"
elif mode == "mismatch-session":
    init["session_id"] = "00000000-0000-4000-8000-000000000000"
emit(init)
if mode.startswith("mismatch"):
    time.sleep(30)  # the relay must kill us before we print a result
    result("MUST-NOT-BE-RETURNED")
    sys.exit(0)
if mode == "noresult":
    sys.exit(0)
if mode == "iserror":
    result("model refused", is_error=True)
    sys.exit(0)
if mode == "exit1":
    result("answer from claude")
    sys.exit(1)
if mode == "empty":
    result("")
    sys.exit(0)
if mode == "hang":
    time.sleep(300)
    sys.exit(0)
result("fresh" if mode == "stale" else "answer from claude")
