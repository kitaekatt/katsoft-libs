# task-kit task metadata: questions answered at commit time -- design plan

Status: design only (unit `summary-on-commit-plan`, 2026-10-21). No code changed.
Code facts are as of dev `d6bb301a` (task-kit 0.13.0, llm-kit 0.3.2, bootstrap 0.158.3).
Paths are relative to `plugins/task-kit/skills/task/scripts/` unless they start with
`plugins/`, `tests/`, `docs/` or `~`.

## Question

How should task-kit generate each task's orientation summary at git commit time, so the
dashboard detail view can orient first: what the task is, where it stands, what is next? The
user widened the question: the summary is one instance of durable, LLM-computed task metadata,
organised as questions and answers, with versioned schemas and automatic upgrades of every task.

## Binding rulings (user, 2026-10-21, verbatim)

> Generated summary. Maybe this can be tied to the git commit process somehow with a script
> that calls a local qwen relay falling back to luna med.

> one thing that I think is a helpful concept to consider is we could as a feature offer taking
> a task and upgrading it to a newer schema version. These upgrades could include llm analysis
> to fill out metadata that would make it easier to display rich task information in a task.
> We have the technology to do this the qwen38-5090 endpoint. I am imagining a world where when
> we update the task schema we just automatically update all the tasks to the new schema.

> we can make use of the llm-kit capabilities to do this the default can be luna med/haiku med,
> but we can prefer our two qwen38 endpoints

> so think not just in terms of what we know about tasks today but include what we can known
> about them with cheap llm calls that durably encode those insights as metadata.

> sure i imagine these are things we'll iterate on. But the core idea is questions and answers
> computed via meta-data.

Distributed-truth rulings this design leans on (`docs/reference/distributed-truth.md`):

> if the data isn't in the task file, wherever it goes still needs to be commited, because we
> run this dashboard on multiple machines

> I don't like any dependency on the dashboard running

> I don't think there should be anything machine specific except for private information
> specific to this machine

## Recommendation in brief

1. **Primitive.** A QUESTION is declared data: id, version, prompt, the input readers it uses,
   an answer schema, the statuses it applies to, an optional model override. Its ANSWER is
   computed through llm-kit and stored in the task folder with provenance: inputs
   fingerprint, as-of commit, model entry, effort, time, re-selections. The catalog is data
   (shipped files plus a fleet layer); a question that uses existing readers is added or
   retired without code.
2. **Freshness is computed.** An answer is current when its stored fingerprint equals the
   fingerprint of the question definition plus the inputs it reads. Editing a question or
   changing an input makes its answers stale; writing an answer changes no fingerprint.
3. **Storage.** `<task>/answers/<question id>/<host id>.yaml`, committed. Each machine writes
   only its own files (principle 2), so concurrent answering never produces conflicting
   commits; readers take the newest current answer from any host.
4. **Schema versions.** The format version stays in `task._schema_version` (deterministic,
   idempotent, byte-identical migrators). The question set is versioned by the catalog. A
   task's coverage is computed from its answers, never stored: a failed upgrade leaves the task
   visibly on its old coverage, with nothing to clean up.
5. **Freshness trigger.** A synchronous git post-commit hook in the repository that versions
   the task folders (the tasks repo; never plugins-kit) refreshes the stale or missing answers
   of the tasks a commit touched, and commits only this machine's answer files before the
   caller's push.
6. **Bulk upgrade.** One verb, `task.py answers upgrade`, run by hand or automatically from
   task-kit's own SessionStart hook when `answers.auto_upgrade` is on (fleet on, shipped off),
   within a per-start budget, with git as the deduplicator. Not bootstrap; not the dashboard.
7. **Model chain through llm-kit.** Fleet declaration `[qwen38-5090, qwen38-m5, luna, haiku]` at
   effort medium; shipped default `[luna, haiku]` at medium. llm-kit selects and falls through
   on halts; on a task error or rejected answer task-kit re-selects with `exclude` and records
   every re-selection (the code-review lane precedent).
8. **First question set:** `orientation` (headline, state, next) and `decisions` (what waits on
   the user, with defaults only when the files state one, each grounded by a verbatim quote).
   Further questions are ranked in D6 and added as data.

## Premise outcomes

