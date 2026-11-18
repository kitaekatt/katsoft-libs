"""claude-cli tool modes: no grant is tool-less, a grant restricts, ``ALL_TOOLS`` is the worker.

The Claude Code CLI reference documents ``--allowedTools`` as an approval list ("Tools that
execute without prompting for permission ... To restrict which tools are available, use
``--tools`` instead"), and the permission-modes page says "Allow rules have no effect in
``bypassPermissions``". The old default argv, ``--permission-mode bypassPermissions
--allowedTools ""``, therefore gave every completion every built-in tool, run unprompted.

These tests pin the argv of each mode through the runner seam. No process is started and no
model runs.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from llm_kit import _runtime
from llm_kit.adapters import claude_command
from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.capabilities import _CLAUDE_TEXT_ONLY_ARGS, CLAUDE_CAPABILITIES, CODEX_CAPABILITIES
from llm_kit.completion.types import BackendOptions, LLMResponse
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.jobs import run as jobs
from llm_kit.jobs.model import Contract, Job, Prompt

TEXT_ONLY = list(_CLAUDE_TEXT_ONLY_ARGS)
#: The text-only argv after ``--tools ""``: no MCP server, no skills, no settings, dontAsk.
ISOLATION = TEXT_ONLY[2:]
#: The argv a job worker has always had: every built-in tool, permission checks bypassed.
WORKER_TAIL = ["--permission-mode", "bypassPermissions", "--allowedTools", ""]
SHIM = r"C:\Users\x\AppData\Roaming\npm\claude.cmd"
PROMPT = "You are a reviewer.\nAnswer with a JSON array."


def _all_tools() -> str:
    from llm_kit.completion.types import ALL_TOOLS  # noqa: PLC0415 -- the name under test

    return ALL_TOOLS


def _grant(name: Optional[str]) -> Optional[str]:
    """A parametrized grant: the literal value, or ``ALL_TOOLS`` resolved at run time."""
    return _all_tools() if name == "ALL_TOOLS" else name


class _Runner:
    def __init__(self, text: str = "ok") -> None:
        self.calls: list = []
        self.text = text

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append((list(cmd), cwd))
        return json.dumps({"result": self.text, "is_error": False, "usage": {}}), "", 0


def _call(options: Optional[BackendOptions] = None, *, text_only: bool = False,
          executable: str = "claude", system: str = "sys") -> tuple[list, LLMResponse]:
    runner = _Runner()
    backend = ClaudeCliBackend(runner=runner, executable=executable, text_only=text_only)
    response = backend.complete(system, "usr", model="m", options=options)
    ((argv, _cwd),) = runner.calls
    return argv, response


@pytest.fixture(autouse=True)
def _no_claudx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)


def test_the_text_only_argv_starts_with_an_empty_tool_list() -> None:
    assert TEXT_ONLY[:2] == ["--tools", ""]


# -- the completion backend --------------------------------------------------


@pytest.mark.parametrize("options", [None, BackendOptions(), BackendOptions(allowed_tools="")],
                         ids=["no-options", "no-grant", "empty-grant"])
def test_a_completion_with_no_grant_is_tool_less(options: Optional[BackendOptions]) -> None:
    argv, response = _call(options)
    assert argv[-len(TEXT_ONLY):] == TEXT_ONLY
    assert "bypassPermissions" not in argv
    assert "--allowedTools" not in argv
    assert response.execution_controls_applied == ("no-session-persistence", "text-only-mode")


def test_a_named_grant_restricts_the_tool_set_without_a_bypass() -> None:
    argv, response = _call(BackendOptions(allowed_tools="Write"))
    at = argv.index("--tools")
    assert argv[at:] == ["--tools", "Write", "--allowedTools", "Write", *ISOLATION]
    assert "bypassPermissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert response.execution_controls_applied == ("allowed-tools", "tool-grant", "no-session-persistence")


@pytest.mark.parametrize("grant, tools", [
    ("Read", "Read"), ("Read,Grep,Glob", "Read,Grep,Glob"), ("read GREP", "read,GREP"),
])
def test_a_bare_read_only_grant_is_not_pre_approved_so_reads_stay_in_the_cwd(grant: str, tools: str) -> None:
    argv, response = _call(BackendOptions(allowed_tools=grant))
    at = argv.index("--tools")
    assert argv[at:] == ["--tools", tools, *ISOLATION]
    assert "--allowedTools" not in argv and "bypassPermissions" not in argv
    assert response.execution_controls_applied == ("tool-grant", "no-session-persistence")


@pytest.mark.parametrize("grant, tools, approved", [
    ("Read(/some/abs/**)", "Read", "Read(/some/abs/**)"),
    ("Read(./**),Grep,Glob", "Read,Grep,Glob", "Read(./**)"),
    ("Read,Write", "Read,Write", "Write"),
    ("Bash(git log *) Read,Bash(git diff *)", "Bash,Read", "Bash(git log *),Bash(git diff *)"),
    ("Bash(git log *) Write Edit", "Bash,Write,Edit", "Bash(git log *),Write,Edit"),
])
def test_scoped_rules_and_non_read_tools_are_pre_approved_exactly_as_written(grant: str, tools: str,
                                                                              approved: str) -> None:
    argv, response = _call(BackendOptions(allowed_tools=grant))
    assert argv[argv.index("--tools") + 1] == tools
    assert argv[argv.index("--allowedTools") + 1] == approved
    assert "bypassPermissions" not in argv
    assert "allowed-tools" in response.execution_controls_applied


def test_all_tools_is_the_explicit_worker_mode_and_keeps_the_worker_argv() -> None:
    argv, response = _call(BackendOptions(allowed_tools=_all_tools()))
    assert argv == [
        "claude", "-p", "--model", "m", "--system-prompt", "sys", "--output-format", "json",
        "--no-session-persistence", *WORKER_TAIL,
    ]
    assert response.execution_controls_applied == (
        "allowed-tools", "permission-bypass", "no-session-persistence",
    )


@pytest.mark.parametrize("grant", ["Read,*", "* Read", "Bash(git log *"])
def test_a_malformed_grant_is_refused_before_dispatch(grant: str) -> None:
    runner = _Runner()
    with pytest.raises(ValueError, match="allowed_tools"):
        ClaudeCliBackend(runner=runner, executable="claude").complete(
            "sys", "usr", model="m", options=BackendOptions(allowed_tools=grant)
        )
    assert runner.calls == []


@pytest.mark.parametrize("grant, bad", [
    ("default", "default"),
    ("Default", "Default"),
    ("Read,default", "default"),
    ("DEFAULT Read", "DEFAULT"),
    ("--dangerously-skip-permissions", "--dangerously-skip-permissions"),
    ("Read -p", "-p"),
    ("Read;Bash", "Read;Bash"),
    ("Bash(git log *) 1Read", "1Read"),
])
def test_a_grant_naming_no_built_in_tool_is_refused_before_dispatch(grant: str, bad: str) -> None:
    runner = _Runner()
    with pytest.raises(ValueError, match=re.escape(repr(bad))):
        ClaudeCliBackend(runner=runner, executable="claude").complete(
            "sys", "usr", model="m", options=BackendOptions(allowed_tools=grant)
        )
    assert runner.calls == []


@pytest.mark.parametrize("grant", ["mcp__relay__reply", "Read mcp__x__y", "MCP__x__y"])
def test_an_mcp_tool_grant_is_refused_because_the_grant_loads_no_mcp_server(grant: str) -> None:
    runner = _Runner()
    with pytest.raises(ValueError, match="MCP server"):
        ClaudeCliBackend(runner=runner, executable="claude").complete(
            "sys", "usr", model="m", options=BackendOptions(allowed_tools=grant)
        )
    assert runner.calls == []


@pytest.mark.parametrize("grant", [["Read"], ("Read", "Grep"), 1])
def test_a_grant_that_is_not_a_string_is_refused_before_dispatch(grant: Any) -> None:
    runner = _Runner()
    with pytest.raises(TypeError, match="allowed_tools"):
        ClaudeCliBackend(runner=runner, executable="claude").complete(
            "sys", "usr", model="m", options=BackendOptions(allowed_tools=grant)
        )
    assert runner.calls == []


@pytest.mark.parametrize("grant", ["Read", "ALL_TOOLS"])
def test_text_only_refuses_any_grant_before_dispatch(grant: str) -> None:
    runner = _Runner()
    with pytest.raises(ValueError, match="text-only"):
        ClaudeCliBackend(runner=runner, executable="claude", text_only=True).complete(
            "sys", "usr", model="m", options=BackendOptions(allowed_tools=_grant(grant))
        )
    assert runner.calls == []


@pytest.mark.parametrize("grant", [None, "Read", "ALL_TOOLS"])
def test_the_deny_list_rides_every_mode(grant: Optional[str]) -> None:
    argv, response = _call(BackendOptions(allowed_tools=_grant(grant), disallowed_tools="Agent Task"))
    assert argv[argv.index("--disallowedTools") + 1] == "Agent Task"
    assert "disallowed-tools" in response.execution_controls_applied


@pytest.mark.parametrize("grant, tail", [
    (None, TEXT_ONLY),
    ("Read,Grep,Glob", ["--tools", "Read,Grep,Glob", *ISOLATION]),
    ("Bash(git log *),Read", ["--tools", "Bash,Read", "--allowedTools", "Bash(git log *)", *ISOLATION]),
    ("ALL_TOOLS", WORKER_TAIL),
], ids=["no-grant", "read-grant", "scoped-grant", "all-tools"])
def test_each_mode_passes_through_the_batch_shim_guard(monkeypatch: pytest.MonkeyPatch,
                                                       grant: Optional[str], tail: list) -> None:
    monkeypatch.setattr(claude_command, "_on_windows", lambda: True)
    argv, _ = _call(BackendOptions(allowed_tools=_grant(grant)), executable=SHIM, system=PROMPT)
    assert argv[0] == SHIM and "--system-prompt-file" in argv
    assert PROMPT not in argv
    assert argv[-len(tail):] == tail


def test_the_record_advertises_the_control_each_mode_reports() -> None:
    controls = {control.id: control for control in CLAUDE_CAPABILITIES.execution_controls}
    grant = controls["tool-grant"]
    assert grant.emits.startswith("--tools <") and "--permission-mode dontAsk" in grant.emits
    assert (grant.source, grant.parameter, grant.subjects) == ("request", "allowed_tools", ())
    bypass = controls["permission-bypass"]
    assert (bypass.parameter, bypass.when_value) == ("allowed_tools", _all_tools())
    assert CLAUDE_CAPABILITIES.guarantees == ()


# -- the code-review lane ----------------------------------------------------


_AGENT_LANE = "agent_lane"
_PURE_LANE = "pure_lane"


@pytest.fixture()
def review_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """``llm_kit.dispatch.review_lane`` over stub ``bootstrap_lib`` lane modules, one Claude endpoint."""

    class LaneOutputError(Exception):
        pass

    prompts = types.ModuleType("bootstrap_lib.code_review.lane_prompts")
    prompts.ENDPOINT_ELIGIBLE_LANES = frozenset({_AGENT_LANE, _PURE_LANE})
    prompts.KNOWN_LANES = frozenset({_AGENT_LANE, _PURE_LANE})
    prompts.LANE_PROMPTS = {lane: SimpleNamespace(system="review") for lane in (_AGENT_LANE, _PURE_LANE)}
    prompts.LANES_REQUIRING_AGENT_LOOP = frozenset({_AGENT_LANE})
    prompts.PROMPT_VERSION = "test"
    prompts.LaneOutputError = LaneOutputError
    prompts.build_user_message = lambda lane, **_kw: "diff"
    prompts.is_agent_alias = lambda model: False
    prompts.parse_issue_array = lambda text, **_kw: json.loads(text)
    output = types.ModuleType("bootstrap_lib.code_review.lane_output")
    output.claude_mds_by_file = lambda *_a, **_kw: {}
    output.load_bundle = lambda *_a, **_kw: {}
    for name, module in (
        ("bootstrap_lib", types.ModuleType("bootstrap_lib")),
        ("bootstrap_lib.code_review", types.ModuleType("bootstrap_lib.code_review")),
        ("bootstrap_lib.code_review.lane_prompts", prompts),
        ("bootstrap_lib.code_review.lane_output", output),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(_runtime, "fleet", lambda: True)
    sys.modules.pop("llm_kit.dispatch.review_lane", None)
    module = importlib.import_module("llm_kit.dispatch.review_lane")
    runner = _Runner(text="[]")
    selection = BackendSelection(
        endpoint="claude-ep", kind="harness", backend=ClaudeCliBackend(runner=runner, executable="claude"),
        model="m", capabilities=CLAUDE_CAPABILITIES,
    )
    monkeypatch.setattr(module, "_lane_route", lambda model, root: (SimpleNamespace(id="claude-ep", via=None), None))
    monkeypatch.setattr(module, "_lane_selection", lambda *_a, **_kw: selection)
    monkeypatch.setattr(module, "_check_window", lambda *_a, **_kw: None)
    monkeypatch.setattr(module, "call_with_fallthrough", lambda _reselector, call, *, first, **_kw: call(first))
    yield SimpleNamespace(module=module, runner=runner, root=tmp_path)
    sys.modules.pop("llm_kit.dispatch.review_lane", None)


def _run_lane(lane_world, lane: str) -> tuple[list, Any]:
    envelope = lane_world.module.run_lane(
        lane=lane, model="claude-ep", effort="high", diff_text="d", project_root=str(lane_world.root),
    )
    assert envelope["issues"] == []
    ((argv, cwd),) = lane_world.runner.calls
    return argv, cwd


def test_an_agent_loop_review_lane_reaches_its_reviewer_with_only_read_grep_and_glob(review_lane) -> None:
    argv, cwd = _run_lane(review_lane, _AGENT_LANE)
    at = argv.index("--tools")
    assert argv[at:at + len(ISOLATION) + 2] == ["--tools", "Read,Grep,Glob", *ISOLATION]
    # Not pre-approved: dontAsk lets reads run only inside the cwd, the project root.
    assert "--allowedTools" not in argv
    assert "bypassPermissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert Path(cwd) == review_lane.root


def test_a_pure_review_lane_reaches_its_reviewer_tool_less(review_lane) -> None:
    argv, _ = _run_lane(review_lane, _PURE_LANE)
    at = argv.index("--tools")
    assert argv[at:at + len(TEXT_ONLY)] == TEXT_ONLY
    assert "bypassPermissions" not in argv and "--allowedTools" not in argv


# -- the jobs runner ---------------------------------------------------------


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import llm_kit.dispatch.declaration as declaration
    import llm_kit.dispatch.usage_budget as usage
    import llm_kit.registry.models as models

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.setattr(models, "discover_model_entries", lambda **_kwargs: {})
    monkeypatch.setattr(declaration, "check_many", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(usage, "record_observed_halt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs.BackpressurePolicy, "from_environment",
                        staticmethod(lambda: jobs.BackpressurePolicy(cap_s=0.0)))


def _job(root: Path, **options: Any) -> Job:
    return Job(id="worker", prompt=Prompt(user="Write the artifact"), models=("proof-claude",), directory=root,
               options=options, contract=Contract(command=(sys.executable, "-I", "-c", "pass"), directory=root))


class _RecordingClaude(ClaudeCliBackend):
    """The real completion backend, recording the ``allowed_tools`` each call receives."""

    seen: list = []

    def complete(self, system, user, *, model, options=None):
        type(self).seen.append(options.allowed_tools if options is not None else None)
        return super().complete(system, user, model=model, options=options)


def _run_claude_job(tmp_path: Path, job: Job, floor: Optional[str] = None) -> tuple[list, list, Any]:
    runner = _Runner(text="done")
    _RecordingClaude.seen = []
    backend = _RecordingClaude(runner=runner, executable="claude")

    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", backend, "opus")

    snapshot = jobs.run_jobs([job], tmp_path / "ledger.sqlite3", run_id="modes", disallowed_tools=floor,
                             backend_factory=factory, capabilities_provider=lambda: {"claude-cli": CLAUDE_CAPABILITIES})
    return [argv for argv, _cwd in runner.calls], list(_RecordingClaude.seen), snapshot


@pytest.mark.usefixtures("isolated")
def test_a_claude_completion_job_with_no_grant_runs_in_the_explicit_worker_mode(tmp_path: Path) -> None:
    calls, seen, snapshot = _run_claude_job(tmp_path, _job(tmp_path))
    assert snapshot.jobs[0].state.value == "accepted"
    assert seen == [_all_tools()]
    assert calls[0][-len(WORKER_TAIL):] == WORKER_TAIL and "--tools" not in calls[0]


@pytest.mark.usefixtures("isolated")
def test_a_job_grant_reaches_the_child_as_a_restriction(tmp_path: Path) -> None:
    calls, seen, snapshot = _run_claude_job(tmp_path, _job(tmp_path, allowed_tools="Read"))
    assert snapshot.jobs[0].state.value == "accepted"
    assert seen == ["Read"]
    assert calls[0][calls[0].index("--tools") + 1] == "Read"
    assert "bypassPermissions" not in calls[0]


@pytest.mark.usefixtures("isolated")
def test_a_write_floor_still_runs_the_job_text_only(tmp_path: Path) -> None:
    calls, seen, snapshot = _run_claude_job(tmp_path, _job(tmp_path), floor="Write")
    assert snapshot.jobs[0].state.value == "accepted"
    assert seen == [None]
    assert calls[0][-len(TEXT_ONLY) - 2:-2] == TEXT_ONLY
    assert calls[0][-2:] == ["--disallowedTools", "Write"]


@pytest.mark.usefixtures("isolated")
def test_a_job_on_another_adapter_is_given_no_claude_tool_value(tmp_path: Path) -> None:
    seen: list = []

    class Codex:
        name = "codex-cli"
        capabilities = CODEX_CAPABILITIES

        def complete(self, system, user, *, model, options=None):
            seen.append(options.allowed_tools)
            return LLMResponse(text="done", model=model, input_tokens=0, output_tokens=0)

        def classify_halt(self, exc):
            return None

    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", Codex(), "gpt")

    snapshot = jobs.run_jobs([_job(tmp_path)], tmp_path / "ledger.sqlite3", run_id="codex", backend_factory=factory,
                             capabilities_provider=lambda: {"codex-cli": CODEX_CAPABILITIES})
    assert snapshot.jobs[0].state.value == "accepted"
    assert seen == [None]
