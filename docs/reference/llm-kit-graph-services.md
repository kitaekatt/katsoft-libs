# llm_kit.graph runtime services

The runtime services control how a graph run calls ops: concurrency, retries,
spend, human waits, halts and commits (decisions Q13, Q29, Q30). The consumer
sets them in its project config. Graph files never set them.

Modules: `llm_kit/graph/services.py` (config, concurrency, the op call
wrapper), `spend.py` (spend admission and settlement), `human.py` (durable
waits), `commit.py` (commit contract check). Verbs: `llm-kit graph answer`
(`commands/answer.py`) and `llm-kit graph resume` (`commands/resume.py`). The
run record format is in `llm-kit-graph-runtime.md`.

## Config: `services`

```yaml
services:
  concurrency: {model-text: 4, model-image: 3, heavy: 1, sound: 2, tags: 1}
  retries: none
  spend:
    ledger_env: CONTENT_PIPELINE_SPEND_LEDGER   # default; the env var that names the ledger file
    guards: [afterplay.beta.moment_caps]        # consumer cap guards, in order
    models: {gpt-6-luna: 0, deepseek-v4.1-flash: 0.01}   # USD per llm.model call, by catalogue row id
  pause_on_halt: true
runs:
  state: "{git_common_dir}/beta-graph/state"    # the durable-wait store; required for human nodes
```

| Key | Value | Effect |
|---|---|---|
| `concurrency` | kind -> integer >= 1 | Slots per kind in one process. An op holds one slot of its kind for its call. A kind that an op uses and that is not in the map fails the node. With no `concurrency` key, there are no limits. |
| `retries` | `none` | The only policy in this build. A failed op fails its node. Run the graph again. |
| `spend.ledger_env` | env var name | The variable that holds the path of the campaign ledger file (`spend_ledger.create_ledger`). Unset: no ledger, so every call with an estimate above 0 is refused. |
| `spend.guards` | list of `<namespace>.<name>` | Consumer cap guards. They run after the ledger, in this order. |
| `spend.models` | catalogue row id -> USD >= 0 | The estimate of one `llm.model` call on that row of `.llm-kit/models.yaml`. `0` declares the row unmetered (a subscription harness, a local model). A row that is not in the map has no estimate. |
| `pause_on_halt` | bool, default true | true: an `OpHalted` (a usage limit) stops the run, and `resume` continues it. false: the halt fails the node. |

The config loader checks the section (`services.check_services`); an unknown
key or a bad value is an error. The personal layer replaces keys one level
deep, as for every section.

`Runner.for_project` builds the services from the config when the caller gives
none (`services.build_services(config, item=, store=)`). The mapping has the
keys `concurrency` (`Limits`), `spend` (`SpendService`), `waits`
(`WaitService`) and `pause_on_halt`. Each op also gets the mapping as
`OpContext.services`. Load the op registry before you build the services: the
guards are found in the namespaces that the registry imported.

## Concurrency

The kind of a call is the op's `limit`. An op that declares a `limit`
parameter lets a node set the kind. `llm.model` has one (default
`model-text`):

```yaml
picture:
  op: llm.model
  model: beta.picture
  mode: workspace
  limit: model-image
```

Slots are per process (`threading` semaphores). Composite nodes hold no slot,
so nested maps cannot deadlock.

## Spend

Every op with `paid=True` is admitted before its call and settled after it.
The ledger is `llm_kit.pipeline.runtime.spend_ledger`: one SQLite file, one cap
fixed when the file is created, safe across processes.

1. Estimate. The op's `estimate(ctx, **inputs) -> USD`. For `llm.model`, the
   `spend.models` entry of the catalogue row that the node's selection key
   resolves to. No estimate: the node is `refused`, reason `no_estimate`, and
   nothing is called.
2. Admit, when the estimate is above 0:
   1. The ledger reserves the estimate (scope `<item>/<graph id>`, identifier
      `<run id>/<node path>`). No ledger: `refused`, reason `no_ledger`. The
      cap or a halted ledger refuses: `refused`, reason `spend_cap`.
   2. Each guard admits, in config order. A guard refuses by raising
      `SpendRefused`; the guards before it and the ledger reservation are
      released, and the node is `refused` (reason `spend_cap`, `refused_by`
      names the guard).
   An estimate of 0 makes no reservation (state `free`).
