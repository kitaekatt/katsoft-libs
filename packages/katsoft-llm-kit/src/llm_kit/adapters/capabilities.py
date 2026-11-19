"""The filled capability record for each adapter family.

Every value below is derived from the code that builds the request, and every
``emits`` string is asserted by a seam test in
``tests/llm-scripting-kit/test_completion_capabilities.py``. When an adapter
changes what it emits, this file changes in the same commit -- that pairing is
the SSOT rule, and the seam tests are what enforce it rather than trust.

The records live beside the adapters rather than in a config file precisely
because a capability is a fact about code. A YAML copy would be a second source
of truth that can disagree with the adapter, which is the drift this replaces.
"""
from __future__ import annotations

import re
from dataclasses import fields
from typing import Any

from llm_kit.registry.adapter_names import (
    ADAPTER_CLAUDE_CLI,
    ADAPTER_CODEX_CLI,
    ADAPTER_OPENAI_COMPAT,
    ADAPTER_OPENCODE_CLI,
    NEW_CAPABILITIES_NAME,
    OLD_CAPABILITIES_NAME,
    warn_renamed,
)
from llm_kit.completion.capabilities import (
    FILESYSTEM_WRITE,
    MODE_COMPLETION,
    MODE_WORKSPACE,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    TEXT_ONLY_MODE,
    TEXT_ONLY_PARAMETER,
    ALLOW,
    APPEND,
    BYPASS,
    Capabilities,
    CONFINE,
    DENY,
    DISABLE,
    ExecutionControl,
    FIXED,
    NATIVE,
    NATIVE_ROLE,
    NONE,
    PASSTHROUGH,
    PROMPT_FOLD,
    ParamCapability,
    REPLACE,
    REQUEST,
    StructuredOutputCapability,
    SkillContextCapability,
    SystemPromptCapability,
    PARSED_RESULT,
    TEXT_RESULT,
    WINDOWS,
    _disallowed_tool_clauses,
)
from llm_kit.completion.contract_types import (
    DELIVERY_NATIVE,
    DELIVERY_PROMPT,
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    SCHEMA_CLASS_OPENAI_STRICT,
    SCHEMA_CLASS_SUBSET,
)
from llm_kit.completion.skill_context_types import DELIVERY_SYSTEM_MESSAGE
from llm_kit.completion.types import ALL_TOOLS, BackendOptions

# Every field on BackendOptions, READ FROM THE DATACLASS rather than restated.
# An adapter's dropped_params is this set minus the params it reads, so a field
# added to BackendOptions is immediately dropped-by-default everywhere instead of
# going unadvertised. A hand-copied list here would be a second source of truth
# free to fall behind the dataclass -- exactly the drift this module exists to
# remove -- and it would fail silently, because a forgotten field simply never
# appears in any record.
_ALL_OPTION_FIELDS = tuple(f.name for f in fields(BackendOptions))


def _dropped(honored: object) -> tuple:
    """BackendOptions fields this adapter does not read, in declaration order."""
    return tuple(name for name in _ALL_OPTION_FIELDS if name not in honored)


def _output_contract_param(emits: "str | None" = None) -> ParamCapability:
    """``output_contract`` as every adapter handles it: READ, then refused
    before dispatch unless the record lists the contract's policy.

    A refused param is still a read one, so it belongs in ``params`` and not in
    ``dropped_params`` -- "dropped" means "not read", and reporting a contract
    as dropped would tell a caller it went nowhere when in fact it stopped the
    call. Which policies an adapter satisfies is its
    ``structured_output.policies``. An adapter that DELIVERS a schema contract
    passes ``emits``, the same string as its ``structured_output.contract_emits``.
    """
    if emits is None:
        return ParamCapability(
            type="output-contract",
            note=(
                "read and refused before dispatch unless structured_output.policies "
                "lists the contract's policy"
            ),
        )
    return ParamCapability(
        type="output-contract",
        emits=emits,
        note=(
            "a listed schema policy is delivered as emitted and the answer is "
            "validated at the seam; text-only emits nothing; a policy "
            "structured_output.policies does not list is refused before dispatch"
        ),
    )


#: ``params.skill_context.emits`` and ``skill_context.emits`` of the one
#: adapter that delivers skill context: the block leads the system message,
#: before the caller's system text and any schema instruction. Asserted
#: against the captured request by
#: tests/llm-scripting-kit/test_completion_skill_context_adapters.py.
_OPENAI_COMPAT_SKILL_CONTEXT_EMITS = "messages[system] leading skill context block"


def _skill_context_param(emits: "str | None" = None) -> ParamCapability:
    """``skill_context`` as every adapter handles it: READ, and either
    delivered as ``emits`` or refused before dispatch.

    Refused is still read, for the reason :func:`_output_contract_param`
    gives: reporting the param as dropped would say it went nowhere when it
    stopped the call.
    """
    if emits is None:
        return ParamCapability(
            type="skill-context",
            note="read and refused before dispatch: the harness loads skills itself",
        )
    return ParamCapability(
        type="skill-context",
        emits=emits,
        note=(
            "the materialized block leads the system message, followed by the "
            "caller's system text and then any output-contract instruction"
        ),
    )


#: ``structured_output.contract_emits`` (and ``params.output_contract.emits``)
#: of the two adapters that deliver a contract in this module. Each names the
#: concrete element the delivery produces, asserted byte for byte by
#: tests/llm-scripting-kit/test_completion_contract_conformance.py.
_OPENAI_COMPAT_CONTRACT_EMITS = "messages[system] schema instruction"
_CODEX_CONTRACT_EMITS = "--output-schema <temp schema file>"
_CLAUDE_CONTRACT_EMITS = "--json-schema <canonical schema json>"
_OPENCODE_CONTRACT_EMITS = "stdin schema instruction"


# -- openai-compat (OpenAI-compatible HTTP; OpenRouter is one endpoint it serves) --
#
# OpenAICompatBackend.complete builds chat-completions kwargs directly. It reads
# temperature, max_tokens, timeout_s, user_cache_prefix, client_id and extras
# unconditionally, and effort only for an endpoint whose effort style delivers
# it (the conditional params below) -- never cwd or allowed_tools, which is
# why cwd is not a core param of this seam.

_OPENAI_COMPAT_PARAMS = {
    "max_tokens": ParamCapability(
        type="integer",
        default=None,
        emits="max_tokens",
        note=(
            "unset: omitted for an openrouter.ai endpoint, so the provider's own limit for the model "
            "applies; any other OpenAI-compatible endpoint is sent 4096"
        ),
    ),
    "temperature": ParamCapability(
        type="number",
        default=None,
        emits="temperature",
        note="server/model default when unset; omitted from the request",
    ),
    "timeout_s": ParamCapability(
        type="number",
        emits="timeout",
        note="omitted from the request entirely when None",
    ),
    "max_retries": ParamCapability(
        type="integer",
        default=None,
        emits="client.with_options(max_retries=...)",
        note="SDK retries per call; unset keeps the client default of 2",
    ),
    "user_cache_prefix": ParamCapability(
        type="string",
        default="",
        emits="messages[user].content[0].cache_control",
        note=(
            "when set, the user message becomes a two-part content list with an "
            "ephemeral cache breakpoint on the static prefix"
        ),
    ),
    "client_id": ParamCapability(
        type="string",
        default=None,
        emits="user",
        note="uses the caller id, a process identity when None, nothing when empty",
    ),
    "extras": ParamCapability(
        type="json-object",
        handling=PASSTHROUGH,
        emits="extra_body",
        note=(
            "every key rides as a TOP-LEVEL request parameter, unvalidated and "
            "unfiltered; the adapter makes no claim the provider accepts any of "
            "them"
        ),
    ),
    "output_contract": _output_contract_param(_OPENAI_COMPAT_CONTRACT_EMITS),
    "skill_context": _skill_context_param(_OPENAI_COMPAT_SKILL_CONTEXT_EMITS),
}

