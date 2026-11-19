"""A Claude launcher that is a Windows batch shim (``claude.cmd``) never meets cmd.exe with unsafe argv.

CreateProcess runs a ``.cmd`` through cmd.exe, which re-parses the command line: ``&``, ``|``, ``^``,
``<`` and ``>`` become command syntax, and a line break ends the command line, so the rest of the
argument is dropped without a word. The three spawn paths -- the completion backend, the agent session
and the ``--bg`` driver -- therefore (1) hand the system prompt to the CLI in a temp file
(``--system-prompt-file`` / ``--append-system-prompt-file``) and (2) refuse any other argv element that
cmd.exe would re-parse. A real executable and every non-Windows platform keep the argv unchanged.

Every test simulates the platform and the launcher; no process is started.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_kit.adapters import claude_command, claude_session
from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.claude_command import (
    batch_shim_spawn_argv,
    check_batch_shim_argv,
    is_batch_shim,
)
from llm_kit.adapters.claude_session import ClaudeAgentSession
from llm_kit.completion.contract_types import POLICY_VALIDATED_RESULT, OutputContract
from llm_kit.completion.session_types import (
    SessionBounds,
    SessionPaths,
    SessionRecord,
    SessionRequest,
)
from llm_kit.completion.types import BackendOptions
from llm_kit.jobs.session import FileSessionChannel, create_attempt_dir, write_session_record
from llm_kit.pipeline.execution.drivers.claude_bg import ClaudeCli

SHIM = r"C:\Users\x\AppData\Roaming\npm\claude.cmd"
EXE = r"C:\Users\x\.local\bin\claude.exe"

#: Ordinary prompt text: multi-line, quotes, and four of the five metacharacters.
PROMPT = 'You are a reviewer.\nSay "ok" when R&D > 3 or a | b.\r\nEnd ^ here.\n'

SESSION_ID = "0b6f2c1e-3d4a-4b5c-8d9e-0f1a2b3c4d5e"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(claude_command, "_on_windows", lambda: True)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(claude_command, "_on_windows", lambda: False)


# -- the predicate ---------------------------------------------------------


@pytest.mark.parametrize("name", ["claude.cmd", "claude.CMD", "CLAUDE.Bat", SHIM])
def test_a_cmd_or_bat_launcher_is_a_batch_shim_on_windows(windows, name):
    assert is_batch_shim(name) is True


@pytest.mark.parametrize("name", ["claude", "claude.exe", EXE, "claude.cmd.exe"])
def test_an_executable_is_not_a_batch_shim(windows, name):
    assert is_batch_shim(name) is False


def test_nothing_is_a_batch_shim_off_windows(posix):
    assert is_batch_shim(SHIM) is False


# -- the check -------------------------------------------------------------


@pytest.mark.parametrize("char", ["&", "|", "^", "<", ">", "%"])
def test_the_check_refuses_each_cmd_metacharacter_in_any_element(windows, char):
    with pytest.raises(ValueError) as caught:
        check_batch_shim_argv([SHIM, "-p", "--model", "a%sb" % char])
    assert repr(char) in str(caught.value)
    assert "cmd.exe" in str(caught.value)


@pytest.mark.parametrize("breaker", ["\n", "\r", "one\ntwo"])
def test_the_check_refuses_a_line_break_that_cmd_would_cut_at(windows, breaker):
    with pytest.raises(ValueError, match="line break"):
        check_batch_shim_argv([SHIM, "--json-schema", "a" + breaker + "b"])


def test_the_check_refuses_the_launcher_path_itself(windows):
    with pytest.raises(ValueError):
        check_batch_shim_argv([r"C:\R&D\npm\claude.cmd", "-p"])


def test_the_check_passes_quotes_spaces_and_a_plain_argv(windows):
    check_batch_shim_argv([SHIM, "-p", "--json-schema", '{"type":"object"}', "--model", "a b"])


def test_the_error_shows_a_bounded_excerpt_not_the_whole_prompt(windows):
    with pytest.raises(ValueError) as caught:
        check_batch_shim_argv([SHIM, "x" * 5000 + "&"])
    assert len(str(caught.value)) < 1500


def test_a_real_executable_is_not_checked(windows):
    check_batch_shim_argv([EXE, "-p", "a&b|c<d>e^f", "x\ny"])


def test_no_argv_is_checked_off_windows(posix):
    check_batch_shim_argv([SHIM, "-p", "a&b|c<d>e^f", "x\ny"])


# -- the spawn argv: system prompt by file ---------------------------------


def test_a_shim_gets_each_system_prompt_as_a_temp_file(windows):
    argv = [SHIM, "-p", "--model", "m", "--system-prompt", PROMPT, "--append-system-prompt", "tail & more", "--effort", "low"]
    seen = {}
    with batch_shim_spawn_argv(argv) as spawn:
        assert "--system-prompt" not in spawn and "--append-system-prompt" not in spawn
        flag = spawn.index("--system-prompt-file")
        aflag = spawn.index("--append-system-prompt-file")
        seen["body"] = Path(spawn[flag + 1]).read_bytes()
        seen["abody"] = Path(spawn[aflag + 1]).read_bytes()
        seen["paths"] = [Path(spawn[flag + 1]), Path(spawn[aflag + 1])]
        assert spawn[:4] == [SHIM, "-p", "--model", "m"] and spawn[-2:] == ["--effort", "low"]
    assert seen["body"] == PROMPT.encode("utf-8")
    assert seen["abody"] == b"tail & more"
    assert not any(path.exists() for path in seen["paths"])
    assert argv[argv.index("--system-prompt") + 1] == PROMPT  # the input is not mutated


def test_a_system_prompt_with_a_percent_sign_travels_by_file_not_refused(windows):
    prompt = "Discount is 100% of %PATH% and %1.\n"
    with batch_shim_spawn_argv([SHIM, "-p", "--system-prompt", prompt, "--append-system-prompt", "50%"]) as spawn:
        assert prompt not in spawn and not any("%" in part for part in spawn)
        body = Path(spawn[spawn.index("--system-prompt-file") + 1]).read_bytes()
        abody = Path(spawn[spawn.index("--append-system-prompt-file") + 1]).read_bytes()
    assert body == prompt.encode("utf-8") and abody == b"50%"


def test_a_percent_sign_in_another_element_is_refused_while_the_prompt_goes_by_file(windows):
    with pytest.raises(ValueError, match="'%'"):
        with batch_shim_spawn_argv([SHIM, "--system-prompt", "100%", "--mcp-config", "http://h/a%20b"]):
            raise AssertionError("the spawn must not be reached")


def test_the_temp_file_is_removed_when_the_spawn_raises(windows):
    holder = {}
    with pytest.raises(RuntimeError, match="boom"):
        with batch_shim_spawn_argv([SHIM, "--system-prompt", "p"]) as spawn:
            holder["path"] = Path(spawn[spawn.index("--system-prompt-file") + 1])
            assert holder["path"].exists()
            raise RuntimeError("boom")
    assert not holder["path"].exists()


def test_a_non_ascii_prompt_survives_the_file(windows):
    with batch_shim_spawn_argv([SHIM, "--system-prompt", "caf\u00e9 \u2014 ok"]) as spawn:
        body = Path(spawn[spawn.index("--system-prompt-file") + 1]).read_bytes()
    assert body.decode("utf-8") == "caf\u00e9 \u2014 ok"


def test_other_elements_are_still_refused_while_the_prompt_goes_by_file(windows):
    with pytest.raises(ValueError, match="'&'"):
        with batch_shim_spawn_argv([SHIM, "--system-prompt", "fine", "--json-schema", '{"a":"x&y"}']):
            raise AssertionError("the spawn must not be reached")


def test_a_refused_argv_leaves_no_temp_file_behind(windows, monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with pytest.raises(ValueError):
        with batch_shim_spawn_argv([SHIM, "--system-prompt", "fine", "--model", "a&b"]):
            raise AssertionError("the spawn must not be reached")
    assert list(tmp_path.iterdir()) == []


def test_the_terminator_ends_the_flag_scan(windows):
    argv = [SHIM, "--bg", "--", "--system-prompt"]
    with batch_shim_spawn_argv(argv, prompt_files=False) as spawn:
        assert spawn == argv


def test_without_file_routing_a_system_prompt_is_checked_like_any_element(windows):
    with pytest.raises(ValueError, match="'&'"):
        with batch_shim_spawn_argv([SHIM, "--bg", "--system-prompt", "a&b", "--", "go"], prompt_files=False):
            raise AssertionError("the spawn must not be reached")


def test_a_real_executable_spawn_argv_is_the_same_list(windows):
    argv = [EXE, "-p", "--system-prompt", PROMPT, "--json-schema", '{"a":"x&y"}']
    with batch_shim_spawn_argv(argv) as spawn:
        assert spawn is argv


def test_off_windows_spawn_argv_is_the_same_list(posix):
    argv = [SHIM, "-p", "--system-prompt", PROMPT, "--model", "a&b"]
    with batch_shim_spawn_argv(argv) as spawn:
        assert spawn is argv


# -- path 1: the completion backend ----------------------------------------


class _Runner:
    def __init__(self, envelope: dict | None = None) -> None:
        self.calls: list = []
        self.files: list = []
        self.envelope = envelope or {"result": "ok", "is_error": False, "usage": {}}

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append((list(cmd), request))
        for flag in ("--system-prompt-file", "--append-system-prompt-file"):
            if flag in cmd:
                self.files.append((flag, Path(cmd[cmd.index(flag) + 1]).read_bytes()))
        return json.dumps(self.envelope), "", 0


def _complete(executable, system="sys", user="usr", model="m", options=None):
    runner = _Runner()
    ClaudeCliBackend(runner=runner, executable=executable).complete(system, user, model=model, options=options)
    return runner


def test_backend_on_a_shim_sends_a_multiline_prompt_by_file_and_the_user_text_on_stdin(windows):
    runner = _complete(SHIM, system=PROMPT, user="user text with & and | and\nlines")
    argv, stdin = runner.calls[0]
    assert PROMPT not in argv and "--system-prompt" not in argv
    assert runner.files == [("--system-prompt-file", PROMPT.encode("utf-8"))]
    assert stdin == "user text with & and | and\nlines"
    assert argv[:3] == [SHIM, "-p", "--model"]
    assert not Path(argv[argv.index("--system-prompt-file") + 1]).exists()


def test_backend_on_a_shim_uses_the_append_file_flag_for_append_mode(windows):
    runner = _complete(SHIM, system=PROMPT, options=BackendOptions(system_prompt_mode="append"))
    assert runner.files == [("--append-system-prompt-file", PROMPT.encode("utf-8"))]


def test_backend_on_a_shim_refuses_a_metacharacter_in_the_model(windows):
    runner = _Runner()
    with pytest.raises(ValueError, match="cmd.exe"):
        ClaudeCliBackend(runner=runner, executable=SHIM).complete("sys", "usr", model="m&calc")
    assert runner.calls == []


def test_backend_on_a_shim_refuses_a_metacharacter_in_the_json_schema(windows):
    schema = {"type": "object", "properties": {"a": {"enum": ["x&y"]}}, "required": ["a"]}
    contract = OutputContract(id="demo", policy=POLICY_VALIDATED_RESULT, schema=schema)
    runner = _Runner()
    with pytest.raises(ValueError, match="cmd.exe"):
        ClaudeCliBackend(runner=runner, executable=SHIM).complete(
            "sys", "usr", model="m", options=BackendOptions(output_contract=contract)
        )
    assert runner.calls == []


def test_backend_on_a_shim_passes_a_plain_json_schema_inline(windows):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    contract = OutputContract(id="demo", policy=POLICY_VALIDATED_RESULT, schema=schema)
    runner = _Runner({"result": "", "structured_output": {"a": 1}, "is_error": False, "usage": {}})
    ClaudeCliBackend(runner=runner, executable=SHIM).complete(
        "sys", "usr", model="m", options=BackendOptions(output_contract=contract)
    )
    argv = runner.calls[0][0]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_backend_with_an_exe_keeps_the_argv_byte_for_byte(windows):
    runner = _complete(EXE, system=PROMPT, model="m&x")
    argv = runner.calls[0][0]
    assert argv[argv.index("--system-prompt") + 1] == PROMPT
    assert "--system-prompt-file" not in argv and runner.files == []
    assert argv[argv.index("--model") + 1] == "m&x"


def test_backend_off_windows_keeps_the_argv_for_a_cmd_named_executable(posix):
    runner = _complete(SHIM, system=PROMPT, model="m&x")
    argv = runner.calls[0][0]
    assert argv[argv.index("--system-prompt") + 1] == PROMPT and runner.files == []


# -- path 2: the agent session ---------------------------------------------


class _Spawned(RuntimeError):
    """Raised by the Popen stub so no process starts."""


def _session_request(tmp_path: Path, system: str = "SYSTEM", model: str = "haiku", **options) -> SessionRequest:
    paths = SessionPaths.for_attempt(tmp_path / "sessions", "job-1", 1)
    create_attempt_dir(paths)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    tag = "lks_" + "0" * 32
    write_session_record(paths, SessionRecord(
        run_id="run-1", job_id="job-1", attempt_no=1, session_id=SESSION_ID, resumed=False, cwd=cwd,
        process_tag=tag, created_at="2026-11-14T00:00:00Z",
    ))
    opts = BackendOptions(timeout_s=30.0, cwd=cwd, system_prompt_mode="append", **options)
    return SessionRequest(
        system=system, user="hello", model=model, options=opts, cwd=cwd, session_id=SESSION_ID,
        resume=False, process_tag=tag, paths=paths,
        bounds=SessionBounds(quiescence_s=0.5, result_wait_s=3.0, eof_wait_s=3.0, sigint_wait_s=2.0),
        channel=FileSessionChannel(paths),
    )


@pytest.fixture
def popen(monkeypatch):
    """Record the argv Popen is given and the temp-file bodies at that moment; start nothing."""
    seen: dict = {"calls": []}

    def fake(argv, **kwargs):
        record = {"argv": list(argv), "files": {}}
        for flag in ("--system-prompt-file", "--append-system-prompt-file"):
            if flag in argv:
                path = Path(argv[argv.index(flag) + 1])
                record["files"][flag] = (path, path.read_bytes())
        seen["calls"].append(record)
        raise _Spawned

    monkeypatch.setattr(claude_session.subprocess, "Popen", fake)
    return seen


def test_session_on_a_shim_sends_the_system_prompt_by_file(windows, popen, tmp_path):
    request = _session_request(tmp_path, system=PROMPT)
    with pytest.raises(_Spawned):
        ClaudeAgentSession(executable=SHIM).run(request)
    record = popen["calls"][0]
    assert PROMPT not in record["argv"] and "--append-system-prompt" not in record["argv"]
    path, body = record["files"]["--append-system-prompt-file"]
    assert body == PROMPT.encode("utf-8")
    assert not path.exists()  # removed once the run is over


def test_session_on_a_shim_refuses_a_metacharacter_in_the_model_before_any_spawn(windows, popen, tmp_path):
    request = _session_request(tmp_path, model="haiku&calc")
    with pytest.raises(ValueError, match="cmd.exe"):
        ClaudeAgentSession(executable=SHIM).run(request)
    assert popen["calls"] == []
    assert not request.paths.events.exists() or request.paths.events.stat().st_size == 0


def test_session_on_a_shim_refuses_a_metacharacter_in_the_deny_list(windows, popen, tmp_path):
    # A grant can no longer carry a metacharacter (a scoped rule is refused
    # earlier); the deny list still reaches argv as --disallowedTools.
    request = _session_request(tmp_path, disallowed_tools="Bash(git status | cat)")
    with pytest.raises(ValueError, match="'\\|'"):
        ClaudeAgentSession(executable=SHIM).run(request)
    assert popen["calls"] == []


def test_session_build_argv_on_a_shim_refuses_what_run_would_refuse(windows, tmp_path):
    request = _session_request(tmp_path, model="haiku&calc")
    with pytest.raises(ValueError, match="cmd.exe"):
        ClaudeAgentSession(executable=SHIM).build_argv(request)


def test_session_build_argv_on_a_shim_still_shows_the_inline_prompt_and_makes_no_file(windows, tmp_path):
    request = _session_request(tmp_path, system=PROMPT)
    argv = ClaudeAgentSession(executable=SHIM).build_argv(request)
    assert argv[argv.index("--append-system-prompt") + 1] == PROMPT


def test_session_with_an_exe_spawns_the_unchanged_argv(windows, popen, tmp_path):
    request = _session_request(tmp_path, system=PROMPT, model="haiku&x")
    with pytest.raises(_Spawned):
        ClaudeAgentSession(executable=EXE).run(request)
    argv = popen["calls"][0]["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == PROMPT
    assert popen["calls"][0]["files"] == {}
    assert argv[argv.index("--model") + 1] == "haiku&x"


def test_session_off_windows_spawns_the_unchanged_argv(posix, popen, tmp_path):
    request = _session_request(tmp_path, system=PROMPT)
    with pytest.raises(_Spawned):
        ClaudeAgentSession(executable=SHIM).run(request)
    argv = popen["calls"][0]["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == PROMPT


# -- path 3: the --bg driver -----------------------------------------------


class _BgRunner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return "", "", 0


BG_PROMPT = "Run id: r1\nUnit id: u1\n\nSubstitute <FENCING_TOKEN> with 7.\n"


def test_bg_launch_on_a_shim_refuses_a_prompt_cmd_would_cut_or_reparse(windows):
    runner = _BgRunner()
    with pytest.raises(ValueError, match="cmd.exe"):
        ClaudeCli(executable=SHIM, runner=runner).launch_bg(BG_PROMPT)
    assert runner.calls == []


def test_bg_launch_on_a_shim_refuses_a_single_line_metacharacter_prompt(windows):
    runner = _BgRunner()
    with pytest.raises(ValueError, match="'&'"):
        ClaudeCli(executable=SHIM, runner=runner).launch_bg('do "x" & more')
    assert runner.calls == []


def test_bg_launch_on_a_shim_refuses_a_metacharacter_in_the_extra_args(windows):
    runner = _BgRunner()
    with pytest.raises(ValueError, match="'\\|'"):
        ClaudeCli(executable=SHIM, runner=runner).launch_bg("go", extra_args=("--agent", "a|b"))
    assert runner.calls == []


def test_bg_launch_on_a_shim_passes_a_plain_prompt_unchanged(windows):
    runner = _BgRunner()
    ClaudeCli(executable=SHIM, runner=runner).launch_bg("go now", extra_args=("--agent", "worker"))
    assert runner.calls == [[SHIM, "--bg", "--agent", "worker", "--", "go now"]]


def test_bg_launch_never_moves_a_system_prompt_to_a_file(windows):
    runner = _BgRunner()
    with pytest.raises(ValueError, match="'&'"):
        ClaudeCli(executable=SHIM, runner=runner).launch_bg("go", extra_args=("--append-system-prompt", "a&b"))
    assert runner.calls == []


def test_bg_other_verbs_on_a_shim_are_checked_too(windows):
    runner = _BgRunner()
    with pytest.raises(ValueError, match="cmd.exe"):
        ClaudeCli(executable=r"C:\a&b\claude.cmd", runner=runner).agents_json()
    assert runner.calls == []


def test_bg_launch_with_an_exe_keeps_the_prompt_byte_for_byte(windows):
    runner = _BgRunner()
    ClaudeCli(executable=EXE, runner=runner).launch_bg(BG_PROMPT)
    assert runner.calls == [[EXE, "--bg", "--", BG_PROMPT]]


def test_bg_launch_off_windows_keeps_the_prompt_for_a_cmd_named_executable(posix):
    runner = _BgRunner()
    ClaudeCli(executable=SHIM, runner=runner).launch_bg(BG_PROMPT)
    assert runner.calls == [[SHIM, "--bg", "--", BG_PROMPT]]


def test_the_module_uses_os_name_for_the_default_platform_test():
    assert claude_command._on_windows() is (os.name == "nt")
