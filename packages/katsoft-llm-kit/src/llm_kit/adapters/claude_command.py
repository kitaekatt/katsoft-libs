"""Claude CLI executable selection -- the single source of truth.

``LLM_KIT_CLAUDE_EXECUTABLE`` names the Claude CLI to run instead of ``claude``
on PATH, with the same contract as ``LLM_KIT_CODEX_EXECUTABLE``
(:mod:`llm_kit.adapters.codex_command`): a bare command name (looked up on PATH)
or an ABSOLUTE path to an executable file. Every place that starts a ``claude``
process resolves through this module -- the completion backend, the auth probe,
the reachability probe and the background-session driver -- so a verdict and a
dispatch always name the same binary.

Unset, the behaviour is unchanged: ``claude`` on PATH. Set to something that
cannot be run, it raises :class:`ClaudeExecutableError` naming the variable
instead of falling back to PATH: a user who named a specific build must never
be served a different one in silence. The module is standard library only (plus
:func:`llm_kit.adapters.codex_command.resolve_cli`), so a plain
``pip install katsoft-llm-kit`` user reaches it without any AI harness.

Batch-shim launchers. On Windows an npm install puts ``claude.cmd`` on PATH, and
CreateProcess runs a ``.cmd`` through cmd.exe, which re-parses the command line.
:func:`batch_shim_spawn_argv` is the one function the three spawn paths (the
completion backend, the agent session and the ``--bg`` driver) pass their argv
through. For a real executable, or on any other platform, it changes nothing.
For a batch shim it moves each system prompt into a temp file the CLI reads
(``--system-prompt-file``) and refuses any remaining argv element cmd.exe would
re-parse. The character set is the Codex builder's guard
(``codex_command._reject_cmd_metacharacters``) plus line breaks; the check is
duplicated here rather than imported from that private name.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import shutil
import tempfile
from typing import Iterator, List, Mapping, Optional, Sequence, Tuple

from llm_kit.adapters.codex_command import resolve_cli

CLAUDE_EXECUTABLE = "claude"

#: Names the Claude CLI to run instead of ``claude`` on PATH: a bare command
#: name (looked up on PATH) or an ABSOLUTE path to an executable file. Set it
#: when more than one Claude build is installed and the one on PATH is not the
#: one to use.
CLAUDE_EXECUTABLE_ENV = "LLM_KIT_CLAUDE_EXECUTABLE"


class ClaudeExecutableError(RuntimeError):
    """``LLM_KIT_CLAUDE_EXECUTABLE`` is set to something that cannot be run."""


def configured_claude_executable(env: Optional[Mapping[str, str]] = None) -> str:
    """The Claude executable to resolve: the variable's value, else ``claude``.

    ``env`` defaults to ``os.environ``. Raises :class:`ClaudeExecutableError`
    when ``LLM_KIT_CLAUDE_EXECUTABLE`` is set but empty, or holds a relative
    path (a name with a directory part that is not absolute): the launcher runs
    from a different working directory than the one the user typed it in, so a
    relative path would resolve somewhere else. A bare command name stays valid.
    """
    source = os.environ if env is None else env
    raw = source.get(CLAUDE_EXECUTABLE_ENV)
    if raw is None:
        return CLAUDE_EXECUTABLE
    value = raw.strip()
    if not value:
        raise ClaudeExecutableError(
            "%s is set but empty; unset it to use `%s` from PATH, or set it to "
            "a command name or an absolute path to the Claude executable"
            % (CLAUDE_EXECUTABLE_ENV, CLAUDE_EXECUTABLE)
        )
    has_dir = os.sep in value or (os.altsep is not None and os.altsep in value)
    if has_dir and not os.path.isabs(value):
        raise ClaudeExecutableError(
            "%s=%r is a relative path; use an absolute path or a bare command "
            "name (the Claude launcher runs from a different working directory)"
            % (CLAUDE_EXECUTABLE_ENV, value)
        )
    return value


def _unrunnable(name: str) -> ClaudeExecutableError:
    return ClaudeExecutableError(
        "%s=%r is not an executable file and not a command on PATH"
        % (CLAUDE_EXECUTABLE_ENV, name)
    )


def resolve_claude_executable(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The configured Claude launcher as one path string, or None.

    With ``LLM_KIT_CLAUDE_EXECUTABLE`` unset this is ``shutil.which("claude")``:
    None when ``claude`` is not on PATH. With it set, an unresolvable value
    raises :class:`ClaudeExecutableError` naming the variable rather than
    returning None, so a misconfiguration cannot read as "Claude is not
    installed".
    """
    source = os.environ if env is None else env
    name = configured_claude_executable(source)
    resolved = shutil.which(name)
    if resolved is None and CLAUDE_EXECUTABLE_ENV in source:
        raise _unrunnable(name)
    return resolved


