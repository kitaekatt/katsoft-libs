# Changelog: @katsoft/llm-kit-config

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar", with the npm checks for lines 1 to 5. An entry records which bar lines pass.

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