#: Params openai-compat emits only for an endpoint whose profile enables them.
#: ``effort`` stays in the family record's dropped_params -- the truth for an
#: endpoint nothing is known about -- and
#: ``endpoint_profile.endpoint_capabilities`` moves it into ``params`` with the
#: concrete emission once an endpoint resolves a delivering effort style.
_OPENAI_COMPAT_CONDITIONAL_PARAMS = {
    "effort": ParamCapability(
        type="string",
        emits="reasoning_effort | chat_template_kwargs.reasoning_effort | reasoning",
        note=(
            "emitted only for a transport entry that resolves a delivering "
            "effort_style (entry effort_style, frontdoor: true, or a declared "
            "routing.effort_style); an effort already in extras (top-level or "
            "chat_template_kwargs) wins verbatim and an explicit null there "
            "sends none; the ninfer style maps high to xhigh; the opt-in "
            "reasoning-object style sends reasoning={effort: value} (none sends "
            "reasoning={enabled: false}) and lets a caller extras.reasoning object win "
            "as a whole"
        ),
    ),
}

OPENAI_COMPAT_CAPABILITIES = Capabilities(
    adapter=ADAPTER_OPENAI_COMPAT,
    params=_OPENAI_COMPAT_PARAMS,
    dropped_params=_dropped(_OPENAI_COMPAT_PARAMS),
    conditional_params=_OPENAI_COMPAT_CONDITIONAL_PARAMS,
    execution_controls=(),
    # A transport adapter exposes no tools, so there is no filesystem write to
    # deny and nothing that could turn one back on. The strongest form of the
    # guarantee and the cheapest: no flag, no sandbox, no checkout.
    guarantees=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
    structured_output=StructuredOutputCapability(
        mode=PASSTHROUGH,
        request_param="extras.response_format",
        result=TEXT_RESULT,
        note=(
            "the adapter neither defines nor validates a schema; it forwards a "
            "caller-supplied response_format through extras and always reads the "
            "result as message.content"
        ),
        # The output-contract path is separate from the legacy passthrough
        # above: the schema is appended to the system message as the exact
        # render_schema_instruction text and the answer is validated at the
        # seam. No response_format is sent, so native-required is not listed.
        policies=(POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_PROMPT,
        contract_emits=_OPENAI_COMPAT_CONTRACT_EMITS,
        # The instruction carries any schema in the supported subset.
        contract_schema_class=SCHEMA_CLASS_SUBSET,
    ),
    system_prompt=SystemPromptCapability(
        mode=NATIVE_ROLE,
        emits="messages[system]",
        note=(
            "a distinct system-role message, not an append: the adapter supplies "
            "the system prompt rather than adding to an existing one"
        ),
    ),
    # The one adapter that DELIVERS skill context. The three harness records
    # carry no block, so each refuses it before dispatch.
    skill_context=SkillContextCapability(
        delivery=DELIVERY_SYSTEM_MESSAGE,
        emits=_OPENAI_COMPAT_SKILL_CONTEXT_EMITS,
    ),
)

# -- claude-cli ------------------------------------------------------------
#
# ClaudeCliBackend.complete builds argv directly. --effort and --disallowedTools
# are conditional. The tool argv has three modes, chosen by allowed_tools:
# no grant (None or "") or backend.text_only emits _CLAUDE_TEXT_ONLY_ARGS; a
# named grant emits _claude_grant_args(grant); ALL_TOOLS emits
# _CLAUDE_ALL_TOOLS_ARGS. A caller's output contract is passed natively as
# --json-schema and read from the envelope's structured_output field. The
# --output-format json flag itself selects claude's TRANSPORT envelope, which the
# adapter parses to reach data["result"], and is not a structured-output channel.

#: The claude-cli flag each ``system_prompt_mode`` emits, consumed by
#: ClaudeCliBackend to BUILD the argv and by the record below to ADVERTISE the
#: menu. One map, both jobs: the advertised ``values`` are its keys and the
#: emitted flag is its value, so a mode cannot be added to the adapter without
#: appearing in the advertisement. It lives on this side of the pair because
#: backends.py already imports this module -- the reverse edge would be a cycle.
#:
#: The two flags are not interchangeable spellings: ``--system-prompt`` makes
#: the caller's text the whole system prompt, while ``--append-system-prompt``
#: adds it to the CLI's own default one.
_CLAUDE_SYSTEM_PROMPT_FLAGS = {
    "replace": "--system-prompt",
    "append": "--append-system-prompt",
}

#: No MCP server from any settings source: ``--strict-mcp-config`` with an
#: EMPTY ``--mcp-config``. The first part of the isolation below, and what an
#: agent session's tool grant adds when the job names no MCP server.
_CLAUDE_NO_MCP_ARGS = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')

#: The isolation that follows the ``--tools`` list whenever ClaudeCliBackend
#: restricts the child's tools: in text-only mode (no grant) and in a named
#: grant. One tuple, two jobs, like the map above: the backend extends argv with
#: it and the record below advertises it, so the two cannot drift.
#:
#: ``--strict-mcp-config`` with an EMPTY ``--mcp-config`` loads no MCP server
#: from any settings source; ``--disable-slash-commands`` disables skills; and
#: ``--permission-mode dontAsk`` denies anything not pre-approved instead of
#: bypassing the check.
#:
#: ``--setting-sources ""`` loads no user, project or local settings and no
#: CLAUDE.md memory. Without it every call injected the user and project
#: CLAUDE.md found from the cwd, which defeated prompt caching: measured
#: 2026-10-21 (17 sonnet calls, cwd inside a repo) ~30.6k cache-write tokens
#: per call against 571 with the flag, same 3,094-token system prompt read.
#: ``--bare`` is not an option: it ignores OAuth login.
_CLAUDE_ISOLATION_ARGS = (
    *_CLAUDE_NO_MCP_ARGS,
    "--disable-slash-commands",
    "--setting-sources",
    "",
    "--permission-mode",
    "dontAsk",
)

#: The tool argv of a ClaudeCliBackend call that grants no tool: its
#: ``text_only`` field is set, or ``allowed_tools`` is None or empty.
#:
#: ``--tools ""`` removes every built-in tool (the Agent tool included), and
#: the isolation above removes MCP servers, skills and settings. Measured
#: 2026-10-07 on Claude Code 2.1.293 (haiku): asked to list every tool it could
#: call and to run ``echo`` if it had a shell, the model answered ``NO_TOOLS``
#: in one turn with no permission denial. Measured 2026-11-18 on Claude Code
#: 2.1.327 through the no-grant argv: the model reported no tools (exit 0, one
#: turn).
_CLAUDE_TEXT_ONLY_ARGS = ("--tools", "", *_CLAUDE_ISOLATION_ARGS)

#: The tool argv of the explicit agent-worker mode, ``allowed_tools=ALL_TOOLS``:
#: every built-in tool and no permission check. It is the argv every call
#: carried before a call with no grant became tool-less. The empty
#: ``--allowedTools`` restricts nothing: the permission-modes reference says
#: "Allow rules have no effect in bypassPermissions". Deny rules still apply
#: ("Deny rules block in every mode, including bypassPermissions").
_CLAUDE_ALL_TOOLS_ARGS = ("--permission-mode", "bypassPermissions", "--allowedTools", "")


def _claude_grant_args(grant: str) -> tuple:
    """The tool argv of a named grant: ``--tools``, ``--allowedTools``, then the isolation.

    ``grant`` is a non-empty ``allowed_tools`` value other than ``ALL_TOOLS``:
    tool names or permission rules separated by commas or spaces
    (``"Read,Grep,Glob"``, ``"Bash(git log *) Read"``). Returns a tuple:

    - ``--tools`` with each distinct tool name once, in the order given, with a
      rule read by the name before its parenthesis. The CLI reference: ``--tools``
      "Restrict[s] which built-in tools Claude can use", and "With ``--tools``
      you get the ones you list".
    - ``--allowedTools`` ("Tools that execute without prompting for
      permission") with every clause of ``grant`` as written, joined by
      commas, except a bare ``Read``, ``Grep`` or ``Glob`` (any case). Those
      three run without approval inside the working directory, and dontAsk
      denies them elsewhere, so leaving them out confines reads to the cwd; a
      bare entry would approve every path. A scoped rule such as ``Read(./**)``
      or ``Read(/abs/**)`` is kept, so the caller gets exactly the scope written.
      The flag is omitted when no clause is left.
    - :data:`_CLAUDE_ISOLATION_ARGS`, whose ``--permission-mode dontAsk``
      "Auto-denies every call that would otherwise prompt". The permission-modes
      reference shows dontAsk with pre-approval for an unattended run: ``claude
      -p ... --permission-mode dontAsk --allowedTools "Bash(npm test)" "Read"``.

    Each tool name (the part of a clause before any parenthesis) must be a
    built-in tool name, ``^[A-Za-z][A-Za-z0-9_]*$``, so nothing else can reach
    ``--tools`` or be read as a flag. ``default`` in any case is refused: the
    help text says ``--tools`` takes "default" for the default set, which would
    hand the child every built-in tool. An MCP tool name (``mcp__...``) is
    refused: the isolation loads no MCP server, so it could never be granted.

    Measured 2026-11-18 on Claude Code 2.1.327 (haiku, dontAsk and the
    isolation, cwd a scratch directory): with ``--tools Read,Grep,Glob`` and
    no ``--allowedTools``, a read in the cwd ran unprompted, Glob and Grep ran,
    and a Read of an absolute path in a sibling directory was denied
    (``permission_denials``, don't-ask mode), exit 0, no hang. With
    ``--allowedTools Read,Grep,Glob`` added, that outside read succeeded,
    which is why the bare three are left out. ``Read(./**),Grep,Glob`` as one
    value confined the Read the same way. A ``Read`` grant had no shell or
    write tool and wrote nothing.

    Tested by the gated live suite ``tests/llm-kit/live/test_claude_grant_live.py``
    (``LLM_KIT_LIVE_CLAUDE=1``; it passed on Claude Code 2.1.328, haiku,
    Windows). Through ``ClaudeCliBackend`` and the real CLI it asserts, by a
    marker that must or must not appear in the answer: with no grant the child
    cannot read a file in the cwd; a bare ``Read`` reads a file in the cwd but
    not an absolute path in a sibling directory, not ``../`` traversal, and not
    a file reached through a symlink (on Windows a junction) in the cwd that
    points outside (skipped where neither can be made); under
    ``Read,Grep,Glob``, Grep finds text in the cwd but not in a sibling
    directory, Glob lists no file of a sibling directory, and with no write or
    shell tool the child creates no file. It does not test a scoped rule, a
    pre-approved Bash or write grant, or any other tool: those are pre-approved
    as granted on the CLI reference's word only.

    Raises ``ValueError`` before dispatch when a rule has unbalanced
    parentheses, when the grant names no tool, when it names ``ALL_TOOLS``
    beside other tools, or when a tool name is refused as above; the message
    names the refused name. The checks are :func:`_claude_grant_clauses`.
    """
    clauses, names = _claude_grant_clauses(grant)
    approvals = tuple(clause for clause in clauses if clause.lower() not in _CWD_READ_TOOLS)
    approve = ("--allowedTools", ",".join(approvals)) if approvals else ()
    return ("--tools", ",".join(names), *approve, *_CLAUDE_ISOLATION_ARGS)


def _claude_grant_clauses(grant: object) -> tuple:
    """Parse and check a named grant; the checks every Claude tool grant shares.

    ``grant`` is a non-empty ``allowed_tools`` value other than ``ALL_TOOLS``.
    Used by :func:`_claude_grant_args` (the completion backend) and
    :func:`_claude_agent_tool_args` (the agent session), so both refuse the
    same values.

    Returns ``(clauses, names)``: every clause as written (split at commas or
    whitespace outside parentheses), and each distinct tool name once, in the
    order given, a rule read by the name before its parenthesis.

    Raises ``TypeError`` when ``grant`` is not a string. Raises ``ValueError``
    when a rule has unbalanced parentheses, when the grant names no tool, when
    it names ``ALL_TOOLS`` beside other tools, or when a tool name is
    ``default`` (any case), an MCP tool name, or not a built-in tool name (see
    :func:`_check_grant_tool_name`); the message names the refused value.
    """
    if not isinstance(grant, str):
        raise TypeError(
            f"allowed_tools must be a string or None, not {type(grant).__name__} "
            f"({grant!r}); nothing was dispatched"
        )
    try:
        clauses = _disallowed_tool_clauses(grant)
    except ValueError as exc:
        raise ValueError(
            f"allowed_tools={grant!r} is not a valid tool grant ({exc}); nothing was dispatched"
        ) from exc
    names = tuple(dict.fromkeys(clause.split("(", 1)[0] for clause in clauses))
    if not names:
        raise ValueError(
            f"allowed_tools={grant!r} names no tool; pass \"\" for no tools; "
            "nothing was dispatched"
        )
    if ALL_TOOLS in names:
        raise ValueError(
            f"allowed_tools={grant!r} names {ALL_TOOLS!r} beside other tools; "
            f"ALL_TOOLS ({ALL_TOOLS!r}) must be the whole value; nothing was dispatched"
        )
    for name in names:
        _check_grant_tool_name(grant, name)
    return clauses, names


def _claude_agent_tool_args(allowed_tools: object) -> tuple:
    """The ``--tools`` argv of a Claude agent session for ``allowed_tools``.

    An agent session always runs with ``--permission-mode bypassPermissions``,
    because an agent worker runs unattended. Under that mode an approval list
    (``--allowedTools``) restricts nothing: the permission-modes reference says
    "Allow rules have no effect in bypassPermissions". ``--tools`` is not a
    permission rule. The CLI reference says it "Restrict[s] which built-in
    tools Claude can use" ("With ``--tools`` you get the ones you list"), so
    the tools left out are absent from the child in every permission mode.
    That ``--tools`` restricts under bypassPermissions rests on that
    documentation only: no test runs the CLI with this argv. The gated live
    suite in ``tests/llm-kit/live/`` covers only the completion path, and
    never starts the CLI with bypassPermissions.

    - ``None`` or ``ALL_TOOLS``: ``()``. The session keeps every tool: the
      agent-worker meaning.
    - ``""``: ``("--tools", "")``, no built-in tool, as on the completion path.
    - Tool names (``"Read,Grep Glob"``): ``("--tools", "Read,Grep,Glob")``,
      each distinct name once, in the order given.

    The caller adds :data:`_CLAUDE_NO_MCP_ARGS` beside a non-empty result when
    the job names no MCP server, so no server from a settings source adds a
    tool outside the grant.

    Raises ``TypeError`` or ``ValueError`` before the spawn for every value
    :func:`_claude_grant_clauses` refuses, and ``ValueError`` for a scoped
    rule such as ``Bash(git log *)`` or ``Read(./**)``: under the bypass the
    CLI does not enforce a rule's scope, so the whole tool would be granted.
    """
    if allowed_tools is None or allowed_tools == ALL_TOOLS:
        return ()
    if allowed_tools == "":
        return ("--tools", "")
    clauses, names = _claude_grant_clauses(allowed_tools)
    scoped = [clause for clause in clauses if "(" in clause]
    if scoped:
        raise ValueError(
            f"allowed_tools={allowed_tools!r} has the scoped rule {scoped[0]!r}; an agent "
            "session runs with --permission-mode bypassPermissions, where a rule's scope "
            "is not enforced, so name whole tools; nothing was dispatched"
        )
    return ("--tools", ",".join(names))


#: Read-only built-in tools that a BARE grant clause leaves unapproved, in
#: lower case. They read inside the working directory without approval, and
#: dontAsk denies a read elsewhere; a bare --allowedTools entry would approve
#: every path. A scoped rule (``Read(./**)``) is approved as written.
_CWD_READ_TOOLS = frozenset({"read", "grep", "glob"})


#: A built-in tool name as ``--tools`` takes it (``Read``, ``NotebookEdit``).
_BUILT_IN_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _check_grant_tool_name(grant: str, name: str) -> None:
    """Refuse one tool name of ``grant`` that ``--tools`` must not receive.

    Raises ``ValueError`` naming ``name`` for ``default`` in any case (the
    CLI's preset for the whole default set), for an MCP tool name (the grant
    loads no MCP server), and for anything that is not a plain built-in tool
    name, such as a name starting with ``-``.
    """
    if name.lower() == "default":
        raise ValueError(
            f"allowed_tools={grant!r} names {name!r}, which --tools reads as the "
            "whole default tool set; name the tools instead; nothing was dispatched"
        )
    if name.lower().startswith("mcp__"):
        raise ValueError(
            f"allowed_tools={grant!r} names the MCP tool {name!r}; --tools takes "
            "built-in tool names only and a tool grant loads no MCP server of its "
            "own (--strict-mcp-config with an empty --mcp-config; an agent session "
            "gets MCP tools only from its mcp_config), so an MCP tool can never be "
            "granted; nothing was dispatched"
        )
    if not _BUILT_IN_TOOL_NAME.fullmatch(name):
        raise ValueError(
            f"allowed_tools={grant!r} names {name!r}, which is not a built-in tool "
            "name (a letter, then letters, digits or underscores); nothing was dispatched"
        )


def _render_args(args: tuple) -> str:
    """An argv fragment as one ``emits`` string; an empty value shows as ``""``."""
    return " ".join(arg if arg else '""' for arg in args)


_CLAUDE_TEXT_ONLY_EMITS = _render_args(_CLAUDE_TEXT_ONLY_ARGS)
_CLAUDE_GRANT_EMITS = (
    "--tools <tool names> [--allowedTools <clauses except a bare Read, Grep or Glob>] "
    + _render_args(_CLAUDE_ISOLATION_ARGS)
)

_CLAUDE_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=900.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="path", emits="subprocess cwd", note="defaults to the process cwd"
    ),
    "effort": ParamCapability(
        type="string",
        emits="--effort",
        note="emitted only when not None; the adapter validates no value menu",
    ),
    "allowed_tools": ParamCapability(
        type="string",
        emits="--tools | --allowedTools | --permission-mode",
        note=(
            "the tool grant, validated before dispatch. None or empty: no "
            "tools (the text-only argv). Tool names or rules: --tools names "
            "those tools, --allowedTools carries every clause except a bare "
            "Read, Grep or Glob (those read only inside the working directory), "
            "and the permission mode is dontAsk (see the tool-grant control). "
            "'*' (ALL_TOOLS): "
            "the agent-worker argv --permission-mode bypassPermissions "
            "--allowedTools \"\", every built-in tool (see permission-bypass). "
            "A non-string, '*' beside other names, unbalanced parentheses, "
            "default, MCP tool names and other non-tool names are refused"
        ),
    ),
    "disallowed_tools": ParamCapability(
        type="string",
        emits="--disallowedTools",
        note=(
            "emitted ONLY when not None; no empty value is sent, because an "
            "empty deny-list restricts nothing"
        ),
    ),
    "system_prompt_mode": ParamCapability(
        type="string",
        default="replace",
        values=tuple(sorted(_CLAUDE_SYSTEM_PROMPT_FLAGS)),
        emits="--system-prompt | --append-system-prompt",
        note=(
            "the menu is advertised because ClaudeCliBackend REJECTS an unknown "
            "mode before dispatch; the two flags differ in meaning, not just "
            "spelling (see system_prompt.emits_by_mode)"
        ),
    ),
    "log_prefix": ParamCapability(
        type="string", default="[llm]", emits="runner log_prefix"
    ),
    "output_contract": _output_contract_param(_CLAUDE_CONTRACT_EMITS),
    "skill_context": _skill_context_param(),
    TEXT_ONLY_PARAMETER: ParamCapability(
        type="boolean",
        default=False,
        emits=_CLAUDE_TEXT_ONLY_EMITS,
        note=(
            "a ClaudeCliBackend field, not a BackendOptions field: "
            "declaration.run sets it when a guarantees requirement admitted "
            "this entry (requirements.arm_requirements). When true the call "
            "carries the text-only argv, which a call with no tool grant "
            "carries anyway, and a non-empty allowed_tools (ALL_TOOLS "
            "included) is refused before dispatch"
        ),
    ),
}

