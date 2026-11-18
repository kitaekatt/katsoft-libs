# llm-kit run records and run stores

`llm_kit.runs` keeps model runs and comparisons in one format and one place. A run record (`llm-kit.run-record/v1`) is one attempt of one case on one model selection. A comparison is the records that share one `group.id`. The record, import and store formats and their reader rules are in `docs/reference/llm-kit-formats.md`. This page gives the store, the write contract, the index and the CLI.

The package is standard library only. It never runs git. It makes no model call itself: a comparison (`llm_kit.runs.compare`, below) takes the caller as an argument, and `llm-kit runs compare` gives it the caller of `llm_kit.dispatch`.

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
llm-kit runs compare --prompt FILE --cases FILE.jsonl --select A,B[,C...] --flow F --content CLASS --max-usd N
                     [--pricing FILE] [--max-output-tokens N] [--timeout-s S] [--group ID] [--label TEXT]
                     [--project-root DIR] [--models-file FILE] [--dry-run]
                     [--scorer-name NAME] [--scorer-version TEXT] [--scorer COMMAND...]
```

- `import` reads a `llm-kit.run-import/v1` file offline, checks it whole and writes it as one batch. `--check` writes nothing.
- `list` prints one JSON index row for each line, oldest first. `--since` and `--until` take a date or a time prefix; `--until` is exclusive. `--limit N` keeps the newest N rows.
- `show` prints the record. `--with-blobs` prints an import line with the record's blobs, ready for another store's `import`.
- `compare` runs a comparison (next section) and prints its result as one JSON object.
- Exit codes: 0 success; 1 the store or the comparison refused (one JSON object `{"error": {"kind": "runs", "code", "message"}}` on stderr, nothing written), or a comparison stopped (the same error object with the stop reason as `code`, and the partial result on stdout); 2 usage, including `no-store`.

## Writers in other languages

A writer in another language builds records as JSON and writes one import file for each run. It then runs `llm-kit runs import`. It needs no llm-kit package of its own. It does not compute the reuse key: the Python reader computes it for the index.

## Comparisons

`llm_kit.runs.compare` sends one prompt file, filled with the slots of each case, to two or more model selections. It records each attempt as a run record in one group (`group.kind: compare`). The comparison is the records that share the group id.

### The command

```
llm-kit runs compare --store STORE --prompt prompts/summary.md --cases cases.jsonl \
  --select summary.primary,summary.candidate --flow summary --content synthetic \
  --max-usd 2 --pricing pricing.yaml --max-output-tokens 400
