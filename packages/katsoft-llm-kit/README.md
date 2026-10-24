# katsoft-llm-kit

LLM access, durable agent jobs and batch content pipelines in one library.
The import package is `llm_kit`; the console script is `llm-kit`.

```
pip install katsoft-llm-kit
pip install "katsoft-llm-kit[sdk,jobs,config]"
llm-kit --help
```

The library needs no AI harness, Claude Code, bootstrap or plugins-kit to
install and use.

The base install depends on `katsoft-contracts` (execution-event envelope,
interrupt contract, model declaration parser) and nothing else. Extras:
`config`, `jobs` and `pipeline` bring PyYAML; `sdk` brings the OpenAI client
(the OpenRouter HTTP path); `local` brings psutil; `frontdoor` brings
FastAPI, uvicorn and httpx.

## Supervised local jobs

`llm_kit.jobs.supervisor` adds opt-in local process ownership in version 1.6.
It requires the `local` extra. The existing job ledger and CLI remain unchanged.

The public functions are `start`, `inspect`, `adopt` and `cancel`.
`JobProcessSpec` declares the command, working directory, deadline and optional ledger cancellation command.
`start` returns a `ProcessHandle` after the durable dispatch handshake.
Handles and status records have JSON-compatible `to_dict()` methods.
`ProcessHandle.from_dict()` restores a saved handle.

Managers can restart while a detached supervisor retains its job.
`adopt` inspects a live supervisor or stops a vanished supervisor's remaining processes.
Cancellation stops the owned tree before the ledger cancellation command runs.
Survivors and failed ledger cancellation retain the resource lease and report `indeterminate`.

Use one shared `lease_path` for all supervised commands that write the same ledger.
Ordinary jobs commands do not participate in this opt-in lease.
Keep run directories private and machine-local. They contain command arguments and explicit environment overrides.
The supervisor does not supply harness permissions, task policy or acceptance evidence.

POSIX ownership covers recorded descendants and the command's session.
A descendant that starts a separate session and reparents before observation can escape that boundary.
Windows uses a kill-on-close Job Object and refuses dispatch if assignment fails.
These are process lifecycle controls, not a security sandbox.

## Codex execution controls

Job `options.extras` and `BackendOptions.extras` accept `native_subagents: "deny"`.
The Codex backend emits `agents.enabled=false`, `features.multi_agent=false` and `features.multi_agent_v2=false`.
Other non-null values refuse before dispatch.
With `sandbox: "workspace-write"`, `network: false` emits an explicit `sandbox_workspace_write.network_access=false` override.
This override prevents a user configuration from silently enabling workspace network access.
Capabilities report these emitted controls. Callers still need version-specific harness evidence for their permission policy.

## Scoped job denials

From version 1.7.1, a Claude job can deny `Agent,Task` without disabling its ordinary tools.
This source expands canonical `subagent-spawn` denial to name `Workflow` and `SendMessage`.
Claude Code 2.1.310 exposes workflow agent creation and stopped-agent resumption through these tools.
Scheduling tools such as `CronCreate` and `ScheduleWakeup` are separate caller policy; they are not child-agent aliases.
Broad write or shell denial requirements still select text-only mode.
A scoped rule such as `Bash(git push --force*)` keeps its original argument filter.
Scoped rules and unknown tool names require a literal `disallowed-tools` control and matching response provenance.
Adapters without that control are refused before dispatch.
Filters must have balanced parentheses. Separators apply only outside filters.
Mixed floors retain their whole-tool guarantees: `Write,Bash(git push *)` still requires filesystem-write denial.
The existing OpenCode permission mapping is unchanged.
These records describe emitted controls; verify the actual harness behavior for your policy.

## Runtime mode

`llm_kit._runtime.mode()` reports how the library is deployed. This
distribution is `"package"`: the contracts come from `katsoft-contracts`.
`llm_kit._runtime.fleet()` reports whether `bootstrap_lib`, the plugin
marketplace's shared library, imports in the same environment. With it, the
features that need it (claudx propagation, skill context,
the code-review lane) work and the model registry reads the layered config.
Without it, features that require the shared library raise
`UnsupportedInPackageMode`, Codex reachability uses a PATH check, and the model
registry reads the shipped defaults; point `MODEL_ENDPOINTS_REGISTRY` at a
registry file to change them. The Codex adapter itself works in package mode
and does not require Claude Code, bootstrap or plugins-kit.

Development source trees also use package contract routing.
Their marker has `MODE = "source"` and carries no release identity.
Release wheels have `MODE = "package"` with a verified version, commit, and tag.
`_runtime.mode()` returns `"package"` for both standalone forms.
A source tree beside installed `katsoft-llm-kit` metadata is refused as ambiguous.

Do not install this distribution into an environment that also holds a
path-linked copy of the `llm_kit` source tree.

## CLI executables

The library starts the Claude and Codex command-line tools by name from PATH. Two environment
variables name a different build, each either a bare command name (looked up on PATH) or an
absolute path to an executable file:

- `LLM_KIT_CLAUDE_EXECUTABLE` replaces `claude`.
- `LLM_KIT_CODEX_EXECUTABLE` replaces `codex`.

A value that is empty, is a relative path, or names nothing runnable raises an error naming the
variable; the library never falls back to PATH in that case. Unset, the tool is `claude` or
`codex` on PATH.

## Consent

The library makes no model calls of its own: the code that uses it does, so the library gates
nothing on a caller's behalf. Every model entry is classified `local`, `subscription` or
`metered` (paid or unverified billing), and the class and the reason for it are reported
(`llm-kit models catalog`, `models describe`, and protocol 2 responses). A protocol 2
request that does not opt in reports a `claude` or `codex` entry's class as `null`, because the
authentication probe that decides it runs only for an opted-in request.