CLAUDE_CAPABILITIES = Capabilities(
    adapter=ADAPTER_CLAUDE_CLI,
    params=_CLAUDE_PARAMS,
    dropped_params=_dropped(_CLAUDE_PARAMS),
    execution_controls=(
        ExecutionControl(
            id="allowed-tools",
            emits="--allowedTools",
            effect=ALLOW,
            source=REQUEST,
            parameter="allowed_tools",
            note=(
                "an APPROVAL list: the CLI reference calls --allowedTools the "
                "tools that execute without prompting, and says to use --tools "
                "to restrict which tools are available. Emitted with a named "
                "grant that has a clause other than a bare Read, Grep or Glob "
                "(beside tool-grant), and with ALL_TOOLS (as \"\", beside "
                "permission-bypass, where allow rules have no effect). Not "
                "emitted when the call grants no tool"
            ),
        ),
        ExecutionControl(
            id="tool-grant",
            emits=_CLAUDE_GRANT_EMITS,
            effect=CONFINE,
            source=REQUEST,
            parameter="allowed_tools",
            note=(
                "emitted when allowed_tools names tools (not ALL_TOOLS): --tools "
                "makes only those built-in tools available (the others are "
                "absent from the child), --allowedTools pre-approves every "
                "clause except a bare Read, Grep or Glob, and the text-only "
                "isolation follows (no MCP servers, no skills, no settings or "
                "CLAUDE.md memory, dontAsk instead of a bypass), so a call that "
                "would prompt is denied rather than prompted. A bare Read "
                "therefore reads only inside the working directory, and Grep "
                "and Glob search only inside it (both tested by the gated live "
                "suite); a scoped rule keeps the scope written. "
                "default, MCP tool names "
                "and anything that is not a built-in tool name are refused "
                "before dispatch. Measured 2026-11-18 on Claude Code 2.1.327 "
                "(haiku): with --tools Read,Grep,Glob and no --allowedTools, a "
                "read in the cwd ran unprompted, Glob and Grep ran, and a Read "
                "of an absolute path in a sibling directory was denied "
                "(permission_denials, don't-ask mode), exit 0, no hang; with "
                "--allowedTools Read,Grep,Glob added, that outside read "
                "succeeded; Read(./**),Grep,Glob confined the Read like the "
                "first shape; a Read grant had no shell or write tool and wrote "
                "nothing. The gated live suite "
                "tests/llm-kit/live/test_claude_grant_live.py asserts that a "
                "bare Read reaches no absolute path, ../ path or symlink "
                "outside the cwd, that Grep and Glob reach no sibling "
                "directory, and that a grant with no write or shell tool "
                "creates no file. Not tested: a scoped rule, and Bash, write "
                "or other pre-approved tools (documented only). No guarantee "
                "is declared for it"
            ),
        ),
        ExecutionControl(
            id="disallowed-tools",
            emits="--disallowedTools",
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "the only real tool DENY channel across the four adapters. "
                "Emitted only when the caller sets the param, so an unset "
                "deny-list reports no control -- suppressing a flag is not a "
                "control. The record claims the EMISSION only: nothing here "
                "establishes that the CLI honors the deny, and the subjects are "
                "caller-supplied rather than a native identifier list, so none "
                "are enumerated. FILESYSTEM_WRITE is carried as a CANONICAL "
                "subject rather than a native one: it names the outcome a "
                "caller can require, and the caller arms it by passing the "
                "deny list"
            ),
        ),
        ExecutionControl(
            id="permission-bypass",
            emits="--permission-mode bypassPermissions",
            effect=BYPASS,
            source=REQUEST,
            parameter="allowed_tools",
            when_value=ALL_TOOLS,
            note=(
                "emitted only for the explicit agent-worker mode "
                "(allowed_tools='*', ALL_TOOLS): every built-in tool runs "
                "without a permission check. Never emitted for a call with no "
                "grant, a named grant, or backend.text_only"
            ),
        ),
        ExecutionControl(
            id="no-session-persistence",
            emits="--no-session-persistence",
            effect=DISABLE,
            subjects=("session-persistence",),
            source=FIXED,
        ),
        ExecutionControl(
            id=TEXT_ONLY_MODE,
            emits=_CLAUDE_TEXT_ONLY_EMITS,
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter=TEXT_ONLY_PARAMETER,
            when_value="true",
            note=(
                "no tools, no MCP servers, no skills, no settings or "
                "CLAUDE.md memory, and no permission "
                "bypass. A guarantees requirement admits claude-cli through "
                "this control and declaration.run arms it, so the caller "
                "passes no flag. A call whose allowed_tools is None or empty "
                "carries the same argv and reports this control unarmed. "
                "Measured 2026-10-07 on Claude Code 2.1.293: "
                "the model reported no callable tool and ran nothing. Measured "
                "2026-11-18 on Claude Code 2.1.327 through the no-grant argv: "
                "the model reported no tools (exit 0, one turn). Neither "
                "allowed-tools nor permission-bypass is emitted in this mode"
            ),
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NONE,
        result=TEXT_RESULT,
        note=(
            "--output-format json is claude's transport envelope, which the "
            "adapter parses to reach data['result']; it is not a caller schema"
        ),
        # The output-contract path is separate from the transport envelope:
        # the canonical schema is passed inline as --json-schema, the CLI
        # returns the answer in the envelope's structured_output field, and
        # the answer is still validated at the seam (finalize_contract).
        policies=(POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_NATIVE,
        contract_emits=_CLAUDE_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_SUBSET,
    ),
    system_prompt=SystemPromptCapability(
        mode=REPLACE,
        emits="--system-prompt",
        modes=(REPLACE, APPEND),
        parameter="system_prompt_mode",
        emits_by_mode={
            REPLACE: "--system-prompt",
            APPEND: "--append-system-prompt",
        },
        note=(
            "REPLACE is the default and makes the caller's text the WHOLE "
            "system prompt; APPEND emits --append-system-prompt, which adds it "
            "to the CLI's own default prompt. That difference is why append is "
            "a separate mode rather than a spelling of the same thing. Both "
            "claims are about the argv this adapter builds; neither asserts "
            "what the CLI then does with the text"
        ),
    ),
)

# -- claude-cli agent session ----------------------------------------------
#
# ClaudeAgentSession (adapters/claude_session.py) runs the same CLI as a
# stream-json agent session: ``claude -p --input-format stream-json
# --output-format stream-json --verbose --model <m> <system flag> <system>
# --permission-mode bypassPermissions [--tools <names>] [--disallowedTools
# <d>] [--effort <e>] [--json-schema <s>] [--strict-mcp-config --mcp-config
# <document>] (--session-id <id> | --resume <id>)``.
# It keeps the session (no --no-session-persistence), has no text-only mode,
# and defaults system_prompt_mode to append. The bypass is fixed: an agent
# worker runs unattended. So allowed_tools is a tool grant through --tools
# (_claude_agent_tool_args), never an --allowedTools approval list, which
# restricts nothing under the bypass. A grant with no job mcp_config carries
# the empty strict MCP document. Same adapter name as the completion record: it
# is the same CLI, selected per job by the ``session: agent`` job option, and
# jobs selection matches an agent job against THIS record instead.

#: The ``emits`` string of the agent session's tool-grant control.
_CLAUDE_AGENT_GRANT_EMITS = (
    "--tools <tool names> [" + _render_args(_CLAUDE_NO_MCP_ARGS) + " when the job sets no mcp_config]"
)

_CLAUDE_AGENT_PARAMS = {
    key: value for key, value in _CLAUDE_PARAMS.items() if key != TEXT_ONLY_PARAMETER
}
_CLAUDE_AGENT_PARAMS["timeout_s"] = ParamCapability(
    type="number",
    emits="session activity deadline",
    note=(
        "the attempt timeout: session activity and every quiescence window run "
        "inside it; the bounded shutdown runs after it"
    ),
)
_CLAUDE_AGENT_PARAMS["allowed_tools"] = ParamCapability(
    type="string",
    emits="--tools",
    note=(
        "the session's tool grant, validated before the spawn (see the "
        "tool-grant control). None or '*' (ALL_TOOLS): no --tools, every tool, "
        "the agent-worker meaning. '': --tools \"\", no built-in tool. Tool "
        "names: --tools with those names. Never --allowedTools: under the "
        "fixed bypassPermissions an approval list restricts nothing. A "
        "non-string, '*' beside other names, unbalanced parentheses, default, "
        "MCP tool names, other non-tool names and scoped rules are refused"
    ),
)
_CLAUDE_AGENT_PARAMS["system_prompt_mode"] = ParamCapability(
    type="string",
    default="append",
    values=tuple(sorted(_CLAUDE_SYSTEM_PROMPT_FLAGS)),
    emits="--system-prompt | --append-system-prompt",
    note="append is the agent-session default; the session keeps Claude Code's own prompt",
)

CLAUDE_AGENT_CAPABILITIES = Capabilities(
    adapter=ADAPTER_CLAUDE_CLI,
    params=_CLAUDE_AGENT_PARAMS,
    dropped_params=_dropped(_CLAUDE_AGENT_PARAMS),
    execution_controls=(
        ExecutionControl(
            id="tool-grant",
            emits=_CLAUDE_AGENT_GRANT_EMITS,
            effect=CONFINE,
            source=REQUEST,
            parameter="allowed_tools",
            note=(
                "emitted and reported only when allowed_tools is '' or names "
                "tools: --tools makes only those built-in tools available (none "
                "for ''), and when the job sets no mcp_config the empty strict "
                "MCP document keeps every settings-sourced MCP server out. Not "
                "emitted for None or '*' (ALL_TOOLS): the session keeps every "
                "tool. Scoped rules are refused, because the fixed bypass does "
                "not enforce a rule's scope. That --tools restricts under "
                "bypassPermissions rests on the CLI reference (--tools "
                "\"Restrict[s] which built-in tools Claude can use\") only: no "
                "test runs the CLI with this argv (the gated live suite covers "
                "the completion path only). No guarantee is declared for it"
            ),
        ),
        ExecutionControl(
            id="disallowed-tools",
            emits="--disallowedTools",
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "emitted only when the caller sets the param; the record claims "
                "the emission, not that the CLI honors the deny"
            ),
        ),
        ExecutionControl(
            id="permission-bypass",
            emits="--permission-mode bypassPermissions",
            effect=BYPASS,
            source=FIXED,
            note=(
                "every agent session; its init event must report it. No "
                "allow-list control is advertised beside it: allow rules have no "
                "effect in bypassPermissions, so the tool grant is --tools"
            ),
        ),
        ExecutionControl(
            id="mcp-servers",
            emits="--strict-mcp-config --mcp-config",
            effect=CONFINE,
            source=REQUEST,
            parameter="session.mcp_config",
            note=(
                "emitted only when the job sets the mcp_config session option: "
                "the document names the MCP servers the session may load, and "
                "--strict-mcp-config asks the CLI to load no server from any "
                "settings source. Absent, no MCP flag is emitted and the CLI's "
                "own MCP settings apply, unless a tool grant carries the empty "
                "document (see tool-grant; this control is not reported then). "
                "The record claims the emission, not that the CLI honors it"
            ),
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NONE,
        result=TEXT_RESULT,
        note=(
            "--output-format stream-json is the event transport; the last "
            "result event carries the answer"
        ),
        policies=(POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_NATIVE,
        contract_emits=_CLAUDE_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_SUBSET,
    ),
    system_prompt=SystemPromptCapability(
        mode=APPEND,
        emits="--append-system-prompt",
        modes=(REPLACE, APPEND),
        parameter="system_prompt_mode",
        emits_by_mode={
            REPLACE: "--system-prompt",
            APPEND: "--append-system-prompt",
        },
        note="APPEND is the agent-session default",
    ),
    call_modes=(MODE_COMPLETION, MODE_WORKSPACE),
)

# -- codex-cli -------------------------------------------------------------
#
# CodexCliBackend delegates argv construction to codex_command, a SEPARATE
# module the capability records do not derive from. Two consequences the
# records must respect: effort is validated by the backend itself
# (model_profiles.check_effort, the same validator CodexAdapter uses), and
# network=False emits an explicit network_access=false under workspace-write
# (so the user's config.toml cannot win) and nothing under other sandbox modes.

#: Codex text-only mode has three parts, because codex's tools come from three
#: places:
#:
#: 1. ``--ignore-user-config``: no ``config.toml``, so no user MCP server
#:    (``-c mcp_servers={}`` MERGES and does not remove them), plugin or app.
#: 2. ``_CODEX_TEXT_ONLY_CONFIG``, the ``-c`` pairs below, with ``-s read-only``
#:    and no network flag: they turn off the feature-gated tool families.
#: 3. A one-model ``model_catalog_json``: the MODEL CATALOG, not the config,
#:    gives a model code mode (``tool_mode: code_mode_only`` -> ``exec`` and
#:    ``wait``), multi-agent v2 (``multi_agent_version`` -> the
#:    ``collaboration.*`` tools, ``spawn_agent`` among them), ``apply_patch``
#:    (``apply_patch_tool_type``), ``tool_search`` and the node REPL. No ``-c``
#:    key or feature flag overrides those, so the backend reads the live
#:    catalog (``codex debug models``), applies ``_CODEX_TEXT_ONLY_CATALOG_SET``
#:    and ``_CODEX_TEXT_ONLY_CATALOG_DROP`` to the requested model's entry, and
#:    passes that entry alone.
#:
#: Measured 2026-10-07 on codex-cli 0.160.0 (gpt-6.1-sol, effort low), asking
#: the model to list every tool and run ``echo`` if it had a shell:
#: - parts 2 only: collaboration.* (spawn_agent ...), functions.exec,
#:   functions.wait, functions.request_user_input;
#: - parts 2+3 without dropping apply_patch: tool_search, three MCP resource
#:   tools, request_user_input, apply_patch;
#: - parts 2+3: the node_repl MCP tools (``mcp__node_repl.js``) from the user
#:   config, the MCP resource tools, request_user_input;
#: - parts 1+2+3 (this mode): ``functions.request_user_input`` only. It asks
#:   the user a question, belongs to no guarantee subject, and codex refuses it
#:   in ``exec`` mode. It is the one tool the mode does not remove.
_CODEX_TEXT_ONLY_CATALOG_SET = {
    "tool_mode": None,
    "multi_agent_version": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
    "node_repl_disabled": True,
}
#: Keys removed from the catalog entry; ``null`` is not accepted for them.
_CODEX_TEXT_ONLY_CATALOG_DROP = ("apply_patch_tool_type",)

_CODEX_TEXT_ONLY_CONFIG = (
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.apps=false",
    "features.plugins=false",
    "features.view_image=false",
    "features.image_generation=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.code_mode_host=false",
    "features.sleep_tool=false",
    "features.tool_suggest=false",
    "features.skill_search=false",
    "features.goals=false",
    'web_search="disabled"',
    "mcp_servers={}",
)

_CODEX_TEXT_ONLY_EMITS = (
    "--ignore-user-config -s read-only "
    + " ".join(f"-c {item}" for item in _CODEX_TEXT_ONLY_CONFIG)
    + " -c model_catalog_json='<temp one-model catalog>'"
)

_CODEX_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=900.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="absolute-path",
        emits="-C",
        note="resolved absolute when None; a relative path is rejected before dispatch",
    ),
    "effort": ParamCapability(
        type="string",
        emits="-c model_reasoning_effort=<value>",
        note=(
            "validated before dispatch against the exact model's measured menu "
            "(llm_kit.registry.model_profiles; gpt-6-luna stops at max, "
            "gpt-6.1-sol also takes ultra, no Codex model takes none); a model "
            "without a measured profile is held to the harness-wide "
            "[low, medium, high, xhigh, max] menu. Omit it to send nothing. "
            "No menu is advertised here because the menu is a property of the "
            "model: the model catalogue reports it"
        ),
    ),
    "log_prefix": ParamCapability(
        type="string", default="[llm]", emits="runner log_prefix"
    ),
    "extras.scratch_dir": ParamCapability(
        type="absolute-path", emits="--add-dir"
    ),
    "extras.add_dirs": ParamCapability(
        # a tuple, not a list: these records are frozen and shared process-wide,
        # so a mutable default would be a shared object a caller could edit
        type="absolute-path-list", default=(), emits="--add-dir (repeated)"
    ),
    "extras.sandbox": ParamCapability(
        type="string", default="workspace-write", emits="-s"
    ),
    "extras.network": ParamCapability(
        type="boolean",
        default=True,
        emits="-c sandbox_workspace_write.network_access=true|false",
        note=(
            "true emits network_access=true; false emits network_access=false "
            "under -s workspace-write (an explicit deny, so the user's "
            "config.toml cannot enable network) and nothing under other "
            "sandbox modes"
        ),
    ),
    "extras.native_subagents": ParamCapability(
        type="string",
        values=("deny",),
        emits="-c agents.enabled=false -c features.multi_agent=false -c features.multi_agent_v2=false",
        note="only deny is supported; other supplied values refuse before dispatch",
    ),
    "extras.output_schema": ParamCapability(
        type="absolute-path", emits="--output-schema"
    ),
    "output_contract": _output_contract_param(_CODEX_CONTRACT_EMITS),
    "skill_context": _skill_context_param(),
    TEXT_ONLY_PARAMETER: ParamCapability(
        type="boolean",
        default=False,
        emits=_CODEX_TEXT_ONLY_EMITS,
        note=(
            "a CodexCliBackend field, not a BackendOptions field: "
            "declaration.run sets it when a guarantees requirement admitted "
            "this entry. Forces -s read-only and network off, ignores the "
            "user config, and runs one extra `codex debug models` call to "
            "build the patched catalog. extras.sandbox other than read-only, "
            "extras.network true, or no model id is refused before dispatch"
        ),
    ),
}

