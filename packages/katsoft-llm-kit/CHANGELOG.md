# Changelog: katsoft-llm-kit

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar". An entry records which bar lines pass for the pieces that version adds or changes.

## Unreleased (1.26.0)

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
2. Semver release with a changelog: pending. This entry; the version stays 1.25.0 until the release, which also carries ledger schema 15 (branch-mode workspaces).
3. Tests for each public function: pass. `test_session_types.py`, `test_claude_stream.py`, `test_jobs_session.py`, `test_jobs_ledger_v14.py`, `test_claude_session.py` and `test_jobs_agent_session.py` call each listed function; the session tests drive a fake `claude` (`tests/llm-kit/fixtures/fake_claude_session.py`) through stdin and stdout.
4. No client names, prompts or data: pass. The fake CLI and the tests use generic prompts.
5. Examples run without a client app: pending. No example exercises an agent session yet; one needs a logged-in Claude CLI.
6. JSON Schema for each file format: pass. The five session-directory files have schemas in `llm_kit/jobs/schemas/`; `test_jobs_session.py` validates every reader and writer against them; `events.jsonl` has the permissive event-line schema.
7. Gate before adoption: recorded in this entry; lines 2 and 5 are pending, so no client adopts agent sessions before the release.

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
