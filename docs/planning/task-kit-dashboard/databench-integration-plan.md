# databench integration plan: a modular databench with a tasks module (2026-10-22)

Status: plan only (unit `revise-databench-plan-modules`, 2026-10-22). No code changed. This
revision replaces the stage plan committed as `adced200` (stages S2 to S6 and a consent-default
stage); section 9 says what was dropped. Code facts are as of plugins-kit dev `adced200`
(task-kit 0.13.0, llm-kit 0.4.0) and databench `902d6ef`. plugins-kit paths are
repository-relative; databench paths start with `databench:`.

Sources: `databench-integration-review.md`, `summary-on-commit-plan.md`, `detail-view/`
(round 4; round 5 is being drawn separately), `docs/planning/llm-kit/cost-class-consent-plan.md`,
and the rulings in the task-kit-extraction `plan.md`.

## 1. Rulings this plan carries (2026-10-22, verbatim)

Direction, in order:

> ok i was thinking something similar lets talk this through a bit more.
> Task kit remains a fully functional plugin.
> All code related to the manager lives in databench.
> However, an implication of this is I want to make databench have different modules that can be
> enabled or disabled (we will later maker it so these can be installed and uninstalled, for now
> let's just make it enabled/disabled).
> This approach requires
> (1) identifying what should be core to databench
> (2) identifying what modules should exist, understanding that any module that exists must be
> able to run without dependencies on other modules
> My own thought is:
> - there's an explorer module which covered browsing repositories and generating summary data of
>   what files are
> - there's an html module which covers creating index.html files throughout the repository
> - there's a tasks module which covered showing the task dashboard

Follow-up answers (bracketed text is the question's context, not the user's words):

> (1) it sounds like the concept of a file summary is core, the data is the same, both explorer
> and html can generate these summaries when they need them, and both don't care how the data was
> generated. A task dashboard wouldn't generate these summaries because it doesn't need this data
> (or maybe it would generate summaries of just task files)

> (2) I agree comments should be core

> (3) sounds good [cross-module links are soft: shown only when the target module is enabled,
> through core's registry; no module-to-module imports]

> (4) sounds good [independence is enforced by an import-lint test plus each module's tests
> running with every other module disabled]

Answers to the previous revision's questions:

> (q1) the databench consent configuration is for databench and its modules only. Plugins-kit
> will manage consent in its own way and not use databench's settings. I don't think we need to
> make consent configurable for plugins-kit modules right now we could but I don't think it's
> necessary.

> (q2) we absolutely can and will ship databench with some consent on by default we have no
> restrictions on what our default configuration is.

> (q3) yes but we aren't focused on databench performing work on tasks right now, we shouldn't
> over invest here until the user need emerges. There is too much to do.

> (q4) open the pdf in my browser and i'll choose

Then, on the round-4 concepts: "C" (intent lenses).

> (q5) why are you asking this question

(Q5 is dropped: the tasks module follows core's access policy like every module.)

> (q6) on by default

On the tasks view:

> i do think we should unify the look and feel with databench in terms of style so can you please
> do a mockup of that. Also there shouldn't be a spend control there I think we have databench to
> configure spend

Still binding: "ok that particular concern is offloaded, for now it's just fine that databench
requires plugins-kit and bootstrap to be installed, later we will switch to .venv if we implement
this task" (`dev/tasks/shared-lib-packaging`); index pages "A drop down configures what type of
regen is enabled. It all is by default."; switches "One set per machine (Recommended)". The
earlier "All callers by default" consent answer is scoped by q1 to databench and its modules:
llm-kit's own default (no filtering unless a caller passes `allow`) is unchanged.

## 2. Premise outcomes

**[E]** = measured or read by this unit.

| Premise | Outcome | Evidence |
|---|---|---|
| `filesystem.py` imports `index_pages` | CONFIRMED [E] | `databench:src/databench/filesystem.py:21` (`PAGE_FILENAME`, `is_generated_page`, `parse_marker`, `MarkerError`); it also parses the `index_pages` config block (`:489-581`) and lazily imports `entities` |
| `paths.py` imports `index_pages` | REFUTED [E] | `paths.py` imports only the standard library (`:17-21`); the edge is the reverse, `index_pages -> paths`. Its docstring mentions `index_pages` |
| `server.py` imports `index_pages` and `index_pages_host` | CONFIRMED [E] | `server.py:28-29` |
| `index_pages_cli` imports `markup` | CONFIRMED [E] | `index_pages_cli.py:132` (`_cache_root`, `_file_lock`) |
| `generate_cli` drives markup and index pages | CONFIRMED [E] | `generate_cli.py` imports `markup`, `index_pages_cli`, `index_pages_survey`, `index_pages_worktree`; it builds the phase model lists itself (`:455-458`) |
| Moving or editing a fingerprinted file makes every repository's index pages stale | REFUTED as stated, here and in the `adced200` revision of this plan [E] | section 6 |

Full intra-package import graph [E] (AST over `databench:src/databench/*.py`; edges from core
files into future module files): `cli -> entities, entity_edit, index_pages_cli`;
`filesystem -> index_pages, entities (lazy)`; `server -> entities, entity_edit, index_pages,
index_pages_host`; `generate_cli -> index_pages_cli, index_pages_survey, index_pages_worktree`;
`generate_config -> index_pages_runner (lazy, for resolve_llm_kit)`. No edge runs between the
future explorer and html files; `index_pages_cli -> markup` becomes a module-to-core edge because
file summaries are core (ruling 1).

## 3. Core and modules

The rule: a module imports core and its own files only; anything two modules need is core; a
disabled module is never imported (eagerly, lazily or by literal `importlib`).

| Part | Files (databench `src/databench/`) | Why |
|---|---|---|
| core: host | `cli.py` (front door), `server.py` (app shell), `modules_registry.py` (added), `module_manifest.py` (added), `path_sources.py`, `generation.py`, `llm_gateway.py`, `service.py`, `background.py` (added in M3 or M6) | runs the process and loads enabled modules |
| core: workspace | `projects.py`, `dev_roots.py` (+ `dev_roots.yaml`), `roots.py`, `filesystem.py` (root resolution, exact spelling, reads), `paths.py`, `ignore_rules.py`, `path_walk.py`, `store.py`, `system_viewer.py` | every module resolves projects and paths the same way |
| core: access | `remote_access.py` | one access policy |
| core: comments | `comments.py`, `comment_locations.py` | ruling (2) |
| core: file summaries | `markup.py` (label store, work list, generation through `llm-kit jobs`), `generate_config.py`, `generate_cli.py` (the `generate` front door over registered operations) | ruling (1); the html module's run state already shares markup's cache root and lock |
| core: raw read | the raw route and its CSP contract | explorer's viewers and html's page viewer both read raw |
| explorer | `entities.py`, `entity_edit.py`, `explorer_module.py` (added); browse, tree, context and raw-write routes; entity CLI operations; viewer components | browsing and editing |
| html | every `index_pages*.py`, `index_pages_refs/`, `assets/`, `html_module.py` (added) | index pages |
| tasks | `tasks_module.py`, `tasks_host.py` (added) | the tasks view, through task-kit's JSON CLI |

Web client (`databench:web/src/`): the shell (`main.tsx` router, projects, session, ui, comments,
markup labels) is core; `web/src/modules/explorer/`, `web/src/modules/html/`,
`web/src/modules/tasks/` hold the views. Every module uses the shell's styling (ruling on look and
feel); none ships its own.

**Registry interface (core, fixed in M1):**

```python
MODULE_IDS = ("explorer", "html", "tasks")
@dataclass(frozen=True)
class ModuleSpec: id: str; title: str; entry: str     # "databench.html_module:register"
class Host:                                            # what register(host) may call
    def route(self, method, path, handler, *, path_source, write=False) -> None
    def cli(self, operation, build_parser, run) -> None     # databench [<dir>] <operation>
    def view(self, view_id, title, *, nav=True) -> None     # announced by GET /api/v1/modules
    def generation(self, op: "GenerationOperation") -> None  # used from M6
    def viewer_annotator(self, fn) -> None                  # adds keys to a file context
    def enabled(self, module_id) -> bool                    # soft links between modules
def load_enabled(path: Path | None = None) -> tuple[str, ...]
    # <config root>/databench/modules.json {"schema": 1, "enabled": [...]}; absent: all three
def register_enabled(host: Host, enabled: Iterable[str]) -> None   # imports enabled entries only
GET /api/v1/modules -> {"enabled": [...], "views": [{"module", "id", "title", "nav"}]}
databench modules list | enable ID | disable ID
```

A write route follows core's existing rule (write header; system-type actions local-only); a
module cannot widen it.

## 4. Stage order

| Order | Stage | Repos | Depends on | User-visible when shipped | An early stop leaves |
|---|---|---|---|---|---|
| 1 | M1 Core registry and import-lint | databench | none | `databench modules list`; nothing can be disabled yet | a registry and a lint that stops any further coupling |
| 2 | M2 Tasks module, read-only | plugins-kit (task-kit), databench | M1 | Tasks in databench's header: every project's tasks, project dropdown, sections, needs, items, working sessions (unverified tinted); detail with Orient, Decide and Trace lenses in databench's styling; `databench modules disable tasks` | tasks visible in databench; task-kit and its dashboard unchanged |
| 3 | M3 Explorer and html extraction | databench | M1 | `databench modules disable explorer` or `html` removes that module's views, routes and CLI operations | three switchable modules over one core |
| 4 | M4 Task answers | plugins-kit (task-kit), databench, claude-settings | M2 | the detail lenses show orientation and decisions answers with answerer, as-of commit and a stale marker; `task.py answers refresh` | answers refreshed by hand or by `task review` |
| 5 | M5 Shared file summaries | databench | M3 | file summaries tracked in `.databench/`, shared through git | summaries shared; nothing regenerates in the background |
| 6 | M6 Generation service | databench, plugins-kit (task-kit hook), claude-settings | M3, M4, M5 | an always-on service; settings panel with regeneration (on by default, type drop-down, all types by default), usage and spend; summaries, index pages and task answers stay fresh | (end of planned work) |
| 7 | M7 Running work on tasks (deferred stub) | -- | a user need (q3) | -- | -- |

M2 goes before M3 because it is the stage that puts tasks inside databench, and it can be clean
from its first commit while the lint's allowlist still names the older couplings. M4 precedes
M5 and M6 because answers are the detail view's main content and run by hand without a service.

## 5. Stages

Every unit owns the files it names; units that may run at once share none. Every check names the
revert that must turn it red (`docs/reference/vacuous-checks.md`). Every databench unit's
verification includes the generator-fingerprint comparison of section 6.

### M1 Core registry and import-lint

**M1-U1 registry and module settings.** Owns `databench:src/databench/modules_registry.py`
(added), `databench:src/databench/server.py` (builds a `Host` in `create_app`, serves
`GET /api/v1/modules`), `databench:src/databench/cli.py` (`modules` operation),
`databench:tests/test_modules_registry.py`, `databench:tests/path_route_registry.py`. Interface:
section 3. Checks: a disabled module's entry is never imported (import spy; revert: import all,
filter later -> red); an unknown id in `modules.json` fails loudly naming the file (revert:
ignore -> red); absent file enables all three.

**M1-U2 import-lint with a ratchet.** Owns `databench:src/databench/module_manifest.py` (added:
`CORE`, and per module a file list with globs, `index_pages*.py` among them),
`databench:tests/test_module_boundaries.py`, `databench:web/src/module-boundaries.test.ts`. The
Python test reads every file under `src/databench` (eager, local, `TYPE_CHECKING` and literal
`importlib` imports): each file belongs to exactly one part; core imports no module file; a module
imports only core and itself; `KNOWN_CROSS_EDGES` lists the edges of section 2 and fails when an
entry is no longer needed, so it can only shrink. The web test applies the same rule to
`web/src` import specifiers. Checks: a core file importing `index_pages` outside the allowlist
(red); a stale allowlist entry (red); an unassigned file (red).

**M1-U3 isolation harness.** Owns `databench:tests/module_isolation.py` (helper) and
`databench:tests/test_module_isolation.py`: per enabled set, a subprocess builds the app and the
CLI parser and asserts no other module's file is in `sys.modules`; a `module(id)` pytest marker
and a `--databench-modules` option run one module's tests with the rest disabled. Sets that
cannot pass before M3 (core alone, explorer alone, html alone) are listed as expected failures
that M3 removes.

### M2 Tasks module, read-only

**M2-U0 protocol freeze** (plugins-kit). Owns
`plugins/task-kit/skills/task/references/tasks-json-protocol.md` and
`tests/task-kit/fixtures/tasks_json/`.

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

**M2-U1 task-kit read verbs** (plugins-kit; after U0). Owns
`plugins/task-kit/skills/task/scripts/task_system/dashboard/read_cli.py` (added),
`dashboard/launcher.py` (a first argument `tasks` delegates to `read_cli.main`),
`tests/task-kit/test_tasks_read_cli.py`. Read only: no lock, runtime database or `server.json`.
Measured cost [E]: 0.65 s cold for 14 projects and 65 tasks (310 KB). Checks:
`test_snapshot_matches_golden` (revert: emit `task.py list` JSON -> red);
`test_parent_replaces_configured_directories`; `test_unknown_item_exit_3`;
`test_read_verbs_write_nothing` (tree and data-dir digests before and after).

**M2-U2 tasks module backend** (databench; after U0 and M1, beside U1 with a fake executable).
Owns `databench:src/databench/tasks_module.py` and `databench:src/databench/tasks_host.py`
(both listed under `tasks` in the manifest), `databench:src/databench/path_sources.py` (a
`task-identity` source: task-kit resolves the identity against its own inventory, databench walks
no client path), the route registry, `databench:tests/test_tasks_module.py` (marked
`module("tasks")`).

```python
TASK_KIT_PROTOCOL = 1
class TaskKitUnavailable(Exception): code: str  # task_kit_absent | task_kit_too_old | task_kit_failed
def resolve_task_kit() -> str
    # $CLAUDE_BOOTSTRAP_DATA_ROOT, else ~/.claude/plugins/data: plugins-kit/task-kit/bin/task-kit[.cmd]; then PATH
class TaskKitClient:
    def __init__(self, executable=None, *, runner=subprocess.run, min_interval_s=2.0, timeout_s=30.0)
    def snapshot(self, parents: Sequence[Path], *, refresh: bool = False) -> Mapping  # cached
    def item(self, checkout: str, task: str, item: str) -> Mapping
def register(host: Host) -> None
GET /api/v1/tasks?project=&q=&refresh=1
    200 {"available": true, "task_kit_version": ..., "payload": ...}
    200 {"available": false, "reason": {"code": ..., "message": ...}}
GET /api/v1/task-item?checkout=&task=&item=        path source: task-identity
databench --view tasks                             (no operation named `tasks`: $DEVROOT/tasks exists)
```

`parents` are databench's expanded development roots. Checks: protocol 2 refused with the update
command (revert -> served -> red); absent and too-old messages differ; the cache holds within the
interval; the module passes with explorer and html disabled once M3 lands (until then, recorded
as an expected failure in M1-U3).

**M2-U3 tasks views** (databench web; after U2's routes). Owns
`databench:web/src/modules/tasks/` (list, detail and tests) and the module-view slot in
`databench:web/src/main.tsx`. List: prototype A's layout with the project dropdown (approved
2026-10-17). Detail: concept C (ruled): Orient (legacy `summary` labelled legacy until M4,
items, working sessions), Decide (needs, blocked-user items), Trace (task-record updates).
Styling is databench's shell (ruling); the round-5 mockup refines it when it lands. No spend,
usage or regeneration control (ruling); no Queue, Dismiss or authoring control (q3). Checks
(vitest): fixture renders each section and lens; the project filter narrows; `available: false`
renders the reason; an unverified session is tinted.

**M2-U4 docs and release.** Owns `databench:README.md` ("Modules", "Tasks module"; the boundary
rule at README:25-27 admits first-party modules through the registry), `databench:CLAUDE.md`
(directory map), task-kit `references/dashboard.md`, `plugin.json`, `pyproject.toml` (next free
minor), `docs/planning/task-kit-dashboard/architecture.md` (decision 1: manager code lives in
databench). Verify: the unit tests above; databench full `uv run pytest`, `cd web && npm test &&
npm run typecheck`; claudx smoke of `task-kit tasks snapshot`; git-code-review;
`publish.py --only task-kit` (pre-authorized); databench push; on this Mac, `databench --view
tasks` shows the counts task-kit's dashboard shows.

### M3 Explorer and html extraction

**M3-U1 explorer module.** Owns `databench:src/databench/explorer_module.py` (added: browse,
tree, context and raw-write routes, entity routes and CLI operations, explorer views),
`databench:src/databench/server.py`, `databench:src/databench/cli.py`,
`databench:src/databench/filesystem.py` (the lazy `entities` import becomes a registered context
contributor), `databench:tests/test_explorer_module.py`.

**M3-U2 html module** (after U1; shares `server.py`, `cli.py`, `filesystem.py`). Owns
`databench:src/databench/html_module.py` (added; not named `index_pages*`): the index-page record
and style routes from `server.py`, a viewer annotator for marked pages (the marker logic leaves
`filesystem.py`), the `index-pages` CLI operation delegating to `index_pages_cli.is_invocation`
and `main`, and the generation operation M6 uses. Also owns `databench:src/databench/generate_cli.py`
(asks the registry for operations instead of importing `index_pages_*`),
`databench:src/databench/generate_config.py` and `databench:src/databench/llm_gateway.py` (added:
core's own `resolve_llm_kit`; the copy in `index_pages_runner.py` stays until the batch in
section 6), `databench:tests/test_html_module.py`. `filesystem.index_pages_runner_settings`
stays in core until the section 6 batch, because its caller `index_pages_cli.py` is fingerprinted.

**M3-U3 web split.** Owns `databench:web/src/modules/explorer/`, `databench:web/src/modules/html/`
and the shell's routing in `main.tsx`; empties the web allowlist.

**M3-U4 close-out.** Owns the manifest's allowlist (emptied), M1-U3's expected-failure list
(emptied), README "Modules", `CLAUDE.md`. Verify: full suite; each module's tests with the others
disabled; `web/tests/browser-check.mjs` with all modules enabled; generator fingerprint unchanged.

### M4 Task answers

`summary-on-commit-plan.md` units U1, U2, U4, U5, U7 and U10 keep their scope, interfaces and
checks with these changes:

- **U2** also owns `task_system/dashboard/snapshot.py`: `task_json` gains `answers`
  (`{qid: {state, answer, answerer, as_of, fingerprint}}`) and `coverage`; protocol stays 1.
- **U3 (re-cut)** has no switch in `task.local.yaml` and no spend statement or checkbox. Phases
  are `allow` sets: phase 1 `{"free"}`, phase 2 the caller's consent. By hand, the consent is
  plugins-kit's (`llm_kit.registry.consent.load_consent()`, per q1); a host passes its own with
  `--allow CLASSES`, which wins. `answerer.tier` is the llm-kit cost class.
- **U4** adds `task-kit tasks answers refresh --format json [--parent DIR]... [--allow CLASSES]
  [--batch N] [--stop-file PATH] [--deadline-s N]` (kind `answers-pass`) and `task-kit tasks
  answers status --format json`. A stop file ends the pass between phases.
- **U6** (service) becomes M6's `task-answers` operation; **U8** (dashboard UI) becomes M4-U8.
- **M4-U8 answers in the tasks module** (databench web; after U2 is published). Owns the lens
  components in `databench:web/src/modules/tasks/`: Orient shows the orientation answer, Decide
  the decisions with their stated defaults, Trace the task-record commits and activity; each
  answer shows answerer, as-of commit and a stale marker. Read-only.
- **U9** drops the Windows service gate (M6) and every `task-kit service` text.

### M5 Shared file summaries

The databench task `shared-generated-content`, items `design-amendment` and `tracked-label-store`:
summaries move from `~/.cache/databench/markup/<key>/labels.json` into `.databench/`. Owns
`databench:src/databench/markup.py` (core) and `databench:docs/markup-design.md`. It keeps
`_cache_root` and `_file_lock` and their meaning, because the fingerprinted `index_pages_cli.py`
imports them. M6's file-summary operation runs by default on more than one machine only after
this lands, otherwise every machine pays for the same summaries.

### M6 Generation service (core)

**M6-U1 settings and consent.** Owns `databench:src/databench/generation.py` (added):
`<config root>/databench/generation.json` `{"schema": 1, "regeneration": {"enabled": bool,
"types": [operation ids]}, "consent": {"usage": bool, "spend": bool}, "updated_at": ...}`. Absent
file: regeneration on with every registered type (q6, "It all is by default"), consent per Q1.
`GET` and `PUT /api/v1/generation-settings` (write header; local clients only). A core settings
panel holds the regeneration toggle, the type drop-down (operations of enabled modules) and the
usage and spend checkboxes with the providers they send text to. `generate.yaml` v2 drops
`harnesses`; a v1 file's harness keys are ignored with a note. Checks: absent file yields the
shipped defaults; a remote `PUT` is refused.

**M6-U2 consent applied through llm-kit.** Owns `databench:src/databench/llm_gateway.py`
(`filter_models(ids, allowed) -> Filtered(usable, switched_off)`, one `llm-kit models describe
<ids> --caller process --allow <classes> --json` per operation pass) and its call sites in
`generate_cli.py` and `markup.py`; `databench:.claude/bootstrap.json` raises llm-kit's
`min_version` to 0.4.0 (the first with `--allow`), with distinct absent and too-old messages.
The html operation receives already-filtered phase lists from `generate_cli`, so no fingerprinted
file changes. An operation whose list filters to empty is skipped with a status line, not exit 2.
llm-kit is not changed.

**M6-U3 service lifecycle.** Owns `databench:src/databench/service.py` (added), `cli.py`
(`databench service ensure|stop|status|run [--json]`; bare `databench` ensures the service and
opens the browser; `--dev` stays foreground), `databench:pyproject.toml` (psutil),
`databench:tests/test_service.py`. Identity, stop protocol and checks as in the previous
revision's S3-U1 (adapted from task-kit's `procspawn` and `procstamp` design).

