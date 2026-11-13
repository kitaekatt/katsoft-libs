# Katsoft libraries

This repository owns standalone Python libraries, their tests, and their PyPI release tools.
The libraries work without an AI harness or plugin installation.

## Development

Work on the existing dev branch. Scope commits to your own files.
Keep the libraries usable from a plain Python installation without an AI harness.
Keep plugin integrations in the plugins-kit repository.

Run `uv run --extra dev pytest` after code changes.
For the npm package in `packages/llm-kit-config`, run `npm ci`, `npm test` and `npm run check-pack` there after its code changes.
Read `docs/reference/package-release.md` before changing release code or releasing a package.
Package releases require the user's explicit publication approval.

The extraction commit retains plugins-kit ancestry and original release commit IDs.
Historical plugin files remain in that ancestry. The current tree contains library development files only.

## Quality bar

The libraries serve all of the owner's clients, not one client.
Clean and reusable libraries are a tier-1 priority.
A library piece must pass every line below before a client adopts it.

Each line names its check. A reviewer check means a person or agent reads the code, docs or tests and records pass or fail.

1. **Public API documented.** Each public name has a docstring that gives its purpose, inputs, outputs and errors. Each package lists its public names in `public-surface.json`, and its tests check that list against the code. Check: reviewer reads the docstring of each listed name.
2. **Semver releases with a changelog.** Each release uses a semantic version (MAJOR.MINOR.PATCH) in its `pyproject.toml`. Each package keeps `CHANGELOG.md` in its directory, with one entry for each version. Check: reviewer confirms the version and entry before release. Step 1 of `docs/reference/package-release.md` covers the version change.
3. **Tests for each public function.** Each public function has at least one test under `tests/` that calls it. Check: reviewer maps each name in `public-surface.json` to a test; `uv run --extra dev pytest` passes.
4. **No client names, prompts or data in the library.** Client names, client prompts and client data stay out of `packages/` and `tests/`. Plugin integrations stay in plugins-kit, as the Development section says. Check: reviewer asks of each public name and each data file: would another client use this as is? A client name, a client prompt or a client-only rule is a fail.
5. **Examples run without a client app.** Each package has at least one example in `packages/<name>/examples/`. Each example runs in a plain Python environment with no AI harness, plugin or client app. Check: build the wheel as the Build contract in `docs/reference/package-release.md` describes, install it in a new virtual environment, and run each example.
6. **JSON Schema for each file format.** Each file format that a library reads or writes has a JSON Schema file. Each reader and writer test validates its data against that schema. The llm-kit relay schemas in `packages/katsoft-llm-kit/src/llm_kit/relay/schemas/` are one example. Check: `uv run --extra dev pytest` passes; reviewer confirms each reader and writer test loads its schema.
7. **Gate before adoption.** A new piece (a new package, or a new public module or file format) passes every line above before a client first adopts it. A piece that fails the bar is not adopted until it passes. An existing piece that clients already use is brought up to the bar when it is next changed for a client, line by line. Check: reviewer records which lines pass in that version's entry in `CHANGELOG.md`.

An npm package uses these checks for lines 1 to 5. Python packages keep the checks above. Lines 4, 6 and 7 apply to both unchanged.

- **Line 1.** Each exported name has a JSDoc comment that gives its purpose, parameters, return value and errors. `public-surface.json` lists the exports, and a test compares that list with the package exports. Check: reviewer reads the JSDoc of each listed name.
- **Line 2.** `package.json` holds a semantic version. `CHANGELOG.md` in the package directory has one entry for each version, and the entry records which bar lines pass. Check: reviewer confirms the version and entry before release.
- **Line 3.** Each exported function has at least one test that calls it, and the package test command passes. Check: reviewer maps each name in `public-surface.json` to a test.
- **Line 5.** Each package has at least one example in its `examples/` directory. Check: install with the lockfile (`npm ci`), build, run `npm pack`, install the tarball in a new empty directory with no workspace link, and run each example with plain `node`. A release builds from the locked tree, and the published tarball's integrity must equal the packed tarball's integrity before the release is recorded.
