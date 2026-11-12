#!/usr/bin/env python3
"""Scriptable stand-in for ``codex app-server --listen stdio://`` (stdlib only).

It speaks the NDJSON dialect measured in plan-codex.md E-K0..E-K12 and models
the behaviours the C2 client has rules for. No real Codex, no model, no quota.

Configuration (environment):
  FAKE_CODEX_LOG   path; every message received and sent is appended as one JSON
                   line {"dir": "in"|"out"|"stub", "msg": {...}}.
  FAKE_CODEX_CFG   JSON object:
    thread_overrides   dict merged into the thread/start|resume result
                       ("sandbox" is merged into the sandbox object).
    resume_error       "no_rollout" | "active_writer" | "other".
    foreign_after_start  "status": another client's turn is announced right after
                       thread/start (turn/started + thread/status active);
                       "turn_started_only": only turn/started (no status change).
    silent_methods     methods that get no response (request timeout tests).
    garbage_after_init  write a non-JSON line after the initialize response.
    ignore_eof         keep running after stdin EOF (forces the kill path).
    spawn_grandchild   spawn a long sleep in the same process group.
    user_agent         initialize userAgent override.

A turn's behaviour comes from a MODE=<name> token in the turn text (default
"reply"); see Stub.run_turn. Under "approval-like" modes the stub models E-K4:
a ``cancel`` answer interrupts the turn at once, anything else (decline or a
JSON-RPC error) lets the turn run on to ``completed`` unless a turn/interrupt
arrives meanwhile.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

CFG = json.loads(os.environ.get("FAKE_CODEX_CFG") or "{}")
LOG = os.environ.get("FAKE_CODEX_LOG")


def _kebab_to_camel(value):
    head, *rest = value.split("-")
    return head + "".join(p.capitalize() for p in rest)


class Stub:
    def __init__(self):
        self.q = queue.Queue()
        self.responses = {}      # server-request id -> client reply
        self.interrupted = set()
        self.threads = {}
        self.turn_n = 0
        self.srv_id = 0
        self.last_turn = {}
        self.out_lock = threading.Lock()
        threading.Thread(target=self._stdin_reader, daemon=True).start()

    # -- io
    def _stdin_reader(self):
        for line in sys.stdin:
            line = line.strip()
            if line:
                self.q.put(json.loads(line))
        self.q.put(None)

    def log(self, direction, msg):
        if LOG:
            with open(LOG, "a") as f:
                f.write(json.dumps({"dir": direction, "msg": msg}) + "\n")

    def send(self, obj):
        self.log("out", obj)
        with self.out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def notify(self, method, params):
        self.send({"method": method, "params": params, "emittedAtMs": int(time.time() * 1000)})

    def next_msg(self, timeout=None):
        try:
            msg = self.q.get(timeout=timeout)
        except queue.Empty:
            return "timeout"
        if msg is None:
            self.eof()
        self.log("in", msg)
        return msg

    def eof(self):
        if CFG.get("ignore_eof"):
            while True:
                time.sleep(3600)
        sys.exit(0)

    # -- main loop
    def run(self):
        if CFG.get("spawn_grandchild"):
            gc = subprocess.Popen(["sleep", "300"])
            self.log("stub", {"grandchild_pid": gc.pid})
        self.log("stub", {"pid": os.getpid(), "pgid": os.getpgid(0)})
        while True:
            msg = self.next_msg()
            self.handle(msg)

    def handle(self, msg):
        if "method" not in msg:
            self.responses[msg["id"]] = msg
            return
        method, rid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if rid is None:
            return  # notifications such as `initialized`
        if method in CFG.get("silent_methods", []):
            return
        if method == "initialize":
            self.send({"id": rid, "result": {
                "userAgent": CFG.get("user_agent", "llm-kit-relay/0.164.0 (Mac OS 26.6.2; arm64) fake"),
                "codexHome": "/tmp/fake-codex-home", "platformFamily": "unix", "platformOs": "macos"}})
            self.notify("account/updated", {"authMode": "chatgpt"})
            if CFG.get("garbage_after_init"):
                with self.out_lock:
                    sys.stdout.write("this is not json\n")
                    sys.stdout.flush()
        elif method == "thread/start":
            self.thread_start(rid, params)
        elif method == "thread/resume":
            self.thread_resume(rid, params)
        elif method == "turn/start":
            self.turn_start(rid, params)
        elif method == "turn/interrupt":
            self.interrupted.add(params["turnId"])
            self.send({"id": rid, "result": {}})
        else:
            self.send({"id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})

    # -- threads
    def thread_result(self, tid, params):
        sw = (params.get("config") or {}).get("sandbox_workspace_write") or {}
        if params["sandbox"] == "read-only":
            sandbox = {"type": "readOnly", "networkAccess": False}
        else:
            sandbox = {"type": "workspaceWrite", "writableRoots": [],
                       "networkAccess": bool(sw.get("network_access")),
                       "excludeTmpdirEnvVar": bool(sw.get("exclude_tmpdir_env_var")),
                       "excludeSlashTmp": bool(sw.get("exclude_slash_tmp"))}
        cwd = os.path.realpath(params["cwd"])
        result = {
            "thread": {"id": tid, "path": f"/fake/sessions/rollout-{tid}.jsonl", "cwd": cwd,
                       "status": {"type": "idle"}, "modelProvider": "openai", "model": "fake-model",
                       "turns": []},
            "model": "fake-model", "modelProvider": "openai", "cwd": cwd,
            "approvalPolicy": params["approvalPolicy"], "approvalsReviewer": params["approvalsReviewer"],
            "sandbox": sandbox,
        }
        over = dict(CFG.get("thread_overrides") or {})
        sandbox_over = over.pop("sandbox", None)
        result.update(over)
        if sandbox_over:
            result["sandbox"] = {**result["sandbox"], **sandbox_over}
        return result

    def thread_start(self, rid, params):
        assert params.get("developerInstructions"), "thread/start must carry developerInstructions"
        tid = str(uuid.uuid4())
        self.threads[tid] = {"rollout": False}
        self.send({"id": rid, "result": self.thread_result(tid, params)})
        self.foreign(tid)

    def thread_resume(self, rid, params):
        assert "developerInstructions" not in params, "thread/resume must not carry developerInstructions"
        tid = params["threadId"]
        err = CFG.get("resume_error")
        if err == "no_rollout":
            return self.send({"id": rid, "error": {"code": -32600, "message": f"no rollout found for thread id {tid}"}})
        if err == "active_writer":
            return self.send({"id": rid, "error": {"code": -32600, "message": f"thread {tid} already has an active writer"}})
        if err == "other":
            return self.send({"id": rid, "error": {"code": -32603, "message": "internal boom"}})
        self.send({"id": rid, "result": self.thread_result(tid, params)})
        self.foreign(tid)

    def foreign(self, tid):
        mode = CFG.get("foreign_after_start")
        if not mode:
            return
        turn = {"id": "foreign-turn", "rootTurnId": "foreign-turn", "items": [], "status": "inProgress"}
        if mode == "status":
            self.notify("thread/status/changed", {"threadId": tid, "status": {"type": "active", "activeFlags": []}})
        self.notify("turn/started", {"threadId": tid, "turn": turn})

    # -- turns
    def turn_start(self, rid, params):
        tid = params["threadId"]
        text = params["input"][0]["text"]
        assert "effort" not in params and "model" not in params, "turn/start must carry no per-turn overrides"
        m = re.search(r"MODE=([\w-]+)", text)
        mode = m.group(1) if m else "reply"
        if mode == "dup":
            turn_id = "foreign-turn"
            self.send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
            return
        self.turn_n += 1
        turn_id = f"turn-{self.turn_n}"
        self.send({"id": rid, "result": {"turn": {"id": turn_id, "rootTurnId": turn_id, "items": [],
                                                  "status": "inProgress", "error": None}}})
        self.run_turn(tid, turn_id, mode, text)

    def turn_obj(self, turn_id, status, final=None, error=None, commentary=None):
        items = []
        if commentary:
            items.append({"type": "agentMessage", "id": f"c-{turn_id}", "text": commentary, "phase": "commentary"})
        if final is not None:
            items.append({"type": "agentMessage", "id": f"f-{turn_id}", "text": final, "phase": "final_answer"})
        return {"id": turn_id, "rootTurnId": turn_id, "items": items, "itemsView": "summary",
                "status": status, "error": error}

    def finish(self, tid, turn_id, status, final=None, error=None, commentary=None, thread_status="idle"):
        self.notify("thread/status/changed", {"threadId": tid, "status": {"type": thread_status}})
        self.notify("turn/completed", {"threadId": tid, "turn": self.turn_obj(
            turn_id, status, final, error, commentary)})
        self.last_turn[tid] = turn_id

    def pump(self, until, timeout):
        """Handle client messages until until() is true or timeout elapses."""
        end = time.time() + timeout
        while not until():
            left = end - time.time()
            if left <= 0:
                return False
            msg = self.next_msg(timeout=left)
            if msg == "timeout":
                return False
            self.handle(msg)
        return True

    def start_events(self, tid, turn_id, text):
        if "HOOK=1" in text:
            self.notify("hook/started", {"threadId": tid, "turnId": turn_id, "run": {"id": "h1"}})
        self.notify("thread/status/changed", {"threadId": tid, "status": {"type": "active", "activeFlags": []}})
        self.notify("turn/started", {"threadId": tid, "turn": {
            "id": turn_id, "rootTurnId": turn_id, "items": [], "status": "inProgress"}})
        item = {"type": "userMessage", "id": f"u-{turn_id}", "content": [{"type": "text", "text": text}]}
        self.notify("item/started", {"item": item, "threadId": tid, "turnId": turn_id})
        self.notify("item/completed", {"item": item, "threadId": tid, "turnId": turn_id})

    def finish_ok(self, tid, turn_id, final):
        msg = {"type": "agentMessage", "id": f"f-{turn_id}", "text": final, "phase": "final_answer"}
        self.notify("item/started", {"item": {**msg, "text": ""}, "threadId": tid, "turnId": turn_id})
        self.notify("item/completed", {"item": msg, "threadId": tid, "turnId": turn_id})
        self.finish(tid, turn_id, "completed", final=final, commentary="thinking")

    def server_request(self, method, params):
        rid = self.srv_id
        self.srv_id += 1
        self.send({"method": method, "id": rid, "params": params})
        return rid

    def run_turn(self, tid, turn_id, mode, text):
        self.start_events(tid, turn_id, text)
        if mode == "reply":
            return self.finish_ok(tid, turn_id, f"answer-{self.turn_n}")
        if mode == "empty":
            return self.finish_ok(tid, turn_id, "")
        if mode == "nofinal":
            return self.finish(tid, turn_id, "completed", commentary="only commentary")
        if mode == "fail":
            self.notify("error", {"error": {"message": "model exploded", "codexErrorInfo": "other"},
                                  "willRetry": False, "threadId": tid, "turnId": turn_id})
            return self.finish(tid, turn_id, "failed", error={"message": "model exploded", "codexErrorInfo": "other"},
                               thread_status="systemError")
        if mode == "stale":
            old = self.last_turn.get(tid, "turn-old")
            self.notify("item/completed", {"item": {"type": "agentMessage", "id": "f-old", "text": "OLD-ANSWER",
                                                    "phase": "final_answer"}, "threadId": tid, "turnId": old})
            self.notify("turn/completed", {"threadId": tid, "turn": self.turn_obj(old, "completed", "OLD-ANSWER")})
            return self.finish_ok(tid, turn_id, f"answer-{self.turn_n}")
        if mode == "exit":
            sys.stderr.write("fatal: boom\n")
            sys.stderr.flush()
            os._exit(3)
        if mode == "hang":
            self.pump(lambda: turn_id in self.interrupted, 30)
            return self.finish(tid, turn_id, "interrupted")
        if mode == "srvreq":
            method = re.search(r"REQ=(\S+)", text).group(1)
            rid = self.server_request(method, {"threadId": tid, "turnId": turn_id})
            self.pump(lambda: rid in self.responses, 5)
            return self.finish_ok(tid, turn_id, f"answer-{self.turn_n}")
        if mode.startswith("appr-"):
            return self.approval_turn(tid, turn_id, mode)
        raise SystemExit(f"unknown mode {mode}")

    APPROVALS = {
        "appr-cmd": ("item/commandExecution/requestApproval",
                     {"kind": "command", "command": "echo x > f", "reason": "write",
                      "availableDecisions": ["accept", "cancel"]}),
        "appr-cmd-nocancel": ("item/commandExecution/requestApproval",
                              {"kind": "command", "command": "echo x > f", "availableDecisions": ["accept"]}),
        "appr-file": ("item/fileChange/requestApproval", {"reason": "patch"}),
        "appr-elicit": ("mcpServer/elicitation/request", {"serverName": "s"}),
        "appr-perm": ("item/permissions/requestApproval", {"permissions": {}, "reason": "net"}),
        "appr-input": ("item/tool/requestUserInput", {"questions": [], "itemId": "i", "isBlocking": True}),
        "appr-exec": ("execCommandApproval", {"callId": "c", "command": ["echo"], "cwd": "/", "parsedCmd": []}),
        "appr-patch": ("applyPatchApproval", {"callId": "c", "fileChanges": {}}),
    }

    def approval_turn(self, tid, turn_id, mode):
        method, params = self.APPROVALS[mode]
        params = dict(params)
        if method in ("execCommandApproval", "applyPatchApproval"):
            params["conversationId"] = tid  # legacy shapes carry no threadId/turnId
        else:
            params.update({"threadId": tid, "turnId": turn_id, "itemId": "item-1"})
        rid = self.server_request(method, params)
        self.pump(lambda: rid in self.responses or turn_id in self.interrupted, 5)
        reply = self.responses.get(rid, {})
        result = reply.get("result") or {}
        if result.get("decision") == "cancel" or result.get("action") == "cancel":
            return self.finish(tid, turn_id, "interrupted")  # E-K4: cancel interrupts at once
        # decline / error: the turn runs on unless an interrupt arrives meanwhile
        self.pump(lambda: turn_id in self.interrupted, 0.5)
        if turn_id in self.interrupted:
            return self.finish(tid, turn_id, "interrupted")
        self.log("stub", {"turn_continued_after_approval": turn_id})
        return self.finish_ok(tid, turn_id, "CONTINUED-AFTER-APPROVAL")


if __name__ == "__main__":
    Stub().run()