**M6-U4 background loop.** Owns `databench:src/databench/background.py` (added) and the startup
wiring in `server.py` (markup launch stderr to the service log, replacing DEVNULL at
`server.py:676-681`). It runs each selected operation of each enabled module: file summaries
(core), index pages (html; per repository after its origin HEAD moves, at most once per 24 h),
task answers (tasks; `task-kit tasks answers refresh --format json --allow <databench consent>
--stop-file <service dir>/stop`). Checks: regeneration off runs nothing; a deselected type does
not run; a disabled module's operation is never registered.

**M6-U5 triggers and wording.** Owns `plugins/task-kit/hooks/hooks.json` and
`plugins/task-kit/hooks/sessionstart/databench-ensure.sh` (async `startup`, `resume`, `clear`;
runs `databench service ensure` when on PATH; absent or failing databench logs one line and exits
0), `tests/task-kit/test_databench_ensure_hook.py`; claude-settings `USER-PREFERENCES.md`
insight `generated_index_pages` (the regeneration switch and its types are the user's discretion;
agents do not regenerate by hand).

**M6-U6 release.** Windows smoke over ssh (detached service survives the starting session; stop
works) or ship with the gap stated; git-code-review; databench push; `publish.py --only task-kit`.

### M7 Running work on tasks (deferred stub)

