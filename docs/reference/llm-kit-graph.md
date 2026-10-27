# llm_kit.graph: graph files, project config, hashes and validation

`llm_kit.graph` is an opt-in package for data-defined generation graphs. Install the `graph` extra (`katsoft-llm-kit[graph]`, PyYAML). Nothing in llm_kit imports the package except the `llm-kit graph` CLI group. `import llm_kit.graph` imports no module; each public name loads on first use from the module that defines it (`MODULES` in `llm_kit/graph/__init__.py`, each module's `__all__`).

This page covers the file format, the project config, definition hashes and `llm-kit graph validate`. The expression language is in [llm-kit-graph-expr.md](llm-kit-graph-expr.md).

## Graph files

A graph file is YAML with JSON values only. Duplicate keys, non-string keys, dates, binary values and non-finite numbers are refused.

```yaml
id: afterplay.beta.story-structure      # dotted name
outer: false                            # true only for an outer graph (commit nodes)
description: ""                         # optional
inputs:                                 # name -> port
  brief: {$ref: ../contracts/brief.v1.schema.json}
  guidance: {schema: {type: string}, default: ""}
outputs:                                # name -> port
  plot: {$ref: ../contracts/plot.v2.schema.json}
nodes:                                  # node id -> node (at least one)
  ...
outputs_from:                           # output name -> expression; exactly the outputs
  plot: keep.out.plot
```

Names (node ids, ports, carried values, body outputs) match `^[A-Za-z][A-Za-z0-9_]*$`. These names are reserved: `inputs carry round item index`, the keywords `and or not true false null` and the functions `len max min any all exists count`.

### Ports

A port is a JSON Schema, or the long form `{schema, default, description}`. A schema is in the `llm_kit.schema.json_schema` subset (no `pattern`, `oneOf`, `allOf`, `format`, `uniqueItems`). A default must match its schema. A port with a default may be left unbound.

A `$ref` that does not start with `#` names a whole JSON file, relative to the file that holds it. The loader bundles each file into the root `$defs` as `file:<path relative to the root's directory>` (a file's own `$defs` become `file:<path>#<name>`) and rewrites the reference. A reference with a fragment (`x.json#/$defs/a`) is refused.

### Bindings

A value position holds an expression (a YAML string) or a literal scalar (`3`, `true`, `null`). A string is always an expression; write a string literal as an expression string: `"'text'"`.

YAML gotcha: inside a flow mapping (`{...}`), quote an expression that contains `??`, `,` or `: `. For example `{init: "baseline.out.score ?? null", next: "max(a, b)"}`.

### Node kinds

Each node has exactly one kind key. Every kind accepts `when` (an expression; a false `when` skips the node, and its `out` is null).

| Kind | Keys | Attributes |
|---|---|---|
| `op` | `op` (dotted op name), `in` (input -> expression), op parameters | `out`, plus what the op declares (`llm.model`: `sent`, `text`) |
| `graph` | `graph` (relative path to a graph file), `in` | `out` (mapping of the subgraph outputs) |
| `map` | `map: {over, key, graph + in | body}` | `out` (list, one entry per item, in `over` order) |
| `repeat` | `repeat: {max, until, carry, graph + in | body}` | `rounds` (list of every round's outputs), `carry` (final carried values) |
| `human` | `human: {kind, schema, payload}` | `out` (the answer) |
| `commit` | `commit: {contract, bundle, adapter}` | `out` (the adapter's result) |

- `map.over` and `map.key` are required. `key` identifies an item across runs.
- `repeat.max` is required: an integer of at least 1, or an expression. `until` is checked after each round, before the carried values update. `carry` is `name: {init, next}`.
- A map or repeat runs a subgraph file (`graph` + `in`) or an inline `body: {nodes, out}`. An inline body has no `in`: it reads the enclosing scope, including the nodes around it. `out` is `name -> expression`.
- A subgraph file may not be an outer graph. A subgraph file may not include itself.
- `human.schema` is a schema; `payload` is `name -> expression`.
- `commit.contract` is a schema or a path to a schema file. `bundle` is an expression or `name -> expression`. `adapter` names a commit adapter op.

### Scope

| Name | Where | Value |
|---|---|---|
| `inputs.<input>` | everywhere in a file and its inline bodies | the graph input |
| `<node>.<attribute>` | sibling nodes; an inline body also sees the enclosing nodes | the node attribute |
| `item`, `index` | map `key`, map `in`, a map's inline body | the item and its position (from 0) |
| `carry.<name>`, `index` | repeat `in`, `next`, `until`, a repeat's inline body | the carried value; the round number (from 0) |
| `round.<out>` | repeat `next` and `until` | this round's output |

## Operations

An op is an `llm_kit.graph.model.Op`: `name` (dotted; the `llm` namespace belongs to llm-kit), `version`, `fn(ctx: OpContext, **inputs) -> JSON | OpResult`, `inputs` (name -> `Port`), `outputs` (the schema of `out`, or `None`), `params` (name -> `ParamSpec`), `attributes` (always includes `out`), `output_schema_param`, `paid`, `estimate`, `limit` (a concurrency kind) and `commit`. Op schemas are self-contained; resolve relative file references with `loader.resolve_schema`.

An op node's keys other than `op`, `in` and `when` are its parameters. Each must be declared by a `ParamSpec(kind, required, slots)`:

| Kind | Value |
|---|---|
| `literal` | any JSON value |
| `expr` | an expression |
| `exprs` | `name -> expression` |
| `schema` | a schema (file references resolve against the graph file) |
| `file` | a relative path; its bytes count toward the definition hash |
| `template` | a relative path to a prompt template; `slots` names the `exprs` parameter that binds its slots |
| `selection` | a model selection key in `.llm-kit/models.yaml` (with the personal layer) |

`OpRegistry` holds ops by name; a second registration of a name is an error, and an unknown name raises `UnknownOpError`. The registry for a project comes from `llm_kit.graph.ops.registry.load_registry(config)`.

A commit adapter (`commit=True`) takes exactly one input, `bundle`. Only a `commit` node may call it.

## Prompt templates

An optional header for people, then one line that is exactly `---PROMPT---`, then the prompt. A file without the marker is all prompt; two marker lines are refused. Slots are `{{name}}` (`[a-z][a-z0-9_]*`). `loader.prompt_body` and `loader.prompt_slots` read them.

## Project config

`.llm-kit/graphs.yaml` (tracked, required for the CLI) and `.llm-kit/graphs.local.yaml` (personal, optional):

```yaml
version: 1
namespaces: {afterplay.beta: pipeline/moments/beta/graph/ops}   # op namespace -> package directory
graphs: {afterplay.beta: pipeline/moments/beta/graph/beta.graph.yaml}
runs:                    # dir, media, blobs, state; placeholders {item} and {git_common_dir}
  dir: pipeline/moments/beta/work/{item}/graph
  blobs: "{git_common_dir}/beta-graph/blobs"
services: {}             # runtime services; checked by the services module
viewer: {panels: [path.mjs], theme: path.css}
```

Paths are relative to the project root. The tracked file may not hold an absolute path; the personal layer may. Each key of a personal section replaces the same key of the tracked section; values are not merged deeper. The `llm` namespace may not be configured, and no namespace may be a prefix of another. `GraphConfig.run_path(key, item=..., git_common_dir=...)` fills a run path.

## Definition hashes

`hashing.hash_graph(graph, registry) -> GraphHashes(graph, nodes, bodies)`. Every hash is a full SHA-256 (64 hex).

A node's definition hash covers the node mapping as canonical JSON (YAML layout and key order do not count), `name@version` of each op it calls, each resolved `schema` parameter, each `file` parameter's bytes, each `template` parameter's prompt text (a header edit does not count), human schemas, commit contracts and adapters, and the graph hash of each subgraph or inline body it runs. A graph hash covers the graph mapping without its nodes, its resolved ports and every node hash. Values that flow into a node are not part of either hash. Paths in hashes are relative, so the hash does not depend on the checkout location.

## `llm-kit graph validate`

```text
llm-kit graph validate [--project DIR] [GRAPH ...]
```

GRAPH is a name from the config or a graph file path; with none, every configured graph. The command prints one line per problem (`<file>: <location>: <message>`) and exits 1, or prints `ok: <n> graph(s)` and exits 0. Usage errors exit 2. It is usable in a pre-push hook.

The loader refuses structural faults first (unknown keys, names, kinds, schemas, missing files, subgraph cycles). Then `validate.validate_graph(graph, registry=..., project_root=...)` reports every problem:

- each expression parses, and each name resolves in its scope, with an attribute its node kind exposes;
- no cycle among sibling nodes (a reference from inside an inline body counts for the node that holds the body); no node reads its own result;
- ops exist; `in` binds exactly the op inputs (inputs with defaults may be left out); parameters match their declarations; template slots equal the bound slot names; selection keys exist;
- a subgraph's `in` binds exactly its inputs;
- commit nodes only at the top level of an outer graph; commit adapters only in commit nodes; an adapter takes only `bundle`;
- a literal `repeat.max` is an integer of at least 1;
- port schemas are compatible where a binding is a bare reference with a known schema: the consumer is `{}`, or the schemas are equal after `$ref` resolution with annotations ignored, or one `anyOf` branch of either side equals the other side. A literal is checked against the consumer schema. Other expressions are not type-checked.

## Verbs

`llm_kit.cli.graph.VERBS` is the verb table: `validate run status answer resume serve replay`. Verb `<v>` is the module `llm_kit.graph.commands.<v>` with `run(argv) -> int`. A verb whose module is not in the build exits 2 and names the missing module.
