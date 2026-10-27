# llm_kit.graph operations: registry, `llm.*` ops and test support

This page covers how a project registers operations, the three operations llm-kit provides (`llm.model`, `llm.validate`, `llm.subprocess`), prompt filling, and the test adapter for `MockBackend`. The graph file format is in [llm-kit-graph.md](llm-kit-graph.md).

## Register a namespace

A consumer repository keeps its operations in a Python package directory and names it in `.llm-kit/graphs.yaml`:

```yaml
version: 1
namespaces:
  afterplay.beta: pipeline/moments/beta/graph/ops
```

```python
# pipeline/moments/beta/graph/ops/plot.py
from llm_kit.graph.ops import op, OpContext

@op("afterplay.beta.plot_errors", version="1",
    inputs={"answer": {"$ref": "../contracts/plot-answer.v2.schema.json"}},
    outputs={"type": "object", "required": ["ok", "errors", "numbered"]})
def plot_errors(ctx: OpContext, answer: dict) -> dict:
    ...
```

`load_registry(config) -> OpRegistry` (`llm_kit.graph.ops.registry`, used by `llm-kit graph validate`) does this:

1. Registers `llm.model`, `llm.validate` and `llm.subprocess`.
2. For each configured namespace, imports the package directory by path under a synthetic module name. Relative imports inside the package work. Nothing is added to `sys.path`.
3. Imports every public module below it. A module or directory whose name starts with `_` or `.` is private and is not imported by the loader. A sub-package needs an `__init__.py`.
4. Collects the functions that `@op` declared in those modules. An op imported from outside the package is ignored.

The load fails with the namespace and the cause when: the directory is missing or has no `__init__.py`; an import raises; the package defines no operations; an op is not named `<namespace>.<name>`; a name is defined twice. The `llm` namespace belongs to llm-kit. Every load imports the package again, so changed source is picked up. A failed load removes its modules.

Importing runs the consumer's code.

### `@op`

```python
op(name, *, version, inputs=None, outputs=None, params=None, attributes=None,
   output_schema_param=None, paid=False, estimate=None, limit=None, commit=False, description="")
```

* `inputs`: input name -> a JSON Schema, a `{schema, default, description}` mapping or a `Port`. `outputs`: the schema of `out`. A relative file `$ref` resolves against the directory of the file that defines the function. Schemas must be in the `llm_kit.schema` subset.
* `params`: parameter name -> `ParamSpec` (see [llm-kit-graph.md](llm-kit-graph.md)). `attributes`: node attributes besides `out` that the function returns in `OpResult.attributes`.
* `paid`, `estimate`, `limit`, `commit`: read by the runtime services. A commit adapter takes exactly one input, `bundle`.
* The function is `fn(ctx: OpContext, **inputs)`. It returns a JSON value or an `OpResult`. The decorator checks the signature at import time and returns the function unchanged, with the declaration on `fn.__graph_op__`.

### Failures

An operation that ran and did not produce a result raises one of these (`llm_kit.graph.ops`):

| Exception | Meaning |
|---|---|
| `OpFailed` | The call failed. `code` is a short token. Retrying the same input will not help. |
| `OpHalted` | The provider stopped the call for a reason outside this call (usage limit, unreachable endpoint). `halt` is llm-kit's halt document. The run pauses and resumes later. |

Both derive from `OpError` and carry `code`, `receipt` (JSON, or `None`) and `attributes` (what the operation had produced, for example `sent` and the raw `text`).

## `llm.model`

```yaml
write:
  op: llm.model
  model: beta.story-structure.generate.story-structure   # selection key in .llm-kit/models.yaml
  prompt: ../prompts/story-structure.md                  # template the consumer owns
  slots: {shapes: prep.out.shapes, task: prep.out.task}
  suffix: prep.out.declared_inputs
  output_schema: {$ref: ../contracts/plot-answer.v2.schema.json}
```

| Parameter | Kind | Meaning |
|---|---|---|
| `model` | selection | Required. A selection key of the project model file (personal layer included). |
| `prompt`, `slots` | template, exprs | Required template file; `slots` binds its slots. |
| `suffix` | expr | A string appended to the filled prompt as written. |
| `output_schema` | schema | The answer must be a JSON value that matches it. |
| `effort` | literal | An effort from the selection's menu; `null` sends none; absent uses the selection's. |
| `mode` | literal | `completion` (default) or `workspace`. |
| `working_dir`, `expect_files`, `watch_files`, `images` | expr | The workspace and image parameters of `invoke`. |
| `max_tokens`, `timeout_s` | literal | Output cap; wall-clock cap. |

The node renders the prompt and calls `llm_kit.dispatch.invoke.invoke` with a protocol 2 request: the same project file, selection, effort menu, mode checks and receipts as `llm-kit completion complete`. It sends no system text.

Attributes: `out` is the validated object when `output_schema` is given, otherwise the answer text. `sent` is the exact prompt. `text` is the raw answer. `files` is the list of files a workspace call wrote (`path`, `size`, `sha256`), otherwise `null`.

The receipt is the result document (`resolved`, `usage`, `cost`, `params`, `consent`, `files`, `halt`) with `output` replaced by `{text_sha256, text_chars, structured}` and two extra keys, `sent_sha256` and `sent_chars`. The run record keeps `sent` and `text` as blobs; the hashes link them to the receipt.