| Premise | Outcome | Evidence |
|---|---|---|
| task-kit generates a one-line `task.summary` with Codex Luna at medium effort, triggered by `review`, persisted with fingerprint and provenance | CONFIRMED, with a correction: provenance is a date only; no model is recorded | `summary_ops.py:22-29`, `:121-155` (calls `CodexCliBackend` directly, not through the registry), `:267-275` (`state_ops.update(summary=, summary_fingerprint=, summary_updated=<ISO date>)`); `task.py:353-355` |
| Writing a summary does not change the fingerprint | CONFIRMED | `listing.py:26` (summary keys excluded), `listing.py:92-109`; `discovery.py:189-214` drops `summary:` log entries |
| Bookkeeping log lines are harmless to freshness | REFUTED | the detail-view mockup's sample task has 22 of 25 dated log lines reading "update: refresh (no field edits)" (`detail-view/index.html`); every such entry enters the fingerprint (`listing.py:92-109`), so each one invalidates the summary |
| Task folders are committed in the private tasks repo, on several machines | CONFIRMED | `dev/tasks -> /Users/christina/Dev/tasks/plugins-kit` (symlink); plugins-kit ignores `dev/tasks/` (`docs/reference/task-folders.md`). The tasks repo has no installed hooks and no `core.hooksPath` |
| Who commits in the tasks repo, and how often | MEASURED | agents (`git add -A && git commit && git push`, tasks-repo `CLAUDE.md` "Always push"), repo-sync `auto_commit: true` at SessionStart (same file), `session_publish.py:330` (`git commit -- .sessions/<host>.yaml`). 517 non-merge commits in the 7 days to 2026-10-21: 500 touched one task folder, 12 touched 2 to 6, 1 touched 16, 5 touched none. 75 task folders: 40 active, 34 closed, 1 deferred |
| llm-kit expresses an ordered declaration with fallback | CONFIRMED, with two limits | Python: `llm_kit.dispatch.declaration.run(names, RunRequest, exclude=, max_attempts=, reachability_cache=)` (`plugins/llm-kit/lib/llm_kit/dispatch/declaration.py:870-907`). CLI: `llm-kit completion complete --models a,b,c --effort medium` (`cli/completion.py:303-311`, `cli/_common.py:69-79`). Render: `llm-kit models describe <ids> --caller process`. Measured on this Mac: `describe qwen38-5090 qwen38-m5 luna haiku --caller process` lists all four usable in declared order (luna is the only paced entry, so pace ordering moves nothing); with `--requirements '{"params":["effort"]}'` all four stay usable, so one effort reaches every entry. Limits: `run` moves on only on a classified halt or launch failure ("A task error, schema-invalid output, or an unclassified timeout stays a failed attempt", printed by describe; `declaration.py:1036-1043`); per-entry effort exists only in `llm_kit.jobs` (`model_efforts`, `jobs/model.py:26-30`). task-kit's dashboard already calls `run` (`dashboard/jobs_edge.py:331-372`) |
| The two qwen38 endpoints | CONFIRMED | `~/.claude/config/model-endpoints.yaml`: `qwen38-5090` (`http://5090w.localdomain:8080/v1`, context 262144) and `qwen38-m5` (`http://m5pro.localdomain:8000/v1`, context 32768), both `billing: {mode: unmetered}`. Naming them directly bypasses the front-door entry `qwen38`, whose third tier is DeepSeek Flash on OpenRouter (local-llm-domain `SKILL.md`, "Front door"). qwen38-m5's 32768-token context caps a question's input |
| A task format version exists | CONFIRMED | `task._schema_version` is required (`schemas.py:23`); `validate.py:471-475` reports an unknown version as a schema violation; the hand-off type knows `("1",)` (`types.py:45`); validate findings gate `work` (`schemas.py`, durable_outputs comment) |
| What the mockups need | READ | `docs/planning/task-kit-dashboard/detail-view/index.html`, "Data the concepts use": a generated summary "with as-of commit, time, model and freshness" in all four concepts. Concept C (`c-situation.html`) shows "as of d6bb301a + task record 7cd92a28", the model, and "Waiting on you: 2 decisions", each with its question, defaults and an "Accept the defaults" action |
| A commit made inside post-commit is safe | MEASURED (git 2.54.0, macOS, scratch repo, removed afterwards) | post-commit receives `GIT_INDEX_FILE=.git/index`; a nested `git commit --only -- <path>` succeeds after a plain commit, a `commit -a`, and a `commit -- <path>` made while another path was staged, and that path stays staged; the hook's nonzero exit is ignored; the nested commit fires post-commit again; post-commit fires during `git rebase` (detached HEAD, `rebase-merge` present) and does not fire for an automatic merge commit |
| A late, detached answer commit is harmless | REFUTED | `session_publish.py` docstring, steps 2 and 4: publication refuses while any local-only commit touches a path other than this machine's session file |
| bootstrap can install the hook | REFUTED for the tasks repo | `bootstrap_lib/engine.py:6305-6316`: `git_config` converges keys only in the session's project directory |
| `~/.claude/task.local.yaml` is shared across machines | REFUTED | claude-settings `.gitignore:96`; the tracked fleet layer convention is `~/.claude/config/<plugin>.yaml` (llm-kit `registry/models.py`, `fleet_config_path`) |
| The dashboard shows summary staleness | REFUTED | it reads only `core.summary` (`dashboard/inventory.py:250`, `dashboard/snapshot.py:144,422`, `dashboard/static/app.js:850`) |
| Windows runs the hook stub and the POSIX `task-kit` shim | UNTESTED | the shim handles `MINGW*`/`MSYS*` (`plugins/task-kit/bin/task-kit`); Git for Windows runs hooks with its bundled sh. U9 measures it |
| Latency of a real question on qwen38-5090, qwen38-m5, luna medium, haiku medium | UNTESTED | sets `hook_deadline_s`; U3 measures it |
| An llm-kit `OutputContract` keeps the qwen transports selectable | UNTESTED | `run` merges a contract's selection requirement into `describe`, which may hide an entry; U3 checks, else task-kit validates the answer itself |

