# Changelog: katsoft-llm-kit

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar". An entry records which bar lines pass for the pieces that version adds or changes.

## Unreleased

Graph pages: `llm-kit graph render` draws graph files as one self-contained HTML page.

Added:

- `llm-kit graph render [--project DIR] [GRAPH ...] [-o FILE] [--title TEXT]` (`llm_kit.graph.commands.render`). With no graph it renders every graph in the project config with a graph browser; with graph names or graph file paths it renders those (a path needs no project config).
- `llm_kit.graph.render.catalog`: `build_catalog`, `project_catalog` and `CATALOG_FORMAT`. A catalog holds the entry graphs, every graph file they run (each once) and the nodes that run each file.
- `llm_kit.graph.render.page`: `render_page` and `write_page`. The page embeds the catalog, the browser script and the layout library (elkjs 0.12.1, Eclipse Public License 2.0, unmodified, licence text beside it) and makes no network requests.
- The format `llm-kit.graph-catalog/v1`: `llm_kit.schema.formats.GRAPH_CATALOG_V1`, `graph-catalog.v1.schema.json`, the shared cases `tests/formats/graph-catalog/cases.json` and its section in `docs/reference/llm-kit-formats.md`.
- The example `examples/render_graphs.py`.

Quality bar for the pieces above:

1. Public API documented: pass. Each new public name has a docstring; `public-surface.json` lists `llm_kit.graph.render.catalog`, `llm_kit.graph.render.page` and `GRAPH_CATALOG_V1`, and `test_public_surface_lists_the_graph_names` and `test_public_surface_lists_the_format_names` check them.
2. Semver release with a changelog: pending; this entry gets its version at release.
3. Tests for each public function: pass. `tests/llm-kit/graph/test_render.py` calls `build_catalog`, `project_catalog`, `render_page`, `write_page` and the command's `run`.
4. No client names, prompts or data: pass. The tests use the `demo.*` fixture project and graphs written in the test.
5. Examples run without a client app: pass from the source tree (`test_the_example_runs_from_the_source_tree`); the wheel check runs at release.
6. JSON Schema for each file format: pass. Catalog writer tests validate against `graph-catalog.v1.schema.json`; the shared cases run in `test_the_shared_catalog_cases`.
7. Gate before adoption: no client has adopted it.
8. Security properties are tested: pass. Text from a graph file cannot close the page's script element (`test_render_page_text_from_a_graph_cannot_close_its_script_element`, shown to fail with the `<` and `>` escapes removed), and the page loads nothing from the network (`test_render_page_embeds_the_catalog_and_loads_nothing_from_the_network`). The browser script sets all catalog text as text, never as markup; no test runs the script in a browser.

## 1.41.0

A preference-list front-door entry is classified by what it lists. 1.40.0 looked for a routing group named like the whole list, found none, and classed every list entry `metered`, so a consent policy that refuses metered entries refused a list of local models.

Added:

- `preference_list_members(entry, entries)` in `llm_kit.registry.model_endpoints`: each listed name of a list entry, in order, with the deployment ids it reaches. `frontdoor_group_members` now uses it.

Changed:

- The cost class of a list entry (`llm_kit.registry.cost.classify`): `local` when every deployment of every listed name is unmetered and self-owned; `metered` when one is not, or a listed name reaches no deployment.

Quality bar:

1. Public API documented: pass, with the 1.33.0 exception. `preference_list_members` has a docstring; `registry.model_endpoints` is not in `covers` and is not listed whole, so its names (like `frontdoor_group_members`) stay unlisted.
2. Semver release with a changelog: pass. Version 1.41.0 (a new public function and a classification fix); this entry.
3. Tests for each public function: pass. `test_frontdoor_members_of_a_preference_list_follow_each_name` calls `preference_list_members`; `test_frontdoor_preference_list_classifies_by_every_listed_name` (`test_cost_class.py`) pins the classification.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. No file format changed.
7. Gate before adoption: pass, with the line 1 exception.
8. Security properties are tested: pass. The cost class gates paid use; its list rule has the test above.

## 1.40.0

Model preference lists steer by request size, and their failover order is fixed. The door estimates a list request's tokens on the high side and skips a listed name whose deployments cannot hold it, so one list (`"fast-64k,big-240k"`) sends small requests to the first name and large ones to the second. The estimate never refuses: when it would leave nothing, the list routes unsteered. Fixes 1.39.0's failover order: the other listed models on a swapper host moved to the end of the walk, so when the chosen model failed, the request reached every later tier (a paid one included) before the same host's other local model. They now stand by right after the chosen model: a standby serves once the chosen model has failed for the request, and never because the chosen model is full, which would swap a busy host.

Added:

- Size steering for a list request (private `_estimated_tokens`, `_steer_by_size`; `_ESTIMATE_CHARS_PER_TOKEN` = 3). A single name routes as before.

Changed:

- `frontdoor_group_members` expands a plain group name in a list with no size filter, as the door reads it (1.39.0 applied the list entry's `context_window`, so a 240k list started its fall-through at a size-only tier).
- The private `_preference_tiers` also returns the standby map; the private `_active_tiers` drops a standby until its host's choice is excluded.

Quality bar:

1. Public API documented: pass. No public name changes; the README states the size and standby rules.
2. Semver release with a changelog: pass. Version 1.40.0 (new request behaviour for a list, plus a fix); this entry.
3. Tests for each public function: pass. `test_frontdoor_preference.py` pins size steering (small request, request too big for the first name, `max_tokens` counted, the estimate never refusing), the standby placement, `test_a_standby_serves_only_after_its_host_choice_is_excluded` the activation rule, and `test_failed_choice_falls_back_through_the_list` the failover through `create_app`.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. No file format changed.
7. Gate before adoption: pass.
8. Security properties are tested: pass. No security property changed.

## 1.39.0

The front door accepts a model preference list. A request whose `model` is a comma-separated list of door names (`"fast,slow"`) may be served by any of them; on a swapper host (deployments sharing a `base_url`) the door keeps a busy host on the listed model it is already serving and sends an idle host the first listed one, so mixed callers stop forcing model swaps. A request with one name routes exactly as before. README "Front door: model preference lists" states the rule.

Added:

- `create_app`'s `/v1/chat/completions` reads a comma-separated `model` as a preference list. An unknown name is a 404 naming it, an empty item a 400. The access log's `group` and `size` are the serving deployment's.
- `frontdoor_group_members` expands a `frontdoor: true` entry whose `model` is a preference list into the members of each listed name, in list order: a name another front-door entry declares has that entry's members (at its size); any other name is a group at the list entry's `context_window`. A client's fall-through past a list entry therefore reaches the same deployments the door would.

Changed:

- The front door walks a precomputed list of tiers (private `_order_tiers`, `_preference_tiers`); a single name's tiers are its `routing.order` groups, as before. A runtime deployment records when it last became busy (`busy_since`).

Quality bar:

1. Public API documented: pass. No public name is added; `create_app`'s request contract is documented in the README, and `frontdoor_group_members`'s docstring states the list rule.
2. Semver release with a changelog: pass. Version 1.39.0 (a minor release: new request behaviour, no change for a single name); this entry.
3. Tests for each public function: pass. `test_frontdoor_preference.py` covers the rule through `_preference_tiers` (idle host, busy host, busy with the first choice, two in flight, another host busy) and through `create_app` (idle list request, failover through the list, unknown and empty items); `test_frontdoor_members_of_a_preference_list_follow_each_name` covers `frontdoor_group_members`; the existing front-door tests pass unchanged.
4. No client names, prompts or data: pass. Generic hosts and names.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. No file format changed.
7. Gate before adoption: pass. Lines 1 to 6 and 8 pass.
8. Security properties are tested: pass. No security property changed; the guard tests pass unchanged.

## 1.38.0

Hardening follow-ups to the 1.37.0 security release. An agent session's tool grant now restricts the tools it names, no Claude child inherits the Claude Code session marker, the Windows batch-launcher guard covers more characters and one more spawn path, and the live suite proves each refusal it reports. Several of these change behaviour: a call or a job that worked before can now be refused or run with fewer tools. Read the first list before upgrading.

Changed behaviour (read before upgrading):

- An agent session's (`ClaudeAgentSession`) `allowed_tools` is now passed as `--tools <names>`, not `--allowedTools`. The session always runs with `--permission-mode bypassPermissions`, where an approval list restricts nothing, so in 1.37.0 a grant did not restrict the session at all. Now the session has only the named built-in tools. When the job sets no `mcp_config`, a grant also carries `--strict-mcp-config --mcp-config {"mcpServers":{}}`, so no settings-sourced MCP server adds a tool. An agent job that used tools outside its grant now runs without them.
- `allowed_tools: ""` leaves an agent session with no built-in tool (`--tools ""`). `None` and `ALL_TOOLS` (`"*"`) keep every tool, as before.
- An agent-session grant is refused before the spawn when it names a scoped rule (`Bash(git log *)`, `Read(./**)`: the bypass does not enforce a rule's scope), `default` in any case, an MCP tool name (`mcp__...`, also when the job sets `mcp_config`: `--tools` takes built-in names only), or `Agent`, `Task` or `Skill` in any case (a subagent or a skill may use tools outside the grant). A job that wants subagents or skills passes no grant or `ALL_TOOLS`.
- The agent record's ledger control is `tool-grant` (effect CONFINE), reported when the argv carries `--tools`. The `allowed-tools` control is no longer on the agent record or in an agent attempt's `execution_controls_applied`.
- On a Windows batch launcher (`claude.cmd`, `codex.cmd`, `.bat`), the refused argv characters are now `&`, `|`, `^`, `<`, `>`, `%` and `!`, plus carriage return and line feed (1.37.0: the first five and the line breaks, on the Claude paths only). An argv element that contains `%` or `!` is refused before a process starts, for both the Claude and the Codex CLI: cmd.exe expands `%VAR%`, and `!VAR!` when delayed expansion is on, which the registry can turn on for every `cmd.exe /c`. A Codex argv element with a line break is now refused too (the Claude paths already refused it). A system prompt with these characters still travels by file. `claude.exe`, `codex.exe` and other platforms are unchanged.
- The relay's resumed Claude turn (`ResumeRunner`) passes the same batch-launcher check: on a Windows `claude.cmd`, a `claude_argv` element with a refused character is refused before the spawn.
- A custom `ClaudeCliBackend` runner is now called with `env=` (the Claude child environment); a runner that takes neither `env` nor `**kwargs` fails. A custom `ClaudeCli` runner now always receives `env=` as a dict, never None.
- `CLAUDECODE` no longer reaches any Claude child: the completion backend, the agent session, the relay's resumed turn, every `ClaudeCli` call (the `--bg` launch, `agents`, `stop`, `rm`, `respawn`, `--version`, the preflight probes), `claude auth status` (`harness_auth.claude_auth`) and `claude --version` (`dispatch.reachability.check_harness`). A command the caller names (a job's contract command, a compare scorer, a supervised process, a graph `llm.subprocess` node) is not a Claude spawn of this package and keeps the environment it had; a caller whose command starts `claude` inside a Claude Code session removes `CLAUDECODE` itself. On Windows the name matches in any case. A `--bg` run's recorded overlay that names `CLAUDECODE` raises `ValueError` instead of putting it back.
- `validate_document` (Python) checks the sandbox extras of the `requests` profiles after every profile's structure, in sorted order of the profile names, the code point order `resolveRequest` uses (1.37.0: in the file's key order, interleaved). A file with more than one refused profile now reports the first by name, as `@katsoft/llm-kit-config` `resolveRequest` does; a file with a structural problem in one profile and a refused extra in an earlier one now reports the structural problem.

