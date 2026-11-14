#!/usr/bin/env python3
"""Stand-in for ``claude -p --input-format stream-json --output-format stream-json --verbose`` (stdlib only).

It reads stream-json lines from stdin and prints stream-json events shaped as Claude Code 2.1.325 prints them
(measured 2026-11-14, agent-runs log.md, verify-stream-json): every turn starts with a ``system:init`` event
naming session_id, cwd and permissionMode; a turn ends with a ``result`` event; the process idles on stdin
after a result and exits 0 at EOF; an interrupt ``control_request`` gets a ``control_response`` and ends a
busy turn with ``result:error_during_execution``.

``FAKE_SESSION_LOG`` names a file prefix. ``<prefix>.argv.json`` gets the argv, cwd, pid, process group, the
session tag and whether CLAUDECODE was set; ``<prefix>.stdin.jsonl`` gets every stdin line as received;
``<prefix>.stdout`` gets every byte written to stdout; ``<prefix>.sigint`` gets one line per SIGINT received.
``FAKE_SESSION_SCENARIO`` picks the behaviour:

  basic        each user message: init, an assistant event, result success "echo: <text>"
  background   as basic, then 0.6 s after the FIRST result a background turn starts by itself:
               init, and 2.0 s later result success "background done"
  busy         each user message starts a turn that works until interrupted (or 60 s): on interrupt,
               control_response then result error_during_execution
  racing       as busy, but the interrupt is answered by a success result "finished anyway"
  slow_turn    each user message: init, then after 1.0 s result success "slow done", even after EOF
  stubborn     init, then busy forever: ignores interrupts, EOF and SIGINT (each SIGINT is logged); starts a
               ``sleep`` child (its pid in ``<prefix>.child``)
  structured   each user message: init, result "done" with structured_output {"a": 1}
  bad_struct   each user message: init, result "done" with structured_output {"a": "x"}
  cwd_parent   init reports the parent of the real cwd, then result success
  cwd_missing  init has no cwd, then result success
  auth         init, then result is_error with api_error_status 401 "Invalid API key"
  exit3        init and result success, then exit 3 at once
  noinit       print nothing and exit 2 with a stderr message
  setsid_late  first user message: init, then a python child that waits ``FAKE_SETSID_DELAY_S`` (default
               1.0) inside the session, calls setsid(), ignores SIGINT/SIGTERM/SIGHUP and sleeps 600 s
               (pid in ``<prefix>.child``; ``<prefix>.setsid`` gets "<pid> <sid>" once it left); the result
               "echo: <text>" follows once it has left the session
  daemon       first user message: init, then a python daemon started in its own session
               (start_new_session) with its stdio on /dev/null and its cwd ``FAKE_DAEMON_CWD`` (default the
               session cwd); it logs any SIGINT/SIGTERM/SIGHUP to ``<prefix>.daemon_signal`` and sleeps
               ``FAKE_DAEMON_S`` (default 600) seconds (pid in ``<prefix>.daemon``); then result "echo: <text>"
  regroup      first user message: init, then an intermediate python process that starts a grandchild in a
               NEW PROCESS GROUP (same session), writes "<pid> <pgid> <sid>" of it to ``<prefix>.regroup``
               and exits, so the grandchild is no descendant of the leader; the grandchild ignores
               SIGINT/SIGTERM/SIGHUP and sleeps 600 s; then result "echo: <text>"
"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

argv = sys.argv[1:]
scenario = os.environ.get("FAKE_SESSION_SCENARIO", "basic")
prefix = os.environ.get("FAKE_SESSION_LOG")
session = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
mode = argv[argv.index("--permission-mode") + 1]
cwd = os.getcwd()

if prefix:
    with open(prefix + ".argv.json", "w") as handle:
        json.dump({
            "argv": argv, "cwd": cwd, "pid": os.getpid(),
            "pgid": os.getpgid(0) if hasattr(os, "getpgid") else None,
            "tag": os.environ.get("LLM_KIT_SESSION_TAG"), "claudecode": "CLAUDECODE" in os.environ,
        }, handle)

lines: "queue.Queue[object]" = queue.Queue()
EOF = object()


def read_stdin():
    for raw in sys.stdin:
        if prefix:
            with open(prefix + ".stdin.jsonl", "a") as handle:
                handle.write(raw if raw.endswith("\n") else raw + "\n")
        try:
            lines.put(json.loads(raw))
        except ValueError:
            continue
    lines.put(EOF)


def emit(obj):
    line = json.dumps(obj) + "\n"
    if prefix:
        with open(prefix + ".stdout", "a") as handle:
            handle.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()


def note_sigint(*_args):
    if prefix:
        with open(prefix + ".sigint", "a") as handle:
            handle.write("sigint\n")


def init(cwd_value=cwd, include_cwd=True):
    event = {"type": "system", "subtype": "init", "session_id": session, "permissionMode": mode,
             "tools": ["Bash"], "model": "fake"}
    if include_cwd:
        event["cwd"] = cwd_value
    emit(event)


def result(text, *, subtype="success", is_error=False, **extra):
    emit({"type": "result", "subtype": subtype, "is_error": is_error, "result": text, "session_id": session,
          "num_turns": 1, "usage": {"input_tokens": 3, "output_tokens": 2}, "total_cost_usd": 0.0, **extra})


def text_of(message):
    content = message.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content if isinstance(part, dict))


SETSID_CHILD = """
import os, signal, sys, time
prefix, delay = sys.argv[1], float(sys.argv[2])
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, signal.SIG_IGN)
time.sleep(delay)
os.setsid()
with open(prefix + ".setsid", "w") as handle:
    handle.write(f"{os.getpid()} {os.getsid(0)}")