def resolve_claude_cli(env: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, ...]]:
    """The configured Claude launcher as an argv prefix (Windows batch shims
    wrapped in ``cmd /c``), or None; same failure contract as
    :func:`resolve_claude_executable`."""
    source = os.environ if env is None else env
    name = configured_claude_executable(source)
    prefix = resolve_cli(name)
    if prefix is None and CLAUDE_EXECUTABLE_ENV in source:
        raise _unrunnable(name)
    return prefix


#: Characters cmd.exe re-parses as command syntax: the same set as the Codex
#: guard (``codex_command._CMD_METACHARACTERS``). ``subprocess.list2cmdline``
#: escapes an inner double quote as backslash-quote, which cmd.exe does not
#: honour, so an element carrying one of these can break out of its quotes and
#: run as a separate command (reproduced: ``R&D`` ran ``D``). A double quote and
#: a percent sign are NOT in the set, as in the Codex guard: every JSON schema
#: carries quotes, and with these five refused a quote cannot reach a command.
_CMD_METACHARACTERS = ("&", "|", "^", "<", ">")

#: cmd.exe ends the command line at the first line break and drops the rest, so a
#: multi-line argument is cut off without an error (reproduced: a two-line
#: argument arrived as its first line plus an opening quote). Not injection, but
#: silent loss of the prompt, so it is refused the same way.
_CMD_LINE_BREAKS = ("\r", "\n")

#: The flag that takes a system prompt as text, and the flag that takes the same
#: prompt as a file path. Both file flags are in ``claude --help`` (the ``--bare``
#: entry: "--system-prompt[-file], --append-system-prompt[-file]") and in the
#: CLI's own usage strings (``--system-prompt-file <file>``,
#: ``--append-system-prompt-file <file>``).
_SYSTEM_PROMPT_FILE_FLAGS = {
    "--system-prompt": "--system-prompt-file",
    "--append-system-prompt": "--append-system-prompt-file",
}

#: Longest excerpt of a refused element shown in an error.
_EXCERPT = 80


def _on_windows() -> bool:
    return os.name == "nt"


def is_batch_shim(executable: str) -> bool:
    """Whether ``executable`` is a Windows batch launcher (``.cmd`` or ``.bat``).

    ``executable`` is a path or command name; the match is case-insensitive.
    Returns False for anything else and on every platform other than Windows,
    where a ``.cmd`` name is just a file name and cmd.exe is never involved.
    """
    if not _on_windows():
        return False
    return ntpath.splitext(str(executable))[1].lower() in (".cmd", ".bat")


def _excerpt(item: str) -> str:
    text = repr(item)
    if len(text) <= _EXCERPT:
        return text
    return "%s... (%d chars)" % (text[:_EXCERPT], len(item))


def _refuse(item: str, position: int, why: str) -> ValueError:
    return ValueError(
        "refusing to launch through cmd.exe: argv element %d (%s) %s. The "
        "Claude launcher resolved to a batch shim (.cmd or .bat), which "
        "cmd.exe re-parses; refusing is deliberate, because the alternative is "
        "running caller-supplied text as a command or silently cutting it "
        "off. Install Claude Code as a native executable (claude.exe), or set "
        "LLM_KIT_CLAUDE_EXECUTABLE to one, so no batch shim is involved."
        % (position, _excerpt(item), why)
    )