Added:

- `tests/llm-kit/live/test_claude_grant_live.py`, the gated live suite (`LLM_KIT_LIVE_CLAUDE=1`, marker `live_claude`). It runs the real `claude` through `ClaudeCliBackend` with the library-built argv (output format swapped to stream-json so it can read the CLI's events) and asserts what a completion grant confines: the init event's tool list for a tool the grant leaves out, and for a tool it includes, that the model called it toward the outside path and that the CLI refused the call. A run in which the model makes no such call is an expected failure marked INCONCLUSIVE, never a pass. `docs/reference/package-release.md` requires it before a release that changes a Claude spawn path.
- Two shared cases in `tests/formats/project-models/tracked-extras.json` (36 in all): two refused profiles in non-sorted file order, and two refused rows.

Changed:

- `llm_kit.adapters.claude_env._claude_child_env` (a private name) is the one definition of a Claude child's environment. `llm_kit.adapters._cmd_shell` (private) is the one definition of the batch-launcher character set and check; its docstring records the decision for each cmd.exe special character, from an echo launcher with npm's cmd-shim structure: `"`, `(`, `)`, `,`, `;`, `=` and a trailing backslash arrive unchanged and stay allowed.
- The capability notes and docstrings of the agent session say plainly what a grant does not confine: the session still loads every settings source (hooks, enabled plugins) and the plugins' skills and MCP servers. No behaviour change there.
- The README's "Claude tool modes" states what the live suite measured and the agent-session grant.

Live suite: `LLM_KIT_LIVE_CLAUDE=1 uv run --extra dev pytest tests/llm-kit/live` on Claude Code 2.1.328 (haiku, Windows 11), 9 passed, 0 expected-failure (INCONCLUSIVE) and 0 failed: every negative case saw a refused attempt (no-grant child had no tools; Read refused for an absolute outside path, a `../` path and a link out; Grep and Glob refused on a sibling directory; no write or shell tool exposed).

Rests on documentation only, not on a test: that `--tools` restricts an agent session under `--permission-mode bypassPermissions`. This rests on the Claude Code CLI reference, and no test runs it: no test starts the CLI with that argv (the live suite runs the completion path only, under `dontAsk`), so the agent-session grant, its refusals of scoped rules and of `Agent`, `Task` and `Skill`, and the statement that settings sources and plugin MCP servers still load are not measured.

Quality bar for the pieces above:

1. Public API documented: pass, with the 1.37.0 exception unchanged. No public name is added, so `public-surface.json` lists no new entry; its purpose now says that private modules and names (`llm_kit.adapters.claude_env._claude_child_env`, `llm_kit.adapters._cmd_shell`) are never listed, and `test_public_surface_lists_no_private_module_or_name` (`test_formats.py`) checks it. The docstrings of the changed public names state the new behaviour: `ClaudeAgentSession` (module docstring), `compose_worker_environment`, `ClaudeCli` (through `_invoke`) and `claude_auth`. `claude_auth`, `check_harness` and `ResumeRunner` are in modules the file does not list, as before.
2. Semver release with a changelog: pass. Version 1.38.0 in `pyproject.toml` (a minor release: security hardening that changes behaviour, listed first; no public name is removed); this entry.
3. Tests for each public function: pass. `test_claude_spawn_env.py` calls `ClaudeCliBackend.complete`, `ClaudeAgentSession.run`, `ResumeRunner._run_claude`, `dispatch_wave`, each `ClaudeCli` call, `claude_auth`, `check_harness` and `compose_worker_environment`; `test_capability_invariants.py` calls `ClaudeAgentSession.build_argv` and `applied_controls`; `test_codex_tracked_extras.py` runs `validate_document` over the shared cases; `test_cmd_shell.py` and `test_claude_batch_shim.py` call `check_batch_shim_argv`, `batch_shim_spawn_argv` and the Codex builders. `uv run --extra dev pytest` passes (5196 passed, 480 skipped, 0 failed in 736 s on Windows 11; 867 passed, 15 skipped in the nine POSIX-relevant test files under WSL Ubuntu).
4. No client names, prompts or data: pass. Generic paths, names and prompts only.
5. Examples run without a client app: pass. No example changed; `tests/packaging/test_examples.py` runs each example from the built wheel (`WHEEL_CLOSURE_SOURCE=local uv run --extra dev pytest tests/packaging`: 57 passed, 1 skipped).
6. JSON Schema for each file format: pass. No file format changes; the two new shared cases are read by both readers' existing tests.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above; line 8 passes except for the one untested claim it names; no new piece is offered for adoption.
8. Security properties are tested: pass for every claim below; one claim has no test and is not claimed as tested: that `--tools` restricts an agent session under `bypassPermissions` (CLI reference only, stated above; no test runs it). Each other claim of this release and its test:
   - An agent grant is `--tools` and reported as `tool-grant`, with no ALLOW control beside the bypass: `test_no_record_lists_an_allow_control_beside_an_unconditional_bypass`, `test_the_agent_record_advertises_the_grant_as_confining` (`test_capability_invariants.py`) and `test_a_grant_runs_the_session_with_only_the_granted_tools_and_reports_the_tool_grant` (`test_claude_session.py`, POSIX).
   - The refused agent grants: `test_an_agent_grant_the_completion_path_would_refuse_is_refused_before_the_spawn` (scoped rules, `default`, MCP names), `test_an_agent_grant_refuses_an_mcp_tool_name_also_when_the_job_sets_mcp_config`, `test_an_agent_grant_refuses_the_tools_that_reach_outside_it` (`Agent`, `Task`, `Skill`).
   - What a completion grant confines on the real CLI: the nine cases of `tests/llm-kit/live/test_claude_grant_live.py`.
   - No Claude child gets `CLAUDECODE`: `test_each_path_spawns_with_an_env_from_the_one_definition` and `test_claudecode_in_the_parent_reaches_no_child` over 12 paths, `test_every_process_spawn_in_the_package_is_a_claude_site_or_a_named_exception` (whole-package scan), `test_every_module_that_resolves_the_claude_executable_is_covered_by_a_path`, `test_names_compare_without_case_where_the_platform_ignores_case`, `test_an_extra_may_not_put_back_a_removed_variable` and `test_a_bg_worker_overlay_that_names_the_marker_raises` (`test_claude_spawn_env.py`).
   - The batch-launcher character set, for both CLIs: the parametrized tests of `test_cmd_shell.py` over `REFUSED_CHARACTERS`, and its Windows echo-launcher tests, which observe `%` and `^` change, `!VAR!` expand under delayed expansion with no `/v:off` on the launch, and each allowed character arrive unchanged; `test_a_system_prompt_with_any_cmd_special_character_travels_by_file` and the relay's `test_relay_resume_on_a_shim_refuses_a_refused_character_before_any_spawn` (`test_claude_batch_shim.py`).
   - Both readers report the same first refused extra: the `first-problem-*` shared cases, run by `test_codex_tracked_extras.py` and `test/tracked-extras.test.mjs`.
   Each policy has one definition: the child environment (`_claude_child_env`), the refused-character set (`_cmd_shell.REFUSED_CHARACTERS`), the agent grant (`_claude_agent_tool_args`) and the tracked-extras rule (the shared cases file).

## 1.37.0

Security fixes. A tracked project file, a project-tree config file, a front door that a web page can reach and a Windows batch launcher each let text that the machine's owner did not write reach a key, a sandbox, a tool set or a command line. Several of these fixes change behaviour: a call or a file that worked before can now be refused, ignored or run with fewer tools. Read the first list before upgrading.

Changed behaviour (read before upgrading):

- A project model file's connection that names a key variable is refused at call time until the machine owner approves it. A tracked `.llm-kit/models.yaml` can declare `endpoints.<name>: {base_url, key_env}` for an endpoint the machine does not know; before, that file alone made the library send the value of any environment variable as a bearer token to any host it named. Now `endpoint_connection` returns such a connection only when the `base_url` names a plain host over https (or plain http to a literal loopback address, `127.0.0.0/8` or `::1`), no endpoint of the machine (machine config layers, shipped defaults and the endpoint registry; never a file in the project tree) sends that key variable to another host, and the user-level consent file holds an approval of exactly that endpoint name, host and key variable. Otherwise it raises `ProjectModelsError` with code `unapproved`, and a protocol 2 call (so `llm-kit runs compare` too) refuses with `unapproved: ...` before it builds a backend or reads a key. The one-time step for each connection, which the refusal message prints with the values filled in: `llm-kit consent connection on --endpoint NAME --host HOST --key-env VAR`. Plain http to a non-loopback host, a `base_url` that names no plain host, and a key variable that a machine endpoint already sends to another host cannot be approved. A keyless connection (`key_env: null`) and an endpoint the machine already knows need no approval. An approval is not read from a file in the project tree. On Windows, key variables compare as the environment reads them (case-insensitive).
- A tracked project file that sets `add_dirs`, `scratch_dir`, `output_schema`, any extras key that starts with `__llm_kit_`, or a `network` value other than `false` in a row's `request_defaults.extras` or in a `requests` profile's `extras` no longer binds: binding refuses it with `invalid` and names the key. `network: false` stays allowed. Portable validation (a read with no machine endpoints) still accepts these keys, and the calling code (`BackendOptions.extras`) and a call layer still set them.
- A model-config layer inside the project tree can no longer set an endpoint's `base_url`, `key_env`, `key_file`, `billing`, `frontdoor` or `endpoint`, for a new endpoint or an existing one. `load_model_config` drops those keys before the merge and writes one note to stderr for each file, endpoint and key; it does not raise. Declare them in the user-level or fleet config. Every other project-layer key is unchanged.
- A Claude completion (`ClaudeCliBackend`) with no `allowed_tools` is now tool-less: `--tools ""`, no MCP servers, no skills, no settings (so none of their hooks or environment) and no CLAUDE.md, permission mode `dontAsk`. Before, it ran every tool under `bypassPermissions` with settings loaded. A machine whose Claude sign-in or proxy lives in a settings file could fail; it worked on the machine tested (Claude Code 2.1.327). A caller that relied on the old default must pass `ALL_TOOLS` (`"*"`).
- A completion job with `allowed_tools: ""` is now tool-less, and a job that names a grant is restricted to it (`--tools` with `dontAsk`, no MCP servers, skills or settings). A Claude completion job with no `allowed_tools` is unchanged: it passes `ALL_TOOLS`, so its argv is the one it had.
- Agent-loop review lanes get only Read, Grep and Glob. None of the three is pre-approved, so a read stays in the working directory (the lane's project root, else the process directory).
- A front door reached through a reverse proxy or a forwarded port answers 421 when its Host header is not a name the bind owns. A loopback bind accepts loopback names on its own port; add other names with `--allowed-hosts` (or `FRONTDOOR_ALLOWED_HOSTS`). A POST under `/v1/` whose Content-Type is not `application/json` answers 415 on every app, including one built with `create_app` and no bind.
- On a Windows batch launcher (`claude.cmd` or `.bat`), any argv element that contains `&`, `|`, `^`, `<`, `>` or a line break is refused before a process starts, in the completion backend, the agent session and the `--bg` driver. An `mcp_config` value that contains one is refused too. A `--bg` launch whose prompt has a line break or one of those characters (a multi-line prompt) is refused, and its system prompt is never moved to a file. `claude.exe` and other platforms are unchanged.

Added:

- Connection approvals in the user-level consent file: `registry.consent.ConnectionApproval(endpoint, host, key_env)`, `approve_connection` and `revoke_connection` (idempotent; they keep the cost-class policy and the other approvals), `connection_command` (the exact `llm-kit consent connection` command), `connection_host(base_url)` (the host an approval records, or None), `env_name_key` and `ENV_NAMES_IGNORE_CASE`, `ConsentConfig.connections` and `save_config(connections=)`. `llm-kit consent show` lists each approval (`connections` in its JSON), and `llm-kit consent connection on|off --endpoint NAME --host HOST --key-env VAR` changes one and prints what an approval allows.
- `llm_kit.registry.project_models.CODE_UNAPPROVED` (`"unapproved"`), the `ProjectModelsError` code.
- Format `llm-kit.consent/v2` for the consent file (`llm_kit.schema.formats.CONSENT_V2`, schema `consent.v2.schema.json`, 25 shared cases in `tests/formats/consent/cases.json`). The file's integer `schema` stays 2. A writer stores `connections` only when an approval exists, so a file without one still reads in an earlier release; an earlier release refuses a file that holds one (unknown key).
- `llm_kit.completion.types.ALL_TOOLS` (`"*"`), the `allowed_tools` value for the agent-worker mode: every built-in tool with permission checks bypassed (`--permission-mode bypassPermissions --allowedTools ""`; the deny list still applies). It must be the whole value, and a text-only backend refuses it.
- Execution control `tool-grant` in the Claude capability record (a named grant: `--tools`, the isolation and `dontAsk`). `permission-bypass` is now keyed to `allowed_tools="*"`, so a call that is not in the agent-worker mode no longer reports it in `execution_controls_applied`; `allowed-tools` is reported for a named grant when `--allowedTools` is emitted, and for `ALL_TOOLS`.
- `llm_kit.adapters.claude_command.is_batch_shim`, `check_batch_shim_argv` and `batch_shim_spawn_argv`: the one function the three Claude spawn paths pass their argv through. With a batch launcher it sends each system prompt as a temp file (`--system-prompt-file`, `--append-system-prompt-file`) and refuses any other element that cmd.exe would re-parse; with a real executable, or off Windows, it changes nothing.
- Front door: `create_app(..., bind_host=, port=, allowed_hosts=, token=)`, the `--allowed-hosts` flag (`FRONTDOOR_ALLOWED_HOSTS`) and the optional `FRONTDOOR_TOKEN`, which makes every route require `Authorization: Bearer <token>` (401 otherwise, constant-time comparison; an empty value is refused). A non-loopback bind accepts any Host unless a list is set, and `main` warns once on stderr when it has neither a list nor a token. The checks run in the order Host, token, content type.
- `registry.models.CONNECTION_KEYS`, `COST_CLASS_KEYS` and `MACHINE_ONLY_ENDPOINT_KEYS`: the endpoint keys that a project-tree layer may not set.

Changed:

- Binding checks a tracked project file's Codex extras. `build_codex_exec_argv` and `CodexCliBackend` still take `add_dirs`, `scratch_dir`, `network` and `output_schema` from their caller; their docstrings now say that the tracked file may not supply them.
- `ClaudeCliBackend` with `text_only` set refuses any non-empty `allowed_tools` (`ALL_TOOLS` included) before dispatch, as before. It now also raises `TypeError` for a grant that is not a string, and `ValueError` for `default`, an MCP tool name, a name that is not a built-in tool, or `*` beside other names, before any process starts.
- The capability record that `llm-kit models endpoints` prints (golden `tests/llm-kit/goldens/cli/models_endpoints.json`) describes the new tool modes.

Quality bar for the pieces above:

1. Public API documented: pass, with one stated exception. Every new name has a docstring or a `#:` comment. `CONSENT_V2` and `CODE_UNAPPROVED` are new entries of `public-surface.json`, in modules it covers whole. The other new or changed public names are listed one by one in `public-surface.json` (22 entries): `registry.consent` (`ConnectionApproval`, `ConsentConfig`, `ConsentConfigError`, `ENV_NAMES_IGNORE_CASE`, `approve_connection`, `connection_command`, `connection_host`, `env_name_key`, `load_config`, `revoke_connection`, `save_config`), `adapters.claude_command` (`batch_shim_spawn_argv`, `check_batch_shim_argv`, `is_batch_shim`), `completion.types` (`ALL_TOOLS`, `BackendOptions`), `adapters.backends.ClaudeCliBackend`, `registry.models` (`CONNECTION_KEYS`, `COST_CLASS_KEYS`, `MACHINE_ONLY_ENDPOINT_KEYS`, `load_model_config`) and `frontdoor.server.create_app`. `test_public_surface_lists_the_1_37_names_and_each_is_documented` (`test_formats.py`) checks that each is listed, resolves in the code and has a docstring (a constant: a comment before it or a docstring after it). The exception is the practice of 1.33.0: those modules are not in `covers` and are not listed whole, so their older names stay unlisted and no test compares their names with the file. Covering them whole would need library changes (`registry.consent`, `adapters.claude_command` and `registry.models` have no `__all__`; `claude_command.CLAUDE_EXECUTABLE` has no comment; `frontdoor.server.main` has no docstring) and tests for older names. The CLI message constant `cli.consent.APPROVAL_NOTICE` (it has a `#:` comment) is not listed either. `frontdoor._guard` is a private module.
2. Semver release with a changelog: pass. Version 1.37.0 in `pyproject.toml` (a minor release: new names, a new format and security fixes that change behaviour, listed first); this entry.
3. Tests for each public function: pass. `test_consent.py` (`ConnectionApproval`, `approve_connection`, `revoke_connection`, `connection_command`, `connection_host`, `env_name_key`, `ENV_NAMES_IGNORE_CASE`, `save_config` and `load_config` with approvals); `test_cli_consent.py` (`llm-kit consent connection on|off` and `show`); `test_project_connection_approval.py` (`endpoint_connection`: each refusal rule, an approval of another endpoint, host or key, the lower-case host, the keyless connection, Windows key case, an approval in the project tree, an unreadable consent file) and `test_invoke_protocol.py` (an unapproved connection builds no backend and sends nothing; an approved one is called); `test_config_layer_connections.py` (`load_model_config` drops each connection and cost-class key from a project layer and notes it; a user layer still sets them); `test_codex_tracked_extras.py` (each refused extra in a row and a profile, `network: false`, portable validation, the calling code); `test_claude_tool_modes.py`, `test_claude_text_only_settings.py` and `test_targeted_deny_floor.py` (`ALL_TOOLS`, no grant, a named grant, a bare Read, each refused grant, the deny list, the capability record, review lanes, jobs); `test_claude_batch_shim.py` (`is_batch_shim`, `check_batch_shim_argv`, `batch_shim_spawn_argv` and the three spawn paths); `test_frontdoor_guard.py` (`is_loopback_bind`, `split_host`, and the Host, token and content type checks through `create_app` and `main`, which also run `check_options`, `host_permitted`, `error_body` and `GuardMiddleware` of the private `_guard` module). The three `registry.models` key tuples are constants with no test of their own; the `load_model_config` tests exercise them. Each commit states the tests that failed before its change.
4. No client names, prompts or data: pass. Generic hosts, endpoint and variable names (`api.vendor.example`, `VENDOR_API_KEY`).
5. Examples run without a client app: pass. No example changed; `tests/packaging/test_examples.py` runs each example from the built wheel.
6. JSON Schema for each file format: pass. The consent file, which had no schema, has `consent.v2.schema.json`. The reader tests (`test_consent_case` in `test_formats.py`, over the 25 shared cases) validate each readable case against it, and the writer tests (`test_an_approval_is_written_read_back_and_conforms_to_the_schema` and `test_every_shape_the_writer_stores_conforms_to_the_schema` in `test_consent.py`) validate the written file against it. The project model file's format and schema are unchanged: the new refusals happen at binding.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, with the line 1 exception stated: a client may adopt the new format (`CONSENT_V2`, `consent.v2.schema.json`), `CODE_UNAPPROVED` and the 22 listed names from this version. The names of the modules outside `covers` that are not listed stay as before.

## 1.36.0

An agent session can be given exactly the MCP servers it may load. A headless Claude worker started by a client otherwise loaded every MCP server its user and project settings named, and a client had no way to give it one server of its own.

Added:

- Job option `mcp_config` (`llm_kit.completion.session_types.OPTION_MCP_CONFIG`), agent sessions only: a mapping `{"mcpServers": {<name>: {"command": <str>, "args": [<str>, ...], "env": {<str>: <str>}}}}` (`args` and `env` optional). When set, the agent argv carries `--strict-mcp-config --mcp-config <compact JSON>` after the other option flags and before the session id, so the session loads exactly those servers and none from any settings source. When absent (or `null`), no MCP flag is emitted and the argv is unchanged. It is job-level only: a `model_options` entry refuses it.
- `llm_kit.completion.session_types.check_mcp_config(value)`: validates that shape at the boundary and returns a deep plain copy. It refuses, with `ValueError`, a value that is not a mapping, a top-level key other than `mcpServers`, a blank or non-string server name, a server without a nonempty string `command`, a server key other than `command`, `args` and `env`, `args` that are not a list of strings, and `env` that is not a mapping of nonempty string names to string values. An empty `mcpServers` is accepted: the session loads no MCP server.
- `SessionOptions.mcp_config` and `SessionRequest.mcp_config` (default `None`) carry the validated copy; `jobs run` passes it from the job to the session.
- Execution control `mcp-servers` in `CLAUDE_AGENT_CAPABILITIES` (`emits` `--strict-mcp-config --mcp-config`, effect `confine`, source `request`, parameter `session.mcp_config`). `ClaudeAgentSession.applied_controls` reports it only when the request sets `mcp_config`, so the attempt's `execution_controls_applied` records it. The server names and commands are in the job mapping the ledger stores.

Changed:

- A completion job (`session` absent or `completion`) with `mcp_config` is refused when the job is built ("mcp_config requires session: agent"), as a bound or `resume_session` already is. The completion record does not advertise `mcp-servers`.
- `SESSION_OPTION_KEYS` has eight keys.

Quality bar for the pieces above:

1. Public API documented: pass. `OPTION_MCP_CONFIG` and `check_mcp_config` have a comment or docstring and are in `__all__` and `public-surface.json`; `test_jobs_agent_session.py` checks the list. The `SessionOptions` and `SessionRequest` docstrings name the new field.
2. Semver release with a changelog: pass. Version 1.33.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_session_types.py` (accepted shapes, every refused shape, the copy, the completion refusal, `SessionRequest` validation), `test_claude_session.py` (`test_agent_argv_strict_mcp_config`: the full argv and the applied controls through the fake CLI; `test_mcp_config_absent_emits_nothing`), `test_jobs_agent_session.py` (an agent job's argv and attempt controls end to end, no flags without it, `test_completion_refuses_mcp_config`, bad shapes and the `model_options` refusal at the job, the job's copy, the agent record). Removing the emission, emitting it always, dropping the applied control, the completion refusal, the top-level and unknown-key checks, the job's copy or the option key each turns its test red.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. No file format changed: job options have no schema, and the session files are unchanged.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

## 1.35.0

Added, for `@katsoft/llm-kit-send` 0.2.0 (fallback chains) and 0.3.0 (the reply cache):

- `llm_kit.registry.fallback_chains`: `read_fallback_chains`, `check_fallback_chains`, `map_input` and `FallbackChainError` (format `llm-kit.fallback-chain/v1`, the same rules as the TypeScript `readFallbackChains` and `mapInput`). It reads and checks a file and maps an input: it sends nothing.
- Schemas `fallback-chain.v1.schema.json` and `call-record.v1.schema.json` (`llm_kit.schema.formats.FALLBACK_CHAIN_V1`, `CALL_RECORD_V1`). Shared cases `tests/formats/fallback-chain/cases.json`, `tests/formats/call-record/cases.json`.
- `llm_kit.registry.reply_cache`: `request_key`, `step_key` and `FormPart` (format `llm-kit.reply-key/v1`, the same keys as the TypeScript `requestKey` and `stepKey`), `read_entry` and `ReplyEntryError` (format `llm-kit.reply-entry/v1`), `REPLY_KEY_FORMAT`, `REPLY_ENTRY_FORMAT`. It reads the formats only: no Python cache, no send.
- Schemas `reply-entry.v1.schema.json` and `call-record.v2.schema.json` (`llm_kit.schema.formats.REPLY_ENTRY_V1`, `CALL_RECORD_V2`); Shared cases `tests/formats/reply-key/cases.json`, `tests/formats/reply-entry/cases.json`, `tests/formats/call-record/cases.v2.json`.

Quality bar for the pieces above:

1. Public API documented: pass. Each name has a docstring and is in `__all__` and `public-surface.json`.
2. Semver release with a changelog: pass. Version 1.35.0 in `pyproject.toml` (a minor release: new modules, names and formats; nothing changed or removed); this entry.
3. Tests for each public function: pass. `tests/llm-kit/test_fallback_chains.py` runs every shared fallback chain case through `read_fallback_chains`, `check_fallback_chains` and `map_input` and validates them and the v1 call-record cases against the schemas. `tests/llm-kit/test_reply_cache.py` runs every shared key and entry case (the files the TypeScript tests run) and the v2 call-record cases against the schemas.
4. No client names, prompts or data: pass. Generic hosts and labels.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. Each new format has a schema; the shared cases validate against them. The reply key is a key, not a file: shared cases, no schema.
7. Gate before adoption: pass. Lines 1 to 6 pass, so a client may adopt the pieces above from this version on.

## 1.34.0

Added:

- Row `wire_model` in the project model file: the model id the row's endpoint is sent, when it differs from `model` (a vendor's own id on its direct API, beside the aggregator-style name a consumer routes on). Omitted, the wire model is `model`, as before. Protocol 2 (`llm_kit.dispatch.invoke`) sends it and reports it as `resolved.requested_model`; binding reads the effort menu for it; `llm-kit runs compare` plans, prices (`--pricing` key) and records it (`resolved.model` of each run record, so it is in the reuse key); `llm-kit models catalog` shows it; the content pipeline's output cap lookup matches a row by it. The format stays `llm-kit.project-models/v1` (an optional key that an older reader refuses as unknown). New `llm_kit.registry.project_models.wire_model_of(row)`. 4 new shared cases in `tests/formats/project-models/cases.json`.
- The resolved request (`request_for`, `resolve_request`, `REQUEST_KEYS`) has the key `wire_model`, always present (`model` when the row names none). A caller that compares a whole resolved request sees the new key. 1 new shared case in `tests/formats/project-models/requests.json`; every other case states the new key.
- A row may declare the explicit effort `none` (reasoning off) as the first item of its `efforts` on a style that sends the effort as written (`top-level`, `chat_template_kwargs`) when the library measured no menu for its wire model. Those styles' menu is `low`, `medium`, `high` because not every server takes `none`; the row states that its provider does, and `none` is sent as written. Nothing else changes: no other effort is added, no value is remapped, a row that does not declare `none` still refuses it for a selection or a call, and a measured or harness menu is never widened. `llm-kit models catalog` reports such a row with `effort_source` `library-endpoint-style+project-none`. Contract: `docs/reference/llm-kit-formats.md`, "Portable validation and binding"; 1 new shared case.

Changed:

- `test_model_profiles.py`: an unmeasured openrouter.ai model on the `top-level` style may now declare `none` first; the test now pins that it still cannot declare an effort outside the style menu (`max`).

Quality bar for the pieces above:

1. Public API documented: pass. `wire_model_of` has a docstring and is in `__all__` and `public-surface.json`; the module docstrings of `project_models`, `request_settings` and `registry.catalog` state the field and the rule.
2. Semver release with a changelog: pass. Version 1.34.0 in `pyproject.toml` (a minor release: new optional key, new name, new resolved-request key); this entry.
3. Tests for each public function: pass. `test_project_models.py`: the declared `none` binds on both styles and is refused when undeclared, out of order, repeated, beside an effort outside the menu, on a measured menu; `wire_model` round-trips in canonical place and binds the wire model's menu. `test_invoke_protocol.py`: a connected row sends its wire model and `reasoning_effort: none`. `test_runs_compare.py`: the dry run, the price, the sent body and the record use the wire model. `test_model_catalog.py`, `test_pipeline_cap.py`, `test_request_settings.py` (protocol 2 sends each case's `wire_model`). Each fails before the change.
4. No client names, prompts or data: pass. Generic vendor names and ids.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. `project-models.v1.schema.json` states `wire_model`; the shared cases validate their data against it.
7. Gate before adoption: pass. Lines 1 to 6 pass, so a client may adopt `wire_model` and the declared `none` from the release that carries this entry.

## 1.33.0

Added:

- A project model file can connect an endpoint the machine does not know: `endpoints.<name>.base_url` and `key_env` (the name of the environment variable that holds the key, or null for a keyless endpoint; never a key). The format stays `llm-kit.project-models/v1` (optional keys that an older reader refuses as unknown). Precedence: a machine whose model config or endpoint registry knows the name uses its own connection whole; otherwise the file's connection binds the rows, which are then not `unbound`; the file's `effort_style` wins either way. A protocol 2 call on such a row (so `llm-kit runs compare` too) sends the row's model as written, takes the key from the named variable and the `.env` layers, and records a native `usage.cost` as an estimate (the file declares no billing). Contract: `docs/reference/llm-kit-formats.md`, "Connections"; 10 new shared cases.
- `llm_kit.registry.project_models.endpoint_connection(document, name)` and `PROJECT_ENDPOINT` (the `EndpointInfo.kind` of such an endpoint); `llm_kit.registry.models.connection_endpoint(name, base_url=, key_env=, effort_style=)`; keyword `connection=` on `llm_kit.dispatch.factory.create_backend`, field `connection` on `OpenAICompatBackend`, keyword `resolved=` on `llm_kit.adapters.client.make_openai_client` and `llm_kit.registry.api_key.get_api_key`.

Quality bar for the pieces above:

1. Public API documented: pass. Each new name and keyword has a docstring; `endpoint_connection` and `PROJECT_ENDPOINT` are in `__all__` and `public-surface.json`.
2. Semver release with a changelog: pass. Version 1.33.0 in `pyproject.toml` (a minor release: new optional keys and names); this entry.
3. Tests for each public function: pass. `test_invoke_protocol.py`: a connected row is called on the file's connection, with the model as written and the file's effort style; the machine registry wins over the file; a client for a connection uses its base URL and the key its variable holds (and the keyless key); `endpoint_connection` is called directly. `test_runs_compare.py`: a compare calls a connected row. Each fails before the change.
4. No client names, prompts or data: pass. Generic endpoint names and hosts.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. `project-models.v1.schema.json` states both endpoint shapes; the shared cases validate their data against it.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

Changed:

- The shipped OpenRouter endpoints (`openrouter`, `or-qwen`, `or-gpt-mini`, `or-gemini-lite` in `DEFAULT_MODEL_CONFIG`) declare `billing: {mode: provider-reported}`. OpenRouter's `usage.cost` is the amount it charged, so a call on them reports it as the authoritative cost (`cost.usd`, `cost.source` `provider`) instead of an estimate (`estimated_source` `openrouter.usage.cost`). `llm-kit runs compare` records it with `source: reported`.
- Behavior change for spend ledgers on these endpoints: a call now settles on OpenRouter's reported cost. Before, a graph `llm.model` call had no `cost.usd`, so it settled as `unknown` at its reservation and never stopped a run. Now a call that costs more than its `services.spend.models` estimate is `overbilled`, and a row estimated at 0 that costs more is `unreserved`; both halt the ledger and stop the run. Set each OpenRouter row's estimate at or above its worst real cost. The content pipeline already reserves the worst case from its pricing and `max_tokens`, so a correct price table keeps reported costs inside the reservation. On 2026-11-16 no client graph config named an OpenRouter row (the Afterplay graphs estimate only harness rows, at 0). An endpoint that does not declare the billing mode keeps a native `usage.cost` as an estimate, as before; a user config layer that points `openrouter` at another base URL should drop the declaration.

Fixed:

- A process started through the console script `llm-kit` no longer holds the launcher open for its lifetime. On Windows the launcher `llm-kit.exe` is a zip archive that the interpreter puts at `sys.path[0]`; the first `importlib.metadata` scan (`library_version()` in `llm-kit relay serve`, the runtime identity check, any distribution lookup) caches a zip path object that keeps the archive open, so Windows refused to delete or rename `llm-kit.exe` (os error 32) and `uv sync` could not upgrade the environment while the relay ran. `llm_kit.cli.main()` called without `argv` (the console path) now drops a file at `sys.path[0]` before its first scan; a directory there (`python -m llm_kit.cli`) and a call with an explicit `argv` keep `sys.path` as it was. Bar: line 1 pass (the helper is private; `main` states the behavior in its docstring), line 3 pass (`test_cli_launcher_archive.py`: the first case fails before the change, with the archive open after the scan, and the other two keep the scope), lines 4 to 6 pass (no client name, no example change, no file format), line 2 pass (version 1.33.0; a patch-level change in a release that carries the minor-level entries above).
- The project model reader (`parse_yaml_document`, so every read of the tracked file and the personal layer) raises `ProjectModelsError` (`invalid`, `where` `""`) for every bad YAML value. Before, text that PyYAML resolves or an explicit tag names but cannot build escaped as `ValueError`, `KeyError`, `AttributeError` or `IndexError`: `2026-02-30`, `2026-01-01 25:00:00`, a timestamp offset of a day or more, `0x_`, `!!bool foo`, `!!int abc`, `!!int ''`, `!!float abc`, `!!timestamp foo`. A decimal integer of more than 4300 digits is refused on every interpreter. A value that nests more than 100 levels of lists and mappings (aliases count as copies) is refused, and a far deeper file no longer escapes as `RecursionError`. Contract: `docs/reference/llm-kit-formats.md`, "Bytes and YAML"; 16 new shared cases in `tests/formats/project-models/cases.json`, which the TypeScript reader runs too.

## 1.32.0

Deletion of a unit branch. A unit whose worktree is removed kept its branch with no supported way to delete it; a client deleting it by hand had no guard against deleting work that was never merged or that moved after the decision.

Added:

- `llm_kit.jobs.units.delete_unit_branch(registry, repository, unit, *, expected_tip, reason, target_ref=None)`, with `reason` in `BRANCH_DELETION_REASONS` (`"merged"`, `"discarded"`). It takes `repository_lock` itself (not reentrant: a caller must not hold it). It refuses, with the new `UnitBranchDeletionRefused` (a `UnitError` with `reason`), a unit that is not `removed`, a branch checked out in any worktree, a branch whose tip is not `expected_tip`, and for `"merged"` a tip that is not an ancestor of `target_ref` (`git merge-base --is-ancestor`; a squash or cherry-pick is not proof). `target_ref` is required for `"merged"` (a ref already fetched, or a commit id; nothing is fetched) and refused for `"discarded"`. The deletion is `git update-ref -d refs/heads/<branch> <expected_tip>`, so a branch that moves at the last moment is kept. A repeat call whose branch is already absent and whose row records the same reason and tip returns that record unchanged.
- `UnitRecord.branch_deleted_reason`, `branch_deleted_tip_sha`, `branch_deleted_target_sha` (`"merged"` only) and `branch_deleted_at`: the deletion evidence on the unit row, also in `to_mapping()`.

Changed:

- The registry schema is 2 (`REGISTRY_SCHEMA`): four nullable columns for the deletion evidence. Opening a version 1 registry migrates it in place and keeps its rows. An llm-kit before 1.32.0 then refuses that registry ("newer than this llm-kit reads"), so every process that shares a worktree root must run 1.32.0 or later.
- `unit_record.json` gains the four keys as optional properties, so a record written before 1.32.0 still conforms; the record `schema` stays 1.

Quality bar for the pieces above:

1. Public API documented: pass. `delete_unit_branch`, `UnitBranchDeletionRefused` and `BRANCH_DELETION_REASONS` have docstrings or comments and are in `__all__` and `public-surface.json`; `test_jobs_workspace.py` checks the list.
2. Semver release with a changelog: pass. Version 1.32.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_jobs_units.py`, against real Git: a merged branch is deleted and the evidence recorded; a branch not reachable from the target, a branch that moved past the expected tip, a unit that is not removed and a branch checked out in another worktree are refused and kept; a discarded branch is deleted; argument validation; a repeat call returns the same record and a mismatched one is refused; an absent branch without a recorded deletion is refused; a version 1 registry migrates. The ancestor check, the expected-tip check (alone and with the `update-ref` old value), the checked-out check, the state check and the idempotent return each turn their test red when reverted.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. `unit_record.json` has the new keys; the tests validate deleted (merged and discarded) and migrated records against it.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

## 1.31.0

The project model file can describe the whole request of a selection, so a request can be rebuilt, compared and cached from data. The format stays `llm-kit.project-models/v1`: every key below is optional, a file without them reads as before, and an older reader refuses a file that uses them (unknown keys), so none misreads one.

Added:

- Format keys: `endpoints` (`{<endpoint>: {effort_style}}`, which wins over the endpoint registry's style for this project's calls: menu, binding and wire), `requests` (named request profiles: `temperature`, `top_p`, `max_output_tokens`, `reasoning_budget`, `extras`), row `traits` (the consumer's labels; never sent), typed row `request_defaults` (`temperature`, `top_p`, `reasoning_budget`) and selection `request` (also in the personal layer). Schemas, the contract (`docs/reference/llm-kit-formats.md`, "Request settings") and 25 new shared cases in `tests/formats/project-models/cases.json`.
- `llm_kit.registry.request_settings`: `resolve_request(document, key, overrides=, call=)`, `request_for(document, selection, call=)` and `REQUEST_KEYS`. Layers: row < selection (effort, request profile) < call; `effort` and `reasoning_budget` are one reasoning control. Shared cases in `tests/formats/project-models/requests.json` (30), which the TypeScript reader runs too.
- `llm_kit.registry.project_models`: `check_setting`, `validate_profile`, `endpoint_styles`, `REQUEST_SETTINGS`, `PROFILE_RESERVED_EXTRAS`, `LEGACY_ROW_EXTRAS`, `CODE_UNKNOWN_SELECTION`, `KEEP_REQUEST`; `set_local_override(..., request=KEEP_REQUEST)` keeps the tracked selection's request profile when it overrides only the model.

Changed:

- Protocol 2 (`llm-kit completion complete`) sends the resolved request: `temperature`, `top_p`, the output cap and a reasoning budget (`reasoning: {max_tokens}`, `reasoning-object` style only) from the file; the project file's effort style places the effort. A harness row refuses `temperature`, `top_p` and `reasoning_budget` (kind `configuration`). A request whose fields are all from v1 files sends the same body as before.
- In a row's extras, `temperature` and `top_p` are now read as the row's typed defaults, as `max_tokens` already was; the body on the wire is the same. On a harness row they are now refused, where before the adapter dropped them.
- `llm-kit runs compare` records the resolved settings in each candidate's `request.extras`, so a changed temperature or budget is a new reuse key; traits are not in it.
- `llm-kit models catalog` shows row `traits` and the project file's effort style in `effort_delivery`.

Migration: a row that kept consumer flags in `request_defaults.extras` (sent as body fields) moves them to `traits`.

Quality bar for the pieces above:

1. Public API documented: pass. Each new name has a docstring and is in `public-surface.json`; `test_formats.py` checks the list.
2. Semver release with a changelog: pass. Version 1.31.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_formats.py` (both shared case files), `test_request_settings.py` (protocol 2 sends each case's wire fields through the real OpenAI-compatible backend; harness refusal; binding with the project style; catalog; the local-override writer), `test_runs_compare.py` (the candidate's settings are in the reuse key).
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. `examples/validate_and_render.py` resolves the request of each selection.
6. JSON Schema for each file format: pass. Both schemas gain the new keys; every valid case conforms.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

## 1.30.0

A project model file whose rows name an endpoint this machine does not know now loads; a call to such a row refuses at the call. The endpoint is the consumer's name (the TypeScript reader `@katsoft/llm-kit-config` never resolved it), so a client may keep rows for its own endpoints (for example picture or video providers) in the same file.

Changed:

- Binding (`validate_document`, every read and write of `llm_kit.registry.project_models`) loads a row whose endpoint resolves to no registry endpoint as unbound, in place of refusing the file. The row keeps the checks that need no endpoint: its declared `efforts`, and the reserved extras other than `reasoning`.
- `ProjectModels.unbound` and `Validation.unbound`: the ids of the unbound rows, in catalogue order (`[]` when none). Both are in `to_json()`, so `llm-kit models project show` and `validate` print them.
- Protocol 2 (`llm-kit completion complete`) refuses a call whose model or selection resolves to an unbound row with kind `configuration`; the message names the row, the endpoint and the known endpoints.
- `llm-kit runs compare` (also `--dry-run`) refuses a selection that resolves to an unbound row (`invalid`, naming the selection, row and endpoint) before any write; the other selections of the file plan as before.
- `llm-kit models catalog` leaves an unbound row out of `models` and adds a note naming it, in place of a `KeyError`.
- `docs/reference/llm-kit-formats.md`, "Portable validation and binding": the rule; shared case `endpoint-is-opaque-text` in `tests/formats/project-models/cases.json`.

Quality bar for the pieces above:

1. Public API documented: pass. The two new attributes have docstrings on their classes; no name is added to `public-surface.json`.
2. Semver release with a changelog: pass. Version 1.30.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_project_models.py` (an unbound row loads and is named; its effort and reserved-extras checks stay), `test_formats.py` (the shared case, portable and bound), `test_invoke_protocol.py` (the call refuses; another row of the file still calls), `test_runs_compare.py` (the dry run plans; the unbound selection refuses before any write), `test_model_catalog.py` (the note); the `models project show` and `validate` CLI goldens print `unbound`.
4. No client names, prompts or data: pass. The tests use generic rows and endpoints.
7. Gate before adoption: pass. Lines 1 to 4 pass for the pieces above, so a client may adopt them from this version on.

## 1.29.0

Comparisons of model selections, recorded in a run store (`llm-kit runs compare`). A comparison sends one prompt file, filled with each case, to two or more selections of the project model file and records every attempt as a `llm-kit.run-record/v1` record of one `compare` group.

Added:

- `llm_kit.runs.compare` (also re-exported by `llm_kit.runs`): `plan_compare` (checks everything and writes no record: cases, prompt, candidates, content class against the store policy, a cost bound for each call, reuse and the group's recorded spend) and `run_compare` (runs the plan with a caller the client gives: the started event before each call, the result after it, no retry). Types `Candidate`, `Price`, `CallResult`, `CompareCase`, `Scorer`, `PlannedAttempt`, `ComparePlan`, `AttemptOutcome`, `CompareResult`, `CompareError`; helpers `read_cases`, `command_scorer`, `attempt_bound_usd`; constants `ACTIONS`, `CALL_STATUSES`, `INPUT_OVERHEAD_TOKENS`, `LABEL_BOUND`, `LABEL_REUSED_FROM`, `COMPARE_CASE_V1`.
  - Reuse: an attempt is `done` when its group holds an `ok` record with the same reuse key (`llm-kit.run-key/v1`) and the same scorer hash; a `copy` (a new record that reuses the answer with no call, label `compare.reused_from`) when an `ok` record with the key is in another group or has another scorer; else a `call`.
  - Budget (`max_usd`): the spend of the whole group across runs, with failed attempts at their recorded cost (their bound when unknown) and started events with no result at their bound (label `compare.bound_usd`). An attempt whose worst-case bound would cross the budget is not started. The bound is the prompt's byte count plus 64 input tokens and the output cap (else the price's `context_window`) at the candidate's price; a selection with no price or no output bound is refused before any write, and a harness only with a free price.
  - Scorer: a program that reads one `llm-kit.run-import/v1` line and returns it with `record.scores` filled; its name, version and hash are in `provenance.scorer`. A failed scorer records the attempt as `invalid` (`scorer-failed`) and stops the run.
- `llm-kit runs compare --prompt FILE --cases FILE --select A,B --flow F --content C --max-usd N [--pricing FILE] [--max-output-tokens N] [--dry-run] [--scorer COMMAND...]`: resolves each selection through the project model file and its personal layer, prices it from the pricing table (`load_pricing` format, by wire model; a `local` endpoint is free), and calls through `llm_kit.dispatch.invoke` with `max_retries: 0`. `--dry-run` prints the plan and the cost bound and calls nothing. The composition is in `llm_kit.cli._runs_compare`.
- The format `llm-kit.compare-case/v1` (one line of a case file: `{id, slots}`): `compare-case.v1.schema.json`, `llm_kit.schema.formats.COMPARE_CASE_V1`, shared cases in `tests/formats/compare-case/cases.json`.
- Protocol 2 of `llm-kit completion complete` takes `max_retries` (a non-negative integer): the automatic retries of an OpenAI-compatible transport call (0 sends the request once). Absent, the client default is kept, as before.
- `examples/compare_models.py`: a comparison with a stand-in caller, run again with every answer reused, and a zero budget that starts no call.

Changed:

- The `llm_kit.runs` component description in `docs/reference/llm-kit-architecture.md`: it makes no model call itself; a comparison takes the caller as an argument and the CLI composes it with `llm_kit.dispatch` and `llm_kit.registry`. The signed edges are unchanged (`runs -> {schema, graph, pipeline.artifacts}`).

Quality bar for the pieces above:

1. Public API documented: pass. Each name of `llm_kit.runs.compare.__all__` has a docstring and is in `public-surface.json` (with its `llm_kit.runs` re-export and `llm_kit.schema.formats.COMPARE_CASE_V1`); `test_runs_compare.py` and `test_runs_record.py` check the list against the code.
2. Semver release with a changelog: pass. Version 1.29.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_runs_compare.py` (record before send, no retry, failed, halted and raising callers, reuse by every input of the key, copies, scorers, the bound, the group budget with failed and pending attempts, plan refusals, the CLI through the project model file with `max_retries: 0` on the wire, dry run), `test_formats.py` (the compare-case shared cases) and `test_invoke_protocol.py` (`max_retries`).
4. No client names, prompts or data: pass. The tests and the example use generic models, prompts and cases.
5. Examples run without a client app: pass. `examples/compare_models.py` runs in the clean venv with no extra (`tests/packaging/test_examples.py`).
6. JSON Schema for each file format: pass. The case file has `compare-case.v1.schema.json`; the scorer reads and writes `llm-kit.run-import/v1` lines; the records are `llm-kit.run-record/v1`.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

## 1.28.0

Recovery of a unit held before any job ledger exists. A client that holds a unit (`WorkspaceManager.hold`), runs a setup command and only then starts `llm-kit jobs run` had no recovery for a crash in that window: no ledger row names the unit, so `recover_run_units` cannot see it, and the lease lives only in the dead process.

Added:

- `llm_kit.jobs.units.record_unit_processes(registry, lease, processes)` and `WorkspaceManager.record_unit_processes(job_id, processes)`: the owner records the processes that may write the worktree (a setup command and its tree) in the unit's registry row, right after starting them. The identities accumulate while the unit is `active` and are cleared by the lease finalization. An unrecorded process is not known to recovery.
- `llm_kit.jobs.units.recover_owned_unit(registry, repository, unit, *, run_id, job_id, processes=(), timeout_s=...)`: finalizes, without its lease and without a ledger, the unit that `run_id`/`job_id` last held. It is `finalize_unit` without a lease, restricted to that owner: a dead owner's `active` unit is committed and parked (`committed` or `clean`) or held `dirty_held`; while any recorded or given process is alive it stays `active` and `indeterminate` and nothing is committed (detached survivors block finalization); a `writer_held` unit is re-checked; a `parked` unit is returned `clean` unchanged. A live owner raises `UnitOwnershipError`, as does another run or job.
- `finalize_unit(..., owned_by=(run_id, job_id))`: without a lease, refuses a unit whose recorded owner run and job differ, checked under the repository lock.

Changed:

- `finalize_unit` without a lease on an `active` unit whose owner died now also treats the identities recorded by `record_unit_processes` as possible writers. Before, only the `processes` argument counted, and a unit recorded by no ledger had none.

Quality bar for the pieces above:

1. Public API documented: pass. Each new name has a docstring; both new functions are in `__all__` and `public-surface.json`.
2. Semver release with a changelog: pass. Version 1.28.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_jobs_units.py` (recovery by run and job beside a recorded live writer and after it exits, another owner, a live owner, idempotence, recording needs the active lease, accumulation and clearing) and `test_jobs_workspace.py` (a real `hold`, record and crash in a child process, recovered without a ledger). Each fails with its change reverted (the recorded-process union, the `owned_by` check, the lease check, the manager method).
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. `unit_record.json` already allows `held_processes` in any state; the test validates an `active` record that carries them.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

## 1.27.1

The example for agent sessions and branch mode. No library code changes; this release carries the example, its packaging test and the bar record that 1.26.0 and 1.27.0 left pending.

Added:

- `examples/agent_session_branch.py`: one agent-session job in branch mode, end to end, in a plain Python environment. It builds a temporary repository with a bare remote and the remote-tracking base ref `origin/dev`, writes a jobs file (`options.session: agent`, `workspace: {mode: branch, unit, root, base_ref}`), runs `llm-kit jobs run --session-root ... --supervisor-budget ...`, appends a mailbox message with `llm_kit.jobs.session.append_message` while the session runs, and then shows the end-of-run commit `WIP: <unit> run <n>` on the unit branch, that the base branch did not move, and the parked unit in the registry (`llm_kit.jobs.units.list_units`). It makes no model call: the documented override `LLM_KIT_CLAUDE_EXECUTABLE` names a stand-in for `claude` that is the same file started with `--as-fake-claude` (a small stream-json program). A real client changes only the executable and the harness entry. It needs the `local` extra (psutil) and the `config` or `jobs` extra (PyYAML), and `git` on PATH.
- `tests/packaging/test_examples.py::test_the_agent_session_examples_run_in_the_clean_venv` runs it from the built wheel in a clean venv (`session_venv` in `tests/packaging/conftest.py`: the closure venv plus the `local` extra), with a scratch HOME and no PYTHONPATH. The test fails when the example is broken: with `--session-root` misspelled, the example exits 1 with `unrecognized arguments: --session-rot` and the test reports it.

Quality bar for this version (all pieces of 1.26.0 and 1.27.0, plus the example):

1. Public API documented: pass. No public name changed. The 1.26.0 and 1.27.0 records stand; the example has a module docstring and a docstring on each function.
2. Semver release with a changelog: pass. Version 1.27.1 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. No public function changed; the 1.26.0 and 1.27.0 tests stand. The example is itself run by the packaging test above.
4. No client names, prompts or data: pass. The example uses a generic unit id, repository and prompts.
5. Examples run without a client app: pass for agent sessions and branch mode, which 1.26.0 and 1.27.0 marked pending. `WHEEL_CLOSURE_SOURCE=local uv run --extra dev pytest tests/packaging` runs `agent_session_branch.py` from the built wheel in a clean venv.
6. JSON Schema for each file format: pass. No file format changed; the session-directory schemas of 1.26.0 and `unit_record.json` of 1.27.0 stand.
7. Gate before adoption: pass. Line 5 is satisfied for agent sessions (1.26.0) and branch mode (1.27.0), so a client may adopt either from this version on.

## 1.27.0

Branch-mode workspaces (agent-runs design 6.1, 6.2, 7.2, 11.5). A job with `workspace: {mode: branch, unit, root, base_ref, branch}` runs every attempt in the worktree of one unit of work, on the unit's own branch (default `agent/<unit>`), at the fixed path `<root>/<repo>/<unit>`. When the job's run ends for any reason, every remaining change is committed to the unit branch as `WIP: <unit> run <n>`. Ledger schema 15 records these worktrees with the workspace status `unit`.

Added:

- `llm_kit.jobs.units`: the unit registry `UnitRegistry` at `<root>/units.sqlite3` (its own schema version, `REGISTRY_SCHEMA` 1), `UnitRecord`, `UnitOwner`, `UnitLease`, `UnitState` (`active`, `parked`, `dirty_held`, `writer_held`, `removed`), `UnitFinalizeStatus`, `UnitFinalizeResult`, and `acquire_unit`, `finalize_unit`, `remove_unit_worktree`, `record_unit_merge`, `list_units`, `live_processes`, `check_unit_id`, `repository_identity`, `repository_lock`, with the errors `UnitError`, `UnitBusyError`, `UnitOwnershipError` and `UnitRemovalRefused`. A unit is identified by its repository's Git common directory and its unit id; the first registry to acquire a unit records itself in the repository configuration (`llm-kit-unit.<unit>.registry`), so a second registry cannot acquire it. Every change to a unit, its worktree or its branch holds an interprocess lock keyed by the Git common directory (`<git common dir>/llm-kit/units.lock`). An owner and every survivor are checked by process identity (pid and creation time), which needs the `local` extra (psutil). The JSON Schema of `UnitRecord.to_mapping` is `llm_kit/jobs/schemas/unit_record.json`.
- `llm_kit.jobs.workspace`: `WorkspaceSpec` branch mode (`mode: branch`, `unit`, `root`, `base_ref` (required), `branch`; exclusive with `isolate`); `resolve_unit_base_ref` (the base is a remote-tracking branch such as `origin/dev`, or a full commit id; never a local branch or the checkout HEAD); `unit_worktree_path`; `BranchWorkspaceHook` (the end-of-run commit: `committed` or `clean` parks the unit; a refused commit gives `dirty_held`; unconfirmed writers or detached survivors give `writer_held`, which `finalize_unit` without a lease finalizes after every recorded identity is gone; crash recovery reports a unit whose owner is gone but whose processes are alive as `indeterminate` in its `UnitRecoveryEntry`); `recover_run_units` and `UnitRecoveryEntry` (the crash path); `WorkspaceManager.working_directory`, `is_unit_job`, `hold` and `finalize_unit_run`, and its `run_id` argument; `WORKSPACE_STATUS_UNIT`.
- `llm-kit jobs cancel`: after the ledger cancellation it finalizes the units that the run's branch-mode jobs still own, terminal jobs included, and reports them under `units`.

Changed:

- Ledger schema 14 -> 15: attempts and reservations can record `workspace_status: unit`. No column changes. An llm-kit before this version refuses a schema 15 ledger and does not fail on a `unit` row. `JobStore.record_reservation_workspace` takes `workspace_status`.
- `jobs/run.py`: a branch-mode job always gets `BranchWorkspaceHook`. The supplied `workspace_hook` (default `NoWorkspaceHook`) serves the other jobs. The first attempt acquires the unit, and the unit stays owned through the retries, the contract and an answered continuation until the hook runs. A unit that another run owns, or that is held, fails the job before it is invoked. Each run and resume pass first calls `recover_run_units`, so a unit that a crashed or hard-cancelled pass left `active` is committed before dispatch. It stays `active` and `indeterminate`, and blocks reuse, merge and removal, while a process recorded in its `cleanup.json` is alive. `check_agent_session_run` adds `session_finalize_s` to each attempt of a branch-mode job. `run_job` refuses a branch-mode job when no `workspace_manager` is given.
- `WorkspaceFinalizeStatus` gains `writer_held`: the hook did not commit because a writer may still be running. `dirty_held` now means only that a commit was attempted and did not leave the branch clean.
- `jobs/run.py`: an agent attempt whose session raised after launching its process is recorded as unaccounted, because its detached processes were never reported. A retry in the same working directory is then refused: the job ends `failed` with `workspace_not_quiescent`. Before, the retry ran beside processes that could still write the directory (seen when the classifier raised on psutil's macOS `SystemError`).
- psutil 7.2 on macOS sometimes raises `SystemError` from `Process.environ()` under load. Both readers now treat it as an unreadable environment, as they already treated `AccessDenied`. The agent session classifier (`llm_kit.adapters.claude_session`) no longer raises during shutdown. It still classifies a process in the leader's session, a recorded member, or a process seen as a descendant without reading its environment. The relay resume tag scan (`llm_kit.relay.resume`) skips the process.
- `gc_workspaces` skips every `unit` attempt and reservation. The path is reused by later runs, so an old run never authorizes removing the worktree of a newer unit. It also does not report a unit worktree that the registry records as an orphan.

Quality bar for the pieces above (`llm_kit.jobs.units`, `llm_kit.jobs.workspace`, the `unit_record.json` format):

1. Public API documented: pass. Each listed name has a docstring (constants a `#:` comment or the module docstring). `public-surface.json` covers `llm_kit.jobs.units` and `llm_kit.jobs.workspace` whole; `tests/llm-kit/test_jobs_workspace.py` compares each list with the module's `__all__`.
2. Semver release with a changelog: pass. Version 1.27.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_jobs_units.py` (real Git: acquisition, the commit, held states, crash recovery, removal, merge records, the interprocess lock), `test_jobs_workspace.py` (detached and branch worktrees, base refs, concurrency, recovery, `jobs cancel`, GC, the spec) and `test_jobs_branch_mode.py` (agent sessions through the fake `claude` in a unit worktree, every exit path, a hard cancel) call each listed function. `test_jobs_ledger_v15.py` migrates a real schema 14 ledger (`fixtures/jobs_ledger_v14.sql`).
4. No client names, prompts or data: pass. Unit ids, repositories and prompts in the tests are generic.
5. Examples run without a client app: pending for branch mode. No example exercises it yet. The existing examples still run in the wheel closure venv (`tests/packaging/test_examples.py` with `WHEEL_CLOSURE_SOURCE=local`: 56 passed).
6. JSON Schema for each file format: pass. `unit_record.json` describes `UnitRecord.to_mapping`, and the registry tests validate every state against it. The registry file itself is SQLite with its own schema version.
7. Gate before adoption: recorded in this entry; line 5 is pending, so no client adopts branch mode until an example passes.

## 1.26.0

Agent sessions for jobs (agent-runs design, sections 5 and 11). A job with `options.session: agent` runs as a persisted `claude -p` stream-json agent session instead of a one-shot completion: the prompt is the first stdin message, a per-attempt mailbox feeds later messages and operator stops, Claude's stdout is kept verbatim as `events.jsonl`, and the last `result` event is the answer the contract judges. Ledger schema 14 records the session on each reservation and attempt.

Added:

- `llm_kit.completion.session_types`: the interface of the agent-session backend: `SessionMode`, `SessionOptions` and `SessionBounds` (job options `session`, `resume_session`, `session_quiescence_s`, `session_result_wait_s`, `session_eof_wait_s`, `session_sigint_wait_s`, `session_finalize_s`), `SessionRequest`, `SessionOutcome`, `SessionFailure`, `StopKind`, `StopMode`, `StopRequest`, `SessionPaths`, `MailboxRecord`, `MailboxState`, `MessageDelivery`, `SessionRecord`, `CleanupRecord`, `ProcessIdentity`, `DetachedSurvivor`, the `SessionChannel`, `AgentSessionBackend` and `SessionWorkspaceHook` protocols, `WorkspaceFinalizeResult`, the deadline helpers `AttemptBudget`, `JobBudget`, `required_supervisor_budget_s`, `check_supervisor_budget` and `SupervisorBudgetError`, and the ledger schema 14 column list `SESSION_LEDGER_COLUMNS`.
- `llm_kit.adapters.claude_stream`: `parse_stream_line`, `init_problem` and `StreamVerdict`, the stream-json parser shared by the relay and the session backend.
- `llm_kit.adapters.claude_session`: `ClaudeAgentSession` (the backend: argv, stdin driver thread, verbatim `events.jsonl`, quiescence, stop, shutdown with SIGINT and identity-bound kill escalation, `cleanup.json`, the outcome and its failure). Session shutdown and workspace writer quiescence are separate results: `SessionOutcome.shutdown_confirmed` means stdout was drained and the leader and every ATTACHED process are gone (a process recorded inside the leader's original session stays attached even after `setsid()`; a different process group alone is not detachment); DETACHED tagged processes (created after the leader, never recorded, outside its original session) are never signalled, get up to B_int to exit, and each survivor is persisted in `cleanup.json` (`detached_survivors`: pid, create time, cwd) and printed as a warning. `SessionOutcome.detached_survivors` and the derived `workspace_quiescent` report them, `user_message_line`, `interrupt_request_line`.
- `llm_kit.adapters.capabilities.CLAUDE_AGENT_CAPABILITIES`: the agent session's advertisement (no `no-session-persistence`, no text-only mode, `permission-bypass` fixed, `system_prompt_mode` default `append`).
- `llm_kit.dispatch.factory`: `create_agent_session`, `agent_session_capabilities`, `AgentSessionUnsupportedError`.
- `llm_kit.jobs.session`: the per-attempt session directory (`create_attempt_dir`, `append_message`, `request_stop`, `read_state`, `read_records`, `write_session_record`, `read_session_record`, `read_cleanup_record`, `FileSessionChannel`) and the JSON Schemas `mailbox_record.json`, `mailbox_state.json`, `session_record.json`, `event_line.json` and `cleanup_record.json` in `llm_kit/jobs/schemas/`.
- `llm_kit.storage.file_lock`: `file_lock`, `POLL_S` (the supervisor's file lock, moved unchanged).
- `llm_kit.jobs.select`: `select_session_endpoint_with_readings` (session admission: the first usable Claude entry; a job with no Claude entry is unroutable, never run as a completion) and `session_capabilities`.
- `llm_kit.jobs.run`: `check_agent_session_run`, `AgentSessionConfigError`, `NoWorkspaceHook` (returns `not_applicable`, naming any detached survivors in `detail`); `run_jobs`, `run_job_file` and `resume_run` take `session_root`, `supervisor_budget_s` and `workspace_hook`.
- `llm-kit jobs run` and `llm-kit jobs resume`: `--session-root <dir>` and `--supervisor-budget <seconds>`, both required when any job declares `session: agent`. The check covers the whole jobs file before the ledger is opened.

Changed:

- Ledger schema 13 -> 14: `session_mode`, `session_id`, `session_cwd`, `session_dir` and `stop_kind` on `attempts` and `reservations`. An agent reservation carries its session id, execution cwd and session directory before any attempt row exists. `LedgerReader` serves schema 10-13 ledgers without migrating them. An llm-kit before this version refuses a schema 14 ledger.
- An operator stop or pause ends the job `cancelled` with error code `operator_stop` / `operator_pause` after one attempt, whatever `max_attempts` is. A session whose attached processes were not all confirmed stopped ends the job `failed` (`shutdown_unconfirmed`) without running the contract. A confirmed shutdown with detached survivors does not fail: the result and contract proceed, and `SessionWorkspaceHook.finalize_run` receives `writers_stopped=False` and `detached_survivors`; its contract keeps finalization, reuse, merge and reaping blocked until each survivor is re-checked gone. A retry that would run in the SAME (non-isolated) working directory re-checks each survivor's identity first: if any is alive the job ends `failed` with the error `workspace_not_quiescent: ...` naming them and nothing is reserved; if all are gone the retry proceeds. A retry in a fresh isolated worktree is not blocked.
- The relay's resume runner reads Claude's stream through `llm_kit.adapters.claude_stream`; its first-valid-init and first-matching-result rules are unchanged.
- `ClaudeCliBackend`: its preparation, option flags and envelope-to-response steps are shared helpers used by both Claude backends; the completion argv and response are unchanged. A non-string `result` field now gives empty text.

Quality bar for the pieces above:

1. Public API documented: pass. Each listed name has a docstring (constants a `#:` comment). `public-surface.json` covers `llm_kit.completion.session_types`, `llm_kit.adapters.claude_stream`, `llm_kit.adapters.claude_session`, `llm_kit.jobs.session`, `llm_kit.storage.file_lock` and `llm_kit.jobs.run` whole; `tests/llm-kit/test_jobs_agent_session.py` compares each with the module (`__all__`, or the names `claude_stream` declares public, since it has no `__all__`).
2. Semver release with a changelog: pass. Version 1.26.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_session_types.py`, `test_claude_stream.py`, `test_jobs_session.py`, `test_jobs_ledger_v14.py`, `test_claude_session.py` and `test_jobs_agent_session.py` call each listed function; the session tests drive a fake `claude` (`tests/llm-kit/fixtures/fake_claude_session.py`) through stdin and stdout.
4. No client names, prompts or data: pass. The fake CLI and the tests use generic prompts.
5. Examples run without a client app: pending. No example exercises an agent session yet; one needs a logged-in Claude CLI.
6. JSON Schema for each file format: pass. The five session-directory files have schemas in `llm_kit/jobs/schemas/`; `test_jobs_session.py` validates every reader and writer against them; `events.jsonl` has the permissive event-line schema.
7. Gate before adoption: recorded in this entry; line 5 is pending, so no client adopts agent sessions until an example passes.

Run records and run stores (`llm_kit.runs`, Phase 2 unit 1). One format for model runs and comparisons, a store with a content-class policy, a derived index and the `llm-kit runs` CLI. The contract is `docs/reference/llm-kit-runs.md`; the formats are in `docs/reference/llm-kit-formats.md`.

Added:

- `llm_kit.runs.record`: the format `llm-kit.run-record/v1` with status-specific fields (`started`, `ok`, `invalid`, `failed`, `refused`), `validate_record`, `start_record`, `finish_record`, `blob_ref`, `blob_refs`, `reuse_key` (key contract `llm-kit.run-key/v1`, `REUSE_KEY_VERSION`), `RunRecordError`, `CONTENT_CLASSES` (`synthetic`, `internal`, `sensitive`), `RECORD_STATUSES`, `RESULT_STATUSES`.
- `llm_kit.runs.store`: `RunRecordStore` (`init`, `open`, `put`, `put_many`, `check_many`, `get`, `events`, `ids`, `records`, `has_blob`, `get_blob`, `export_line`, `index`, `check_ignored`), `PutResult`, `RunStoreError`, `read_import_file`, `import_file`, `resolve_store_path`, `STORE_ENV` (`LLM_KIT_RUN_STORE`), `INDEX_FILE`. Events are write-once (a started event and a result event per attempt; the result repeats every started field). A batch is checked whole (validity, blob hashes, policy, blob completeness, unreferenced blobs, conflicts) before any file is written.
- `llm_kit.runs.index`: `RunIndex` (`sync`, `rebuild`, `query`), `INDEX_COLUMNS`, `INDEX_VERSION`: a metadata-only SQLite cache at `.index.sqlite`, refused unless the store's `.gitignore` ignores it.
- `llm_kit.runs`: the names above, re-exported.
- `llm_kit.cli.runs` and the `runs` group of `llm-kit`: `init`, `import` (`--check`), `list`, `show` (`--with-blobs`), `reindex`.
- `llm_kit.schema.formats`: `RUN_RECORD_V1`, `RUN_STORE_V1`, `RUN_IMPORT_V1` and their schema files; shared cases in `tests/formats/run-record/cases.json`.
- `examples/record_runs.py`.

Changed:

- `llm_kit.graph.records.resolve_record_path` uses the new module helper `check_record_text` for its syntax check; its behavior is unchanged.

Quality bar for the pieces above (`llm_kit.runs`, `llm_kit.cli.runs`, the three formats):

1. Public API documented: pass. Each listed name has a docstring; `public-surface.json` covers `llm_kit.runs`, `llm_kit.runs.record`, `llm_kit.runs.store`, `llm_kit.runs.index` and `llm_kit.cli.runs` whole, and `tests/llm-kit/test_runs_record.py` compares each with the module's `__all__`.
2. Semver release with a changelog: pass. Version 1.26.0 in `pyproject.toml`; this entry.
3. Tests for each public function: pass. `test_runs_record.py`, `test_runs_store.py` and `test_runs_cli.py` call each listed function.
4. No client names, prompts or data: pass. The cases and tests use generic flows, models and text.
5. Examples run without a client app: pass. `examples/record_runs.py` needs no extra and no network.
6. JSON Schema for each file format: pass. `run-record.v1`, `run-import.v1` and `run-store.v1` schemas; the record, import and store tests validate their data against them. The index is a local SQLite cache, not an interchange format.
7. Gate before adoption: recorded in this entry. The owner signed off the `runs` component's import edges on 2026-11-14 (`docs/reference/llm-kit-architecture.md`, "Decision summary").

## 1.25.0

Shared file formats. Three llm-kit file formats get a format id, a JSON Schema and shared cases that readers in other languages also run. The contract is `docs/reference/llm-kit-formats.md`.

Added:

- `llm_kit.schema.formats`: `PROJECT_MODELS_V1`, `PROJECT_MODELS_LOCAL_V1`, `PROMPT_FILE_V1`, `SCHEMA_FILES`, `load_schema(format_id)`, and the schema files `project-models.v1.schema.json`, `project-models-local.v1.schema.json` and `prompt-file.v1.schema.json` (in `llm-scripting-kit.json-schema-subset/v1`).
- `llm_kit.registry.project_models.validate_portable_document(data)` and `validate_portable_local_document(data, tracked_document)`: validation of the project model file and its personal layer without the machine's endpoint registry (structure, JSON values, references). Binding (endpoints, effort menus, reserved extras) stays in `validate_document` and every read and write.
- `ProjectModelsError.where`: the path of the first problem (`catalog[1].efforts[0]`), `""` for the file as a whole.
- `llm_kit.registry.project_models.__all__`; the module's public names are listed in `public-surface.json`.
- `llm_kit.graph.prompt.PromptFile`, `parse_prompt_file(data)` and `read_prompt_file(path)`: a prompt file's header, body, slots and body SHA-256. `llm_kit.graph.loader.prompt_parts(text)`: header and body.
- `tests/formats/`: shared cases for the project model file (85) and the prompt file (32).
- `examples/validate_and_render.py`.

Changed:

- Prompt files (`llm-kit.prompt-file/v1`). Every reader removes one leading byte order mark and reads CRLF and lone CR as LF before it looks for the marker line. Before, `prompt_body`, `prompt_slots` and `render_prompt` split text only on LF and kept a byte order mark, while `render_prompt_file` and the graph loader read files with CRLF and CR changed to LF. Text with CR now reads the same through every function. A file that starts with a byte order mark now finds a marker on its first line, and its body no longer starts with U+FEFF. Graph node hashes of files with only LF and no byte order mark do not change.
- The project model YAML reader refuses merge keys (`<<`), `!!omap` and `!!pairs`. Before, a merge key was expanded and `!!omap` and `!!pairs` were read as nested lists.
- The project model file refuses an integer outside -(2**53 - 1) to 2**53 - 1 in `extras` and in `max_output_tokens`, because other JSON readers cannot hold it exactly.
- A value in `extras` that holds itself through a YAML alias is refused as `invalid`. Before, it caused a `RecursionError`.

Quality bar for the pieces above (`llm_kit.schema.formats`, the three formats, `llm_kit.registry.project_models`, the new names in `llm_kit.graph.prompt` and `llm_kit.graph.loader`):

1. Public API documented: pass. Each listed name has a docstring with its purpose, inputs, outputs and errors (constants have `#:` comments). `public-surface.json` lists them; `tests/llm-kit/test_formats.py` and the graph surface tests compare the list with each module's `__all__`.
2. Semver release with a changelog: pass. Version 1.25.0 in `pyproject.toml`; this entry. Versions before 1.25.0 have no entries here; `packages/released.json` records their release commits.
3. Tests for each public function: pass. Each listed function is called in `tests/llm-kit/test_formats.py`, `test_project_models.py` or `test_project_models_local.py`.
4. No client names, prompts or data: pass for these pieces. The example and the shared cases use generic names.
5. Examples run without a client app: pass. `tests/packaging/test_examples.py` installs the built wheels in a clean venv and runs each example.
6. JSON Schema for each file format: pass. The three schemas ship in `llm_kit.schema.formats`. The shared-case tests validate reader output against them, and the writer tests (`render_yaml`, `render_local_yaml`, `write_project_models`, `write_local_overrides`, `set_local_override`, `graph.testing.make_project`) validate written files against them.
7. Gate before adoption: recorded in this entry.

Open-label corpus mining. Pure functions for the map-reduce half of an LLM corpus analysis; the consumer owns the corpus, prompts and model calls. The contract is `docs/reference/llm-kit-mining.md`.

Added:

- `llm_kit.pipeline.analysis.mining`: `stable_batches` and `batch_key` (`Batch`, `KEY_CHARS`): chunk keys that survive appends to a group; `normalize_label` and `label_inventory` (`LabelCount`); `Taxonomy.from_answer`, `family_of` and `coverage` (`Family`, `TaxonomyError`, `OTHER`): a model's label families, checked and applied by code; `family_stats`: records, distinct counts, flag shares and detail tallies per family.
- `examples/mine_labels.py`.

Quality bar for `llm_kit.pipeline.analysis.mining`:

1. Public API documented: pass. Each name has a docstring (constants a `#:` comment) and is listed in `public-surface.json`.
2. Semver release with a changelog: pass. Part of 1.25.0; this entry.
3. Tests for each public function: pass. `tests/llm-kit/test_pipeline_mining.py` calls each function.
4. No client names, prompts or data: pass. The tests and the example use generic data.
5. Examples run without a client app: pass. `examples/mine_labels.py` needs no extras; `tests/packaging/test_examples.py` runs it.
6. JSON Schema for each file format: not applicable. The module reads and writes no file; the taxonomy answer is checked by `Taxonomy.from_answer`, and a consumer passes its own output schema to its model call.
7. Gate before adoption: recorded in this entry.
