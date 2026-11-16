# Changelog: katsoft-llm-kit

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar". An entry records which bar lines pass for the pieces that version adds or changes.

## Unreleased

Changed:

- The shipped OpenRouter endpoints (`openrouter`, `or-qwen`, `or-gpt-mini`, `or-gemini-lite` in `DEFAULT_MODEL_CONFIG`) declare `billing: {mode: provider-reported}`. OpenRouter's `usage.cost` is the amount it charged, so a call on them reports it as the authoritative cost (`cost.usd`, `cost.source` `provider`) instead of an estimate (`estimated_source` `openrouter.usage.cost`). `llm-kit runs compare` records it with `source: reported`, and a graph spend ledger settles on it. An endpoint that does not declare the billing mode keeps a native `usage.cost` as an estimate, as before; a user config layer that points `openrouter` at another base URL should drop the declaration.

Fixed:

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
