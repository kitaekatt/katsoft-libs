# @katsoft/llm-kit-config

Readers for three llm-kit file formats, for Node.js:

- the project model file (`llm-kit.project-models/v1`): a catalog of models and named selections,
- overlays in the personal-layer format (`llm-kit.project-models-local/v1`): per-key replacements of selections,
- prompt files (`llm-kit.prompt-file/v1`): an optional header, a `---PROMPT---` line and a body with `{{slots}}`.

The rules are the same as the Python reader in the `katsoft-llm-kit` package. Both readers run the same shared cases. The contract is `docs/reference/llm-kit-formats.md` in the [katsoft-libs repository](https://github.com/kitaekatt/katsoft-libs).

```sh
npm install @katsoft/llm-kit-config
```

The package needs Node.js 24 or later. It is ESM only. Its one runtime dependency is `yaml`.

## Models

```js
import { applyOverlay, loadOverlay, loadRegistry, select } from "@katsoft/llm-kit-config";

const registry = applyOverlay(loadRegistry(".llm-kit/models.yaml"), loadOverlay("config/staging.models.yaml"));
const { row, effort, source } = select(registry, "summary.write");
// row: {id, label, endpoint, model, efforts?, max_output_tokens?, traits?, request_defaults?}
```

- `parseRegistry(input, {source})` and `loadRegistry(path)` read and validate the tracked file. The result is a frozen `Registry`: `document` (the normalized file) and `resolved` (each key's `effective` and `default` selection and its `source`).
- `parseOverlay(input, {source})` and `loadOverlay(path)` read an overlay and check its structure.
- `applyOverlay(registry, ...overlays)` checks each overlay against the catalog and applies the overlays in order. Per key, the last layer that names the key replaces the whole selection. An entry equal to the tracked entry is not an override. A key that only an overlay names has no default.
- `select(registry, key)` returns `{key, row, effort, source}`. It throws `unknown-selection` when no layer names the key. `findSelection(registry, key)` returns `undefined` in that case, and only in that case.

## Requests

A selection can describe the whole request: the row's defaults, a named request profile (`requests`) that the selection's `request` names, and the call, in that order. `endpoints` in the file says where an endpoint takes the effort, and may name its connection: `base_url` and `key_env`, the name of the environment variable that holds the key (never a key). A caller whose own configuration knows the endpoint uses its own connection. Read "The file is untrusted" below before a caller sends a key to a connection. `traits` are your labels for a row; they are never sent. A row's optional `wire_model` is the model id its endpoint is sent when it differs from `model`; `request.wire_model` is always set (`model` when the row names none).

```js
import { chatFields, resolveRequest } from "@katsoft/llm-kit-config";

const request = resolveRequest(registry, "summary.write", { max_output_tokens: 300 });
// {row, endpoint, model, wire_model, effort, effort_style, temperature, top_p, max_output_tokens, reasoning_budget, extras, traits}
const body = { model: request.wire_model, messages, ...chatFields(request, { effortStyle: "top-level" }) };
```

- `resolveRequest(registry, key, call)` layers the settings. Per setting the highest layer wins; `extras` merge per key. `effort` and `reasoning_budget` are one reasoning control: the highest layer that sets either wins. A call `effort: null` sends no reasoning control.
- `chatFields(request, {effortStyle})` returns the OpenAI-compatible body fields: `max_tokens`, `temperature`, `top_p`, the effort in its style, the budget and the extras. `effortStyle` is used only when the file declares no style for the endpoint. It throws `wire` when the style cannot carry the effort or the budget.
- The Python reader resolves the same request and its protocol 2 call sends the same fields; the shared cases are `tests/formats/project-models/requests.json`.

The package does not know environment names, endpoints or credentials. The caller selects which overlay files to apply and maps `endpoint` to a provider. The package does not bind endpoints or check efforts against a provider's menu. The Python reader calls these checks "binding".

All the load functions are synchronous, so a program can read its configuration when it starts.

### The file is untrusted

The project model file is repository content: anyone who can change it can name any host in `base_url` and any environment variable in `key_env`, and any wire field in `extras`. This package validates the shape of the file and nothing more. It never reads an environment variable, never sends a request and does not approve a connection.

- **Connections.** The Python library uses a connection from the file that has a `key_env` only after the machine owner approves it. All of these must hold: the host is plain https (or http to a literal loopback address), no endpoint of the machine already sends that key variable to another host, and the user-level consent file approves that exact endpoint name, host and key variable. The owner approves with `llm-kit consent connection on --endpoint NAME --host HOST --key-env VAR`. A keyless connection (`key_env: null`) needs no approval. This reader applies none of these rules, so a caller must not send the value of `process.env[endpoint.key_env]` to `endpoint.base_url` unless it applies the same rule itself. Take the approval from the machine owner's own configuration, never from the project tree. The rules are in `docs/reference/llm-kit-formats.md`, "Approval of a connection that sends a key".
- **Extras.** The Python binding refuses these keys in a row's `request_defaults.extras` and in a profile's `extras`: `add_dirs`, `scratch_dir`, `output_schema`, any key that starts with `__llm_kit_`, and `network` with any value but `false`. They widen a Codex sandbox. This reader accepts all of them as syntax, `resolveRequest` merges them into `extras`, and `chatFields` puts them in the body. A caller that gives extras to a Codex-style backend, or that does not want these wire fields, must refuse them itself.

## Prompts

```js
import { loadPrompt, renderPrompt } from "@katsoft/llm-kit-config";

const prompt = loadPrompt("prompts/summary.md"); // {source, header, body, slots, sha256}
const text = renderPrompt(prompt, { kind: "article", text: input }, { suffix: "\n\nAnswer:" });
```

- The bytes must be UTF-8. One leading byte order mark is removed. CRLF and CR become LF.
- `sha256` is the SHA-256 hex digest of the body. Use it to see when the body changes.
- `renderPrompt` is strict. The values must name exactly the slots, and each value must be a string. Each slot is filled one time. A value that contains `{{x}}` is inserted as written. Values and the suffix are not changed.

## Errors

Every function stops at the first problem and throws `LlmKitConfigError`. Nothing falls back to a default.

| `code` | Meaning |
|---|---|
| `invalid` | The content is wrong. `where` is the path of the first problem, the same path that the Python reader gives: `""` (the bytes or the YAML), `document`, `version`, `catalog[1].efforts[0]`, `selections['draft'].effort`. |
| `missing` | The file does not exist. |
| `io` | The file exists but cannot be read. `cause` holds the system error. |
| `unknown-selection` | `select` got a key that no layer names. |
| `slot` | `renderPrompt` got values that do not match the slots. `where` is the slot name, or `suffix`. |
| `wire` | `chatFields` cannot put the request on the wire. `where` is `effort`, `reasoning_budget` or `extras.<key>`. |

`source` is the file path, or the `source` option you gave. A wrong argument type is a `TypeError`.

## YAML

The project model files are YAML 1.1, read as PyYAML's safe loader reads them. For example, `on` and `yes` are booleans, `y` is text, `0100` is octal 64, `1:30` is 90, `1.0e3` is text, and `2026-11-13` is a date (and so not a string). The reader refuses a duplicate key, a merge key (`<<`), `!!omap`, `!!pairs`, a value that is not JSON data, and an integer outside -(2**53 - 1) to 2**53 - 1.

The parser is the `yaml` package, with PyYAML's rules for values and some of its scanner rules (tabs, tags, anchors, directives). This package refuses some inputs that PyYAML accepts. The reader stops with `invalid`; it does not read a different value. These inputs are:

- a plain `-` alone in a flow collection (`[a, -]`),
- a comment directly after a quoted scalar with no space (`'a'#c`),
- a flow collection whose next lines are indented less than its key,
- U+2028 and U+2029 anywhere in the file.

Any other difference from the Python reader is a bug. Report it with the input.

## Schemas

The JSON Schemas of the three formats ship in the package as `@katsoft/llm-kit-config/schemas/<file>.schema.json`. `SCHEMA_IDS` holds their ids. The package copies them at pack time from the Python package `katsoft-llm-kit`.

## Development

```sh
npm ci
npm test            # builds, then runs the shared cases and the unit tests
npm run check-pack  # packs, installs the tarball in a new directory, runs examples/, checks the .d.ts with TypeScript 5.8
```

The release procedure is in `docs/reference/package-release.md`, section "npm packages".