CODEX_CAPABILITIES = Capabilities(
    adapter=ADAPTER_CODEX_CLI,
    # extras IS read, but only for the keys above; every other extras key is
    # dropped, which the note records rather than the coarse field name.
    params=_CODEX_PARAMS,
    dropped_params=_dropped(set(_CODEX_PARAMS) | {"extras"}),
    execution_controls=(
        ExecutionControl(
            id="sandbox-mode",
            emits="-s <value>",
            effect=CONFINE,
            subjects=(FILESYSTEM_WRITE,),
            source=REQUEST,
            parameter="extras.sandbox",
            note=(
                "always emitted, defaulting to workspace-write. The value is "
                "forwarded as given -- the adapter validates no mode menu, so "
                "modes beyond workspace-write and read-only reach the CLI. "
                "It carries FILESYSTEM_WRITE because read-only confines it -- "
                "but the DEFAULT does not, so a caller requiring that subject "
                "must pass extras.sandbox=read-only to arm it"
            ),
        ),
        ExecutionControl(
            id="network-enable",
            emits="-c sandbox_workspace_write.network_access=true",
            effect=ALLOW,
            subjects=("network-egress",),
            source=REQUEST,
            parameter="extras.network",
            when_value="true",
            note="true explicitly enables network access in the workspace-write sandbox",
        ),
        ExecutionControl(
            id="network-disable",
            emits="-c sandbox_workspace_write.network_access=false",
            effect=DENY,
            subjects=("network-egress",),
            source=REQUEST,
            parameter="extras.network",
            when_value="false",
            note=(
                "emitted only under -s workspace-write, the one mode that reads "
                "the key; it overrides a network_access=true in the user's "
                "config.toml. Other sandbox modes emit nothing, and the absence "
                "of a flag is not a control"
            ),
        ),
        ExecutionControl(
            id="native-subagents-deny",
            emits="-c agents.enabled=false -c features.multi_agent=false -c features.multi_agent_v2=false",
            effect=DISABLE,
            subjects=(SUBAGENT_SPAWN,),
            source=REQUEST,
            parameter="extras.native_subagents",
            when_value="deny",
            note="reports the emitted deny configuration; actual harness behavior requires version-specific proof",
        ),
        ExecutionControl(
            id="windows-sandbox-mode",
            emits='-c windows.sandbox="unelevated"',
            effect=CONFINE,
            source=FIXED,
            platform=WINDOWS,
            note=(
                "a compatibility selector without which workspace-write silently "
                "degrades to read-only on Windows; not itself a filesystem "
                "confinement control"
            ),
        ),
        ExecutionControl(
            id="skip-git-repo-check",
            emits="--skip-git-repo-check",
            effect=DISABLE,
            subjects=("git-repo-check",),
            source=FIXED,
        ),
        ExecutionControl(
            id=TEXT_ONLY_MODE,
            emits=_CODEX_TEXT_ONLY_EMITS,
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter=TEXT_ONLY_PARAMETER,
            when_value="true",
            note=(
                "user config ignored, feature-gated tools off, and the "
                "model's catalog entry patched to drop code mode, multi-agent, "
                "apply_patch, tool_search and the node REPL. Measured "
                "2026-10-07 on codex-cli 0.160.0 (gpt-6.1-sol): the model "
                "listed only functions.request_user_input, which belongs to "
                "no guarantee subject and which codex refuses in exec mode"
            ),
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NATIVE,
        request_param="extras.output_schema",
        result=PARSED_RESULT,
        note=(
            "--output-schema is a first-class CLI schema control, and the adapter "
            "parses the -o result file into the normalized structured field when "
            "-- and only when -- a caller schema was sent. Unparseable output "
            "leaves structured None; text still carries the result verbatim"
        ),
        # The output-contract path: the canonical schema is written to a temp
        # file passed as --output-schema, and the answer is validated at the
        # seam. The CLI applies OpenAI strict-mode rules to that schema (a
        # schema without additionalProperties false and a full required list
        # on every object fails the run with a non-zero exit), so the record
        # accepts only openai-strict schemas: selection skips codex for any
        # other schema, and prepare_contract refuses one before dispatch.
        policies=(POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_NATIVE,
        contract_emits=_CODEX_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_OPENAI_STRICT,
    ),
    system_prompt=SystemPromptCapability(
        mode=PROMPT_FOLD,
        separator="\n\n---\n\n",
        emits="stdin",
        note="system text is concatenated ahead of the user text into one prompt",
    ),
    # workspace: codex exec runs inside the absolute -C directory under
    # `-s workspace-write` (measured behavior: a unit writes files there). It
    # is the only adapter that advertises it; the image or build meaning of
    # what gets written is the caller's.
    call_modes=(MODE_COMPLETION, MODE_WORKSPACE),
)

# -- opencode-cli ----------------------------------------------------------
#
# OpencodeCliBackend injects policy as SCALAR settings under opencode's
# permission namespace via OPENCODE_CONFIG_CONTENT -- not deny lists. The
# subjects below are the exact native key paths written, except the canonical
# FILESYSTEM_WRITE carried by the caller-armed deny control: opencode has no
# deny list, so a neutral disallowed_tools value is TRANSLATED into the
# permission scalars that express it.

_OPENCODE_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=120.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="absolute-path",
        emits="--dir",
        note="also the process cwd; --dir is NOT a filesystem-confinement boundary",
    ),
    "disallowed_tools": ParamCapability(
        type="string",
        emits="OPENCODE_CONFIG_CONTENT permission.{edit,bash,task}=deny",
        note=(
            "read as a NEUTRAL tool-deny vocabulary and translated into "
            "opencode's permission scalars, which have no deny-list form. The "
            "edit scalar gates write, edit and patch together; unrecognized "
            "names are ignored rather than guessed at"
        ),
    ),
    "effort": ParamCapability(
        type="string",
        emits="--variant",
        note="any nonempty provider variant; the adapter validates no menu",
    ),
    "log_prefix": ParamCapability(
        type="string", default="[llm]", emits="runner log_prefix"
    ),
    "output_contract": _output_contract_param(_OPENCODE_CONTRACT_EMITS),
    "skill_context": _skill_context_param(),
}

