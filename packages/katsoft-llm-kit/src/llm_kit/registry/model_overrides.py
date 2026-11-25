"""User usage policy for declarations and consult seats; reads never create files.

Only the user file is read. Project configuration cannot replace this policy.
YAML parsing needs the ``config`` extra only when a file exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from llm_kit.registry.conserve import Budget
from llm_kit.registry.user_paths import model_overrides_file

__all__ = ["ModelOverride", "ModelOverrides", "load_model_overrides"]


@dataclass(frozen=True)
class ModelOverride:
    """Resolved boolean switches for an entry; disabled takes precedence.

    Inputs are ``disabled`` and ``reset_mode`` booleans (default false).
    Construct through ModelOverrides.resolve for validated user policy.
    No I/O or errors; to_json returns the effective switches.
    """

    disabled: bool = False
    reset_mode: bool = False

    def budget_usable(self, budget: Budget | None) -> bool:
        """Whether a budget permits selection under these switches.

        ``None`` is unpaced. Reset bypasses measured exhaustion, while an
        observed dispatch halt still blocks selection. Returns bool; no errors.
        Reachability, consent, requirements and caller exclusions are separate.
        """
        return not self.disabled and (
            budget is None or budget.usable or (self.reset_mode and not budget.observed_halt)
        )

    def to_json(self) -> dict[str, bool]:
        """Return effective switches as JSON data; no inputs or errors."""
        return {"disabled": self.disabled, "reset_mode": self.reset_mode and not self.disabled}


@dataclass(frozen=True)
class ModelOverrides:
    """Validated user policy and its source path, returned by load_model_overrides.

    ``harnesses`` and ``models`` contain partial boolean records; ``path`` is
    the file read, including when absent. Resolve combines fields individually.
    Construction performs no I/O; malformed files raise at the loader boundary.
    """

    path: Path
    harnesses: Mapping[str, Mapping[str, bool]]
    models: Mapping[str, Mapping[str, bool]]

    def resolve(self, entry_id: str, harness: str | None = None) -> ModelOverride:
        """Return effective switches for an id and optional harness; no errors.

        Model fields replace harness fields individually. Unset fields are
        false; disabled suppresses reset even when reset was explicitly true.
        """
        fields = dict(self.harnesses.get((harness or "").lower(), {}))
        fields.update(self.models.get(entry_id, {}))
        disabled = fields.get("disabled", False)
        return ModelOverride(disabled, fields.get("reset_mode", False) and not disabled)

    def to_json(self, entries: Mapping[str, Any]) -> dict[str, Any]:
        """Return path, configured fields and resolved policy for registry entries.

        ``entries`` maps ids to objects with an optional harness attribute.
        Model-only ids absent from the registry are included too. No probes,
        writes or errors; the result is JSON data with an id-keyed models map.
        """
        return {
            "path": str(self.path),
            "schema_version": 1,
            "configured": {"harnesses": dict(self.harnesses), "models": dict(self.models)},
            "models": {
                name: self.resolve(name, getattr(entries.get(name), "harness", None)).to_json()
                for name in sorted(set(entries) | set(self.models))
            },
        }


def load_model_overrides() -> ModelOverrides:
    """Read the user usage policy, resolving the path on every call.

    No arguments. Uses LLM_KIT_MODEL_OVERRIDES_FILE, else the neutral user
    config directory. Returns empty ModelOverrides when the file is missing.
    Raises ValueError naming the file and invalid field for malformed YAML,
    unknown keys, non-object scopes or non-boolean switches; OSError for other
    read failures. Reads create nothing and never consult project policy.
    """
    path = model_overrides_file()
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return ModelOverrides(path, {}, {})
    from llm_kit.registry.project_models import ProjectModelsError, parse_yaml_document

    try:
        document = parse_yaml_document(content)
    except ProjectModelsError as exc:
        raise ValueError(f"{path}: invalid model overrides: {exc}") from exc
    _check_document(document, path)
    return ModelOverrides(path, document.get("harnesses", {}), document.get("models", {}))


def _check_document(document: Any, path: Path) -> None:
    """Check the schema at the registry boundary, without a schema-component edge."""
    def refuse(where: str, message: str) -> None:
        raise ValueError(f"{path}: invalid model overrides at {where}: {message}")

    if not isinstance(document, dict):
        refuse("/", "expected an object")
    unknown = set(document) - {"schema_version", "harnesses", "models"}
    if unknown:
        refuse("/", f"unknown keys: {sorted(map(str, unknown))}")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        refuse("schema_version", "expected integer 1")
    for scope in ("harnesses", "models"):
        if scope not in document:
            continue
        if not isinstance(document[scope], dict):
            refuse(scope, "expected an object")
        for name, fields in document[scope].items():
            where = f"{scope}.{name}"
            if not isinstance(name, str) or not name.strip():
                refuse(where, "expected a non-empty text id")
            if not isinstance(fields, dict):
                refuse(where, "expected an object")
            unknown = set(fields) - {"disabled", "reset_mode"}
            if unknown:
                refuse(where, f"unknown keys: {sorted(map(str, unknown))}")
            for field, value in fields.items():
                if type(value) is not bool:
                    refuse(f"{where}.{field}", "expected a boolean")
