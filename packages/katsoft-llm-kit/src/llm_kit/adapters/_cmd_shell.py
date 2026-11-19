"""Characters cmd.exe cannot carry through a Windows batch launcher -- one definition.

Private to :mod:`llm_kit.adapters`. A Claude or Codex CLI installed by npm or
scoop is a ``.cmd`` launcher on Windows. CreateProcess runs it through cmd.exe,
which re-parses the command line, so some characters in an argv element do not
arrive as text. :data:`REFUSED_CHARACTERS` is the only list of them and
:func:`check_cmd_argv` the only check; ``claude_command`` and ``codex_command``
both call it, so a character added here is refused on both paths.

How the launcher is started: CPython's ``subprocess`` with a list argv joins it
with ``list2cmdline`` and calls CreateProcess, which runs a ``.cmd`` as
``cmd.exe /c <command line>``, with no ``/v:off`` and no ``/d`` (observed:
``%CMDCMDLINE%`` reads ``cmd.exe /c <launcher> <args>``). So the machine's
registry decides whether delayed expansion is on (``DelayedExpansion``) and
whether an ``AutoRun`` command runs first.

Decision for each cmd.exe special character, observed on Windows 11 through
an echo launcher with the structure npm's cmd-shim gives ``claude.cmd`` and
``codex.cmd`` (``%*`` on its last line, outside any parenthesised block); the
observations are tests in ``tests/llm-kit/test_cmd_shell.py``:

- ``& | ^ < >``: refused. Command syntax; ``^`` is dropped (``a^b`` arrived as
  ``ab``).
- ``%``: refused. ``%VAR%`` is replaced by the variable, quoted or not.
- ``!``: refused. With delayed expansion on (registry ``DelayedExpansion=1``, or
  ``cmd /v:on``), ``a!VAR!b`` arrived with the variable's value in place of
  ``!VAR!``: a prompt could read a secret from the environment into its own
  argument. It is off on this machine, but the launcher cannot turn it off.
- line breaks: refused. cmd.exe ends the command line there.
- ``"``: allowed. ``list2cmdline`` writes ``\\"``; cmd.exe's quote state flips,
  but with every character above refused nothing it could expose remains, and
  the program's own parser restores the quote (``say "hi" now`` arrived
  unchanged). Every JSON schema carries quotes.
- ``(`` and ``)``: allowed. They are syntax only inside a parenthesised block,
  and the launcher's ``%*`` is outside one (``x b(c)`` and ``)`` arrived
  unchanged). A launcher that put ``%*`` inside a block would need them refused.
- ``,`` ``;`` ``=``: allowed. They split ``%1``-style batch arguments, but the
  launcher passes ``%*`` through as written (``a b,c;d=e`` arrived unchanged).
- a trailing backslash, or a backslash before a quote: allowed.
  ``list2cmdline`` doubles the backslashes before a closing or an escaped quote
  and the program's parser halves them (``C:\\dir with space\\`` and ``a\\"b``
  arrived unchanged).

A system prompt is not checked: the Claude spawn paths send it by file
(``claude_command.batch_shim_spawn_argv``), so it may hold any of these.
"""

from __future__ import annotations

from typing import Optional, Sequence

#: Characters cmd.exe treats as command syntax or variable expansion.
#: ``subprocess.list2cmdline`` escapes an inner double quote as backslash-quote,
#: which cmd.exe does not honour, so an element carrying ``& | ^ < >`` can break
#: out of its quotes and run as a separate command (reproduced: ``R&D`` ran
#: ``D``). ``%`` is expanded as ``%VAR%`` in a batch launcher's arguments
#: whether or not the element is quoted, and ``!VAR!`` when delayed expansion is
#: on. A double quote is NOT in the set (see the module docstring for each
#: character left out).
CMD_METACHARACTERS = ("&", "|", "^", "<", ">", "%", "!")

#: cmd.exe ends the command line at the first line break and drops the rest, so
#: a multi-line argument is cut off without an error (reproduced: a two-line
#: argument arrived as its first line plus an opening quote). Not injection, but
#: silent loss of the argument, so it is refused the same way.
CMD_LINE_BREAKS = ("\r", "\n")

#: Every character :func:`check_cmd_argv` refuses.
REFUSED_CHARACTERS = CMD_METACHARACTERS + CMD_LINE_BREAKS

#: Longest excerpt of a refused element shown in an error.
EXCERPT_LENGTH = 80


def refused_character(item: str) -> Optional[str]:
    """The first character of ``item`` in :data:`REFUSED_CHARACTERS`, or None."""
    return next((c for c in REFUSED_CHARACTERS if c in item), None)


def _excerpt(item: str) -> str:
    text = repr(item)
    if len(text) <= EXCERPT_LENGTH:
        return text
    return "%s... (%d chars)" % (text[:EXCERPT_LENGTH], len(item))


def _reason(char: str) -> str:
    if char in CMD_LINE_BREAKS:
        return (
            "contains a line break, where cmd.exe ends the command line (the "
            "rest of the argument would be silently dropped)"
        )
    if char == "%":
        return (
            "contains '%', which cmd.exe expands as %VAR% in a batch "
            "launcher's arguments (the value would arrive changed)"
        )
    if char == "!":
        return (
            "contains '!', which cmd.exe expands as !VAR! in a batch "
            "launcher's arguments when delayed expansion is on (the value would "
            "arrive changed)"
        )
    return (
        "contains %r, which cmd.exe re-parses as command syntax (it would run "
        "as a separate command)" % char
    )


def check_cmd_argv(
    argv: Sequence[str], *, advice: str, skip: frozenset = frozenset()
) -> None:
    """Raise ``ValueError`` when an element of ``argv`` holds a refused character.

    ``argv`` is a sequence of strings; the elements at the positions in ``skip``
    are not checked. The error names the element position, a bounded excerpt
    of the element and the character, then ends with ``advice``, the caller's
    own sentence on how to avoid the batch launcher. Returns None when every
    checked element is clean. The caller decides whether the launcher is a batch
    launcher at all; this function checks every element it is given.
    """
    for position, item in enumerate(argv):
        if position in skip:
            continue
        hit = refused_character(item)
        if hit is not None:
            raise ValueError(
                "refusing to launch through cmd.exe: argv element %d (%s) %s. %s"
                % (position, _excerpt(item), _reason(hit), advice)
            )
