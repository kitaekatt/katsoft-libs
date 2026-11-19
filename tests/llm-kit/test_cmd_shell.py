"""The Claude and Codex paths refuse the same characters on a Windows batch launcher.

One private module (``llm_kit.adapters._cmd_shell``) owns the characters cmd.exe cannot carry through
a ``.cmd`` launcher and the check; ``claude_command`` and ``codex_command`` both call it. These tests
parametrize over that set itself, so a character added to it is covered on both paths without a new
test, and they pin that neither CLI module keeps a second copy.

Every test simulates the platform and the launcher; no process is started.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from llm_kit.adapters import _cmd_shell, claude_command, codex_command
from llm_kit.adapters.claude_command import check_batch_shim_argv
from llm_kit.adapters.codex_command import build_codex_app_server_argv, build_codex_exec_argv

CLAUDE_SHIM = r"C:\Users\x\AppData\Roaming\npm\claude.cmd"
CLAUDE_EXE = r"C:\Users\x\.local\bin\claude.exe"
CODEX_BATCH_PREFIX = ("cmd", "/c", r"C:\Users\x\AppData\Roaming\npm\codex.cmd")
CODEX_EXE_PREFIX = (r"C:\Users\x\.local\bin\codex.exe",)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(claude_command, "_on_windows", lambda: True)


def _codex_exec(tmp_path, prefix, model):
    return build_codex_exec_argv(root=tmp_path, model=model, argv_prefix=prefix)


# -- the shared set is the one every path is tested against ----------------


def test_the_shared_set_holds_the_percent_sign_the_bang_and_both_line_breaks():
    assert {"&", "|", "^", "<", ">", "%", "!", "\r", "\n"} <= set(_cmd_shell.REFUSED_CHARACTERS)


#: Characters the module docstring decides NOT to refuse, each observed to arrive unchanged through a
#: ``.cmd`` launcher (``test_each_allowed_character_arrives_unchanged_through_a_real_cmd_launcher``).
ALLOWED = ['"', "(", ")", ",", ";", "=", "\\"]


@pytest.mark.parametrize("char", ALLOWED)
def test_the_characters_the_module_allows_are_not_in_the_set(windows, char):
    assert char not in _cmd_shell.REFUSED_CHARACTERS
    assert _cmd_shell.refused_character("a%sb" % char) is None
    check_batch_shim_argv([CLAUDE_SHIM, "-p", "--model", "a%sb" % char, "--model", "x %s" % char])


@pytest.mark.parametrize("char", _cmd_shell.REFUSED_CHARACTERS)
def test_the_claude_check_refuses_every_shared_character_on_a_batch_launcher(windows, char):
    with pytest.raises(ValueError, match="cmd.exe"):
        check_batch_shim_argv([CLAUDE_SHIM, "-p", "--model", "a%sb" % char])


@pytest.mark.parametrize("char", _cmd_shell.REFUSED_CHARACTERS)
def test_the_codex_exec_builder_refuses_every_shared_character_on_a_batch_launcher(tmp_path, char):
    with pytest.raises(ValueError, match="cmd.exe"):
        _codex_exec(tmp_path, CODEX_BATCH_PREFIX, "a%sb" % char)


@pytest.mark.parametrize("char", _cmd_shell.CMD_METACHARACTERS)
def test_the_codex_app_server_builder_refuses_every_shared_metacharacter_on_a_batch_launcher(tmp_path, char):
    with pytest.raises(ValueError, match="cmd.exe"):
        build_codex_app_server_argv(root=tmp_path, model="a%sb" % char, argv_prefix=CODEX_BATCH_PREFIX)


@pytest.mark.parametrize("char", _cmd_shell.REFUSED_CHARACTERS)
def test_the_codex_check_function_refuses_every_shared_character(char):
    with pytest.raises(ValueError, match="cmd.exe"):
        codex_command._reject_cmd_metacharacters(["codex", "a%sb" % char])


def test_a_refusal_names_the_element_and_the_character(windows):
    with pytest.raises(ValueError) as caught:
        check_batch_shim_argv([CLAUDE_SHIM, "-p", "--model", "a%b"])
    message = str(caught.value)
    assert "element 3" in message and "'%'" in message


# -- one definition ---------------------------------------------------------


def test_both_clis_use_the_same_check_object():
    assert claude_command.check_cmd_argv is _cmd_shell.check_cmd_argv
    assert codex_command.check_cmd_argv is _cmd_shell.check_cmd_argv


@pytest.mark.parametrize("module", [claude_command, codex_command], ids=lambda module: module.__name__)
def test_neither_cli_module_keeps_a_second_copy_of_the_set(module):
    metacharacters = set(_cmd_shell.CMD_METACHARACTERS)
    copies = [
        name
        for name, value in vars(module).items()
        if isinstance(value, (tuple, list, frozenset, set, str))
        and not isinstance(value, bool)
        and len(value) >= 2
        and len(metacharacters & set(value)) >= 3
    ]
    assert copies == []


# -- what is not refused ----------------------------------------------------


def test_a_clean_argv_passes_on_both_paths(windows, tmp_path):
    check_batch_shim_argv([CLAUDE_SHIM, "-p", "--json-schema", '{"type":"object"}', "--model", "a b"])
    argv = _codex_exec(tmp_path, CODEX_BATCH_PREFIX, "gpt-x")
    assert argv[:3] == list(CODEX_BATCH_PREFIX) and "gpt-x" in argv


def test_a_native_launcher_is_not_checked_on_either_path(windows, tmp_path):
    dirty = "a" + "".join(_cmd_shell.REFUSED_CHARACTERS) + "b"
    check_batch_shim_argv([CLAUDE_EXE, "-p", "--model", dirty])
    argv = _codex_exec(tmp_path, CODEX_EXE_PREFIX, dirty)
    assert dirty in argv


def test_the_claude_check_does_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(claude_command, "_on_windows", lambda: False)
    check_batch_shim_argv([CLAUDE_SHIM, "-p", "--model", "a%b&c\nd"])


# -- what a real batch launcher does (Windows only; a harmless echo launcher, no CLI) ---------------

_ECHO = "import json, sys\nsys.stdout.write(json.dumps(sys.argv[1:]))\n"

#: The structure npm's cmd-shim gives ``claude.cmd`` and ``codex.cmd`` (scoop's nodejs copy is the same):
#: ``%*`` on the last line, outside any parenthesised block. The program is Python echoing its argv.
_NPM_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n"
    "CALL :find_dp0\r\n\r\nIF EXIST \"%dp0%\\node.exe\" (\r\n  SET \"_prog=%dp0%\\node.exe\"\r\n) ELSE (\r\n"
    "  SET \"_prog={python}\"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n"
    "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & \"%_prog%\"  \"%dp0%\\echo_args.py\" %*\r\n"
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs a .cmd launcher through cmd.exe")


@pytest.fixture
def echo_shim(tmp_path, monkeypatch):
    (tmp_path / "echo_args.py").write_text(_ECHO, encoding="utf-8")
    shim = tmp_path / "echo.cmd"
    shim.write_bytes(_NPM_SHIM.replace("{python}", sys.executable).encode("mbcs"))
    monkeypatch.setenv("LLM_KIT_CMD_PROBE", "EXPANDED")
    return str(shim)


def _arrives(argv: list) -> list:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


@windows_only
@pytest.mark.parametrize("value", ['a"b', 'say "hi" now', '"', "a(b)", ")", "x b(c)", "a,b;c=d", "a b,c;d=e",
                                   "a\\", "C:\\dir with space\\", 'a\\"b'])
def test_each_allowed_character_arrives_unchanged_through_a_real_cmd_launcher(echo_shim, value):
    """The decision for ``"``, ``(``, ``)``, ``,``, ``;``, ``=`` and a trailing backslash rests on this."""
    assert _cmd_shell.refused_character(value) is None
    assert _arrives([echo_shim, value]) == [value]


