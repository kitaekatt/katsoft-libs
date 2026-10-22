# databench integration plan: stages S2 to S6 (re-plan of 2026-10-22)

Status: plan only (unit `replan-databench-s2-s6`, 2026-10-22). No code changed. Code facts are
as of plugins-kit dev `e0ef0f13` (task-kit 0.13.0, llm-kit 0.4.0) and databench `902d6ef`.
Paths in plugins-kit are repository-relative; databench paths start with `databench:`.

Sources this plan re-cuts: `databench-integration-review.md` (stages S0 to S6, user
decisions, distribution investigation), `summary-on-commit-plan.md` (task questions and
answers), `detail-view/` (rounds 1 to 4), `docs/planning/llm-kit/cost-class-consent-plan.md`
(S1, shipped as llm-kit 0.4.0), and the user rulings in the task-kit-extraction `plan.md`.

## 1. Rulings this plan carries

Earlier rulings (verbatim in the task-kit-extraction `plan.md`): host "Yes, retire task-kit's
(Recommended)"; switches "One set per machine (Recommended)"; index pages "A drop down
configures what type of regen is enabled. It all is by default."

Rulings of 2026-10-22, verbatim:

> ok that particular concern is offloaded, for now it's just fine that databench requires
> plugins-kit and bootstrap to be installed, later we will switch to .venv if we implement this
> task

("this task" is the plugins-kit task `dev/tasks/shared-lib-packaging`, a feasibility study for
making `llm_kit` and `bootstrap_lib` installable into a .venv as versioned packages.)

> I think whatever we are doing right now is likely temporary, vs final. It's handling the fact
> that databench isn't public, but needs an installer for tasks-kit which is public (currently)

Consent question "Should every unattended llm-kit caller honor the per-machine consent file
(regeneration / usage / spend) by default?": the user picked "All callers by default".

## 2. Premise outcomes

**[E]** = measured or read by this unit; **[H]** = hypothesis.

| Premise | Outcome | Evidence |
|---|---|---|
| databench's CLI imports the index-pages runner at import time, and the runner refuses unless `llm_kit` >= 0.1.9 and `bootstrap_lib` >= 0.135.0 are linked | CONFIRMED [E] | `databench:src/databench/cli.py:43` imports `index_pages_cli`, which imports the runner at `index_pages_cli.py:126`; `index_pages_runner.py:72-73,159` |
| databench is provisioned by its project manifest | CONFIRMED [E] | `databench:.claude/bootstrap.json`: tool `node18`, tool `databench-launcher`, plugin `plugins-kit:llm-kit` >= 0.1.9, `project_venv.shared_lib_imports` `llm_kit` and `bootstrap_lib`, `project_npm` |
| databench source imports no task code | CONFIRMED [E] | no file under `databench:src/databench` mentions `task_system`, `task_kit` or `task-kit` |
| h1: databench should reach task-kit through task-kit's processes (CLI with JSON) rather than import `task_system` | CONFIRMED, refined: one-shot CLI verbs for reads and answer passes, and task-kit's own engine process for runs and authoring (section 3) | below |
| h2: task-kit's CLI already has usable JSON output | PARTLY CONFIRMED: JSON exists but does not cover the Tasks view; the full read model exists as a function with no CLI | below |
| h3: "All callers by default" is an llm-kit default flip plus migration of existing unattended callers | CONFIRMED, with three paths that no llm-kit default reaches as built | below |

