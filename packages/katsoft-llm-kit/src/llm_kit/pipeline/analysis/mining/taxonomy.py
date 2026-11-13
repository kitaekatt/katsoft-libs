"""A taxonomy: the families a model merged open labels into, applied by code.

The model answer is ``{"families": [{"id", "name", "definition", "labels": [...]}]}``. Code, not the model, maps each
label to its family, so the counts that follow are exact. A label the answer does not place maps to ``OTHER``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .labels import normalize_label

#: The family of a label that no family lists.
OTHER = "other"


class TaxonomyError(ValueError):
    """The model answer is not a usable taxonomy (a missing field, a repeated id, a label in two families)."""


@dataclass(frozen=True)
class Family:
    """One family of a taxonomy: its id, display name, one-sentence definition and normalized labels."""

    id: str
    name: str
    definition: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    """Checked label families with a label -> family index. Build it with ``from_answer``; ``family_of`` maps a
    label (any spelling ``normalize_label`` accepts) to its family id or ``OTHER``."""

    families: tuple[Family, ...]
    _index: Mapping[str, str]

    @classmethod
    def from_answer(cls, answer: Mapping[str, Any]) -> "Taxonomy":
        """Check the answer and index its labels (normalized). Every fault is listed in one ``TaxonomyError``."""
        problems: list[str] = []
        families: list[Family] = []
        index: dict[str, str] = {}
        for position, raw in enumerate(answer.get("families") or []):
            missing = [k for k in ("id", "name", "definition", "labels") if k not in raw]
            if missing:
                problems.append(f"family {position}: missing {', '.join(missing)}")
                continue
            family_id = str(raw["id"])
            if family_id == OTHER or any(f.id == family_id for f in families):
                problems.append(f"family id {family_id!r} is reserved or repeated")
                continue
            labels = tuple(normalize_label(label) for label in raw["labels"])
            for label in labels:
                if index.get(label, family_id) != family_id:
                    problems.append(f"label {label!r} is in {index[label]!r} and {family_id!r}")
                index[label] = family_id
            families.append(Family(family_id, str(raw["name"]), str(raw["definition"]), labels))
        if not families and not problems:
            problems.append("the answer has no families")
        if problems:
            raise TaxonomyError("; ".join(problems))
        return cls(tuple(families), index)

    def family_of(self, label: str) -> str:
        """The family id of ``label`` (normalized first), or ``OTHER``. A label empty after normalizing raises ``ValueError``."""
        return self._index.get(normalize_label(label), OTHER)

    def coverage(self, counts: Iterable[tuple[str, int]]) -> float:
        """The share of label occurrences (``(label, count)`` pairs) that a family places. 0.0 for no occurrences."""
        total = placed = 0
        for label, n in counts:
            total += n
            if self.family_of(label) != OTHER:
                placed += n
        return placed / total if total else 0.0


__all__ = ["OTHER", "Family", "Taxonomy", "TaxonomyError"]
