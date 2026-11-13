/**
 * The format ids this package reads.
 *
 * @module
 */

/**
 * The format id of each file format this package reads. Each id is the `$id`
 * of the JSON Schema that ships in this package's `schemas/` directory
 * (`@katsoft/llm-kit-config/schemas/<file>`), copied from katsoft-llm-kit.
 *
 * - `projectModels`: the tracked project model file (`project-models.v1.schema.json`).
 * - `projectModelsLocal`: the overlay, in the personal-layer format (`project-models-local.v1.schema.json`).
 * - `promptFile`: the parsed form of a prompt file (`prompt-file.v1.schema.json`).
 */
export const SCHEMA_IDS = Object.freeze({
  projectModels: "llm-kit.project-models/v1",
  projectModelsLocal: "llm-kit.project-models-local/v1",
  promptFile: "llm-kit.prompt-file/v1",
} as const);