## D1. The primitive: questions and answers

**Question file.** Shipped: `plugins/task-kit/skills/task/questions/<id>.yaml`, listed in
`questions/catalog.yaml` (`version: 1`, active ids). Fleet layer:
`~/.claude/config/task-kit/questions/<id>.yaml`; a file with a shipped id replaces it, and
`retired: true` retires it. Unknown keys, an unknown reader or a bad schema are errors naming
the file.

```yaml
_schema_version: "1"
id: orientation
version: 1
title: Orientation
applies_to: [active, blocked, deferred, closed]
inputs: [task, claude_md, plan_md, items, activity]
max_input_chars: 60000
prompt: |
  From the task files below, answer for someone opening this task: what it is,
  where it stands, and the single next action. Return one JSON object.
answer:                 # JSON schema subset
  type: object
  required: [headline, state, next]
  additionalProperties: false
  properties:
    headline: {type: string, maxLength: 100}
    state: {type: string, maxLength: 200}
    next: {type: string, maxLength: 160}
grounding: []           # answer fields that must quote the inputs verbatim
models: null            # null: the configured chain; else a declaration overriding it
effort: null            # null: the configured effort
```

**Input readers** are code; each has a fixed canonical text and size limit, and a question
names them.

| Reader | Reads | Excludes |
|---|---|---|
| `task` | the task.yaml task block | `_schema_version`; legacy `summary`, `summary_fingerprint`, `summary_updated` |
| `claude_md` | CLAUDE.md | -- |
| `plan_md` | plan.md | -- |
| `items` | parsed `task_items` (id, title, state, priority, note) | -- |
| `activity` | dated log.md entries, newest first, last 40 | summary-maintenance entries and `refresh (no field edits)` bookkeeping |
| `task_commits` (v2) | git log of the task folder | -- |
| `code_commits` (v2) | the project's git log after the task's previous as-of code commit; needs a task-root to project-checkout map | -- |

**Answer record**, `<task>/answers/<question id>/<host id>.yaml` (host id from
`machine.host_id`, as working sessions use):

```yaml
_schema_version: "1"
question: orientation
question_version: 1
answer:
  headline: Generate each task's orientation at commit time for the dashboard detail view
  state: Design plan written; no implementation unit has started
  next: User answers the commit-wait question, then U1, U3 and U6 start in parallel
inputs_fingerprint: <64 hex>
as_of:
  task_commit: <sha>      # tasks-repo commit whose content was read
  code_commit: null       # set only by questions that read code inputs
generated:
  at: "2026-10-21T19:05:12-07:00"
  entry: qwen38-5090
  effort: medium
  reselections: []        # e.g. ["qwen38-5090: invalid answer: next exceeds 160 chars"]
```

**Fingerprint:** sha256 over canonical JSON of the question digest (id, version, prompt,
inputs, answer schema, grounding, limits) and each input's canonical text. The model chain is
not part of it, so changing models does not invalidate answers.

**Best answer and states:**

| State | Rule | Shown |
|---|---|---|
| `current` | a valid record's fingerprint equals the current one; the newest such (`generated.at`, then host id) wins | the answer with as-of commit, time, model |
| `stale` | valid records exist, none current; the newest is shown | the answer marked stale, with its as-of commit and time |
| `missing` | no record | "not answered yet" |
| `invalid` | a file that does not parse or fails the answer schema | a note naming the file, never a finding that gates `work`; valid records from other hosts still count |
| `not-applicable` | the task's status is outside `applies_to` | nothing |

**Answer validation** (task-kit side, after the call): one JSON object matching the answer
schema; values single-line and non-empty; typographic punctuation converted to ASCII (dashes to
`--`, curly quotes to `'` and `"`, ellipsis to `...`, arrows to `->`), any other non-ASCII
rejected (the tasks repo is ASCII-only); each `grounding` field must contain a
whitespace-normalised verbatim substring of the inputs. A rejected answer is a task error for
the chain (D5); nothing beyond the listed conversions is repaired.

## D2. Storage

- **Per question, per host, in the task folder, committed.** Principle 2 ("one writer per
  file"): two machines answering one question at once (two upgraders, or commits of one task on
  two machines) write different files, so their commits merge cleanly and the reader decides.
  Cost: one small file per machine per question per task.
- **Why not task.yaml keys** (where `summary` lives at d6bb301a): `state_ops.update`
  re-serialises task.yaml and appends a log line (`state_ops.py:206-208`), so every refresh
  would rewrite two agent-edited files, and one shared file is the cross-machine conflict
  principle 2 rules out.
- **No loop.** No reader reads `answers/`; writing an answer appends no log line. The commit
  carrying answers sets `TASK_KIT_ANSWERS_HOOK=skip`, so the hook it fires exits at once. Either
  guard alone stops recursion; both are tested.
- **Committed content only.** The hook answers only for a task folder whose files (except
  `answers/`) are unchanged from HEAD; otherwise the task is deferred, naming the files.
- **Housekeeping.** A machine rewrites its own file in place. Files of retired questions or of a
  vanished host are inert (never current); only their owner removes them (`answers prune`).