OPENCODE_CAPABILITIES = Capabilities(
    adapter=ADAPTER_OPENCODE_CLI,
    params=_OPENCODE_PARAMS,
    dropped_params=_dropped(_OPENCODE_PARAMS),
    execution_controls=(
        ExecutionControl(
            id="permission-bash-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.bash=deny",
            effect=DENY,
            subjects=(SHELL_EXEC,),
            source=REQUEST,
            parameter="disallowed_tools",
            note="armed by a deny list naming a shell tool",
        ),
        ExecutionControl(
            id="permission-task-request-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.task=deny",
            effect=DENY,
            subjects=(SUBAGENT_SPAWN,),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "the fixed permission-task-deny below already denies task on "
                "every call; this records the same scalar as ALSO reachable "
                "through a caller's deny list"
            ),
        ),
        ExecutionControl(
            id="permission-edit-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.edit=deny",
            effect=DENY,
            subjects=(FILESYSTEM_WRITE,),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "armed by a caller-supplied deny list naming any write tool. "
                "Verified 2026-09-05 on opencode 1.18.25 with a two-arm check: "
                "denied, the agent reports read-only tools and writes nothing; "
                "undenied, the same prompt and model create the file. So the "
                "deny survives --auto, which approves only what is not already "
                "denied"
            ),
        ),
        ExecutionControl(
            id="permission-external-directory-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.external_directory=deny",
            effect=DENY,
            subjects=("permission.external_directory",),
            source=FIXED,
        ),
        ExecutionControl(
            id="permission-task-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.task=deny",
            effect=DENY,
            subjects=("permission.task",),
            source=FIXED,
            note=(
                "task lives in opencode's PERMISSION namespace; this is not a "
                "tool allow/deny list"
            ),
        ),
        ExecutionControl(
            id="agent-permission-external-directory-deny",
            emits="OPENCODE_CONFIG_CONTENT agent.build.permission.external_directory=deny",
            effect=DENY,
            subjects=("agent.build.permission.external_directory",),
            source=FIXED,
        ),
        ExecutionControl(
            id="agent-permission-task-deny",
            emits="OPENCODE_CONFIG_CONTENT agent.build.permission.task=deny",
            effect=DENY,
            subjects=("agent.build.permission.task",),
            source=FIXED,
        ),
        ExecutionControl(
            id="pure-mode",
            emits="--pure",
            effect=DISABLE,
            subjects=("external-plugins",),
            source=FIXED,
        ),
        ExecutionControl(
            id="auto-approve",
            emits="--auto",
            effect=BYPASS,
            source=FIXED,
            note=(
                "auto-approves permissions not explicitly denied above; shell "
                "remains available"
            ),
        ),
        ExecutionControl(
            id="agent-selection",
            emits="--agent build",
            effect=CONFINE,
            subjects=("agent.build",),
            source=FIXED,
            note="the agent is fixed, not caller-selectable",
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NONE,
        result=TEXT_RESULT,
        note="--format json is deliberately unused; stdout is read as the answer",
        # The output-contract path: the exact schema instruction is appended
        # to the system half that is folded into the stdin prompt, and the
        # answer is validated at the seam. native-required is not listed.
        policies=(POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_PROMPT,
        contract_emits=_OPENCODE_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_SUBSET,
    ),
    system_prompt=SystemPromptCapability(
        mode=PROMPT_FOLD,
        separator="\n\n---\n\n",
        emits="stdin",
    ),
)


