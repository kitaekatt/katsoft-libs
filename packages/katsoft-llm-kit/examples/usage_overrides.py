"""Inspect usage overrides with no harness or client app.

Install katsoft-llm-kit[config], then run this file with plain Python.
It uses a temporary policy file and does not read or write user configuration.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from llm_kit.registry.model_overrides import load_model_overrides


def main() -> None:
    """Resolve one harness reset and one model disable; errors propagate."""
    variable = "LLM_KIT_MODEL_OVERRIDES_FILE"
    previous = os.environ.get(variable)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "model-overrides.yaml"
            path.write_text(
                "schema_version: 1\nharnesses:\n  codex: {reset_mode: true}\n"
                "models:\n  paused: {disabled: true}\n",
                encoding="utf-8",
            )
            os.environ[variable] = str(path)
            policy = load_model_overrides()
            print(json.dumps({
                "worker": policy.resolve("worker", "codex").to_json(),
                "paused": policy.resolve("paused", "codex").to_json(),
            }))
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


if __name__ == "__main__":
    main()
