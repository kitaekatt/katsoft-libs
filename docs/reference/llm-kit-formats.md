# llm-kit shared file formats

Some llm-kit files have readers in more than one language. Each of these formats has one format id, one JSON Schema in the package `llm_kit.schema.formats`, and one shared cases file under `tests/formats/`. Every reader, in every language, must pass every case of its format. This page is the contract. When this page, a schema and a case disagree, the case is correct; fix the page or the schema.

| Format id | Schema (in `llm_kit/schema/formats/`) | Python reader | Shared cases |
|---|---|---|---|
| `llm-kit.project-models/v1` | `project-models.v1.schema.json` | `llm_kit.registry.project_models` | `tests/formats/project-models/cases.json` |
| `llm-kit.project-models-local/v1` | `project-models-local.v1.schema.json` | `llm_kit.registry.project_models` | `tests/formats/project-models/cases.json` |
| `llm-kit.prompt-file/v1` | `prompt-file.v1.schema.json` | `llm_kit.graph.prompt` | `tests/formats/prompt-file/cases.json` |
| `llm-kit.run-record/v1` | `run-record.v1.schema.json` | `llm_kit.runs.record` | `tests/formats/run-record/cases.json` (`records`) |
| `llm-kit.run-import/v1` | `run-import.v1.schema.json` | `llm_kit.runs.store` | `tests/formats/run-record/cases.json` (`import_lines`) |
| `llm-kit.run-store/v1` | `run-store.v1.schema.json` | `llm_kit.runs.store` | `tests/formats/run-record/cases.json` (`stores`) |

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
5. Refuse a duplicate key at any depth, a merge key (`<<`), `!!omap`, `!!pairs`, a control character that YAML does not allow (such as U+0007), an empty document and more than one document.
6. Anchors and aliases are allowed. An alias is a copy of its anchored value. A value that holds itself through an alias is refused at the path of the alias.
7. Every value must be JSON data. Refuse a date, a timestamp, binary data, a set, a key that is not a string, NaN, infinity (also a float that overflows, such as `1.0e+400`) and an integer outside -(2**53 - 1) to 2**53 - 1. A YAML decimal stays a decimal: `65536.0` is not an integer, although a JSON reader can see 65536.

### Structure

| Key | Rule |
|---|---|
| `version` | Required. The integer 1. |
| `catalog` | Optional list of rows, default empty. Order is kept. |
| row `id`, `label`, `endpoint`, `model` | Required text: not empty, no leading or trailing whitespace. Ids are unique. |
| row `efforts` | Optional list of non-empty text with no repeats. Omitted: the row does not narrow the effort menu. `[]`: the row has no effort. |
| row `max_output_tokens` | Optional integer from 1 to 2**53 - 1. |
| row `request_defaults` | Optional mapping that holds only `extras`, a mapping of JSON values. |
| `selections` | Optional mapping, default empty. A key is text with no leading or trailing whitespace. A value is `{model, effort}`. |
| selection `model` | Required. A catalog id. |
| selection `effort` | Optional. Omitted and null mean the same: no effort. When the row declares `efforts`, the effort is one of them; when the row declares `[]`, the effort is null. |

No other keys are allowed at any level.

### Portable validation and binding

Validation has two parts.

- Portable validation checks the bytes, the YAML, the structure and the references above. It needs only the file. Python: `validate_portable_document(data)`. Every reader does this part.
- Binding needs the machine's endpoint registry. It checks that each `endpoint` resolves, that each effort is in the library effort menu for that endpoint and model, and that `extras` has no reserved key (`model`, `messages`, `stream`, `api_key`, `reasoning_effort`, and `reasoning` on a `reasoning-object` endpoint). Python: `validate_document`, and every read and write. A row that declares no `efforts` accepts any non-empty effort in portable validation; binding checks it against the menu.

Binding never accepts a file that portable validation refuses. When the file has no binding problem, full validation refuses it at the same path as portable validation.

### Result

Portable validation returns the normalized document: `version` 1, `catalog` (each row with only the keys it declares), and `selections` with `effort` always present (null when omitted). Aliases are expanded.

### Errors

A refusal has a code and a path. Every refusal of the file content has the code `invalid`. The path names the first problem:

- `""`: the bytes or the YAML (not UTF-8, a YAML error, a duplicate key, a merge key, an empty file).
- `document`: the top level is not a mapping, or has an unknown key.
- `version`, `catalog`, `selections`.
- `catalog[1]`, `catalog[1].id`, `catalog[1].efforts[0]`, `catalog[0].request_defaults.extras.a.b[2]`.
- `selections['draft.write']`, `selections['draft.write'].model`, `selections['draft.write'].effort`. A selection key is shown in single quotes. The cases use only keys without quotes, backslashes or control characters; for other keys the quoting is the reader's own.
- `selections`: a key that is not a string.

Python raises `ProjectModelsError` with `code` and `where` (the path).

## llm-kit.project-models-local/v1

The personal layer beside the tracked file, by default `.llm-kit/models.local.yaml`. The bytes and YAML rules are the same as for the tracked file.

- It holds `version` (required, 1) and `selections` (optional). It holds no catalog.
- Each selection is checked as a tracked selection, against the tracked catalog. Python: `validate_portable_local_document(data, tracked_document)`.
- Resolution is per key and replaces the whole selection. A key in the local layer wins over the same key in the tracked file. A key that only the local layer names is an override with no default. A local entry equal to the tracked entry is no override (an omitted effort equals null). Python: `resolve_selections(defaults, overrides)` returns, for each key, `effective`, `default` (null for a local-only key) and `source` (`default` or `override`).

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

Fields of every record: `format`, `id`, `status`, `started_at`, `flow` (the writer's name for the work), `group` (`id`, `kind` `compare`, `eval` or `single`, `label`), `case` (`id`, `input_sha256`, `input`), `prompt` (`id`, `file`, `sha256`, `slots_sha256`, or `null`), `selection` (`name`, `model`, `effort`), `resolved` (`row`, `endpoint`, `model`, `effort`, `registry_sha256`, or `null`), `request` (`max_output_tokens`, `extras`, `sent_sha256`), `inputs`, `provenance` (`git_sha`, `git_dirty`, `tool`, `files`, `scorer`), `content` and `labels`. A result adds `ended_at`, `output` (`text`, `parsed`), `error` (`code`, `message`), `scores`, `usage`, `cost` and `timings`.

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

## Change a format

- A change that a v1 reader would read in a different way is a new format version: a new id, a new schema file and new cases. Keep the v1 files.
- Before you change a reader rule, add the case that shows it.
- Every Python writer of a format checks its output against the schema in its tests (`tests/llm-kit/test_formats.py`, `tests/llm-kit/test_project_models*.py`).
