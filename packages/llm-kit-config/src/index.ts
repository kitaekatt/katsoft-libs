/**
 * Readers for the llm-kit shared file formats: the project model file, its
 * overlays, and prompt files. The rules match the Python reader in
 * katsoft-llm-kit; the shared cases under `tests/formats/` in the
 * katsoft-libs repository fix them. Contract: `docs/reference/llm-kit-formats.md`.
 *
 * @packageDocumentation
 */

export { LlmKitConfigError } from "./errors.js";
export type { LlmKitConfigErrorCode } from "./errors.js";
export { applyOverlay, findSelection, loadOverlay, loadRegistry, parseOverlay, parseRegistry, select } from "./registry.js";
export type {
  CatalogRow, JsonValue, Overlay, OverlayDocument, ParseOptions, ProjectModelsDocument, Registry, Resolved,
  ResolvedSelection, Selection, SelectionSource,
} from "./registry.js";
export { loadPrompt, parsePrompt, renderPrompt } from "./prompt.js";
export type { PromptFile, RenderOptions } from "./prompt.js";
export { SCHEMA_IDS } from "./schema-ids.js";
