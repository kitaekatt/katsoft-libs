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
// row: {id, label, endpoint, model, efforts?, max_output_tokens?, request_defaults?}
```

- `parseRegistry(input, {source})` and `loadRegistry(path)` read and validate the tracked file. The result is a frozen `Registry`: `document` (the normalized file) and `resolved` (each key's `effective` and `default` selection and its `source`).
- `parseOverlay(input, {source})` and `loadOverlay(path)` read an overlay and check its structure.
- `applyOverlay(registry, ...overlays)` checks each overlay against the catalog and applies the overlays in order. Per key, the last layer that names the key replaces the whole selection. An entry equal to the tracked entry is not an override. A key that only an overlay names has no default.
- `select(registry, key)` returns `{key, row, effort, source}`. It throws `unknown-selection` when no layer names the key. `findSelection(registry, key)` returns `undefined` in that case, and only in that case.

The package does not know environment names, endpoints or credentials. The caller selects which overlay files to apply and maps `endpoint` to a provider. The package does not bind endpoints or check efforts against a provider's menu. The Python reader calls these checks "binding".

All the load functions are synchronous, so a program can read its configuration when it starts.

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
