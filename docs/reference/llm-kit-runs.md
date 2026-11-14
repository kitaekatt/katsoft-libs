# llm-kit run records and run stores

`llm_kit.runs` keeps model runs and comparisons in one format and one place. A run record (`llm-kit.run-record/v1`) is one attempt of one case on one model selection. A comparison is the records that share one `group.id`. The record, import and store formats and their reader rules are in `docs/reference/llm-kit-formats.md`. This page gives the store, the write contract, the index and the CLI.

The package is standard library only. It never runs git and makes no model call.

## Store layout

A run store is one directory:

| Path | What |
|---|---|
| `store.json` | `llm-kit.run-store/v1`: the content classes that the store accepts. |
| `.gitignore` | Ignores the index. `init` writes it. |
| `records/<YYYY-MM>/<id>.started.json` | The started event of an attempt. Optional. |
| `records/<YYYY-MM>/<id>.result.json` | The result event of an attempt. |
| `blobs/<h[:2]>/<h>` | Blob bytes, named by their SHA-256 (the blob store of `llm_kit.graph.media`). |
| `.index.sqlite` | The derived index. Never committed. |

`YYYY-MM` comes from the record id. A store can be its own git repository, so that a team shares it, or a local folder that is never pushed. Use two stores when some runs hold sensitive content: a shared store that accepts `synthetic` and `internal`, and a local store that accepts `sensitive`.

## Started and finished: immutable events

Every file in a store is written one time and is never replaced. An attempt has at most one started event and at most one result event:

1. Before the call, a writer can put the started event (`status: started`).
2. After the call, the writer puts the result event (`ok`, `invalid`, `failed` or `refused`). The result repeats every field of the started event with the same value and adds the result fields. The store refuses a result that changes a started field (code `conflict`).
3. The record of an id is its result event. While no result exists, it is the started event, so an unfinished attempt stays visible with status `started`.

A result without a started event is allowed (a writer that records only at the end). To put an event again with the same bytes does nothing, so an import can run again. Two clones of a store never conflict, because two writers never write the same file with different bytes. An event file appears whole: the store writes a temporary file and links it into place, so a reader never sees a partial event.

Python: `start_record(...)` builds a started event and `finish_record(started, status=..., ...)` builds its result.

## Write contract: the whole batch first

`RunRecordStore.put_many(items)` takes `(record, blobs)` pairs. `blobs` maps a `blob:` reference to its bytes or text. Before the store writes any file, it checks the whole batch in this order. The first failure refuses the whole batch, and the store writes nothing.

| Check | Code |
|---|---|
| Each record is a valid `llm-kit.run-record/v1` record. | `invalid` |
| The bytes of each supplied blob hash to its reference. | `blob-hash` |
| The declared class of each record is in the store's `accepts`. | `policy` |
| Each blob that a record names is supplied or is already in the store. | `incomplete` |
| Each supplied blob is named by a record of the batch. A blob that no record names has no class. | `unreferenced` |
| No event differs from an event with the same id and kind in the store or the batch, and no result changes a field of its started event. | `conflict` |

Then the store writes the blobs, then the events. `check_many(items)` runs the same checks and writes nothing. `put(record, blobs)` is a batch of one.

### What the store can and cannot detect

The store cannot read text and decide what it is about. It does not detect personal data, secrets or client content in a record or a blob. It enforces these items:

- The declared class. Each record declares one class (`synthetic`, `internal` or `sensitive`) for itself and for every blob it names. A record with no class or an unknown class is refused. A writer that is not sure declares `sensitive`.
- The policy. A record whose class is not in `accepts` refuses the batch, so a mixed batch is refused as a whole in a store that does not accept every class in it.
- Completeness. No record refers to a blob that is missing, and no blob enters without a record that gives its class.
- The path rules of the format. No absolute path, drive path or `..` path in a path field.

The declared class is the writer's statement. The store refuses what that statement and the policy do not allow.

## Index

`store.index()` returns a `RunIndex` on `.index.sqlite`. The index is a cache of metadata. The events are the truth.

- It holds ids, statuses, times, flow, group id and kind, case id, selection and model names, content class, cost, token counts, total time, error code and the reuse key. It holds no group label, label, error message, blob or answer.
- `query(...)` syncs first: it reads new and changed event files and drops records whose files are gone. Records that arrive by `git pull` are found with no manual step.
- `rebuild()` deletes the file and makes it again.
- The index refuses to open when the store's `.gitignore` does not hold the line `.index.sqlite*` (code `index-not-ignored`). `llm-kit runs init` adds the line.

## Store location

`--store DIR`, else the `LLM_KIT_RUN_STORE` environment variable. With neither, the command refuses (code `no-store`). A run never writes to a directory that nobody chose.

## CLI

```
llm-kit runs init --store DIR --accepts synthetic,internal
llm-kit runs import FILE.jsonl [--check]
llm-kit runs list [--flow F] [--group G] [--model M] [--status S] [--case C] [--since D] [--until D] [--limit N]
llm-kit runs show ID [--with-blobs]
llm-kit runs reindex
```

- `import` reads a `llm-kit.run-import/v1` file offline, checks it whole and writes it as one batch. `--check` writes nothing.
- `list` prints one JSON index row for each line, oldest first. `--since` and `--until` take a date or a time prefix; `--until` is exclusive. `--limit N` keeps the newest N rows.
- `show` prints the record. `--with-blobs` prints an import line with the record's blobs, ready for another store's `import`.
- Exit codes: 0 success; 1 the store refused (one JSON object `{"error": {"kind": "runs", "code", "message"}}` on stderr, nothing written); 2 usage, including `no-store`.

## Writers in other languages

A writer in another language builds records as JSON and writes one import file for each run. It then runs `llm-kit runs import`. It needs no llm-kit package of its own. It does not compute the reuse key: the Python reader computes it for the index.

## Comparisons (later)

The format holds what a comparison command needs: `group` (kind `compare`), the reuse key, `provenance.scorer` (name, version, SHA-256) and the started event that a runner writes before each call. `RunIndex.query(key=...)` finds earlier records with the same reuse key. The comparison command is not part of this release.
