"""The Claude and Codex paths refuse the same characters on a Windows batch launcher.

One private module (``llm_kit.adapters._cmd_shell``) owns the characters cmd.exe cannot carry through
a ``.cmd`` launcher and the check; ``claude_command`` and ``codex_command`` both call it. These tests
parametrize over that set itself, so a character added to it is covered on both paths without a new
test, and they pin that neither CLI module keeps a second copy.

Every test simulates the platform and the launcher; no process is started.
"""
from __future__ import annotations

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


def test_the_shared_set_holds_the_percent_sign_and_both_line_breaks():
    assert {"&", "|", "^", "<", ">", "%", "\r", "\n"} <= set(_cmd_shell.REFUSED_CHARACTERS)
    assert '"' not in _cmd_shell.REFUSED_CHARACTERS  # every JSON schema carries quotes


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
