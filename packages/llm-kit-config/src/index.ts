/**
 * Readers for the llm-kit shared file formats: the project model file, its
 * overlays, and prompt files; and the request a selection resolves to. The rules match the Python reader in
 * katsoft-llm-kit; the shared cases under `tests/formats/` in the
 * katsoft-libs repository fix them. Contract: `docs/reference/llm-kit-formats.md`.
 *
 * @packageDocumentation
 */

export { LlmKitConfigError } from "./errors.js";
export type { LlmKitConfigErrorCode } from "./errors.js";
export { applyOverlay, findSelection, loadOverlay, loadRegistry, parseOverlay, parseRegistry, select } from "./registry.js";
export type {
  CatalogRow, EffortStyle, JsonValue, Overlay, OverlayDocument, ParseOptions, ProjectModelsDocument, Registry,
  RequestDefaults, RequestProfile, Resolved, ResolvedSelection, Selection, SelectionSource,
} from "./registry.js";
export { chatFields, resolveRequest } from "./request.js";
export type { ChatFieldsOptions, RequestCall, ResolvedRequest } from "./request.js";
export { loadPrompt, parsePrompt, renderPrompt } from "./prompt.js";
export type { PromptFile, RenderOptions } from "./prompt.js";
export { SCHEMA_IDS } from "./schema-ids.js";