def _check_elements(argv: Sequence[str], skip: frozenset = frozenset()) -> None:
    for position, item in enumerate(argv):
        if position in skip:
            continue
        hit = next((c for c in _CMD_METACHARACTERS if c in item), None)
        if hit is not None:
            raise _refuse(
                item,
                position,
                "contains %r, which cmd.exe re-parses as command syntax (it "
                "would run as a separate command)" % hit,
            )
        if any(c in item for c in _CMD_LINE_BREAKS):
            raise _refuse(
                item,
                position,
                "contains a line break, where cmd.exe ends the command line "
                "(the rest of the argument would be silently dropped)",
            )


def _prompt_value_positions(argv: Sequence[str]) -> List[int]:
    """Indexes of the values that follow a system-prompt flag, before any ``--``."""
    found: List[int] = []
    for position, item in enumerate(argv):
        if item == "--":
            break
        if item in _SYSTEM_PROMPT_FILE_FLAGS and position + 1 < len(argv):
            found.append(position + 1)
    return found


def check_batch_shim_argv(argv: Sequence[str]) -> None:
    """Refuse an argv that a batch-shim launcher would let cmd.exe re-parse.

    ``argv`` starts with the launcher. Does nothing unless ``argv[0]`` is a
    batch shim on Windows (see :func:`is_batch_shim`). Otherwise raises
    ``ValueError`` naming the element position and a bounded excerpt when an
    element contains ``&``, ``|``, ``^``, ``<`` or ``>`` (the Codex guard's set)
    or a line break. The value after ``--system-prompt`` or
    ``--append-system-prompt`` is skipped, because :func:`batch_shim_spawn_argv`
    sends it by file instead; use that function at a spawn.
    """
    if not argv or not is_batch_shim(argv[0]):
        return
    _check_elements(argv, frozenset(_prompt_value_positions(argv)))


@contextlib.contextmanager
def batch_shim_spawn_argv(
    argv: Sequence[str], *, prompt_files: bool = True
) -> Iterator[Sequence[str]]:
    """The argv to spawn for ``argv``, safe for a batch-shim launcher.

    Use as ``with batch_shim_spawn_argv(argv) as spawn:`` around the spawn and
    the whole life of the process. When ``argv[0]`` is not a batch shim, or the
    platform is not Windows, it yields ``argv`` itself, untouched. For a batch
    shim it first refuses any element cmd.exe would re-parse (``ValueError``,
    see :func:`check_batch_shim_argv`), then, with ``prompt_files`` true,
    yields a copy in which each ``--system-prompt <text>`` or
    ``--append-system-prompt <text>`` pair is replaced by the matching
    ``-file`` flag and a private temp file holding the text as UTF-8. The files
    are deleted when the block exits, also on error. With ``prompt_files``
    false (a launch that outlives the call, such as ``--bg``) nothing is moved
    to a file and a system prompt is checked like any other element. A
    ``--`` element ends the flag scan. ``argv`` is never modified.
    """
    if not argv or not is_batch_shim(argv[0]):
        yield argv
        return
    positions = _prompt_value_positions(argv) if prompt_files else []
    _check_elements(argv, frozenset(positions))
    if not positions:
        yield argv
        return
    created: List[str] = []
    try:
        spawn = list(argv)
        for position in positions:
            handle, path = tempfile.mkstemp(prefix="claude_system_prompt_", suffix=".txt")
            created.append(path)
            with os.fdopen(handle, "wb") as stream:
                stream.write(argv[position].encode("utf-8"))
            spawn[position - 1] = _SYSTEM_PROMPT_FILE_FLAGS[argv[position - 1]]
            spawn[position] = path
        # The temp paths are argv too, and the temp directory may hold a
        # metacharacter (a user name containing "&").
        _check_elements(spawn)
        yield spawn
    finally:
        for path in created:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