```

- `--prompt` is a `llm-kit.prompt-file/v1` file under the project root (`--project-root`, default the current directory). `--cases` is a JSON Lines file of `llm-kit.compare-case/v1` lines (`{"id", "slots"}`). Each case must fill exactly the slots of the prompt.
- `--select` names two or more selection keys of the project model file (`.llm-kit/models.yaml`, or `--models-file`). The personal layer (`models.local.yaml`) applies, so a candidate model can be a local selection. Two keys that resolve to the same model, effort and request are refused.
- `--flow` is the writer's name for the work. It is part of the reuse key. `--content` is the content class of every record, and the store must accept it.
- `--max-usd` is the budget of the group, failed and pending attempts included (see "Budget").
- `--pricing` is a pricing table in the format of `llm_kit.pipeline.runtime.platform.load_pricing` (YAML or JSON): wire model (the row's `wire_model`, else its `model`) -> `{input, output, cache_hit, context_window}`, in USD per one million tokens. An endpoint that the registry classifies `local` (unmetered and owned by self) costs 0 with no entry. Every other selection needs an entry; `{input: 0, output: 0}` declares it free (for example a harness on a subscription).
- `--max-output-tokens` sets the output cap of every transport call. Without it, a call uses the row's `max_output_tokens`, else its `request_defaults.extras.max_tokens`, else no cap. A harness takes no cap.
- `--dry-run` prints the plan (each attempt with its action and cost bound, the spend so far, the total bound and whether it fits) and calls nothing. It writes no record.
- `--scorer COMMAND...` takes every word after it, so give it last. `--group` and `--label` name the group; the default group id comes from the flow, the prompt hash, the cases and the selection names, so the same command run again continues the same group.

The output is one JSON object: the group, the spend, `stopped` and `message`, the totals of each selection (record statuses, the USD it added and the mean of each numeric score), and each case with the answers of every selection side by side (action, record id, status, error, scores, text).

### Record before send, no retry

For each attempt that needs a call, the comparison writes the started event before the call and the result event after it. The started event holds the attempt's cost bound in the label `compare.bound_usd`. The call goes through `llm_kit.dispatch.invoke` (protocol 2) with `max_retries: 0`, so an OpenAI-compatible transport sends the request once; the Claude and Codex CLI transports run once. A failed attempt stays failed and the run goes on with the next attempt. A halted call (a quota, credit or auth stop) is recorded and stops the run. When you run the command again, an attempt with no `ok` record is called again.

### Reuse

Two attempts with the same reuse key (`llm-kit.run-key/v1`) asked the same question of the same model. The key covers the flow, the case input, the prompt hash, the slot hash, the resolved endpoint, wire model and effort, and the whole request (output cap, request extras, the hash of the prompt as sent). A score also depends on its scorer. So for each attempt:

| The store holds | Action |
|---|---|
| An `ok` record in this group with the same key and the same scorer hash (both null counts as the same). | `done`: nothing is written or sent. |
| An `ok` record with the same key in another group, or with another scorer. | `copy`: a new `ok` record in this group reuses the answer with no call. It has the label `compare.reused_from` (the original record id), no usage, no cost and no timings. The current scorer scores it. |
| No `ok` record with the same key. | `call`. |

A changed prompt, case, flow, model, effort, cap or request extra changes the key, so the attempt is called again.

### Budget

The budget covers the whole group, across runs. The spend of the group is the sum of:

- the recorded cost of each result, or its cost bound when the cost is unknown (failed attempts are included);
- the cost bound of each started event that has no result (an attempt that stopped after it was sent, which may have been billed);
- 0 for each copy.

Before each call, the comparison adds the attempt's cost bound to the spend. If the sum is more than `--max-usd`, the attempt is not started and the run stops (`stopped: budget`). A record in the group that has no known cost and no bound label refuses the plan (`conflict`); use another group.

The bound is a worst case: the UTF-8 byte count of the prompt plus 64 input tokens (a token holds at least one byte), at the larger of the input and cache rates, and the output cap (else the price's `context_window`) at the output rate. A selection with no price, or with neither a cap nor a context window, is refused before any write (`unpriced`). A harness adds its own context to the prompt, so only a free price is accepted for a harness. The recorded cost of a call is the provider's authoritative cost (`reported`: a native `usage.cost` from an endpoint that declares `billing.mode: provider-reported`, as the shipped OpenRouter endpoints do, or a front door), else the larger of the backend's estimate and the price of the reported tokens (`estimated`), else unknown. After a call, a recorded cost that takes the spend over the budget stops the run (`stopped: over-budget`).

A scorer's own model calls are not in the budget.

### Scorers

A scorer is a program that reads one `llm-kit.run-import/v1` line on stdin: the result record (with `scores` empty) and the blobs of its case input and answer. It writes the same line on stdout with `record.scores` filled. Each score has the run-record score shape (`name`, `value`, `scale`, `by`, `note`). A note is a `blob:` reference whose text the line adds to `blobs`. Any other change to the record fails the scorer.

```python
import json, sys
line = json.loads(sys.stdin.read())
answer = line["blobs"][line["record"]["output"]["text"]]["text"]
line["record"]["scores"] = [{"name": "words", "value": len(answer.split()), "scale": None,
                             "by": {"kind": "rule", "name": "words", "selection": None}, "note": None}]
print(json.dumps(line))
```

`provenance.scorer` is `{name, version, sha256}`. The hash covers the words of the command and the bytes of each word that names a file. It does not cover a program found on PATH or a file that the scorer reads itself; name those in `--scorer-version`. When a scorer fails, the comparison records the attempt as `invalid` (error code `scorer-failed`, the answer kept), stops and exits 1.

### Python

`plan_compare(store, project_root=, prompt=, cases=, candidates=, flow=, content=, max_usd=, scorer=None, group_id=None, label=None, tool=None)` checks everything and returns a `ComparePlan` (`to_dict()` is the dry-run output). `run_compare(plan, call)` runs it and returns a `CompareResult`. `call(candidate, prompt) -> CallResult` makes exactly one model call and does not retry; a client with its own transport gives its own caller. A `Candidate` is a resolved selection with its `Price`. `read_cases`, `command_scorer` and `attempt_bound_usd` are the other helpers. `examples/compare_models.py` runs a comparison with a stand-in caller.