## D3. Schema versions, upgrade, and who runs it

**Two layers.**
- *Format version* -- `task._schema_version` in task.yaml. A structural change ships a migrator
  `N-1 -> N` that is deterministic (byte-identical output on every machine, so two machines
  applying it merge cleanly), idempotent (a folder at N is untouched), validates its result,
  and leaves the folder at N-1 on any failure. validate reports an unknown version and findings
  gate `work`, so a release teaches validate a version one release before any migrator writes
  it. The question mechanism needs no format change: v1 ships the migrator runner, exercised by
  a test fixture, with no production migrator.
- *Question set* -- the catalog version. Answering is the per-version migration for questions:
  idempotent (a current answer is never recomputed) and safe to repeat on any machine. A task's
  coverage ("catalog 1: 2 of 2 current") is computed from its answer files, never stored.

**Upgrade verb.** `task.py answers upgrade [--all | <ref>...] [--budget N] [--no-commit]
[--publish]`: for each eligible task folder in the repository, apply pending format migrators,
then answer every applicable question whose best answer is missing or stale. It fetches before
each batch and skips tasks that gained a current answer upstream, commits each batch of this
machine's own answer files with `--only`, and with `--publish` pushes through the session
publisher, whose own-files rule U5 widens from "this machine's session file" to "this machine's
session file and answer files".

**Who runs it.**

| Runner | Verdict | Why |
|---|---|---|
| bootstrap pass | No | bootstrap provisions dependencies; the tasks repo is not the session's project; a model workload would lengthen every session start |
| dashboard | No | principle 9 and the ruling "I don't like any dependency on the dashboard running"; every dashboard would become a writer |
| `task.py answers upgrade` by hand | Yes | the one implementation |
| task-kit SessionStart hook with `answers.auto_upgrade: true` | Yes: the automatic path | detached (the hook is async), one upgrader per machine under an OS lock (transient coordination, principle 1), at most `answers.upgrade_budget` tasks per session start; failures written to `<data dir>/answers/upgrade-failures.log` only on failure (precedent: `sessions/session-start-hook.log`) |
| post-commit hook | Yes, for the touched tasks | keeps worked tasks current (D4) |

**Races.** Concurrent upgraders on two machines write disjoint files and merge cleanly; the
only cost is duplicate calls for tasks neither had fetched. Nothing waits on another machine's
claim (principle 4).

**Failure.** A question that cannot be answered leaves that task's answer missing or stale,
visibly on every machine (computed from git); the reason is reported where the run happened
(stderr, or the failure log of the background run). A failed format migrator leaves
`_schema_version` unchanged. No answer is deleted because a refresh failed.

**Un-upgraded folders keep every verb's contract.** No verb requires an answer; `work` and
`validate` never gate on answers. `list --format json` serves `summary` from the orientation
answer, else from legacy `task.summary` (read compatibility for a format-1 field, labelled
`summary_source: legacy`). `update --summary` keeps writing `task.summary` and prints a note
when an orientation answer supersedes it for display.

## D4. Freshness: the commit-time refresh

| Option | Verdict | Deciding evidence |
|---|---|---|
| pre-commit hook | Rejected | network before the record is committed (violates principle 7); under fail_loudly a model failure would abort commits, including repo-sync's and session publication's |
| post-commit, synchronous | **Chosen** | commit first; failures go to the committer's stderr and the commit stands; the answer commit precedes the caller's `git push`, so one push carries both |
| post-commit, detached | Rejected as default | races the caller's next git command for `index.lock`, and lands after the push (offered as question 1) |
| a task.py verb called by `update` / hand-off | Kept as repair, not the trigger | most task commits are raw `git commit` by agents and repo-sync |
| dashboard lazy generation | Rejected | principle 9; every dashboard a writer |
| pre-push | Rejected | a commit made during pre-push is not in the push being performed |

**Which repository:** only the one that versions task folders (the tasks repo here). plugins-kit
never commits a task folder (`dev/tasks/` ignored, load-bearing). A consumer whose `dev/tasks`
is tracked in its project repo installs the hook there.

**Which commits:** task folders the commit touched (diff against the first parent; all files
for a root commit), eligible statuses only. Automatic merges do not fire post-commit (measured);
the machine that wrote the incoming change refreshed its own answers.

**Algorithm (`task-kit answers refresh --post-commit`):**
1. Exit 0 at once when `TASK_KIT_ANSWERS_HOOK=skip`.
2. Skip with one stderr note on a detached HEAD or a sequencer operation (`rebase-merge`,
   `rebase-apply`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `MERGE_HEAD` via `git rev-parse
   --git-path`). Measured: post-commit fires during a rebase.
3. Map touched paths to task folders (nearest ancestor holding `task.yaml`); `.sessions/` and
   root files map to none. None: exit 0 without importing llm-kit.
4. Keep clean folders; defer dirty ones with reasons; beyond `max_tasks_per_commit`, defer the
   rest ("run task-kit answers upgrade").
5. For each kept task, answer every applicable question whose best answer is missing or stale,
   in parallel (4 workers), within `hook_deadline_s`, sharing one reachability cache.