@windows_only
@pytest.mark.parametrize("value", ["%LLM_KIT_CMD_PROBE%", "a^b"])
def test_a_refused_character_does_arrive_changed_through_a_real_cmd_launcher(echo_shim, value):
    """The probe is sound: it sees cmd.exe change ``%VAR%`` and ``^`` (both refused)."""
    assert _arrives([echo_shim, value]) != [value]


@windows_only
def test_delayed_expansion_is_the_machines_choice_so_the_bang_is_refused(echo_shim, tmp_path):
    """CreateProcess starts a ``.cmd`` as ``cmd.exe /c`` with no ``/v:off``, so the registry's
    ``DelayedExpansion`` decides; with it on (``/v:on`` here), ``!VAR!`` is replaced by the variable."""
    line = tmp_path / "cmdline.cmd"
    line.write_bytes(b"@echo %CMDCMDLINE%\r\n")
    started = subprocess.run([str(line)], capture_output=True, text=True, timeout=60, check=True).stdout.lower()
    assert " /c " in started and "/v:" not in started and " /d " not in started
    assert _arrives(["cmd.exe", "/v:on", "/c", echo_shim, "a!LLM_KIT_CMD_PROBE!b"]) == ["aEXPANDEDb"]
    assert _cmd_shell.refused_character("a!LLM_KIT_CMD_PROBE!b") == "!"