ADAPTER_CAPABILITIES = {
    OPENAI_COMPAT_CAPABILITIES.adapter: OPENAI_COMPAT_CAPABILITIES,
    CLAUDE_CAPABILITIES.adapter: CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES.adapter: CODEX_CAPABILITIES,
    OPENCODE_CAPABILITIES.adapter: OPENCODE_CAPABILITIES,
}


def adapter_capabilities() -> dict:
    """Every adapter family's advertisement, keyed by the backend's own name.

    Keyed by the current names. Look one up with
    :func:`llm_kit.registry.adapter_names.lookup_by_adapter` to also accept the
    retired ``openrouter`` spelling.
    """
    return dict(ADAPTER_CAPABILITIES)


def __getattr__(name: str) -> Any:
    """Keep the retired ``OPENROUTER_CAPABILITIES`` name resolving, with a warning."""
    if name == OLD_CAPABILITIES_NAME:
        warn_renamed(__name__, OLD_CAPABILITIES_NAME, NEW_CAPABILITIES_NAME)
        return OPENAI_COMPAT_CAPABILITIES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ADAPTER_CAPABILITIES",
    "adapter_capabilities",
    "OPENAI_COMPAT_CAPABILITIES",
    "CLAUDE_CAPABILITIES",
    "CLAUDE_AGENT_CAPABILITIES",
    "CODEX_CAPABILITIES",
    "OPENCODE_CAPABILITIES",
    "_CLAUDE_SYSTEM_PROMPT_FLAGS",
    "_CLAUDE_TEXT_ONLY_ARGS",
    "_CODEX_TEXT_ONLY_CATALOG_DROP",
    "_CODEX_TEXT_ONLY_CATALOG_SET",
    "_CODEX_TEXT_ONLY_CONFIG",
]
