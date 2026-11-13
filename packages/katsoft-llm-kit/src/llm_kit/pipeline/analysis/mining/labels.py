"""Open labels: one spelling per label, and the inventory a taxonomy call reads."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_SPACE = re.compile(r"\s+")
_EDGE = " \t\r\n.,;:!?\"'`"


def normalize_label(text: str) -> str:
    """Lower case, inner whitespace collapsed, edge punctuation and quotes removed. An empty result raises."""
    label = _SPACE.sub(" ", text.lower()).strip(_EDGE)
    if not label:
        raise ValueError(f"label {text!r} is empty after normalizing")
    return label


@dataclass(frozen=True)
class LabelCount:
    """One normalized label and how many times it occurred."""

    label: str
    count: int


def label_inventory(labels: Iterable[str]) -> list[LabelCount]:
    """Each normalized label with its count, most frequent first, then by label. A label that is empty after
    normalizing raises ``ValueError``."""
    counts: dict[str, int] = {}
    for text in labels:
        label = normalize_label(text)
        counts[label] = counts.get(label, 0) + 1
    return [LabelCount(label, n) for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


__all__ = ["LabelCount", "label_inventory", "normalize_label"]