time.sleep(600)
"""

DAEMON = """
import os, signal, sys, time
prefix, seconds = sys.argv[1], float(sys.argv[2])
def note(signum, _frame):
    with open(prefix + ".daemon_signal", "a") as handle:
        handle.write(f"{signum}\\n")
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, note)
with open(prefix + ".daemon", "w") as handle:
    handle.write(str(os.getpid()))
time.sleep(seconds)
"""


REGROUP = """
import os, subprocess, sys
prefix = sys.argv[1]
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import signal, time\\n"
     "for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): signal.signal(s, signal.SIG_IGN)\\n"
     "time.sleep(600)"],
    process_group=0, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(prefix + ".regroup", "w") as handle:
    handle.write(f"{grandchild.pid} {os.getpgid(grandchild.pid)} {os.getsid(grandchild.pid)}")
"""


def wait_for_file(path, timeout_s=20.0):
    end = time.monotonic() + timeout_s
    while not os.path.exists(path):
        if time.monotonic() >= end:
            raise SystemExit(f"fake: {path} never appeared")
        time.sleep(0.02)


def setsid_child():
    delay = os.environ.get("FAKE_SETSID_DELAY_S", "1.0")
    child = subprocess.Popen([sys.executable, "-c", SETSID_CHILD, prefix, delay],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(prefix + ".child", "w") as handle:
        handle.write(str(child.pid))
    wait_for_file(prefix + ".setsid")


def daemon():
    seconds = os.environ.get("FAKE_DAEMON_S", "600")
    subprocess.Popen([sys.executable, "-c", DAEMON, prefix, seconds], start_new_session=True,
                     cwd=os.environ.get("FAKE_DAEMON_CWD", cwd), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for_file(prefix + ".daemon")


def regroup():
    subprocess.run([sys.executable, "-c", REGROUP, prefix], check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def busy_turn(answer_success):
    end = time.monotonic() + 60
    while time.monotonic() < end:
        try:
            item = lines.get(timeout=0.05)
        except queue.Empty:
            continue
        if item is EOF:
            lines.put(EOF)
            time.sleep(0.2)
            result("ended at eof")
            return
        if item.get("type") == "control_request":
            emit({"type": "control_response", "response": {"subtype": "success",
                  "request_id": item.get("request_id"), "response": {"still_queued": []}}})
            if answer_success:
                result("finished anyway")
            else:
                result("[Request interrupted by user]", subtype="error_during_execution", is_error=True)
            return
    result("worked a minute")


def main():
    threading.Thread(target=read_stdin, daemon=True).start()
    if scenario == "noinit":
        sys.stderr.write("fake: cannot start\n")
        sys.exit(2)
    if scenario == "stubborn":
        signal.signal(signal.SIGINT, note_sigint)
        child = subprocess.Popen(["sleep", "600"])
        if prefix:
            with open(prefix + ".child", "w") as handle:
                handle.write(str(child.pid))
        lines.get()
        init()
        while True:
            time.sleep(1)
    turns = 0
    background_due = None
    while True:
        try:
            item = lines.get(timeout=0.05)
        except queue.Empty:
            if background_due is not None and time.monotonic() >= background_due:
                background_due = None
                init()
                time.sleep(2.0)
                result("background done")
            continue
        if item is EOF:
            sys.exit(0)
        if item.get("type") != "user":
            continue
        turns += 1
        text = text_of(item)
        if scenario == "cwd_parent":
            init(os.path.dirname(cwd))
        elif scenario == "cwd_missing":
            init(include_cwd=False)
        else:
            init()
        emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
              "session_id": session})
        if scenario in ("busy", "racing"):
            busy_turn(scenario == "racing")
        elif scenario == "slow_turn":
            time.sleep(1.0)
            result("slow done")
        elif scenario in ("structured", "bad_struct"):
            result("done", structured_output={"a": 1 if scenario == "structured" else "x"})
        elif scenario in ("setsid_late", "daemon", "regroup"):
            if turns == 1:
                {"setsid_late": setsid_child, "daemon": daemon, "regroup": regroup}[scenario]()
            result(f"echo: {text}")
        elif scenario == "auth":
            result("Invalid API key · Please run /login", subtype="success", is_error=True,
                   api_error_status=401)
        else:
            result(f"echo: {text}")
            if scenario == "exit3":
                sys.exit(3)
            if scenario == "background" and turns == 1:
                background_due = time.monotonic() + 0.6


main()