The optional consent layer applies a policy you configure only when you
opt in, and you may build your own policy instead. To opt in, pass `--consent [--consent-scope NAME]` to
`llm-kit models describe`, send `"consent": true` or `"consent": {"scope": "NAME"}` in a
protocol 2 request, or call `llm_kit.registry.consent.policy(scope=None)`. To apply your own
filter instead, pass `--allow local,subscription` (not combinable with `--consent`).

With the layer applied and no file, `local` and `subscription` are on and `metered` is off.
One file, `$XDG_CONFIG_HOME/llm-kit/consent.json` (`~/.config/llm-kit/consent.json` by
default; `LLM_KIT_CONSENT_FILE` overrides), holds the global policy and optional named scopes.
A scope can only restrict the global policy; a class the scope omits inherits.

```
llm-kit consent show [--scope NAME] [--json]
llm-kit consent set [--scope NAME] --local|--subscription|--metered on|off
```

## Output cap

A model row of the project model file (`.llm-kit/models.yaml`) may declare `max_output_tokens`
(a positive integer; `llm-kit models catalog` shows it). A protocol 2 call to an OpenAI-compatible
row sends, in order: the request's `max_tokens`, the row's `max_output_tokens`, the row's
`request_defaults.extras.max_tokens`, then on openrouter.ai no cap (the model's full allowance)
and on other endpoints 4096. Explicit values are never clamped. When a call fails after the
provider answered, the protocol 2 `error.finish_reason` is the provider's raw stop string;
`"length"` means the cap ended the answer. Full statement: `completion-seam-contract.md` in the
plugin references.

## Effort style `reasoning-object`

An endpoint (or a front-door `routing`) may opt in with `effort_style: reasoning-object`
to send an effort as the OpenRouter `reasoning` object. The default `top-level` style and
every other style are unchanged; nothing selects the new style implicitly.

- A typed effort `low|medium|high|minimal|xhigh|max` sends `{"reasoning": {"effort": value}}`;
  `none` sends `{"reasoning": {"enabled": false}}`. An effort outside that vocabulary is refused
  before any request. A typed effort of `None` sends nothing. An exact-model menu
  (`llm-kit models catalog`) can still narrow what a model accepts.
- A caller `extras["reasoning"]` wins as a whole over the typed effort. A mapping is sent
  intact (`exclude`, `max_tokens` and other members included, and it is copied, not mutated);
  `None` sends no `reasoning` at all; any other value, or a mapping beside a non-null
  `reasoning_effort` (top-level or in `chat_template_kwargs`), is refused before any request.
  This holds even when the typed effort is `None`.
- The front door treats an incoming `reasoning` object as opaque and forwards it intact. An
  incoming scalar `reasoning_effort` (top-level or in `chat_template_kwargs`) is translated to the
  nested object. A malformed object or a scalar beside an object returns HTTP 400.
- A project-file row on such an endpoint may not default `request_defaults.extras.reasoning`.

MiMo (`xiaomi/mimo-v2.6-pro`) exposes only the measured `none` control on a
`reasoning-object` endpoint. Other styles expose an empty menu for that model.
A live Novita probe on 2026-10-24 used the same prompt and a 4096-token cap.
`reasoning.enabled=false` returned 0 reasoning tokens. `reasoning.enabled=true` returned 596.
This evidence establishes an off switch. It does not establish positive effort levels.
A typed effort of `None` omits the control and retains the provider's default behavior.

## Source

The source lives in the katsoft-libs repository (`packages/katsoft-llm-kit/src/llm_kit`).
Releases are tagged `katsoft-llm-kit-v<version>` in the repository named by the
`Source` project URL.

## Protocol 2 workspace requests

Protocol 2 accepts optional `images`, `codex_config` and `watch_files` fields.
`images` is a list of relative paths to existing regular files without symlink
components. Codex attaches them to the initial prompt with `-i`; adapters without
image input refuse the request. In workspace mode paths are relative to
`working_dir`; in completion mode they are relative to `project_root`.

`codex_config` is a closed object. It accepts
`"shell_environment_policy.inherit": "none"` or `"core"`, and
`"approval_policy": "never"`. Each field is optional, and omitted fields emit
no configuration override. Request values override the matching row default in
`request_defaults.extras.codex_config`. Every Codex protocol 2 call requires the effective sandbox to be
`read-only` or `workspace-write`. Other keys and values are refused; callers cannot pass raw `-c` text through the request.

`watch_files` is an optional, non-empty list of normalized relative POSIX paths
in workspace mode. The receipt hashes the union of this list and `expect_files`
by direct file lookup. The result reports `manifest.scope: "watch"` and the
paths it checked. This scope does not detect writes to unlisted files. Deletions
are not reported in either `all` or `watch` scope. Omitting `watch_files` keeps
the full-tree receipt and reports `manifest.scope: "all"`.

Keep `.llm-kit/models.yaml` in the consumer's own worktree, set
`project_root` to that worktree, and use `watch_files` for a focused receipt.
For isolated work, prepare a separate project root with its own model file and
endpoint configuration, then point the request at that root. For example:

```json
{
  "protocol": 2,
  "project_root": "scratch-run",
  "file": ".llm-kit/models.yaml",
  "model": "writer",
  "prompt": "Create the requested files",
  "mode": "workspace",
  "working_dir": ".",
  "watch_files": ["result.md"]
}
```

`working_dir` must be the project root or an existing descendant directory,
with no symlink components. The library does not add an external writable
directory to Codex.
