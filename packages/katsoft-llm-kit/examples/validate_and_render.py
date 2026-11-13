"""Validate a project model file and render a prompt file with katsoft-llm-kit.

Run it in a plain Python environment; it needs no AI harness, plugin, client
app, endpoint registry or network:

    python -m venv .venv
    .venv/bin/python -m pip install "katsoft-llm-kit[config]"
    .venv/bin/python validate_and_render.py

The script reads models.yaml and summary.prompt.md beside it. It checks each
against the format's JSON Schema, runs the portable validation of the project
model file, resolves one selection, and fills the prompt. It exits non-zero
with a message when a check fails.
"""

from __future__ import annotations

from pathlib import Path

from llm_kit.schema.formats import PROJECT_MODELS_V1, PROMPT_FILE_V1, load_schema
from llm_kit.graph.prompt import read_prompt_file, render_prompt_file
from llm_kit.registry.project_models import parse_yaml_document, validate_portable_document
from llm_kit.schema.json_schema import validate

HERE = Path(__file__).resolve().parent


def check_schema(format_id: str, data: object, name: str) -> None:
    problems = validate(load_schema(format_id), data)
    if problems:
        raise SystemExit(f"{name} does not match the {format_id} schema: {list(problems)}")


def main() -> int:
    data = parse_yaml_document((HERE / "models.yaml").read_bytes())
    check_schema(PROJECT_MODELS_V1, data, "models.yaml")
    document = validate_portable_document(data)
    pick = document["selections"]["summary.write"]
    row = next(r for r in document["catalog"] if r["id"] == pick["model"])
    print(f"summary.write: endpoint {row['endpoint']}, model {row['model']}, effort {pick['effort']}")

    prompt_path = HERE / "summary.prompt.md"
    prompt = read_prompt_file(prompt_path)
    check_schema(PROMPT_FILE_V1, prompt.to_json(), "summary.prompt.md")
    print(f"prompt slots: {', '.join(prompt.slots)}; body sha256 {prompt.sha256}")
    text = render_prompt_file(
        prompt_path,
        {"audience": "a new team member", "text": "The release moved to Friday. Tests pass."},
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
