# Changelog: @katsoft/llm-kit-config

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

## 1.4.0

Security fix. The tracked project file is repository content, so it may not set the Codex sandbox extras. The Python binding has refused them since katsoft-llm-kit 1.37.0; this reader accepted them and passed them on (1.3.0 and earlier, and the README of 1.3.0 said so). Now it refuses them too. This is a minor release because it follows the Python release of the same rule (a minor release, with the behaviour change listed first), the format and its schemas do not change, and no file that the Python library accepts is refused here.

Changed behaviour (read before upgrading):

- `resolveRequest` throws `invalid` when the file sets, in a row's `request_defaults.extras` or in a `requests` profile's `extras`, any of `add_dirs`, `scratch_dir`, `output_schema`, a key that starts with `__llm_kit_`, or `network` with a value other than `false`. `where` is `catalog[<i>].request_defaults.extras.<key>` or `requests['<name>'].extras.<key>`, and the message names the key. It checks the whole file, before it looks up the selection key (as the Python `validate_document` does), so a refused file is refused for every key, an unknown key included. A caller that sent these extras on to a Codex-style backend now gets the error instead. `network: false` and every other extras key stay allowed.
- Unchanged: `parseRegistry`, `loadRegistry`, `parseOverlay`, `loadOverlay` and `applyOverlay` read a file that holds these keys, as the Python portable validation does. A call's own `extras` (`resolveRequest(registry, key, {extras})`) may set them, and `chatFields` still does not check extras.
- The connection-approval rule stays a duty of the caller (README, "The file is untrusted"): it depends on the machine's consent file, which this package never reads.

Added:

- `tests/formats/project-models/tracked-extras.json`: 34 shared cases (refused and accepted keys in a row and in a profile, and which problem is the first). Python runs them against `validate_document` (`tests/llm-kit/test_codex_tracked_extras.py`), this package against `resolveRequest` (`test/tracked-extras.test.mjs`). Both fail when the file is missing or holds no case.

Quality bar for the pieces above:

1. Public API documented: pass. The JSDoc of `resolveRequest` (`@throws`), `chatFields`, `RequestDefaults.extras` and `RequestProfile.extras` states the rule; no export is added, so `public-surface.json` is unchanged; `test/surface.test.mjs` passes.
2. Semver release with a changelog: pass. Version 1.4.0 in `package.json` and `package-lock.json` (a minor release, for the reasons above); this entry.
3. Tests for each public function: pass. `test/tracked-extras.test.mjs` runs the 34 cases through `parseRegistry`, `loadRegistry` and `resolveRequest`, and checks the order (the file before the key), an overlay, the call layer and `chatFields`; it fails before the change.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed; `npm run check-pack` runs them from the packed tarball.
6. JSON Schema for each file format: pass. The schema does not change and cannot state the rule (it accepts every case; the test checks that with ajv); the shared cases state it for both readers.
7. Gate before adoption: pass. Lines 1 to 6 pass.

## 1.3.0

Added:

- `parseRegistry` reads a row's optional `wire_model`: the model id the row's endpoint is sent, when it differs from `model`. `CatalogRow.wire_model`.
- `resolveRequest` returns `wire_model`, always set (the row's `wire_model`, else `model`); a sender puts it in the body. `ResolvedRequest.wire_model`. A caller that compares a whole resolved request sees the new key. 4 new shared cases in `tests/formats/project-models/cases.json` and 1 in `requests.json`; every request case states the new key.
- A row may declare the explicit effort `none`; the reader accepts it as any declared effort, and `chatFields` sends it as written on `top-level` (Python binding holds it to the library rule: `docs/reference/llm-kit-formats.md`, "Portable validation and binding"). 1 new shared case.

Quality bar for the pieces above:

1. Public API documented: pass. The JSDoc of `CatalogRow.wire_model` and `ResolvedRequest.wire_model` states them; no export is added, so `public-surface.json` is unchanged; `test/surface.test.mjs` passes.
2. Semver release with a changelog: pass. Version 1.3.0 in `package.json` and `package-lock.json` (a minor release); this entry.
3. Tests for each public function: pass. `test/cases.test.mjs` and `test/requests.test.mjs` run the new cases through `parseRegistry`, `resolveRequest` and `chatFields`; they fail before the change.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed; the README example sends `request.wire_model`.
6. JSON Schema for each file format: pass. The schema states `wire_model`; the cases validate with ajv.
7. Gate before adoption: pass. Lines 1 to 6 pass.

## 1.2.0

Added:

- `parseRegistry` reads an endpoint's connection: `endpoints.<name>.base_url` and `key_env` (both or neither; `key_env` is an environment variable name or null, never a key), returned in the normalized document. New type `Endpoint`. The reader does not call endpoints; a caller whose own configuration knows the endpoint uses its own connection (`docs/reference/llm-kit-formats.md`, "Connections"). 10 new shared cases.

Quality bar for the pieces above:

1. Public API documented: pass. `Endpoint` has a JSDoc block and is in `public-surface.json`; `test/surface.test.mjs` passes.
2. Semver release with a changelog: pass. Version 1.2.0 in `package.json` and `package-lock.json` (a minor release); this entry.
3. Tests for each public function: pass. `test/cases.test.mjs` runs the new cases through `parseRegistry`.
4. No client names, prompts or data: pass.
5. Examples run without a client app: pass. No example changed.
6. JSON Schema for each file format: pass. The schema states both endpoint shapes; the cases validate with ajv.
7. Gate before adoption: pass. Lines 1 to 6 pass, so a client may adopt `Endpoint` and the connection keys from this version on.

Fixed:

- `parseRegistry` and `parseOverlay` refuse, at the path `""`, a decimal integer of more than 4300 digits (as the Python reader does) and a value that nests more than 100 levels of lists and mappings (aliases count as copies). A far deeper file threw a `RangeError` (stack overflow) instead of an `LlmKitConfigError`. 16 new shared cases in `tests/formats/project-models/cases.json`.

## 1.1.0

The project model file can describe the whole request of a selection, so a client builds it from the file alone. The format stays `llm-kit.project-models/v1`: every key below is optional, and a file without them reads as before.

Added:

- Reading the new keys: `endpoints` (`{<endpoint>: {effort_style}}`), `requests` (named request profiles: `temperature`, `top_p`, `max_output_tokens`, `reasoning_budget`, `extras`), row `traits` (consumer labels, never sent), typed row `request_defaults` (`temperature`, `top_p`, `reasoning_budget`) and selection `request`. Overlays may name a `request`; `applyOverlay` checks it against the tracked `requests`.
- `resolveRequest(registry, key, call)`: the resolved request (row < selection < call), every key present.
- `chatFields(request, {effortStyle})`: its OpenAI-compatible body fields, with the effort placed in its style.
- Types `EffortStyle`, `RequestDefaults`, `RequestProfile`, `RequestCall`, `ResolvedRequest`, `ChatFieldsOptions`; error code `wire`.
- `examples/request.mjs`.

Quality bar for the pieces above:

1. Public API documented: pass. Each new name has a JSDoc block; `test/surface.test.mjs` passes.
2. Semver release with a changelog: pass. Version 1.1.0 in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. `test/requests.test.mjs` runs all 30 request cases through `resolveRequest` and `chatFields`; `test/cases.test.mjs` runs the 25 new project model cases; `test/api.test.mjs` covers errors, sources, freezing and argument types.
4. No client names, prompts or data: pass. Generic rows and endpoints.
5. Examples run without a client app: pass. `npm run check-pack` runs `examples/request.mjs` from the packed tarball.
6. JSON Schema for each file format: pass. The schemas gain the new keys; the shared cases validate parsed data and results with ajv.
7. Gate before adoption: pass. Lines 1 to 6 pass for the pieces above, so a client may adopt them from this version on.

## 1.0.1

No code change. This is the first release that GitHub Actions publishes through npm trusted publishing, with provenance. The code, the types and the schemas are the same as in 1.0.0.

Quality bar:

1. Public API documented: pass. No change from 1.0.0; `test/surface.test.mjs` passes.
2. Semver release with a changelog: pass. Version 1.0.1 in `package.json` and `package-lock.json`; this entry.
3. Tests for each public function: pass. No change from 1.0.0; `npm test` passes.
4. No client names, prompts or data: pass. No change from 1.0.0.
5. Examples run without a client app: pass. `npm run check-pack` installs the packed tarball in a new empty directory and runs both examples with plain `node`.
6. JSON Schema for each file format: pass. The schemas are the same as in 1.0.0; `test/pack.test.mjs` passes.
7. Gate before adoption: recorded in this entry.

## 1.0.0

First release. Readers for `llm-kit.project-models/v1`, `llm-kit.project-models-local/v1` (used as overlays) and `llm-kit.prompt-file/v1`, with the rules of the Python reader in katsoft-llm-kit 1.25.0.

Added:

- `parseRegistry`, `loadRegistry`: read and validate a project model file (portable validation: structure, JSON values, references).
- `parseOverlay`, `loadOverlay`, `applyOverlay`: overlays in the personal-layer format. `applyOverlay` checks the catalog references and replaces whole selections per key; the last layer wins.
- `select`, `findSelection`: the row, effort and source (`default` or `override`) of a selection key.
- `parsePrompt`, `loadPrompt`, `renderPrompt`: prompt files and strict one-pass rendering.
- `LlmKitConfigError` with `code` (`invalid`, `missing`, `io`, `unknown-selection`, `slot`), `where` (the Python reader's path) and `source`.
- `SCHEMA_IDS` and the three JSON Schemas in `schemas/`.

Quality bar:

1. Public API documented: pass. Each name in `public-surface.json` has a JSDoc block with its purpose, parameters, return value and errors. `test/surface.test.mjs` compares the list with the runtime exports and the `.d.ts` exports, and checks each block.
2. Semver release with a changelog: pass. Version 1.0.0 in `package.json`; this entry.
3. Tests for each public function: pass. `test/api.test.mjs` calls each function; `test/cases.test.mjs` runs all 85 project model cases and all 32 prompt cases (17 render entries) through the parse and load functions; `npm test` passes.
4. No client names, prompts or data: pass. The examples, tests and README use generic names.
5. Examples run without a client app: pass. `npm run check-pack` installs the packed tarball in a new empty directory and runs `examples/registry.mjs` and `examples/prompt.mjs` with plain `node`.
6. JSON Schema for each file format: pass. The shared-case tests validate the parsed data and the normalized results against the three schemas with ajv. `test/pack.test.mjs` compares the packed schemas with the Python package's files byte for byte.
7. Gate before adoption: recorded in this entry.
