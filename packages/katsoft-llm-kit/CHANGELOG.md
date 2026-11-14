# Changelog: katsoft-llm-kit

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar". An entry records which bar lines pass for the pieces that version adds or changes.

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
