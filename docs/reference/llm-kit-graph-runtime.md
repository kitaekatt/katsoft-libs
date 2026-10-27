# llm_kit.graph runtime: runs, records, freshness and stores

This page covers how `llm_kit.graph` runs a graph for one work item, the run
record format, the reuse rule (freshness), seeding, the content-addressed
stores and the `run` and `status` verbs. The file format is in
[llm-kit-graph.md](llm-kit-graph.md); the expression language is in
[llm-kit-graph-expr.md](llm-kit-graph-expr.md).

Modules: `run.py` (the runner), `records.py` (run records and the per-item
run store), `freshness.py` (references, input hashes, the reuse index),
`media.py` (the content-addressed value, blob and media stores),
`commands/run.py` and `commands/status.py`.

## Where a run writes

The project config (`.llm-kit/graphs.yaml`, section `runs`) sets the paths.
`{item}` is the work item id; `{git_common_dir}` is the output of
`git rev-parse --git-common-dir` in the project root (asked for only when a
template uses it).

| Store | Config key | Default | Content |
|---|---|---|---|
| run records | `runs.dir` (required) | none | `<dir>/runs/<run_id>.json` |
| values | `runs.dir` | `<dir>/values/` | `<dir>/values/<h[:2]>/<h>.json`: one JSON value in its own key order |
| blobs | `runs.blobs` | `<dir>/blobs/` | `<blobs>/<h[:2]>/<h>`: raw bytes (exact prompts, raw answers) |
| media | `runs.media` | `<dir>/media/` | `<media>/<h[:16]><ext>`: media files (images, audio) |

Every store is content-addressed and write-once. `h` is the SHA-256 (64 hex)
of the stored bytes; a value file holds the value as compact ASCII JSON (no
spaces) in the value's own key order, so `sha256sum` of the file equals its
name. Key order is data for a consumer that serializes the value, so a value
read back from a record has the key order its node produced. Two values that
differ only in key order have different references. Freshness is unaffected:
the input hash covers the canonical `stable_json` (sorted keys) of the reads.
A value with sorted keys has the same bytes and reference as before. A media name
is the first 16 hex of the SHA-256 plus the extension (`a1b2c3d4e5f60718.png`);
writing different bytes under an existing media name is an error.

Item ids match `^[A-Za-z0-9][A-Za-z0-9._-]*$`.

### References

A record never holds a node value inline. It holds a reference string:

| Reference | Resolves to |
|---|---|
| `value:<64 hex>` | the JSON value in the value store |
| `blob:<64 hex>` | the bytes in the blob store (UTF-8 text for an attribute) |
| `media:<16 hex><ext>` | the file in the media store |

`RunStore.resolve(ref)` returns the JSON value, the decoded text or the media
`Path`. A missing file raises.

## Run record format (`llm-kit.graph.run/v1`)

One JSON file per run or seed, `<dir>/runs/<run_id>.json`, written atomically
(indent 1, sorted keys, ASCII). The runner rewrites it after every node, so a
reader sees progress; `status: "running"` marks a run in progress or a run
that died without finishing.

`run_id` is `YYYYMMDDTHHMMSSffffffZ-<4 hex>` (UTC); sorting ids sorts runs by
start time.

```json
{
 "format": "llm-kit.graph.run/v1",
 "run_id": "20261026T101500123456Z-3f9a",
 "kind": "run",
 "item": "night-petition",
 "status": "done",
 "started_at": "2026-10-26T10:15:00.123456Z",
 "ended_at": "2026-10-26T10:16:02.000001Z",
 "graph": {"id": "afterplay.beta", "file": "pipeline/moments/beta/graph/beta.graph.yaml", "hash": "<64 hex>"},
 "graphs": {"<graph hash>": {"...": "see Graph structure"}},
 "files": {"pipeline/moments/beta/prompts/story-structure.md": "blob:<64 hex>"},
 "target": ["brief"],
 "force": [],
 "inputs": "value:<64 hex>",
 "inputs_from": {"moment": "20261026T090000000000Z-0a1b"},
 "outputs": null,
 "stop": null,
 "error": null,
 "counts": {"done": 12, "reused": 3},
 "nodes": {"<node path>": {"...": "see Node record"}}
}
```