Not planned (q3). Queue, run, cancel, authoring, session dismiss and any retirement of
task-kit's own dashboard wait until the user needs them. The design kept for that point is the
`adced200` revision of this file, section 5, stage 6 (a headless task-kit engine supervised by
databench's service, proxied routes, UI parity).

## 6. The index-pages generator fingerprint

**What it is [E].** `generator_fingerprint(settings)` (`databench:src/databench/index_pages_worktree.py:845-920`)
hashes the package-relative NAME and bytes of every `index_pages*.py`, `paths.py`,
`ignore_rules.py`, `path_walk.py`, every file under `index_pages_refs/` and `assets/`, five llm-kit
modules (`pipeline.execution.{scheduler,deadlines,failure_cache}`, `storage.atomic_json`,
`pipeline.runtime.gate`), and the run settings. It is a skip key, not a page key: when it
changes, the next run of each configured repository is a real one (fetch, worktree, scan; about 24
Git processes instead of 4) and is model-free when nothing is pending (databench README, "skip
key" and "The generator fingerprint"; `index_pages_worktree.py:904-911`). Pages are replanned only
when an eligibility key, a `source_sha` or the generation spec hash moves; the spec hash is keyed
by the five generation inputs' logical names and bytes (`index_pages.py:655-670`), not their
paths. 13 repositories are configured on this Mac (`$DEVROOT/.databench/config.yaml`,
`index_pages.repositories`). The `adced200` revision of this plan said such an edit "makes every
repository's index pages stale"; that overstated it.

**How the split handles it.**
1. Nothing fingerprinted moves or changes. html membership is declared in the manifest, so
   `index_pages*.py`, `index_pages_refs/` and `assets/` stay at their paths; `paths.py`,
   `ignore_rules.py` and `path_walk.py` stay as core files at theirs.
2. Every coupling is cut on the non-fingerprinted side: `filesystem.py`, `server.py`, `cli.py`,
   `generate_cli.py`, `generate_config.py`. `index_pages_cli -> markup` needs no edit because
   markup is core. Two leftovers stay until a batch: `filesystem.index_pages_runner_settings`
   (called from `index_pages_cli.py`) and `index_pages_runner.resolve_llm_kit` (a copy beside
   core's).
3. No added file may be named `index_pages*.py` or sit under `index_pages_refs/` or `assets/`;
   either would join the hash. The html registration file is `html_module.py`.
4. Verification for every databench unit: a scratch script prints `generator_fingerprint` for a
   fixed settings mapping at the unit's base commit and at its result, using the same llm-kit;
   they must be equal. A plugins-kit llm-kit release that edits one of the five hashed modules
   moves it too, so M4 and M6 touch none of them.
5. If a later change must touch fingerprinted files (the two leftovers, or moving html files
   into a directory), it goes in ONE databench commit, so the cost is paid once: one model-free
   local run per configured repository. Editing the bytes of the five generation inputs is
   outside that batch, because it moves the spec hash and restamps records in every repository.

## 7. Changes to `summary-on-commit-plan.md`

- **D2.** Three llm-kit classes (free, usage, spend); `luna` and `haiku` are usage. Phases are
  `allow` sets. Consent is plugins-kit's when the pass runs by hand and databench's when
  databench runs it (q1). The spend checkbox and its statement leave task-kit (ruling: "there
  shouldn't be a spend control there").
- **D4.** The host is databench's generation service (M6). Deleted: `task-kit service ...`,
  `<data dir>/answers/service.{json,lock,stop,log}`, `hooks/sessionstart/service-ensure.sh`,
  `answers.service`, `answers.spend`, `answers.poll_s`. The pass algorithm stays task-kit code,
  run as `task-kit tasks answers refresh --format json --allow ... --stop-file`. The row "inside
  the dashboard server: Rejected" reverses for databench's detached service.
- **D7.** "service off" reads "regeneration off"; "spend off" reads "class not consented".
- **Units.** U3 re-cut; U6 becomes M6-U4's `task-answers` operation; U8 becomes M4-U8; U9 loses
  the Windows service gate.

## 8. Open questions (recommended default in brackets)

- **Q1. databench's shipped consent.** q2 allows consent on by default; which classes?
  [usage on, spend off: free and subscription harness models run, metered API models do not until
  the user ticks spend.]
- **Q2. task-kit's own dashboard.** "Task kit remains a fully functional plugin" and "All code
  related to the manager lives in databench", with q3 deferring running work. [Keep task-kit's
  dashboard working and frozen: no further features there; every further manager feature goes
  into databench's tasks module; moving runs out of task-kit waits for M7.]

## 9. Decisions this plan makes, and what it dropped

Decided on the user's behalf:
- html membership by manifest rather than a directory move (section 6).
- File summaries, comments, raw read, store, system open and the workspace files are core;
  browse, tree, raw write and entities are explorer.
- databench applies its consent by pre-filtering through `llm-kit models describe --allow`, so
  llm-kit does not change; databench's llm-kit floor becomes 0.4.0.
- Regeneration and consent share one machine-written `generation.json`; `generate.yaml` stays
  hand-edited model lists.
- All three modules are enabled by default; the lint starts with a shrink-only allowlist emptied
  in M3.
- The tasks module reaches task-kit through its JSON CLI (process boundary, no `task_system`
  import), as in the previous revision.
- Index pages in the background run per repository after its origin HEAD moves, at most once per
  24 h.

Dropped from `adced200`: stage SC (llm-kit default flip and migration of task-kit, workflow-kit
and other plugins-kit callers; q1 scopes consent to databench); the remote-access question (q5);
stage S5's run host, engine supervision, UI parity and retirement (M7, deferred).
