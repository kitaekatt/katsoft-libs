# Changelog: katsoft-llm-kit

Each released version has one entry. The quality bar is in the repository's root `AGENTS.md`, "Quality bar". An entry records which bar lines pass for the pieces that version adds or changes.

## 1.25.0

Shared file formats. Three llm-kit file formats get a format id, a JSON Schema and shared cases that readers in other languages also run. The contract is `docs/reference/llm-kit-formats.md`.

Added:

- `llm_kit.schema.formats`: `PROJECT_MODELS_V1`, `PROJECT_MODELS_LOCAL_V1`, `PROMPT_FILE_V1`, `SCHEMA_FILES`, `load_schema(format_id)`, and the schema files `project-models.v1.schema.json`, `project-models-local.v1.schema.json` and `prompt-file.v1.schema.json` (in `llm-scripting-kit.json-schema-subset/v1`).
- `llm_kit.registry.project_models.validate_portable_document(data)` and `validate_portable_local_document(data, tracked_document)`: validation of the project model file and its personal layer without the machine's endpoint registry (structure, JSON values, references). Binding (endpoints, effort menus, reserved extras) stays in `validate_document` and every read and write.
- `ProjectModelsError.where`: the path of the first problem (`catalog[1].efforts[0]`), `""` for the file as a whole.
- `llm_kit.registry.project_models.__all__`; the module's public names are listed in `public-surface.json`.
- `llm_kit.graph.prompt.PromptFile`, `parse_prompt_file(data)` and `read_prompt_file(path)`: a prompt file's header, body, slots and body SHA-256. `llm_kit.graph.loader.prompt_parts(text)`: header and body.
- `tests/formats/`: shared cases for the project model file (85) and the prompt file (32).
- `examples/validate_and_render.py`.

Changed:

- Prompt files (`llm-kit.prompt-file/v1`). Every reader removes one leading byte order mark and reads CRLF and lone CR as LF before it looks for the marker line. Before, `prompt_body`, `prompt_slots` and `render_prompt` split text only on LF and kept a byte order mark, while `render_prompt_file` and the graph loader read files with CRLF and CR changed to LF. Text with CR now reads the same through every function. A file that starts with a byte order mark now finds a marker on its first line, and its body no longer starts with U+FEFF. Graph node hashes of files with only LF and no byte order mark do not change.
- The project model YAML reader refuses merge keys (`<<`), `!!omap` and `!!pairs`. Before, a merge key was expanded and `!!omap` and `!!pairs` were read as nested lists.
- The project model file refuses an integer outside -(2**53 - 1) to 2**53 - 1 in `extras` and in `max_output_tokens`, because other JSON readers cannot hold it exactly.
- A value in `extras` that holds itself through a YAML alias is refused as `invalid`. Before, it caused a `RecursionError`.

Quality bar for the pieces above (`llm_kit.schema.formats`, the three formats, `llm_kit.registry.project_models`, the new names in `llm_kit.graph.prompt` and `llm_kit.graph.loader`):

1. Public API documented: pass. Each listed name has a docstring with its purpose, inputs, outputs and errors (constants have `#:` comments). `public-surface.json` lists them; `tests/llm-kit/test_formats.py` and the graph surface tests compare the list with each module's `__all__`.
2. Semver release with a changelog: pass. Version 1.25.0 in `pyproject.toml`; this entry. Versions before 1.25.0 have no entries here; `packages/released.json` records their release commits.
3. Tests for each public function: pass. Each listed function is called in `tests/llm-kit/test_formats.py`, `test_project_models.py` or `test_project_models_local.py`.
4. No client names, prompts or data: pass for these pieces. The example and the shared cases use generic names.
5. Examples run without a client app: pass. `tests/packaging/test_examples.py` installs the built wheels in a clean venv and runs each example.
6. JSON Schema for each file format: pass. The three schemas ship in `llm_kit.schema.formats`. The shared-case tests validate reader output against them, and the writer tests (`render_yaml`, `render_local_yaml`, `write_project_models`, `write_local_overrides`, `set_local_override`, `graph.testing.make_project`) validate written files against them.
7. Gate before adoption: recorded in this entry.
