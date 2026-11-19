"""Invariants every capability record the library exports must keep.

A capability record (:class:`~llm_kit.completion.capabilities.Capabilities`) advertises the
execution controls an adapter emits, and the ledger records the ones each call reports. Two
properties make that report honest:

1. No record lists an allow-only control beside an unconditional permission bypass. Under a bypass
   every call runs without a permission check, so an ALLOW control (an approval list) restricts
   nothing; listing it reads as a restriction that does not exist. The Claude permission-modes
   reference says "Allow rules have no effect in bypassPermissions".
2. Every control a record advertises is reported by some request through the adapter's real
   builder, and every reported control's fixed flags are in the argv the builder made.

The records are enumerated from the code: every ``Capabilities`` instance the record module
exports, plus the record each backend class carries. A test also checks that no other module in
the package builds a record, so a new record cannot escape these checks. No process is started
and no model runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterator, Optional

import pytest

from llm_kit.adapters import capabilities as records
from llm_kit.adapters import codex_command
from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.claude_session import ClaudeAgentSession
from llm_kit.adapters.codex_backend import CodexCliBackend
from llm_kit.adapters.opencode_backend import OpencodeCliBackend
from llm_kit.completion.capabilities import ALLOW, BYPASS, CONFINE, FIXED, Capabilities
from llm_kit.completion.session_types import SessionBounds, SessionPaths, SessionRequest
from llm_kit.completion.types import ALL_TOOLS, BackendOptions
from llm_kit.jobs.session import FileSessionChannel

PACKAGE_SRC = Path(records.__file__).resolve().parents[1]
SESSION_ID = "0b6f2c1e-3d4a-4b5c-8d9e-0f1a2b3c4d5e"
TAG = "lks_" + "0" * 32
MCP = {"mcpServers": {"tools": {"command": "/bin/tools-mcp", "args": []}}}


def _exported_records() -> list[Capabilities]:
    """Every distinct record the record module exports or a backend class carries."""
    found: dict[int, Capabilities] = {}
    for name in records.__all__:
        value = getattr(records, name)
        if isinstance(value, Capabilities):
            found[id(value)] = value
    for value in records.adapter_capabilities().values():
        found[id(value)] = value
    for backend in (ClaudeCliBackend, ClaudeAgentSession, CodexCliBackend, OpencodeCliBackend):
        found[id(backend.capabilities)] = backend.capabilities
    return list(found.values())


def _record_name(record: Capabilities) -> str:
    for name in records.__all__:
        if getattr(records, name) is record:
            return name
    return record.adapter


RECORDS = _exported_records()
RECORD_IDS = [_record_name(record) for record in RECORDS]


def test_the_enumeration_finds_every_adapter_family_and_the_agent_record() -> None:
    names = set(RECORD_IDS)
    assert {"CLAUDE_CAPABILITIES", "CLAUDE_AGENT_CAPABILITIES", "CODEX_CAPABILITIES",
            "OPENCODE_CAPABILITIES", "OPENAI_COMPAT_CAPABILITIES"} <= names


def test_no_module_outside_the_record_module_builds_a_record() -> None:
    """A record built elsewhere would escape the enumeration above."""
    builder = re.compile(r"(?<![\w.])Capabilities\(")
    allowed = {Path(records.__file__).resolve(), (PACKAGE_SRC / "completion" / "capabilities.py").resolve()}
    offenders = [
        str(path.relative_to(PACKAGE_SRC))
        for path in PACKAGE_SRC.rglob("*.py")
        if path.resolve() not in allowed and builder.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# -- invariant 1: no allow-only control beside a fixed bypass -----------------


@pytest.mark.parametrize("record", RECORDS, ids=RECORD_IDS)
def test_no_record_lists_an_allow_control_beside_an_unconditional_bypass(record: Capabilities) -> None:
    bypasses = [c.id for c in record.execution_controls if c.effect == BYPASS and c.source == FIXED]
    allows = [c.id for c in record.execution_controls if c.effect == ALLOW]
    assert not (bypasses and allows), (
        f"{_record_name(record)} carries the unconditional bypass {bypasses} and lists the "
        f"allow-only controls {allows}, which restrict nothing under that bypass"
    )


# -- invariant 2: every advertised control is reachable and carried -----------

#: One driven request: the argv the builder made (None for an env-based adapter) and the
#: control ids the adapter reported for it.
Driven = tuple[Optional[list[str]], tuple[str, ...]]


class _Runner:
    def __init__(self) -> None:
        self.argv: list[str] = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.argv = [str(part) for part in cmd]
        return json.dumps({"result": "ok", "is_error": False, "usage": {}}), "", 0


def _claude_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Driven]:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    shapes = [
        (False, None), (True, None), (False, BackendOptions(allowed_tools="Read")),
        (False, BackendOptions(allowed_tools="Write")), (False, BackendOptions(allowed_tools=ALL_TOOLS)),
        (False, BackendOptions(disallowed_tools="Agent")),
    ]
    for text_only, options in shapes:
        runner = _Runner()
        response = ClaudeCliBackend(runner=runner, executable="claude", text_only=text_only).complete(
            "sys", "usr", model="m", options=options)
        yield runner.argv, tuple(response.execution_controls_applied)


def _session_request(tmp_path: Path, mcp_config: Optional[dict] = None, **options: object) -> SessionRequest:
    cwd = tmp_path.resolve()
    paths = SessionPaths.for_attempt(cwd / "sessions", "job-1", 1)
    return SessionRequest(
        system="sys", user="usr", model="haiku",
        options=BackendOptions(timeout_s=30.0, cwd=cwd, system_prompt_mode="append", **options),  # type: ignore[arg-type]
        cwd=cwd, session_id=SESSION_ID, resume=False, process_tag=TAG, paths=paths, bounds=SessionBounds(),
        channel=FileSessionChannel(paths), mcp_config=mcp_config,
    )


def _agent_driven(request: SessionRequest) -> Driven:
    backend = ClaudeAgentSession(executable="claude")
    return backend.build_argv(request), backend.applied_controls(request)


def _claude_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Driven]:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    for mcp_config, options in [
        (None, {}), (None, {"allowed_tools": "Read"}), (None, {"allowed_tools": ""}),
        (None, {"allowed_tools": ALL_TOOLS}), (None, {"disallowed_tools": "Agent"}), (MCP, {}),
        (MCP, {"allowed_tools": "Read,Edit"}),
    ]:
        yield _agent_driven(_session_request(tmp_path, mcp_config, **options))


def _codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Driven]:
    backend = CodexCliBackend.__new__(CodexCliBackend)
    root = tmp_path.resolve()
    text_only = {
        "sandbox": "read-only", "network": False, "ignore_user_config": True,
        "extra_config": (*records._CODEX_TEXT_ONLY_CONFIG, f"model_catalog_json='{root / 'catalog.json'}'"),
    }
    for os_name in ("posix", "nt"):
        with monkeypatch.context() as patch:
            patch.setattr(codex_command.os, "name", os_name)
            for kwargs in ({}, {"network": False}, {"native_subagents": "deny"}, text_only):
                argv = [str(part) for part in codex_command.build_codex_exec_argv(
                    root=root, argv_prefix=("codex",), output_file=root / "o.txt", **kwargs)]
                yield argv, backend._applied_controls(argv)


def _opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Driven]:
    backend = OpencodeCliBackend.__new__(OpencodeCliBackend)
    for deny in (None, "Write Bash Agent"):
        yield None, backend._applied_controls(BackendOptions(disallowed_tools=deny))


def _openai_compat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Driven]:
    # The record advertises no execution control; nothing is driven.
    return iter(())


DRIVERS: dict[int, Callable[[Path, pytest.MonkeyPatch], Iterator[Driven]]] = {
    id(records.CLAUDE_CAPABILITIES): _claude_completion,
    id(records.CLAUDE_AGENT_CAPABILITIES): _claude_agent,
    id(records.CODEX_CAPABILITIES): _codex,
    id(records.OPENCODE_CAPABILITIES): _opencode,
    id(records.OPENAI_COMPAT_CAPABILITIES): _openai_compat,
}


def _fixed_tokens(emits: str) -> list[str]:
    """The literal argv tokens of an ``emits`` string: up to the first optional part, no placeholder."""
    head = re.sub(r"<[^>]*>", "\0", emits.split("[", 1)[0])
    return [token for token in head.split() if "\0" not in token and token != '""']


@pytest.mark.parametrize("record", RECORDS, ids=RECORD_IDS)
def test_every_advertised_control_is_reported_by_some_request_and_carried_by_its_argv(
        record: Capabilities, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = DRIVERS.get(id(record))
    assert driver is not None, f"{_record_name(record)} has no driver in DRIVERS; add one"
    controls = {control.id: control for control in record.execution_controls}
    reported: set[str] = set()
    for argv, applied in driver(tmp_path, monkeypatch):
        reported.update(applied)
        if argv is None:
            continue
        for control_id in applied:
            missing = [t for t in _fixed_tokens(controls[control_id].emits) if t not in argv]
            assert missing == [], f"{control_id} was reported but the argv lacks {missing}: {argv}"
    assert reported == set(controls), (
        f"{_record_name(record)} advertises controls no request reports: {sorted(set(controls) - reported)}"
    )


# -- the Claude agent session's tool grant -----------------------------------


def _tools(argv: list[str]) -> Optional[str]:
    return argv[argv.index("--tools") + 1] if "--tools" in argv else None


@pytest.mark.parametrize("grant, tools", [("Read", "Read"), ("Read,Grep Glob", "Read,Grep,Glob"),
                                          ("Edit,Edit Bash", "Edit,Bash")])
def test_an_agent_grant_reaches_the_child_as_a_tool_restriction_under_the_bypass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grant: str, tools: str) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    argv, applied = _agent_driven(_session_request(tmp_path, allowed_tools=grant))
    assert _tools(argv) == tools
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--allowedTools" not in argv
    # No MCP server from any settings source adds a tool outside the grant.
    at = argv.index("--mcp-config")
    assert argv[at - 1] == "--strict-mcp-config" and json.loads(argv[at + 1]) == {"mcpServers": {}}
    assert applied == ("permission-bypass", "tool-grant")


def test_an_agent_grant_beside_the_jobs_mcp_servers_keeps_those_servers(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    argv, applied = _agent_driven(_session_request(tmp_path, MCP, allowed_tools="Read"))
    assert _tools(argv) == "Read"
    assert argv.count("--mcp-config") == 1 and json.loads(argv[argv.index("--mcp-config") + 1]) == MCP
    assert applied == ("permission-bypass", "tool-grant", "mcp-servers")


def test_an_empty_agent_grant_is_no_built_in_tool_like_the_completion_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    argv, applied = _agent_driven(_session_request(tmp_path, allowed_tools=""))
    assert _tools(argv) == ""
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert applied == ("permission-bypass", "tool-grant")


@pytest.mark.parametrize("grant", [None, ALL_TOOLS], ids=["no-grant", "all-tools"])
def test_an_agent_session_with_no_grant_or_all_tools_keeps_every_tool(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grant: Optional[str]) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    argv, applied = _agent_driven(_session_request(tmp_path, allowed_tools=grant))
    assert "--tools" not in argv and "--allowedTools" not in argv and "--mcp-config" not in argv
    assert applied == ("permission-bypass",)


@pytest.mark.parametrize("grant, error, match", [
    ("default", ValueError, "'default'"),
    ("Read,DEFAULT", ValueError, "'DEFAULT'"),
    ("mcp__relay__reply", ValueError, "MCP"),
    ("-p", ValueError, "'-p'"),
    ("Read --dangerously-skip-permissions", ValueError, "'--dangerously-skip-permissions'"),
    ("Read,*", ValueError, "ALL_TOOLS"),
    ("Bash(git log *)", ValueError, "bypassPermissions"),
    ("Read(./**)", ValueError, "bypassPermissions"),
    ("Bash(git log *", ValueError, "allowed_tools"),
    ("   ", ValueError, "names no tool"),
    (["Read"], TypeError, "allowed_tools"),
    (1, TypeError, "allowed_tools"),
])
def test_an_agent_grant_the_completion_path_would_refuse_is_refused_before_the_spawn(
        tmp_path: Path, grant: object, error: type, match: str) -> None:
    request = _session_request(tmp_path, allowed_tools=grant)
    backend = ClaudeAgentSession(executable="claude")
    with pytest.raises(error, match=match):
        backend.build_argv(request)
    with pytest.raises(error, match=match):
        backend.applied_controls(request)


def test_the_agent_record_advertises_the_grant_as_confining() -> None:
    controls = {control.id: control for control in records.CLAUDE_AGENT_CAPABILITIES.execution_controls}
    grant = controls["tool-grant"]
    assert (grant.effect, grant.source, grant.parameter) == (CONFINE, "request", "allowed_tools")
    assert grant.emits.startswith("--tools <")
    # The live suite covers the completion path only; the note must not claim it covers this grant.
    assert "no test runs the CLI with this argv" in grant.note
    assert "allowed-tools" not in controls
    assert records.CLAUDE_AGENT_CAPABILITIES.params["allowed_tools"].emits == "--tools"
