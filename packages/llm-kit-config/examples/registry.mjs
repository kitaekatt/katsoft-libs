// Read a project model file and an overlay, layer them, and select a model.
// Runs with plain `node` after `npm install @katsoft/llm-kit-config`.
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { LlmKitConfigError, applyOverlay, loadOverlay, loadRegistry, parseOverlay, select } from "@katsoft/llm-kit-config";

const directory = mkdtempSync(join(tmpdir(), "llm-kit-config-example-"));
try {
  const tracked = join(directory, "models.yaml");
  writeFileSync(tracked, [
    "version: 1",
    "catalog:",
    "  - id: fast",
    "    label: Fast",
    "    endpoint: api",
    "    model: vendor/fast-1",
    "    efforts: [low, high]",
    "    max_output_tokens: 4096",
    "  - id: careful",
    "    label: Careful",
    "    endpoint: api",
    "    model: vendor/careful-2",
    "    request_defaults:",
    "      extras: {temperature: 0.2}",
    "selections:",
    "  summary.write: {model: fast, effort: high}",
    "  summary.check: {model: careful}",
    "",
  ].join("\n"));
  const local = join(directory, "staging.yaml");
  writeFileSync(local, "version: 1\nselections:\n  summary.write: {model: fast, effort: low}\n");

  const registry = applyOverlay(loadRegistry(tracked), loadOverlay(local));
  for (const key of Object.keys(registry.resolved)) {
    const { row, effort, source } = select(registry, key);
    console.log(`${key}: ${row.endpoint} ${row.model} effort=${effort ?? "none"} (${source})`);
  }

  try {
    applyOverlay(registry, parseOverlay("version: 1\nselections:\n  summary.write: {model: missing}\n", { source: "bad.yaml" }));
  } catch (error) {
    if (!(error instanceof LlmKitConfigError)) throw error;
    console.log(`refused: code=${error.code} where=${error.where} source=${error.source}`);
  }
} finally {
  rmSync(directory, { recursive: true, force: true });
}
