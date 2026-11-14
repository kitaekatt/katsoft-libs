"""Run an agent-session job in branch mode, end to end, with no client app and no real Claude.

Run it in a plain Python environment; it needs no AI harness, plugin, client app,
network or account, only `git` on PATH and the `local` and `config` extras:

    python -m venv .venv
    .venv/bin/python -m pip install "katsoft-llm-kit[local,config]"
    .venv/bin/python agent_session_branch.py

What a client does, in the order this script does it:

1. A repository with a remote-tracking base ref. Branch mode builds a unit's
   branch from `workspace.base_ref`, which must be a remote-tracking branch
   such as `origin/dev` (or a full commit id), never a local branch or the
   checkout's HEAD. The script makes a bare "remote", clones it, and so has
   `origin/dev`.
2. A jobs file. `options.session: agent` runs the job as a persisted
   `claude -p` stream-json session instead of a one-shot completion.
   `workspace: {mode: branch, unit, root, base_ref}` runs every attempt in the
   worktree of one unit of work, on the unit's own branch (default
   `agent/<unit>`), at the fixed path `<root>/<repo>/<unit>`.
3. `llm-kit jobs run <file> --session-root <dir> --supervisor-budget <s>`.
   Both session flags are required when any job declares `session: agent`.
   The budget is the supervisor's run-wide deadline in seconds; the command
   refuses a budget below the run's worst case (attempt timeouts, shutdown
   allowances and finalization), so give it generous room.
   The session root holds one directory per attempt: `events.jsonl` (the
   verbatim stream), the mailbox, and the session records.
4. A mailbox message. While the session runs, any process appends a user
   message to the attempt's mailbox with `llm_kit.jobs.session.append_message`.
   The session delivers it as the next turn.
5. The end-of-run commit. When the run ends, whatever the agent left in the
   worktree is committed to the unit branch as `WIP: <unit> run <n>`; the unit
   is then parked in the unit registry (`<root>/units.sqlite3`). Nothing is
   pushed or merged: the unit branch waits for the client to merge it.

The real `claude` is replaced by a stand-in. The documented executable override
`LLM_KIT_CLAUDE_EXECUTABLE` names the program to run instead of `claude`. The
stand-in is this same file started with `--as-fake-claude` (see `fake_claude`
at the end); it speaks the stream-json protocol, leaves a file in its working
directory for each message, and makes no model call. With a real Claude CLI on
PATH and a real harness entry, steps 1 to 5 are unchanged.

The script exits non-zero with a message when a check fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

JOB_ID = "write-notes"
UNIT = "item-1"
RUN_ID = "demo-run"
FIRST_PROMPT = "Write the release notes."
FOLLOW_UP = "Also list the open questions."
ENDPOINT = "example-claude"


def git(*args: str, cwd: Path, env: dict[str, str]) -> str:
    """Run Git in `cwd` and return its stripped stdout; a failure stops the script."""
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


def make_repository(base: Path, env: dict[str, str]) -> Path:
    """A bare "remote" with one commit on `dev`, and a clone of it that has `origin/dev`."""
    remote, seed, clone = base / "remote.git", base / "seed", base / "project"
    git("init", "-q", "--bare", "-b", "dev", str(remote), cwd=base, env=env)
    git("init", "-q", "-b", "dev", str(seed), cwd=base, env=env)
    git("config", "user.name", "Example", cwd=seed, env=env)
    git("config", "user.email", "example@example.invalid", cwd=seed, env=env)
    (seed / "README.txt").write_text("seed\n", encoding="ascii")
    git("add", "-A", cwd=seed, env=env)
    git("commit", "-q", "-m", "seed", cwd=seed, env=env)
    git("remote", "add", "origin", str(remote), cwd=seed, env=env)
    git("push", "-q", "origin", "dev", cwd=seed, env=env)
    git("clone", "-q", str(remote), str(clone), cwd=base, env=env)
    # The end-of-run commit is made with this repository's identity.
    git("config", "user.name", "Example", cwd=clone, env=env)
    git("config", "user.email", "example@example.invalid", cwd=clone, env=env)
    git("config", "commit.gpgsign", "false", cwd=clone, env=env)
    return clone


def write_inputs(base: Path, project: Path, units: Path) -> tuple[Path, Path, Path]:
    """The jobs file, the model-endpoint registry and the stand-in `claude` launcher."""
    # A harness entry: the job's `models` names it, and it drives the Claude CLI.
    registry = base / "model-endpoints.yaml"
    registry.write_text(f"version: 1\nmodels:\n  {ENDPOINT}:\n    harness: claude\n    model: haiku\n",
                        encoding="ascii")

    # The launcher runs this file as the stand-in. The override needs one program path.
    launcher = base / "fake-claude"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -I "{Path(__file__).resolve()}" --as-fake-claude "$@"\n',
        encoding="ascii",
    )
    launcher.chmod(0o755)

    # JSON scalars are valid YAML, so json.dumps quotes every value safely.
    contract = [sys.executable, "-I", "-c", "import sys; sys.exit(0 if sys.stdin.read().startswith('done') else 1)"]
    jobs_file = base / "jobs.yaml"
    jobs_file.write_text(
        "jobs:\n"
        f"  - id: {JOB_ID}\n"
        f"    models: [{ENDPOINT}]\n"
        f"    prompt: {{user: {json.dumps(FIRST_PROMPT)}}}\n"
        f"    directory: {json.dumps(str(project))}\n"
        "    options:\n"
        "      session: agent\n"
        "      session_quiescence_s: 2\n"  # the attempt ends 2 s after the last turn's result
        "    workspace:\n"
        "      mode: branch\n"
        f"      unit: {UNIT}\n"
        f"      root: {json.dumps(str(units))}\n"
        "      base_ref: origin/dev\n"
        "    contract:\n"
        f"      command: {json.dumps(contract)}\n"
        f"      directory: {json.dumps(str(project))}\n",
        encoding="ascii",
    )
    return jobs_file, registry, launcher


def main() -> int:
    from llm_kit.completion.session_types import SessionPaths
    from llm_kit.jobs.session import append_message, read_state
    from llm_kit.jobs.units import list_units

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory).resolve()
        home = base / "home"
        home.mkdir()
        empty_gitconfig = base / "gitconfig"
        empty_gitconfig.write_text("", encoding="ascii")
        # A scratch HOME and no global Git configuration: the example reads nothing of the machine's.
        env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(empty_gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}

        project = make_repository(base, env)
        units = base / "units"
        jobs_file, registry, launcher = write_inputs(base, project, units)
        session_root = base / "sessions" / RUN_ID  # one directory per run
        ledger = base / "ledger.sqlite3"

        # Step 3: the run. A real client starts the same command; here it is a child process so that this
        # script can append the mailbox message while the session is live.
        runner_env = {**env, "MODEL_ENDPOINTS_REGISTRY": str(registry), "LLM_KIT_CLAUDE_EXECUTABLE": str(launcher)}
        run = subprocess.Popen(
            [sys.executable, "-m", "llm_kit.cli", "jobs", "run", str(jobs_file), "--store", str(ledger),
             "--run-id", RUN_ID, "--session-root", str(session_root), "--supervisor-budget", "3600"],
            env=runner_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        # Step 4: the attempt directory appears when the attempt starts; append a message to its mailbox.
        paths = SessionPaths.for_attempt(session_root, JOB_ID, 1)
        deadline = time.monotonic() + 60
        while not paths.mailbox.exists():
            if run.poll() is not None or time.monotonic() > deadline:
                run.kill()
                out, err = run.communicate()
                raise SystemExit(f"the session never started: {err.strip()[-800:]}\n{out.strip()[:3000]}")
            time.sleep(0.02)
        record = append_message(paths, FOLLOW_UP)
        print(f"appended mailbox message {record.seq}: {FOLLOW_UP!r}")

        try:
            stdout, stderr = run.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            run.kill()
            raise SystemExit("the run did not finish in 120 s")
        if run.returncode != 0:
            raise SystemExit(f"jobs run exited {run.returncode}: {stderr.strip()[-800:]}\n{stdout.strip()[:3000]}")
        snapshot = json.loads(stdout.strip().splitlines()[-1])
        job = snapshot["jobs"][0]
        print(f"job {job['id']}: {job['state']}")
        if job["state"] != "accepted":
            raise SystemExit(f"the job was not accepted: {job}")
        delivery = read_state(paths).delivery(record.seq)
        print(f"mailbox message {record.seq}: {delivery.value}")
        if delivery.value != "forwarded":  # forwarded to the session's stdin; never "consumed" (stdout does not echo it)
            raise SystemExit(f"the mailbox message was {delivery.value}, not forwarded")

        # Step 5: the end-of-run commit on the unit branch, and the unit registry.
        branch = f"agent/{UNIT}"
        subjects = git("log", "--format=%s", branch, cwd=project, env=env).splitlines()
        print(f"{branch} log: {subjects}")
        if subjects != [f"WIP: {UNIT} run 1", "seed"]:
            raise SystemExit(f"unexpected unit branch log: {subjects}")
        notes = git("show", f"{branch}:notes.txt", cwd=project, env=env)
        print(f"notes.txt on {branch}: {notes.splitlines()}")
        if notes.splitlines() != [FIRST_PROMPT, FOLLOW_UP]:
            raise SystemExit(f"the agent did not see both messages: {notes!r}")
        if git("rev-parse", "dev", cwd=project, env=env) != git("rev-parse", "origin/dev", cwd=project, env=env):
            raise SystemExit("branch mode must not move the base branch")

        (unit,) = list_units(units)
        print(f"unit {unit.unit}: state {unit.state.value}, branch {unit.branch}, run count {unit.run_count}")
        if (unit.unit, unit.branch, unit.state.value, unit.run_count) != (UNIT, branch, "parked", 1):
            raise SystemExit(f"unexpected unit record: {unit}")
        if unit.last_commit_sha != git("rev-parse", branch, cwd=project, env=env):
            raise SystemExit("the unit record does not name the end-of-run commit")
        print(f"unit worktree: {unit.worktree.relative_to(base)}")
    return 0


def fake_claude(argv: list[str]) -> int:
    """The stand-in for `claude -p --input-format stream-json --output-format stream-json --verbose`.

    A session reads one stream-json user message per stdin line and prints
    stream-json events: each turn starts with a `system:init` event and ends
    with a `result` event; the process idles on stdin after a result and
    exits 0 when stdin closes. For each message this stand-in appends the
    message text to `notes.txt` in its working directory (the worktree of the
    unit), as an agent that edits a file would, and answers `done: <n>`.
    Before routing a job, llm-kit checks that the executable runs `--version`.
    """
    if "--version" in argv:
        print("2.1.325 (Claude Code)")
        return 0
    session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
    mode = argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv else "default"
    turns = 0
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("type") != "user":
            continue
        content = message["message"]["content"]
        text = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content)
        turns += 1
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "cwd": os.getcwd(),
                          "permissionMode": mode, "tools": ["Bash"], "model": "fake"}), flush=True)
        time.sleep(1.0)  # a turn takes time, so a message can arrive while it runs
        with open("notes.txt", "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": f"done: {turns}",
                          "session_id": session, "num_turns": 1, "usage": {"input_tokens": 1, "output_tokens": 1},
                          "total_cost_usd": 0.0}), flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--as-fake-claude"]:
        raise SystemExit(fake_claude(sys.argv[2:]))
    raise SystemExit(main())