3. Call the op.
4. Settle:
   - The op returned: the cost is the receipt's `cost.usd`; a receipt cost
     class `local` or `subscription` is 0; else the cost is unknown and the
     ledger holds the reservation as `unknown` at the estimate.
   - The op raised: the same rule, from the error's receipt. An `OpError`
     with no receipt and a code that starts with `refused` (the
     pre-dispatch refusals of `llm.model`) sent nothing, so the admission is
     released.
   - A cost above the reservation is `overbilled`: the ledger halts itself and
     the run stops (reason `spend_overbilled`). The node keeps its result.
   - A cost above 0 on a call estimated at 0 is `unreserved`: the runner
     halts the ledger and the run stops (reason `spend_unreserved`). Correct
     the estimate, then resume the ledger (`SpendLedger.resume`).
   - Each guard gets `settle(token, charge, cost_usd)` (`None` = unknown) or
     `release(token, charge)`.

A refusal stops the run: no new node starts, nodes that run finish, and the
rest are `pending`. `run` and `resume` print the stop, the top-level nodes
done, the nodes remaining and the ledger state. `run --dry-run` prints each
paid op due to run with its estimate (`[paid $0.005000]`) and calls nothing.

The node record's `spend`:

```json
{"estimate_usd": 0.6, "estimate_source": "op afterplay.beta.sound", "model": null,
 "reservation": "<ledger row id>", "guards": ["afterplay.beta.moment_caps"],
 "state": "settled", "cost_usd": 0.5}
```

`state`: `free`, `settled`, `unknown`, `released`, `overbilled` or
`unreserved`.

### Cap guards

A guard is declared in a configured op namespace:

```python
from llm_kit.graph.spend import SpendRefused, cap_guard

@cap_guard("afterplay.beta.moment_caps")
def moment_caps(setup):          # GuardSetup: project_root, item, config, store
    return MomentCaps(setup)     # an object with admit, settle and release
```

- `admit(charge) -> token`. `charge` (`spend.Charge`) has `run_id`, `item`,
  `graph_id`, `node_path`, `op`, `amount_usd` and `model` (the catalogue row
  of an `llm.model` call). Raise `SpendRefused(message)` to refuse.
- `settle(token, charge, cost_usd)` and `release(token, charge)`.
- Earlier spend of the item is in the `spend` of the node records:
  `setup.store.runs()`.

## Human nodes (durable waits)

A `human` node waits for an answer that matches its schema. The wait store is
an `llm_kit.pipeline.execution.store.ExecutionStore` at
`<runs.state>/waits.sqlite`. Each work item is one store run. One wait is one
unit, `<graph id>/<node path>@<key>#<n>`; `key` digests the node's
definition hash and input hash, so the same question with the same inputs is
the same wait.

1. A run reaches the node and no answer is recorded. The runner claims the
   unit (fencing token), requests an interrupt (kind = the node `kind`, schema
   = the node `schema`, payload = graph, node, run, item and a reference to the
   payload value) and stops the run (node `stopped`, reason `waiting`; the
   stop has `wait`).
2. A person answers:

   ```sh
   llm-kit graph answer GRAPH --item ID                       # list the open waits
   llm-kit graph answer GRAPH --item ID --node PATH --value '{"ok": true}'   # or --value @answer.json
   ```

   The value must match the node schema, or nothing is recorded (exit 1).
   Python: `human.answer_wait(config, item, node, value)` and
   `human.open_waits(config, item)`.
3. `llm-kit graph resume GRAPH --item ID` runs the graph again. The finished
   nodes are reused. The human node claims the unit (the next fencing token),
   accepts it with the answer, and its `out` is the answer. The node record
   keeps `wait`: `{"unit", "interrupt", "fencing_token", "answered"}`. Later
   runs reuse the node record.

`--force PATH` on an answered human node asks again (unit `#n+1`). Two
processes that race for one unit get the store's claim error. The interrupt
contract comes from `katsoft_contracts` (or `bootstrap_lib` in the fleet
mode); without it, a human node fails with `InterruptSupportError`.

## Resume

```sh
llm-kit graph resume GRAPH --item ID [--run RUN] [--project DIR] [--json]
```

`resume` continues the newest stopped run of GRAPH (or `--run`). It runs the
graph again with that run's inputs and target. Finished nodes are reused. A
`--force` path of the stopped run stays forced until a run finishes it. The new
record has `resumed_from`. `resume` refuses a run that is not `stopped`, and a
run whose human node still waits. Exit codes are those of `run`: 0 done, 1
failed or refused, 2 usage, 3 stopped again.

## Commit nodes

Only a `commit` node has a side effect (Q13, Q13'). It must be at the top
level of an outer graph. The runner:

1. evaluates the bundle;
2. checks it against the contract schema (`commit.contract_errors`). A
   failure fails the node with code `contract`, and the adapter is not called;
3. calls the adapter: an op with `commit=True` and one input, `bundle`, under
   the same services as any op (a paid adapter is admitted and settled).

An op node that names a commit adapter is refused by `validate` and again by
the runner. A commit node record is reused like any other, so an unchanged
bundle is not committed twice. Use `--force` to commit again.
