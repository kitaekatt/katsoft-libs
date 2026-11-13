// Compiled by scripts/check-pack.mjs against the installed tarball's .d.ts files
// (TypeScript 5.8, module nodenext). It is never run.
import {
  LlmKitConfigError, SCHEMA_IDS, applyOverlay, findSelection, loadOverlay, loadPrompt, loadRegistry, parseOverlay,
  parsePrompt, parseRegistry, renderPrompt, select,
} from "@katsoft/llm-kit-config";
import type {
  CatalogRow, JsonValue, LlmKitConfigErrorCode, Overlay, OverlayDocument, ParseOptions, ProjectModelsDocument,
  PromptFile, Registry, RenderOptions, Resolved, ResolvedSelection, Selection, SelectionSource,
} from "@katsoft/llm-kit-config";

const options: ParseOptions = { source: "models.yaml" };
const registry: Registry = parseRegistry("version: 1\n", options);
const loaded: Registry = loadRegistry("models.yaml");
const overlay: Overlay = parseOverlay(new Uint8Array(), {});
const overlayDocument: OverlayDocument = loadOverlay("models.local.yaml").document;
const layered: Registry = applyOverlay(registry, overlay, overlay);
const document: ProjectModelsDocument = layered.document;
const row: CatalogRow | undefined = document.catalog[0];
const tokens: number | undefined = row?.max_output_tokens;
const extras: JsonValue | undefined = row?.request_defaults?.extras?.["x"];
const resolved: Resolved = select(loaded, "k");
const maybe: Resolved | undefined = findSelection(loaded, "k");
const entry: ResolvedSelection | undefined = layered.resolved["k"];
const selection: Selection | null = entry?.default ?? null;
const source: SelectionSource = resolved.source;
const effort: string | null = resolved.effort;
const prompt: PromptFile = parsePrompt("Hi {{who}}");
const fromFile: PromptFile = loadPrompt("p.md");
const renderOptions: RenderOptions = { suffix: "\n" };
const text: string = renderPrompt(prompt, { who: "you" }, renderOptions);
const id: "llm-kit.project-models/v1" = SCHEMA_IDS.projectModels;
try {
  select(registry, "absent");
} catch (error) {
  if (error instanceof LlmKitConfigError) {
    const code: LlmKitConfigErrorCode = error.code;
    const where: string = error.where;
    const from: string | undefined = error.source;
    void [code, where, from];
  }
}
void [overlayDocument, tokens, extras, maybe, selection, source, effort, fromFile, text, id];
