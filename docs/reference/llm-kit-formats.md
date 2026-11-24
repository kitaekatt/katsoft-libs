# llm-kit shared file formats

Some llm-kit files have readers in more than one language. Each of these formats has one format id, one JSON Schema in the package `llm_kit.schema.formats`, and one shared cases file under `tests/formats/`. Every reader, in every language, must pass every case of its format. This page is the contract. When this page, a schema and a case disagree, the case is correct; fix the page or the schema.

| Format id | Schema (in `llm_kit/schema/formats/`) | Python reader | Shared cases |
|---|---|---|---|
| `llm-kit.project-models/v1` | `project-models.v1.schema.json` | `llm_kit.registry.project_models`, `llm_kit.registry.request_settings` | `tests/formats/project-models/cases.json`, `tests/formats/project-models/requests.json` |
| `llm-kit.project-models-local/v1` | `project-models-local.v1.schema.json` | `llm_kit.registry.project_models` | `tests/formats/project-models/cases.json` |
| `llm-kit.prompt-file/v1` | `prompt-file.v1.schema.json` | `llm_kit.graph.prompt` | `tests/formats/prompt-file/cases.json` |
| `llm-kit.run-record/v1` | `run-record.v1.schema.json` | `llm_kit.runs.record` | `tests/formats/run-record/cases.json` (`records`) |
| `llm-kit.run-import/v1` | `run-import.v1.schema.json` | `llm_kit.runs.store` | `tests/formats/run-record/cases.json` (`import_lines`) |
| `llm-kit.run-store/v1` | `run-store.v1.schema.json` | `llm_kit.runs.store` | `tests/formats/run-record/cases.json` (`stores`) |
| `llm-kit.compare-case/v1` | `compare-case.v1.schema.json` | `llm_kit.runs.compare` | `tests/formats/compare-case/cases.json` |
| `llm-kit.fallback-chain/v1` | `fallback-chain.v1.schema.json` | `llm_kit.registry.fallback_chains` | `tests/formats/fallback-chain/cases.json` (`documents`, `mappings`) |
| `llm-kit.call-record/v1` | `call-record.v1.schema.json` | none (the TypeScript `@katsoft/llm-kit-send` 0.2.0 writes it) | `tests/formats/call-record/cases.json` (`records`) |
| `llm-kit.call-record/v2` | `call-record.v2.schema.json` | none (the TypeScript `@katsoft/llm-kit-send` 0.3.0 and later writes it) | `tests/formats/call-record/cases.v2.json` (`records`) |
| `llm-kit.reply-entry/v1` | `reply-entry.v1.schema.json` | `llm_kit.registry.reply_cache` | `tests/formats/reply-entry/cases.json` (`entries`, `reader_rules`) |
| `llm-kit.reply-key/v1` | none (a key, not a file) | `llm_kit.registry.reply_cache` | `tests/formats/reply-key/cases.json` (`parts`, `requests`, `same`, `different`) |
| `llm-kit.consent/v2` | `consent.v2.schema.json` | `llm_kit.registry.consent` | `tests/formats/consent/cases.json` |
| `llm-kit.graph-catalog/v1` | `graph-catalog.v1.schema.json` | `llm_kit.graph.render.page` (`render_page` validates every catalog it draws; `llm_kit.graph.render.catalog` writes it) | `tests/formats/graph-catalog/cases.json` (`documents`) |
| `llm-kit.graph-catalog/v2` | `graph-catalog.v2.schema.json` | `llm_kit.graph.render.page` (`render_page`; the browser script draws what it embeds), written by `llm_kit.graph.render.labels` | `tests/formats/graph-catalog/cases.v2.json` (`documents`) |
| `llm-kit.graph-labels/v1` | `graph-labels.v1.schema.json` | `llm_kit.graph.render.labels` | `tests/formats/graph-labels/cases.json` (`documents`) |

`llm_kit.schema.formats.load_schema(format_id)` returns a schema. `llm_kit.schema.formats.SCHEMA_FILES` maps each format id to its file. The file's `$id` is the format id.

## Rules for every reader

- The schemas use `llm-scripting-kit.json-schema-subset/v1`, so `llm_kit.schema.json_schema` validates them with no third-party package. A JSON Schema 2020-12 validator also reads them.
- The subset has no `pattern`, `propertyNames` or `uniqueItems`. A rule that the subset cannot state is a reader rule. Each schema's `description` lists its reader rules, and the shared cases fix them.
- A case input is `lines` (join the lines with LF and add one final LF), `text` (encode as UTF-8) or `base64` (the exact bytes). Compare results as JSON values. Object key order does not count.
- Each cases file starts with a `description` that says how to run its cases.
- Python runs the cases in `tests/llm-kit/test_formats.py`. A reader in another language reads the same files by relative path.

## llm-kit.project-models/v1

The tracked project model file, by default `.llm-kit/models.yaml`. The module docstring of `llm_kit.registry.project_models` explains what the fields mean. This section gives the reading rules.

### Bytes and YAML

