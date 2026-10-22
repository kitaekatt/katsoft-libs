# task-kit export protocol

`task-kit export` gives another program the facts of every task task-kit can
see, as JSON. It is the interface databench's tasks module reads. This page is
normative; `task-export.schema.json` beside it is the JSON Schema of each
document. task-kit works the same whether or not anything reads the export.

The export carries task FACTS: what each task folder, its validation, its
items and its working-session registrations say. Views built from them
(sections, needs, hand-off commands, item UI states, counts, filters) belong to
the consumer.

## Commands

```
task-kit export protocols --format json
    no discovery; kind "protocols", payload {"protocols": [1]}
task-kit export tasks --format json --protocol N [--parent DIR]...
task-kit export item --format json --protocol N --checkout ID --task PATH --item ID
```

`--parent DIR` (absolute, repeatable) names a directory whose child
directories are projects. Without it, task-kit uses its own configuration:
`project_directories` in `~/.claude/task.local.yaml`, else `$DEVROOT`.

## Output

- stdout: exactly one JSON document and a newline, for success and for every
  handled error. Nothing else is ever written to stdout.
- stderr: human diagnostics only. A consumer never parses it.
- Success: `{"protocol": N, "task_kit_version": "x.y.z", "kind": "<kind>", "payload": {...}}`
- Error: `{"protocol": N, "task_kit_version": "x.y.z", "error": {"code": "<code>", "message": "<text>"}}`
- `protocol` in an error document is the requested protocol when this
  task-kit serves it, else the highest protocol it serves.
- Exit codes: 0 ok; 1 internal (also `discovery_failed`); 2 usage; 3 not
  found; 4 protocol unsupported.
- Error codes: `usage`, `unknown_checkout`, `unknown_task`, `unknown_item`,
  `protocol_unsupported`, `discovery_failed`, `internal`.

## Negotiation and compatibility

- The consumer calls `export protocols` first, then passes the highest
  protocol both sides support. An unsupported `--protocol` exits 4, and the
  message lists the supported protocols.
- A task-kit that predates `export` answers `export protocols --format json`
  with exit 2 and an empty stdout. A current task-kit never does, because every
  handled outcome writes a document; that is how a consumer tells "too old"
  from "failed".
- Within a protocol, fields are only added. Enums are open: a consumer maps an
  unknown value to "other". A removed, renamed, retyped or newly required field
  takes the next protocol number, and the old protocol stays served for one
  task-kit minor release.

## Payload of `tasks` (protocol 1)

Every field is present; `?` marks a value that may be null.

| Field | Type |
|---|---|
| `generated_at` | string, ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`) |
| `revision` | string; changes whenever any field below changes |
| `repositories[]` | `{id: string, name: string, checkouts: [string]}`; one per origin, checkouts merged |
| `checkouts[]` | `{id: string, root: string (absolute), repository: string}` |
| `tasks[].checkout`, `.path`, `.stub`, `.location` | string; `path` is project-relative (`dev/tasks/<stub>`) |
| `tasks[].revision` | string; the content revision of the task's `task.yaml`, `plan.md` and `log.md` |
| `tasks[].title?`, `.priority?`, `.description?`, `.summary?` | string; `summary` is the headline of the task's orientation answer (current or stale) when it has one, else `task.summary` |
| `tasks[].summary_source?` | open enum: `"answer"` (the orientation answer's headline), `"task.yaml"` |
| `tasks[].answers` | `{<question id>: {state, answer?, answerer?, as_of?, fingerprint?}}`, one entry per question of the question catalog (empty when the catalog cannot be loaded) |
| `tasks[].coverage?` | `{catalog_version: integer, applicable: integer, current: integer}`: of the catalog's questions that apply to the task's classification, how many have a current answer; null when the catalog cannot be loaded |
| `tasks[].classification` | open enum: active, blocked, deferred, closed, archived, orphaned, remote, invalid |
| `tasks[].blocked_by` | [string] |
| `tasks[].last_activity?` | string, `YYYY-MM-DD HH:MM` of the newest update (a date-only entry reads as 12:00) |
| `tasks[].updates[]` | `{date: string, time?: string, detail: string}`, newest first, at most 20 |
| `tasks[].updates_total` | integer |
| `tasks[].has_items_block` | boolean |
| `tasks[].items[]` | `{id, title: string, state: open enum available, in-flight, blocked-user, deferred, priority?: string, note?: string}` |
| `tasks[].findings` | `{errors, warnings, item_errors, notes: [string]}` |
| `tasks[].sessions[]` | `{host, session_id, registered_at: string, state: open enum running, not running, unverified, local: boolean, recorded_at?: string, task_state?: string}` |
| `errors[]` | `{code: string, message: string, root?: string}`: discovery problems that did not stop the export |

Identity and grouping:

- A checkout id is the first 16 hex digits of SHA-256 over the checkout's
  canonical real path. A repository id is the first 16 hex digits of SHA-256
  over `repo:` plus its normalized `origin` URL (`path:<real path>` for a
  checkout without one).
- A task folder reached through several checkouts is listed once, under the
  checkout with the shortest directory name (the rule `task list --scope all`
  uses).
- `tmp/` tasks are not exported.
- Order: repositories by name, checkouts by root, tasks by repository name,
  then path, then checkout root.
- `errors[].code` is an open enum. task-kit 0.15.0 emits `unreadable`,
  `repo_identity` and `sessions`; the release that adds `tasks[].answers` also
  emits `answers` and `questions`.

Answers (`tasks[].answers`):

- A question is declared data (id, version, the inputs it reads, an answer
  schema, the classifications it applies to); the catalog is the shipped
  questions plus a fleet layer. An answer is current when the fingerprint it
  stored equals the fingerprint of the question definition plus its inputs now.
- `state` is an open enum: `current`, `stale` (an answer exists for older
  inputs), `missing`, `invalid` (answer files exist and none is usable) and
  `not-applicable` (the question does not apply to the task's classification).
  An unusable answer file is also an `errors[]` entry with code `answers`,
  naming the file; it never stops the export.
- `answer` is the chosen answer, an object shaped by the question's answer
  schema, null for `missing`, `invalid` and `not-applicable`. Any machine
  chooses the same one among the records of several machines, using nothing
  machine-local.
- `answerer` is `{kind: "model" | "script", entry?, name?, tier?, effort?,
  attempts: [{entry, outcome}]}`: a model answer names its endpoint entry, cost
  tier and effort; a script answer names the script. `as_of` is
  `{task_commit?, code_commit?}`, the commits whose content the answer
  describes. `fingerprint` is the inputs fingerprint a model answer stored,
  null for a script answer.
- A script answer is computed when the export runs, from the task folder's git
  history and the clock, and is never stored. The shipped script questions are
  `activity` (`days_since`, `last_at`, `last_entry`) and `task_commits`
  (`commits`: `{sha, date, subject}`, newest first, at most 10).

## Payload of `item`

`{checkout, path, item: <a tasks[].items[] record>, plan_section?: string,
plan_section_truncated: boolean}`. `plan_section` is the body of the item's
plan.md section (the heading whose first token is the item id), at most 8000
characters; null when there is no such section. The item must belong to a
task the `tasks` export lists under that checkout: an unknown checkout, task
or item answers `unknown_checkout`, `unknown_task` or `unknown_item` (exit 3).

## What the export never does

It writes nothing: no task folder, no data directory file, and no git index
refresh (its git commands run with `GIT_OPTIONAL_LOCKS=0`). It never imports
the dashboard's code.