6. Re-compute each fingerprint before writing; a task changed meanwhile is reported superseded.
7. Write this host's answer files atomically; one commit
   `git commit -q -m "task-kit answers: <stubs>" --only -- <answer paths>` with
   `TASK_KIT_ANSWERS_HOOK=skip`. Never push.
8. Print one line per task and question; exit 1 when anything failed or was deferred.

**Install.** task-kit renders the hook, so it is a plugin output (root `CLAUDE.md`,
"Anti-pattern: hand-creating an artifact a plugin is supposed to produce"). `task-kit answers
hook install --repo <dir>` writes the POSIX sh stub to `git rev-parse --git-path hooks` (honours
`core.hooksPath`), LF, mode 755, with a version marker; it refuses a post-commit hook it did not
write; `check` is nonzero when missing or outdated; `install` is idempotent. The stub calls
`"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}/plugins-kit/task-kit/bin/task-kit"
answers refresh --post-commit`, with no PATH dependency. A claude-settings env_check converges it
per machine (U10). Installing is the per-repository opt-in. Not a tracked `.githooks/` in the
tasks repo: the hook is a task-kit output and that repo holds only task data.

**Deadline.** `hook_deadline_s: 90` keeps an agent's `git commit && git push` within the Claude
Code Bash tool's 120 s default timeout; U3's latency measurement may lower it.

## D5. Model chain through llm-kit

**Config** (`answers:` section; layers, highest first: `~/.claude/task.local.yaml` per key ->
`~/.claude/config/task-kit.yaml` (fleet, tracked in claude-settings) -> shipped default; unknown
keys or wrong types are errors naming the file):

```yaml
answers:
  models: [qwen38-5090, qwen38-m5, luna, haiku]   # model declaration, preference order
  effort: medium
  hook_deadline_s: 90
  max_tasks_per_commit: 4
  auto_upgrade: true
  upgrade_budget: 20
```