| Field | Meaning |
|---|---|
| `kind` | `run`, or `seed` (written by `Runner.seed`) |
| `status` | `running`, `done`, `failed` (a node failed), `stopped` (a node stopped the run; see `stop`) |
| `graph` | the root graph: id, file (relative to the project root, POSIX), graph hash |
| `graphs` | the structure of the root graph and every graph it runs, keyed by graph hash (each `file` relative to the project root, POSIX) |
| `files` | snapshots of the graph files and the `file`/`template` parameter files, by project-relative path, in the blob store (for replay against the recorded version) |
| `target` | the top-level node ids the run was limited to, or `null` (the whole graph) |
| `force` | node paths whose records were not reused |
| `inputs` | the graph inputs after inheritance and defaults |
| `inputs_from` | input name -> run id it was inherited from (absent names were given or defaulted) |
| `outputs` | the graph outputs, or `null` when the run did not finish or ran with a target |
| `stop` | `null`, or `{"reason", "node", "message"}` (plus `halt`, llm-kit's halt document, for reason `halted`; `wait` for `waiting`; the spend detail for a spend reason); reason `unsupported` = a human node without the wait service |
| `resumed_from` | present on a run made by `resume`: the stopped run it continues |
| `replay` | present on a run made by `replay`: `{run, node, graph, graph_file}`, the source run, the replayed node, and the recorded graph hash and file |
| `error` | `null`, or `{"type", "message", "traceback"}` when the runner itself raised (the run is `failed`) |
| `counts` | node records per status |
| `nodes` | node path -> node record, in the order the records were made |

Every file path in a record is relative to the project root in POSIX form
(`records.record_path`, which refuses a path outside the project root), so a
committed record names no machine path. `records.resolve_record_path(root,
text)` turns one back into a path under the project root and refuses an
absolute or escaping path.

Every string in a record is also made portable when the record is written
(`portable.PathScrubber`): the project root becomes a project-relative path
(the root itself becomes `.`) and the home directory becomes `~`. This covers
what an op writes into its receipt (a `llm.subprocess` `cwd`, `argv` and
`stderr` tail) and the error and stop messages. A path matches only at a path
boundary (`/work/proj2` is not the root `/work/proj`).

### Graph structure (`graphs`)

```json
{
 "id": "afterplay.beta.story-structure#loop",
 "file": "pipeline/moments/beta/graph/stages/story-structure.graph.yaml",
 "location": "nodes.loop.repeat.body",
 "inputs": ["brief", "incumbent"],
 "outputs": ["plot"],
 "nodes": {
  "write": {"kind": "op", "op": "llm.model@1", "paid": false, "deps": ["prep"],
            "body": null, "definition_hash": "<64 hex>"}
 }
}
```

`location` is empty for a graph file; an inline body has no `inputs` and
lists its `out` names as `outputs`. `deps` are the sibling node ids a node
reads (the edges to draw). `body` is the graph hash of the subgraph, map body
or repeat body the node runs, or `null`. `op` is `name@version` for an op node
and the adapter for a commit node.

### Node paths

A path names one node execution. Segments are joined with `/`:

- a top-level node: `plot`
- a node inside a `graph` node's subgraph: `plot/baseline`
- a map item: `pose_images[p-3]` (the item key; `%`, `/`, `[` and `]` in a
  key are percent-encoded); a node in it: `pose_images[p-3]/draw`
- a repeat round: `loop[0]` (from 0); a node in it: `plot/loop[2]/write`

### Node record

```json
{
 "path": "plot/loop[1]/write",
 "node": "write",
 "kind": "op",
 "graph": "<graph hash of the level that holds the node>",
 "op": "llm.model@1",
 "status": "done",
 "definition_hash": "<64 hex>",
 "input_hash": "<64 hex>",
 "reads": "value:<64 hex>",
 "inputs": "value:<64 hex>",
 "params": "value:<64 hex>",
 "attributes": {"out": "value:<64 hex>", "sent": "blob:<64 hex>", "text": "blob:<64 hex>"},
 "receipt": {"model": "...", "usage": {}, "cost": {}},
 "media": ["media:a1b2c3d4e5f60718.png"],
 "started_at": "...", "ended_at": "...", "seconds": 4.2,
 "error": null,
 "reused_from": null,
 "seed": null
}
```

| Field | Meaning |
|---|---|
| `kind` | a node kind (`op graph map repeat human commit`), or `item` (one map item) or `round` (one repeat round) |
| `node` | the node id (for `item` and `round`, the id of the map or repeat node) |
| `key`, `index` | `item`: the item key and position; `round`: `index` is the round number |
| `status` | see the table below |
| `definition_hash` | the node definition hash (`hashing.hash_graph`); `null` for `item` and `round` |
| `input_hash` | SHA-256 of `reads` (see Freshness); `null` for `item` and `round` |
| `reads` | `{reference: {"value": v} or {"missing": true}}` for every reference the node reads from outside itself |
| `inputs` | op: the input values (with port defaults); `graph` node and file bodies: the bound inputs; `human`: the payload; `commit`: the bundle; else `null` |
| `params` | op: the resolved parameters (expressions evaluated, schemas resolved, files as project-relative paths); else `null` |
| `attributes` | attribute name -> reference: `out` for most kinds, `rounds` and `carry` for `repeat`; an op's other attributes (`sent`, `text`) are `blob:` when the value is a string. A skipped node's attributes are `null` values |
| `receipt` | the op's call receipt (JSON, as the op returned it), or `null` |
| `media` | media references the op reported |
| `error` | `{"type", "message"}` for `failed` and `stopped`; `code` for an op error (`OpError`), `halt` for `OpHalted`, `traceback` for an unexpected exception type; type `Inner` on a composite, item or round whose inner node failed or stopped |
| `reused_from` | `{"run", "path"}` of the record a `reused` record copies |
| `seed` | for `seeded`: `{"source": <JSON from the caller>, "valid": bool}` |
| `spend` | a paid op only: the spend admission and settlement (`llm-kit-graph-services.md`) |
| `wait` | an answered `human` node only: the wait unit, interrupt and fencing token (`llm-kit-graph-services.md`) |
| `until`, `carry` | `round` only: the stop rule's value and a reference to the carried values after the round |

Node statuses:

| Status | Meaning |
|---|---|
| `done` | ran in this run and succeeded |
| `reused` | a fresh earlier record was copied (`reused_from`); nothing ran |
| `seeded` | written by `Runner.seed`; an accepted external output |
| `skipped` | its `when` was false; its attributes are null |
| `failed` | raised an error (`error`) |
| `gated` | not run because a node it reads failed |
| `stopped` | stopped the run cleanly (`error`; also in the run's `stop`) |
| `refused` | the spend service refused the call before it was made; the run stops (reason `spend_cap`, `no_estimate` or `no_ledger`) |
| `pending` | not started because the run stopped |

A composite node (`graph`, `map`, `repeat`) has its own record; the records of
its inner nodes, items and rounds follow under its path. A reused composite
has no inner records in this run; follow `reused_from`.

## Freshness: when a node record is reused (Q17)

Before a node runs, the runner computes:

- the definition hash: `hashing.hash_graph` (covers prompts, schemas, files,
  ops `name@version` and bodies);
- the reads: every reference the node's own expressions make to names outside
  the node (`inputs.*`, other nodes' attributes, `item`, `index`, `carry` of an
  enclosing body), including the expressions inside its inline bodies, minus
  the names the node introduces itself (a map's `item` and `index`, a repeat's
  `carry`, `round` and `index`, and the node ids of its bodies). Each
  reference (`plan.out.plot`) is evaluated; a missing one is recorded as
  missing. The input hash is the SHA-256 of `stable_json(reads)`.

A node is reused when an earlier record at the same path (newest first, over
every run and seed of the item) has status `done` or `reused` with the same
definition hash and input hash, or status `seeded` with `valid: true` and
the same input hash (a seed ignores the definition hash: Q39). The record's
attributes are copied into the new run as status `reused`. A forced path
(`force`) is never reused, nor is any node above or below it.

Items and rounds are never reused as a whole; their inner nodes are. So a map
whose one item changed reruns only that item's nodes, and a repeat replays
its earlier rounds from records while their carried values are unchanged.

## Running

```python
from llm_kit.graph.run import Runner

runner = Runner.for_project(project_root, "afterplay.beta", item="night-petition")
result = runner.run(inputs={"max_rounds": 3}, target=["brief"], force=())
plan = runner.plan(inputs=None, target=["brief"])          # dry run, writes nothing
runner.seed("plot", output, source={"path": "work.json", "blob": "<git sha>"})
```

`Runner(graph, registry, store, *, item, project_root, services=None,
max_parallel=8)` is the direct constructor (tests build a registry from
`OpRegistry` and a store from `RunStore(dir)`). The constructor validates the
graph (`validate.validate_graph`) and raises `GraphError` listing every
problem.

Run steps:

1. Inputs: the given inputs, then for each input not given, the value recorded
   by the newest run or seed of this item for the same graph id (`inputs_from`),
   then the port default. A missing required input or an unknown name raises.
   Each value is checked against its port schema.
2. Nodes run as a DAG (`pipeline.execution.scheduler`): a node starts when
   the sibling nodes it reads are complete. A failed node gates the nodes that
   read it; unrelated nodes keep running. With `target`, only the named
   top-level nodes and the nodes they read run, and `outputs` is `null`.
3. Each node: `when` false -> `skipped`; a fresh record -> `reused`; else it
   runs by kind:
   - `op`: inputs are evaluated, defaults added and checked against the op's
     port schemas; parameters are resolved; `fn(OpContext, **inputs)` runs;
     `out` is checked against the op's output schema (or the node's
     `output_schema_param` schema). The op must return every declared
     attribute and no other. The call runs under the runtime services
     (`llm-kit-graph-services.md`): a concurrency slot of its kind, and for a
     `paid` op spend admission before the call and settlement after it. A
     `paid` op fails unless the runner has the spend service
     (`services["spend"]`). A commit adapter is refused in an op node.
   - `graph`: the subgraph runs with its bound inputs (defaults added,
     checked); `out` is its outputs, checked against its output ports.
   - `map`: `over` must be a list; `key` must give a unique string or integer
     per item; items run in parallel; `out` is the list of item results in
     `over` order. One failed item fails the map; the other items finish.
   - `repeat`: `max` (an integer of at least 1) rounds at most. After each
     round, `until` (a boolean) is evaluated with `round`, the carried values
     before the update and `index`; then every carried value updates with its
     `next`; then the loop stops if `until` was true. Every round's result is
     in `rounds`; `carry` holds the values after the last update.
   - `human`: a durable wait (`llm-kit-graph-services.md`). With no answer
     the node stops the run (`stopped`, reason `waiting`; see rule 4 for what
     keeps running); with a recorded
     answer `out` is the answer. Without the wait service (`runs.state`) it
     stops the run with reason `unsupported`.
   - `commit`: the bundle is checked against the contract (a failure fails
     the node with code `contract`), then the commit adapter runs.
4. An op (or a service) may raise `run.NodeStopped(reason, message, status=, detail=)`
   to stop the run cleanly: the node is `stopped` (or `refused`), `detail` is
   added to the run's `stop`, no new node starts anywhere, nodes
   already running finish, and the rest are `pending`. The reason `waiting`
   (a `human` node with no answer) is the exception: the run drains. Only the
   nodes that read the waiting node, directly or through other nodes, are
   `pending`; every node that does not depend on it, in any subgraph, map or
   repeat, still starts and runs to completion, and then the run stops
   (status `stopped`, reason `waiting`). One run finishes the independent work;
   after the answer, `resume` reuses it. Every other reason (`halted`,
   `unsupported`, a spend refusal) stops new nodes at once, as described.
   `llm_kit.graph.ops.OpHalted`
   (a usage limit) stops the run the same way with reason `halted` (or fails
   the node when `services.pause_on_halt` is false); `OpError`
   and `OpFailed` fail the node with their `code`. For both, the receipt and
   the attributes the op made before the error (`sent`, raw `text`) are kept in
   the record. Any other exception fails the node.

The `OpContext` an op receives has `store` set to the item's `RunStore`
(`put_blob`, `put_media`, `put_value`, `media_path`) and `services` set to the
runner's services mapping.

### Seeding (Q39)

`Runner.seed(node_path, output, source, *, valid=True, inputs=None)` writes
one `seed` run record with one `seeded` node record. `node_path` is a
top-level node id. `output` is the node's attributes: `out` is required
(`rounds` and `carry` for a repeat); other declared attributes (an op's `sent`
and `text`) may be left out and are stored as null. With `valid=True` the
value is checked like a run result (a `graph` node's outputs against the
subgraph's output ports, an op's `out` against its output schema, a human
answer against its schema); `valid=False` skips the check. `source` is any JSON object (for example the source file and
its git blob sha). The seed's reads are computed now, from the graph inputs
(`inputs`, else the recorded item inputs) and the newest record of each node
it reads, so seed in dependency order; a node it reads with no record raises.
A seed with `valid=False` (an adopted value that fails today's contract) is
shown by `status` but never reused.

### Dry run

`Runner.plan(...)` (CLI `run --dry-run`) evaluates the run without calling an
op and without writing. Each node gets one `PlanEntry(path, kind, op, paid,
action, reason, run)`: action `reuse` (with the record's run id), `skip`,
`run` (reason `no record`, `definition changed`, `inputs changed`, `forced`,
`seed invalid`), `unknown` (it reads a value that a node due to run would
make), or `error` (evaluating the node failed; the reason is the message).
A composite node due to run is entered when its inputs are known, so its
fresh parts show as `reuse`; the ops under an unknown composite are listed
with action `unknown`, so paid ops are visible before any call.

## Verbs

```text
llm-kit graph run GRAPH --item ID [--project DIR] [--target NODE]... [--input NAME=JSON|@FILE]...
                        [--force PATH]... [--dry-run] [--json]
llm-kit graph status GRAPH --item ID [--project DIR] [--input NAME=JSON|@FILE]... [--runs N] [--all] [--json]
```

GRAPH is a graph name from the config or a graph file path. `--input` takes a
JSON value or `@path` to a JSON file. `run` prints one line per top-level node
(`<status> <path>`), then `run <id>: <status>` and the record path. Exit codes:
0 done, 1 a node failed or the graph or config is invalid, 2 usage, 3 the run
stopped. `--dry-run` prints the plan (every path) and exits 0.

`status` prints the newest runs (`--runs`, default 5) and, for each top-level
node (every path with `--all`), the newest record (`status`, run id, `invalid`
for an invalid seed) and the plan action for a run now. Exit 0, or 1 for an
invalid graph or config.

## Python surface for other units

- `records.RunStore(dir, *, blobs=None, media=None)`, `RunStore.for_item(config, item)`,
  `run_ids()`, `load_run(run_id)`, `latest_records()`, `resolve(ref)`,
  `put_value/get_value/put_blob/get_blob/put_media/media_path` (from
  `media.ContentStore`).
- `records.list_items(config)`: item ids that have a run directory.
- `freshness.node_reads(node)`, `freshness.reads_document(refs, scope)`,
  `freshness.input_hash(reads)`, `freshness.ReuseIndex`.