A completed call is the only success. A `halted` outcome raises `OpHalted`. A refused request raises `OpFailed` with code `refused-<kind>` (`selection`, `effort`, `mode`, `path`, `project-file`, ...). Any other failure raises `OpFailed` with the code from llm-kit (`output-contract-violation` for an answer that fails `output_schema`, `expected-files-missing`, `execution`, `timeout`, ...). A slot error raises `PromptError` before any call. The declaration is `paid=True`, `limit="model-text"`.

Backend factory, in order: `ctx.services["backend_factory"]`, the factory installed with `llm_kit.graph.ops.model.set_backend_factory` (tests), then `llm_kit.dispatch.factory.create_backend` looked up at call time.

## `llm.validate`

```yaml
check:
  op: llm.validate
  in: {value: write.out}
  schema: {$ref: ../contracts/plot.v2.schema.json}
  mode: report
```

`out` is `{ok, errors, numbered}` in both modes. `errors` lists `"<pointer> <keyword>"` (`/` is the root; the pointer names the instance location, for example `/count required`). `numbered` is the same list as `1. ...` lines for a repair prompt. `mode: fail` (default) raises `OpFailed` with code `schema-mismatch` when the value does not match; `mode: report` returns the result.

## `llm.subprocess`

```yaml
encode:
  op: llm.subprocess
  in: {payload: bundle.out}
  argv: [node, pipeline/moments/beta/encode/stdio.mjs]
  timeout_s: 120
```

The command receives the `payload` input (default `null`) as one UTF-8 JSON document on stdin. It must print exactly one JSON value on stdout and exit with status 0. `out` is that value.

| Parameter | Default | Meaning |
|---|---|---|
| `argv` | required | List of strings. No shell. |
| `cwd` | project root | A directory under the project root. |
| `env_allow` | `PATH HOME LANG LC_ALL TMPDIR` | Environment variables passed through if set. |
| `env` | none | Extra variables (name -> string). |
| `timeout_s` | 300 | Wall-clock cap for the whole call. |
| `max_output_bytes` | 64 MiB | Stdout cap. |
| `output_schema` | none | The stdout value must match it. |

The child runs in a new session. A timeout, an oversize output and the end of the call each kill the whole process group, so grandchildren do not outlive the node. The receipt holds `argv`, `cwd`, `env_names` (never values), `exit_code`, `wall_ms`, `stdout_bytes`, `stdout_sha256`, `stderr` (the last 64 KiB) and `stderr_truncated`. Codes: `spawn-failed`, `timeout`, `output-too-large`, `exit-status`, `invalid-json` (empty, several values, `NaN`, duplicate keys, not UTF-8), `schema-mismatch`, `unsupported-platform`. A bad parameter raises `ValueError`. POSIX only. The definition hash covers the node mapping and the op version, not the bytes of the script the command runs.

## Prompt templates

`llm_kit.graph.prompt.render_prompt(text, slots, suffix=None)` and `render_prompt_file(path, slots, suffix=None)`. The template rules are in [llm-kit-graph.md](llm-kit-graph.md) (header, `---PROMPT---`, `{{slot}}`). Filling is strict both ways: a slot without a value and a value without a slot are both `PromptError`, listed together. A value must be a string. Values are inserted in one pass, so a value that contains `{{x}}` is sent as written. The suffix is appended exactly as written: whoever produces it owns its newlines.

## Test support: `llm_kit.graph.testing`

`invoke` needs a backend factory that returns a `BackendSelection` (endpoint, model, capabilities, and a backend that speaks `llm_kit.completion` types). The pipeline `MockBackend` is only a `complete` that returns `pipeline.runtime.platform.LLMResponse` and ignores output contracts. `MockBackendFactory` adapts it:

* It resolves the selection with the real `create_backend` (so the project model file, endpoint registry, effort menu and capabilities are real) and replaces the transport with a `MockCompletionBackend`.
* The adapter follows the transport's order: refuse an unsupported contract; append the schema instruction to the system text; take the next scripted answer; convert it to `completion.types.LLMResponse`; judge it with `finalize_contract` (a valid answer gets `structured`; an invalid one raises `OutputContractViolation`, as the real transport does).
* A scripted exception is raised and classified by `MockBackend.classify_halt` (a `halt_kind` attribute or a `PipelineHaltError` produces a `halted` outcome).
* Workspace mode is not mocked. The adapter raises `NotImplementedError` for a call with a working directory. Replace the operation that builds files.

```python
from llm_kit.graph.testing import make_project, mock_backend, op_context

project = make_project(tmp_path, ["draft.write"])          # root, models.yaml, endpoint registry
with project.environment(), mock_backend(['{"title": "x"}'], cost_usd=0.01) as backend:
    result = llm_model(op_context(project.root, params={...}))   # or run a graph
assert backend.calls[0]["user"] == result.attributes["sent"]
```

* `MockBackendFactory(responses=None, keyed_responses=None, cost_usd=None)`: `responses` is a queue of text, dict, `platform.LLMResponse` or exception entries; `keyed_responses` maps a prompt substring to an entry. `calls` records `system`, `user`, `model`, `options`. `cost_usd` reports a provider cost on every answer.
* `mock_backend(...)`: context manager that installs the factory for every `llm.model` call.
* `make_project(directory, selections)`: `selections` is a list of keys or a mapping key -> effort. Returns a `MockProject` (`root`, `endpoints`, `env`, `environment()`).
* `op_context(project_root, params=..., services=..., ...)`: an `OpContext` for calling an operation function directly.