**h2 evidence [E].** `task.py list --format json` emits `schema_version: "1"` for ONE project root
(`scope: project`), 0.62 s wall for 19 tasks (195 KB, mostly `updates`); it carries no
`task_items`, needs, hand-off commands or run state. `task.py sessions --format json` exists.
`show`, `items` and `status` print text only (`task.py:888-940`). The `task-kit` command's
verbs are `status`, `stop`, `restart`, `serve`, `characterize` (`dashboard/launcher.py:560`),
none of which prints task data. The complete read model is
`dashboard/snapshot.dashboard_payload(Inventory().snapshot())`, served only as the
token-protected `GET /api/v1/dashboard`: `schema: 1`, a `revision`, 65 tasks in 14 projects,
310 KB, 0.65 s cold on this Mac, with per-task `items`, `needs`, `sessions`, `handoff`,
`findings` and `summary`; item detail with its plan.md section is `snapshot.item_detail`
(`GET /api/v1/items/<project>/<task>/<item>`). Gap closed in S4-U1 by one verb over those two
functions. I did not treat this as a halting refutation: the brief anticipated the gap ("check
what else the Tasks view needs").

**h3 evidence [E].** In llm-kit 0.4.0 only `describe(allow=)`, `run(allow=)` and `models describe
--allow` filter, and the default is no filtering (`dispatch/declaration.py:663,1072`;
`plugins/llm-kit/CLAUDE.md`, "Opt-in is per caller"). Unattended callers found by grep over
plugins-kit, databench and the other `$DEVROOT` repositories:

| Caller | Path to a model | Reached by an llm-kit default? |
|---|---|---|
| databench index pages | `llm-kit completion complete --models <one id>` (`index_pages_runner.py:1304`); a harness adds `--requirements` | Harness: yes (`declaration.run`). A single transport entry with no requirements takes the original one-call path byte for byte (`cli/completion.py:204-214`): NO |
| databench content markup | `llm-kit jobs run` job file with `models` (`markup.py:57,1275`) | yes (`jobs/select.py` calls `describe(caller="process")`) |
| task-kit dashboard runs | `llm_kit.cli jobs run` (`dashboard/jobs_edge.py:386`) | yes |
| task-kit endpoint helpers | `declaration.run` (`dashboard/jobs_edge.py:357`) | yes |
| task-kit `review` summaries | constructs `CodexCliBackend` directly (`summary_ops.py:85,122`) | NO |
| workflow-kit `openrouter` nodes | `declaration.run` over `default_declaration()`, shipped as `["openrouter"]`, a spend transport (`scripts/openrouter_run.py:411,539`; `registry/models.py:328-344`) | yes |
| md-domain audit jobs | prints `llm-kit jobs run` for the agent (`skills-kit .../emit_audit_jobs.py:910`); declaration luna then sonnet (usage) | yes |
| llm-kit pipelines | `pipeline.runtime.backends` resolves through `describe` | yes |
| llm-kit `claude_bg` waves | launches `claude` background sessions without `describe` (`pipeline/execution/drivers/claude_bg.py`) | NO |
| flecs-ecs `tools/databench.sh` | `llm-kit jobs` passthrough with free `qwen38-*` endpoints | yes; unaffected (free) |
| codex-kit md-domain lane instruction | `llm-kit completion complete --models opus` (single entry) | NO (same direct path as index pages); an attended Codex lane |
| Attended: git-kit and p4-kit review lanes | `llm_kit.dispatch.review_lane` calls `describe(caller="process")` (`review_lane.py:423`) | yes, which would gate a review run with the user present (Q1) |
| Attended: orchestrate | `describe(caller="session")` | only if the session default flips (Q1) |

Measured on this Mac: `~/.claude/plugins/data/plugins-kit/llm-kit/consent.json` is absent
(free-only once the default flips); `~/.config/databench/generate.yaml` lists only
`qwen38-5090` (free) with both harnesses false, so databench generation here is unaffected;
task-kit dashboard runs (claude harness, usage) stop until usage consent is given.

## 3. Decisions this plan makes (reported, not asked)

1. **h1: process boundary, no `task_system` import.** databench reaches task-kit through
   JSON-speaking task-kit processes: one-shot `task-kit tasks ...` verbs (reads, answer passes)
   and, for runs and authoring, task-kit's existing server in a headless mode that databench's
   service supervises (S5; Q3 asks the user to confirm that part). Reasons:
   - No library export. `task_system` sits in `plugins/task-kit/skills/task/scripts/` beside
     `task.py`, `task_hold.py`, `task_session_start.py` and a vendored `bootstrap_guard.py`, so a
     `shared_libs` entry would put those on databench's `sys.path` too, or force a relocation
     with deprecation shims. It hard-imports `skills_kit_lib` (`discovery.py:83-84`), so
     databench would also link skills-kit and add psutil and openai to its venv.
   - Version skew is diagnosable. A shared-lib link pins no version (`plugins/CLAUDE.md`, "The
     bootstrap-provisioned venv and shared libs"); a JSON envelope carries a `protocol` number,
     so absent, too old and failed are three distinct messages in one place.
   - The scheduler's correctness rests on in-process locks with a documented order
     (`dashboard/scheduler.py:42-56`) and one process owning the runtime database. Keeping
     that process task-kit's avoids porting it, and `task_hold.py` needs no retarget.
   - It is independent of the distribution question the user called temporary: moving
     databench to a .venv (`shared-lib-packaging`) changes how `llm_kit` reaches databench,
     not how tasks do.
   - Task code runs in task-kit's own venv, which its runners already need.
   - Cost [E]: about 0.65 s per cold snapshot, so databench caches (2 s minimum interval,
     explicit refresh); never per keystroke.
2. **Order by value.** The read-only Tasks view (S4) ships first: it needs neither a library nor
   a service, and works in the foreground `databench` server.
3. **Project set.** The Tasks view passes databench's expanded development roots to task-kit
   as `--parent` (task-kit's `Inventory(parents=...)`, `dashboard/inventory.py:273-339`);
   without it task-kit uses `project_directories`. On this Mac both are `$DEVROOT`.
4. **Reach.** Per the 2026-10-22 ruling, a machine gets databench from its checkout and project
   manifest; no user-layer manifest entry is added. Every task-kit edge to databench DEGRADEs:
   the SessionStart hook logs and skips, and `task-kit` refuses with the remedy.
5. **Session start.** task-kit's SessionStart hook runs `databench service ensure` when
   databench is on PATH (the round-3 ruling "let's do Claude code sessions start it as well"),
   behind a task-kit setting that defaults to on (razor).
6. **Regeneration state** is a machine-written databench file
   (`<config root>/databench/regeneration.json`), not a key in the hand-edited
   `generate.yaml`, so a UI toggle never rewrites a commented user file.
7. **No CLI operation named `tasks` in databench.** `databench <operation>` collides with a
   directory of that name (README "Starting the server"), and `$DEVROOT/tasks` exists; use
   `databench --view tasks`.

## 4. Stage order

| Order | Stage | Goal | Repos | Depends on | User-visible when shipped | An early stop leaves |
|---|---|---|---|---|---|---|
| 1 | S4 Tasks view (read-only) | tasks inside databench | plugins-kit (task-kit), databench | none | `databench`, Tasks: every project's tasks, project dropdown, sections, needs, items, working sessions (unverified tinted), item detail | tasks visible in databench; task-kit's dashboard still queues and authors |
| 2 | SC Consent by default | every unattended llm-kit call honors usage and spend consent | plugins-kit (llm-kit, task-kit, workflow-kit), databench | none (llm-kit units run beside S4) | switched-off entries named in output with the `llm-kit consent set` remedy; databench usage and spend checkboxes; `generate.yaml` v2 | consent honored everywhere; no background service |
| 3 | S2 Task answers (by hand) | orientation and decisions answers in the databench detail view | plugins-kit (task-kit), databench, claude-settings | S4; SC-U5 before S2-U7 | detail view: Orient, Decide, Trace with answerer, as-of commit and stale marker; `task.py answers refresh` | answers refreshed by hand or by `review` |
| 4 | S6 Shared generated content | markup labels tracked in `.databench/` | databench | none (parallel track) | labels follow git between machines | labels shared; nothing regenerates in the background |
| 5 | S3 databench service | always-on per-machine service with background regeneration | databench, plugins-kit (task-kit hook), claude-settings | SC, S2, S6 (markup default) | `databench service status`; regeneration switch and type drop-down; answers, markup and index pages refresh on their own | one service, tasks still run from task-kit's dashboard |
| 6 | S5 Full move | runs and authoring in databench; task-kit's dashboard UI retired | plugins-kit (task-kit), databench | S3, S4; Q3 | one UI: queue, run, cancel, retry, authoring, dismiss in databench; `task-kit` opens the databench Tasks view | (end state) |

Stage ids keep the review's numbering for traceability (S4 was "read-only Tasks view", S2 was
"task library"); the review's S2 library export is dropped (decision 1), S0's documentation
items fold into S4-U4 and S5-U5, and SC finishes S1's databench half (switch file v2), which did
not ship (`databench:src/databench/generate_config.py` still gates on `harnesses`).

## 5. Stages

Each unit names the files it owns; no two units that can run at the same time share a file.
Every check names its revert and must go red under it (`docs/reference/vacuous-checks.md`).

### Stage 1: S4 Tasks view (read-only)

**S4-U0 protocol freeze** (plugins-kit). Owns
`plugins/task-kit/skills/task/references/tasks-json-protocol.md` and
`tests/task-kit/fixtures/tasks_json/` (one fixture project, its expected snapshot and item
envelopes). Interface, for every `task-kit tasks` verb with `--format json`:

```
exit 0: {"protocol": 1, "task_kit_version": "<x.y.z>", "kind": "<kind>", "payload": {...}}
exit 1 failure, 2 usage, 3 unknown task or item:
        {"protocol": 1, "error": {"code": "<code>", "message": "<text>"}}
Additive keys keep protocol 1; a removed, renamed or retyped key raises it.
task-kit tasks snapshot --format json [--parent DIR]... [--refresh]
    kind "snapshot", payload = dashboard_payload(inventory, server=None, runtime=None)
task-kit tasks item --checkout ID --task KEY --item ID --format json
    kind "item", payload = item_detail(task, item_id)
```

**S4-U1 task-kit read verbs** (plugins-kit; after U0). Owns
`plugins/task-kit/skills/task/scripts/task_system/dashboard/read_cli.py` (added),
`dashboard/launcher.py` (a `tasks` first argument delegates to `read_cli.main`),
`tests/task-kit/test_tasks_read_cli.py`. The verbs only read: no lock, runtime database or
`server.json`. Checks: `test_snapshot_matches_golden` (revert: emit `task.py list` JSON -> red);
`test_parent_replaces_configured_directories` (revert: ignore `--parent` -> configured projects
appear -> red); `test_unknown_item_exit_3` (revert: exit 0 with null -> red);
`test_read_verbs_write_nothing` (tree and data-dir digests equal before and after; revert: call
`Inventory.invalidate` through a writer -> red).

**S4-U2 databench task host and routes** (databench; after U0, beside U1 using a fake
executable). Owns `databench:src/databench/tasks_host.py` (added),
`databench:src/databench/server.py` (two routes), `databench:src/databench/path_sources.py`
(a `task-identity` source: the caller submits a task identity that task-kit resolves against
its own inventory; databench walks no client path), `databench:tests/path_route_registry.py`,
`databench:tests/test_tasks_host.py`.

```python
TASK_KIT_PROTOCOL = 1
class TaskKitUnavailable(Exception): code: str  # task_kit_absent | task_kit_too_old | task_kit_failed
def resolve_task_kit() -> str
    # $CLAUDE_BOOTSTRAP_DATA_ROOT, else ~/.claude/plugins/data: plugins-kit/task-kit/bin/task-kit[.cmd]; then PATH
class TaskKitClient:
    def __init__(self, executable=None, *, runner=subprocess.run, min_interval_s=2.0, timeout_s=30.0)
    def snapshot(self, parents: Sequence[Path], *, refresh: bool = False) -> Mapping  # cached, one call in flight
    def item(self, checkout: str, task: str, item: str) -> Mapping
GET /api/v1/tasks?project=&q=&refresh=1
    200 {"available": true, "task_kit_version": ..., "payload": ...}
    200 {"available": false, "reason": {"code": ..., "message": ...}}
GET /api/v1/task-item?checkout=&task=&item=        path source: task-identity
```

Checks: `test_protocol_two_refused` (revert: skip the protocol check -> served -> red);
`test_absent_and_too_old_differ` (revert: one message -> red); `test_snapshot_cached_within_interval`
(fake runner call count; revert: no cache -> red); the route registry's exact-equality test
(revert: drop the declaration -> red); `test_remote_get_allowed_with_token` (Q5 default).

**S4-U3 databench Tasks view** (databench web; after U2's route shapes). Owns
`databench:web/src/tasks.tsx`, `databench:web/src/tasks.test.tsx`,
`databench:web/src/main.tsx` (route kinds `tasks` and `task` outside any root, a header entry;
the prefix must not equal a root slug, tested). Layout: prototype A's list with the project
dropdown (approved 2026-10-17); detail opens in place with the Orient region (legacy `summary`
labelled legacy, items, sessions) and, if Q4 picks C, empty Decide and Trace tabs filled in S2.
Read-only: no Queue, Dismiss or authoring control. Checks (vitest): fixture renders every
section; project filter narrows; `available: false` renders the reason text; an unverified
session is tinted.

**S4-U4 docs and release** (both repos). Owns `databench:README.md` (a "Tasks" section; the
boundary rule at README:25-27 admits first-party apps through a host adapter, as
`index_pages_host` does), `databench:CLAUDE.md` (directory map), task-kit `references/dashboard.md`
(the read verbs), task-kit `plugin.json`, `pyproject.toml` (next free minor),
`docs/planning/task-kit-dashboard/architecture.md` (decision 1 superseded, pointing here).
Verify: `uv run --extra dev pytest tests/task-kit/test_tasks_read_cli.py`; databench `uv run
pytest tests/test_tasks_host.py tests/test_path_route_registry.py tests/test_path_oracle.py` and
`cd web && npm test && npm run typecheck`; claudx smoke of `task-kit tasks snapshot`;
git-code-review over the range; `publish.py --only task-kit` (pre-authorized); databench push;
on this Mac, `databench --dev`, open Tasks, compare counts with the task-kit dashboard.

### Stage 2: SC Consent by default

Hazard for every SC and S3 unit [E]: databench's index-pages generator fingerprint hashes
`index_pages*.py`, `index_pages_refs/`, `assets/` and the llm-kit modules
`pipeline.execution.{scheduler,deadlines,failure_cache}`, `storage.atomic_json` and
`pipeline.runtime.gate` (`databench:src/databench/index_pages_worktree.py:846-882`). Editing any
of them makes every repository's index pages stale. No unit below touches them; each databench
unit's verification includes the fingerprint before and after.

**SC-U1 llm-kit default** (plugins-kit; first). Owns `plugins/llm-kit/lib/llm_kit/registry/consent.py`,
`dispatch/declaration.py`, `tests/llm-kit/test_consent_default.py` (added),
`tests/llm-kit/test_allow_filter.py`.

```python
CONSENT: Final = <sentinel>      # load_consent() at call time
DEFAULT: Final = <sentinel>      # describe's default: CONSENT for caller="process",
                                 # None for caller="session" (Q1)
def resolve_allow(allow, *, caller: str) -> tuple[frozenset[str] | None, str]
                                 # (allowed classes or None, note)
describe(..., allow=DEFAULT)
run(..., allow=CONSENT)          # run is always a process caller
# allow=None stays "no filter, nothing classified or probed" (the attended exemption);
# an explicit iterable wins over consent.
Ranking.consent_note: str        # set when the consent default switched an entry off;
                                 # render() adds "Consent: <note>"; the floor message names
                                 # `llm-kit consent set --usage on` / `--spend on`
```

A harness entry is decided without an auth probe when consent allows both or neither of usage
and spend. Checks: `test_default_switches_off_harness_without_consent` (revert: default None ->
dispatched -> red); `test_explicit_none_byte_identical` (C15's literals, with spies proving no
classify or probe; revert: treat None as consent -> red); `test_session_default_unfiltered`;
`test_floor_carries_note` (revert: drop the note -> red); `test_no_probe_when_outcome_fixed`
(probe spy; revert: probe always -> red).

**SC-U2 llm-kit entry points** (plugins-kit; after U1's interface). Owns
`plugins/llm-kit/lib/llm_kit/cli/completion.py`, `cli/models.py`, `cli/jobs.py`,
`jobs/select.py`, `tests/llm-kit/test_cli_completion_consent.py`,
`tests/llm-kit/test_jobs_consent.py`. `complete` applies consent on the single-entry direct
path too (a switched-off entry exits with the floor envelope before any call); `complete`,
`jobs run` and `models describe` take `--allow CLASSES` and `--consent apply|ignore` (default
apply for process callers). The job ledger gains no key: allowance is per `jobs run`
invocation, never in `definition_json` or event bytes (schema 13 is frozen,
`plugins/llm-kit/CLAUDE.md`); if a recorded payload would change, stop and report. Checks:
`test_single_entry_spend_switched_off` (revert: keep the byte-for-byte path -> dispatched -> red);
`test_jobs_run_honors_consent`; `test_ledger_bytes_unchanged` (golden ledger rows).

**SC-U3 llm-kit waves and attended exemption** (plugins-kit; after U1). Owns
`pipeline/execution/drivers/claude_bg.py` (`dispatch_wave(..., allow=CONSENT)` classifies the
`claude` harness once and refuses the wave before any launch, naming the note),
`dispatch/review_lane.py` (passes `allow=None`, per Q1), `tests/llm-kit/test_claude_bg_consent.py`,
`tests/llm-kit/test_review_lane_consent.py`. Checks: a wave without usage consent launches
nothing (launch spy; revert -> launched -> red); a review lane without consent still ranks its
claude entry (revert: drop `allow=None` -> floor -> red).

**SC-U4 llm-kit docs and release** (plugins-kit; last). Owns `plugins/llm-kit/CLAUDE.md`
("Cost classes and consent": the default is consent for process callers; attended callers pass
`allow=None`), `README.md`, `references/jobs.md`, `references/completion-seam-contract.md`,
`plugin.json`, `pyproject.toml` (next free minor), and the "Who talks to an LLM" table in
`plugins/CLAUDE.md`. Verify: `tests/llm-kit/test_import_graph.py` plus the files above; claudx
smoke (`llm-kit consent show`; `models describe <ids> --caller process` without consent lists
switched-off entries); publish (llm-kit is pre-authorized).

**SC-U5 task-kit callers** (plugins-kit; after SC-U4 published). Owns
`task_system/summary_ops.py` (classifies `luna` against `load_consent()` and skips repair with
the note when not allowed), `task_system/dashboard/jobs_edge.py` and `dashboard/translate.py`
(Queue refuses, non-blocking, with "usage consent off: `llm-kit consent set --usage on`" before
writing a job file), `tests/task-kit/test_task_consent.py`, task-kit version files. Checks:
review without consent writes no summary and prints the note (revert -> codex called -> red);
Queue without consent returns the reason (revert -> run created -> red).

**SC-U6 workflow-kit** (plugins-kit; after SC-U4). Owns `plugins/workflow-kit/scripts/openrouter_run.py`
(prints `Consent.note` in the node's failure report), its tests and docs, version files. Check:
an `openrouter` node without spend consent fails with the note (revert: swallow -> red).

**SC-U7 databench generate config v2 and consent controls** (databench; after SC-U4 published
and S4-U2/U3 merged, because it shares `server.py`, `main.tsx` and the route registry). Owns
`databench:src/databench/generate_config.py` (v2: `version: 2`, `models` only; v1 still loads,
and a true harness prints "run `llm-kit consent set --usage on` to keep harness models",
Q2), `databench:src/databench/generate_cli.py` (pre-filters each list through `llm-kit models
describe --caller process --json` and skips an operation whose list filters to empty with a
status line instead of exiting 2; the runner is not edited), `databench:src/databench/switches.py`
(added: reads consent through `llm_kit.registry.consent`, probing `save_consent`),
`databench:src/databench/server.py` (`GET /api/v1/switches`; `PUT /api/v1/switches` for
`usage` and `spend`, write header required, local clients only, `403 switches_remote` otherwise;
path source `none`), `databench:tests/path_route_registry.py`, `databench:web/src/switches.tsx`
(+ test; checkboxes show the consent statement `llm-kit consent set` prints), `databench:tests/test_switches.py`,
`databench:tests/test_generate_config.py`, README "Generate". Checks: a remote PUT is refused
(revert -> written -> red); a list that filters to empty is skipped with exit 0 (revert -> exit 2
-> red); the generator fingerprint is unchanged.

User-visible: a run whose entries consent switches off says which and how to allow them;
databench shows usage and spend checkboxes. Migration on each machine: the user runs `llm-kit
consent set --usage on` (or ticks the box) where dashboard runs, harness markup or audit jobs
should keep running (Q2).

### Stage 3: S2 Task answers (by hand)

`summary-on-commit-plan.md` units U1, U2, U4, U5, U7 and U10 keep their scope, interfaces and
checks, with these changes; U3 is re-cut; U6 and U8 move to databench (section 6).

- **S2-U1 catalog, inputs, scripts**: unchanged.
- **S2-U2 store and readers**: also owns `task_system/dashboard/snapshot.py`: `task_json` gains
  `answers` (`{qid: {state, answer, answerer, as_of, fingerprint}}`) and `coverage`; protocol
  stays 1 (additive). Added check: the S4 golden snapshot gains the keys and nothing else.
- **S2-U3 config and chain (re-cut)**: `answers_config` drops `service` and `spend` (no switch in
  `task.local.yaml`); `AnswersConfig(models, effort, batch_size, parallel)`. Phases are allow
  sets: phase 1 `run(..., allow={"free"})`, phase 2 `run(..., allow=consent.allowed)` when that
  set holds `usage` or `spend`. `answerer.tier` is `free`, `usage` or `spend` from
  `llm_kit.registry.cost.classify`. The statement text is llm-kit's. Probe: `run` accepts
  `allow` and `llm_kit.registry.consent.load_consent` imports (absent and too old differ). Checks:
  the summary plan's U3 checks with "spend" read as "consent"; luna (usage) is not dispatched with
  `usage: false, spend: true` (revert: treat harness entries as spend -> red).
- **S2-U4 engine and CLIs**: as planned, plus `read_cli.py` verbs
  `task-kit tasks answers refresh --format json [--parent DIR]... [--batch N] [--stop-file PATH]
  [--deadline-s N]` (kind `answers-pass`: per repository answered, superseded, skipped, unpublished,
  notes, and the consent read) and `task-kit tasks answers status --format json`. The pass checks
  the stop file at the D4 safe points and exits only between phases. Added check: a stop file
  written during `answering` ends the pass before `commit` with exit 0 and no partial file
  (revert: ignore it -> red).
- **S2-U5 publisher**, **S2-U7 task.py, review, validate** (after SC-U5), **S2-U10 fleet wiring**:
  unchanged.
- **S2-U8 databench detail view** (databench web; after S2-U2 published). Owns
  `databench:web/src/task-detail.tsx` (+ test): Orient (orientation headline, state, next;
  activity age; sessions), Decide (decisions with stated defaults; needs), Trace (task-record
  commits, item states, log entries); each answer shows answerer, as-of commit and a stale
  marker. Read-only; "Accept the defaults" waits for S5.
- **S2-U9 docs and release**: as planned minus the Windows service gate (moved to S3) and minus
  `task-kit service` text.

User-visible: the databench detail view orients first with provenance; `task.py answers refresh`
and `task.py review` fill answers; nothing runs in the background.

### Stage 4: S6 Shared generated content (parallel track)

databench task `shared-generated-content`: items `design-amendment` and `tracked-label-store`
(labels move from `~/.cache/databench/markup/<key>/labels.json` into `.databench/`). Owns
`databench:src/databench/markup.py`, `databench:docs/markup-design.md`, markup tests. Its
`page-location-decision` (blocked on the user) and `cross-machine-verify` stay in that task.
Gate: S3 includes content markup in the default regeneration types only after
`tracked-label-store` lands; otherwise every machine pays for the same labels.

### Stage 5: S3 databench service

**S3-U1 lifecycle** (databench). Owns `databench:src/databench/service.py` (added),
`databench:src/databench/cli.py` (`databench service ensure|stop|status|run [--json]`;
`--view tasks`; bare `databench` ensures the service and opens the browser; `--dev` stays
foreground), `databench:pyproject.toml` (psutil), `databench:tests/test_service.py`. State in
`<user cache root>/databench/service/`: `service.lock`, `service.json` (pid, process stamp,
nonce, code version, phase), `service.stop`, `service.log`; ported from task-kit's
`procspawn`/`procstamp` design (`architecture.md`, "Server discovery, port, and code identity").
Checks: the summary plan's U6 checks, re-homed (second `ensure` starts nothing; a stale stamp
signals nothing; `stop` during a commit phase waits; stderr reaches the log; an older code
version is restarted).

**S3-U2 background loop and regeneration switch** (databench; after U1). Owns
`databench:src/databench/background.py` (added), `databench:src/databench/server.py` (startup
wiring; markup launch stderr to `service.log` instead of DEVNULL, `server.py:676-681`;
`GET /api/v1/service`; regeneration in `PUT /api/v1/switches`), `databench:src/databench/switches.py`
(`regeneration.json`: `{"schema": 1, "enabled": bool, "types": [...], "updated_at": ...}`;
types `task-answers`, `content-markup`, `index-pages`, all by default per the ruling; absent
file: `enabled` false, Q6), route registry, `databench:tests/test_background.py`. Operations,
each skipped unless enabled and its type selected; consent applies through llm-kit (SC):
`task-answers` runs `task-kit tasks answers refresh --format json --stop-file <service dir>`;
`content-markup` uses the existing enqueue and launch; `index-pages` runs the generate CLI's
index-pages phase per repository at the Q6 cadence. Checks: switch off runs nothing (revert ->
ran -> red); a type removed from the list is not run; the fingerprint is unchanged.

**S3-U3 service UI** (databench web; after U2's routes). Owns `databench:web/src/service-panel.tsx`
(+ test): running state and phase, per-operation last pass and pending counts, the regeneration
switch and type drop-down.

**S3-U4 start trigger** (plugins-kit). Owns `plugins/task-kit/hooks/hooks.json` (an async
SessionStart entry for `startup`, `resume`, `clear`), `plugins/task-kit/hooks/sessionstart/databench-ensure.sh`
(added; databench absent or failing to import: one line in task-kit's data-dir log, exit 0),
`tests/task-kit/test_databench_ensure_hook.py`, task-kit version files. Check: a `resume`
input with no session registrations still runs `ensure` (revert to the sessions fast path ->
red); absent databench exits 0 and logs (revert: exit 1 -> red).

**S3-U5 preference wording** (claude-settings). Owns `USER-PREFERENCES.md` insight
`generated_index_pages`: index pages regenerate when the user's regeneration switch includes
`index-pages`; agents still do not schedule them by hand.

**S3-U6 release**: Windows smoke over ssh (the service runs detached after the starting session
ends and stops through the protocol), or ship with the gap stated; git-code-review; databench
push; `publish.py --only task-kit`.

User-visible: databench is always on where a Claude session or `databench` started it; answers,
labels and index pages regenerate under the switch, types and consent.

### Stage 6: S5 Full move and retirement (Q3)

**S5-U1 task-kit engine** (plugins-kit). Owns `dashboard/launcher.py`
(`task-kit engine ensure|status|stop --format json`; status payload `{running, url, token}` read
from `server.json`), `dashboard/server.py` (`serve --headless`: API only, no static routes, no
browser), `tests/task-kit/test_engine_cli.py`. The scheduler, runtime database, session
reconcile pass, run re-adoption and `task_hold.py`'s handshake stay as they are.

**S5-U2 databench supervision and proxy** (databench). Owns `databench:src/databench/tasks_host.py`
(engine supervision: ensure on service start, stop on service stop; reads switch to the engine's
`GET /api/v1/dashboard` when it runs, for run state), `databench:src/databench/server.py`
(`/api/v1/task-engine/<route>` forwarding exactly task-kit's `MUTATION_ROUTES` plus its run GET
routes; the engine token never reaches the browser; every forwarded write is local-only,
`403 task_action_remote`), route registry, `databench:tests/test_task_engine_proxy.py`. Checks:
a contract test compares the forward table with `MUTATION_ROUTES` (revert: drop a route -> red);
a cross-origin browser request without the write header is refused (the review's open [H] on
CSRF); a remote client's Queue is refused.

**S5-U3 databench UI parity** (databench web). Owns `databench:web/src/task-actions.tsx` (+ tests):
Queue, Queue all, Remove, Cancel, Retry, run options, run log, Add item, Defer and Undefer, New
task, Re-check, session Dismiss, "Accept the defaults". Parity list: task-kit 0.3.0 to 0.13.0
features in the task-kit-extraction `plan.md`.

**S5-U4 retirement** (plugins-kit; after the user tries S5-U3, per the anti-pattern "building UX
beyond the approved prototype A without the user's review"). Owns `dashboard/static/` (deleted),
`dashboard/launcher.py` browser code (bare `task-kit` runs `databench --view tasks`, refusing
with the remedy when databench is absent), task-kit `SKILL.md` (`/task dashboard`),
`README.md`, `references/dashboard.md`, version files.

User-visible: one UI. `task-kit` and `/task dashboard` open databench's Tasks view.

## 6. Changes to `summary-on-commit-plan.md`

- **D2.** Three classes from llm-kit (free, usage, spend); `luna` and `haiku` are usage.
  "Spend is the consent" becomes the llm-kit consent file; its statement is llm-kit's and is
  shown by databench's checkboxes. Two phases stay, expressed as `allow` sets.
- **D4.** The host is the databench service (S3). Deleted: `task-kit service ...`,
  `<data dir>/answers/service.{json,lock,stop,log}`, `hooks/sessionstart/service-ensure.sh`.
  The pass algorithm (prepare, answer, commit) stays task-kit code, run by databench as
  `task-kit tasks answers refresh --format json --stop-file`. The row "inside the dashboard
  server: Rejected" reverses for the databench service: it is detached, so closing the browser
  does not stop it, and every verb still works without it (principle 9). Switches: `answers.service`
  becomes databench's regeneration switch with type `task-answers`; `answers.spend` becomes
  llm-kit consent; `answers.poll_s` becomes the databench cadence.
- **D7.** "service off" reads "regeneration off"; "spend off, free entries down" reads "usage and
  spend not consented, free entries down".
- **Contract changes.** Added: `task-kit tasks snapshot|item|answers refresh|answers status`.
  Removed: `task-kit service ...` and its hook entry.
- **Submit gates.** Razor entries for the switches move to databench; task-kit keeps
  `answers.models`, `effort`, `batch_size` and the catalog.
- **Units.** U3 re-cut; U6 becomes S3-U1/U2; U8 becomes S2-U8 (databench web); U9's Windows gate
  moves to S3-U6; an llm-kit unit before U3 is no longer needed (llm-kit 0.4.0 and SC).

## 7. The detail-view redesign in databench

Round 4 put three concepts on the real screen (A question ledger, B situation brief, C intent
lenses); no pick is recorded (Q4). The data every concept uses is the same: the orientation
answer with as-of commit, time, answerer and freshness; decisions waiting on the user with
defaults; activity age; task-record commits; items; working sessions. What changes from the
mockups: the screen is databench's Preact shell rather than task-kit's `app.js`; the region
lives in the task detail route (S4-U3, S2-U8) rather than an inline row expansion; run controls
arrive in S5 instead of sharing the first screen. The phone rendering rule (explanation page,
paginated 430x932) applies to any further mockup round.

## 8. Open questions (recommended default in brackets)

- **Q1. Scope of "All callers by default".** Attended callers: the git-kit and p4-kit review
  lanes (`llm_kit.dispatch.review_lane`), session-caller `describe` (orchestrate, review
  ranking) and agent-run CLI calls such as the codex-kit md-domain lane. [Exempt the review
  lanes with an explicit `allow=None` and keep session callers unfiltered; every unattended
  process path honors consent. Without the exemption git-code-review floors on any machine
  without usage consent. Agent-run `complete` and `jobs run` calls honor consent.]
- **Q2. Consent migration.** [Never write consent automatically. `generate.yaml` v1 with a true
  harness prints the `llm-kit consent set --usage on` command; task-kit's Queue refuses with the
  same remedy. The user sets consent once per machine that should run harness work. The review
  proposed writing `usage: true` from `harnesses`; on this Mac both harnesses are false, so the
  difference shows only on other machines.]
- **Q3. Run host in S5.** A headless task-kit engine supervised by databench's service (one
  service owner and one UI; two processes) or the scheduler imported into databench through a
  `task_system` shared library. [Supervised engine; needed before S5 only.]
- **Q4. Detail-view concept.** [C, intent lenses: the user asked for "different views into a
  task and showing the right information and controls for that view".]
- **Q5. Remote (tailnet) access to tasks** (review decision 5, unanswered). [View only; every task
  write and run route local-only.]
- **Q6. Regeneration defaults.** The ruling sets the type drop-down to all by default. [The
  master switch is off until the user turns it on per machine, because index pages push commits
  to every repository's origin; index pages run per repository after its HEAD changes, at most
  once per 24 h.]

## 9. Blockers and outside dependencies

- None for S4 and SC. S2-U10 and S3-U5 change claude-settings. S3-U6 needs ssh to a Windows
  machine. S5 needs Q3 and the user's look at S5-U3 before S5-U4.
- Publishing task-kit and llm-kit is pre-authorized (task-kit-extraction `CLAUDE.md`, root
  `CLAUDE.md`); workflow-kit needs the user's go-signal. databench releases by push.