1. Decode the bytes as UTF-8. Refuse bytes that are not UTF-8: a malformed sequence, an overlong encoding, an encoded surrogate, UTF-16.
2. Skip one leading byte order mark (U+FEFF).
3. CRLF, lone CR and LF are line breaks.
4. Read one YAML 1.1 document as the PyYAML safe loader resolves it:
   - `yes`, `no`, `on`, `off`, `true` and `false` are booleans in lower case, with a capital first letter, or in all capitals. `y` and `n` are strings.
   - `~`, `null`, `Null` and `NULL` are null.
   - `0x10000` is 65536. `0100` is octal 64. `65_536` is 65536. `1:30` is base 60 (90). `019` and `0o17` are strings.
   - A float needs a dot, and its exponent needs a sign: `1.0e+3` is 1000.0, but `1.0e3` and `1e3` are strings. `.5` is 0.5.
   - `2026-11-13` is a date. `!!str 2026-11-13` is a string.
   - Text that resolves to a type, or that an explicit tag names, but cannot be built is refused at `""`: an impossible date or time (`2026-02-30`, `2026-01-01 25:00:00`), a timestamp offset of a day or more, an integer with no digit (`0x_`), `!!bool foo`, `!!int abc`, `!!int ''`, `!!float abc`, `!!timestamp foo`.
   - A decimal integer of more than 4300 digits is refused at `""` (Python's default limit, held on every interpreter). A shorter one outside the exact range is refused at its path (rule 7).
5. Refuse a duplicate key at any depth, a merge key (`<<`), `!!omap`, `!!pairs`, a control character that YAML does not allow (such as U+0007), an empty document, more than one document, and a value that nests more than 100 levels of lists and mappings. The top-level mapping is level 1; an alias counts as a copy of its anchored value; where a container that holds itself repeats, it adds no level. A reader does not fail with a stack overflow on a deeper file.
6. Anchors and aliases are allowed. An alias is a copy of its anchored value. A value that holds itself through an alias is refused at the path of the alias.
7. Every value must be JSON data. Refuse a date, a timestamp, binary data, a set, a key that is not a string, NaN, infinity (also a float that overflows, such as `1.0e+400`) and an integer outside -(2**53 - 1) to 2**53 - 1. A YAML decimal stays a decimal: `65536.0` is not an integer, although a JSON reader can see 65536.

### Structure

| Key | Rule |
|---|---|
| `version` | Required. The integer 1. |
| `endpoints` | Optional mapping. A key is the endpoint of a catalog row (text with no leading or trailing whitespace). A value is `{effort_style, base_url, key_env}`. `effort_style` is required, one of `top-level`, `ninfer`, `chat_template_kwargs`, `reasoning-object`, `unsupported`; see "Request settings". `base_url` and `key_env` are optional and come together: the endpoint's OpenAI-compatible connection (see "Connections"). |
| endpoint `base_url` | `http://` or `https://`, then a host, then printable ASCII (U+0021 to U+007E) with no space. |
| endpoint `key_env` | Required with `base_url` and refused without it. The name of the environment variable that holds the key (an ASCII letter or `_`, then ASCII letters, digits and `_`), or null for a keyless endpoint. The file never holds a key, and a machine sends the key only after its owner approves the connection (see "Connections"). |
| `catalog` | Optional list of rows, default empty. Order is kept. |
| row `id`, `label`, `endpoint`, `model` | Required text: not empty, no leading or trailing whitespace. Ids are unique. `model` is the consumer's name for the model. |
| row `wire_model` | Optional text, as `model` (null is refused; omit the key). The model id the endpoint is sent, when it differs from `model`: for example the vendor's own id beside the name the consumer routes on. Omitted: the wire model is `model`. Calls send the wire model, binding reads the effort menu for it, a price is looked up by it and a run record names it (`resolved.model`). |
| row `efforts` | Optional list of non-empty text with no repeats. Omitted: the row does not narrow the effort menu. `[]`: the row has no effort. |
| row `max_output_tokens` | Optional integer from 1 to 2**53 - 1. The row's default output cap. |
| row `traits` | Optional list of non-empty text with no repeats: the consumer's labels for the row. No reader puts a trait in a request. |
| row `request_defaults` | Optional mapping: `temperature`, `top_p`, `reasoning_budget` (typed settings, see "Request settings") and `extras`, a mapping of JSON values. A typed default may not also be in `extras` (`temperature`, `top_p`; `reasoning` beside `reasoning_budget`). |
| `requests` | Optional mapping of request profiles. A key is text with no leading or trailing whitespace. A profile holds any of `temperature`, `top_p`, `max_output_tokens`, `reasoning_budget` and `extras`; its `extras` hold no reserved name (see "Request settings"). |
| `selections` | Optional mapping, default empty. A key is text with no leading or trailing whitespace. A value is `{model, effort, request}`. |
| selection `model` | Required. A catalog id. |
| selection `effort` | Optional. Omitted and null mean the same: no effort. When the row declares `efforts`, the effort is one of them; when the row declares `[]`, the effort is null. |
| selection `request` | Optional. A key of `requests` (null is refused; omit the key for no profile). A selection with an effort names no request that sets `reasoning_budget`. |

No other keys are allowed at any level. Checks run in this order, so the first problem is the same in every reader: `version`, the structure of `endpoints`, `catalog`, that each `endpoints` key is a row's endpoint, `requests`, `selections`.

### Portable validation and binding

Validation has two parts.

- Portable validation checks the bytes, the YAML, the structure and the references above. It needs only the file. Python: `validate_portable_document(data)`. Every reader does this part.
- Binding needs the machine's endpoint registry. It checks that each `endpoint` resolves, that each effort is in the library effort menu for that endpoint and wire model, and that `extras` has no reserved key (`model`, `messages`, `stream`, `api_key`, `reasoning_effort`, and `reasoning` on a `reasoning-object` endpoint), and that no row's or profile's `extras` widens the Codex sandbox (`add_dirs`, `scratch_dir`, `network` other than `false`, `output_schema`, or any `__llm_kit_`-prefixed key; see "Request settings"). Python: `validate_document`, and every read and write. TypeScript (llm-kit-config 1.4.0 and later): `resolveRequest` refuses the same Codex sandbox extras; the other binding checks need the machine's registry and stay in Python. Both readers run the shared cases in `tests/formats/project-models/tracked-extras.json`. A row that declares no `efforts` accepts any non-empty effort in portable validation; binding checks it against the menu.
- An effort style that the file's `endpoints` declares wins over the registry's style of that endpoint for this project: the menu, the reserved `reasoning` extra and the wire all use it. A model with no measured profile then takes that style's menu (`reasoning-object` lists `none`). Binding refuses a style on a harness endpoint (`endpoints['x'].effort_style`).
- The explicit `none` (reasoning off). The menu of `top-level` and `chat_template_kwargs` is `low`, `medium`, `high`, because not every server takes `none`. A row on one of these styles whose wire model has no measured profile may declare `none` as the first item of its `efforts`: the project states that its provider takes `reasoning_effort: none`, and it is sent as written. No other effort is added, and no value is remapped. A selection or call of a row that does not declare `none` is still refused for it, and a measured menu or a harness menu is never widened. Python tests: `test_project_models.py`, "an explicit `none` the row declares".
- An `endpoint` is the consumer's name; no reader refuses a file because the machine's registry does not know it. Binding loads a row whose endpoint resolves to no registry endpoint, and that the file does not connect (see "Connections"), as unbound: the row keeps the checks that need no endpoint (its declared `efforts`, and the reserved extras other than `reasoning`), and the result names it (Python: `ProjectModels.unbound`, `Validation.unbound`). A call or a plan that resolves to an unbound row refuses at that point and names the row and the endpoint. Case `endpoint-is-opaque-text`.

### Connections

An `endpoints` entry with `base_url` and `key_env` is a connection: it says how to reach the endpoint on a machine that does not know it. Precedence, per endpoint name:

1. The machine wins. When the machine's own configuration knows the name (Python: the fleet and user model config layers, then the endpoint registry; a model config layer in the project tree cannot supply or change a `base_url`, `key_env`, `key_file`, `billing`, `frontdoor` or `endpoint`, see "Model config layers" in the architecture reference), a call uses the machine's connection whole: its base URL, its key, its billing mode. The file's `base_url` and `key_env` are not used on that machine.
2. Otherwise the file's connection binds the rows on that endpoint. A call sends the row's wire model (`wire_model`, else `model`) as written (no alias map), with the key from the variable `key_env` names (Python: the environment, then the `.env` layers), when the machine approves the connection (see "Approval of a connection that sends a key"). The file declares no billing, so a native `usage.cost` is an estimate and the cost class is `metered`.
3. Either way, the file's `effort_style` wins over the machine's style (see above).

A reader that does not call endpoints (the TypeScript package) returns the connection in the normalized document and leaves the precedence and the approval to its caller. Python: `endpoint_connection(document, name)`; protocol 2 calls (and so `llm-kit runs compare`) use it. Cases `endpoint-connection` and `endpoint-connection-keyless`.

#### Approval of a connection that sends a key

The file is repository content: anyone who can change it can name any host and any environment variable. So the file alone never makes a machine send a key. A caller that calls a connection uses one with a `key_env` only when all of these are true. A keyless connection (`key_env` null) sends no key and needs none of them.

1. The base URL names a plain host. The host is the text between `://` and the first `/`, `?` or `#`, in lower case. It must be a DNS name or an IPv4 address, or an IPv6 address in brackets, with an optional `:port`. A URL with user info (`https://a.example@b.example/`), a percent escape or a backslash there is refused, because a client can read it as a host that the text does not show.
2. The scheme is `https`, or `http` to a literal loopback address (`127.0.0.0/8` or `[::1]`). `localhost` is a name that a hosts file can point anywhere, so plain http to it is refused.
3. No endpoint of the machine sends the same key variable to another host. Python reads the endpoint registry entries and the model config endpoints of the machine layers (the shipped defaults too), never a config layer in the project tree. For example, the shipped `openrouter` endpoints send `OPENROUTER_API_KEY` to `openrouter.ai`, so a project file cannot send that key to another host. Key variables compare as the environment reads them: on Windows, where environment names ignore case, `openrouter_api_key` is `OPENROUTER_API_KEY`; elsewhere the name is exact.
4. The machine owner approved exactly this endpoint name, host (with its port when the URL names one) and key variable (compared as in rule 3). The approval is in the user-level consent file (`llm-kit.consent/v2`, `connections`), never in the project tree. Add an approval with `llm-kit consent connection on --endpoint NAME --host HOST --key-env VAR`. `off` removes it, and on Windows also removes an approval of the same variable in another case.

An approval cannot lift rules 1 to 3. A refusal names the endpoint, the host and the key variable. For rule 4 it also gives the exact command. Python raises `ProjectModelsError` with code `unapproved` (path `endpoints['<name>']`). A protocol 2 call refuses with kind `configuration` before it builds a backend or reads the key. Portable validation and binding do not apply these rules: a file with an unapproved connection still reads, validates and binds, and only the call refuses. Python tests: `tests/llm-kit/test_project_connection_approval.py`.

Binding never accepts a file that portable validation refuses. When the file has no binding problem, full validation refuses it at the same path as portable validation.

### Result

Portable validation returns the normalized document: `version` 1, `endpoints` and `requests` when the file has them (sorted by key), `catalog` (each row with only the keys it declares), and `selections` with `effort` always present (null when omitted) and `request` only when given. Aliases are expanded.

### Errors

A refusal has a code and a path. Every refusal of the file content has the code `invalid`. The path names the first problem:

- `""`: the bytes or the YAML (not UTF-8, a YAML error, a value that cannot be built, a duplicate key, a merge key, an empty file, nesting past 100 levels).
- `document`: the top level is not a mapping, or has an unknown key.
- `version`, `catalog`, `selections`.
- `catalog[1]`, `catalog[1].id`, `catalog[1].efforts[0]`, `catalog[0].request_defaults.extras.a.b[2]`.
- `endpoints['api']`, `endpoints['api'].effort_style`, `endpoints['api'].base_url`, `endpoints['api'].key_env`, `requests['p']`, `requests['p'].temperature`, `requests['p'].extras`, `catalog[0].traits[1]`, `catalog[0].request_defaults.extras`.
- `selections['draft.write']`, `selections['draft.write'].model`, `selections['draft.write'].effort`, `selections['draft.write'].request`. A selection key is shown in single quotes. The cases use only keys without quotes, backslashes or control characters; for other keys the quoting is the reader's own.
- `selections`: a key that is not a string.

Python raises `ProjectModelsError` with `code` and `where` (the path).

## llm-kit.project-models-local/v1

The personal layer beside the tracked file, by default `.llm-kit/models.local.yaml`. The bytes and YAML rules are the same as for the tracked file.

- It holds `version` (required, 1) and `selections` (optional). It holds no catalog.
- Each selection is checked as a tracked selection, against the tracked catalog. Python: `validate_portable_local_document(data, tracked_document)`.
- Each selection is checked as a tracked selection, and its `request` against the tracked `requests`.
- Resolution is per key and replaces the whole selection, its `request` included: an override that changes only the model repeats the `request` (Python's `set_local_override` copies it by default). A key in the local layer wins over the same key in the tracked file. A key that only the local layer names is an override with no default. A local entry equal to the tracked entry is no override (an omitted effort equals null; the `request` must be equal too). Python: `resolve_selections(defaults, overrides)` returns, for each key, `effective`, `default` (null for a local-only key) and `source` (`default` or `override`).

## Request settings

A selection describes the whole request, so a client can build it from the file alone and a repeated request is the same request. The shared cases are `tests/formats/project-models/requests.json`. Python: `llm_kit.registry.request_settings.resolve_request(document, key, overrides=, call=)` and `request_for(document, selection, call=)`. TypeScript: `resolveRequest(registry, key, call)` and `chatFields(request, {effortStyle})`.

### Typed settings

| Setting | Rule |
|---|---|
| `temperature` | A number, 0 or more. |
| `top_p` | A number from 0 to 1. |
| `max_output_tokens` | An integer from 1 to 2**53 - 1 (`60.0` is refused). |
| `reasoning_budget` | An integer from 1 to 2**53 - 1: reasoning tokens. Effort `none` turns reasoning off. |

### Layers

1. The row: its `request_defaults` and its own `max_output_tokens`. In the row's `extras`, `temperature`, `top_p` and `max_tokens` are the older spellings of the row's typed defaults: the reader moves them out of `extras` and checks them as settings when the request is resolved (a bad value is refused at `catalog[i].request_defaults.extras.<key>`). The row's typed `max_output_tokens` wins over `extras.max_tokens`.
2. The selection: its `effort` and the profile its `request` names.
3. The call: any of the profile keys, and `effort` (text or null). Only `effort` may be null. Paths of a call problem start with `call` (`call`, `call.temperature`, `call.extras`, `call.effort`). A call effort is held to the row's declared `efforts`.

Per typed setting, the highest layer that sets it wins. `extras` merge per top-level key, row then profile then call; a nested object is replaced whole. Reasoning is one control with two forms, `effort` and `reasoning_budget`. The highest layer that sets either wins; one layer may not set both (the selection's effort and its profile's budget are one layer). A call that names `effort`, null included, decides the reasoning: `effort: null` sends no effort and no budget.

The `extras` of a profile or a call may not hold `model`, `messages`, `stream`, `api_key`, `reasoning_effort`, `reasoning`, `temperature`, `top_p`, `max_tokens`, `max_completion_tokens` or `chat_template_kwargs.reasoning_effort` (path: the `extras` mapping, or its `chat_template_kwargs`).

The tracked file is repository content, so binding also refuses the Codex extras that widen the sandbox in a row's `extras` and in a profile's `extras`: `add_dirs` and `scratch_dir` (further writable roots), and `network` with any value but `false` (path: `catalog[i].request_defaults.extras.<key>` or `requests['p'].extras.<key>`). `network: false` only narrows the sandbox and stays allowed. A key that starts with `__llm_kit_` is refused there too: those names are internal to the library. So is `output_schema` (it names a file Codex reads as the answer schema; a request's output contract sets the schema). The code that calls the backend (`BackendOptions.extras`) and a call set `add_dirs`, `scratch_dir`, `network` and `output_schema` as before (the `__llm_kit_` names are set by the library's own code); the portable check does not refuse any of them. The TypeScript reader's `resolveRequest` (llm-kit-config 1.4.0 and later) refuses the same keys at the same paths, with code `invalid`. It checks the whole file before it looks up the selection key, as `validate_document` does, so a refused file is refused for every key. Both readers report the same first problem: the rows in catalog order, then the profiles in code point order of their names (not the key order of the file), and within one `extras` mapping `output_schema`, then the `__llm_kit_` keys in code point order, then `add_dirs`, `scratch_dir` and `network`. `parseRegistry`, `loadRegistry` and the overlay functions still read such a file, as the portable check does, and a call's own `extras` may set the keys. The shared cases in `tests/formats/project-models/tracked-extras.json` fix the rule for both readers (Python: `tests/llm-kit/test_codex_tracked_extras.py`; TypeScript: `test/tracked-extras.test.mjs`).

### The resolved request

`{row, endpoint, model, wire_model, effort, effort_style, temperature, top_p, max_output_tokens, reasoning_budget, extras, traits}`. Every key is present; `model` is the row's model and `wire_model` the model id its endpoint is sent (the row's `wire_model`, else `model`), which a sender puts in the body; an unset value is null, `extras` is `{}` and `traits` is `[]` when empty. `effort_style` is the style the file's `endpoints` declares for the row's endpoint, else null (the client uses its own: Python uses the endpoint registry). A key no layer names is refused with code `unknown-selection` at `selections['<key>']`.

### The wire (OpenAI-compatible chat)

The body fields of a resolved request, beside `model` and `messages`:

- `max_tokens` (from `max_output_tokens`), `temperature`, `top_p`, when set; then every `extras` key. An extras key that repeats one of these is refused (`extras.<key>`).
- The effort, by style: `top-level` sends `reasoning_effort`; `ninfer` the same with `high` sent as `xhigh`; `chat_template_kwargs` adds `reasoning_effort` to that object and keeps its other members; `reasoning-object` sends `reasoning: {effort}`, or `{enabled: false}` for `none`, and takes only `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. No style, or `unsupported`, refuses an effort (`effort`). Extras that already hold the effort's key refuse it (`extras.reasoning`, `extras.reasoning_effort`, `extras.chat_template_kwargs.reasoning_effort`).
- A reasoning budget needs `reasoning-object` and sends `reasoning: {max_tokens}` (else refused at `reasoning_budget`).
- `traits` are never sent.

TypeScript `chatFields` returns these fields (code `wire` for a refusal). Python protocol 2 (`llm_kit.dispatch.invoke`) sends them for a transport row: its test sends each case and compares the recorded request. A harness row (claude, codex) refuses `temperature`, `top_p` and `reasoning_budget`, which it has no control for.

## llm-kit.prompt-file/v1

A prompt file is an optional header for people, one marker line, and the prompt body. The schema describes the parsed form `{header, body, slots, sha256}`. The text rules are below; the cases fix them.

1. Decoding. Decode the bytes as UTF-8. Refuse bytes that are not UTF-8.
2. Byte order mark. When the text starts with U+FEFF, remove that one character. Another U+FEFF is text.
3. Newlines. Replace each CRLF with LF. Then replace each remaining CR with LF.
4. Marker. Split the text into lines at LF. A marker line is a line that is exactly `---PROMPT---`, with no other characters. Refuse a file with more than one marker line.
5. Parts. With no marker line, `header` is null and `body` is the whole text. With one marker line, `header` is the lines before it joined with LF, and `body` is the lines after it joined with LF. The LF before the marker and the LF after it belong to neither part.
6. Slots. In the body only, scan from left to right for `{{name}}`, where `name` matches `[a-z][a-z0-9_]*`. Matches do not overlap. `slots` lists each name once, in order of first use. Other brace text is literal: `{{ a }}`, `{{A}}`, `{a}`.
7. Hash. `sha256` is the lowercase SHA-256 hex digest of the body encoded as UTF-8.
8. Rendering. The values must name exactly the slots: refuse a slot with no value and a value with no slot. Each value is a string. Replace each slot with its value in one pass; a value is never scanned for slots. Append the suffix, when given, as written. Values and the suffix keep their bytes; the newline rule does not apply to them.

Python: `parse_prompt_file(data)` and `read_prompt_file(path)` return a `PromptFile`. `render_prompt` and `render_prompt_file` fill a template. `llm_kit.graph.loader.prompt_body` and `prompt_parts` apply the same rules to text. The graph definition hash of a template parameter covers the body, so a CRLF or byte order mark copy of a file has the same node hash.

Up to katsoft-llm-kit 1.24.1, the text functions split only on LF and kept a byte order mark, while the file readers changed CRLF and CR to LF. Version 1.25.0 applies rules 2 and 3 in every reader. A file with only LF line ends and no byte order mark reads as before, so its node hashes do not change.

## llm-kit.run-record/v1

One attempt of one case on one model selection. A comparison is the records that share one `group.id`. The store, the policy and the CLI that use this format are in `docs/reference/llm-kit-runs.md`.

### Status and fields

Each status is one branch of the schema (`$defs/started`, `$defs/ok`, `$defs/invalid`, `$defs/failed`, `$defs/refused`). Every field of a branch is required; a field that can be empty holds `null`, `{}` or `[]`.

| Status | Meaning | `output` | `error` |
|---|---|---|---|
| `started` | The attempt began. The record has no result fields: no `ended_at`, `output`, `error`, `scores`, `usage`, `cost` or `timings`. | not present | not present |
| `ok` | An accepted answer. | required | `null` |
| `invalid` | An answer arrived but failed the caller's parse or contract. | required | required |
| `failed` | No usable answer (transport, provider or runner error). | object or `null` | required |
| `refused` | The model or provider declined. | object or `null` | required |

Fields of every record: `format`, `id`, `status`, `started_at`, `flow` (the writer's name for the work), `group` (`id`, `kind` `compare`, `eval` or `single`, `label`), `case` (`id`, `input_sha256`, `input`), `prompt` (`id`, `file`, `sha256`, `slots_sha256`, or `null`), `selection` (`name`, `model`, `effort`), `resolved` (`row`, `endpoint`, `model`, `effort`, `registry_sha256`, or `null`; `model` is the model the call sent, for a project model file row its wire model), `request` (`max_output_tokens`, `extras`, `sent_sha256`), `inputs`, `provenance` (`git_sha`, `git_dirty`, `tool`, `files`, `scorer`), `content` and `labels`. A result adds `ended_at`, `output` (`text`, `parsed`), `error` (`code`, `message`), `scores`, `usage`, `cost` and `timings`.

Text bodies are not in the record. `case.input`, `output.text` and `scores[].note` are blob references `blob:<64 lowercase hex>`, the SHA-256 of the bytes in the store.

### Content classes

`content` is the class of the whole record and of every blob it names. One record has one class.

- `synthetic`: made for testing. It holds no data from or about a real person.
- `internal`: the owner's own material (prompts, documents, cases the team wrote) with no personal data.
- `sensitive`: it can hold personal data, user content or anything else that must stay private.

When a record has parts of different classes, the writer declares the most sensitive class. When the writer is not sure, it declares `sensitive`. A client maps its own data to these classes.

### Reader rules

The schema cannot state these rules. The cases fix them. A refusal has the code `invalid` and the JSON Pointer of the first problem.

1. The record is JSON data: no NaN, no infinity. Integers are inside -(2**53 - 1) to 2**53 - 1.
2. Pick the branch by `status`. An unknown status is refused at `/status`. Then apply the branch. The validator names a missing field and an unknown field at the field's own pointer (`/content`, `/ended_at`).
3. `id` is `YYYYMMDDTHHMMSSffffffZ-` followed by 4 to 32 lowercase hex digits. Ids sort by start time.
4. A time is UTC: `YYYY-MM-DDTHH:MM:SS`, an optional fraction of 1 to 9 digits, then `Z`. It must be a real date. `ended_at` is not before `started_at`; fractions compare as numbers (`.5` is after `.123456`).
5. Every hash is 64 lowercase hex digits. A blob reference is `blob:` and 64 lowercase hex digits.
6. When `case.input` is not null, it is `blob:` followed by `case.input_sha256`.
7. `prompt.file` and each key of `provenance.files` are project-relative POSIX paths: not absolute, no backslash, no `..`, no drive.

### Reuse key (llm-kit.run-key/v1)

`llm_kit.runs.record.reuse_key(record)` hashes every input that can change the answer. Two records with the same key asked the same question of the same model.

- Hashed: `flow`; `case.input_sha256`; `prompt.sha256` and `prompt.slots_sha256` (`null` when `prompt` is null); the model that answered: `{resolved: true, endpoint, model, effort}` from `resolved`, or `{resolved: false, endpoint: null, model, effort}` from `selection` when `resolved` is null; the whole `request`; the whole `inputs`.
- Not hashed: `id`, the times, `status`, `group`, `labels`, `content`, `selection.name`, `resolved.row`, `resolved.registry_sha256`, `provenance` (the scorer too) and every result field.
- Bytes: SHA-256 over `llm-kit.run-key/v1`, one zero byte, the payload `{flow, case, prompt, model, request, inputs}` as JSON with sorted keys, ASCII escapes and no spaces, and one zero byte. The cases give the key of each valid record.
- A writer that adds anything else to the request (a system text, rules, the code that builds a multi-step request) names its SHA-256 in `inputs`. A writer that cannot name the files that change the answer puts the commit there.
- A score depends on its scorer too: a score can be reused only when the reuse key and `provenance.scorer.sha256` are both equal.

## llm-kit.run-import/v1

A JSON Lines file (UTF-8). Each line is one record event and the blobs it brings: `{"format": "llm-kit.run-import/v1", "record": {...}, "blobs": {"blob:<hex>": {"text": "..."}}}`. A blob value is `{"text": ...}` (the bytes are the UTF-8 of the text) or `{"base64": ...}` (strict base64).

1. A final LF is allowed. An empty line elsewhere is refused. A CR before the LF is removed.
2. Each line conforms to the schema. The SHA-256 of each blob's bytes equals its reference (code `blob-hash`).
3. The record is a valid `llm-kit.run-record/v1` record.
4. A refusal names the line (`line 2`). The reader checks the whole file before a store writes anything.

`llm-kit runs show <id> --with-blobs` prints a line of this format.

## llm-kit.run-store/v1

`store.json` at the root of a run store: `{"format": "llm-kit.run-store/v1", "accepts": [...]}`. `accepts` names one to three content classes, each one time (reader rule). The store refuses a record whose class is not in `accepts`.

## llm-kit.compare-case/v1

One line of a comparison case file (JSON Lines, UTF-8): `{"id": "c1", "slots": {"text": "..."}}`. `llm-kit runs compare --cases` reads it. The schema describes one line.

1. A final LF is allowed. An empty line elsewhere is refused. A CR before the LF is removed. A file with no case is refused.
2. Each line conforms to the schema: `id` is non-empty text, `slots` maps each slot name to text, and no other key is allowed.
3. The ids of one file are unique.
4. A refusal has the code `invalid` and names the line (`line 2`). A refusal of the whole file (not UTF-8, no case) names no line.
5. The comparison checks each case against the prompt it fills: a slot with no value and a value with no slot are refused before any call.

Python: `llm_kit.runs.compare.read_cases(path_or_bytes)`.

## llm-kit.fallback-chain/v1

A JSON file that says which hosts serve a request in which order, and the same model's endpoint and input on each host. Readers: Python `llm_kit.registry.fallback_chains` (`read_fallback_chains`, `check_fallback_chains`, `map_input`); TypeScript `@katsoft/llm-kit-send` (`readFallbackChains`, `mapInput`, `runChain`).

```json
{"format": "llm-kit.fallback-chain/v1",
 "chains": {"speech": {"steps": [{"host": "primary", "next_on": ["no_key", "account"]}, {"host": "backup", "next_on": []}]}},
 "models": {"tts-1": {"hosts": {"primary": {"endpoint": "tts-1"},
   "backup": {"endpoint": "vendor/tts-1", "from": "primary", "input": [{"key": "text"}, {"key": "speed", "number": true, "max": 2}]}}}}}
```

1. `chains.<name>.steps` lists hosts in order. `next_on` lists the failure classes that move on to the next step. `no_key` skips a step whose host has no key; a step with no key and no `no_key` stops the chain. Any other class is the caller's (for example `account`: the host refused the account for credits, quota or the key).
2. Host ids match `^[a-z0-9][a-z0-9_.-]*$`, class ids `^[a-z][a-z0-9_]*$`. The hosts of one chain and the classes of one step are unique.
3. `models.<id>.hosts.<host>.endpoint` is the model's endpoint on that host. `from` names the one host whose input the caller gives; `input` maps it. `from` names another host of the same model, and that host has no `from`: mappings do not compose. `input` needs `from`; `from` without `input` means the same input.
4. An input rule has `key` and either `value` (a scalar constant) or a field source `from` (default `key`). A field rule may add `number: true` (copy only a number, never a boolean; with `max`, the smaller value) or `id_key` with `id_match: {"charset": "alnum", "length": n}` (a string of exactly n ASCII letters and digits goes under `id_key`, any other value under `key`). `max` needs `number`; `id_key` and `id_match` go together and not with `number`. The output fields (`key` and `id_key`) of one rule list are unique.
5. Mapping: rules apply in order and the output keys are in rule order. A missing input field is left out; null is copied.

## llm-kit.call-record/v1

One sent try of a model call: `attempt`, `model`, `provider`, `ok`, `status`, `aborted`, `ms`, `inputTokens`, `cachedTokens`, `outputTokens`, `cost`, `providerMessage`. Every field is present (null when unknown); no prompt or reply text, and no other field. `@katsoft/llm-kit-send` gives one to `onCall` per try of `sendWithRetries`; its tests validate the records against this schema. A consumer that stores records adds its own fields in its own store, beside the record.

## llm-kit.call-record/v2

Version 1 plus the required `replayed` (boolean). `@katsoft/llm-kit-send` 0.3.0 and later gives one to `onCall` per try of `sendWithRetries`, and one record with `replayed: true` when the reply cache answered (nothing sent: tokens null, cost 0, `ms` the lookup time, `ok` and `status` read from the stored answer). Version 1 stays for records written before.

## llm-kit.reply-key/v1

The key of one request in the reply cache. Not a file: a hex string that both readers must compute the same way.

1. `requestKey(parts)` is the lowercase hex SHA-256 of each part written as `<byte length>:<bytes>\n`, in order. A text part is its UTF-8 bytes.
2. `stepKey(account, request, scope)` is `requestKey` over: the format id `llm-kit.reply-key/v1`; the account label (non-secret; it names the account of the key actually sent); one part `scope:<requestKey(scope)>` only when the scope is not empty; the method in upper case; the URL exactly as sent; the kept request headers as one part (only `accept` and `content-type`, names lowercased, sorted by name, one `name:value` line each, joined by LF); then the body.
3. The body: a string or bytes is one part; no body is one empty part; a multipart form is four parts per entry, in form order: `name`, `filename`, `type`, bytes (a text field has an empty file name and type). The multipart boundary is not an input.
4. Never inputs: `authorization`, `api-key`, `x-api-key`, `xi-api-key` and every other header outside the allowlist (attribution labels, `content-length`, `user-agent`, request ids).

TypeScript: `requestKey`, `stepKey` in `@katsoft/llm-kit-send`. Python: `llm_kit.registry.reply_cache.request_key`, `step_key`.

## llm-kit.reply-entry/v1

One stored answer of the reply cache, kept by the consumer's `ReplyStore`. Layer `step` is one provider request's answer: `outcome` (`refusal`, or `success` of a deterministic request), `status`, `headers` (the kept response headers: `content-type` and the transport's named ones, never `set-cookie` or account headers), `body` (the bytes, base64; empty for a headers-only entry) and `at`. Layer `operation` is the final refusal of a consumer's whole operation: `outcome` `refusal`, `status`, `note` (`code` and the consumer's fixed text, never a prompt or a picture) and `at`. No other field.

Reader rules beyond the schema: `body` is canonical base64 with padding; response header names are lowercase and not empty. A reader reports the path of the first rule an entry breaks (`entries[].path`).

TypeScript: `readEntry` in `@katsoft/llm-kit-send` (throws `ReplyEntryError`). Python: `llm_kit.registry.reply_cache.read_entry` (raises `ReplyEntryError`).

## llm-kit.consent/v2

The user-level consent file: `$LLM_KIT_CONSENT_FILE`, else `$XDG_CONFIG_HOME/llm-kit/consent.json` (fallback `$HOME/.config/llm-kit/consent.json`). An earlier release's file under `~/.claude` is read only when the neutral file does not exist. No reader takes this file from a project tree. It holds the machine owner's decisions: the cost-class policy, which applies only when a caller opts in, and the connection approvals, which always apply (see "Approval of a connection that sends a key"). The module docstring of `llm_kit.registry.consent` explains the policy.

1. The file is one JSON object in UTF-8. `schema` is required and is the integer 2. The other allowed keys are `local`, `subscription`, `metered`, `scopes`, `connections` and `updated_at`. Any other key is refused.
2. `local`, `subscription` and `metered` are booleans. A class that the file omits takes the built-in default: local and subscription on, metered off.
3. `scopes` maps a scope name (text with no leading or trailing whitespace) to an object of those classes.
4. `connections` is a list of approvals. Each approval has exactly the keys `endpoint`, `host` and `key_env`. `endpoint` is text with no leading or trailing whitespace. `host` is a host as rule 1 of the approval defines it, in lower case, with the port its base URL names. `key_env` is an ASCII letter or `_`, then ASCII letters, digits and `_`. It is never null, because a keyless connection needs no approval.
5. `updated_at` is the time of the last write. The reader does not check it.
6. A file that breaks a rule is refused as a whole. A missing file is the default policy with no approval.
7. A writer writes `connections` only when it holds an approval, sorted and with no repeats. Thus a release before approvals reads a file without one, and refuses a file with one (an unknown key).

Python: `llm_kit.registry.consent.load_config(path)`, `save_config`, `update_config`, `approve_connection` and `revoke_connection`.

## llm-kit.graph-catalog/v1

The graphs a rendered graph page draws (`llm-kit graph render`). Writer: Python `llm_kit.graph.render.catalog` (`build_catalog`, `project_catalog`). Reader of record: Python `llm_kit.graph.render.page.render_page`, which validates the catalog against this schema before it embeds it in the page and raises `ValueError` for a catalog that fails. The page's browser script (`llm_kit/graph/render/static/browser.js`) draws only the catalog that `render_page` embedded in the same file, so it never reads an unchecked one. A catalog that carries labels is version 2, below.

```json
{"format": "llm-kit.graph-catalog/v1", "project": "demo",
 "entries": [{"name": "demo.night", "file": "graphs/night.graph.yaml"}],
 "files": {"graphs/night.graph.yaml": {"id": "demo.night", "file": "graphs/night.graph.yaml", "outer": true,
   "description": "", "inputs": [{"name": "moment", "description": ""}], "outputs": ["bundle"],
   "output_sources": {"bundle": ["bundle"]},
   "nodes": [{"id": "bundle", "kind": "op", "subtitle": "demo.bundle", "when": null, "deps": [],
     "reads_inputs": ["moment"], "detail": {"op": "demo.bundle"}, "definition": {"op": "demo.bundle"}}]}},
 "used_by": {}}
```

1. `files` holds each graph file once, keyed by its path relative to the root the catalog was built at (a file outside the root keeps its absolute path). `entries` lists the graphs the catalog was built from; every `entries[].file` is a key of `files`.
2. A node that runs a graph file has `ref`, a key of `files`. A map or repeat with an inline body has `body` (`location`, `outputs`, `output_sources`, `outer_deps`, `nodes`) instead. A node has at most one of the two.
3. `deps` names sibling nodes the node's expressions read; `reads_inputs` names graph inputs it reads; `output_sources` maps each output to the sibling nodes its binding reads; `outer_deps` names enclosing nodes an inline body reads. These come from a scan of expressions and are for drawing only: `llm-kit graph validate` is the check.
4. `used_by` maps a graph file to the nodes that run it: `file` and `node`, the node path in that file (`/` joins the ids of nested inline bodies).
5. `definition` is the node as written, except that an inline body's `body` is the text `(inline body)`.

## llm-kit.graph-catalog/v2

Version 1 plus one required top-level key, `labels`: the titles, descriptions and edge texts that a model inferred for each graph file. `llm-kit graph render` writes it by default, and `--no-labels` writes version 1. Writer: Python `llm_kit.graph.render.labels` (`label_catalog`, which takes a version 1 catalog and returns this one). Reader of record: `llm_kit.graph.render.page.render_page`, as for version 1.

Labels are a new version, not an optional key of version 1, because no version 1 reader refuses an unknown key: `render_page` checked only `format` and `entries`, and the browser script parsed the catalog without refusing. A reader of version 1 would have dropped the labels in silence. It refuses version 2 by its `format` check instead.

```json
{"format": "llm-kit.graph-catalog/v2", "project": "demo", "entries": [], "files": {}, "used_by": {},
 "labels": {"graphs/night.graph.yaml": {
   "sources": [{"prompt": "llm-kit.graph-labels.prompt/1", "key": "<64 hex digits>",
                "calls": [{"entry": "haiku", "model": "claude-haiku-5-5"}]}],
   "graph": {"title": "Nightly bundle", "purpose": "Bundles a moment, then writes a plot.",
             "inputs": {"moment": "the moment to bundle"}, "outputs": {"plot": "the written plot"}, "source": 0},
   "nodes": {"approve": {"title": "Approve the bundle", "description": "A person approves the bundle.",
                         "reads": {"bundle": "the bundle to approve"}, "source": 0}}}}}
```

(`entries` and `files` are as in version 1; the example leaves them empty to stay short, and a real catalog has at least one entry.)

1. Every key of version 1 means what it means there. `labels` is required and is an object; `{}` is allowed. Any other new key is refused.
2. `labels` maps a key of `files` to that file's block. A block always has `sources`, `graph` and `nodes`; a writer never writes a partial block.
3. `sources` is a non-empty list. Each source is one labelling request: `prompt` (the prompt version), `key` (the cache key of its primary call: 64 lowercase hex digits; the schema checks the length, and the lowercase hex is a reader rule) and `calls`. `calls` holds one item, the primary call, or two, the primary call and then one follow-up for keys the primary call left out. Each call names `entry`, the model entry id that served it, and `model`, the model that answered. The entry that served the primary call serves the follow-up. A file labelled in several requests (a large file is split by nodes) has one source for each.
4. `source`, in `graph` and in each node label, is an index into `sources` of the same block.
5. `graph` has `title`, `purpose`, `inputs` and `outputs` (port name to gloss) and `source`. A node label has `title`, `description`, `reads` and `source`. `reads` maps the id of a dep node to the text of the edge from that dep into this node; it is `{}` when the node has none.
6. A key of `nodes` is a node path in that file (`/` joins the ids of nested inline bodies, as in `loop/write`). A key of `reads` is one of that node's `deps`. A key of `inputs` or `outputs` names a port of the graph. A reader ignores a label whose key names nothing and draws an item that has no label as version 1 draws it. The schema cannot check these keys, so the case `label-with-no-target-is-ignored` fixes the rule: it is valid, and `render_page` draws it.
7. Limits, counted in characters; every string is at least 1 character: title 80, purpose 400, node description 300, edge text 60, port gloss 160. The schema refuses a longer string.
8. The text is written by a model from graph files and prompts. A reader shows it as inferred and never reads it as markup: `render_page` escapes `<`, `>`, `&`, U+2028 and U+2029 in the embedded JSON, and the browser script sets text with `textContent` only.
9. `render_page` runs every case of version 1 and version 2: a valid case gives a page, and an invalid case raises `ValueError`.

## llm-kit.graph-labels/v1

One cache entry of the labels that `llm-kit graph render` inferred: the answer of one labelling request. Writer and reader: Python `llm_kit.graph.render.labels`. The file is `<key>.json` in the labels cache directory (`$XDG_CACHE_HOME/llm-kit/graph-labels/`, else `~/.cache/llm-kit/graph-labels/`, or the directory of `--label-cache`).

```json
{"format": "llm-kit.graph-labels/v1", "key": "<64 hex digits>", "prompt": "llm-kit.graph-labels.prompt/1",
 "declaration": ["haiku"], "calls": [{"entry": "haiku", "model": "claude-haiku-5-5"}],
 "answer": {"graph": {"title": "Nightly bundle", "purpose": "Bundles a moment, then writes a plot."},
            "nodes": {"approve": {"title": "Approve the bundle", "description": "A person approves the bundle."}},
            "edges": {"bundle->approve": "the bundle to approve"},
            "inputs": {"moment": "the moment to bundle"}, "outputs": {"plot": "the written plot"}}}
```

1. All six keys are required and no other key is allowed. `key` is the cache key of the primary call, 64 lowercase hex digits, and equals the file name without `.json`. `prompt` is the prompt version. `declaration` lists the model entry ids the request named. `calls` is as in a version 2 source.
2. `answer` is the merged answer of the primary call and its follow-up, in the shape the model answers in. `nodes` and `edges` are always present (`edges` may be `{}`). `graph`, `inputs` and `outputs` are present only when the request had the file header, which is the first request of a file.
3. A key of `nodes` is a node path. A key of `edges` is `<dep path>-><node path>`, where both paths are node paths in the file. Node ids never hold `/` or `->`, so a key splits in one way.
4. Limits are those of version 2: title 80, purpose 400, node description 300, edge text 60, port gloss 160; every string is at least 1 character.
5. A reader that finds a file that fails these rules, whose `key` is not its file name, or whose answer lacks a key the request needs, refuses the file as a whole and names it. It never uses part of it.

## Change a format

- A change that a v1 reader would read in a different way is a new format version: a new id, a new schema file and new cases. Keep the v1 files. A key that no older reader refuses is such a change, because an older reader would drop it in silence (the graph catalog's labels).
- An optional key that a v1 reader refuses as unknown can be added to v1: every file without it reads as before, and an older reader refuses a file that uses it, so no reader misreads one. The request settings (`endpoints`, `requests`, row `traits`, typed `request_defaults`, selection `request`) and the endpoint connection (`base_url`, `key_env`) were added this way, and so were the consent file's `connections`.
- Before you change a reader rule, add the case that shows it.
- Every Python writer of a format checks its output against the schema in its tests (`tests/llm-kit/test_formats.py`, `tests/llm-kit/test_project_models*.py`).