Shipped default: `models: [luna, haiku]`, `effort: medium`, `hook_deadline_s: 90`,
`max_tasks_per_commit: 4`, `auto_upgrade: false`, `upgrade_budget: 20`. The fleet value above is
U10. `qwen38-*` ids exist only in the fleet's private registry, so they never appear in the
shipped default (OP-1). `[luna, haiku]` lists a Codex and a Claude entry (root `CLAUDE.md`
convention, `tests/repo-scripts/test_shipped_model_families.py`; U3 adds this default to it).
The declaration is validated with `bootstrap_lib.model_declaration.parse` (bootstrap 0.129.0,
below task-kit's 0.138.0 floor). One effort for every entry is enough: all four entries admit
`params: ["effort"]` (measured), so per-entry effort is not needed.

**Dispatch.** `declaration.run(models, RunRequest(system=<question prompt>, prompt=<inputs>,
options=BackendOptions(effort=..., timeout_s=<remaining deadline, or 180 outside the hook>,
client_id="task-kit:answers")), requirements={"guarantees": ["filesystem-write", "shell-exec",
"subagent-spawn"]}, exclude=<entries already failed>, max_attempts=len(models),
reachability_cache=<per batch>)`. llm-kit chooses the entry (declared order; pace among paced
entries; reachability; quota) and moves on by itself after a halt or launch failure. When `run`
returns a failed attempt (task error or timeout) or task-kit rejects the answer (D1), task-kit
adds that entry to `exclude` and calls `run` again, at most once per entry, and records each
re-selection in `reselections` and on stderr. Precedent: code-review lanes re-select through
describe with `--exclude` and disclose every route (`plugins/CLAUDE.md` register, "An
unexplained lane failure re-selects through describe, and every re-selection is disclosed").
Without it, a qwen task error (such as the empty-content case local-llm-domain lists as
`reasoning_effort_eats_the_budget`) would end the run with no fall-through. A question's own
`models`/`effort` override the chain; a local-only question declares
`models: [qwen38-5090, qwen38-m5]`.

**Privacy.** Inputs are private task files. The qwen entries are owner-hosted LAN endpoints;
luna and haiku send inputs to OpenAI and Anthropic, covered by the standing authorization
`private_ai_review_and_consultation` (configured providers).

**Optional edge.** Answering keeps task-kit's REFUSE posture for llm-kit: absent or too old, the
verb exits with the install or update command (distinct messages), probing the newest call shape
used (`run` with `exclude`, `requirements` and `reachability_cache`); U3 states the minimum
version. Un-upgraded folders and every other verb work without llm-kit.

## D6. Question catalog (ranked by value per cost)

| # | Question | Inputs | Call cost | Stale when | qwen-safe | Mockup surface | Ships |
|---|---|---|---|---|---|---|---|
| 1 | orientation: headline, state, next | task, claude_md, plan_md, items, activity | one call, about 3k-15k tokens in, about 150 out | any of its inputs change (about every task commit) | yes (fits qwen38-m5's 32k) | the summary block in A, B, C, D; C "Where it stands" | v1 |
| 2 | decisions waiting on the user: item, question, default (only when the files state one), evidence quote; at most 5 | items, plan_md, claude_md, activity; applies to active and blocked | one call, same input size, short output | plan.md, CLAUDE.md or item changes | yes | C "Waiting on you" with defaults and "Accept the defaults"; header markers in A and B | v1 |
| 3 | code commits that belong to the task: sha, subject, one-line why | task, items, code_commits after the previous as-of | one call; up to 50 subjects and paths, about 2k-5k tokens | the task is committed and its project gained commits after the stored as-of code commit | yes | "Latest code commits for the task" in A, B, C, D; D timeline; the as-of code commit in C's provenance line | v2 (code_commits reader, task-root to checkout map) |
| 4 | current item of a working session | items, task_commits | small | a task commit | yes | C working card | v2 (deterministic heuristic first: item id in the newest commit) |
| 5 | blocker kind (user, external, technical, none) and one-line risk | task, items, activity | small; may fold into orientation as fields | as orientation | yes | header markers in A; list row tint | v2 |
| 6 | item-to-event links | items, task_commits, activity | medium | task commits | yes | D timeline filter | v3 (deterministic id match first) |
| 7 | related tasks | this task's orientation answer, every task's headline | one call with about 75 headlines | any headline changes, fanning out to every task; needs a coarser trigger | yes | B fact rail | v3 |
| 8 | remaining size estimate | items, plan_md | small | plan.md changes | yes | B fact rail; list sort | v3 |

Not questions, because the files or git answer them without a model: the hand-off note (a
convention `/task hand-off` writes; C), abandonment (activity age), item state changes over
time (diff of `task_items`; D), latest task-record commits (git log). Items 1 and 2 lead because
they serve every concept's first screen and read only the task folder; item 3 is the gap the
mockups name ("connects the task to the work") but needs a cross-repository reader.

## D7. Failure reporting

| Situation | Commit | Answers | Reported |
|---|---|---|---|
| qwen entries fail, luna or haiku succeeds | stands | written; `reselections` set | stderr names each re-selection |
| offline (every entry fails) | stands | unchanged: stale or missing, visible | stderr lists each entry's reason; exit 1 |
| llm-kit absent or too old | stands | unchanged | install or update command on stderr |
| deadline, per-commit cap or upgrade budget reached | stands | unchanged for deferred tasks | stderr (or failure log) names them and the repair command |
| uncommitted source edits | stands | deferred | stderr names the files |
| answer commit fails (`index.lock` held by another git process) | stands | written, left uncommitted; the next commit carries it | stderr names the git error; no retry |
| format migrator fails | -- | -- | `_schema_version` unchanged; reported |
| hook not installed on a machine | stands | not refreshed by that machine's commits | env_check `check` fails naming the repo |

The only fallback anywhere is the configured chain, and every use of it is recorded.

## Contract changes

- `task.py review` (default `--generate-missing-summaries`) answers the orientation question
  into answer files; it no longer writes task.yaml summary keys or a log line; it still does not
  commit.
- `list --format json`: adds `answers` (state per question); `summary` comes from the
  orientation answer, else legacy; `summary_status` gains `invalid` and `not-applicable`, so
  `schema_version` goes to `"2"` (a widened value set; `docs/reference/vacuous-checks.md`
  Shape 9).
- `update --summary`: unchanged write; a note when an orientation answer supersedes it.
- Added verbs `task.py answers refresh|upgrade|status|prune|hook`; the `task-kit` launcher
  forwards `task-kit answers ...`.
- Added environment variable `TASK_KIT_ANSWERS_HOOK` (`skip`).
- `session_publish` publishes this machine's answer files as well as its session file.
- task-kit's SessionStart hook gains the `startup` source (automatic upgrade).
- `plugins/CLAUDE.md` "Who talks to an LLM" row for task-kit: `llm_kit.dispatch.declaration.run`
  instead of `BackendOptions` and `CodexCliBackend`.

## Submit gates

- **Razor:** `answers.models` (default `[luna, haiku]`), `answers.effort` (medium),
  `hook_deadline_s` (90), `max_tasks_per_commit` (4), `auto_upgrade` (false), `upgrade_budget`
  (20), the question catalog (shipped plus fleet files); commit-time refresh is opt-in per
  repository by `answers hook install`. Hardcoded, test fails: the answer file layout and record
  schema (task-kit's own data format) and the input reader vocabulary (code). Git-only hook and
  answer commits: covered by the registered stance "task-kit:task privileges git as the
  automated VCS", which U9 extends to name them (OP-7).
- **Write paths:** answer files are project-durable (committed task data); the hook stub goes
  into the repository's git hooks directory (machine-local repository configuration, not
  tracked); `<data dir>/answers/upgrade-failures.log` is user-scoped, written only on failure.
  None is derived from the script's own location.
- **Agent directives:** SKILL.md text for `answers` and `review` (U9) states the configured chain,
  not "Codex Luna at medium effort", which stops being true.
- **Optional dependency:** llm-kit REFUSE for answering (D5).
- **Shipped model families:** `[luna, haiku]`, added to the guard (U3).

## Implementation units

Fixed interfaces (all in `task_system/`):
- `questions.py` (U1): `Question(id, version, title, applies_to, inputs, max_input_chars,
  prompt, answer_schema, grounding, models, effort, digest)`, `Catalog(version, questions)`,
  `load_catalog(shipped_dir, fleet_dir=None)`, `CatalogError`.
- `question_inputs.py` (U1): `READERS`, `read_inputs(folder, block, names, limit) ->
  InputBundle(texts)`, `inputs_fingerprint(question, bundle) -> str`.
- `answer_store.py` (U2): `answer_path(folder, qid, host)`, `AnswerRecord`,
  `read_answers(folder, qid)`, `write_answer(folder, qid, host, record)`,
  `best_answer(folder, question, current_fp) -> AnswerState(status, record, host, notes)`,
  `coverage(folder, catalog, block) -> Coverage`.
- `answers_config.py` (U3): `AnswersConfig(models, effort, hook_deadline_s,
  max_tasks_per_commit, auto_upgrade, upgrade_budget)`, `load_answers_config(fleet_path=None,
  machine_path=None)`, `DEFAULT_MODELS`.
- `answer_chain.py` (U3): `ask(question, bundle, config, *, deadline=None,
  reachability_cache=None, run=None) -> Asked(answer, entry, effort, reselections)`;
  `validate_answer(question, raw, bundle)`; raises `ChainExhausted`, `AnswerBackendUnavailable`.
- `answer_sync.py` (U4): `refresh(repo_root, *, mode, refs=(), commit=True, publish=False,
  budget=None, config=None) -> RefreshReport`, `touched_task_folders(repo_root, rev="HEAD")`,
  `sequencer_state(repo_root)`.
- `format_migrations.py` (U4): `MIGRATORS`, `migrate_folder(folder) -> MigrationResult`.
- `answer_hook.py` (U6): `render_stub()`, `hook_path(repo_root)`, `install(repo_root)`,
  `check(repo_root)`, `remove(repo_root)`.
- `answers_cli.py` (U4): `main(argv) -> int`.

| Unit | Scope | Owns | Depends on |
|---|---|---|---|
| U1 catalog and inputs | question file schema, layers, retire; readers incl. bookkeeping exclusion; digest and fingerprint; the two v1 question files | `task_system/questions.py`; `task_system/question_inputs.py`; `plugins/task-kit/skills/task/questions/{catalog.yaml,orientation.yaml,decisions.yaml}`; `tests/task-kit/test_task_questions.py`; `tests/task-kit/test_task_question_inputs.py` | none |
| U2 answer store and readers | record format, best-answer rule, states, coverage, ASCII and caps; `listing.py` answers, legacy read compatibility, projection `"2"` | `task_system/answer_store.py`; `task_system/listing.py`; `tests/task-kit/test_task_answer_store.py`; listing tests asserting summary fields | U1 interface |
| U3 config and chain | config layers; `run` plus `exclude` re-selection; answer validation incl. grounding; llm-kit probe and minimum version; latency of the four entries on 10 real tasks (p50/p95, sets the deadline); whether an `OutputContract` keeps the transports | `task_system/answers_config.py`; `task_system/answer_chain.py`; `tests/task-kit/test_task_answers_config.py`; `tests/task-kit/test_task_answer_chain.py`; `tests/repo-scripts/test_shipped_model_families.py` | none |
| U4 refresh and upgrade engine | D4 algorithm; upgrade with budget, batches, fetch-skip; `--only` commits; format migrator runner (fixture-tested); CLI; launcher routing | `task_system/answer_sync.py`; `task_system/format_migrations.py`; `task_system/answers_cli.py`; `plugins/task-kit/bin/task_kit_entrypoint.py`; `tests/task-kit/test_task_answer_sync.py` | U1, U2, U3 |
| U5 publication and automatic upgrade | own-files rule in the publisher; SessionStart `startup` source spawning one locked, budgeted upgrader with its failure log | `task_system/session_publish.py`; `plugins/task-kit/hooks/hooks.json`; `plugins/task-kit/hooks/sessionstart/session-start.sh`; `task_session_start.py`; `tests/task-kit/test_task_sessions_publish.py`; `tests/task-kit/test_task_session_start_hook.py` | U4 |
| U6 hook install | stub, install/check/remove, foreign-hook refusal, `core.hooksPath`, LF and mode; end-to-end once U4 lands | `task_system/answer_hook.py`; `tests/task-kit/test_task_answer_hook.py` | none; end-to-end after U4 |
| U7 task.py, review, validate | `answers` verb dispatch; `review` through U3 and U2; `update --summary` note; review HTML; answer files as non-gating notes in validate; `summary_ops.py` reduced to the review adapter | `task.py`; `task_system/summary_ops.py`; `task_system/review_html.py`; `task_system/validate.py`; `tests/task-kit/test_task_answers_review.py` and review/update/validate tests asserting summary text | U2, U4 |
| U8 dashboard | `answers` and coverage in inventory and snapshot (digest included); orientation block with as-of commit, model, stale marker; decisions with defaults; row tooltip = headline; layout from the picked mockup | `task_system/dashboard/{inventory,snapshot}.py`; `task_system/dashboard/static/{app.js,app.css}`; `plugins/task-kit/skills/task/references/dashboard.md`; `tests/task-kit/test_dashboard_answers.py`; `tests/task-kit/test_dashboard_ui_contract.py` | U2; mockup pick |
| U9 docs, Windows gate, release | SKILL.md; README; design doc section; `plugins/CLAUDE.md` (LLM table row, git-privilege register entry); version bump in `plugin.json` and `pyproject.toml`; Windows smoke over ssh (hook from Git Bash and from PowerShell-launched git); claudx smoke; git-code-review; md-domain lanes; publish on the user's go-signal | `plugins/task-kit/skills/task/SKILL.md`; `plugins/task-kit/{README.md,.claude-plugin/plugin.json,pyproject.toml}`; `plugins/task-kit/skills/task/design/task-system-design.md`; `plugins/CLAUDE.md` | U1 to U8 |
| U10 fleet wiring (claude-settings) | `config/task-kit.yaml` with the fleet `answers:` values; env_check running `answers hook check` / `install --repo "$DEVROOT/tasks"`; first `answers upgrade --all` on one machine (75 tasks) | claude-settings `config/task-kit.yaml`; `scripts/env/tasks-links.sh` or a sibling env_check | U9 published |

Order: U1, U3 and U6 in parallel (U2 may start on U1's fixed interface); U4; then U5 and U7 in
parallel; U8 alongside once U2 lands and a mockup is picked; U9; U10.

## Checks (each shown to fail by the stated revert)

- **U1** A fleet file with a shipped id replaces it; `retired: true` drops it; an unknown reader
  is an error naming the file (revert: skip it silently -> red). Appending a `refresh (no field
  edits)` line leaves the orientation fingerprint unchanged (revert the exclusion -> changed,
  red). Editing a prompt changes the digest.
- **U2** Writing an answer changes no fingerprint (revert: hash every folder file -> stale at
  once, red). A current record beats a newer stale one; among current records the newest wins
  (revert the ordering -> red). A malformed file is an `invalid` note while another host's valid
  answer still shows. Legacy `task.summary` is served, labelled, when no orientation answer
  exists.
- **U3** A fake first entry returning invalid JSON is excluded and the second entry answers,
  with `reselections` naming the first (revert: one `run` without `exclude` -> `RUN_FAILED`,
  red). `effort: medium` reaches luna (revert -> registry default `high`, red). A default absent
  verbatim from the inputs is rejected (revert grounding -> accepted, red). All entries failing
  raises `ChainExhausted` naming each. Layer precedence per key; unknown key names the file. The
  families guard goes red when `haiku` is removed from the default.
- **U4** (scratch git repos) A commit touching one task yields one follow-up commit with only
  this host's answer files, and a separately staged path stays staged (revert `--only` -> swept
  in, red). Each recursion guard alone stops recursion. A commit during a rebase writes nothing
  (revert the sequencer guard -> red). A dirty `plan.md` defers. Upgrade skips a task holding a
  current answer from another host (revert -> recomputed, red) and honours the budget. A fixture
  migrator applied twice yields byte-identical files; a failing one leaves `_schema_version`
  unchanged.
- **U5** The publisher pushes a commit holding only this host's answer files and refuses one
  holding another host's (revert the host check -> pushed, red). `startup` with `auto_upgrade:
  false` starts nothing; with `true` and pending work it spawns one upgrader; a second start
  finds the lock held and exits.
- **U6** LF, executable stub with marker; second install a no-op; a foreign hook refused and left
  byte-identical (revert -> overwritten, red); `core.hooksPath` honoured; `check` nonzero when
  missing or outdated.
- **U7** `review` writes answer files and leaves task.yaml and log.md byte-identical (revert to
  `state_ops.update` -> both change, red); `update --summary` prints the superseded note; an
  invalid answer file never gates `work`.
- **U8** A stale answer renders its marker and as-of commit (revert -> no marker, red); decisions
  render their defaults; a changed answer changes the snapshot digest.
- **U9** Windows: the hook runs from Git Bash and from PowerShell-launched git and the follow-up
  commit exists; or ship with the gap stated.

## Questions for the user

1. **Commit wait.** At the measured rate (517 tasks-repo commits in 7 days, 500 touching one
   task) every task commit would wait for its answers: seconds on a qwen38 endpoint (U3
   measures it), at most the 90 s deadline on luna or haiku. Accept that, or prefer commits
   that never wait, with answers committed and published in the background afterwards
   (fresh on other machines later, and the caller's next git command can meet a held
   `index.lock`)?
2. **Fallback spend for automatic upgrades.** When both qwen38 endpoints are down, should the
   automatic bulk upgrade fall back to luna and haiku (spends subscription quota, up to
   `upgrade_budget` tasks x 2 questions per session start), or answer only on qwen and leave
   the rest for later? Commit-time refresh would still fall back either way.
3. **First questions.** Are `orientation` and `decisions` (waiting-on-you with defaults) the
   right first pair, or should another catalog row lead?

## Blockers and outside dependencies

- None for U1 to U9.
- U10 changes the claude-settings repository, outside plugins-kit.
- Publishing task-kit needs the user's go-signal (root `CLAUDE.md`); U10 needs the published
  launcher.
- The Windows check needs ssh access to a Windows fleet machine.
